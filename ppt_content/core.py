"""公共基础：路径、读写、配置、状态、日志。

日志设计（全部在 <workdir>/logs/ 下，便于问题定位与对比分析）：
  invocations.jsonl  每次命令调用：参数、开始/结束时间、退出码、异常堆栈
  events.jsonl       结构化事件流：阶段开始/结束、计数、警告、错误（含 data 字段）
  run.log            人类可读的文本日志（与 events 对应）
  llm_calls.jsonl    每次模型调用一行摘要：任务、目标、模型、参数、耗时、token、重试、校验问题
  llm/<call_id>.json 每次模型调用的完整记录：完整 messages、原始返回、思考内容、解析结果
  gates.jsonl        每个质量关卡对每个对象的判定（通过/失败/原因）
  metrics.json       各阶段汇总指标（耗时、调用数、token、成本、通过率等）
  environment.json   运行环境、技能版本、配置快照（已脱敏）、提示词/Schema 哈希、输入文件哈希
"""
import datetime as _dt
import hashlib
import json
import os
import platform
import sys
import threading
import time
import traceback
import uuid

from . import VERSION

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 0.2：自上而下——先通读理解、再规划页面、再按页回原文取材写作、再编辑审查
STAGES = ["ingest", "understand", "plan", "write", "review", "check", "export", "design", "render"]

STAGE_DIRS = {
    "ingest": "01_ingest", "understand": "02_understand", "plan": "03_plan",
    "write": "04_write", "review": "05_review", "check": "06_check", "export": "07_export",
    "design": "08_visual", "render": "08_visual",
}

# 0 成功；1 运行错误；2 需要用户决定（检查点）；3 配置/环境/输入错误；4 需要智能体作答（provider: agent）
EXIT_OK, EXIT_ERROR, EXIT_NEED_USER, EXIT_CONFIG = 0, 1, 2, 3
EXIT_NEED_MODEL = 4


class PipelineError(Exception):
    def __init__(self, msg, code=EXIT_ERROR, data=None):
        super().__init__(msg)
        self.code = code
        self.data = data or {}


class NeedUser(Exception):
    """流水线需要用户决定时抛出，退出码 2。"""
    def __init__(self, msg, files=None):
        super().__init__(msg)
        self.files = files or []


class NeedModel(Exception):
    """provider: agent 时需要智能体作答：退出码 4。jobs 是待作答的 job 列表。"""
    def __init__(self, jobs):
        self.jobs = jobs or []
        super().__init__("需要智能体作答 %d 个 job" % len(self.jobs))

    @property
    def files(self):
        return [j.get("answer_path") for j in self.jobs if j.get("answer_path")] + \
               [j.get("job_path") for j in self.jobs if j.get("job_path")]


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as f:   # 兼容带 BOM 的文件（记事本/PowerShell 写入）
        return json.load(f)


_UMASK = None


def _default_file_mode():
    """按当前 umask 得到普通文件的默认权限（写 JSON 时恢复，避免 mkstemp 的 0600）。"""
    global _UMASK
    if _UMASK is None:
        _UMASK = os.umask(0)
        os.umask(_UMASK)
    return 0o666 & ~_UMASK


def _chmod(path, mode):
    """跨平台改权限：os.fchmod 只在 Unix 存在（Windows 上改为 os.chmod，仅只读位生效，不会崩）。"""
    try:
        os.chmod(path, mode)
    except (OSError, AttributeError):
        pass


def write_json(path, obj):
    """原子写：临时文件用 mkstemp（同目录、唯一名），再 os.replace。

    固定名 `<path>.tmp` 在多线程并发写同一路径时会互相删掉对方的临时文件
    （provider: agent 的并行分支可能同时写同一个 job / state），所以必须唯一化。
    """
    import tempfile
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        _chmod(tmp, _default_file_mode())        # mkstemp 是 0600，恢复成按 umask 的常规权限（N7）
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- config
def load_yaml(path):
    import yaml
    with open(path, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f)


def load_config(path=None):
    path = path or os.environ.get("PPTC_CONFIG") or os.path.join(SKILL_ROOT, "config.yaml")
    if not os.path.exists(path):
        raise PipelineError("找不到配置文件: %s" % path, EXIT_CONFIG)
    cfg = load_yaml(path)
    cfg["_path"] = os.path.abspath(path)
    prov = os.environ.get("PPTC_LLM_PROVIDER")
    if prov:
        cfg["llm"]["provider"] = prov
    return cfg


def load_scenario(name):
    p = os.path.join(SKILL_ROOT, "scenarios", name + ".yaml")
    if not os.path.exists(p):
        avail = sorted(f[:-5] for f in os.listdir(os.path.join(SKILL_ROOT, "scenarios")) if f.endswith(".yaml"))
        raise PipelineError("未知场景 %s，可选：%s" % (name, ", ".join(avail)), EXIT_CONFIG)
    return load_yaml(p)


# ---------------------------------------------------------------- workdir
PROCESS_DIR = "_过程文件"


class Workdir:
    """工作目录（0.7.0-dev7）：用户给的目录顶层只放成果（PPT、注意事项.md），过程文件全部在 _过程文件/。
    root 是过程文件目录（各阶段读写都基于它，结构与以前相同）；top 是用户给的目录。
    旧工作目录（顶层已有 brief.json / state.json）照旧：root 就是顶层。"""

    def __init__(self, root):
        self.top = os.path.abspath(root)
        self.legacy = any(os.path.exists(os.path.join(self.top, f)) for f in ("brief.json", "state.json"))
        self.root = self.top if self.legacy else os.path.join(self.top, PROCESS_DIR)

    def deliver(self, *parts):
        return os.path.join(self.top, *parts)

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def stage(self, stage, *parts):
        return os.path.join(self.root, STAGE_DIRS[stage], *parts)

    def logs(self, *parts):
        return os.path.join(self.root, "logs", *parts)

    def brief(self):
        b = read_json(self.p("brief.json"))
        if not b:
            raise PipelineError("工作目录未初始化（缺少 brief.json），请先运行 init", EXIT_CONFIG)
        return b

    # ---- state
    def state(self):
        return read_json(self.p("state.json"), {"stages": {}})

    def set_stage(self, stage, status, **extra):
        st = self.state()
        rec = st["stages"].get(stage, {})
        rec.update({"status": status, "updated_at": now_iso()})
        rec.update(extra)
        st["stages"][stage] = rec
        # 上游重跑后，下游全部标记为过期
        if status == "done":
            idx = STAGES.index(stage)
            for s in STAGES[idx + 1:]:
                if st["stages"].get(s, {}).get("status") == "done":
                    st["stages"][s]["status"] = "stale"
        write_json(self.p("state.json"), st)

    def require(self, *stages):
        st = self.state()["stages"]
        for s in stages:
            if st.get(s, {}).get("status") != "done":
                raise PipelineError("前置阶段 %s 未完成（状态：%s），请先运行它" % (s, st.get(s, {}).get("status", "未运行")), EXIT_CONFIG)


# ---------------------------------------------------------------- logger
class RunLogger:
    """线程安全的结构化日志。一个命令调用对应一个 invocation_id。"""

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

    def __init__(self, wd, cfg, command):
        self.wd = wd
        self.cfg = cfg
        self.command = command
        self.invocation_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.lock = threading.Lock()
        self.stage = None
        self.console_level = self.LEVELS.get(cfg.get("logging", {}).get("console_level", "INFO"), 20)
        os.makedirs(wd.logs("llm"), exist_ok=True)
        self.metrics = read_json(wd.logs("metrics.json"), {"stages": {}})
        self._stage_t0 = None
        self._stage_counters = {}

    def _append(self, name, obj):
        with self.lock:
            # errors="replace"：命令行参数中的非法字符（如被截断的中文路径）不能让日志写入失败
            with open(self.wd.logs(name), "a", encoding="utf-8", errors="replace") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def event(self, level, event, msg, **data):
        rec = {"ts": now_iso(), "inv": self.invocation_id, "stage": self.stage,
               "level": level, "event": event, "msg": msg}
        if data:
            rec["data"] = data
        self._append("events.jsonl", rec)
        line = "%s [%s] %s %s: %s" % (rec["ts"], level, self.stage or "-", event, msg)
        with self.lock:
            with open(self.wd.logs("run.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.LEVELS.get(level, 20) >= self.console_level:
            print(line if level != "INFO" else "[%s] %s" % (self.stage or "-", msg), file=sys.stderr)
        if level in ("WARN", "ERROR"):
            self.count("warnings" if level == "WARN" else "errors")

    def info(self, event, msg, **d):
        self.event("INFO", event, msg, **d)

    def warn(self, event, msg, **d):
        self.event("WARN", event, msg, **d)

    def error(self, event, msg, **d):
        self.event("ERROR", event, msg, **d)

    def debug(self, event, msg, **d):
        self.event("DEBUG", event, msg, **d)

    def gate(self, gate, target, passed, reason=None, **detail):
        rec = {"ts": now_iso(), "inv": self.invocation_id, "stage": self.stage,
               "gate": gate, "target": target, "passed": bool(passed)}
        if reason:
            rec["reason"] = reason
        if detail:
            rec["detail"] = detail
        self._append("gates.jsonl", rec)
        self.count("gate:%s:%s" % (gate, "pass" if passed else "fail"))

    def count(self, key, n=1):
        with self.lock:
            self._stage_counters[key] = self._stage_counters.get(key, 0) + n

    def set_metric(self, key, value):
        with self.lock:
            self._stage_counters[key] = value

    def llm_call(self, summary, full):
        self._append("llm_calls.jsonl", summary)
        write_json(self.wd.logs("llm", summary["call_id"] + ".json"), full)
        with self.lock:
            c = self._stage_counters
            c["llm_calls"] = c.get("llm_calls", 0) + 1
            u = summary.get("usage") or {}
            for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "prompt_cache_hit_tokens"):
                if u.get(k):
                    c["tokens:" + k] = c.get("tokens:" + k, 0) + u[k]
            if summary.get("cost") is not None:
                c["cost"] = round(c.get("cost", 0) + summary["cost"], 6)
            c["llm_latency_ms_total"] = c.get("llm_latency_ms_total", 0) + (summary.get("latency_ms") or 0)
            if not summary.get("ok"):
                c["llm_failed_attempts"] = c.get("llm_failed_attempts", 0) + 1

    def begin_stage(self, stage):
        self.stage = stage
        self._stage_t0 = time.time()
        self._stage_counters = {}
        self.info("stage_start", "开始阶段 %s" % stage)

    def end_stage(self, status="done", **summary):
        dur = round(time.time() - (self._stage_t0 or time.time()), 2)
        rec = {"status": status, "duration_s": dur, "finished_at": now_iso(),
               "invocation": self.invocation_id, "counters": dict(self._stage_counters)}
        rec.update(summary)
        self.metrics["stages"][self.stage] = rec
        write_json(self.wd.logs("metrics.json"), self.metrics)
        self.info("stage_end", "阶段 %s 结束（%s，%.1fs）" % (self.stage, status, dur), summary=summary,
                  counters=dict(self._stage_counters))

    def invocation(self, argv, exit_code, t0, err=None):
        rec = {"inv": self.invocation_id, "command": self.command, "argv": argv,
               "started_at": t0, "ended_at": now_iso(), "exit_code": exit_code}
        if err:
            rec["error"] = err
        self._append("invocations.jsonl", rec)


def redacted_config(cfg):
    c = json.loads(json.dumps(cfg, ensure_ascii=False, default=str))
    return c


def write_environment(wd, cfg):
    prompts_dir = os.path.join(SKILL_ROOT, "prompts")
    schemas_dir = os.path.join(SKILL_ROOT, "schemas")
    scen_dir = os.path.join(SKILL_ROOT, "scenarios")

    def hashes(d):
        out = {}
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                fp = os.path.join(d, fn)
                if os.path.isfile(fp):
                    out[fn] = sha256_file(fp)[:16]
        return out

    pkgs = {}
    for m in ["yaml", "jsonschema", "docx", "pdfplumber", "pptx", "openpyxl", "bs4", "xlrd"]:
        try:
            mod = __import__(m)
            pkgs[m] = getattr(mod, "__version__", "ok")
        except Exception:
            pkgs[m] = None
    env = {
        "written_at": now_iso(),
        "skill_version": VERSION,
        "python": sys.version,
        "platform": platform.platform(),
        "packages": pkgs,
        "config_path": cfg.get("_path"),
        "config": redacted_config(cfg),
        "llm_provider": cfg["llm"].get("provider"),
        "network_needed": cfg["llm"].get("provider") == "deepseek",
        "api_key_present": bool(os.environ.get(cfg["llm"].get("api_key_env", "DEEPSEEK_API"))
                                or os.environ.get("DEEPSEEK_API_KEY")),
        "prompt_hashes": hashes(prompts_dir),
        "schema_hashes": hashes(schemas_dir),
        "scenario_hashes": hashes(scen_dir),
    }
    hist = read_json(wd.logs("environment.json"), {"history": []})
    hist["current"] = env
    hist["history"] = (hist.get("history") or [])[-20:] + [{"at": env["written_at"], "skill_version": VERSION,
                                                              "prompt_hashes": env["prompt_hashes"],
                                                              "config_hash": sha256_text(json.dumps(env["config"], sort_keys=True, ensure_ascii=False))}]
    write_json(wd.logs("environment.json"), hist)


def format_exc():
    return traceback.format_exc()
