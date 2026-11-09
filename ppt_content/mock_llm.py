"""离线模拟模型（provider: mock）。

只用于在没有 API 的环境下验证流水线代码路径、日志与关卡；用简单规则"假装"完成每个任务，
输出结构合法但内容质量没有意义，不能用来评估提炼效果。
个别调用会故意在第 1 次输出中留问题，以覆盖"带反馈重试"和"审查后重写"的路径。
"""
import json
import re
import threading

from .checks import bigrams, extract_numbers

_LOCK = threading.Lock()
_STATE = {"review_calls": 0}
HEDGES = ["预计", "计划", "拟", "力争", "有望", "争取", "目标"]
IMPORTANT = "功能|需求|范围|边界|指标|目标|背景|计划|约束|风险|任务|成果|交付|模型|知识|数据|性能|考核|步骤|操作|研究|进展|问题"
TOPIC_END = ("介绍", "分析", "概述", "概况", "说明", "简介", "情况")


def _cov(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / float(len(A) or 1)


def safe_cut(s, n):
    if len(s) <= n:
        return s
    cut = s[:n]
    m = max(cut.rfind("，"), cut.rfind("；"), cut.rfind("。"))
    if m >= n * 0.5:
        return cut[:m]
    cut = re.sub(r"[\d.,%：:（(]+$", "", cut)
    return cut


def respond(task, v, attempt, vote):
    fn = globals().get("_" + task)
    data = fn(v, attempt) if fn else {}
    content = json.dumps(data, ensure_ascii=False)
    usage = {"prompt_tokens": 500 + sum(len(str(x)) for x in v.values()) // 2, "completion_tokens": len(content) // 2,
             "prompt_cache_hit_tokens": 0, "reasoning_tokens": 0}
    return content, usage


def _ping(v, a):
    return {"pong": True}


def parse_blocks(text):
    out, cur = [], None
    for ln in text.splitlines():
        m = re.match(r"#+ \[(\S+)\] (.*)", ln)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"\[(\S+?)\](（.*?）)? (.*)", ln)
        if m:
            out.append({"id": m.group(1), "sec": cur, "text": m.group(3)})
    return out


def parse_outline(text):
    out = []
    for ln in text.splitlines():
        m = re.match(r"(\s*)\[(\S+)\] (.*?)(（\d+字）)?( —— (.*))?$", ln)
        if m:
            out.append({"key": m.group(2), "title": m.group(3), "level": len(m.group(1)) // 2 + 1, "note": m.group(6) or ""})
    return out


def _chapters(v, attempt):
    blocks = parse_blocks(v["full_text"])
    req = [x for x in v["required_sections"].split("、") if x]
    titles = {o["key"]: o["title"] for o in parse_outline(v["outline"])}
    chs = []
    for k in req:
        sub = [b for b in blocks if b["sec"] and (b["sec"] == k or b["sec"].startswith(k + "."))]
        if not sub:
            continue
        t = titles.get(k, "")
        imp = "high" if re.search(IMPORTANT, t) else ("low" if re.search("引言|目的|参考|术语|缩写|使用范围", t) else "medium")
        chs.append({"section": k, "role": "说明" + t[:6], "summary": safe_cut(sub[0]["text"], 70),
                    "key_points": [{"text": safe_cut(b["text"], 45), "ids": [b["id"]]} for b in sub[:2]],
                    "importance": imp, "importance_reason": "（mock）按标题关键词判断"})
    if attempt == 1 and len(chs) > 2:
        chs = chs[:-1]          # 故意漏一章，触发反馈重试
    return chs, blocks


def _understand(v, a):
    chs, blocks = _chapters(v, a)
    pick = lambda pat: [{"text": safe_cut(b["text"], 50), "ids": [b["id"]]} for b in blocks if re.search(pat, b["text"])][:5]
    return {"doc_title": "（mock）文档标题", "doc_type": "（mock）文档", "doc_type_reason": "mock", "written_for": "评审专家", "effect": "原文未说明",
            "suggested_scenario": "", "suggested_scenario_reason": "",
            "storyline": ["现状问题", "建设内容", "指标与边界"], "chapters": chs,
            "key_items": {"decisions": pick("建议由甲方|需评审|请.*确认"), "metrics": pick("≤|≥|不低于|不少于"),
                          "scope_in": pick("构建|开发|建设"), "scope_out": pick("不承担"), "deliverables": pick("交付"),
                          "constraints": pick("须|约束|顺延")},
            "doc_issues": []}


def _understand_chapter(v, a):
    chs, _ = _chapters(v, a)
    return {"chapters": chs}


def _understand_merge(v, a):
    return {"doc_type": "（mock）长文档", "doc_type_reason": "mock", "written_for": "", "effect": "", "suggested_scenario": "",
            "suggested_scenario_reason": "", "storyline": ["mock"], "key_items": {}, "doc_issues": []}


def _plan(v, a):
    ol = parse_outline(v["outline"])
    rec, mn, mx = int(v["rec_pages"]), int(v["min_pages"]), int(v["max_pages"])
    cand = [o for o in ol if "重要性高" in o["note"] and o["level"] <= 2]
    cand = [o for o in cand if not any(c["key"] != o["key"] and c["key"].startswith(o["key"] + ".") for c in cand)] or cand
    others = [o for o in ol if o not in cand and o["level"] <= 2 and "重要性" in o["note"]]
    chosen = (cand + others)[:max(mn, min(rec, mx))]
    if not chosen:
        chosen = [o for o in ol if o["level"] >= 1][:max(1, mn)]
    sup = re.findall(r"\[(USER-G\d+)\]", v.get("user_supplements", ""))
    n = len(chosen) + len(sup)
    m = round(float(v["content_minutes"]) / max(1, n), 2)
    pages = []
    for o in chosen:
        t = o["title"]
        title = ("%s：关键内容与结论" % t)[:30] if not t.endswith(TOPIC_END) else ("%s的结论" % t[:-2])[:30]
        pages.append({"title_draft": title, "question": "听众想知道%s的要点" % t, "sources": [o["key"]], "points": ["要点一", "要点二"], "minutes": m})
    for s in sup:
        pages.append({"title_draft": "用户补充内容的结论", "question": "补充内容", "sources": [s], "points": ["补充"], "minutes": m})
    cl = re.findall(r"^(C\d+) ", v["focus_checklist"], re.M)
    return {"storyline": "（mock）按文档结构讲清要点", "section_decisions": [{"section": o["key"], "decision": "page" if o in chosen else "skip", "reason": "mock"}
                                                                   for o in ol if "重要性" in o["note"] and o["level"] <= 2],
            "pages": pages, "agenda": [p["title_draft"][:8] for p in pages[:6]],
            "not_covered": [], "checklist_coverage": [{"item": c, "pages": [1] if i % 4 else [], "note": "" if i % 4 else "（mock）原文没有相关内容"}
                                                       for i, c in enumerate(cl, 1)],
            "self_check": {"duplicates": "无", "order": "合理", "total_minutes": m * n}}


def _write_page(v, a):
    blocks = parse_blocks(v["source_text"])
    if not blocks:
        for ln in v["source_text"].splitlines():
            mm = re.match(r"\[(\S+?)\] (.*)", ln)
            if mm:
                blocks.append({"id": mm.group(1), "text": mm.group(2)})
    mt, mb, nb = int(v["max_title_chars"]), int(v["max_bullet_chars"]), int(v["max_bullets"])
    first = blocks[0]["text"] if blocks else "无内容"
    title = safe_cut(first, mt - 2) or "本页结论"
    if title.endswith(TOPIC_END):
        title = title[:-2] + "要点"
    bl = []
    for b in blocks[:nb]:
        t = safe_cut(b["text"], mb - 3)
        if not t:
            continue
        if any(h in b["text"] for h in HEDGES) and extract_numbers(t) & extract_numbers(b["text"]) and not any(h in t for h in HEDGES + ["约", "目标"]):
            t = "计划" + t[:mb - 3]
        bl.append({"text": t, "ids": [b["id"]]})
    while len(bl) < int(v["min_bullets"]) and blocks:
        bl.append({"text": safe_cut(blocks[-1]["text"], mb - 3) or "见原文", "ids": [blocks[-1]["id"]]})
    notes = "".join(b["text"] for b in blocks)[:int(v["notes_max"])]
    while len(notes) < int(v["notes_min"]):
        notes += "这部分内容结合原文展开。"
    facts = []
    if "空数组" not in v.get("facts_rule", "空数组"):
        for b in blocks[nb:nb + 4] or blocks[:3]:
            t = safe_cut(b["text"], 30)
            if t and t not in [x["text"] for x in bl]:
                facts.append({"text": t, "ids": [b["id"]]})
    return {"title": title, "bullets": bl, "facts": facts, "notes": notes, "visual": {"type": "要点列表", "description": "（mock）"},
            "minutes": float(v["minutes"])}


def _review(v, a):
    with _LOCK:
        if a == 1:
            _STATE["review_calls"] += 1
        first = _STATE["review_calls"] % 2 == 1
    issues = [{"page": 1, "type": "title", "severity": "medium", "problem": "（mock）标题可以更具体", "suggestion": "写出关键事实", "ids": []},
              {"page": 0, "type": "completeness", "severity": "low", "problem": "（mock）可补充更多数据", "suggestion": "", "ids": []}] if first else []
    return {"overall": {"score": 3 if first else 4, "summary": "（mock）结构基本合理", "strengths": ["结构清晰"]}, "issues": issues}


def _entail(v, a):
    c = _cov(v["claim"], v["evidence"])
    verdict = "supported" if c >= 0.6 else "partial" if c >= 0.35 else "unsupported"
    return {"counter_arguments": ["（mock）"], "verdict": verdict, "reason": "字面重合度 %.2f（mock）" % c, "problem_span": ""}


def _eval_match(v, a):
    out = []
    for ln in v["key_points"].splitlines():
        m = re.match(r"(K\d+)：(.*)", ln)
        if m:
            c = _cov(m.group(2), v["outline"])
            out.append({"key": m.group(1), "coverage": "full" if c > 0.7 else "partial" if c > 0.4 else "none", "pages": [], "reason": "mock %.2f" % c})
    return {"matches": out}


# ---------------------------------------------------------------- 视觉设计（0.4）
def _design_deck(v, a):
    pages = re.findall(r"第(\d+)页｜(.+)\n  要点：(.*)", v["pages"])
    nch = len([l for l in v["agenda"].splitlines() if re.match(r"^\d+\.", l)]) or 1
    out, prev = [], None
    n = len(pages)
    for i, (no, title, pts) in enumerate(pages):
        nb = len([x for x in pts.split("；") if x.strip()])
        cands = (["L13", "L06", "L05"] if nb >= 3 else ["L06"]) + ["L39"]
        lay = next(c for c in cands if c != prev)
        prev = lay
        topic = re.split(r"[，,：:；;。]", title)[0][:10]
        if a == 1 and i == 0:
            topic = title            # 第 1 次故意给超长主题，覆盖"带问题清单重试"的路径
        out.append({"no": int(no), "chapter": min(nch, 1 + i * nch // max(1, n)), "topic": topic, "layout": lay,
                    "alt": "L39", "relation": "并列"})
    return {"pages": out}


def _design_page(v, a):
    lid = v["layout_id"]
    bl = [x[2:].strip() for x in v["bullets"].splitlines() if x.startswith("- ")]
    title = v["title"]

    def hb(t, hn=8, bn=34):
        m = re.match(r"^(.{2,10})[：:](.+)$", t)
        if m:
            return {"head": m.group(1)[:hn], "body": safe_cut(m.group(2), bn)}
        return {"head": t[:hn], "body": safe_cut(t, bn)}
    if lid == "L13":
        spec = {"steps": [hb(t) for t in bl[:6]]}
    elif lid == "L06":
        half = max(1, (len(bl) + 1) // 2)
        groups = [bl[:half], bl[half:]] if len(bl) > 1 else [bl]
        spec = {"panels": [{"title": hb(g[0])["head"], "items": [hb(t) for t in g[:4]]} for g in groups if g]}
        if len(spec["panels"]) < 2:
            spec["panels"].append({"title": title[:6], "items": [hb(title)]})
    elif lid == "L05":
        spec = {"columns": [{"title": hb(t)["head"][:6], "subtitle": safe_cut(t, 12)} for t in bl[:5]]}
    else:
        spec = {"lead": safe_cut(title, 32), "items": [hb(t, 10, 46) for t in bl[:6]]}
    return {"layout": lid, "intro": safe_cut(bl[0] if bl else title, 40), "spec": spec}


def _frame(v, a):
    secs = re.findall(r"^  \[(D\d+-S[\d.]+)\] (\S+)（(\d+)字，([\d.]+)%", v["outline"], re.M)
    lvl2 = [(k, t) for k, t, c, p in secs if float(p) >= 3][:5]
    tops = re.findall(r"^\[(D\d+-S\d+)\] (\S+)（\d+字，([\d.]+)%", v["outline"], re.M)
    units = [{"name": t[:12], "sections": [k], "overview_sections": [], "metric_sections": [], "summary": t} for k, t in lvl2[:4]]
    used = {u["sections"][0] for u in units}
    rest = [k for k, t, p in tops if not any(u.startswith(k + ".") or u == k for u in used)]
    qs = [{"q": "%s具体怎么实现、效果如何" % u["name"], "level": "主" if i < 3 else "次", "answer_sections": u["sections"],
           "answerable": "能", "basis": "（mock）"} for i, u in enumerate(units)]
    while len(qs) < 5 and units:
        u = units[len(qs) % len(units)]
        qs.append({"q": "%s的指标如何验收" % u["name"], "level": "次", "answer_sections": u["sections"], "answerable": "部分",
                   "basis": "（mock）"})
    return {"profile": {"doc_kind": "方案", "judged_by": ["是否可行", "能否交付"], "core": "（mock）"}, "units": units,
            "shell": [{"module": "概述", "sections": rest, "note": ""}], "special": {}, "questions": qs,
            "weak_points": [{"q": "测试条件是什么", "gap": "（mock）"}]}


def _frame_verify(v, a):
    out = []
    for m in re.finditer(r"### 问题(\d+)：.*?\n(.*?)(?=\n### 问题|\n=====|\Z)", v["items"], re.S):
        ids = re.findall(r"\[(D\d+-S[\d.]+-[A-Z]\d+(?:-R\d+)?)\]", m.group(2))[:2]
        out.append({"no": int(m.group(1)), "answerable": "能" if ids else "不能", "evidence": ids, "answer": "（mock）"})
    wout = [{"no": int(m.group(1)), "covered": "没讲", "evidence": []}
            for m in re.finditer(r"### 薄弱点(\d+)：", v["items"])]
    return {"results": out, "weak_results": wout}


def _plan_outline(v, a):
    chs = []
    for m in re.finditer(r"^- (K\d+) (.+?)（.*?建议 (\d+) 页", v["chapters"], re.M):
        chs.append({"id": m.group(1), "title": m.group(2)[:12], "pages": int(m.group(3)), "message": "（mock）", "questions": []})
    nq = len(re.findall(r"^\d+\. ", v["questions"], re.M))
    for i in range(1, nq + 1):
        if chs:
            chs[(i - 1) % len(chs)]["questions"].append(i)
    return {"storyline": "（mock）按骨架讲清建设内容", "chapters": chs}


def _plan_chapter(v, a):
    n = int(v["n_pages"])
    keys = re.findall(r"^\s*\[(D\d+-S[\d.]+)\]", v["chapter_outline"], re.M)
    deep = [k for k in keys if k.count(".") >= 2] or keys
    for qk in re.findall(r"出处 ([^｜]+)", v["questions"]):
        for k in qk.split(","):
            k = k.strip()
            if k in keys and k not in deep:
                deep.append(k)
    groups = [[] for _ in range(max(1, n))]
    for i, k in enumerate(deep):
        groups[i % len(groups)].append(k)
    pages = [{"title_draft": "%s：要点与结论（mock）" % v["chapter_title"][:12], "question": "（mock）",
              "sources": g or keys[:1], "points": ["要点一", "要点二", "要点三", "要点四"], "minutes": 0.75} for g in groups]
    return {"pages": pages, "section_decisions": []}


def _speech(v, a):
    items = [l[2:] for l in v["slide"].splitlines() if l.startswith("- ")]
    topic = next((l[3:] for l in v["slide"].splitlines() if l.startswith("标题：")), "")
    t = "%s方面，" % re.sub(r"^[\d.]+\s*", "", topic)[:8]
    for k, x in enumerate(items):
        nxt = "%s是%s；" % ("一二三四五六七八九十"[k % 10], re.sub(r"\d", "", x))
        if len(t) + len(nxt) > int(v["cap"]) - 10:
            break
        t += nxt
    while len(t) < int(v["low"]):
        t += "这些安排共同支撑方案目标。"
    return {"notes": t[:int(v["cap"])]}


def _fix_claim(v, a):
    return {"action": "rewrite", "text": v["claim"]}


def _page_edit(v, a):
    import re as _re
    bl = [{"from": m.group(1), "text": m.group(2)} for m in _re.finditer(r"\[(\S+?)\] (.+)", v.get("bullets", ""))]
    return {"title": v.get("title", ""), "bullets": bl, "facts": []}
