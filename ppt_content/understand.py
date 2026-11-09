"""第①步：通读理解。像专家一样先读完全文，产出"文档理解卡"：
文档类型与用途、论证主线、逐章理解（作用/摘要/要点/对本次听众的重要性）、关键事项清单、文档自身问题。

全文不超过 understand.max_single_chars 时一次读完；否则按一级章节分组理解，再汇总。
"""
import os

from .checks import numbers_not_in, truncate
from .core import PipelineError, SKILL_ROOT, load_scenario, write_json, write_text
from .docmodel import Doc

IMP_ORDER = {"high": 0, "medium": 1, "low": 2}
ITEM_KEYS = ["decisions", "metrics", "scope_in", "scope_out", "deliverables", "constraints", "time_limits", "exceptions"]


def scenario_list():
    d = os.path.join(SKILL_ROOT, "scenarios")
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".yaml"):
            sc = load_scenario(fn[:-5])
            out.append("%s：%s（%s）" % (fn[:-5], sc.get("display_name"), sc.get("description", "")))
    return "\n".join(out)


TOP_FIELDS = ["doc_issues", "doc_title", "doc_date", "doc_type", "doc_type_reason", "written_for", "effect", "storyline",
              "suggested_scenario", "suggested_scenario_reason", "chapters"]


def hoist(d):
    """模型漏写右括号时，后面的顶层字段会被嵌套进 key_items（或最后一章）里：移回顶层。"""
    if not isinstance(d, dict):
        return d
    for holder in ("key_items",):
        h = d.get(holder)
        if isinstance(h, dict):
            for k in TOP_FIELDS:
                if k in h and k not in d:
                    d[k] = h.pop(k)
    chs = d.get("chapters") or []
    if chs and isinstance(chs[-1], dict):
        for k in TOP_FIELDS + ["key_items"]:
            if k in chs[-1] and k not in d:
                d[k] = chs[-1].pop(k)
    return d


def number_warnings(d, doc):
    """数字核对：对照该章全部原文 + 文档标题中的数字。只作为警告记录，不触发重试（重试要重送全文，代价高）。"""
    warns = []
    tnums = doc.title_numbers([d.get("doc_title", "")])
    for c in d.get("chapters", []):
        key = doc.normalize_key(c.get("section", ""))
        if not doc.valid_section(key):
            continue
        ev = [doc.subtree_text(key)]
        for j, p in enumerate(c.get("key_points", [])):
            miss = numbers_not_in(p.get("text", ""), ev, tnums)
            if miss:
                warns.append("%s 要点%d 的数字 %s 未在该章原文中找到：%s" % (key, j + 1, miss, truncate(p.get("text", ""), 40)))
    return warns


REPAIRS = []   # 本次运行中就地修复的段落ID（写入日志）


def repair_ids(doc, ids):
    """不存在的段落ID就地修复，不为此重送全文（0.3.0）：
    章节键 → 该节第一个正文段落；同一节中不存在的段落号 → 该节最接近的段落；无法对应 → 去掉。"""
    import re
    out, fixed = [], []
    for i in ids or []:
        if doc.has_block(i):
            out.append(i)
            continue
        m = re.match(r"^(D\d+-S[0-9.]+)(?:-P(\d+))?$", str(i).strip())
        k = doc.normalize_key(m.group(1)) if m else None
        cand = doc.resolve([k]) if k and doc.valid_section(k) else []
        own = [b for b in cand if doc.by_id.get(b) and "%s-S%s" % (doc.by_id[b]["doc"], doc.by_id[b]["section"]) == k] or cand
        if own:
            n = int(m.group(2)) if m.group(2) else 1
            pick = min(own, key=lambda b: abs(int(re.sub(r"\D", "", b.rsplit("-P", 1)[-1]) or 0) - n))
            out.append(pick)
            fixed.append("%s→%s" % (i, pick))
        else:
            fixed.append("%s→（去掉）" % i)
    return list(dict.fromkeys(out)), fixed


def make_validator(doc, required_keys, need_doc_fields=True, repairs=None):
    """只检查结构性问题（缺章节、章节键不存在）：这类问题会让后续步骤失去依据，值得重试。
    段落ID错误就地修复并记录（repairs），不重试。"""
    def check_ids(obj, where, issues):
        if not isinstance(obj, dict):
            return
        new, fixed = repair_ids(doc, obj.get("ids"))
        if fixed:
            obj["ids"] = new
            if repairs is not None:
                repairs.append({"where": where, "fixed": fixed})

    def validate(d):
        hoist(d)
        issues = []
        seen = set()
        for k, c in enumerate(d.get("chapters", [])):
            key = doc.normalize_key(c.get("section", ""))
            if not doc.valid_section(key):
                issues.append("chapters[%d] 的章节键 %s 不存在，请使用目录方括号中的章节键" % (k, c.get("section")))
                continue
            seen.add(key)
            for j, p in enumerate(c.get("key_points", [])):
                check_ids(p, "chapters[%d].key_points[%d]" % (k, j), issues)
        missing = [x for x in required_keys if x not in seen]
        if missing:
            issues.append("以下章节没有输出逐章理解（每个一、二级章节都必须有）：%s" % "、".join(missing[:30]))
        if need_doc_fields:
            for key in ITEM_KEYS:
                for j, it in enumerate((d.get("key_items") or {}).get(key, []) or []):
                    check_ids(it, "key_items.%s[%d]" % (key, j), issues)
            for j, it in enumerate(d.get("doc_issues", []) or []):
                check_ids(it, "doc_issues[%d]" % j, issues)
        return issues[:25]
    return validate


def run(wd, cfg, log, llm):
    brief = wd.brief()
    doc = Doc(wd)
    uc = cfg.get("understand", {})
    REPAIRS.clear()
    required = doc.key_sections(2)
    total = doc.total_chars
    common = {"audience": brief.get("audience", ""), "purpose": brief.get("purpose", ""),
              "duration": brief.get("duration_min", ""), "scenario_list": scenario_list(),
              "chosen_scenario": "%s（%s）" % (brief["scenario"], brief.get("scenario_name", ""))}
    log.info("understand_input", "全文 %d 字，需逐章理解的章节 %d 个" % (total, len(required)),
             total_chars=total, required_sections=required)
    if total <= int(uc.get("max_single_chars", 300000)):
        mode = "single"
        r = llm.call_json("understand", dict(common, outline=doc.outline_text(), full_text=doc.full_text(),
                                             required_sections="、".join(required)),
                          target="all", validator=make_validator(doc, required, repairs=REPAIRS))
        data = hoist(r["data"] or {})
        ok, issues = r["ok"], r["issues"]
    else:
        mode = "chapters"
        data, ok, issues = understand_by_chapters(doc, cfg, log, llm, common, required)
    if not data.get("chapters"):
        raise PipelineError("通读理解失败：模型未返回有效的逐章理解（详见 logs/llm）")
    if REPAIRS:
        log.warn("understand_ids_repaired", "就地修复了 %d 处无效段落ID（未重试）" % sum(len(x["fixed"]) for x in REPAIRS), repairs=REPAIRS[:50])
    nwarn = number_warnings(data, doc)
    for w in nwarn:
        log.warn("understand_number_check", w)

    # 规范化 + 过滤无效内容（保留有效部分，记录被丢弃的）
    chapters, dropped = [], 0
    for c in data.get("chapters", []):
        key = doc.normalize_key(c.get("section", ""))
        if not doc.valid_section(key):
            dropped += 1
            continue
        kps = [p for p in c.get("key_points", []) if p.get("ids") and all(doc.has_block(i) for i in p["ids"])]
        chapters.append({"section": key, "title": doc.sections[key]["title"], "level": doc.sections[key]["level"],
                         "chars": doc.subtree_chars(key), "role": c.get("role", ""), "summary": c.get("summary", ""),
                         "key_points": kps, "importance": c.get("importance", "medium") if c.get("importance") in IMP_ORDER else "medium",
                         "importance_reason": c.get("importance_reason", "")})
    covered = {c["section"] for c in chapters}
    for k in required:
        log.gate("understand_section_covered", k, k in covered)
    items = {}
    for key in ITEM_KEYS:
        items[key] = [it for it in (data.get("key_items") or {}).get(key, []) or []
                      if all(doc.has_block(i) for i in it.get("ids", []) or [])]
    doc_issues = [it for it in data.get("doc_issues", []) or [] if all(doc.has_block(i) for i in it.get("ids", []) or [])]
    for it in sum_mismatches(doc):
        log.warn("doc_sum_mismatch", it["text"], ids=it["ids"])
        doc_issues.append(it)
    und = {"mode": mode, "doc_title": data.get("doc_title", ""), "doc_date": data.get("doc_date", ""), "doc_type": data.get("doc_type", ""), "doc_type_reason": data.get("doc_type_reason", ""),
           "written_for": data.get("written_for", ""), "effect": data.get("effect", ""),
           "storyline": data.get("storyline", []), "suggested_scenario": data.get("suggested_scenario", ""),
           "suggested_scenario_reason": data.get("suggested_scenario_reason", ""),
           "chapters": chapters, "key_items": items,
           "doc_issues": doc_issues,
           "llm_ok": ok, "residual_issues": issues, "number_warnings": nwarn}
    write_json(wd.stage("understand", "understanding.json"), und)
    write_text(wd.stage("understand", "understanding.md"), render_md(und))
    if brief["scenario"] == "auto":
        import os as _os
        sid = und["suggested_scenario"] if und["suggested_scenario"] and _os.path.exists(
            _os.path.join(SKILL_ROOT, "scenarios", und["suggested_scenario"] + ".yaml")) else "generic"
        brief["scenario"] = sid
        brief["scenario_name"] = load_scenario(sid).get("display_name", sid)
        brief["scenario_auto"] = True
        write_json(wd.p("brief.json"), brief)
        log.info("scenario_auto", "自动选定场景：%s（%s）%s" % (sid, brief["scenario_name"], und.get("suggested_scenario_reason", "")))
    elif und["suggested_scenario"] and und["suggested_scenario"] != brief["scenario"]:
        log.warn("scenario_mismatch", "当前场景为 %s，理解阶段建议使用 %s：%s" % (
            brief["scenario"], und["suggested_scenario"], und["suggested_scenario_reason"]))
    imp = {}
    for c in chapters:
        imp[c["importance"]] = imp.get(c["importance"], 0) + 1
    return {"mode": mode, "total_chars": total, "doc_type": und["doc_type"], "chapters": len(chapters),
            "required_sections": len(required), "required_covered": sum(1 for k in required if k in covered),
            "importance": imp, "key_items": {k: len(v) for k, v in items.items()}, "doc_issues": len(und["doc_issues"]),
            "dropped_invalid_chapters": dropped, "suggested_scenario": und["suggested_scenario"], "llm_ok": ok,
            "number_warnings": len(nwarn)}


def understand_by_chapters(doc, cfg, log, llm, common, required):
    """超长文档：按一级章节分组理解，再汇总文档层面的结论。"""
    maxc = int(cfg.get("understand", {}).get("chapter_max_chars", 120000))
    tops = [k for k, s in doc.sections.items() if s["level"] <= 1 and doc.subtree_chars(k) > 0]
    groups, cur, n = [], [], 0
    for k in tops:
        c = doc.subtree_chars(k)
        if cur and n + c > maxc:
            groups.append(cur)
            cur, n = [], 0
        cur.append(k)
        n += c
    if cur:
        groups.append(cur)
    log.info("understand_chapters", "超长文档：分 %d 组理解" % len(groups), groups=groups)

    def do(g):
        req = [k for k in required if any(k == t or k.startswith(t + ".") for t in g)]
        r = llm.call_json("understand_chapter", dict(common, outline=doc.outline_text(), full_text=doc.full_text(g),
                                                     required_sections="、".join(req)),
                          target="+".join(g)[:60], validator=make_validator(doc, req, need_doc_fields=False, repairs=REPAIRS))
        return r
    results = llm.map(do, groups, "understand_chapter")
    chapters, ok, issues = [], True, []
    for r in results:
        if isinstance(r, Exception):
            raise r
        chapters += (r["data"] or {}).get("chapters", [])
        ok = ok and r["ok"]
        issues += r["issues"]
    digest = "\n".join("[%s]（%s）%s ｜要点：%s" % (c.get("section"), c.get("importance"), c.get("summary", ""),
                                               "；".join("%s%s" % (p.get("text", ""), p.get("ids", [])) for p in c.get("key_points", [])))
                       for c in chapters)
    r = llm.call_json("understand_merge", dict(common, outline=doc.outline_text(), chapter_digest=digest),
                      target="merge", validator=make_validator(doc, [], need_doc_fields=True, repairs=REPAIRS))
    data = dict(hoist(r["data"] or {}))
    data["chapters"] = chapters
    return data, ok and r["ok"], issues + r["issues"]


def render_md(u):
    IMPN = {"high": "高", "medium": "中", "low": "低"}
    L = ["# 文档理解卡", "", "- 文档类型：%s（%s）" % (u["doc_type"], u["doc_type_reason"]),
         "- 写给谁：%s" % u["written_for"], "- 通过后的作用：%s" % u["effect"],
         "- 建议场景：%s %s" % (u["suggested_scenario"], u["suggested_scenario_reason"]), "", "## 论证主线", ""]
    L += ["%d. %s" % (i, s) for i, s in enumerate(u["storyline"], 1)]
    L += ["", "## 逐章理解", "", "| 章节 | 标题 | 字数 | 重要性 | 作用 | 摘要 |", "|---|---|---|---|---|---|"]
    for c in u["chapters"]:
        L.append("| %s | %s | %d | %s | %s | %s |" % (c["section"], c["title"], c["chars"], IMPN.get(c["importance"]),
                                                   c["role"], c["summary"].replace("|", "/")))
    names = {"decisions": "需要听众决定或确认的事项", "metrics": "量化指标与验收标准", "scope_in": "做什么",
             "scope_out": "不做什么", "deliverables": "交付什么", "constraints": "前提条件与约束",
             "time_limits": "时限承诺", "exceptions": "例外与兜底处理"}
    for k in ITEM_KEYS:
        L += ["", "## " + names[k], ""] + ["- %s `%s`" % (it.get("text", ""), " ".join(it.get("ids", []))) for it in u["key_items"].get(k, [])]
    L += ["", "## 文档自身的问题（仅记录）", ""] + ["- %s `%s`" % (it.get("text", ""), " ".join(it.get("ids", []))) for it in u["doc_issues"]]
    return "\n".join(L) + "\n"


def compact(u, max_points=3):
    """给后续步骤用的精简版理解卡（控制长度）。"""
    IMPN = {"high": "高", "medium": "中", "low": "低"}
    L = ["文档类型：%s；写给：%s；通过后：%s；文档日期：%s" % (u["doc_type"], u["written_for"], u["effect"], u.get("doc_date") or "原文未说明"), "论证主线："]
    L += ["  %d. %s" % (i, s) for i, s in enumerate(u["storyline"], 1)]
    L.append("逐章理解（章节｜重要性｜作用｜摘要｜要点）：")
    for c in u["chapters"]:
        kp = "；".join(truncate(p["text"], 50) for p in c["key_points"][:max_points])
        L.append("  [%s] %s｜%s｜%s｜%s｜%s" % (c["section"], c["title"], IMPN.get(c["importance"]), c["role"], c["summary"], kp))
    names = {"decisions": "待决定/确认", "metrics": "量化指标", "scope_in": "做什么", "scope_out": "不做什么",
             "deliverables": "交付", "constraints": "约束", "time_limits": "时限承诺", "exceptions": "例外与兜底"}
    for k in ITEM_KEYS:
        if u["key_items"].get(k):
            L.append("%s：%s" % (names[k], "；".join("%s%s" % (it.get("text", ""), it.get("ids", [])) for it in u["key_items"][k])))
    if u.get("doc_issues"):
        L.append("文档自身问题（不作为上屏内容）：%s" % "；".join("%s%s" % (it.get("text", ""), it.get("ids", [])) for it in u["doc_issues"]))
    return "\n".join(L)


_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def sum_mismatches(doc):
    """确定性检查：原文"共N项，其中A项、B项、C项"这类写法，分项之和与总数不符时记为文档问题（G33）。"""
    import re
    out = []
    pat = re.compile(r"共(?:计|有)?\s*(\d+)\s*([项个类条套名人家处篇本册])[^。；;]{0,6}?其中([^。；;]{2,160})")
    for b in doc.blocks:
        for m in pat.finditer(b["text"]):
            total, unit, rest = int(m.group(1)), m.group(2), m.group(3)
            parts = re.findall(r"(\d+)\s*" + unit, rest)
            if len(parts) >= 2:
                s = sum(int(x) for x in parts)
                if s != total:
                    out.append({"text": "总数与分项之和不符：原文“共%d%s”，分项 %s 合计 %d%s" % (total, unit, "+".join(parts), s, unit),
                                "ids": [b["id"]], "source": "script"})
    return out
