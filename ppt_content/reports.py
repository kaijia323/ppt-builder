"""运行报告、诊断打包、评测。"""
import json
import os
import zipfile

from .checks import norm, truncate
from .core import SKILL_ROOT, STAGES, VERSION, now_iso, read_json, read_jsonl, write_json, write_text


def _top(d, n=8):
    return sorted(d.items(), key=lambda kv: -kv[1])[:n]


def issue_key(s):
    s = str(s)
    for sep in ("（", "：", " %", "["):
        pass
    import re
    s = re.sub(r"\[\d+\]", "[i]", s)
    s = re.sub(r"[A-Z]\d+(-[A-Za-z0-9.]+)*", "<id>", s)
    s = re.sub(r"\d+(\.\d+)?", "<n>", s)
    return s[:60]


def run_report(wd):
    m = read_json(wd.logs("metrics.json"), {"stages": {}})
    # 日志保留全部历史；报告只统计每个阶段最近一次运行（避免重跑时数据叠加）
    latest = {s: r.get("invocation") for s, r in m["stages"].items()}

    def is_latest(r):
        return r.get("stage") in latest and r.get("inv") == latest[r.get("stage")]
    calls = [c for c in read_jsonl(wd.logs("llm_calls.jsonl")) if is_latest(c)]
    gates = [g for g in read_jsonl(wd.logs("gates.jsonl")) if is_latest(g)]
    events = read_jsonl(wd.logs("events.jsonl"))
    env = (read_json(wd.logs("environment.json"), {}) or {}).get("current", {})
    brief = read_json(wd.p("brief.json"), {})
    st = read_json(wd.p("state.json"), {"stages": {}})
    L = ["# 运行报告", "", "> 统计口径：每个阶段最近一次运行。完整历史见 logs/*.jsonl。", "", "- 生成时间：%s" % now_iso(), "- 技能版本：%s" % env.get("skill_version", VERSION),
         "- 场景：%s；输入：%s" % (brief.get("scenario"), "，".join(os.path.basename(x) for x in brief.get("inputs", []))),
         "- 模型提供方：%s" % ((env.get("config") or {}).get("llm", {}).get("provider")), ""]
    L += ["## 阶段", "", "| 阶段 | 状态 | 耗时(s) | 模型调用 | 失败尝试 | 输入tokens | 输出tokens | 思考tokens | 警告 | 错误 | 关键指标 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in STAGES:
        rec = m["stages"].get(s)
        status = st["stages"].get(s, {}).get("status", "-")
        if not rec:
            L.append("| %s | %s | | | | | | | | | |" % (s, status))
            continue
        c = rec.get("counters", {})
        keys = {k: v for k, v in (rec.get("summary") or {}).items() if not isinstance(v, (dict, list))}
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            s, status, rec.get("duration_s"), c.get("llm_calls", 0), c.get("llm_failed_attempts", 0),
            c.get("tokens:prompt_tokens", 0), c.get("tokens:completion_tokens", 0), c.get("tokens:reasoning_tokens", 0),
            c.get("warnings", 0), c.get("errors", 0), truncate("；".join("%s=%s" % kv for kv in list(keys.items())[:6]), 120)))
    # 模型调用分析
    L += ["", "## 模型调用（按任务）", "", "| 任务 | 调用次数 | 一次通过率 | 最终失败 | 平均耗时(ms) | P95耗时(ms) | 输入tokens | 输出tokens | 常见问题 |",
          "|---|---|---|---|---|---|---|---|---|"]
    by = {}
    for c in calls:
        by.setdefault(c["task"], []).append(c)
    for t, cs in sorted(by.items()):
        groups = {}
        for c in cs:
            # HTTP provider 的 call_id 以 .<invocation> 结尾，去掉才能归成一次调用；
            # agent provider 的 call_id 就是 job_id（每次尝试一个），按 target 归组。
            tail = "agent" if c.get("model") == "agent" else c["call_id"].rsplit(".", 1)[-1]
            groups.setdefault((c["target"], c.get("vote", 0), tail), []).append(c)
        first_ok = sum(1 for g in groups.values() if min(g, key=lambda x: x["attempt"])["ok"])
        final_fail = sum(1 for g in groups.values() if not max(g, key=lambda x: x["attempt"])["ok"])
        lat = sorted(c["latency_ms"] for c in cs)
        iss = {}
        for c in cs:
            for i in c.get("issues", []):
                k = issue_key(i)
                iss[k] = iss.get(k, 0) + 1
        L.append("| %s | %d | %.0f%% | %d | %d | %d | %d | %d | %s |" % (
            t, len(cs), 100.0 * first_ok / max(1, len(groups)), final_fail, sum(lat) / max(1, len(lat)),
            lat[int(len(lat) * 0.95) - 1] if lat else 0, sum((c.get("usage") or {}).get("prompt_tokens") or 0 for c in cs),
            sum((c.get("usage") or {}).get("completion_tokens") or 0 for c in cs),
            truncate("；".join("%s×%d" % kv for kv in _top(iss, 3)), 150)))
    # 关卡
    L += ["", "## 质量关卡", "", "| 关卡 | 通过 | 未通过 | 通过率 | 主要原因 |", "|---|---|---|---|---|"]
    gs = {}
    for g in gates:
        r = gs.setdefault(g["gate"], {"p": 0, "f": 0, "reasons": {}})
        if g["passed"]:
            r["p"] += 1
        else:
            r["f"] += 1
            k = issue_key(g.get("reason", ""))
            r["reasons"][k] = r["reasons"].get(k, 0) + 1
    for k, r in sorted(gs.items()):
        L.append("| %s | %d | %d | %.0f%% | %s |" % (k, r["p"], r["f"], 100.0 * r["p"] / max(1, r["p"] + r["f"]),
                                                  truncate("；".join("%s×%d" % kv for kv in _top(r["reasons"], 3)), 150)))
    vr = read_json(wd.stage("check", "check_report.json"))
    if vr:
        s = vr["summary"]
        L += ["", "## 核验结论", "", "```json", json.dumps(s, ensure_ascii=False, indent=1), "```"]
    ev = [e for e in events if e["level"] in ("WARN", "ERROR")]
    L += ["", "## 警告与错误（最近 60 条）", ""]
    for e in ev[-60:]:
        L.append("- %s [%s] %s/%s：%s" % (e["ts"][11:19], e["level"], e.get("stage"), e["event"], truncate(e["msg"], 160)))
    L += ["", "## 文件索引", "", "- 每次模型调用完整记录：logs/llm/<call_id>.json（含完整提示词、原始输出、思考内容）",
          "- 调用摘要：logs/llm_calls.jsonl；关卡判定：logs/gates.jsonl；事件：logs/events.jsonl",
          "- 各阶段中间结果：01_ingest … 07_export；理解卡见 02_understand/understanding.md，页面规划见 checkpoint1.md"]
    write_text(wd.logs("run_report.md"), "\n".join(L) + "\n")
    return wd.logs("run_report.md")


def bundle(wd, out=None, include_inputs=False):
    run_report(wd)
    brief = read_json(wd.p("brief.json"), {})
    top = getattr(wd, "top", wd.root)
    out = out or os.path.join(os.path.dirname(top), "%s-debug-%s.zip" % (os.path.basename(top), now_iso()[:19].replace(":", "")))
    files = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, fns in os.walk(wd.root):
            for fn in fns:
                fp = os.path.join(root, fn)
                if fp.endswith(".tmp") or os.path.abspath(fp) == os.path.abspath(out):
                    continue
                arc = os.path.join("workdir", os.path.relpath(fp, wd.root))
                z.write(fp, arc)
                files.append({"path": arc, "bytes": os.path.getsize(fp)})
        if top != wd.root and os.path.exists(os.path.join(top, "注意事项.md")):
            z.write(os.path.join(top, "注意事项.md"), "deliverables/注意事项.md")
        for sub in ("prompts", "schemas", "scenarios"):
            d = os.path.join(SKILL_ROOT, sub)
            for fn in sorted(os.listdir(d)):
                z.write(os.path.join(d, fn), os.path.join("skill_snapshot", sub, fn))
        z.write(os.path.join(SKILL_ROOT, "config.yaml"), "skill_snapshot/config.yaml")
        if include_inputs:
            for f in brief.get("inputs", []):
                if os.path.exists(f):
                    z.write(f, os.path.join("inputs", os.path.basename(f)))
                    files.append({"path": "inputs/" + os.path.basename(f), "bytes": os.path.getsize(f)})
        manifest = {"created_at": now_iso(), "skill_version": VERSION, "workdir": wd.root, "include_inputs": include_inputs,
                    "note": "API Key 不会写入任何日志。注意：blocks.jsonl 与 logs/llm/*.json 中包含原文内容。",
                    "files": files}
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return out


# ---------------------------------------------------------------- eval
def evaluate(wd, gold_path, llm, log):
    gold = read_json(gold_path)
    content = read_json(wd.stage("export", "content.json"))
    if not content:
        raise RuntimeError("尚未导出 content.json")
    vr = read_json(wd.stage("check", "check_report.json"), {}).get("summary", {})
    rv = read_json(wd.stage("review", "review_round1.json"), {}) or {}
    pages = [p for p in content["pages"] if p["kind"] == "content"]
    text_all = "\n".join([p["title"] for p in pages] + [b["text"] for p in pages for b in p["bullets"]] + [p.get("notes", "") for p in pages])
    res = {"evaluated_at": now_iso(), "gold": os.path.abspath(gold_path), "skill_version": VERSION}
    kps = gold.get("key_points", [])
    if kps:
        outline_txt = "\n".join("第%d页 %s：%s" % (p["no"], p["title"], "；".join(b["text"] for b in p["bullets"])) for p in pages)
        kp_txt = "\n".join("K%d：%s" % (i + 1, k) for i, k in enumerate(kps))

        def validate(d):
            got = {x.get("key") for x in d.get("matches", [])}
            miss = ["K%d" % (i + 1) for i in range(len(kps)) if "K%d" % (i + 1) not in got]
            return ["缺少对 %s 的判定" % miss] if miss else []
        r = llm.call_json("eval_match", {"key_points": kp_txt, "outline": outline_txt}, target="eval", validator=validate)
        ms = (r["data"] or {}).get("matches", [])
        full = sum(1 for x in ms if x.get("coverage") == "full")
        part = sum(1 for x in ms if x.get("coverage") == "partial")
        res["key_points"] = {"total": len(kps), "full": full, "partial": part,
                             "recall_full": round(full / float(len(kps)), 3), "recall_any": round((full + part) / float(len(kps)), 3),
                             "details": [dict(x, text=kps[int(x["key"][1:]) - 1]) for x in ms if str(x.get("key", "")).startswith("K")]}
    if gold.get("expected_numbers"):
        hit = [n for n in gold["expected_numbers"] if norm(n) in norm(text_all)]
        res["expected_numbers"] = {"total": len(gold["expected_numbers"]), "hit": len(hit),
                                   "missing": [n for n in gold["expected_numbers"] if n not in hit]}
    if gold.get("forbidden"):
        hits = [f for f in gold["forbidden"] if norm(f) in norm(text_all)]
        res["forbidden_hits"] = hits
    if gold.get("expected_pages"):
        lo, hi = gold["expected_pages"]
        res["pages"] = {"content_pages": len(pages), "in_range": lo <= len(pages) <= hi}
    tv = vr.get("title_verdicts", {}) or {}
    ntv = sum(tv.values()) or 1
    res["verify"] = {"content_pages": vr.get("pages"), "bullets": vr.get("bullets"),
                     "title_unsupported_rate": round(tv.get("unsupported", 0) / float(ntv), 3),
                     "title_partial_rate": round(tv.get("partial", 0) / float(ntv), 3),
                     "items_by_severity": vr.get("items_by_severity"), "review_round1_score": (rv.get("overall") or {}).get("score"),
                     "review_final_score": vr.get("review_final_score")}
    dec = content["quality"].get("user_decisions", [])
    n_b = sum(len(p["bullets"]) for p in pages) or 1
    res["user_edit_rate"] = round(sum(1 for d in dec if d["decision"] in ("edit", "drop")) / float(n_b), 3)
    m = read_json(wd.logs("metrics.json"), {"stages": {}})
    tot = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "duration_s": 0.0}
    for s in m["stages"].values():
        c = s.get("counters", {})
        tot["llm_calls"] += c.get("llm_calls", 0)
        tot["prompt_tokens"] += c.get("tokens:prompt_tokens", 0)
        tot["completion_tokens"] += c.get("tokens:completion_tokens", 0)
        tot["duration_s"] += s.get("duration_s", 0)
    res["cost"] = tot
    write_json(wd.p("eval_result.json"), res)
    return res


def eval_summary(workdirs, out):
    from .core import Workdir
    rows = []
    for w in workdirs:
        r = read_json(Workdir(w).p("eval_result.json"))
        if not r:
            continue
        rows.append({"workdir": os.path.basename(os.path.abspath(w)), "recall_full": r.get("key_points", {}).get("recall_full"),
                     "recall_any": r.get("key_points", {}).get("recall_any"),
                     "numbers_hit": "%s/%s" % (r.get("expected_numbers", {}).get("hit", "-"), r.get("expected_numbers", {}).get("total", "-")),
                     "content_pages": r["verify"].get("content_pages"), "title_partial": r["verify"].get("title_partial_rate"),
                     "title_unsupported": r["verify"].get("title_unsupported_rate"), "review_score": r["verify"].get("review_final_score"),
                     "edit_rate": r.get("user_edit_rate"), "llm_calls": r["cost"]["llm_calls"],
                     "tokens": r["cost"]["prompt_tokens"] + r["cost"]["completion_tokens"], "duration_s": round(r["cost"]["duration_s"], 1)})
    if not rows:
        return None
    keys = list(rows[0].keys())
    L = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    for r in rows:
        L.append("| " + " | ".join(str(r[k]) for k in keys) + " |")
    write_text(out, "\n".join(L) + "\n")
    return out
