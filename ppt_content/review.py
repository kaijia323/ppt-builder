"""第④步：编辑审查。以评审专家兼汇报教练的视角审查整套大纲：
完整性、结构、标题、要点、概念与语气。R1 后对 high/medium 问题的页带意见重写；终审仍有 high 问题时再修订一轮
（review.max_revise_rounds，默认 2），最后一轮审查只出报告。
审查会收到用户在检查点 1 的意见，不得推翻（0.3.0）。
整套层面的问题（如漏掉某章、页面重复）无法靠单页重写解决，会列入检查点 2 的最前面。
"""
from .checks import count_conflicts, dup_pairs, norm_loose, truncate
from .core import read_json, write_json, write_text
from .plan import checklist
from .understand import compact
from .write import load_ctx, render_pages, write_one

TYPES = ["completeness", "structure", "duplication", "title", "bullet", "notes_misplaced", "concept", "tone", "accuracy", "other"]
TYPE_CN = {"completeness": "完整性", "structure": "结构", "duplication": "重复", "title": "标题", "bullet": "要点",
           "notes_misplaced": "关键信息放在备注", "concept": "概念错位", "tone": "语气", "accuracy": "准确性", "other": "其他"}


def user_feedback(wd):
    hist = read_json(wd.stage("plan", "feedback_history.json"), []) or []
    return "\n".join("- " + h["feedback"] for h in hist) if hist else "（无）"


def fix_ids(doc, ids):
    """审查给出的依据：段落ID保留；章节键换成该节第一个正文段落；不存在的直接去掉（不为此重试）。"""
    out = []
    for i in ids or []:
        if doc.has_block(i):
            out.append(i)
            continue
        k = doc.normalize_key(i)
        if doc.valid_section(k):
            r = doc.resolve([k])
            if r:
                out.append(r[0])
    return list(dict.fromkeys(out))


def review_once(wd, cfg, log, llm, pages, plan, und, scen, brief, doc, tag):
    n = len(pages)
    deck = []
    for p in pages:
        deck.append("第%d页 标题：%s\n  依据章节：%s\n  要点：\n%s\n  备注：%s" % (
            p["no"], p["title"], "、".join("%s（%s）" % (s, truncate(doc.path_str(s), 40)) for s in p["sources"]),
            "\n".join("    - %s %s" % (b["text"], b["ids"]) for b in p["bullets"]), truncate(p["notes"], 220)))
    # 0.6 三步规划：听众关注点用文档自己提出的问题（规划已逐题落页），不再用场景的固定清单——
    # 实测固定清单让审查提出原文并不存在的“资源与成本”缺口
    qc = plan.get("question_coverage") or []
    if qc:
        cl = [{"id": "Q%d" % x["no"], "text": "%s（规划中讲在第 %s 页）" % (x["q"], "、".join(map(str, x.get("pages") or [])) or "—")} for x in qc]
    else:
        cl = checklist(scen)

    def validate(d):
        iss = []
        for k, x in enumerate(d.get("issues", [])):
            pg = x.get("page")
            if not isinstance(pg, int) or pg < 0 or pg > n:
                iss.append("issues[%d] 的 page=%s 无效（0 表示整套，1—%d 为页码）" % (k, pg, n))
        return iss[:15]
    r = llm.call_json("review", {
        "audience": brief.get("audience", ""), "purpose": brief.get("purpose", ""), "duration": plan["budget"]["duration_min"],
        "scenario": scen.get("display_name"), "principle": scen.get("principle", ""),
        "focus_checklist": "\n".join("%s %s" % (c["id"], c["text"]) for c in cl) or "（无）",
        "understanding": compact(und), "storyline": plan["storyline"], "deck": "\n\n".join(deck), "n_pages": n,
        "user_feedback": user_feedback(wd),
        "dup_hints": "\n".join("- 第%d页[%s]“%s” ↔ 第%d页[%s]“%s”" % (x["pages"][0], x["a"], x["text_a"], x["pages"][1], x["b"], x["text_b"])
                               for x in dup_pairs(pages, float(cfg.get("review", {}).get("dup_threshold", 0.6))))
                     + "".join("\n- 数量不一致：“%s” %s" % (c["noun"], "；".join("第%s页为%s" % ("、".join(map(str, v)), k) for k, v in c["counts"].items()))
                               for c in count_conflicts(pages)) or "（无）",
    }, target=tag, validator=validate)
    d = r["data"] or {}
    issues = []
    for k, x in enumerate(d.get("issues", [])):
        pg = x.get("page") if isinstance(x.get("page"), int) and 0 <= x.get("page") <= n else 0
        issues.append({"rid": "%s-%02d" % (tag, k + 1), "page": pg, "type": x.get("type") if x.get("type") in TYPES else "other",
                       "severity": x.get("severity") if x.get("severity") in ("high", "medium", "low") else "medium",
                       "problem": x.get("problem", ""), "suggestion": x.get("suggestion", ""),
                       "ids": fix_ids(doc, x.get("ids"))})
    # accuracy 类意见须有原文佐证（G36）：给出的 quote 在原文中找不到时，降为 low，不据此修订
    for x, raw in zip(issues, d.get("issues", [])):
        if x["type"] != "accuracy":
            continue
        q = norm_loose(raw.get("quote", ""))
        pool = [doc.block(i)["text"] for i in x["ids"]] or [b["text"] for b in doc.blocks]
        ok = len(q) >= 4 and (any(q in norm_loose(t) for t in pool) or any(q in norm_loose(b["text"]) for b in doc.blocks))
        if not ok:
            # 不自动据此修订，但保留严重程度交给用户判断：实测中这类意见有对有错（错例：把正确的"矢量图层类"改错；对例：指出"各1套"有歧义）
            log.warn("review_accuracy_unverified", "%s 第%d页的准确性意见未附可核对的原文，不自动修订，交用户判断：%s" % (tag, x["page"], truncate(x["problem"], 60)))
            x["unverified"] = True
            x["problem"] += "（审查未附可核对的原文，请人工判断）"
        else:
            x["quote"] = raw.get("quote", "")
    for x in issues:
        log.gate("review_issue", "%s/P%02d" % (tag, x["page"]), False, "%s/%s：%s" % (x["severity"], x["type"], truncate(x["problem"], 60)))
    return {"overall": d.get("overall", {}), "issues": issues, "llm_ok": r["ok"], "residual": r["issues"]}


def dup_tasks(rv, pages, cfg):
    """整套层面的重复（page=0）和脚本检出的跨页重复，拆成具体页面的修订任务（G43）：
    保留一处（审查建议中"保留第N页"所指的页，否则编号小的页），其余页删除或改写重复内容。"""
    import re
    tasks, groups = [], []
    n = len(pages)
    for x in rv["issues"]:
        if x["page"] != 0 or x["type"] != "duplication" or x.get("unverified"):
            continue
        nums = sorted({int(v) for v in re.findall(r"第\s*(\d+)\s*[、，,和与及至—-]?", x["problem"] + x["suggestion"]) if 0 < int(v) <= n})
        if len(nums) < 2:
            continue
        m = re.search(r"保留(?:在)?第\s*(\d+)\s*页", x["suggestion"])
        keep = int(m.group(1)) if m and int(m.group(1)) in nums else nums[0]
        groups.append(set(nums))
        for no in nums:
            if no != keep:
                tasks.append(dict(x, page=no, rid=x["rid"] + "-p%d" % no, severity="medium",
                                  problem="与第%d页重复：%s" % (keep, x["problem"]),
                                  suggestion="本页删除或改写与第%d页重复的内容（第%d页保留）。%s" % (keep, keep, x["suggestion"])))
    for d in dup_pairs(pages, float(cfg.get("review", {}).get("dup_threshold", 0.6))):
        later = d["pages"][1]
        if any(set(d["pages"]) <= g for g in groups):     # 审查已给出保留哪一页，以审查为准
            continue
        tasks.append({"rid": "DUP-%s" % d["b"], "page": later, "type": "duplication", "severity": "medium",
                      "problem": "要点[%s]“%s”与第%d页要点“%s”重复" % (d["b"], d["text_b"], d["pages"][0], d["text_a"]),
                      "suggestion": "删除该要点，或改写为本页特有的内容（第%d页保留）" % d["pages"][0], "ids": []})
    return tasks


def revise_pages(wd, cfg, log, llm, pages, rv, sev, plan, und, scen, brief, doc, tag):
    by_page = {}
    extra = dup_tasks(rv, pages, cfg) if "medium" in sev else []
    if extra:
        log.info("dup_tasks", "跨页重复拆解为 %d 个页面修订任务" % len(extra), tasks=[(t["page"], t["problem"][:60]) for t in extra])
    for x in list(rv["issues"]) + extra:
        if x["page"] > 0 and x["severity"] in sev and not x.get("unverified"):
            by_page.setdefault(x["page"], []).append(x)
    if not by_page:
        return []
    pmap = {p["no"]: p for p in plan["pages"]}
    ufb = user_feedback(wd)
    import os
    approved = os.path.exists(wd.stage("plan", "approved.json"))

    def redo(no):
        fb = "\n".join("- [%s/%s] %s 建议：%s%s" % (TYPE_CN.get(x["type"]), x["severity"], x["problem"], x["suggestion"],
                                                   ("（依据 %s）" % ",".join(x["ids"])) if x["ids"] else "") for x in by_page[no])
        if ufb != "（无）":
            fb += "\n用户在检查点 1 的意见（修改时不得违背）：\n" + ufb
        if approved:
            # G44：规划标题方向已由用户确认；审查意见与之冲突时保留方向，冲突留给检查点 2
            fb += "\n本页规划标题（用户已在检查点 1 确认）：%s。可以改进措辞、补全事实，但不要改变它的方向；审查意见若要求改变方向，保留规划方向。" % pmap[no]["title_draft"]
        return write_one(wd, cfg, log, llm, pmap[no], plan, und, scen, brief, doc, review_feedback=fb, attempt_tag=tag)
    res = llm.map(redo, sorted(by_page), "revise")
    newp = {}
    for x in res:
        if isinstance(x, Exception):
            raise x
        newp[x["no"]] = x
    revised = []
    for i, p in enumerate(pages):
        if p["no"] in newp:
            old = p
            pages[i] = newp[p["no"]]
            pages[i]["previous"] = {"title": old["title"], "bullets": [b["text"] for b in old["bullets"]]}
            revised.append(p["no"])
    log.info("pages_revised", "按审查意见重写 %d 页：%s" % (len(revised), revised))
    return revised


def run(wd, cfg, log, llm):
    plan, und, scen, brief, doc = load_ctx(wd)
    rc = cfg.get("review", {})
    pages = read_json(wd.stage("write", "pages.json"))
    r1 = review_once(wd, cfg, log, llm, pages, plan, und, scen, brief, doc, "R1")
    write_json(wd.stage("review", "review_round1.json"), r1)
    revised, rounds = [], []
    if rc.get("revise", True):
        revised += revise_pages(wd, cfg, log, llm, pages, r1, set(rc.get("revise_severities", ["high", "medium"])),
                                plan, und, scen, brief, doc, "-rev")
    final = None
    if rc.get("final_pass", True):
        final = review_once(wd, cfg, log, llm, pages, plan, und, scen, brief, doc, "R2")
        k = 2
        # 终审仍有页面级 high 问题：再修订一轮并复审（G8）
        while rc.get("revise", True) and k < 1 + int(rc.get("max_revise_rounds", 2)) and \
                any(x["page"] > 0 and x["severity"] == "high" and not x.get("unverified") for x in final["issues"]):
            rounds.append(final)
            write_json(wd.stage("review", "review_round%d.json" % k), final)
            revised += revise_pages(wd, cfg, log, llm, pages, final, {"high"}, plan, und, scen, brief, doc, "-rev%d" % k)
            k += 1
            final = review_once(wd, cfg, log, llm, pages, plan, und, scen, brief, doc, "R%d" % k)
        write_json(wd.stage("review", "review_final.json"), final)
    write_json(wd.stage("review", "pages.json"), pages)
    write_text(wd.stage("review", "reviewed.md"), render_pages(pages, "审查修订后"))
    last = final or r1

    def cnt(rv):
        c = {}
        for x in rv["issues"]:
            c[x["severity"]] = c.get(x["severity"], 0) + 1
        return c
    return {"round1_score": r1["overall"].get("score"), "round1_issues": cnt(r1), "pages_revised": sorted(set(revised)),
            "review_rounds": 1 + (1 if final else 0) + len(rounds),
            "final_score": last["overall"].get("score"), "final_issues": cnt(last),
            "deck_level_issues": sum(1 for x in last["issues"] if x["page"] == 0)}
