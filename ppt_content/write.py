"""第③步：逐页写作。每页只把规划中指定章节的完整原文（带章节路径）交给模型，写标题、要点、备注、图示建议。

确定性校验（不合格则带问题清单重写）：引用的段落必须是提供给本页的原文；数字必须出现在所引段落中；
不得出现原文没有的程度词；原文是计划/预计时要保留限定语气；标题、要点、备注的篇幅。
"""
import os
import re

from .checks import dwidth, enum_counts, extract_numbers, norm, numbers_not_in, overstatements, truncate
from .core import load_scenario, read_json, write_json, write_text
from .docmodel import Doc
from .plan import TOPIC_ENDINGS

# "目标"不算限定语气：技术文档中"建设目标""目标值：≤2分钟"是要求而非预期（0.3.0，实测误报 20 余次）
PLAN_HEDGES = ["预计", "计划", "拟", "力争", "有望", "争取"]

FACTS_RULE = ("6. facts：屏上细节 3—%d 条，每条不超过 %s 字，写在 PPT 图示里给听众看、不必口述的具体事实："
              "参数与取值、组成项、步骤、数量、规则、示例（如“根标识符码 2 位，取 MA”“切片重叠 10%%～15%%”）。"
              "\n   - 每条只讲一个具体事实，补充要点没写的细节，不要重复要点原话；ids 写出处段落ID，数字照抄原文。"
              "\n   - 本页原文没有这类具体内容时可以少写。")


CONFIRM_TITLE = re.compile(r"(待|需|请).{0,12}(确认|明确|决定|拍板)|确认事项|待定事项")
CONFIRM_SRC = re.compile(r"待确认|待明确|待定|共同确认|另行确认|予以明确|建议.{0,30}明确|评审时.{0,10}明确|由.{0,10}确定|尚未约定|未约定|确认后")


def table_evidence(doc, ids):
    """引用了表格行时：把该表的表头行（前 3 行）也作为证据，并允许使用该表的数据行数（"交付17项成果"）。"""
    import re
    cache = doc.__dict__.setdefault("_tables", {})
    if not cache:
        for b in doc.blocks:
            m = re.match(r"^(.*-T\d+)-R(\d+)$", b["id"])
            if m:
                cache.setdefault(m.group(1), []).append((int(m.group(2)), b))
    texts, counts = [], set()
    for t in {re.match(r"^(.*-T\d+)-R\d+$", i).group(1) for i in ids if re.match(r"^(.*-T\d+)-R\d+$", i)}:
        rows = sorted(cache.get(t, []), key=lambda x: x[0])
        texts += [b["text"] for r, b in rows if r <= 2]
        counts.add(str(sum(1 for r, b in rows if not b["text"].startswith("表头"))))
    return texts, counts


# 讲稿中的元话语：对本页的说明、给汇报人的提示（0.6：实测备注写成“本页回答‘……’。政策依据按时间顺序讲：”）
NOTES_META = re.compile(r"本页|这一页|此页|该页|本张|回答[“\"「].{0,40}[”\"」]|按(时间|顺序|时间顺序|.{0,6}顺序)讲|重点(讲|介绍|说明)|可(以)?强调|建议(讲|说|强调|提及)|讲者|汇报人(可|应|需)|要点[一二三四1-4]|（[^）]{0,6}页）")


def notes_range(minutes, wc):
    """讲稿字数按本页时长：每分钟 chars_per_minute 字（默认 260，正常汇报语速），允许 0.8—1.2 倍。"""
    cpm = float(wc.get("notes_chars_per_minute", 270))
    m = float(minutes or 0)
    if m <= 0:
        return int(wc.get("notes_min_chars", 80)), int(wc.get("notes_max_chars", 400))
    return max(int(wc.get("notes_min_chars", 80)), int(m * cpm * 0.8)), max(120, int(m * cpm * 1.2))


def page_issues(d, ctx, cfg):
    """返回问题清单（用于模型重写反馈，也用于最终核验）。"""
    wc, lex = cfg.get("write", {}), cfg.get("lexicon", {})
    doc, allowed, src_text = ctx["doc"], ctx["allowed"], ctx["src_text"]
    tn = ctx.get("title_nums", set())
    iss = []
    t = d.get("title", "")
    if not t:
        iss.append("缺少标题")
    if dwidth(t) > int(wc.get("max_title_chars", 32)):
        iss.append("标题 %d 字（按显示宽度计），超过 %s 字" % (dwidth(t), wc.get("max_title_chars", 32)))
    if t.rstrip("。").endswith(TOPIC_ENDINGS):
        iss.append("标题“%s”是话题式，请直接写出本页结论" % t)
    miss = numbers_not_in(t, [src_text], tn)
    if miss:
        iss.append("标题中的数字 %s 不在本页原文中" % miss)
    ov = overstatements(t, [src_text], lex.get("overstatement", []))
    if ov:
        iss.append("标题使用了原文没有的程度词 %s" % ov)
    bl = d.get("bullets", [])
    # 多 1 条只记提示（核验阶段为 info），不为此重写：实测内容密集的页常写 6 条，重试 3 次仍是 6 条
    if len(bl) < int(wc.get("min_bullets", 2)) or len(bl) > int(wc.get("max_bullets", 5)) + (0 if ctx.get("strict") else 1):
        iss.append("要点 %d 条，应为 %s—%s 条" % (len(bl), wc.get("min_bullets", 2), wc.get("max_bullets", 5)))
    for k, b in enumerate(bl):
        txt, ids = b.get("text", ""), b.get("ids") or []
        if dwidth(txt) > int(wc.get("max_bullet_chars", 45)):
            iss.append("要点%d 有 %d 字（按显示宽度），超过 %s 字，请只保留一件事并精简" % (k + 1, dwidth(txt), wc.get("max_bullet_chars", 45)))
        if not ids:
            iss.append("要点%d 没有注明段落ID" % (k + 1))
            continue
        bad = [i for i in ids if i not in allowed]
        if bad:
            iss.append("要点%d 引用的段落 %s 不在本页提供的原文中" % (k + 1, bad[:3]))
            continue
        ev = [doc.block(i)["text"] for i in ids]
        tev, tcnt = table_evidence(doc, ids)
        miss = numbers_not_in(txt, ev + tev, set(tn) | tcnt | enum_counts(txt))
        if miss:
            iss.append("要点%d 的数字 %s 不在其引用的段落中（数字须照抄原文，并引用包含该数字的段落）" % (k + 1, miss))
        ov = overstatements(txt, [src_text], lex.get("overstatement", []))   # 程度词对照本页全部原文（原文他处写了"唯一依据"就不算夸大）
        if ov:
            iss.append("要点%d 使用了原文没有的程度词 %s" % (k + 1, ov))
        nums = extract_numbers(txt)
        for i in ids:
            et = doc.block(i)["text"]
            hs = [h for h in PLAN_HEDGES if h in et]
            if hs and nums & extract_numbers(et) and not any(norm(h) in norm(txt) for h in lex.get("hedges", [])):
                iss.append("要点%d 引用的原文是“%s”类表述，要点中须保留相应语气" % (k + 1, hs[0]))
                break
    # "需确认事项"页：每条要点的依据须写明待确认（G46）
    if ctx.get("allow_confirm", True) and CONFIRM_TITLE.search(ctx.get("plan_title", "") or t) and CONFIRM_SRC.search(src_text):   # 原文没有待确认表述时不逼模型凑（G48）
        for k, b in enumerate(bl):
            ev = "".join(doc.block(i)["text"] for i in b.get("ids") or [] if i in allowed)
            if ev and not CONFIRM_SRC.search(ev):
                iss.append("要点%d 列为需确认事项，但所引原文没有“待确认/共同确认/建议……明确”之类的表述；原文已有安排的事项不要列入" % (k + 1))
    # 屏上细节（0.5，dense）：支撑要点的具体事实，用于上屏，不必口述；同样须有出处
    mf = int(ctx.get("max_facts", 0))
    facts = d.get("facts") or []
    if mf == 0 and facts:
        facts = []
    if len(facts) > mf:
        iss.append("facts 有 %d 条，最多 %d 条" % (len(facts), mf))
    if mf and len(facts) < min(3, mf) and len(src_text) > 1500:
        iss.append("facts 只有 %d 条：本页原文较丰富，请补充上屏用的具体事实（参数、组成、步骤、数量），至少 3 条" % len(facts))
    btxt = [norm(b.get("text", "")) for b in bl]
    for k, f in enumerate(facts[:mf] if mf else []):
        txt, ids = (f.get("text", "") if isinstance(f, dict) else str(f)), (f.get("ids") or [] if isinstance(f, dict) else [])
        if dwidth(txt) > int(wc.get("max_fact_chars", 34)) + 2:      # 超 1—2 字由版面阶段处理，不为此重写
            iss.append("细节%d 有 %d 字，超过 %s 字" % (k + 1, dwidth(txt), wc.get("max_fact_chars", 34)))
        if not ids or [i for i in ids if i not in allowed]:
            iss.append("细节%d 没有注明段落ID，或引用了本页原文以外的段落" % (k + 1))
            continue
        ev = [doc.block(i)["text"] for i in ids]
        tev, tcnt = table_evidence(doc, ids)
        miss = numbers_not_in(txt, ev + tev, set(tn) | tcnt | enum_counts(txt))
        if miss:
            iss.append("细节%d 的数字 %s 不在其引用的段落中" % (k + 1, miss))
        if norm(txt) in btxt:
            iss.append("细节%d 与要点原话重复，细节应补充要点没写的具体内容" % (k + 1))
    notes = d.get("notes", "")
    # 0.6：最终讲稿在版式定稿后单独生成（visual/speech.py），这里的备注只是讲解要点，供审查和排版参考，不再为篇幅、口吻重写
    if len(notes) < 40:
        iss.append("备注过短（%d 字），写出本页的讲解要点" % len(notes))
    miss = numbers_not_in(notes, [src_text], tn)
    if miss:
        iss.append("演讲备注中的数字 %s 不在本页原文中" % miss)
    return iss


def build_ctx(doc, page, cfg, title_nums=None):
    text, used, trunc = doc.source_text(page["sources"], int(cfg.get("write", {}).get("max_source_chars", 24000)))
    return {"doc": doc, "src": text, "allowed": set(used), "src_text": "\n".join(doc.block(i)["text"] for i in used), "truncated": trunc,
            "title_nums": title_nums if title_nums is not None else doc.title_numbers()}


def write_one(wd, cfg, log, llm, page, plan, und, scen, brief, doc, review_feedback=None, attempt_tag=""):
    from .plan import density
    dname, dc = density(brief, scen, cfg)
    cfg = dict(cfg, write=dict(cfg.get("write", {}), max_bullets=int(dc.get("max_bullets") or cfg.get("write", {}).get("max_bullets", 5))))
    ctx = build_ctx(doc, page, cfg, doc.title_numbers([und.get("doc_title", "")]))
    ctx["plan_title"] = page.get("title_draft", "")
    ctx["minutes"] = page.get("minutes")
    ctx["allow_confirm"] = scen.get("confirm_page", True)     # 评审类不设“需确认事项”页：标题含“待评审明确”时不按确认事项页检查（实测误报 3 条）
    ctx["max_facts"] = int(dc.get("max_facts") or 0)
    if ctx["truncated"]:
        log.warn("source_truncated", "%s 依据原文超过 %s 字，已截断（可在规划中缩小依据章节）" % (page["page_key"], cfg["write"]["max_source_chars"]))
    pages = plan["pages"]
    i = page["no"]
    summ = "\n".join("[%s] %s：%s" % (c["section"], c["title"], c["summary"]) for c in und["chapters"]
                     if any(c["section"] == s or c["section"].startswith(s + ".") or s.startswith(c["section"] + ".") for s in page["sources"]))
    wc = cfg.get("write", {})
    chap = next((c for c in plan.get("chapters") or [] if c.get("id") == page.get("chapter_id")), None)
    if chap:
        chapter_info = "第%d章“%s”（%s）" % (page.get("chapter", 0), chap.get("title", ""), chap.get("message") or "")
        from .outline import synth_chapter_ids
        if chap["id"] in synth_chapter_ids(plan, scen):
            chapter_info += ("\n- 本页是跨章归纳页（原文没有专章，内容从各章提炼）：每条要点写成“难点/亮点/措施 —— 原文中的具体做法或数字”，"
                             "ids 必须引用写明这一做法的原文段落；原文没写的不要归纳出来，宁可少写一条")
    else:
        chapter_info = "（未分章）"
    r = llm.call_json("write_page", {
        "chapter_info": chapter_info,
        "page_no": i, "total": len(pages), "storyline": plan["storyline"],
        "audience": brief.get("audience", ""), "purpose": brief.get("purpose", ""), "scenario": scen.get("display_name"),
        "title_draft": page["title_draft"], "question": page["question"], "points": "；".join(page.get("points", [])),
        "minutes": page["minutes"], "prev_title": pages[i - 2]["title_draft"] if i > 1 else "（本页是第一个内容页）",
        "next_title": pages[i]["title_draft"] if i < len(pages) else "（本页是最后一个内容页）",
        "all_titles": "\n".join("%d. %s ｜分工：%s" % (p["no"], p["title_draft"], "；".join(p.get("points", [])[:5]) or "—") for p in pages),
        "title_status": "用户已在检查点 1 确认，保持方向" if os.path.exists(wd.stage("plan", "approved.json")) else "草稿",
        "doc_issues": "\n".join("- %s %s" % (it.get("text", ""), it.get("ids", [])) for it in und.get("doc_issues", [])
                                if set(it.get("ids", []) or []) & ctx["allowed"]) or "（无）",
        "chapter_summaries": summ or "（无）", "source_text": ctx["src"],
        "max_title_chars": wc.get("max_title_chars", 32), "min_bullets": wc.get("min_bullets", 2), "max_bullets": wc.get("max_bullets", 5),
        "max_bullet_chars": wc.get("max_bullet_chars", 45), "notes_min": wc.get("notes_min_chars", 80), "notes_max": 300,
        "title_style": scen.get("title_style", "结论式标题"),
        "review_feedback": review_feedback or "（无）",
        "facts_rule": (FACTS_RULE % (ctx["max_facts"], wc.get("max_fact_chars", 34))) if ctx["max_facts"] else
                      "6. facts：本场景不需要，输出空数组 []。",
    }, target=page["page_key"] + attempt_tag, validator=lambda d: page_issues(d, ctx, cfg))
    d = r["data"] or {}
    out = {"no": i, "page_key": page["page_key"], "title": d.get("title", page["title_draft"]), "density": dname,
           "bullets": [{"id": "%s-b%d" % (page["page_key"], k + 1), "text": b.get("text", ""), "ids": [x for x in b.get("ids") or [] if x in ctx["allowed"]]}
                       for k, b in enumerate(d.get("bullets", []))],
           "facts": [{"id": "%s-f%d" % (page["page_key"], k + 1), "text": f.get("text", ""),
                      "ids": [x for x in f.get("ids") or [] if x in ctx["allowed"]]}
                     for k, f in enumerate((d.get("facts") or [])[:ctx["max_facts"]]) if isinstance(f, dict) and f.get("text")],
           "notes": d.get("notes", ""), "visual": d.get("visual") or {}, "minutes": float(d.get("minutes") or page["minutes"]),
           "sources": page["sources"], "source_block_ids": sorted(ctx["allowed"]), "llm_ok": r["ok"], "residual_issues": r["issues"],
           "revised": bool(review_feedback)}
    log.gate("page_written_clean", page["page_key"] + attempt_tag, r["ok"], None if r["ok"] else r["issues"][0])
    return out


def load_ctx(wd):
    brief = wd.brief()
    return (read_json(wd.stage("plan", "plan.json")), read_json(wd.stage("understand", "understanding.json")),
            load_scenario(brief["scenario"]), brief, Doc(wd))


def run(wd, cfg, log, llm):
    # 重新写作后，检查点 2 的旧决定（按旧成稿的条目编号）一律过期
    if os.path.exists(wd.p("review_decisions.json")):
        from .core import now_iso
        os.makedirs(wd.p("answers_history"), exist_ok=True)
        os.replace(wd.p("review_decisions.json"), wd.p("answers_history", "%s_review_decisions.json" % now_iso()[:19].replace(":", "")))
        log.info("review_decisions_archived", "重新写作：检查点 2 的旧决定已归档")
    plan, und, scen, brief, doc = load_ctx(wd)
    res = llm.map(lambda p: write_one(wd, cfg, log, llm, p, plan, und, scen, brief, doc), plan["pages"], "write")
    pages = []
    for x in res:
        if isinstance(x, Exception):
            raise x
        pages.append(x)
    write_json(wd.stage("write", "pages.json"), pages)
    write_text(wd.stage("write", "draft.md"), render_pages(pages, "初稿"))
    return {"pages": len(pages), "bullets": sum(len(p["bullets"]) for p in pages),
            "pages_with_residual_issues": sum(1 for p in pages if not p["llm_ok"]),
            "avg_bullet_chars": round(sum(len(b["text"]) for p in pages for b in p["bullets"]) / max(1, sum(len(p["bullets"]) for p in pages)), 1)}


def render_pages(pages, label):
    L = ["# PPT 内容（%s）" % label, ""]
    for p in pages:
        L.append("## %d. %s" % (p["no"], p["title"]))
        for b in p["bullets"]:
            L.append("- %s  <sub>%s</sub>" % (b["text"], " ".join(b["ids"])))
        if p.get("visual"):
            L.append("\n图示：%s — %s" % (p["visual"].get("type", ""), p["visual"].get("description", "")))
        L.append("\n备注：%s\n" % p["notes"])
    return "\n".join(L)
