"""第⑥步：应用检查点 2 的决定，导出成果（供后续排版使用）。
content.json：每页、每条要点带原文段落ID → 文件/章节/原文 的完整溯源与检查状态
outline.md  ：人类可读的大纲（含出处、备注、图示建议、用时）
sources.md  ：本 PPT 用到的全部原文段落
"""
import re
import copy

from .checks import caret_to_sup
from .core import EXIT_CONFIG, NeedUser, PipelineError, VERSION, now_iso, read_json, write_json, write_text
from .write import load_ctx


def page_figures(doc, p, plan_page):
    """本页依据中的原文图片，按与本页的相关度排序（图题与标题、要点、规划要点的词语重合）。"""
    from .check import _terms
    want = _terms(p.get("title", "") + "".join(b["text"] for b in p.get("bullets", [])) + "".join(plan_page.get("points") or []))
    hint = "原图" in "".join(plan_page.get("points") or []) or "原图" in str(p.get("visual") or "")
    num_only = plan_page.get("number_sources") or []
    out = []
    for bid in p.get("source_block_ids") or []:
        b = doc.block(bid)
        if not b or not b.get("image"):
            continue
        sk = "%s-S%s" % (b.get("doc"), b.get("section"))
        if any(sk == k or sk.startswith(k + ".") for k in num_only):
            continue          # 只是为核对标题数字补引的章节里的图，与本页无关（实测总体技术路线图因此被三页引用）
        cap = b.get("caption", "")
        ct = {t for t in _terms(re.sub(r"^\s*图\s*[\d\-－—.．]+", "", cap)) if len(t) >= 2}
        score = len(ct & want) + (2 if hint else 0)
        out.append(dict(b["image"], block_id=bid, caption=cap, score=score))
    out.sort(key=lambda f: -f["score"])
    return out


def run(wd, cfg, log, llm=None):
    from . import media
    media.ensure(wd, log)          # 旧工作目录：补取原文图片
    plan, und, scen, brief, doc = load_ctx(wd)
    rep = read_json(wd.stage("check", "check_report.json"))
    items = {it["item_id"]: it for it in rep["items"]}
    dec_file = read_json(wd.p("review_decisions.json"))
    if dec_file is None and llm is not None and cfg.get("check", {}).get("auto_decisions", True):
        # 0.7：不再停在检查点 2，审查问题按默认修改意见处理，处理不了的写入《待核实清单》
        from . import autofix
        dec, pending = autofix.decide(rep["items"], read_json(wd.stage("review", "pages.json")) or [], doc, llm, log)
        dec_file = {"decisions": dec, "changes": [], "_auto": True}
        write_json(wd.p("review_decisions.json"), dec_file)
        write_text(wd.p("待核实清单.md"), autofix.pending_md(pending, dec, items))
        # 0.7.0-dev7：结构化一份，供《注意事项.md》合并（notice.py）
        write_json(wd.stage("export", "pending.json"), {
            "pending": [{"page": it.get("page"), "detail": it.get("detail", "")} for it in pending],
            "fixed": [{"page": items[i]["page"], "text": items[i].get("text") or "",
                       "action": ("改为：" + d["text"]) if d["decision"] == "edit" else "删除"}
                      for i, d in dec.items() if i in items]})
    if dec_file is None:
        raise NeedUser("检查点 2：请把 checkpoint2.md 交给用户确认，把决定写入 review_decisions.json（参照 review_template.json；"
                       "无需修改时写入 {\"decisions\": {}}）", ["checkpoint2.md", "review_template.json"])
    decisions = dec_file.get("decisions", {}) or {}
    missing = [i for i, it in items.items() if it["severity"] == "blocking" and isinstance(it["page"], int) and it["page"] > 0
               and not (decisions.get(i) or {}).get("decision")]
    invalid = []
    for i, d in decisions.items():
        if i not in items:
            invalid.append("%s 不存在" % i)
        elif d.get("decision") not in ("keep", "drop", "edit", "", None):
            invalid.append("%s 的 decision 无效" % i)
        elif d.get("decision") == "edit" and not (d.get("text") or "").strip():
            invalid.append("%s 选择 edit 但没有填写 text" % i)
    pages0 = read_json(wd.stage("review", "pages.json")) or read_json(wd.stage("write", "pages.json"))
    changes, cerr = parse_changes(dec_file.get("changes") or [], pages0)
    invalid += cerr
    if missing:
        raise NeedUser("以下必须处理的问题尚未决定：%s" % ", ".join(missing), ["checkpoint2.md", "review_decisions.json"])
    if invalid:
        raise PipelineError("review_decisions.json 有误：" + "；".join(invalid), EXIT_CONFIG)

    pages = copy.deepcopy(pages0)
    verdicts = rep.get("verdicts", {})
    applied, drop_b, drop_p, edits = [], set(), set(), {}
    for iid, d in decisions.items():
        it, dd = items[iid], (d or {}).get("decision")
        if not dd:
            continue
        pg, tgt = it["page"], it["target"]
        if dd == "drop":
            if "-b" in str(tgt):
                drop_b.add(tgt)
            elif tgt == "title":
                drop_p.add(pg)
            else:
                log.warn("decision_not_applicable", "%s 的 drop 无法作用于 %s，按 keep 处理" % (iid, tgt))
                dd = "keep"
        elif dd == "edit":
            if "-b" in str(tgt) or tgt == "title":
                edits[(pg, tgt)] = d["text"].strip()
            else:
                log.warn("decision_not_applicable", "%s 的 edit 无法作用于 %s，按 keep 处理" % (iid, tgt))
                dd = "keep"
        applied.append({"item": iid, "severity": it["severity"], "category": it["category"], "page": pg, "target": tgt, "decision": dd})
    adds, moves = {}, []
    for c in changes:          # 直接修改（不依赖检查项）：后写的覆盖先写的
        a, pg = c["action"], c["page"]
        if a == "edit":
            edits[(pg, c["target"])] = c["text"]
        elif a == "drop":
            if c["target"] == "title":
                drop_p.add(pg)
            else:
                drop_b.add(c["target"])
        elif a == "drop_page":
            drop_p.add(pg)
        elif a == "add":
            adds.setdefault(pg, []).append(c)
        elif a == "move":
            moves.append(c)
        applied.append(dict(c, item="change", decision=a))
    if moves:
        order = [p["no"] for p in pages]
        for c in moves:
            order.remove(c["page"])
            pos = 0 if c["after"] == 0 else order.index(c["after"]) + 1 if c["after"] in order else len(order)
            order.insert(pos, c["page"])
        pm = {p["no"]: p for p in pages}
        pages = [pm[n] for n in order]
    issues_by = {}
    for it in rep["items"]:
        issues_by.setdefault((it["page"], it["target"]), []).append(it)
    dec_of = {a["item"]: a["decision"] for a in applied}

    def status(pg, tgt, cid=None):
        if (pg, tgt) in edits:
            return {"state": "user_edited_unverified", "issues": []}   # 用户改写的文字不沿用旧判定
        its = issues_by.get((pg, tgt), [])
        st = {"state": "ok" if not its else "has_issues",
              "issues": [{"item_id": i["item_id"], "severity": i["severity"], "detail": i["detail"],
                          "decision": dec_of.get(i["item_id"], "keep")} for i in its]}
        if cid and cid in verdicts:
            st["entail"] = verdicts[cid]
        return st

    out_pages, used = [], {}
    plan_by_key = {q["page_key"]: q for q in plan.get("pages") or []}
    from .outline import synth_chapter_ids
    synth_ids = synth_chapter_ids(plan, scen)
    cover = cover_info(doc)
    title = brief.get("title") or cover.get("title") or clean_title(und.get("doc_title") or doc.docs.get("D1", {}).get("file", "汇报"))
    st = plan["budget"]["structural"]
    sm = plan["budget"]["structural_minutes"]
    if "cover" in st:
        out_pages.append({"kind": "structural", "role": "cover", "title": title, "subtitle": brief.get("subtitle") or scen.get("display_name"), "bullets": [], "minutes": sm})
    if "agenda" in st:
        out_pages.append({"kind": "structural", "role": "agenda", "title": "目录", "bullets": [{"text": x} for x in agenda(plan, doc)], "minutes": sm})
    for p in pages:
        if p["no"] in drop_p:
            continue
        pp = plan_by_key.get(p["page_key"], {})
        q = {"kind": "content", "page_key": p["page_key"], "title": caret_to_sup(edits.get((p["no"], "title"), p["title"])),
             "chapter": pp.get("chapter"), "chapter_id": pp.get("chapter_id"), "figures": page_figures(doc, p, pp),
             "title_status": status(p["no"], "title", "%s-title" % p["page_key"]), "sources": p["sources"],
             "notes": p["notes"], "visual_hint": p.get("visual", {}), "minutes": p["minutes"], "revised_by_review": p.get("revised", False), "bullets": []}
        add_after = {}
        for k, c in enumerate(adds.get(p["no"], []), 1):
            add_after.setdefault(c.get("after") or "__end__", []).append(
                {"id": "%s-u%d" % (p["page_key"], k), "text": caret_to_sup(c["text"]), "user_edited": True, "user_added": True,
                 "provenance": [], "status": {"state": "user_added_unverified", "issues": []}})
        for b in p["bullets"]:
            if b["id"] in drop_b:
                q["bullets"] += add_after.pop(b["id"], [])
                continue
            prov = []
            for i in b["ids"]:
                bk = doc.block(i)
                prov.append({"block_id": i, "file": doc.docs.get(bk.get("doc"), {}).get("file", "用户补充"), "page": bk.get("page"),
                             "section": " > ".join(bk.get("section_path") or []), "quote": bk["text"]})
                used[i] = bk
            q["bullets"].append({"id": b["id"], "text": caret_to_sup(edits.get((p["no"], b["id"]), b["text"])), "user_edited": (p["no"], b["id"]) in edits,
                                 "provenance": prov, "status": status(p["no"], b["id"], b["id"])})
            q["bullets"] += add_after.pop(b["id"], [])
        for rest in add_after.values():
            q["bullets"] += rest
        q["facts"] = []
        for f in p.get("facts") or []:
            prov = []
            for i in f.get("ids") or []:
                bk = doc.block(i)
                if not bk:
                    continue
                prov.append({"block_id": i, "section": " > ".join(bk.get("section_path") or []), "quote": bk["text"]})
                used[i] = bk
            q["facts"].append({"id": f["id"], "text": caret_to_sup(f["text"]), "provenance": prov})
        out_pages.append(q)
    if "closing" in st:
        out_pages.append({"kind": "structural", "role": "closing", "title": scen.get("closing_title", "谢谢！欢迎提问"), "bullets": [], "minutes": sm})
    for i, p in enumerate(out_pages, 1):
        p["no"] = i
    content = {"meta": {"skill_version": VERSION, "generated_at": now_iso(), "title": title, "scenario": brief["scenario"],
                        "audience": brief.get("audience"), "purpose": brief.get("purpose"), "duration_min": brief.get("duration_min"),
                        "storyline": plan["storyline"], "doc_type": und.get("doc_type"), "cover": cover,
                        "density": plan["budget"].get("density", "standard"),
                        "chapters": ([{"title": d["title"], "synthesis": any(i in synth_ids for i in d["ids"])}
                                      for d in plan["display_chapters"]] if plan.get("display_chapters") else
                                     [{"title": c["title"], "synthesis": c["id"] in synth_ids} for c in plan.get("chapters") or []]),
                        "inputs": [{"doc_id": d["id"], "file": d["file"], "sha256": d["sha256"]} for d in doc.docs.values()]},
               "quality": dict(rep["summary"], user_decisions=applied), "pages": out_pages}
    write_json(wd.stage("export", "content.json"), content)
    write_text(wd.stage("export", "outline.md"), render_md(content))
    write_text(wd.stage("export", "sources.md"), "\n".join("- `[%s]` %s（%s）" % (k, v["text"], " > ".join(v.get("section_path") or []))
                                                        for k, v in sorted(used.items())) + "\n")
    write_json(wd.stage("export", "decisions_applied.json"), applied)
    return {"pages": len(out_pages), "content_pages": sum(1 for p in out_pages if p["kind"] == "content"),
            "bullets": sum(len(p["bullets"]) for p in out_pages if p["kind"] == "content"),
            "dropped_bullets": len(drop_b), "dropped_pages": len(drop_p), "edits": len(edits), "sources_used": len(used)}


def parse_changes(raw, pages):
    """检查点 2 的直接修改。要点可写 b3 或 P05-b3。返回 (规范化后的修改, 错误)。"""
    pm = {p["no"]: p for p in pages}
    out, err = [], []
    for k, c in enumerate(raw):
        if not isinstance(c, dict):
            err.append("changes[%d] 不是对象" % k)
            continue
        a, pg = c.get("action"), c.get("page")
        if pg not in pm:
            err.append("changes[%d] 的 page=%s 不存在" % (k, pg))
            continue
        p = pm[pg]
        ids = {b["id"] for b in p["bullets"]}

        def bid(t):
            t = str(t or "").strip()
            if t == "title":
                return t
            t = t if t.startswith(p["page_key"]) else "%s-%s" % (p["page_key"], t)
            return t if t in ids else None
        if a in ("edit", "drop"):
            t = bid(c.get("target"))
            if not t:
                err.append("changes[%d] 的 target=%s 在第 %s 页不存在" % (k, c.get("target"), pg))
            elif a == "edit" and not (c.get("text") or "").strip():
                err.append("changes[%d] 选择 edit 但没有 text" % k)
            else:
                out.append({"action": a, "page": pg, "target": t, "text": (c.get("text") or "").strip()})
        elif a == "add":
            after = bid(c.get("after")) if c.get("after") else None
            if c.get("after") and not after:
                err.append("changes[%d] 的 after=%s 在第 %s 页不存在" % (k, c.get("after"), pg))
            elif not (c.get("text") or "").strip():
                err.append("changes[%d] 选择 add 但没有 text" % k)
            else:
                out.append({"action": "add", "page": pg, "after": after, "text": c["text"].strip()})
        elif a == "drop_page":
            out.append({"action": "drop_page", "page": pg})
        elif a == "move":
            af = c.get("after")
            if af != 0 and af not in pm:
                err.append("changes[%d] 的 after=%s 不是有效页码（0 表示最前）" % (k, af))
            else:
                out.append({"action": "move", "page": pg, "after": af})
        else:
            err.append("changes[%d] 的 action=%s 无效（edit/drop/add/drop_page/move）" % (k, a))
    return out, err


def clean_title(t):
    import re
    return re.sub(r"\.(docx?|pdf|pptx?|xlsx?|md|txt|wps|html?)$", "", (t or "").strip(), flags=re.I)


def cover_info(doc):
    """从原文封面（标题前内容）提取项目名称、文档类型、建设/承建单位与日期（0.5）。找不到的项留空。"""
    import re
    blocks = [b for b in doc.blocks if b.get("section") in ("0", "") or str(b.get("id", "")).split("-")[1:2] == ["S0"]][:30]
    lines = [b["text"].strip() for b in blocks if b["text"].strip()]
    out, fields = {}, {}
    kind_re = re.compile(r"^(技术方案|实施方案|建设方案|设计方案|需求(分析)?(说明书|报告)|可行性研究报告|开题报告|工作(总结|报告)|.{0,8}(方案|报告|说明书))$")
    title_parts = []
    for ln in lines:
        if kind_re.match(ln):
            out["doc_kind"] = ln
            break
        if re.search(r"[：:]", ln) and re.match(r"^(项目)?(建设|承建|编制|委托|申报|实施|协作)单位", ln):
            break
        if re.match(r"^(项目编号|项目代码|编号|密级|版本号?)\s*[:：]", ln):      # 封面上的编号、密级不是标题的一部分
            continue
        if len(ln) <= 60 and ln not in title_parts:
            title_parts.append(ln)
    if title_parts and out.get("doc_kind"):
        out["title"] = "".join(title_parts)
    cur = None
    for ln in lines:
        m = re.match(r"^(?:项目)?(建设单位|承建单位|编制单位|委托单位|申报单位|实施单位|协作单位|依托部门)\s*[:：]\s*(.*)$", ln)
        if m and m.group(2).strip() and not re.search(r"盖章", m.group(1) + m.group(2)):
            cur = m.group(1)
            fields[cur] = m.group(2).strip()
            continue
        if cur and re.search(r"(公司|中心|研究院|大学|局|厅|院|所)$", ln) and "：" not in ln and ":" not in ln:
            fields[cur] += "、" + ln
            continue
        cur = None
        if re.match(r"^[二〇○一二三四五六七八九零0-9]{4}\s*年\s*[一二三四五六七八九十0-9]{1,2}\s*月", ln) and "日期" not in fields:
            fields["日期"] = ln
    if fields:
        out["fields"] = fields
    return out


def agenda(plan, doc):
    """目录：优先用规划给出的目录项；没有则用各页依据的一级章节标题（按出现顺序去重）。"""
    if plan.get("agenda"):
        return plan["agenda"]
    out = []
    for p in plan["pages"]:
        for s in p["sources"]:
            top = s.split(".")[0]
            t = doc.sections[top]["title"] if top in doc.sections else None
            if t and t not in out:
                out.append(t)
    return out


def render_md(c):
    m = c["meta"]
    q = c["quality"]
    L = ["# %s" % m["title"], "",
         "> 场景：%s ｜ 听众：%s ｜ 目的：%s ｜ 时长：%s 分钟" % (m["scenario"], m.get("audience") or "-", m.get("purpose") or "-", m.get("duration_min") or "-"),
         "> 汇报主线：%s" % m.get("storyline", ""),
         "> 质量：编辑终审 %s/5；标题核验 %s；待处理问题 %s" % (q.get("review_final_score"), q.get("title_verdicts"), q.get("items_by_severity")), ""]
    for p in c["pages"]:
        tmark = ""
        if p["kind"] == "content":
            ts = p["title_status"]
            tmark = " ✎(用户修改，未核验)" if ts["state"] == "user_edited_unverified" else (" ⚠" if ts["state"] == "has_issues" else "")
        L.append("## %d. %s%s" % (p["no"], p["title"], tmark))
        if p.get("subtitle"):
            L.append("*%s*" % p["subtitle"])
        for b in p.get("bullets", []):
            if p["kind"] == "content":
                st = b["status"]["state"]
                mk = {"user_edited_unverified": " ✎(用户修改，未核验)", "user_added_unverified": " ✚(用户补充，未核验)",
                      "has_issues": " ⚠"}.get(st, "")
                L.append("- %s%s  <sub>%s</sub>" % (b["text"], mk, " ".join(x["block_id"] for x in b["provenance"])))
            else:
                L.append("- %s" % b["text"])
        if p["kind"] == "content" and p.get("facts"):
            L += ["", "屏上细节："] + ["  - %s  <sub>%s</sub>" % (f["text"], " ".join(x["block_id"] for x in f["provenance"]))
                                      for f in p["facts"]]
        if p["kind"] == "content":
            vh = p.get("visual_hint") or {}
            if vh:
                L += ["", "图示建议：%s — %s" % (vh.get("type", ""), vh.get("description", ""))]
            if p.get("notes"):
                L += ["", "演讲备注：%s" % p["notes"]]
        L += ["", "<sub>建议用时 %.1f 分钟</sub>" % (p.get("minutes") or 0), ""]
    return "\n".join(L)
