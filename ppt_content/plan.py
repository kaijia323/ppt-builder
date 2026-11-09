"""第②步：内容规划。根据文档理解卡、听众与场景关注点，规划"讲哪些、不讲哪些、每页讲什么"。

检查点 1：用户确认页面规划。answers.json：
  {"decision": "approve"}                                 确认，进入写作
  {"decision": "revise", "feedback": "第5页和第6页合并……"}  按意见重新规划
  {"gaps": {"G1": {"choice": "text", "text": "……"}}}       为缺口补充内容（会自动重新规划以纳入）
"""
import math
import os
import re

from .checks import dwidth, extract_numbers, numbers_not_in, truncate
from .core import (EXIT_CONFIG, NeedUser, PipelineError, load_scenario, now_iso, read_json, write_json, write_text)
from .docmodel import Doc
from .understand import compact

TOPIC_ENDINGS = ("介绍", "分析", "概述", "概况", "说明", "简介", "情况")

DENSITY_RULES = {
    "standard": "讲解型：每页讲透一个主题，页数少而精。",
    "dense": ("评审型高密度（对标专家评审汇报）：听众会边听边看屏幕，每页信息要足。"
              "\n- 页面划分到原文三级章节这一层：篇幅大的章节（如“智能体构建”“技术方案”）按其实现环节拆页，相关的小环节两两合并，"
              "不要把十几个小节压成两三页；"
              "\n- 评审常看的环节各有落脚页：建设内容总览、已有基础、总体技术路线、各核心技术环节（数据处理/编码规则/关系构建/检索问答/模型部署与评测/标注与质控等）、"
              "成果与指标、实施计划，以及测试验收、质量与安全、技术重难点与创新点（原文有依据时）；"
              "\n- 每页 points 写 4—6 个，指出本页要上屏的具体事实（数字、组成、步骤、参数），不要只写方向。"),
}


def density(brief, scen, cfg):
    """内容密度：brief.density > 场景默认 > standard。返回 (名称, 参数)。"""
    ds = cfg.get("plan", {}).get("densities", {}) or {}
    name = brief.get("density") or scen.get("density") or "standard"
    if name not in ds:
        name = "standard"
    return name, dict(ds.get(name) or {})


def budget(brief, scen, cfg):
    pc = cfg.get("plan", {})
    dname, dc = density(brief, scen, cfg)
    dur = float(brief.get("duration_min") or scen.get("default_duration_min", 15))
    struct = scen.get("structural_pages", ["cover", "agenda", "closing"])
    smin = float(scen.get("structural_minutes", 0.3))
    content = max(1.0, dur - smin * len(struct))
    mpp = float(brief.get("minutes_per_page") or dc.get("minutes_per_page") or scen.get("minutes_per_page", 1.4))
    pmax = max(2, int(math.floor(content / float(dc.get("min_minutes_per_page") or pc.get("min_minutes_per_page", 1.0)))))
    pmin = max(2, int(math.ceil(content / float(dc.get("max_minutes_per_page") or pc.get("max_minutes_per_page", 2.0)))))
    rec = min(pmax, max(pmin, int(round(content / mpp))))
    if brief.get("max_pages"):
        cap = max(2, int(brief["max_pages"]) - len(struct))
        pmax, rec, pmin = min(pmax, cap), min(rec, cap), min(pmin, cap)
    return {"duration_min": dur, "content_minutes": round(content, 1), "structural": struct, "structural_minutes": smin,
            "min_pages": pmin, "max_pages": pmax, "recommended_pages": rec, "minutes_per_page": mpp, "density": dname}


def body_sections(doc, und):
    """正文章节（参与页数分配与覆盖检查）：排除重要性为低的章节（附件、清单）与标题前内容。"""
    low = {c["section"] for c in und["chapters"] if c.get("importance") == "low"}
    out = {}
    for k, sec in doc.sections.items():
        if sec["level"] < 1 or any(k == l or k.startswith(l + ".") for l in low):
            continue
        out[k] = sec
    return out


def allocation_issues(doc, und, pages, cfg):
    """页数分配与原文工作重心相称（0.5，dense）：占正文 15% 以上的一、二级章节，
    至少分到 份额 × 页数 × 0.5 页（一页引用多个章节时按比例计）。"""
    pc = cfg.get("plan", {})
    body = body_sections(doc, und)
    tops = [k for k, sec in body.items() if sec["level"] in (1, 2)]
    total = float(sum(doc.subtree_chars(k) for k, sec in body.items() if sec["level"] == 1) or 1)
    n = len(pages)
    iss = []
    for k in tops:
        share = doc.subtree_chars(k) / total
        if share < float(pc.get("allocation_min_share", 0.15)):
            continue
        got = 0.0
        for p in pages:
            srcs = [doc.normalize_key(x) for x in p.get("sources") or []]
            if not srcs:
                continue
            hit = sum(1 for x in srcs if x == k or x.startswith(k + "."))
            got += hit / float(len(srcs))
        need = int(share * n * float(pc.get("allocation_factor", 0.5)))
        if got + 0.01 < need:
            iss.append("章节 %s“%s”占正文 %.0f%%，目前只分到约 %.1f 页，至少应有 %d 页：按其中听众关心的子环节拆页"
                       % (k, doc.sections[k]["title"], share * 100, got, need))
    return iss


def coverage_issues(doc, und, pages, decisions, cfg):
    """实质性小节的覆盖（0.5，dense）：占正文 1.5% 以上的三级章节，须被某页引用（引用其本身、上级或下级均可），
    或在 section_decisions 中给出 notes/skip 的理由。"""
    pc = cfg.get("plan", {})
    body = body_sections(doc, und)
    total = float(sum(doc.subtree_chars(k) for k, sec in body.items() if sec["level"] == 1) or 1)
    used = {doc.normalize_key(x) for p in pages for x in p.get("sources") or []}
    decided = {doc.normalize_key(x.get("section", "")) for x in decisions or [] if x.get("decision") in ("notes", "skip")}
    miss = []
    for k, sec in body.items():
        if sec["level"] != 3 or doc.subtree_chars(k) / total < float(pc.get("coverage_min_share", 0.015)):
            continue
        cov = any(u == k or u.startswith(k + ".") or (k.startswith(u + ".") and doc.sections.get(u, {}).get("level", 9) >= 2)
                  for u in used)
        if not cov and k not in decided:
            miss.append("%s %s（%d字）" % (k, sec["title"], doc.subtree_chars(k)))
    if miss:
        return ["以下实质性小节既没有页面引用，也没有在 section_decisions 中说明不讲的理由：%s。高密度汇报要讲到这一层，"
                "可单独成页或与相关小节合并成页（sources 写到小节键）" % "；".join(miss[:12])]
    return []


def checklist(scen):
    return [{"id": "C%d" % i, "text": t} for i, t in enumerate(scen.get("focus_checklist", []), 1)]


def run(wd, cfg, log, llm, feedback=None):
    brief = wd.brief()
    scen = load_scenario(brief["scenario"])
    doc = Doc(wd)
    und = read_json(wd.stage("understand", "understanding.json"))
    bud = budget(brief, scen, cfg)
    dname, dcfg = density(brief, scen, cfg)
    # 是否允许“需确认事项”页：场景默认（评审类不设）；用户在必须包含或修改意见中明确要求时允许
    ask = (brief.get("must_include") or "") + " ".join(f.get("feedback", "") for f in read_json(wd.stage("plan", "feedback_history.json"), []) or [])
    allow_confirm = scen.get("confirm_page", True) or bool(re.search(r"确认事项|待确认|提请", ask + (feedback or "")))
    cl = checklist(scen)
    imp = {c["section"]: c["importance"] for c in und["chapters"]}
    IMPN = {"high": "重要性高", "medium": "重要性中", "low": "重要性低"}
    outline = doc.outline_text(notes={k: IMPN.get(v, "") for k, v in imp.items()})
    high_secs = [c["section"] for c in und["chapters"] if c["importance"] == "high" and c["level"] <= 2]
    prev = read_json(wd.stage("plan", "plan.json"))
    fb_hist = read_json(wd.stage("plan", "feedback_history.json"), [])
    if not feedback and fb_hist:
        # 不带修改意见重新规划（直接运行 plan）= 从头规划：以前的修改意见针对的是旧大纲（页号、章节都已不同），归档不再沿用
        # （实测：0.2 版时提的“第4页……第13页……”被 0.6 的新大纲自动沿用）
        os.makedirs(wd.p("answers_history"), exist_ok=True)
        os.replace(wd.stage("plan", "feedback_history.json"),
                   wd.p("answers_history", "%s_feedback_history.json" % now_iso()[:19].replace(":", "")))
        log.info("plan_feedback_archived", "从头规划：以前的 %d 条修改意见已归档，不再沿用" % len(fb_hist))
        fb_hist = []
    # 新一版规划生成时，旧的确认记录（检查点 1/2）一律过期：否则直接运行 plan 后再 run，
    # 会用上一版的 approve 自动通过这一版（0.6 实测：旧工作目录里留着 0.4 时的 answers.json）
    archive_answers(wd)
    if feedback:
        fb_hist.append({"at": now_iso(), "feedback": feedback})
        write_json(wd.stage("plan", "feedback_history.json"), fb_hist)
    user_sup = "\n".join("[%s] %s" % (b["id"], b["text"]) for b in doc.user_blocks.values()) or "（无）"
    tol = float(cfg.get("plan", {}).get("duration_tolerance", 0.1)) + 0.05

    def validate(d):
        iss = []
        pages = d.get("pages", [])
        n = len(pages)
        if n < bud["min_pages"] or n > bud["max_pages"]:
            iss.append("内容页 %d 页，不在允许范围 %d—%d 页内（建议 %d 页左右）" % (n, bud["min_pages"], bud["max_pages"], bud["recommended_pages"]))
        for i, p in enumerate(pages, 1):
            srcs = p.get("sources") or []
            if not srcs:
                iss.append("第%d页没有 sources" % i)
            bad = [s for s in srcs if not (doc.valid_section(doc.normalize_key(s)) or doc.has_block(s))]
            if bad:
                iss.append("第%d页 sources 中的 %s 不存在，请使用目录方括号中的章节键" % (i, bad[:3]))
            # 标题草稿的长度、话题式只在规划后记警告：写作阶段会重写标题，不值得为此重送全文（0.3.0）
        got = {c.get("item") for c in d.get("checklist_coverage", [])}
        miss = [c["id"] for c in cl if c["id"] not in got]
        if miss:
            iss.append("checklist_coverage 缺少关注点 %s 的覆盖说明（每项都要说明由哪一页覆盖，或为什么不覆盖）" % miss)
        for c in d.get("checklist_coverage", []):
            badp = [x for x in c.get("pages", []) or [] if not isinstance(x, int) or x < 1 or x > n]
            if badp:
                iss.append("checklist_coverage 中 %s 的页码 %s 无效" % (c.get("item"), badp))
        decided = {doc.normalize_key(x.get("section", "")) for x in d.get("section_decisions", [])}
        nod = [k for k in high_secs if k not in decided]
        if nod:
            iss.append("以下重要性高的章节没有给出取舍决定：%s" % "、".join(nod[:20]))
        tot = sum(float(p.get("minutes") or 0) for p in pages)
        if abs(tot - bud["content_minutes"]) > bud["content_minutes"] * tol:
            iss.append("内容页总时长 %.1f 分钟，应接近 %.1f 分钟" % (tot, bud["content_minutes"]))
        if not allow_confirm:
            from .write import CONFIRM_TITLE
            cp = [i for i, p in enumerate(pages, 1) if CONFIRM_TITLE.search(p.get("title_draft", "") + p.get("question", ""))
                  or re.search(r"评审确认|需.{0,4}确认|待确认事项|提请", p.get("title_draft", ""))]
            if cp:
                iss.append("第%s页是“需确认/待评审确认事项”页：本场景不设这类页面（议题由听众决定），"
                           "请删除该页，把原文中待确认或备选的事项写进相关页面的 points" % "、".join(map(str, cp)))
        if dcfg.get("allocation_check") and pages:
            iss += allocation_issues(doc, und, pages, cfg)
        if dcfg.get("coverage_check") and pages:
            iss += coverage_issues(doc, und, pages, d.get("section_decisions", []), cfg)
        return iss[:20]

    if cfg.get("plan", {}).get("mode", "staged") == "staged":
        return run_staged(wd, cfg, log, llm, feedback, brief, scen, doc, und, bud, dname, dcfg, allow_confirm, prev, fb_hist, high_secs)
    prev_txt = "（无）"
    if prev and feedback:
        prev_txt = "\n".join("第%d页：%s ｜依据 %s" % (i, p["title_draft"], ",".join(p["sources"])) for i, p in enumerate(prev["pages"], 1))
    r = llm.call_json("plan", {
        "audience": brief.get("audience", ""), "purpose": brief.get("purpose", ""), "duration": bud["duration_min"],
        "content_minutes": bud["content_minutes"], "min_pages": bud["min_pages"], "max_pages": bud["max_pages"],
        "rec_pages": bud["recommended_pages"], "scenario": scen.get("display_name"), "principle": scen.get("principle", ""),
        "focus_checklist": "\n".join("%s %s" % (c["id"], c["text"]) for c in cl) or "（无）",
        "special_rules": "\n".join("- " + x for x in scen.get("special_rules", [])) or "（无）",
        "must_include": brief.get("must_include") or "（无）", "understanding": compact(und), "outline": outline,
        "user_supplements": user_sup, "previous_plan": prev_txt, "doc_date": und.get("doc_date") or "原文未说明",
        "user_feedback": "\n".join("- " + f["feedback"] for f in fb_hist) if fb_hist else "（无）",
        "density_rules": DENSITY_RULES.get(dname, ""),
    }, target="all", validator=validate)
    d = r["data"] or {}
    if not d.get("pages"):
        raise PipelineError("内容规划失败：模型没有返回页面规划（详见 logs/llm）")
    pages = []
    for i, p in enumerate(d["pages"], 1):
        srcs = []
        for s in p.get("sources") or []:
            k = s if doc.has_block(s) else doc.normalize_key(s)
            if (doc.valid_section(k) or doc.has_block(k)) and k not in srcs:
                srcs.append(k)
        pages.append({"no": i, "page_key": "P%02d" % i, "title_draft": p.get("title_draft", ""), "question": p.get("question", ""),
                      "sources": srcs, "points": p.get("points", []), "minutes": float(p.get("minutes") or bud["minutes_per_page"])})
        log.gate("plan_page_has_sources", "P%02d" % i, bool(srcs))
    title_warn = []
    tn = doc.title_numbers([und.get("doc_title", "")])
    for p in pages:
        t = p["title_draft"]
        if dwidth(t) > 34:
            title_warn.append("%s 标题草稿 %d 字偏长（写作时会精简）" % (p["page_key"], dwidth(t)))
        if t.rstrip("。").endswith(TOPIC_ENDINGS):
            title_warn.append("%s 标题草稿是话题式（写作时会改为结论式）" % p["page_key"])
        add = fix_title_number_sources(doc, p, tn)
        if add:
            log.info("plan_sources_added", "%s 标题中的数字 %s 不在依据章节中，已补充引用 %s" % (p["page_key"], add["numbers"], add["sections"]), **add)
    for w in title_warn:
        log.warn("plan_title_draft", w)
    # 原文没有任何"待确认"类表述，却规划了"需确认事项"页（G48）
    from .write import CONFIRM_SRC, CONFIRM_TITLE
    has_confirm = any(CONFIRM_SRC.search(b["text"]) for b in doc.blocks)
    empty_confirm = [p["no"] for p in pages if CONFIRM_TITLE.search(p["title_draft"]) and not has_confirm]
    for no in empty_confirm:
        log.warn("plan_empty_confirm_page", "第%d页为需确认事项页，但原文没有待确认类表述，建议删除该页" % no)
    # 标题高度相似的两页：多半是原文平行章节各占一页、复述同一组要素（G42）
    from .checks import bigrams
    similar = []
    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            A, B = bigrams(pages[i]["title_draft"]), bigrams(pages[j]["title_draft"])
            sim = len(A & B) / float(min(len(A), len(B)) or 1)
            if sim >= 0.5:
                similar.append({"pages": [pages[i]["no"], pages[j]["no"]], "sim": round(sim, 2)})
                log.warn("plan_similar_titles", "第%d页与第%d页标题相似（%.2f），可能重复" % (pages[i]["no"], pages[j]["no"], sim))
    cov = {c.get("item"): c for c in d.get("checklist_coverage", [])}
    gaps = []
    for c in cl:
        x = cov.get(c["id"], {})
        ok = bool(x.get("pages"))
        log.gate("plan_checklist_covered", c["id"], ok, None if ok else x.get("note", "未覆盖"))
        if not ok:
            gaps.append({"gid": "G%d" % (len(gaps) + 1), "item": c["id"], "text": c["text"], "note": x.get("note", "")})
    # 章节覆盖：重要性高的章节是否被至少一页引用
    used = set()
    for p in pages:
        for s in p["sources"]:
            used.add(s)
    def cited(k):
        return any(u == k or u.startswith(k + ".") or k.startswith(u + ".") for u in used)
    high_uncited = [k for k in high_secs if not cited(k)]
    for k in high_secs:
        log.gate("plan_high_section_cited", k, cited(k))
    plan = {"version": len(fb_hist) + 1, "budget": bud, "storyline": d.get("storyline", ""), "pages": pages,
            "agenda": [x for x in d.get("agenda", []) if isinstance(x, str)][:8],
            "section_decisions": d.get("section_decisions", []), "not_covered": d.get("not_covered", []),
            "checklist": cl, "checklist_coverage": d.get("checklist_coverage", []), "self_check": d.get("self_check", {}),
            "gaps": gaps, "high_sections_uncited": high_uncited, "similar_titles": similar, "empty_confirm_pages": empty_confirm,
            "scattered": [c.get("item") for c in d.get("checklist_coverage", []) if len(c.get("pages") or []) >= 4], "llm_ok": r["ok"], "residual_issues": r["issues"]}
    write_json(wd.stage("plan", "plan.json"), plan)
    if prev and feedback:
        write_json(wd.stage("plan", "plan_v%d.json" % (plan["version"] - 1)), prev)
    write_text(wd.p("checkpoint1.md"), render_checkpoint(plan, und, doc, scen, brief))
    tmpl = {"decision": "approve 或 revise", "feedback": "选择 revise 时写修改意见", "gaps": {g["gid"]: {"choice": "text 或 skip", "text": ""} for g in gaps}}
    write_json(wd.p("answers_template.json"), tmpl)
    return {"plan_version": plan["version"], "content_pages": len(pages), "budget_pages": "%d—%d（建议%d）" % (bud["min_pages"], bud["max_pages"], bud["recommended_pages"]),
            "minutes": round(sum(p["minutes"] for p in pages), 1), "checklist_uncovered": len(gaps),
            "high_sections_uncited": len(high_uncited), "llm_ok": r["ok"], "attempts": r["attempts"]}


def ensure_frame(wd, cfg, log, llm):
    """三步规划的前提：大纲骨架。没有或比通读结果旧时先生成。"""
    from . import frame as framemod
    fp, up = wd.stage("plan", "frame.json"), wd.stage("understand", "understanding.json")
    fr = read_json(fp)
    stale = []
    if fr and not fr.get("fallback"):
        # 旧版生成的骨架：用当前校验规则复查，出现生成时没有的问题就重新生成（实测：父节放外壳、子节与建设内容平行，旧校验漏判）
        try:
            from .docmodel import Doc
            from .core import load_scenario
            und = read_json(up)
            now = framemod.frame_issues(fr, Doc(wd), und, load_scenario(wd.brief()["scenario"]))
            stale = [x for x in now if x not in (fr.get("residual_issues") or [])]
        except Exception as e:      # noqa
            log.warn("plan_frame_recheck_failed", "复查大纲骨架失败：%r" % e)
    if stale:
        log.info("plan_frame_stale", "已有大纲骨架不符合当前规则（%s），重新生成" % stale[0][:80], issues=stale)
    if not fr or stale or (os.path.exists(up) and os.path.getmtime(fp) < os.path.getmtime(up)):
        log.info("plan_frame", "生成大纲骨架与追问清单")
        framemod.run(wd, cfg, log, llm)
        fr = read_json(fp)
    return fr


def run_staged(wd, cfg, log, llm, feedback, brief, scen, doc, und, bud, dname, dcfg, allow_confirm, prev, fb_hist, high_secs):
    from . import outline
    fr = ensure_frame(wd, cfg, log, llm)
    o = outline.run(wd, cfg, log, llm, fr, bud, dname, dcfg, scen, brief, doc, und, feedback, fb_hist, prev, allow_confirm,
                    DENSITY_RULES.get(dname, ""))
    pages = o["pages"]
    for p in pages:
        log.gate("plan_page_has_sources", p["page_key"], bool(p["sources"]))
    tn = doc.title_numbers([und.get("doc_title", "")])
    for p in pages:
        add = fix_title_number_sources(doc, p, tn)
        if add:
            log.info("plan_sources_added", "%s 标题中的数字 %s 不在依据章节中，已补充引用 %s" % (p["page_key"], add["numbers"], add["sections"]), **add)
    from .checks import bigrams
    similar = []
    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            A, B = bigrams(pages[i]["title_draft"]), bigrams(pages[j]["title_draft"])
            sim = len(A & B) / float(min(len(A), len(B)) or 1)
            if sim >= 0.5:
                similar.append({"pages": [pages[i]["no"], pages[j]["no"]], "sim": round(sim, 2)})
                log.warn("plan_similar_titles", "第%d页与第%d页标题相似（%.2f），可能重复" % (pages[i]["no"], pages[j]["no"], sim))
    used = {s for p in pages for s in p["sources"]}
    cited = lambda k: any(u == k or u.startswith(k + ".") or k.startswith(u + ".") for u in used)
    high_uncited = [k for k in high_secs if not cited(k)]
    for k in high_secs:
        log.gate("plan_high_section_cited", k, cited(k))
    warn_cov = coverage_issues(doc, und, pages, o["section_decisions"], cfg) if dcfg.get("coverage_check") else []
    warn_alloc = allocation_issues(doc, und, pages, cfg) if dcfg.get("allocation_check") else []
    for w in warn_cov + warn_alloc:
        log.warn("plan_global_check", w)
    sp = fr.get("special") or {}
    not_cov = [{"item": "%s %s" % (x.get("section"), doc.sections.get(doc.normalize_key(x.get("section", "")), {}).get("title", "")),
                "reason": "信息量低，不单独成页：" + x.get("reason", "")} for x in sp.get("low_info") or []]
    plan = {"version": len(fb_hist) + 1, "mode": "staged", "budget": bud, "storyline": o["storyline"], "pages": pages,
            "chapters": o["chapters"], "dropped_chapters": o["dropped_chapters"], "agenda": o["agenda"],
            "display_chapters": o.get("display_chapters") or [],
            "missing_modules": [m["module"] for part in ("before", "after") for m in (scen.get("shell") or {}).get(part) or []
                                if (m.get("required") or m.get("expected")) and not any(
                                    x.get("module") == m["module"] and x.get("sections") for x in fr.get("shell") or [])],
            "section_decisions": o["section_decisions"], "not_covered": not_cov,
            "checklist": [], "checklist_coverage": [], "question_coverage": o["question_coverage"], "weak_points": o["weak_points"],
            "self_check": {}, "gaps": [], "high_sections_uncited": high_uncited, "similar_titles": similar,
            "cross_duplicates": o["cross_duplicates"], "global_warnings": warn_cov + warn_alloc, "empty_confirm_pages": [],
            "scattered": [], "llm_ok": o["outline_llm_ok"] and all(c["llm_ok"] for c in o["chapters"]),
            "outline_fallback": o["outline_fallback"], "residual_issues": o["outline_issues"]}
    write_json(wd.stage("plan", "plan.json"), plan)
    if prev and feedback:
        write_json(wd.stage("plan", "plan_v%d.json" % (plan["version"] - 1)), prev)
    write_text(wd.p("规划说明.md"), render_checkpoint_staged(plan, und, doc, scen, brief, fr))   # 详细版（逐页规划、问题落页、薄弱点等）
    write_text(wd.p("checkpoint1.md"), render_checkpoint_brief(plan, und, doc, scen, brief))
    write_json(wd.p("answers_template.json"), {"decision": "approve 或 revise", "feedback": "选择 revise 时写修改意见"})
    uncovered = [x["no"] for x in o["question_coverage"] if not x["pages"]]
    return {"plan_version": plan["version"], "mode": "staged", "chapters": len(o["chapters"]), "content_pages": len(pages),
            "budget_pages": "%d—%d（建议%d）" % (bud["min_pages"], bud["max_pages"], bud["recommended_pages"]),
            "minutes": round(sum(p["minutes"] for p in pages), 1), "questions_uncovered": uncovered,
            "chapter_fallbacks": [c["id"] for c in o["chapters"] if c["fallback"]], "outline_fallback": o["outline_fallback"],
            "high_sections_uncited": len(high_uncited), "llm_ok": plan["llm_ok"]}


def render_checkpoint_staged(plan, und, doc, scen, brief, fr):
    b = plan["budget"]
    pages = {p["no"]: p for p in plan["pages"]}
    L = ["# 规划说明（第 %d 版，检查点 1 的详细版）" % plan["version"], "",
         "场景：%s ｜ 听众：%s ｜ 时长 %.0f 分钟 ｜ %d 章 %d 个内容页（允许 %d—%d 页），估算 %.1f 分钟" % (
             scen.get("display_name"), brief.get("audience", ""), b["duration_min"], len(plan["chapters"]), len(plan["pages"]),
             b["min_pages"], b["max_pages"], sum(p["minutes"] for p in plan["pages"])), "",
         "**汇报主线**：%s" % plan["storyline"], ""]
    if und.get("suggested_scenario") and und["suggested_scenario"] != brief["scenario"]:
        L += ["> ⚠ 文档被识别为“%s”，建议使用场景 `%s`：%s。" % (und["doc_type"], und["suggested_scenario"], und.get("suggested_scenario_reason", "")), ""]
    L += ["## 一、大纲（章 → 页）", ""]
    L += ["| 章 | 页数 | 这一章要让听众记住 |", "|---|---|---|"]
    for i, c in enumerate(plan["chapters"], 1):
        L.append("| %d %s | %d | %s |" % (i, c["title"], len(c["pages"]), c.get("message") or ""))
    for i, c in enumerate(plan["chapters"], 1):
        flag = "（兜底分页，建议修改）" if c.get("fallback") else ""
        L += ["", "### %d %s%s" % (i, c["title"], flag), "", "| 页 | 标题草稿 | 回答听众的问题 | 要上屏的要点 | 依据 |", "|---|---|---|---|---|"]
        for no in c["pages"]:
            p = pages[no]
            L.append("| %d | %s | %s | %s | %s |" % (no, p["title_draft"], p["question"], "；".join(p.get("points") or [])[:160],
                                                 "、".join(section_label(doc, s) for s in p["sources"])))
    if plan.get("dropped_chapters"):
        L += ["", "> 未设的章：%s（原文依据不足）" % "、".join(x["name"] for x in plan["dropped_chapters"])]
    L += ["", "## 二、汇报要讲清的问题落在哪", "", "| # | 主/次 | 问题 | 讲在第几页 |", "|---|---|---|---|"]
    for x in plan["question_coverage"]:
        L.append("| %d | %s | %s | %s |" % (x["no"], x["level"], x["q"], "、".join(map(str, x["pages"])) or "**未落页**"))
    L += ["", "## 三、不讲或只放备注的内容", ""]
    seen = set()
    for x in plan["section_decisions"]:
        if x.get("decision") not in ("skip", "notes"):
            continue
        k = doc.normalize_key(x.get("section", ""))
        if k in seen:
            continue
        seen.add(k)
        L.append("- %s %s：%s（%s）" % (k.split("-S")[-1], doc.sections.get(k, {}).get("title", ""), {"skip": "不讲", "notes": "只放备注"}[x["decision"]], x.get("reason", "")))
    for x in plan["not_covered"]:
        L.append("- %s：%s" % (x["item"], x["reason"]))
    if len(L) and L[-1] == "":
        L.append("（无）")
    wk = plan.get("weak_points") or []
    if wk:
        L += ["", "## 四、薄弱点（原文没讲清，不上屏，供汇报人准备答疑）", "", "| # | 专家可能怎么问 | 原文缺什么 |", "|---|---|---|"]
        L += ["| %d | %s | %s |" % (i, w.get("q", ""), w.get("gap", "")) for i, w in enumerate(wk, 1)]
    warn = []
    if plan.get("cross_duplicates"):
        warn.append("以下页面在不同章引用同一小节、标题相近，可能重复：%s" % "；".join("第%d页与第%d页" % tuple(x["pages"]) for x in plan["cross_duplicates"]))
    if plan.get("similar_titles"):
        warn.append("以下页面标题相似：%s" % "；".join("第%d页与第%d页" % tuple(x["pages"]) for x in plan["similar_titles"]))
    if plan["high_sections_uncited"]:
        warn.append("以下重要章节没有页面引用：%s" % "、".join("%s %s" % (k, doc.sections[k]["title"]) for k in plan["high_sections_uncited"]))
    warn += plan.get("global_warnings") or []
    if warn:
        L += ["", "## 提示", ""] + ["> ⚠ " + w for w in warn]
    if und.get("doc_issues"):
        L += ["", "## 通读时发现的文档自身问题（仅供参考，不影响 PPT）", ""]
        L += ["- %s `%s`" % (it.get("text", ""), " ".join(it.get("ids", []))) for it in und["doc_issues"]]
    return "\n".join(L) + "\n"


CN_NUM = "一二三四五六七八九十"


def cn_chapter(i):
    if i <= 10:
        return "第%s章" % CN_NUM[i - 1]
    return "第十%s章" % CN_NUM[i - 11] if i < 20 else "第%d章" % i


INTRO_RE = re.compile(r"^(引言|前言|序言|导言|编制说明|编写说明|编写目的|编制目的|文档说明|说明|概述与说明)$")


def checkpoint_reminders(plan, und, doc, scen, brief, limit=3):
    """检查点 1 的提醒：只列会影响结构判断的问题，每条一句话，最多 limit 条。"""
    out = []
    # 输入预检（0.7.0-dev8）：原文本身的问题放在最前面，解释后面的结构判断
    out += [x for d in (getattr(doc, "docs", {}) or {}).values() for x in d.get("precheck") or []][:2]
    if und.get("suggested_scenario") and und["suggested_scenario"] != brief["scenario"]:
        from .core import load_scenario
        try:
            nm = load_scenario(und["suggested_scenario"]).get("display_name", und["suggested_scenario"])
        except Exception:      # noqa
            nm = und["suggested_scenario"]
        out.append("这份文档更像“%s”，建议改用“%s”场景（当前：%s）" % (und.get("doc_type") or "", nm, scen.get("display_name")))
    # 原文篇幅大的内容没有安排页面（也没有说明不讲）
    from .frame import body_info
    body, share, low = body_info(doc, und)
    used = [s for p in plan["pages"] for s in p["sources"]]
    decided = {doc.normalize_key(x.get("section", "")) for x in plan.get("section_decisions") or [] if x.get("decision") in ("skip", "notes")}
    miss = []
    # 引言、前言、编制说明这类说明性章节不讲是正常的，不提醒（0.7.0-dev9）
    intro_re = re.compile(r"^[\d.\s、第一二三四五六七八九十章节]*(引言|前言|序言|绪论|概述说明|编制说明|编写说明|编写目的|编制目的|文档说明|"
                          r"文档约定|阅读对象|预期读者|适用范围)\s*$")
    for k, s in body.items():
        if s["level"] > 3 or share.get(k, 0) < 0.03 or k in decided:
            continue
        if intro_re.match(doc.sections[k]["title"] or ""):
            continue
        if INTRO_RE.search(re.sub(r"^[\d.\s、第一二三四五六七八九十章节]+", "", s.get("title") or "")):
            continue                     # 引言、前言、编制说明这类不必单独讲，不提醒（0.7.0-dev9）
        if any(u == k or u.startswith(k + ".") or k.startswith(u + ".") for u in used):
            continue
        if any(m != k and k.startswith(m + ".") for m in miss):
            continue
        miss.append(k)
    if miss:
        out.append("原文以下内容没有安排页面：%s" % "、".join(
            "“%s”（约%.1f万字）" % (doc.sections[k]["title"], doc.subtree_chars(k) / 10000.0) if doc.subtree_chars(k) >= 10000 else
            "“%s”" % doc.sections[k]["title"] for k in miss[:3]))
    req = {m["module"] for part in ("before", "after") for m in (scen.get("shell") or {}).get(part) or [] if not m.get("optional")}
    gone = list(dict.fromkeys((plan.get("missing_modules") or []) +
                              [x["name"] for x in plan.get("dropped_chapters") or [] if x["name"] in req]))
    if gone:
        out.append("原文没有找到“%s”的内容，未设章" % "”“".join(gone))
    unq = [x for x in plan.get("question_coverage") or [] if x["level"] == "主" and not x["pages"]]
    if unq:
        out.append("专家可能问的“%s”目前没有页面回答" % unq[0]["q"][:30])
    fb = [c["title"] for c in plan["chapters"] if c.get("fallback")]
    if fb:
        out.append("“%s”的分页由程序兜底生成，建议看一下" % "”“".join(fb[:2]))
    return out[:limit]


def render_checkpoint_brief(plan, und, doc, scen, brief):
    """检查点 1（0.7）：只给大纲表与最多 3 条提醒；详细内容在 规划说明.md。"""
    b = plan["budget"]
    disp = plan.get("display_chapters") or [{"title": c["title"], "ids": [c["id"]], "pages": c["pages"]} for c in plan["chapters"]]
    by_id = {c["id"]: c for c in plan["chapters"]}
    L = ["# 检查点 1：确认汇报大纲", "", "| 章节号 | 主要呈现内容 | 规划页数 |", "|---|---|---|"]
    for i, d in enumerate(disp, 1):
        title = d["title"]
        if len(d["ids"]) > 1:
            title += "（%s）" % "、".join(by_id[x]["title"] for x in d["ids"] if x in by_id)
        L.append("| %s | %s | %d |" % (cn_chapter(i), title, len(d["pages"])))
    mins = sum(p["minutes"] for p in plan["pages"])
    L.append("| 合计 | %d 章 | %d 页（约 %.0f 分钟） |" % (len(disp), len(plan["pages"]), mins))
    rem = checkpoint_reminders(plan, und, doc, scen, brief)
    if rem:
        L += [""] + ["> 提醒：" + r for r in rem]
    L += ["", "逐页规划、问题落页、薄弱点等详细内容见 `规划说明.md`。",
          "", "**请回复**：确认，或用一句话说明怎么调整（如“第二章加到 4 页”“第五章并入第四章”“风险分析单独成章”）。",
          "写入 answers.json：`{\"decision\": \"approve\"}` 或 `{\"decision\": \"revise\", \"feedback\": \"用户原话\"}`"]
    return "\n".join(L) + "\n"


def fix_title_number_sources(doc, page, title_nums):
    """标题草稿中的数字必须能在本页依据原文中找到；找不到时，把原文中含该数字的章节补进依据（G25）。"""
    src = "\n".join(doc.block(b)["text"] for b in doc.resolve(page["sources"]))
    import re
    t = re.sub(r"^\s*\d+(\.\d+)*[\s、.．]+", "", page["title_draft"])     # 开头的章节编号不算
    miss = numbers_not_in(t, [src], title_nums)
    if not miss:
        return None
    added, found = [], []
    # 候选段落按"同一章优先 + 与标题的词语重合度"排序：同一个数字可能指不同对象（"88项文件"与"88项指标"）
    from .check import _terms
    tops = {k.split(".")[0] for k in page["sources"] if "-S" in k}
    q = _terms(t)
    for n in miss:
        cands = []
        for b in doc.blocks:
            if b["type"] == "heading" or n not in (extract_numbers(b["text"]) | {x.rstrip("%") for x in extract_numbers(b["text"])}):
                continue
            overlap = len(q & _terms(b["text"])) - (1 if n in q else 0)
            same = "%s-S%s" % (b["doc"], b["section"].split(".")[0]) in tops
            cands.append((overlap, same, b))
        cands = [c for c in cands if c[0] > 0]
        if not cands:
            continue
        cands.sort(key=lambda c: (-c[0], not c[1]))
        b = cands[0][2]
        k = "%s-S%s" % (b["doc"], b["section"])
        if k not in page["sources"] and k not in added:
            added.append(k)
        found.append(n)
    page["sources"].extend(added)
    if added:
        page["number_sources"] = list(dict.fromkeys((page.get("number_sources") or []) + added))   # 只为核对数字补引的章节
    return {"numbers": miss, "found": found, "sections": added} if added else None


def section_label(doc, k):
    """依据章节的显示名：原标题已带编号（"2.1技术路线""一、论文概况"）时不再重复章节键里的编号。"""
    if k not in doc.sections:
        return k
    t = doc.sections[k]["title"]
    import re
    if re.match(r"^\s*([0-9]+(\.[0-9]+)*|[一二三四五六七八九十]+[、.．])", t):
        return truncate(t.strip(), 14)
    return "%s %s" % (k.split("-S")[-1], truncate(t, 12))


def render_checkpoint(plan, und, doc, scen, brief):
    b = plan["budget"]
    L = ["# 检查点 1：确认内容规划（第 %d 版）" % plan["version"], "",
         "场景：%s ｜ 听众：%s ｜ 时长 %.0f 分钟 ｜ 内容页 %d 页（允许 %d—%d 页），估算 %.1f 分钟" % (
             scen.get("display_name"), brief.get("audience", ""), b["duration_min"], len(plan["pages"]), b["min_pages"], b["max_pages"],
             sum(p["minutes"] for p in plan["pages"])), "",
         "**汇报主线**：%s" % plan["storyline"], ""]
    if und.get("suggested_scenario") and und["suggested_scenario"] != brief["scenario"]:
        L += ["> ⚠ 文档被识别为“%s”，建议使用场景 `%s`：%s。如需更换，请重新 init 并指定 --scenario。" % (
            und["doc_type"], und["suggested_scenario"], und.get("suggested_scenario_reason", "")), ""]
    L += ["## 页面规划", "", "| 页 | 标题草稿 | 回答听众的问题 | 依据章节 | 用时 |", "|---|---|---|---|---|"]
    for p in plan["pages"]:
        L.append("| %d | %s | %s | %s | %.1f |" % (p["no"], p["title_draft"], p["question"],
                                                 "、".join(section_label(doc, s) for s in p["sources"]), p["minutes"]))
    L += ["", "## 听众关注点覆盖", "", "| 关注点 | 覆盖页 | 说明 |", "|---|---|---|"]
    cov = {c.get("item"): c for c in plan["checklist_coverage"]}
    for c in plan["checklist"]:
        x = cov.get(c["id"], {})
        L.append("| %s %s | %s | %s |" % (c["id"], c["text"], "、".join(str(i) for i in x.get("pages", []) or []) or "**未覆盖**", x.get("note", "")))
    memo = [it for it in (und.get("key_items", {}) or {}).get("decisions", []) or [] if isinstance(it, dict)]
    if memo:
        L += ["", "## 原文中的待确认事项（不上屏，供汇报人准备答疑）", ""] + [
            "- %s `%s`" % (it.get("text", ""), " ".join(it.get("ids", []) or [])) for it in memo]
    if plan.get("empty_confirm_pages"):
        L += ["", "> ⚠ 第%s页是“需确认事项”页，但原文没有任何待确认、待明确类表述，建议删除该页（revise 中写“删除第N页”）" % "、".join(map(str, plan["empty_confirm_pages"]))]
    if plan.get("similar_titles"):
        L += ["", "> ⚠ 以下页面标题相似，可能在复述同一组内容（可考虑合并或明确分工）：%s" % "；".join(
            "第%d页与第%d页" % tuple(x["pages"]) for x in plan["similar_titles"])]
    if plan.get("scattered"):
        L += ["", "> ⚠ 以下关注点分散在 4 页以上，没有一页集中讲：%s" % "、".join(plan["scattered"])]
    if plan["high_sections_uncited"]:
        L += ["", "> ⚠ 以下被判定为“重要性高”的章节没有任何一页引用：%s" % "、".join(
            "%s %s" % (k, doc.sections[k]["title"]) for k in plan["high_sections_uncited"])]
    skipped = [x for x in plan["section_decisions"] if x.get("decision") in ("skip", "notes")]
    if skipped or plan["not_covered"]:
        L += ["", "## 不讲或只放备注的内容", ""]
        seen = set()
        for x in skipped:
            k = doc.normalize_key(x.get("section", ""))
            if k in seen:
                continue
            seen.add(k)
            L.append("- %s %s：%s（%s）" % (k, doc.sections[k]["title"] if k in doc.sections else "", {"skip": "不讲", "notes": "只放备注"}[x["decision"]], x.get("reason", "")))
        import re
        for x in plan["not_covered"]:
            item = x.get("item", "")
            m = re.search(r"D\d+-S[0-9.]+", item)
            key = doc.normalize_key(m.group(0)) if m else item
            if key in seen:      # 与上面的章节取舍重复（同一节在 not_covered 中再写一遍）
                continue
            seen.add(key)
            L.append("- %s：%s" % (item, x.get("reason", "")))
    if plan["gaps"]:
        L += ["", "## 需要你补充的内容（缺口）", ""]
        for g in plan["gaps"]:
            L.append("- **%s**（关注点 %s：%s）%s —— 可在 answers.json 的 gaps 中提供内容（choice=text），或选择 skip" % (g["gid"], g["item"], g["text"], g["note"]))
    if und.get("doc_issues"):
        L += ["", "## 通读时发现的文档自身问题（仅供参考，不影响 PPT）", ""]
        L += ["- %s `%s`" % (it.get("text", ""), " ".join(it.get("ids", []))) for it in und["doc_issues"]]
    L += ["", "---", "", "**请回复**：确认（approve），或提出修改意见（revise + feedback），例如“第5页和第6页合并”“增加一页讲数据需求”“第3页不需要”。",
          "写入 answers.json：`{\"decision\": \"approve\"}` 或 `{\"decision\": \"revise\", \"feedback\": \"……\"}`"]
    return "\n".join(L) + "\n"


def archive_answers(wd):
    """重新规划时，旧的确认记录与检查点 2 的决定都已过期，移入 answers_history/。"""
    os.makedirs(wd.p("answers_history"), exist_ok=True)
    ts = now_iso()[:19].replace(":", "")
    for f in ("answers.json", "review_decisions.json"):
        if os.path.exists(wd.p(f)):
            os.replace(wd.p(f), wd.p("answers_history", "%s_%s" % (ts, f)))


def resolve_checkpoint(wd, cfg, log, llm):
    """在写作前调用：已确认返回 True；需要重新规划时自动重规划并抛出 NeedUser。"""
    ans = read_json(wd.p("answers.json"))
    files = ["checkpoint1.md", "answers_template.json"]
    if ans is None:
        raise NeedUser("检查点 1：请把 checkpoint1.md 的页面规划交给用户确认，回答写入 answers.json 后重新运行", files)
    dec = ans.get("decision") or "approve"
    gaps_ans = ans.get("gaps") or {}
    plan = read_json(wd.stage("plan", "plan.json"))
    gids = {g["gid"]: g for g in plan.get("gaps", [])}
    bad = [k for k, v in gaps_ans.items() if k not in gids or (v.get("choice") == "text" and not (v.get("text") or "").strip())
           or v.get("choice") not in ("text", "skip", "", None)]
    if dec not in ("approve", "revise") or bad:
        raise PipelineError("answers.json 格式有误：decision 须为 approve 或 revise；gaps 中 %s 无效" % bad, EXIT_CONFIG)
    supplements = [(k, v["text"].strip()) for k, v in gaps_ans.items() if v.get("choice") == "text"]
    # 0.6：用户补充原文没有的内容（不依赖“缺口”编号）：{"supplements": [{"text": "……", "for": "第3页"}]}
    free = [x for x in ans.get("supplements") or [] if isinstance(x, dict) and (x.get("text") or "").strip()]
    if dec == "approve" and not supplements and not free:
        write_json(wd.stage("plan", "approved.json"), {"at": now_iso(), "plan_version": plan["version"], "answers": ans})
        log.info("plan_approved", "用户确认第 %d 版页面规划" % plan["version"])
        return True
    # 需要重新规划：登记用户补充内容，归档本次回答
    ub = read_json(wd.p("user_blocks.json"), [])
    fb = []
    for k, t in supplements:
        bid = "USER-%s" % gids[k]["item"]      # 按关注点编号命名（如 USER-C4），重新规划后编号不变
        ub = [b for b in ub if b["id"] != bid] + [{"id": bid, "doc": "USER", "section": "USER", "type": "para", "text": t,
                                                     "section_path": ["用户补充：" + gids[k]["text"]]}]
        fb.append("用户为关注点“%s”补充了内容（段落ID %s），请在规划中使用：%s" % (gids[k]["text"], bid, truncate(t, 200)))
    for k, x in enumerate(free, 1):
        bid = "USER-S%d" % k
        ub = [b for b in ub if b["id"] != bid] + [{"id": bid, "doc": "USER", "section": "USER", "type": "para", "text": x["text"].strip(),
                                                     "section_path": ["用户补充" + ("（%s）" % x["for"] if x.get("for") else "")]}]
        fb.append("用户补充了原文没有的内容（段落ID %s%s），请放进相关页面，sources 写 %s：%s"
                  % (bid, "，用于" + x["for"] if x.get("for") else "", bid, truncate(x["text"].strip(), 200)))
    write_json(wd.p("user_blocks.json"), ub)
    if dec == "revise" and ans.get("feedback"):
        fb.append(ans["feedback"])
    archive_answers(wd)
    log.info("plan_replan", "按用户意见重新规划", feedback=fb)
    return "\n".join(fb)
