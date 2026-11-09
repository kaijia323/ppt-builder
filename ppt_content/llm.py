"""模型调用层：OpenAI 兼容接口（默认 DeepSeek），只用标准库发 HTTP，便于移植。

每次调用都会：
  1. 用 prompts/<task>.md 渲染提示词（记录模板哈希）
  2. 发送请求；网络错误/429/5xx 指数退避重试
  3. 解析 JSON -> Schema 校验 -> 语义校验（由调用方传入 validator）
  4. 不合格则把问题清单反馈给模型重新输出（max_semantic_retries 次）
  5. 每一次尝试都完整写入 logs/llm/<call_id>.json，并在 llm_calls.jsonl 记一行摘要
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .core import SKILL_ROOT, PipelineError, EXIT_CONFIG, now_iso, sha256_text, read_json

_TEMPLATE_CACHE = {}
_SCHEMA_CACHE = {}
_ADAPT = {}           # 运行中自动适配：某档位不支持 json_mode / thinking 时记录下来
_ADAPT_LOCK = threading.Lock()


def load_template(name):
    if name not in _TEMPLATE_CACHE:
        p = os.path.join(SKILL_ROOT, "prompts", name + ".md")
        if not os.path.exists(p):
            raise PipelineError("缺少提示词模板 prompts/%s.md" % name, EXIT_CONFIG)
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read()
        _TEMPLATE_CACHE[name] = (txt, sha256_text(txt))
    return _TEMPLATE_CACHE[name]


def load_schema(name):
    if name not in _SCHEMA_CACHE:
        p = os.path.join(SKILL_ROOT, "schemas", name + ".json")
        _SCHEMA_CACHE[name] = read_json(p) if os.path.exists(p) else None
    return _SCHEMA_CACHE[name]


def render(template, variables):
    def rep(m):
        k = m.group(1)
        if k not in variables:
            raise PipelineError("提示词变量缺失: %s" % k)
        v = variables[k]
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, indent=1)
        return str(v)
    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", rep, template)


REPAIR_NOTE = "JSON 结构不完整（缺少右括号），已自动补齐后解析"


def _balance(t):
    """扫描括号（忽略字符串内的内容），返回 (未闭合的括号栈, 是否有多余的右括号)。"""
    stack, in_str, esc, extra = [], False, False, False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            else:
                extra = True
    return stack, extra, in_str


def parse_json_text(text, return_meta=False):
    """从模型正文中取出 JSON：支持纯 JSON、```json 代码块、前后有说明文字；
    长输出偶尔漏写右括号时，自动补齐后再解析（return_meta=True 时返回是否修复）。"""
    def ret(d, repaired=False):
        return (d, repaired) if return_meta else d
    if text is None:
        raise ValueError("空输出")
    t = text.strip()
    try:
        return ret(json.loads(t))
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", t, re.S)
    if m:
        try:
            return ret(json.loads(m.group(1)))
        except Exception:
            t = m.group(1)
    start = t.find("{")
    if start < 0:
        raise ValueError("未找到可解析的 JSON")
    body = t[start:]
    # 1) 取第一个完整闭合的对象
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(body):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return ret(json.loads(body[:i + 1]))
                except Exception:
                    break
    # 2) 括号不闭合：去掉结尾非 JSON 文本后按栈补齐右括号
    cand = body.rstrip()
    cand = re.sub(r"[^\]\}\"0-9a-zA-Z]+$", "", cand)
    stack, extra, open_str = _balance(cand)
    if not open_str and stack:
        fixed = cand.rstrip().rstrip(",") + "".join("}" if c == "{" else "]" for c in reversed(stack))
        try:
            return ret(json.loads(fixed), True)
        except Exception:
            pass
    raise ValueError("未找到可解析的 JSON")


def schema_errors(data, schema):
    if not schema:
        return []
    try:
        import jsonschema
    except ImportError:
        return []
    v = jsonschema.Draft7Validator(schema)
    errs = []
    for e in sorted(v.iter_errors(data), key=lambda e: list(e.path))[:15]:
        loc = "/".join(str(x) for x in e.path) or "(根)"
        errs.append("Schema 不符 @ %s: %s" % (loc, e.message[:200]))
    return errs


LEGACY_KEY_ENV = {"DEEPSEEK_API": "DEEPSEEK_API_KEY"}     # 0.7.0-dev24 改名前的变量名，已设置旧名的电脑不用重设


def api_key(lc):
    """按 config 的 api_key_env 读取 API Key；没设置时再看改名前的旧变量名。只读环境变量，不写入任何文件或日志。"""
    name = lc.get("api_key_env", "DEEPSEEK_API")
    return os.environ.get(name) or os.environ.get(LEGACY_KEY_ENV.get(name, ""), "") or None


class LLM:
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.lc = cfg["llm"]
        self.log = logger
        self.provider = self.lc.get("provider", "deepseek")
        if self.provider != "mock":
            self.api_key = api_key(self.lc)
            if not self.api_key:
                raise PipelineError("未找到环境变量 %s（API Key）。也可设置 PPTC_LLM_PROVIDER=mock 做离线流程测试。"
                                    % self.lc.get("api_key_env"), EXIT_CONFIG)
        self.system_tpl = load_template("system")

    # ------------------------------------------------------------ profile
    def profile_for(self, task):
        pname = self.lc.get("tasks", {}).get(task, "write")
        prof = dict(self.lc["profiles"][pname])
        prof["_name"] = pname
        with _ADAPT_LOCK:
            for k, v in _ADAPT.get(pname, {}).items():
                prof[k] = v
        return prof

    def _body(self, prof, messages):
        body = {"model": prof["model"], "messages": messages, "stream": False}
        if prof.get("max_tokens"):
            body["max_tokens"] = prof["max_tokens"]
        if prof.get("temperature") is not None:
            body["temperature"] = prof["temperature"]
        if prof.get("json_mode"):
            body["response_format"] = {"type": "json_object"}
        th = prof.get("thinking")
        if th in ("enabled", "disabled") and not prof.get("_no_thinking_param"):
            body["thinking"] = {"type": th}
            if th == "enabled" and prof.get("reasoning_effort"):
                body["reasoning_effort"] = prof["reasoning_effort"]
        return body

    # ------------------------------------------------------------ http
    def _http(self, body, call_id):
        url = self.lc["base_url"].rstrip("/") + "/chat/completions"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        tries = int(self.lc.get("max_http_retries", 4))
        last = None
        http_log = []
        for i in range(tries + 1):
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + self.api_key})
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=float(self.lc.get("timeout_s", 300))) as r:
                    raw = r.read().decode("utf-8")
                    http_log.append({"try": i, "status": r.status, "ms": int((time.time() - t0) * 1000)})
                    return json.loads(raw), http_log
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")[:2000]
                http_log.append({"try": i, "status": e.code, "ms": int((time.time() - t0) * 1000), "body": txt})
                last = (e.code, txt)
                if e.code in (429, 500, 502, 503, 504) and i < tries:
                    wait = min(60, 2 ** i * 2)
                    self.log.warn("http_retry", "%s HTTP %s，%ss 后重试" % (call_id, e.code, wait))
                    time.sleep(wait)
                    continue
                raise HTTPFail(e.code, txt, http_log)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                http_log.append({"try": i, "status": None, "ms": int((time.time() - t0) * 1000), "error": repr(e)})
                last = (None, repr(e))
                if i < tries:
                    wait = min(60, 2 ** i * 2)
                    self.log.warn("http_retry", "%s 网络错误 %r，%ss 后重试" % (call_id, e, wait))
                    time.sleep(wait)
                    continue
                raise HTTPFail(None, repr(e), http_log)
        raise HTTPFail(last[0] if last else None, last[1] if last else "unknown", http_log)

    def _adapt_on_400(self, prof, err_text):
        """服务端不支持某参数时自动去掉并记录（例如 thinking 模式不支持 json_object）。"""
        t = (err_text or "").lower()
        changed = {}
        if prof.get("json_mode") and ("response_format" in t or "json" in t):
            changed["json_mode"] = False
        elif "thinking" in t or "reasoning_effort" in t:
            changed["_no_thinking_param"] = True
        elif "temperature" in t and prof.get("temperature") is not None:
            changed["temperature"] = None
        if changed:
            with _ADAPT_LOCK:
                _ADAPT.setdefault(prof["_name"], {}).update(changed)
            self.log.warn("param_adapt", "档位 %s 自动调整参数 %s（服务端返回 400）" % (prof["_name"], changed),
                          error=err_text[:500])
        return changed

    # ------------------------------------------------------------ main
    def call_json(self, task, variables, target="-", validator=None, vote=0, template=None):
        """返回 dict: data(最后一次可解析结果或None), ok, issues(残留问题), attempts, call_ids, reasoning"""
        tpl_name = template or task
        tpl, tpl_hash = load_template(tpl_name)
        sys_txt, sys_hash = self.system_tpl
        user_txt = render(tpl, variables)
        messages = [{"role": "system", "content": sys_txt}, {"role": "user", "content": user_txt}]
        schema = load_schema(task)
        max_sem = int(self.lc.get("max_semantic_retries", 2))
        call_ids, last_data, last_issues, reasoning = [], None, ["未调用"], None
        safe_target = re.sub(r"[^A-Za-z0-9_.-]", "_", str(target))[:80]

        for attempt in range(1, max_sem + 2):
            prof = self.profile_for(task)
            call_id = "%s.%s.v%d.a%d.%s" % (task, safe_target, vote, attempt, self.log.invocation_id[-6:])
            call_ids.append(call_id)
            t0 = time.time()
            started = now_iso()
            raw, http_log, content, reasoning, finish, usage, err = None, [], None, None, None, {}, None
            json_repaired = False
            body = self._body(prof, messages)
            try:
                if self.provider == "mock":
                    from . import mock_llm
                    content, usage = mock_llm.respond(task, variables, attempt, vote)
                    raw = {"mock": True}
                    finish = "stop"
                else:
                    try:
                        raw, http_log = self._http(body, call_id)
                    except HTTPFail as hf:
                        if hf.status == 400 and self._adapt_on_400(prof, hf.text):
                            prof = self.profile_for(task)
                            body = self._body(prof, messages)
                            raw, http_log2 = self._http(body, call_id)
                            http_log = hf.http_log + http_log2
                        else:
                            raise
                    ch = (raw.get("choices") or [{}])[0]
                    msg = ch.get("message") or {}
                    content = msg.get("content")
                    reasoning = msg.get("reasoning_content")
                    finish = ch.get("finish_reason")
                    u = raw.get("usage") or {}
                    usage = {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
                             "prompt_cache_hit_tokens": u.get("prompt_cache_hit_tokens"),
                             "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens")}
            except HTTPFail as hf:
                err = "HTTP %s: %s" % (hf.status, hf.text[:500])
                http_log = hf.http_log
            except Exception as e:  # noqa
                err = "调用异常: %r" % e

            data, issues = None, []
            if err:
                issues = [err]
            else:
                if finish == "length":
                    issues.append("输出被截断（finish_reason=length），请精简输出或减少条目")
                try:
                    data, repaired = parse_json_text(content, return_meta=True)
                    if repaired:
                        json_repaired = True
                        self.log.warn("json_repaired", "%s：%s" % (call_id, REPAIR_NOTE))
                except Exception as e:
                    issues.append("JSON 解析失败: %s" % e)
                if data is not None:
                    issues += schema_errors(data, schema)
                    if not issues and validator:
                        try:
                            issues += validator(data) or []
                        except Exception as e:
                            issues.append("校验器异常: %r" % e)
            latency = int((time.time() - t0) * 1000)
            cost = self._cost(prof["model"], usage)
            summary = {"call_id": call_id, "ts": started, "inv": self.log.invocation_id, "stage": self.log.stage, "task": task, "target": str(target),
                       "vote": vote, "attempt": attempt, "model": prof["model"], "profile": prof["_name"],
                       "thinking": prof.get("thinking"), "json_mode": bool(prof.get("json_mode")),
                       "prompt_template": tpl_name, "prompt_hash": tpl_hash, "system_hash": sys_hash,
                       "prompt_chars": sum(len(m["content"]) for m in messages),
                       "output_chars": len(content or ""), "reasoning_chars": len(reasoning or ""),
                       "latency_ms": latency, "finish_reason": finish, "usage": usage, "cost": cost,
                       "ok": not issues, "issues": issues[:20], "http_tries": len(http_log), "json_repaired": json_repaired}
            full = dict(summary)
            full["request_params"] = {k: v for k, v in body.items() if k != "messages"}
            if self.cfg.get("logging", {}).get("save_full_messages", True):
                full["messages"] = messages
            full["content"] = content
            if self.cfg.get("logging", {}).get("save_reasoning", True):
                full["reasoning_content"] = reasoning
            full["parsed"] = data
            full["http_log"] = http_log
            full["raw_response_meta"] = {k: v for k, v in (raw or {}).items() if k != "choices"} if isinstance(raw, dict) else None
            self.log.llm_call(summary, full)

            if data is not None:
                last_data = data
            last_issues = issues
            if not issues:
                return {"data": data, "ok": True, "issues": [], "attempts": attempt, "call_ids": call_ids,
                        "reasoning": reasoning}
            if err and "HTTP 4" in err and "HTTP 429" not in err:
                break  # 客户端错误（鉴权、参数等）不再重试
            # 带反馈重试
            if content:
                messages = messages + [{"role": "assistant", "content": content}]
            fb = render(load_template("feedback")[0], {"issues": "\n".join("- " + i for i in issues[:20])})
            messages = messages + [{"role": "user", "content": fb}]
            self.log.warn("semantic_retry", "%s 第 %d 次输出不合格（%d 个问题），带反馈重试" % (call_id, attempt, len(issues)),
                          issues=issues[:10])
        self.log.error("llm_failed", "%s/%s 多次尝试后仍不合格" % (task, target), issues=last_issues[:10])
        return {"data": last_data, "ok": False, "issues": last_issues, "attempts": len(call_ids),
                "call_ids": call_ids, "reasoning": reasoning}

    def _cost(self, model, usage):
        p = (self.lc.get("pricing") or {}).get(model) or {}
        if not usage or p.get("input") is None or p.get("output") is None:
            return None
        return round(((usage.get("prompt_tokens") or 0) * p["input"] + (usage.get("completion_tokens") or 0) * p["output"]) / 1e6, 6)

    # ------------------------------------------------------------ parallel
    def map(self, fn, items, label="items"):
        workers = max(1, int(self.lc.get("max_workers", 4)))
        results = [None] * len(items)

        def run(i):
            try:
                results[i] = fn(items[i])
            except Exception as e:
                import traceback
                self.log.error("worker_error", "%s[%d] 处理异常: %r" % (label, i, e), trace=traceback.format_exc()[-3000:])
                results[i] = e
        if workers == 1 or len(items) <= 1:
            for i in range(len(items)):
                run(i)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(run, range(len(items))))
        return results


class HTTPFail(Exception):
    def __init__(self, status, text, http_log):
        super().__init__("HTTP %s" % status)
        self.status, self.text, self.http_log = status, text, http_log
