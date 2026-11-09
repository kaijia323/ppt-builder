"""内容规划（0.6 三步法）：在大纲骨架（frame）之上，先定章、再按章展开到页、最后确定性整合。

  3a 定章（plan_outline，1 次调用）：章 = 外壳前段模块 + 骨架单元 + 外壳后段模块；每章页数由代码按原文篇幅算出建议值和允许范围，
     模型在范围内调整、给听众看得懂的章名和一句话要点，并把“汇报要讲清的问题”分配到章
  3b 展开（plan_chapter，每章 1 次，并行）：只看本章负责的原文章节（全文目录到细级）+ 全局章表（避免越界重复），
     逐页给出标题草稿、回答的问题、依据、要上屏的要点；本章的实质性小节要么有页面、要么说明不讲的理由
  3c 整合（确定性）：编号、按时长缩放用时、问题落页核对（主要问题没有落页 → 只重跑负责该问题的那一章）、跨章重复与相似标题提示

产出与旧版 plan.json 兼容（pages 字段不变），新增 chapters / question_coverage / weak_points，供检查点 1 与后续阶段使用。
任一步失败都有确定性兜底，不会因为某一章出错而整体失败。
"""
import math
import re

from .checks import bigrams
from .core import now_iso, read_json, write_json
from .frame import body_info, covers, parallel_lines, top_keys
from .understand import compact


# ------------------------------------------------------------------ 章与配额（确定性）
def _share_of(keys, share, exclude=()):
    ks = [k for k in top_keys(keys) if not any(k == e or k.startswith(e + ".") for e in exclude)]
    return sum(share.get(k, 0) for k in ks)


def build_chapters(frame, scen, doc, und):
    """章的骨架：外壳前段 + 单元 + 外壳后段。每章带负责的原文章节。"""
    body, share, low = body_info(doc, und)
    by_mod = {m.get("module"): m for m in frame.get("shell") or []}
    sh = scen.get("shell") or {}
    unit_secs = [k for u in frame.get("units") or [] for k in u.get("sections") or []]
    lines = parallel_lines(doc, und)
    chs = []

    def shell_ch(m, part):
        f = by_mod.get(m["module"]) or {}
        secs = list(f.get("sections") or [])
        if not secs:                      # 原文没有对应内容的外壳模块不设章（没有依据可讲）
            return None
        own = [k for k in secs if not any(k == u or k.startswith(u + ".") for u in unit_secs)]
        # 外壳里对后面各单元的概要（如“2.2 建设任务”下 2.2.1—2.2.5 与“4 建设内容”逐项平行）：只做一页总览，不按篇幅分页
        ov = [k for ln in lines if any(covers(x, unit_secs) for x in ln) for k in ln
              if not covers(k, unit_secs) and any(k == s or k.startswith(s + ".") for s in secs)]
        return {"kind": "shell", "part": part, "module": m["module"], "name": f.get("label") or m["module"],
                "optional": bool(m.get("optional")), "fixed_pages": m.get("pages"), "min_pages": m.get("min_pages"),
                "synthesis": bool(m.get("synthesis")), "topics": list(m.get("topics") or []),
                "sections": secs, "overview": [], "metric": [], "evidence_only": bool(m.get("optional")) and not own,
                "share": _share_of(own, share, exclude=ov), "note": f.get("note", ""), "covers": m.get("covers", ""),
                "overview_of_units": ov}
    for m in sh.get("before") or []:
        c = shell_ch(m, "before")
        if c:
            chs.append(c)
    for i, u in enumerate(frame.get("units") or []):
        chs.append({"kind": "unit", "unit": i, "name": u.get("name", ""), "sections": list(u.get("sections") or []),
                    "overview": list(u.get("overview_sections") or []), "metric": list(u.get("metric_sections") or []),
                    "share": _share_of(u.get("sections") or [], share), "summary": u.get("summary", "")})
    known = {m["module"] for part in ("before", "after") for m in sh.get(part) or []}
    for m in sh.get("after") or []:
        c = shell_ch(m, "after")
        if c:
            chs.append(c)
    for mod, f in by_mod.items():          # 模型额外给出的外壳模块（场景里没有）：按 after 指定的位置插入，没有指定放在最后
        if mod not in known and f.get("sections"):
            c = {"kind": "shell", "part": "after", "module": mod, "name": f.get("label") or mod, "optional": True,
                 "sections": f["sections"], "overview": [], "metric": [], "evidence_only": False, "model_added": True,
                 "share": _share_of(f["sections"], share), "note": f.get("note", "")}
            aft = (f.get("after") or "").strip()
            pos = None
            if aft:
                if aft in ("骨架", "建设内容", "units", (scen.get("units_title") or "")):
                    us = [i for i, x in enumerate(chs) if x["kind"] == "unit"]
                    pos = us[-1] + 1 if us else None
                else:
                    hit = [i for i, x in enumerate(chs) if x.get("module") == aft or x.get("name") == aft]
                    pos = hit[-1] + 1 if hit else None
            if pos is None:
                chs.append(c)
            else:
                chs.insert(pos, c)
    if scen.get("group_units"):
        us = [x for x in chs if x["kind"] == "unit"]
        if len(us) >= 3:                   # 同类单元（子系统、模块、分包）≥3 个：汇报时合成一章，每个单元作为小节
            gt = (frame.get("units_title") or scen.get("units_title") or "建设内容").strip()[:14]
            for x in us:
                x["group"] = gt
    for i, c in enumerate(chs, 1):
        c["id"] = "K%d" % i
    return chs


def quotas(chs, n_total):
    """每章建议页数与允许范围。外壳：按篇幅，前段第一个模块至少 2 页；归纳类（只有分散依据）按依据数；
    单元：分剩余页数，按篇幅平方根、最大余数法，篇幅 ≥4% 的单元至少 2 页。"""
    used = 0
    first_before = next((c for c in chs if c["kind"] == "shell" and c.get("part") == "before"), None)
    for c in chs:
        if c["kind"] != "shell":
            continue
        tops = len(top_keys(c["sections"]))
        if c.get("fixed_pages"):
            q = int(c["fixed_pages"])                   # 场景规定页数（如立项评审的“项目概况”一页）
        elif c.get("synthesis") and c.get("topics"):
            q = len(c["topics"])                       # 归纳章：每个主题一页（依据少时允许按 topic_skips 少设，见 range；实测给 2 页时把“创新”和“质量安全”硬塞进一页）
        elif c.get("evidence_only"):
            q = max(1, min(3, len(c["sections"]) // 3 + 1))
        else:
            lo = 2 if c is first_before else max(1, min(2, tops))
            lo = max(lo, int(c.get("min_pages") or 0))
            q = max(lo, min(5 if c is first_before else 4, int(round(c["share"] * n_total))))
        c["quota"] = q
        if c.get("synthesis") and c.get("topics") and len(c["sections"]) < 3:
            # 依据章节少的归纳章：按依据算出的页数照常占用，补足到主题数的那部分不挤占单元页数
            # （实测：归纳章从 2 页补到 3 页时，篇幅 16% 的单元被压到只剩一页总览）
            q0 = max(1, min(3, len(c["sections"]) // 3 + 1)) if c.get("evidence_only") else max(1, min(4, int(round(c["share"] * n_total))))
            q0 = min(q0, q)
            c["_extra"] = q - q0
            used += q0
        else:
            used += q
    synth_q = sum(c.get("_extra", 0) for c in chs)
    units = [c for c in chs if c["kind"] == "unit"]
    rest = max(len(units), n_total - used)
    mins = {c["id"]: (2 if c["share"] >= 0.04 else 1) for c in units}
    # 按篇幅的平方根分配：篇幅大的单元多给，但不按比例线性放大（标杆：占 14%/10%/48% 的三个单元分 5/4/7 页，
    # 线性分配会给出 3/2/10——大单元的小节可两两合并，小单元的每个环节却都要有落脚页）
    w = {c["id"]: math.sqrt(max(c["share"], 0.0)) for c in units}
    tot = sum(w.values()) or 1.0
    base = {}
    for c in units:
        base[c["id"]] = max(mins[c["id"]], int(math.floor(rest * w[c["id"]] / tot)))
    left = rest - sum(base.values())
    order = sorted(units, key=lambda c: -(rest * w[c["id"]] / tot - math.floor(rest * w[c["id"]] / tot)))
    i = 0
    while left > 0 and order:
        base[order[i % len(order)]["id"]] += 1
        left -= 1
        i += 1
    while left < 0:                      # 最小页数挤占时，从页数最多的单元扣
        big = max(units, key=lambda c: base[c["id"]] - mins[c["id"]])
        if base[big["id"]] <= mins[big["id"]]:
            break
        base[big["id"]] -= 1
        left += 1
    for c in units:
        c["quota"] = base[c["id"]]
    # 最小页数合计超过总页数时（短汇报、章多），从页数最多的章往下扣，每章至少 1 页
    floor = lambda c: int(c.get("fixed_pages") or c.get("min_pages") or 1)
    while sum(c["quota"] for c in chs) > max(n_total + synth_q, len(chs)):
        big = max(chs, key=lambda c: c["quota"] - floor(c))
        if big["quota"] <= floor(big):
            break
        big["quota"] -= 1
    for c in chs:
        q = c["quota"]
        lo = 0 if c.get("optional") else max(1, q - 1)
        if c.get("fixed_pages"):
            c["range"] = [q, q]
            continue
        if c.get("min_pages"):
            lo = max(lo, int(c["min_pages"]))
        if c.get("synthesis") and c.get("topics") and len(c["sections"]) >= 3:
            lo = q                                     # 依据充分的归纳章：每个主题一页，不能被压掉
        c["range"] = [lo, q + max(1, int(round(q * 0.3)))]
    return chs


def owner_of_question(q, chs):
    """问题归哪一章：出处章节落在哪章负责的章节里最多。"""
    best, hit = None, 0
    for c in chs:
        keys = c["sections"] + c.get("overview", []) + c.get("metric", [])
        h = sum(1 for k in q.get("answer_sections") or [] if covers(k, keys))
        if h > hit:
            best, hit = c["id"], h
    return best


# ------------------------------------------------------------------ 文本素材
def sec_line(doc, k):
    return "[%s] %s" % (k, doc.sections[k]["title"]) if k in doc.sections else k


def chapters_text(doc, chs, with_quota=True):
    L = []
    for c in chs:
        secs = "、".join(sec_line(doc, k) for k in top_keys(c["sections"])[:8]) or "—"
        extra = ""
        if c.get("overview"):
            extra += "；概要处：" + "、".join(sec_line(doc, k) for k in c["overview"][:3])
        if c.get("metric"):
            extra += "；指标处：" + "、".join(sec_line(doc, k) for k in c["metric"][:4])
        kind = "骨架单元" if c["kind"] == "unit" else (
            "外壳·跨章归纳，主题：%s" % "、".join(t["name"] for t in c["topics"]) if c.get("synthesis") and c.get("topics")
            else ("外壳·归纳（只有分散依据）" if c.get("evidence_only") else "外壳"))
        q = ("，建议 %d 页（允许 %d—%d）" % (c["quota"], c["range"][0], c["range"][1])) if with_quota else ""
        L.append("- %s %s（%s，占正文 %.0f%%%s）：%s%s" % (c["id"], c["name"], kind, c["share"] * 100, q, secs, extra))
    return "\n".join(L)


def questions_text(qs):
    return "\n".join("%d. [%s] %s ｜出处 %s ｜原文怎么答：%s" % (i, q.get("level", ""), q.get("q", ""), ",".join(q.get("answer_sections") or []),
                                                    (q.get("basis") or "")[:120]) for i, q in enumerate(qs, 1)) or "（无）"


def chapter_outline(doc, und, c, max_level=6):
    """本章负责章节的细目录（到最细一级），带字数与占正文比例。"""
    body, share, low = body_info(doc, und)
    keys = top_keys(c["sections"] + c.get("overview", []) + c.get("metric", []))
    L = []
    for k, s in doc.sections.items():
        if not any(k == t or k.startswith(t + ".") for t in keys) or s["level"] > max_level or doc.subtree_chars(k) == 0:
            continue
        tag = "概要" if any(covers(k, [x]) for x in c.get("overview", [])) else ("指标" if any(covers(k, [x]) for x in c.get("metric", [])) else "")
        base = min(doc.sections[t]["level"] for t in keys if k == t or k.startswith(t + "."))
        L.append("%s[%s] %s（%d字，%.1f%%）%s" % ("  " * (s["level"] - base), k, s["title"], doc.subtree_chars(k),
                                             share.get(k, 0) * 100, (" —— " + tag) if tag else ""))
    return "\n".join(L)


def allowed_keys(c):
    def _l(x):              # 0.7.0-dev22：字段不是列表时按空处理（旧版把“模型额外给出的模块”标记也叫 extra=True，会在这里报错）
        return list(x) if isinstance(x, (list, tuple)) else []
    return list(dict.fromkeys(_l(c.get("sections")) + _l(c.get("overview")) + _l(c.get("metric")) + _l(c.get("extra"))))


SYNTH_QUERY = "难点 难题 挑战 瓶颈 关键 创新 首次 突破 优势 质量控制 校验 核验 抽检 一致性 准确率 测试 验收 试运行 安全 保密 权限 信创 国产 适配 风险 应对"


def synthesis_extra(doc, und, c, max_chars):
    """归纳章（原文无专章）：除骨架给出的分散依据外，在正文中按关键词检索难点、亮点、质量与安全相关段落作为依据。
    返回 (章节键列表, 段落ID列表)。"""
    body, share, low = body_info(doc, und)
    terms = [t for t in SYNTH_QUERY.split() if t]
    cands = []
    for b in doc.blocks:
        if b.get("type") == "heading":
            continue
        k = "%s-S%s" % (b["doc"], b["section"])
        if k not in body or any(covers(k, [e]) for e in c["sections"]):
            continue
        hits = sum(1 for t in terms if t in b["text"])
        if hits >= 2 and len(b["text"]) >= 30:
            cands.append((hits, b["id"], k, len(b["text"])))
    cands.sort(key=lambda x: -x[0])
    secs, ids, n = [], [], 0
    for hits, bid, k, ln in cands:
        if n + ln > max_chars:
            continue
        ids.append(bid)
        n += ln
        if k not in secs:
            secs.append(k)
    return secs, ids


def chapter_text(doc, c, max_chars):
    """本章负责章节的原文（带段落ID）：展开到页要看原文写了什么，只看目录只能复述小节标题。超长时按小节篇幅比例摘取。
    跨章归纳章：素材是其他章已规划的页面 + 这些页面依据原文的均衡摘录（每处依据都保留开头部分），
    不再整段给骨架挑出的几个“分散依据”小节——实测模型会只盯着拿到全文的那几节（选型、部署），漏掉其他章里真正的重难点与创新。"""
    if c.get("planned"):
        keys = sorted(c.get("cited") or [], key=lambda k: list(doc.sections).index(k) if k in doc.sections else 10 ** 6)
        t = "【其他章已规划的页面】（标题、要点与依据；归纳时从这里提炼，sources 写其依据章节）\n" + c["planned"]
        t2, _, _ = doc.source_text(keys, max_chars)
        t += "\n\n【这些页面依据的原文摘录】（每处依据按篇幅比例摘取）\n" + t2
        if c.get("extra_blocks"):
            t3, _, _ = doc.source_text(c["extra_blocks"], max_chars // 6)
            t += "\n\n【全文中与难点、亮点、质量与安全相关的其他段落】\n" + t3
        return t
    keys = top_keys([k for k in c["sections"] + c.get("overview", []) + c.get("metric", [])])
    t, used, trunc = doc.source_text(keys, max_chars)
    t += "\n（原文较长，以上按各小节篇幅比例摘取）" if trunc else ""
    return t


def synth_chapter_ids(plan, scen):
    """规划中的跨章归纳章（兼容 0.6 开发版生成、chapters 里还没有 synthesis 标记的 plan.json：按场景外壳模块名判断）。"""
    names = {m["module"] for part in ("before", "after") for m in (scen.get("shell") or {}).get(part) or [] if m.get("synthesis")}
    return {c["id"] for c in plan.get("chapters") or [] if c.get("synthesis") or (c.get("kind") == "shell" and c.get("name") in names)}


def is_synth(c):
    """跨章归纳章：场景外壳中标 synthesis 的模块（如方案评审的“保障与亮点”）。"""
    return c["kind"] == "shell" and bool(c.get("synthesis"))


def sec_of(doc, x):
    """依据（章节键或段落ID）→ 章节键。"""
    x = str(x)
    if x.startswith("USER"):
        return None
    if doc.has_block(x):
        b = doc.block(x)
        return "%s-S%s" % (b["doc"], b["section"])
    k = doc.normalize_key(x)
    return k if doc.valid_section(k) else None


def title_echo(point, titles):
    """要点只是复述小节标题（没有写出原文事实）。"""
    p = re.sub(r"[（(].*?[）)]|插入原图|[：:；;，,。\s]", "", point or "")
    if not p:
        return True
    A = bigrams(p)
    for t in titles:
        B = bigrams(t)
        if A and B and len(A & B) / float(len(A)) >= 0.8 and len(p) <= len(t) + 4:
            return True
    return False


def in_allowed(doc, s, allowed):
    if s.startswith("USER"):
        return True
    k = s if doc.has_block(s) else doc.normalize_key(s)
    if doc.has_block(k):
        k = "%s-S%s" % (doc.block(k)["doc"], doc.block(k)["section"])
    return any(k == a or k.startswith(a + ".") for a in allowed)


def substantive_in(doc, und, c, min_share):
    """本章负责章节之下的实质性小节（比负责的章节低一级及以下的三级及更深小节，占正文 ≥ min_share）。"""
    body, share, low = body_info(doc, und)
    out = []
    for t in top_keys(c["sections"]):
        if t not in doc.sections:
            continue
        lv = doc.sections[t]["level"]
        for k, s in doc.sections.items():
            if k.startswith(t + ".") and s["level"] == max(3, lv + 1) and share.get(k, 0) >= min_share and k in body:
                out.append(k)
    return out


# ------------------------------------------------------------------ 3a 定章
def outline_issues(d, chs, qs, bud):
    iss = []
    got = {x.get("id"): x for x in d.get("chapters") or [] if isinstance(x, dict)}
    for c in chs:
        x = got.get(c["id"])
        if not x:
            iss.append("缺少章 %s（%s）" % (c["id"], c["name"]))
            continue
        try:
            n = int(x.get("pages"))
        except Exception:
            iss.append("%s 的 pages 不是整数" % c["id"])
            continue
        lo, hi = c["range"]
        if not lo <= n <= hi:
            iss.append("%s“%s”页数 %d 不在允许范围 %d—%d 内" % (c["id"], c["name"], n, lo, hi))
        t = re.sub(r"\s", "", x.get("title") or "")
        if n and not 2 <= len(t) <= 14:
            iss.append("%s 的章名“%s”应为 2—14 字" % (c["id"], t))
    extra = [k for k in got if k not in {c["id"] for c in chs}]
    if extra:
        iss.append("章 %s 不存在：只能使用给定的章，不要增删" % extra)
    tot = sum(int(x.get("pages") or 0) for x in got.values() if str(x.get("pages", "")).lstrip("-").isdigit())
    if not bud["min_pages"] <= tot <= bud["max_pages"]:
        iss.append("各章页数合计 %d，不在 %d—%d 内（建议 %d）" % (tot, bud["min_pages"], bud["max_pages"], bud["recommended_pages"]))
    elif abs(tot - bud["recommended_pages"] - sum(c.get("_extra", 0) for c in chs)) > 2:
        iss.append("各章页数合计 %d，与建议的 %d 页相差超过 2 页" % (tot, bud["recommended_pages"] + sum(c.get("_extra", 0) for c in chs)))
    assigned = {}
    for x in got.values():
        for qn in x.get("questions") or []:
            if not isinstance(qn, int) or not 1 <= qn <= len(qs):
                iss.append("%s 的 questions 中 %s 不是有效的问题编号" % (x.get("id"), qn))
                continue
            if int(x.get("pages") or 0) == 0:
                iss.append("%s 分到了问题 %d，但页数为 0" % (x.get("id"), qn))
            assigned.setdefault(qn, []).append(x.get("id"))
    for i, q in enumerate(qs, 1):
        if q.get("level") == "主" and i not in assigned:
            iss.append("主要问题 %d“%s”没有分配到任何章" % (i, q.get("q", "")[:24]))
    return iss[:20]


def outline_fallback(chs, qs):
    out = []
    for c in chs:
        out.append({"id": c["id"], "title": c["name"][:14], "pages": c["quota"], "message": c.get("summary", "") or c.get("covers", ""),
                    "questions": []})
    by = {x["id"]: x for x in out}
    for i, q in enumerate(qs, 1):
        o = owner_of_question(q, chs)
        if o in by:
            by[o]["questions"].append(i)
    return {"storyline": "", "chapters": out, "fallback": True}


# ------------------------------------------------------------------ 3b 展开
def _nb(x):
    return bigrams(re.sub(r"[\s，。；、：:（）()“”\"+—\-/]", "", x))


def dup_points(a, b, thr=0.5):
    """a 中与 b 某条要点字面基本相同的要点。"""
    out = []
    for x in a:
        A = _nb(x)
        if len(A) < 4:
            continue
        if any(len(A & _nb(y)) / float(min(len(A), len(_nb(y))) or 1) >= thr for y in b):
            out.append(x)
    return out



def chapter_issues(d, c, n, doc, und, qs_here, allow_confirm, dcfg, cfg):
    iss = []
    pages = d.get("pages") or []
    lo, hi = max(1, n - 1), n + 1
    if not lo <= len(pages) <= hi:
        iss.append("本章应有 %d 页（允许 %d—%d），现在 %d 页" % (n, lo, hi, len(pages)))
    allowed = allowed_keys(c)
    titles = [doc.sections[k]["title"] for k in doc.sections if any(k == a or k.startswith(a + ".") for a in allowed)]
    for i, p in enumerate(pages, 1):
        srcs = p.get("sources") or []
        if not srcs:
            iss.append("第%d页没有 sources" % i)
        bad = [s for s in srcs if not (doc.valid_section(doc.normalize_key(s)) or doc.has_block(s) or str(s).startswith("USER"))]
        if bad:
            iss.append("第%d页 sources 中的 %s 不存在，请使用目录方括号中的章节键" % (i, bad[:3]))
        out = [s for s in srcs if s not in bad and not in_allowed(doc, s, allowed)]
        if out:
            iss.append("第%d页引用了本章不负责的章节 %s：这些内容由其他章讲，本章不要引用" % (i, out[:3]))
        pts = p.get("points") or []
        if dcfg.get("coverage_check") and len(pts) < 3:
            iss.append("第%d页 points 只有 %d 个，高密度汇报每页写 4—6 个要上屏的具体事实" % (i, len(pts)))
        echo = [x for x in pts if title_echo(x, titles)]
        if pts and len(echo) * 2 > len(pts):
            iss.append("第%d页的 points 大多只是小节标题（如“%s”）：写出原文中的具体事实——数字、组成、步骤、参数、做法" % (i, echo[0][:16]))
    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            A, B = bigrams(pages[i].get("title_draft", "")), bigrams(pages[j].get("title_draft", ""))
            if A and B and len(A & B) / float(min(len(A), len(B))) >= 0.5:
                iss.append("第%d页与第%d页标题相近，可能在讲同一件事：合并，或写清各自的结论" % (i + 1, j + 1))
        if not allow_confirm:
            if re.search(r"评审确认|需.{0,4}确认|待确认事项|提请", p.get("title_draft", "")):
                iss.append("第%d页是“需确认事项”页：本场景不设这类页面，把待确认事项写进相关页面的 points" % i)
    ov = c.get("overview_of_units") or []
    if ov:
        hit = [i for i, p in enumerate(pages, 1) if any(covers(doc.normalize_key(s), ov) for s in p.get("sources") or [] if not str(s).startswith("USER"))]
        if len(hit) > 1:
            iss.append("第%s页都在讲后面各章内容的概要（%s）：这些内容在对应章详细展开，本章只用一页总览（每项一句话说明是什么），其余页删去或改讲本章自己的内容"
                       % ("、".join(map(str, hit)), "、".join("%s %s" % (k.split("-S")[-1], doc.sections[k]["title"][:12]) for k in ov[:5])))
    if is_synth(c):
        # 不设某主题页的理由只能是“原文没有依据”；“已在其他章体现/融入别的页/页数不够”不是理由（实测模型用这类理由跳过创新点）
        bad_reason = re.compile(r"专页|融入|已在|体现|页数|篇幅|合并|不另设|不单设|其他章|别的章")
        skipped = ""
        for x in d.get("topic_skips") or []:
            if not isinstance(x, dict):
                continue
            rs = str(x.get("reason", ""))
            if rs and re.search(r"没有|未|无|不足|缺", rs) and not bad_reason.search(rs):
                skipped += " " + str(x.get("topic", ""))
            elif x.get("topic"):
                iss.append("topic_skips 中“%s”的理由不成立（%s）：只有原文确实没有依据才能不设页；其他章已讲过的内容正是跨章归纳的素材"
                           % (x.get("topic"), rs[:30]))
        for t in c.get("topics") or []:
            hit = any(any(k in (p.get("title_draft", "") + p.get("question", "")) for k in t.get("keys") or []) for p in pages)
            if not hit and t["name"] not in skipped and not any(k in skipped for k in t.get("keys") or []):
                iss.append("归纳主题“%s”没有页面（标题或问题中体现），也没有在 topic_skips 中说明原文依据不足" % t["name"])
        for i, p in enumerate(pages, 1):
            A = bigrams(p.get("title_draft", ""))
            ps = [sec_of(doc, x) for x in p.get("sources") or []]
            ps = [x for x in ps if x]
            for q in c.get("planned_pages") or []:
                B = bigrams(q["title"])
                sim = len(A & B) / float(min(len(A), len(B))) if A and B else 0
                cov = sum(1 for x in ps if covers(x, q.get("sources") or [])) / float(len(ps) or 1)
                if sim >= 0.45 or (sim >= 0.3 and cov >= 0.5):
                    iss.append("第%d页与 %s 章已规划的“%s”基本是同一页：归纳章不要换个标题再讲一遍，改为跨章提炼，或只用一条概括"
                               % (i, q["chapter"], q["title"][:24]))
                    break
                # 标题不像、要点却照搬（实测：底座页的“任务拆解、暂停续接、异常自愈”在重难点页原样再列一遍）
                dp = dup_points(p.get("points") or [], q.get("points") or [])
                if len(dp) >= 2 or (dp and len(dp) * 2.5 >= len(p.get("points") or [])):
                    iss.append("第%d页的要点“%s”等与 %s 章“%s”的要点重复：那一页讲它是什么、怎么建，本页只讲难在哪、新在哪、怎么保障，"
                               "同一事实只用一句话带过，不再逐条列出" % (i, dp[0][:20], q["chapter"], q["title"][:20]))
                    break
    used = [doc.normalize_key(s) for p in pages for s in p.get("sources") or [] if not str(s).startswith("USER")]
    decided = {doc.normalize_key(x.get("section", "")) for x in d.get("section_decisions") or [] if x.get("decision") in ("notes", "skip")}
    if dcfg.get("coverage_check"):
        # 引用上级整节不算覆盖到小节：高密度要讲到小节一层
        miss = [k for k in substantive_in(doc, und, c, float(cfg.get("plan", {}).get("coverage_min_share", 0.015)))
                if not any(u == k or u.startswith(k + ".") for u in used) and k not in decided]
        if miss:
            iss.append("本章以下实质性小节既没有页面引用，也没有在 section_decisions 中说明不讲的理由：%s（可单独成页，或与相关小节合并成页，sources 写到小节键）"
                       % "；".join("%s %s" % (k, doc.sections[k]["title"]) for k in miss[:10]))
    for qn, q in qs_here:
        ans = [k for k in q.get("answer_sections") or [] if any(covers(k, [a]) for a in allowed)]
        if ans and not any(covers(doc.normalize_key(s), ans) for p in pages for s in p.get("sources") or [] if not str(s).startswith("USER")):
            iss.append("问题 %d“%s”分在本章，但没有一页引用它的出处 %s" % (qn, q.get("q", "")[:24], ",".join(ans[:3])))
    return iss[:20]


def chapter_fallback(c, n, doc, und):
    """确定性兜底：把本章负责章节的下级小节按篇幅分到 n 页。"""
    body, share, low = body_info(doc, und)
    leaves = []
    for t in top_keys(c["sections"]):
        kids = [k for k, s in doc.sections.items() if k.startswith(t + ".") and s["level"] == doc.sections.get(t, {}).get("level", 0) + 1]
        leaves += kids or [t]
    leaves = [k for k in leaves if k in doc.sections] or [k for k in c["sections"] if k in doc.sections]
    groups = [[] for _ in range(max(1, n))]
    for i, k in enumerate(sorted(leaves, key=lambda k: list(doc.sections).index(k))):
        groups[min(len(groups) - 1, i * len(groups) // max(1, len(leaves)))].append(k)
    pages = []
    for g in groups:
        if not g:
            continue
        t = doc.sections[g[0]]["title"]
        pages.append({"title_draft": t[:30], "question": "%s怎么做" % t[:20], "sources": g, "points": [doc.sections[k]["title"][:20] for k in g][:6],
                      "minutes": 0.75})
    return {"pages": pages, "section_decisions": [], "fallback": True}


# ------------------------------------------------------------------ 主流程
def run(wd, cfg, log, llm, frame, bud, dname, dcfg, scen, brief, doc, und, feedback, fb_hist, prev, allow_confirm, density_rules):
    qs = frame.get("questions") or []
    chs = quotas(build_chapters(frame, scen, doc, und), bud["recommended_pages"])
    log.info("plan_chapters", "章 %d 个，建议页数 %s" % (len(chs), ",".join("%s=%d" % (c["id"], c["quota"]) for c in chs)),
             chapters=[{k: c.get(k) for k in ("id", "name", "kind", "share", "quota", "range")} for c in chs])
    fb_txt = "\n".join("- " + f["feedback"] for f in fb_hist) if fb_hist else "（无）"
    prev_txt = "（无）"
    if prev and feedback:
        prev_txt = "\n".join("第%d页：%s ｜依据 %s" % (i, p["title_draft"], ",".join(p["sources"])) for i, p in enumerate(prev["pages"], 1))
    common = {"audience": brief.get("audience", ""), "purpose": brief.get("purpose", ""), "scenario": scen.get("display_name"),
              "principle": scen.get("principle", ""), "special_rules": "\n".join("- " + x for x in scen.get("special_rules", [])) or "（无）",
              "must_include": brief.get("must_include") or "（无）", "user_feedback": fb_txt, "previous_plan": prev_txt,
              "doc_date": und.get("doc_date") or "原文未说明", "density_rules": density_rules,
              "user_supplements": "\n".join("[%s] %s" % (b["id"], b["text"]) for b in doc.user_blocks.values()) or "（无）"}
    pf = frame.get("profile") or {}
    # ---------------- 3a
    r = llm.call_json("plan_outline", dict(common, **{
        "understanding": compact(und), "profile": "文档是什么：%s\n一句话核心：%s\n听众拿什么衡量：%s" % (
            pf.get("doc_kind", ""), pf.get("core", ""), "；".join(pf.get("judged_by") or [])),
        "chapters": chapters_text(doc, chs), "questions": questions_text(qs),
        "duration": bud["duration_min"], "content_minutes": bud["content_minutes"], "min_pages": bud["min_pages"],
        "max_pages": bud["max_pages"], "rec_pages": bud["recommended_pages"] + sum(c.get("_extra", 0) for c in chs),
    }), target="outline", validator=lambda d: outline_issues(d, chs, qs, bud))
    if r["ok"] and r["data"]:
        od = r["data"]
        od["fallback"] = False
    else:
        log.warn("plan_outline_fallback", "定章未通过校验，按代码配额定章：%s" % "；".join(r["issues"][:3]))
        od = outline_fallback(chs, qs)
    got = {x["id"]: x for x in od["chapters"]}
    for c in chs:
        x = got.get(c["id"], {})
        c["pages_n"] = int(x.get("pages") if str(x.get("pages", "")).isdigit() else c["quota"])
        c["title"] = (x.get("title") or c["name"])[:14]
        c["message"] = x.get("message", "")
        c["questions"] = [q for q in x.get("questions") or [] if isinstance(q, int) and 1 <= q <= len(qs)]
    active = [c for c in chs if c["pages_n"] > 0]
    def g_line(c):
        if c.get("evidence_only"):
            return "- %s %s（%d 页）：归纳页，从其他章已讲的内容中提炼难点、亮点与保障（依据散在 %s）；这些小节仍由其所在章正常展开" % (
                c["id"], c["title"], c["pages_n"], "、".join(k.split("-S")[-1] for k in c["sections"][:8]))
        return "- %s %s（%d 页）：负责 %s" % (c["id"], c["title"], c["pages_n"], "、".join(sec_line(doc, k) for k in top_keys(c["sections"])[:6]))
    gtxt = "\n".join(g_line(c) for c in active)
    sp = frame.get("special") or {}

    def notes_for(c):
        keys = allowed_keys(c)
        L = []
        for x in sp.get("parallel") or []:
            ks = [k for k in x.get("sections") or [] if any(covers(k, [a]) for a in keys)]
            if len(ks) >= 2 or (ks and c["kind"] == "unit"):
                L.append("- 平行章节 %s：%s（业务需求讲痛点、数据需求讲数据、功能需求讲怎么解决，不要各页复述同一组要素）" % ("、".join(x.get("sections") or []), x.get("reason", "")))
        for x in sp.get("low_info") or []:
            if any(covers(x.get("section", ""), [a]) for a in keys):
                L.append("- 低信息 %s：%s（不单独成页）" % (x.get("section"), x.get("reason", "")))
        if is_synth(c):
            tl = "；".join(t["name"] for t in c.get("topics") or []) or (c.get("covers") or c["name"])
            L.append("- 本章是跨章归纳：原文没有专章。主题：%s。每个主题一页；原文确实没有依据的主题不设页，在 topic_skips 中写明主题和理由。\n"
                     "  素材是【本章原文】中“其他章已规划的页面”和相关段落；原文不一定用“难点”“创新”这些字眼，要从做法本身判断：\n"
                     "  · 重难点：原文花了专门篇幅解决的技术问题（数据一致、规模与性能、准确与可追溯、更新与同步、口径统一等），每条写成“问题 —— 原文的具体做法”\n"
                     "  · 创新点：区别于常规做法的设计（编码、结构、架构、机制、流程上的独到之处），每条写成“设计 —— 具体是什么、带来什么”\n"
                     "  · 质量与安全：数据质量与校核、样本或成果质检、效果评测、测试与试运行、权限与数据安全、环境适配等措施\n"
                     "  每条必须出自原文（sources 写所引页面的依据章节），不得自拟；不要把其他章的某一页换个标题再讲一遍——其他章已有专页的内容（如测试与试运行）只用一条概括" % tl)
        if c.get("note"):
            L.append("- 骨架说明：%s" % c["note"])
        return "\n".join(L) or "（无）"

    for c in active:
        if is_synth(c):
            c["extra"], c["extra_blocks"] = synthesis_extra(doc, und, c, int(cfg.get("plan", {}).get("synthesis_text_chars", 15000)))

    def expand(c, extra_fb=None):
        n = c["pages_n"]
        qs_here = [(i, qs[i - 1]) for i in c["questions"]]
        prev_c = "（无）"
        if prev and feedback:
            pc = [p for p in prev["pages"] if p.get("chapter_id") == c["id"]]
            if pc:
                prev_c = "\n".join("- %s ｜依据 %s" % (p["title_draft"], ",".join(p["sources"])) for p in pc)
        v = dict(common, **{
            "storyline": od.get("storyline", ""), "chapter_id": c["id"], "chapter_title": c["title"], "chapter_message": c["message"] or "（无）",
            "chapter_kind": "骨架单元（文档在讲的一项内容）" if c["kind"] == "unit" else "外壳模块：%s" % (c.get("covers") or c["name"]),
            "n_pages": n, "minutes_per_page": bud["minutes_per_page"], "chapter_outline": chapter_outline(doc, und, c),
            "global_chapters": gtxt, "questions": "\n".join("%d. %s ｜出处 %s ｜原文怎么答：%s" % (i, q.get("q", ""), ",".join(q.get("answer_sections") or []),
                                                                                  (q.get("basis") or "")[:160]) for i, q in qs_here) or "（无）",
            "chapter_notes": notes_for(c), "previous_chapter": prev_c,
            "chapter_text": chapter_text(doc, c, int(cfg.get("plan", {}).get("chapter_text_chars", 50000))),
            "extra_feedback": extra_fb or "（无）",
        })
        rr = llm.call_json("plan_chapter", v, target=c["id"],
                           validator=lambda d: chapter_issues(d, c, n, doc, und, qs_here, allow_confirm, dcfg, cfg))
        if rr["data"] and rr["data"].get("pages"):
            dd = rr["data"]
            dd["ok"] = rr["ok"]
            dd["issues"] = rr["issues"]
            # 只认 notes/skip 两种“不讲”的决定（模型偶尔写 covered 之类，已被页面覆盖的不必登记）
            dd["section_decisions"] = [x for x in dd.get("section_decisions") or [] if isinstance(x, dict) and x.get("decision") in ("notes", "skip", "page", "merge")]
            if is_synth(c) and not rr["ok"]:
                # 归纳章多次重试仍不合格：去掉与其他章重复的页面，宁缺毋滥（重复页会在成稿里讲两遍）
                keep = []
                for p in dd["pages"]:
                    one = chapter_issues({"pages": [p]}, dict(c, topics=[]), 1, doc, und, [], allow_confirm, {}, cfg)
                    if any("基本是同一页" in x for x in one):
                        log.warn("plan_synth_dup_dropped", "%s 归纳页“%s”与其他章重复，已去掉" % (c["id"], p.get("title_draft", "")[:30]))
                    else:
                        keep.append(p)
                if keep:
                    dd["pages"] = keep
            return dd
        log.warn("plan_chapter_fallback", "%s %s 展开失败，按小节确定性分页：%s" % (c["id"], c["title"], "；".join(rr["issues"][:3])))
        return chapter_fallback(c, n, doc, und)

    # 先展开非归纳章（并行），再展开归纳章：归纳章以其他章已规划的页面为素材跨章提炼（实测只靠关键词检索，
    # 找不到“编码跨年一致”“关系增量更新”这类原文不叫“难点”的真正重难点）
    synth = [c for c in active if is_synth(c)]
    first = [c for c in active if c not in synth]
    res = llm.map(expand, first, "plan_chapter")
    for c, x in zip(first, res):
        c["result"] = x if isinstance(x, dict) else chapter_fallback(c, c["pages_n"], doc, und)
    if synth:
        planned = []
        for c in first:
            for p in c["result"].get("pages") or []:
                planned.append("- [%s %s] %s ｜要点：%s ｜依据 %s" % (c["id"], c["title"], p.get("title_draft", ""),
                                                             "；".join(p.get("points") or [])[:300], ",".join(p.get("sources") or [])))
        cited = []
        for c in first:
            for p in c["result"].get("pages") or []:
                for x in p.get("sources") or []:
                    k = sec_of(doc, x)
                    if k and k not in cited:
                        cited.append(k)
        pp = [{"chapter": c0["id"], "title": p.get("title_draft", ""), "points": list(p.get("points") or []),
               "sources": [k for k in (sec_of(doc, x) for x in p.get("sources") or []) if k]}
              for c0 in first for p in c0["result"].get("pages") or []]
        for c in synth:
            c["planned"] = "\n".join(planned)
            c["planned_pages"] = pp
            c["cited"] = cited
            c["extra"] = list(dict.fromkeys((c.get("extra") if isinstance(c.get("extra"), list) else []) + cited))
        res = llm.map(expand, synth, "plan_chapter")
        for c, x in zip(synth, res):
            c["result"] = x if isinstance(x, dict) else chapter_fallback(c, c["pages_n"], doc, und)
    # ---------------- 3c 整合
    def assemble():
        pages = []
        for c in active:
            for p in c["result"].get("pages") or []:
                srcs = []
                for s in p.get("sources") or []:
                    k = str(s) if str(s).startswith("USER") else sec_of(doc, s)     # 段落ID 归并到所在章节
                    if k and k not in srcs:
                        srcs.append(k)
                pages.append({"title_draft": p.get("title_draft", ""), "question": p.get("question", ""), "sources": srcs,
                              "points": p.get("points", []), "minutes": float(p.get("minutes") or bud["minutes_per_page"]),
                              "chapter_id": c["id"], "chapter": active.index(c) + 1})
        tot = sum(p["minutes"] for p in pages) or 1.0
        k = bud["content_minutes"] / tot
        for i, p in enumerate(pages, 1):
            p["no"], p["page_key"] = i, "P%02d" % i
            p["minutes"] = round(p["minutes"] * k, 2)
        return pages

    def q_cover(pages):
        cov = []
        for i, q in enumerate(qs, 1):
            ans = q.get("answer_sections") or []
            hit = [p["no"] for p in pages if any(covers(s, ans) for s in p["sources"] if not str(s).startswith("USER"))]
            cov.append({"no": i, "q": q.get("q", ""), "level": q.get("level", ""), "pages": hit, "answerable": q.get("answerable")})
        return cov
    pages = assemble()
    cov = q_cover(pages)
    # 主要问题没有落页：只重跑负责它的那一章（一次）
    rerun = {}
    for x in cov:
        if x["level"] == "主" and not x["pages"] and qs[x["no"] - 1].get("answer_sections"):
            q = qs[x["no"] - 1]
            o = next((c for c in active if x["no"] in c["questions"]), None) or next((c for c in active if c["id"] == owner_of_question(q, active)), None)
            if o:
                o["questions"] = sorted(set(o["questions"]) | {x["no"]})
                rerun.setdefault(o["id"], (o, []))[1].append("问题 %d“%s”必须有页面讲清（引用其出处 %s）" % (x["no"], x["q"], ",".join(q.get("answer_sections") or [])))
    for cid, (c, msgs) in rerun.items():
        log.info("plan_chapter_rerun", "%s %s：主要问题没有落页，重新展开" % (cid, c["title"]), issues=msgs)
        c["result"] = expand(c, "\n".join(msgs))
    if rerun:
        pages = assemble()
        cov = q_cover(pages)
    for x in cov:
        log.gate("plan_question_covered", "Q%d" % x["no"], bool(x["pages"]), x["q"][:40])
    # 跨章重复：不同章的页面只引用同一个小节
    dup = []
    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            a, b = pages[i], pages[j]
            if a["chapter_id"] != b["chapter_id"] and set(a["sources"]) & set(b["sources"]) and \
                    (len(a["sources"]) == 1 or len(b["sources"]) == 1):
                A, B = bigrams(a["title_draft"]), bigrams(b["title_draft"])
                if len(A & B) / float(min(len(A), len(B)) or 1) >= 0.4:
                    dup.append({"pages": [a["no"], b["no"]], "sources": sorted(set(a["sources"]) & set(b["sources"]))})
                    log.warn("plan_cross_dup", "第%d页与第%d页（不同章）引用同一小节且标题相近，可能重复" % (a["no"], b["no"]))
    decisions = []
    for c in active:
        for x in c["result"].get("section_decisions") or []:
            if isinstance(x, dict) and x.get("section"):
                x = dict(x)
                x["chapter_id"] = c["id"]
                decisions.append(x)
    chapters = [{"id": c["id"], "title": c["title"], "kind": c["kind"], "name": c["name"], "message": c["message"],
                 "synthesis": is_synth(c), "topics": [t["name"] for t in c.get("topics") or []] if is_synth(c) else [],
                 "quota": c["quota"], "pages_n": c["pages_n"], "share": round(c["share"], 3),
                 "pages": [p["no"] for p in pages if p["chapter_id"] == c["id"]], "questions": c["questions"],
                 "llm_ok": c["result"].get("ok", False), "fallback": bool(c["result"].get("fallback")),
                 "residual_issues": c["result"].get("issues", [])} for c in active]
    dropped = [{"id": c["id"], "name": c["name"]} for c in chs if c["pages_n"] == 0]
    # 汇报章（目录、过渡页、页码按它编）：同组的规划章（如 5 个子系统）合成一章；规划仍按单元逐章展开
    disp = []
    for c in active:
        if c.get("group") and disp and disp[-1].get("group") == c["group"]:
            disp[-1]["ids"].append(c["id"])
        else:
            disp.append({"title": c.get("group") or c["title"], "group": c.get("group"), "ids": [c["id"]], "synthesis": is_synth(c)})
    idx = {cid: i for i, d in enumerate(disp, 1) for cid in d["ids"]}
    for p in pages:
        p["chapter"] = idx.get(p["chapter_id"], p["chapter"])
    for ch in chapters:
        ch["display"] = idx.get(ch["id"])
    for d in disp:
        d["pages"] = [p["no"] for p in pages if p["chapter_id"] in d["ids"]]
    return {"storyline": od.get("storyline", "") or pf.get("core", ""), "pages": pages, "chapters": chapters, "dropped_chapters": dropped,
            "display_chapters": disp, "agenda": [d["title"] for d in disp], "section_decisions": decisions,
            "question_coverage": cov, "weak_points": frame.get("weak_points") or [], "cross_duplicates": dup,
            "outline_fallback": bool(od.get("fallback")), "outline_llm_ok": r["ok"], "outline_issues": r["issues"]}
