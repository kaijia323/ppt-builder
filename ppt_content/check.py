"""第⑤步：核验，并生成检查点 2。
- 脚本检查（模型不可推翻）：出处、数字、程度词、限定语气、篇幅
- 标题蕴含核验（可配置 none/titles/all）：标题是归纳，最容易说过头
- 汇总编辑审查终审仍未解决的问题、规划阶段未覆盖的关注点
检查点 2 按"整套层面 → 必须处理 → 建议确认 → 参考"排序，避免真问题被淹没。
"""
from .checks import count_conflicts, dup_pairs, truncate
from .core import read_json, write_json, write_text
from .review import TYPE_CN
from .write import build_ctx, load_ctx, page_issues, table_evidence

SEV_ORDER = {"deck": 0, "blocking": 1, "review": 2, "info": 3}


def classify(msg):
    if "数字" in msg or "不在本页提供的原文" in msg or "没有注明段落ID" in msg:
        return "blocking", "number_or_source"
    if "程度词" in msg:
        return "review", "overstatement"
    if "语气" in msg:
        return "review", "hedge"
    if "需确认事项" in msg:
        return "review", "confirm_item"
    if "话题式" in msg:
        return "review", "title_style"
    return "info", "limit"


def target_of(msg, page):
    import re
    m = re.match(r"要点(\d+)", msg)
    if m:
        return "%s-b%s" % (page["page_key"], m.group(1))
    if msg.startswith("标题"):
        return "title"
    if "备注" in msg:
        return "notes"
    return "page"


def _terms(text):
    import re
    t = re.sub(r"\s+", "", text or "")
    words = {w.lower().strip("-+") for w in re.findall(r"[A-Za-z][A-Za-z0-9\-+]{1,}|\d+(?:\.\d+)?%?", t)}
    cjk = re.sub(r"[^\u4e00-\u9fa5]", "|", t)
    grams = {seg[i:i + 2] for seg in cjk.split("|") for i in range(len(seg) - 1)}
    return words | grams


_STOP = {"进行", "实现", "支持", "通过", "以及", "需要", "研究", "建设", "提供", "相关"}


def related_blocks(doc, claim, exclude, k):
    """全文中与标题关键词重合最多的段落（不含已引用段落），用于减少"原文其他章节明明有"的误判。"""
    q = _terms(claim) - _STOP
    if not q or k <= 0:
        return []
    scored = []
    for b in doc.blocks:
        if b["id"] in exclude or b["type"] == "heading":
            continue
        hit = q & _terms(b["text"])
        if len(hit) >= max(2, int(len(q) * 0.25)):
            score = sum(2 if h.isascii() else 1 for h in hit)     # 专名、缩写、数字更有区分度
            scored.append((score, -len(b["text"]), b))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in scored[:k]]


def run(wd, cfg, log, llm):
    plan, und, scen, brief, doc = load_ctx(wd)
    pages = read_json(wd.stage("review", "pages.json")) or read_json(wd.stage("write", "pages.json"))
    final_rv = read_json(wd.stage("review", "review_final.json")) or read_json(wd.stage("review", "review_round1.json")) or {"issues": []}
    items = []

    def add(sev, cat, page, target, text, detail, ids=None, suggestion=""):
        items.append({"item_id": "V%03d" % (len(items) + 1), "severity": sev, "category": cat, "page": page, "target": target,
                      "text": text, "detail": detail, "ids": ids or [], "suggestion": suggestion})
        log.gate("check:" + cat, "P%02d/%s" % (page, target) if isinstance(page, int) else str(page), False, detail, severity=sev)

    # ---- 整套层面
    for g in plan.get("gaps", []):
        add("deck", "checklist_uncovered", 0, "deck", "", "听众关注点“%s”没有任何一页覆盖%s" % (g["text"], ("：" + g["note"]) if g.get("note") else ""))
    for k in plan.get("high_sections_uncited", []):
        add("deck", "high_section_uncited", 0, "deck", "", "重要性高的章节 %s %s 没有任何一页引用" % (k, doc.sections[k]["title"] if k in doc.sections else ""))
    for x in final_rv["issues"]:
        if x["page"] == 0 and x["severity"] in ("high", "medium"):
            add("deck", "review_" + x["type"], 0, "deck", "", "%s：%s" % (TYPE_CN.get(x["type"]), x["problem"]), x["ids"], x["suggestion"])
    # ---- 脚本检查
    for p in pages:
        ctx = build_ctx(doc, {"sources": p["sources"]}, cfg, doc.title_numbers([und.get("doc_title", "")]))
        ctx["strict"] = True
        for msg in page_issues(p, ctx, cfg):
            sev, cat = classify(msg)
            tgt = target_of(msg, p)
            txt = p["title"] if tgt == "title" else next((b["text"] for b in p["bullets"] if b["id"] == tgt), "")
            add(sev, cat, p["no"], tgt, txt, msg)
        for b in p["bullets"]:
            log.gate("bullet_has_source", b["id"], bool(b["ids"]))
    # ---- 标题方向与已确认规划差异过大（G44 兜底）
    import os
    if os.path.exists(wd.stage("plan", "approved.json")):
        from .checks import bigrams
        pm = {q["no"]: q for q in plan["pages"]}
        for p in pages:
            td = pm.get(p["no"], {}).get("title_draft", "")
            A, B = bigrams(td), bigrams(p["title"])
            if td and A and B and len(A & B) / float(min(len(A), len(B))) < 0.25:
                add("review", "title_drift", p["no"], "title", p["title"],
                    "标题与检查点 1 确认的规划标题“%s”差异较大，请确认方向是否被改变" % td, suggestion="如需恢复，用 edit 写回规划标题")
    # ---- 跨页数量不一致（G47）
    for x in count_conflicts(pages):
        desc = "；".join("第%s页为%s" % ("、".join(str(n) for n in v), k) for k, v in x["counts"].items())
        first = min(min(v) for v in x["counts"].values())
        add("review", "count_conflict", first, "page", "", "“%s”的数量前后不一致：%s（可能源自原文不同章节的口径差异，请统一或注明口径）" % (x["noun"], desc))
    # ---- 跨页疑似重复（脚本检测，G38）
    for x in dup_pairs(pages, float(cfg.get("review", {}).get("dup_threshold", 0.6))):
        add("review", "duplicate_bullet", x["pages"][1], x["b"], x["text_b"],
            "与第%d页要点[%s]“%s”疑似重复（相似度 %.2f）" % (x["pages"][0], x["a"], truncate(x["text_a"], 40), x["sim"]),
            suggestion="保留一处，另一处删除（changes 中 action=drop）或改写")
    # ---- 终审仍存在的单页问题
    for x in final_rv["issues"]:
        if x["page"] > 0:
            sev = "review" if x["severity"] in ("high", "medium") or x.get("unverified") else "info"
            add(sev, "review_" + x["type"], x["page"], "page", "", "%s：%s" % (TYPE_CN.get(x["type"]), x["problem"]), x["ids"], x["suggestion"])
    # ---- 标题（及可选要点）蕴含核验
    mode = cfg.get("check", {}).get("entail", "titles")
    votes = max(1, int(cfg.get("check", {}).get("votes", 1)))
    claims = []
    # 跨章归纳页（如“技术重难点与应对”）：每条都是从各章提炼的判断，最容易“归纳出原文没有的东西”，要点也逐条核验（0.6）
    from .outline import synth_chapter_ids
    synth_ch = synth_chapter_ids(plan, scen)
    synth_keys = {q["page_key"] for q in plan.get("pages") or [] if q.get("chapter_id") in synth_ch}
    if mode in ("titles", "all"):
        for p in pages:
            ids = sorted({i for b in p["bullets"] for i in b["ids"]})
            claims.append({"cid": "%s-title" % p["page_key"], "page": p["no"], "target": "title", "text": p["title"], "ids": ids})
            if mode == "all" or p["page_key"] in synth_keys:
                for b in p["bullets"]:
                    claims.append({"cid": b["id"], "page": p["no"], "target": b["id"], "text": b["text"], "ids": b["ids"]})

    pmap = {p["no"]: p for p in pages}

    def ev(c):
        out = []
        for i in c["ids"]:
            b = doc.block(i)
            path = " > ".join(b.get("section_path") or [])[-60:]
            out.append("[%s]（%s）%s" % (i, path, b["text"]))
        tev, tcnt = table_evidence(doc, c["ids"])      # G45：引用表格行时附表头与行数
        if tev:
            out.append("\n【所引表格的表头】\n" + "\n".join(tev) + "\n（所引表格共 %s 行数据）" % "、".join(sorted(tcnt)))
        if c["target"] == "title":
            # 标题是整页的归纳：再给出依据章节的标题，以及全文中与标题关键词最相关、但本页未引用的段落（G9/G28）
            p = pmap[c["page"]]
            heads = [doc.path_str(k) for k in p["sources"] if k in doc.sections]
            if heads:
                out.append("\n【本页依据章节（章节标题也是原文）】\n" + "\n".join("- " + h for h in heads))
            extra = related_blocks(doc, c["text"], set(c["ids"]), int(cfg.get("check", {}).get("entail_extra_blocks", 8)))
            if extra:
                out.append("\n【补充证据：全文中与标题相关、本页未引用的段落】")
                for b in extra:
                    out.append("[%s]（%s）%s" % (b["id"], " > ".join(b.get("section_path") or [])[-60:], truncate(b["text"], 300)))
        return "\n".join(out)

    def ent(c):
        res = []
        for v in range(1, votes + 1):
            r = llm.call_json("entail", {"claim": c["text"], "evidence": ev(c), "claim_kind": "标题（对本页要点的概括）" if c["target"] == "title" else "要点"},
                              target=c["cid"], vote=v)
            res.append((r["data"] or {}))
        return c, res
    rank = {"supported": 0, "partial": 1, "unsupported": 2}
    verdicts = {}
    for out in llm.map(ent, claims, "entail"):
        if isinstance(out, Exception):
            raise out
        c, res = out
        vs = [x for x in res if x.get("verdict") in rank]
        if not vs:
            verdicts[c["cid"]] = {"verdict": "unverified"}
            add("review", "entail_failed", c["page"], c["target"], c["text"], "核验调用失败，未能判定", c["ids"])
            continue
        worst = max(vs, key=lambda x: rank[x["verdict"]])
        verdicts[c["cid"]] = {"verdict": worst["verdict"], "reason": worst.get("reason", ""), "problem": worst.get("problem_span", "")}
        log.gate("entail", c["cid"], worst["verdict"] == "supported", None if worst["verdict"] == "supported" else worst.get("reason", ""))
        if worst["verdict"] == "unsupported":
            add("blocking", "claim_unsupported", c["page"], c["target"], c["text"], "原文不支持：%s" % worst.get("reason", ""), c["ids"], "按原文改写")
        elif worst["verdict"] == "partial":
            add("review", "claim_partial", c["page"], c["target"], c["text"], "部分支持：%s" % worst.get("reason", ""), c["ids"], "按原文收窄表述")
    items.sort(key=lambda x: (SEV_ORDER[x["severity"]], x["page"] if isinstance(x["page"], int) else 0))
    for k, it in enumerate(items, 1):
        it["item_id"] = "V%03d" % k
    sevc = {}
    for it in items:
        sevc[it["severity"]] = sevc.get(it["severity"], 0) + 1
    summary = {"pages": len(pages), "bullets": sum(len(p["bullets"]) for p in pages), "items_by_severity": sevc,
               "title_verdicts": {v: sum(1 for x in verdicts.values() if x["verdict"] == v) for v in ("supported", "partial", "unsupported", "unverified")},
               "review_final_score": (final_rv.get("overall") or {}).get("score"),
               "estimated_minutes": round(sum(p["minutes"] for p in pages) + len(plan["budget"]["structural"]) * plan["budget"]["structural_minutes"], 1),
               "passed": sevc.get("blocking", 0) == 0}
    write_json(wd.stage("check", "check_report.json"), {"summary": summary, "items": items, "verdicts": verdicts})
    decide = [it for it in items if it["severity"] in ("blocking", "review") and isinstance(it["page"], int) and it["page"] > 0]
    write_json(wd.p("review_template.json"), {
        "_说明": "decisions：对每一项填写 decision：keep（保留）/ drop（删除该要点；作用于标题时删除整页）/ edit（在 text 中给出修改后的文字）。"
                "blocking 必须填写，review 不填视为 keep。"
                "changes：不受上面条目限制的直接修改，可改/删任意标题和要点、补充要点、删除或移动页面："
                "{page, target: title 或 b3, action: edit/drop, text} ｜ {page, action: add, text, after: b2（可省略，默认加在最后）} ｜ "
                "{page, action: drop_page} ｜ {page, action: move, after: 页码（0 表示最前）}。要点编号见 checkpoint2.md 成稿中的 [P05-b3]。"
                "整套结构需要大改时请 replan。另存为 review_decisions.json。",
        "decisions": {it["item_id"]: {"decision": "", "text": ""} for it in decide},
        "changes": []})
    write_text(wd.p("checkpoint2.md"), render(pages, items, summary, verdicts, final_rv))
    return summary


def render(pages, items, s, verdicts, rv):
    L = ["# 检查点 2：成稿确认", "",
         "内容页 %d 页、要点 %d 条，估算 %.1f 分钟；编辑终审评分 %s/5；标题核验：%s。" % (
             s["pages"], s["bullets"], s["estimated_minutes"], s["review_final_score"],
             "，".join("%s %d" % ({"supported": "支持", "partial": "部分支持", "unsupported": "不支持", "unverified": "未核验"}[k], v) for k, v in s["title_verdicts"].items() if v)), ""]
    if (rv.get("overall") or {}).get("summary"):
        L += ["> 终审意见：%s" % rv["overall"]["summary"], ""]
    groups = [("deck", "一、整套层面的问题（如需调整，运行 replan --feedback \"意见\" 回到内容规划）"), ("blocking", "二、必须处理"), ("review", "三、建议确认"), ("info", "四、仅供参考")]
    for sev, name in groups:
        its = [x for x in items if x["severity"] == sev]
        if not its:
            continue
        L += ["## %s（%d 项）" % (name, len(its)), ""]
        for x in its[:40]:
            where = "整套" if x["page"] == 0 else "第 %s 页 %s" % (x["page"], x["target"])
            L.append("- **%s** %s：%s%s" % (x["item_id"], where, x["detail"], ("；建议：" + x["suggestion"]) if x.get("suggestion") else ""))
            if x.get("text"):
                L.append("  - 原文字：%s" % truncate(x["text"], 60))
        if len(its) > 40:
            L.append("- ……其余 %d 项见 06_check/check_report.json" % (len(its) - 40))
        L.append("")
    L += ["## 成稿", ""]
    for p in pages:
        v = verdicts.get("%s-title" % p["page_key"], {}).get("verdict")
        mark = {"partial": " ⚠", "unsupported": " ⛔"}.get(v, "")
        L.append("### %d. %s%s" % (p["no"], p["title"], mark))
        for b in p["bullets"]:
            L.append("- [%s] %s  `%s`" % (b["id"], b["text"], " ".join(b["ids"])))
        if p.get("facts"):
            L.append("  - 屏上细节：" + "；".join(f["text"] for f in p["facts"]))
        L.append("")
    return "\n".join(L) + "\n"
