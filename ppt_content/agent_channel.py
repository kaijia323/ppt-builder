"""provider: agent —— 模型调用由会话里的智能体作答，整个过程不发起任何网络请求。

机制（0.8-dsh）：
  1. 流水线需要模型输出时，把「提示词 + schema + 输出路径」写成 job 文件：
       <workdir>/agent/jobs/<job_id>.json
     然后抛 NeedModel（退出码 4），本命令结束。除了这个 job 文件，不会调用任何模型。
  2. 智能体读 job → 按 schema 作答 → 把 JSON 写到：
       <workdir>/agent/answers/<job_id>.json
  3. 重跑同一条命令：已答的 job 原样复用，阶段继续推进到下一个未答 job；如此循环直到阶段完成。
  4. job_id = sha256(任务|目标|attempt|vote|完整 messages|schema)[:16]，
     所以重跑幂等；同一份提示词永远对应同一个 job，答案可回溯、可审计。

答案不合格（JSON 解析失败 / schema 不符 / 语义校验不过）时，走与 HTTP provider 相同的路径：
把问题清单反馈进 messages，attempt+1，生成一个新 job 让智能体重写。旧答案保留备查。
"""
import hashlib
import json
import os
import shlex
import sys
import threading
import time

from .core import SKILL_ROOT, NeedModel, now_iso, read_json, write_json
from .llm import load_schema, load_template, parse_json_text, render, schema_errors

ANSWER_HINT = (
    "把符合 schema 的 JSON 本体写到 answer_path（UTF-8，不要 ```json 代码块、不要任何解释文字），"
    "然后重跑 resume_command；已答的 job 会自动复用，流水线继续推进到下一个 job。"
)


def _job_id(task, target, vote, attempt, messages, schema):
    """同一份提示词 + 同一个目标 + 同一个 attempt 一定得到同一个 id（重跑幂等）。"""
    h = hashlib.sha256()
    for part in (task, str(target), str(vote), str(attempt),
                 json.dumps(messages, ensure_ascii=False, sort_keys=True),
                 json.dumps(schema, ensure_ascii=False, sort_keys=True) if schema else ""):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


class AgentLLM:
    """与 llm.LLM 同接口（call_json / profile_for / map），但把模型调用换成 job 文件。"""

    provider = "agent"

    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.lc = cfg["llm"]
        self.log = logger
        self.wd = logger.wd
        self.jobs_dir = self.wd.p("agent", "jobs")
        self.answers_dir = self.wd.p("agent", "answers")
        self._job_lock = threading.Lock()      # 并行分支可能算出同一个 job_id，写入要串行（D3）
        os.makedirs(self.jobs_dir, exist_ok=True)
        os.makedirs(self.answers_dir, exist_ok=True)
        self.system_tpl = load_template("system")
        self.max_attempts = max(1, int(self.lc.get("max_semantic_retries", 2)) + 1)

    # ------------------------------------------------------------ profile
    def profile_for(self, task):
        """agent 模式不选模型，但保留档位信息写进 job（提示智能体该不该深想）。"""
        pname = self.lc.get("tasks", {}).get(task, "write")
        prof = dict((self.lc.get("profiles") or {}).get(pname) or {})
        prof["_name"] = pname
        return prof

    # ------------------------------------------------------------ paths
    def job_path(self, job_id):
        return os.path.join(self.jobs_dir, job_id + ".json")

    def answer_path(self, job_id):
        return os.path.join(self.answers_dir, job_id + ".json")

    def resume_command(self):
        """给智能体直接可复制执行的命令：用当前解释器 + 技能目录的绝对路径（D1）。"""
        return "%s %s run --workdir %s" % (shlex.quote(sys.executable),
                                           shlex.quote(os.path.join(SKILL_ROOT, "run.py")),
                                           shlex.quote(self.wd.top))

    # ------------------------------------------------------------ main
    def call_json(self, task, variables, target="-", validator=None, vote=0, template=None):
        tpl_name = template or task
        tpl, tpl_hash = load_template(tpl_name)
        sys_txt, sys_hash = self.system_tpl
        user_txt = render(tpl, variables)
        messages = [{"role": "system", "content": sys_txt}, {"role": "user", "content": user_txt}]
        schema = load_schema(task)
        call_ids, last_data, last_issues, reasoning = [], None, ["未作答"], None

        for attempt in range(1, self.max_attempts + 1):
            prof = self.profile_for(task)
            job_id = _job_id(task, target, vote, attempt, messages, schema)
            call_ids.append(job_id)
            job_file, ans_file = self.job_path(job_id), self.answer_path(job_id)
            t0 = time.time()
            started = now_iso()

            with self._job_lock:
                fresh = not os.path.exists(job_file)
                if fresh:
                    write_json(job_file, {
                    "job_id": job_id,
                    "created_at": started,
                    "stage": self.log.stage,
                    "task": task,
                    "target": str(target),
                    "attempt": attempt,
                    "vote": vote,
                    "template": tpl_name,
                    "prompt_hash": tpl_hash,
                    "system_hash": sys_hash,
                    "profile": prof.get("_name"),
                    "thinking": prof.get("thinking"),
                    "schema": schema,
                    "answer_path": ans_file,
                    "job_path": job_file,
                    "resume_command": self.resume_command(),
                    "instructions": ANSWER_HINT,
                    "messages": messages,
                    })
            if fresh:
                self._log_call(task, target, vote, attempt, prof, tpl_name, tpl_hash, sys_hash,
                               messages, None, None, None, job_file, ans_file, ["等待智能体作答"], started)
                raise NeedModel([self._job_record(job_file, ans_file)])

            if not os.path.exists(ans_file):
                self._log_call(task, target, vote, attempt, prof, tpl_name, tpl_hash, sys_hash,
                               messages, None, None, None, job_file, ans_file, ["等待智能体作答"], started)
                raise NeedModel([self._job_record(job_file, ans_file)])

            data, issues, content = None, [], ""
            try:
                content = self._read_answer(ans_file)
            except Exception as e:      # 非 UTF-8（如 Windows PowerShell 的 UTF-16）、路径是目录等
                issues.append("答案文件无法读取（需 UTF-8 编码的 JSON 文件）: %r" % e)
            if content:
                try:
                    data = parse_json_text(content)
                except Exception as e:
                    issues.append("JSON 解析失败: %s" % e)
            if not issues and data is None:
                issues.append("答案为空：必须输出 JSON 对象（不能是 null 或空文件）")
            if data is not None:
                issues += schema_errors(data, schema)
                if not issues and validator:
                    try:
                        issues += validator(data) or []
                    except Exception as e:
                        issues.append("校验器异常: %r" % e)
            if data is not None:
                last_data = data
            last_issues = issues
            self._log_call(task, target, vote, attempt, prof, tpl_name, tpl_hash, sys_hash,
                           messages, content, data, issues, job_file, ans_file, issues, started)

            if not issues:
                return {"data": data, "ok": True, "issues": [], "attempts": attempt,
                        "call_ids": call_ids, "reasoning": reasoning}
            if attempt >= self.max_attempts:
                break
            # 带问题清单重写：同一套与 HTTP provider 一致的反馈协议
            messages = messages + [{"role": "assistant", "content": content}]
            fb = render(load_template("feedback")[0], {"issues": "\n".join("- " + i for i in issues[:20])})
            messages = messages + [{"role": "user", "content": fb}]
            self.log.warn("agent_retry", "%s 第 %d 次作答不合格（%d 个问题），生成新的 job 重写"
                          % (task, attempt, len(issues)), issues=issues[:10])

        self.log.error("llm_failed", "%s/%s 多次作答后仍不合格" % (task, target), issues=last_issues[:10])
        return {"data": last_data, "ok": False, "issues": last_issues, "attempts": len(call_ids),
                "call_ids": call_ids, "reasoning": reasoning}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _read_answer(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _job_record(job_file, ans_file):
        job = read_json(job_file) or {}
        return {"job_id": job.get("job_id"), "task": job.get("task"), "target": job.get("target"),
                "attempt": job.get("attempt"), "job_path": job_file, "answer_path": ans_file,
                "resume_command": job.get("resume_command")}

    def _log_call(self, task, target, vote, attempt, prof, tpl_name, tpl_hash, sys_hash,
                  messages, content, data, issues, job_file, ans_file, issues_for_summary, started):
        summary = {"call_id": os.path.splitext(os.path.basename(job_file))[0], "ts": started,
                   "inv": self.log.invocation_id, "stage": self.log.stage, "task": task,
                   "target": str(target), "vote": vote, "attempt": attempt,
                   "model": "agent", "profile": prof.get("_name"), "thinking": prof.get("thinking"),
                   "json_mode": True, "prompt_template": tpl_name, "prompt_hash": tpl_hash,
                   "system_hash": sys_hash, "prompt_chars": sum(len(m["content"]) for m in messages),
                   "output_chars": len(content or ""), "reasoning_chars": 0, "latency_ms": 0,
                   "finish_reason": "agent", "usage": {}, "cost": None,
                   "ok": not issues_for_summary, "issues": list(issues_for_summary or [])[:20],
                   "http_tries": 0, "json_repaired": False,
                   "job_path": job_file, "answer_path": ans_file}
        full = dict(summary)
        full["messages"] = messages
        full["content"] = content
        full["parsed"] = data
        self.log.llm_call(summary, full)

    # ------------------------------------------------------------ parallel
    def map(self, fn, items, label="items"):
        """并行执行阶段任务；任一 job 缺失时先让其它分支也把 job 写出来，再统一抛 NeedModel。

        这样一轮可以同时拿到多个 job（例如各章的规划），减少来回次数。
        """
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, int(self.lc.get("max_workers", 4)))
        results = [None] * len(items)
        pending = []
        lock = threading.Lock()

        def run(i):
            try:
                results[i] = fn(items[i])
            except NeedModel as e:
                with lock:
                    pending.extend(e.jobs)
            except Exception as e:
                import traceback
                self.log.error("worker_error", "%s[%d] 处理异常: %r" % (label, i, e),
                               trace=traceback.format_exc()[-3000:])
                results[i] = e
        if workers == 1 or len(items) <= 1:
            for i in range(len(items)):
                run(i)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(run, range(len(items))))
        if pending:
            seen, uniq = set(), []
            for j in pending:
                if j.get("job_id") in seen:
                    continue
                seen.add(j.get("job_id"))
                uniq.append(j)
            raise NeedModel(uniq)
        return results

    # ------------------------------------------------------------ pending
    def _answer_issues(self, job):
        """只看 JSON 与 schema 判定当前答案能不能用（语义 validator 由重跑时的调用方判定）。"""
        job_id = job.get("job_id") or ""
        ans = os.path.join(self.answers_dir, job_id + ".json")
        if not os.path.exists(ans):
            return ["未作答"]
        try:
            content = self._read_answer(ans)
        except Exception as e:
            return ["答案文件无法读取: %r" % e]
        if not content.strip():
            return ["答案为空"]
        try:
            data = parse_json_text(content)
        except Exception as e:
            return ["JSON 解析失败: %s" % e]
        if data is None:
            return ["答案为空"]
        return schema_errors(data, job.get("schema")) or []

    def pending_jobs(self):
        """待作答（含答案不合格）的 job：每个 (task,target,vote) 只看最新一次尝试。

        这样写错的答案会继续显示为待作答，被新尝试取代的旧 job 不会永久挂着（D4）。
        """
        if not os.path.isdir(self.jobs_dir):
            return []
        latest = {}
        for fn in sorted(os.listdir(self.jobs_dir)):
            if not fn.endswith(".json") or fn.endswith(".tmp"):
                continue
            job = read_json(os.path.join(self.jobs_dir, fn))
            if not job:
                continue
            key = (job.get("task"), job.get("target"), job.get("vote", 0))
            if key not in latest or int(job.get("attempt") or 0) >= int(latest[key].get("attempt") or 0):
                latest[key] = job
        out = []
        for job in latest.values():
            issues = self._answer_issues(job)
            if not issues:
                continue
            rec = self._job_record(self.job_path(job["job_id"]), self.answer_path(job["job_id"]))
            rec["prompt_chars"] = len(json.dumps(job.get("messages") or [], ensure_ascii=False))
            rec["issues"] = issues[:5]
            out.append(rec)
        out.sort(key=lambda r: (r.get("task") or "", r.get("target") or ""))
        return out
