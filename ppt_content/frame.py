"""大纲骨架与追问清单（0.6，规划的第一小步）。

在通读结果的基础上，一次模型调用产出：
  profile    文档画像：文档是什么、听众拿什么衡量它、一句话核心
  units      内容骨架：文档在讲的几项建设内容（需求模块、研究内容……），每项对应的实现章节、概要章节、指标章节
  shell      场景外壳模块在原文中的对应章节
  special    特殊章节：概要+实现两处讲同一内容、指标章、低信息章、平行章
  questions  专家可能追问的问题（按文档生成），每条标原文出处与能否回答

确定性校验（不合格带问题清单重试）：章节键存在；骨架 2—8 个且互不大面积重叠；正文中篇幅 ≥2% 的一、二级章节都有归属；
问题 5—10 个、主要问题 3—6 个；能回答的问题须给出处，且出处章节与问题有词语重合；“不能”回答的不得给出处。
仍不合格 → 确定性兜底（正文篇幅最大的一级章节的下级章节作骨架；底线问题）。

产出：03_plan/frame.json、frame.md（给用户看的骨架与追问清单）。
"""
import re

from .check import _terms
from .core import load_scenario, read_json, write_json, write_text
from .docmodel import Doc
from .understand import compact

_STOPQ = {"如何", "怎么", "是否", "能否", "哪些", "什么", "保证", "实现", "进行", "通过", "以及", "项目", "方案", "建设"}


def body_info(doc, und):
    """正文章节与篇幅占比（排除重要性为低的章节与标题前内容）。"""
    low = {c["section"] for c in und["chapters"] if c.get("importance") == "low"}
    body = {k: s for k, s in doc.sections.items()
            if s["level"] >= 1 and not any(k == l or k.startswith(l + ".") for l in low)}
    total = float(sum(doc.subtree_chars(k) for k, s in body.items() if s["level"] == 1) or 1)
    share = {k: doc.subtree_chars(k) / total for k in body}
    return body, share, low


def outline_with_share(doc, und, max_level=3):
    imp = {c["section"]: {"high": "重要性高", "medium": "重要性中", "low": "重要性低"}.get(c["importance"], "")
           for c in und["chapters"]}
    body, share, low = body_info(doc, und)
    total = float(sum(doc.subtree_chars(k) for k, s in body.items() if s["level"] == 1) or 1)
    L = []
    for k, s in doc.sections.items():
        if s["level"] < 1 or s["level"] > max_level or doc.subtree_chars(k) == 0:
            continue
        pct = doc.subtree_chars(k) / total * 100
        note = imp.get(k) or ("附属内容" if k not in body else "")
        L.append("%s[%s] %s（%d字，%s）%s" % ("  " * (s["level"] - 1), k, s["title"], doc.subtree_chars(k),
                                          ("%.1f%%" % pct) if k in body else "不计入正文", (" —— " + note) if note else ""))
    return "\n".join(L)


_GEN_TITLE = ["数据需求分析", "需求分析", "功能需求", "数据需求", "需求", "分析", "说明", "设计", "建设", "内容", "场景"]


def _tcore(t):
    t = re.sub(r"[“”\"'（）()、，,\s]", "", t or "")
    for g in _GEN_TITLE:
        t = t.replace(g, "")
    return t


def _bg(t):
    return {t[i:i + 2] for i in range(len(t) - 1)}


def parallel_lines(doc, und, thr=0.75):
    """原文平行结构：同一项内容在不同一级章中分别展开（如业务需求 3.2 / 数据需求 4.1 / 功能需求 5.1，
    或建设任务概要 2.2.1 / 建设内容 4.1 / 指标 6.1）。按标题相似度确定性识别，去掉全文高频词，不跨同一章。"""
    from collections import Counter
    body, share, low = body_info(doc, und)
    cand = [k for k, s in body.items() if s["level"] in (2, 3)]
    df = Counter(x for k in cand for x in _bg(_tcore(doc.sections[k]["title"])))
    common = {x for x, c in df.items() if c >= max(4, 0.12 * len(cand))}
    B = {k: _bg(_tcore(doc.sections[k]["title"])) - common for k in cand}
    top = lambda k: k.split("-S")[-1].split(".")[0]
    par = {}

    def f(x):
        par.setdefault(x, x)
        while par[x] != x:
            x = par[x]
        return x
    for i, a in enumerate(cand):
        for b in cand[i + 1:]:
            if top(a) == top(b) or len(B[a]) < 2 or len(B[b]) < 2:
                continue
            if len(B[a] & B[b]) / float(min(len(B[a]), len(B[b]))) >= thr:
                par[f(a)] = f(b)
    groups = {}
    for x in list(par):
        groups.setdefault(f(x), []).append(x)
    out = []
    for v in groups.values():
        v = [k for k in v if not any(o != k and k.startswith(o + ".") for o in v)]
        if len({top(k) for k in v}) >= 2:
            out.append(sorted(v, key=lambda k: list(doc.sections).index(k)))
    return out


def lines_text(doc, lines):
    return "\n".join("- " + " / ".join("[%s] %s" % (k, doc.sections[k]["title"]) for k in ln) for ln in lines) or "（未识别到）"


def _keys(doc, xs):
    out, bad = [], []
    for x in xs or []:
        k = doc.normalize_key(str(x))
        (out if doc.valid_section(k) else bad).append(k if doc.valid_section(k) else x)
    return out, bad


def covers(k, keys):
    return any(k == u or k.startswith(u + ".") or u.startswith(k + ".") for u in keys)


def frame_issues(d, doc, und, scen):
    iss = []
    body, share, low = body_info(doc, und)
    pf = d.get("profile") or {}
    if not pf.get("doc_kind") or not pf.get("core") or not (2 <= len(pf.get("judged_by") or []) <= 5):
        iss.append("profile 不完整：doc_kind、core 必填，judged_by 2—5 条")
    units = d.get("units") or []
    if not 2 <= len(units) <= 8:
        iss.append("units 有 %d 个，应为 2—8 个" % len(units))
    if scen.get("adaptive"):
        # 自适应骨架：附录、编制依据、参考文献等不应成章（除非汇报目的就是讲它）
        APX = re.compile(r"^(附录|附件|附表|参考文献|编制依据|术语|缩略语|名词解释|填表说明)")
        for u in units:
            ks = _keys(doc, u.get("sections"))[0]
            if ks and all(APX.match(doc.sections[k]["title"] or "") or any(APX.match(doc.sections[a]["title"] or "")
                                                                           for a in doc.sections if k.startswith(a + ".")) for k in ks):
                iss.append("“%s”只由附录/参考文献/编制依据等章节构成：与汇报目的关系不大的内容不设章，放进 low_info；"
                           "确实要讲的（如附录中的预算表）并入相关章" % u.get("name"))
    all_unit_keys = []
    for i, u in enumerate(units, 1):
        ks, bad = _keys(doc, u.get("sections"))
        ok2, bad2 = _keys(doc, (u.get("overview_sections") or []) + (u.get("metric_sections") or []))
        if bad or bad2:
            iss.append("骨架“%s”的章节键 %s 不存在" % (u.get("name"), (bad + bad2)[:4]))
        if not ks:
            iss.append("骨架“%s”没有 sections" % u.get("name"))
        all_unit_keys.append(set(ks))
        if not 4 <= len(re.sub(r"\s", "", u.get("name", ""))) <= 14:
            iss.append("骨架名称“%s”应为 4—14 字" % u.get("name"))
    for i in range(len(all_unit_keys)):
        for j in range(i + 1, len(all_unit_keys)):
            dup = [k for k in all_unit_keys[i] if covers(k, all_unit_keys[j])]
            if dup:
                iss.append("骨架“%s”与“%s”的章节重叠（%s），同一章节只归一个单元：如果一节原文（如一节业务需求）同时对应这几个单元，"
                           "说明它们是原文的同一项内容，应合并为一个单元；只是共享的概要则放 overview_sections。不要为避开重叠把它移进外壳"
                           % (units[i]["name"], units[j]["name"], dup[:3]))
    got_mod = {m.get("module"): m for m in d.get("shell") or []}
    for part in ("before", "after"):
        for m in (scen.get("shell") or {}).get(part) or []:
            if m.get("required") and not _keys(doc, (got_mod.get(m["module"]) or {}).get("sections"))[0]:
                iss.append("外壳模块“%s”是必设模块（%s），请从原文各处找到对应章节填入 sections" % (m["module"], m.get("covers", "")))
    # 归属：篇幅 ≥2% 的一、二级正文章节须出现在骨架、外壳或特殊章节中
    placed = set()
    for u in units:
        for f in ("sections", "overview_sections", "metric_sections"):
            placed |= set(_keys(doc, u.get(f))[0])
    optional = {m["module"] for part in ("before", "after") for m in (scen.get("shell") or {}).get(part) or []
                if m.get("optional")}
    unit_secs = set().union(*all_unit_keys) if all_unit_keys else set()
    for m in d.get("shell") or []:
        ks, bad = _keys(doc, m.get("sections"))
        if bad:
            iss.append("外壳“%s”的章节键 %s 不存在" % (m.get("module"), bad[:4]))
        if m.get("module") not in optional:
            clash = [k for k in ks if any(k == u or k.startswith(u + ".") for u in unit_secs)]
            if clash:
                iss.append("外壳“%s”占用了已归入骨架单元的章节 %s：这些章节在单元里讲，外壳不要再放" % (m.get("module"), clash[:4]))
        placed |= set(ks)
    # 平行结构：同一项内容在不同章的展开归同一个单元，不拆到两个单元，也不放进外壳
    owner = {}
    for i, u in enumerate(units):
        for f in ("sections", "overview_sections", "metric_sections"):
            for k in _keys(doc, u.get(f))[0]:
                owner.setdefault(k, set()).add(i)
    shell_keys = {k for m in d.get("shell") or [] if m.get("module") not in optional for k in _keys(doc, m.get("sections"))[0]}
    for ln in parallel_lines(doc, und):
        own = set()
        for k in ln:
            for o, ids in owner.items():
                if covers(k, [o]):
                    own |= ids
        names = "、".join("%s %s" % (k.split("-S")[-1], doc.sections[k]["title"]) for k in ln)
        in_shell = [k for k in ln if any(sk == k or sk.startswith(k + ".") for sk in shell_keys) and share.get(k, 0) >= 0.01]
        if not own and in_shell:
            iss.append("原文 %s 是同一项内容在不同章的展开，属于文档在讲的一项内容，应归入骨架单元（独立成单元或并入相关单元），"
                       "不要放进外壳" % names)
        elif in_shell:
            iss.append("原文 %s 是同一项内容在不同章的展开，应归入同一个骨架单元（主体放 sections，概要放 overview_sections），"
                       "不要把 %s 放进外壳" % (names, "、".join(k.split("-S")[-1] for k in in_shell)))
        main = [i for i, u in enumerate(units) if any(covers(k, _keys(doc, u.get("sections"))[0]) for k in ln)]
        if len(main) > 1:
            iss.append("原文 %s 是同一项内容，却分在“%s”几个单元的 sections 里：合并为一个单元，或只在一个单元里讲"
                       % (names, "”“".join(units[i]["name"] for i in main)))
    sp = d.get("special") or {}
    for x in sp.get("overview_impl") or []:
        placed |= set(_keys(doc, [x.get("overview"), x.get("impl")])[0])
    placed |= set(_keys(doc, sp.get("metric_chapters"))[0])
    placed |= set(_keys(doc, [x.get("section") for x in sp.get("low_info") or []])[0])
    for x in sp.get("parallel") or []:
        placed |= set(_keys(doc, x.get("sections"))[0])
    miss = [k for k, s in body.items() if s["level"] in (1, 2) and share[k] >= 0.02 and not covers(k, placed)]
    if miss:
        iss.append("以下正文章节没有归属（应归入某个骨架单元、外壳模块，或列为低信息/指标章节）：%s"
                   % "、".join("%s %s" % (k, doc.sections[k]["title"]) for k in miss[:10]))
    qs = d.get("questions") or []
    if not 5 <= len(qs) <= 8:
        iss.append("questions 有 %d 个，应为 5—8 个" % len(qs))
    mains = [q for q in qs if q.get("level") == "主"]
    if not 3 <= len(mains) <= 6:
        iss.append("主要问题有 %d 个，应为 3—6 个" % len(mains))
    from .checks import bigrams
    issue_txt = [it.get("text", "") for it in und.get("doc_issues", []) or []]
    for i, q in enumerate(qs, 1):
        ans = q.get("answerable")
        ks, bad = _keys(doc, q.get("answer_sections"))
        if ans not in ("能", "部分"):
            iss.append("问题%d 的 answerable 只能取 能/部分（原文不能回答的放进 weak_points）" % i)
        if bad:
            iss.append("问题%d 的出处 %s 不存在" % (i, bad[:3]))
        if not ks:
            iss.append("问题%d 没有给出处章节" % i)
        else:
            qt = {t for t in _terms(q.get("q", "")) if t not in _STOPQ and len(t) >= 2}
            st = set()
            for k in ks:
                st |= _terms(doc.subtree_text(k)[:20000])
            if qt and not (qt & st):
                iss.append("问题%d“%s”与所给出处章节没有共同的词，请核对出处" % (i, q.get("q", "")[:30]))
        if len(q.get("q", "")) < 8:
            iss.append("问题%d 太笼统，要具体到这份文档" % i)
    for i, w in enumerate((d.get("weak_points") or []) + qs, 1):
        A = bigrams(w.get("q", ""))
        for t in issue_txt:
            B = bigrams(t)
            if A and B and len(A & B) / float(min(len(A), len(B))) >= 0.5:
                iss.append("“%s”是文档自身的笔误或前后不一致，不作为追问或薄弱点" % w.get("q", "")[:30])
                break
    if len(d.get("weak_points") or []) > 5:
        iss.append("weak_points 最多 5 个")
    for i, q in enumerate(qs, 1):
        A = bigrams(q.get("q", ""))
        for w in d.get("weak_points") or []:
            Bw = bigrams(w.get("q", ""))
            if A and Bw and len(A & Bw) / float(min(len(A), len(Bw))) >= 0.65:
                iss.append("问题%d“%s”与薄弱点“%s”问的是同一件事：原文讲清的部分作问题，没讲清的部分只放薄弱点"
                           % (i, q.get("q", "")[:24], w.get("q", "")[:24]))
                break
    return iss[:20]


def fallback(doc, und, scen):
    """确定性兜底：正文篇幅最大的一级章节，其下级章节作骨架；底线问题（无出处）。"""
    body, share, low = body_info(doc, und)
    tops = sorted([k for k, s in body.items() if s["level"] == 1], key=lambda k: -share[k])
    units = []
    if scen.get("adaptive"):
        # 自适应场景兜底：按原文顺序取篇幅 ≥2% 的一级章节（去掉附录、参考文献、编制依据等）
        APX = re.compile(r"^(附录|附件|附表|参考文献|编制依据|术语|缩略语|名词解释|填表说明)")
        keep = [k for k, s in body.items() if s["level"] == 1 and share[k] >= 0.02 and not APX.match(s["title"] or "")]
        units = [{"name": doc.sections[k]["title"][:14], "sections": [k], "overview_sections": [], "metric_sections": [],
                  "summary": "（兜底：按原文章节）"} for k in keep[:8]]
        tops = [] if units else tops
    if tops:
        big = tops[0]
        kids = [k for k, s in body.items() if s["level"] == 2 and k.startswith(big + ".") and share[k] >= 0.02]
        units = [{"name": doc.sections[k]["title"][:14], "sections": [k], "overview_sections": [], "metric_sections": [],
                  "summary": "（兜底：按原文章节）"} for k in kids[:8]] or \
                [{"name": doc.sections[big]["title"][:14], "sections": [big], "overview_sections": [], "metric_sections": [],
                  "summary": "（兜底）"}]
    return {"profile": {"doc_kind": und.get("doc_type", ""), "judged_by": [], "core": ""}, "units": units, "shell": [],
            "special": {}, "questions": [{"q": q, "level": "主", "answer_sections": [], "answerable": "部分",
                                          "basis": "（兜底：场景底线问题）"} for q in scen.get("baseline_questions", [])],
            "fallback": True}


def normalize(d, doc):
    for u in d.get("units") or []:
        for f in ("sections", "overview_sections", "metric_sections"):
            u[f] = _keys(doc, u.get(f))[0]
    for m in d.get("shell") or []:
        m["sections"] = _keys(doc, m.get("sections"))[0]
    for q in d.get("questions") or []:
        q["answer_sections"] = _keys(doc, q.get("answer_sections"))[0]
    return d


def verify_questions(d, doc, llm, log, per_q=6000, total=48000, per_w=2500):
    """逐题核对（0.6）：
    - 问题：把出处章节的原文交给模型（放不下时按与问题的相关度挑段落），确认能否回答并给出段落ID；“不能”的移入薄弱点
    - 薄弱点：在全文中检索与该质疑最相关的段落，确认原文是否真的没讲；原文其实讲清的删除，讲了一部分的改写缺口
    （通读只看了摘要，判断不可靠；实测按篇幅截断会把“与包2/包3的界线”这类末尾小节截掉，误判为“缺”）"""
    qs = d.get("questions") or []
    wk = d.get("weak_points") or []
    if not qs:
        return
    budget = max(1500, min(per_q, total // len(qs)))
    blocks, allowed, wallowed = [], {}, {}
    for i, q in enumerate(qs, 1):
        txt, used, part = doc.focused_text(q.get("answer_sections") or [], q.get("q", ""), budget)
        allowed[i] = set(used)
        blocks.append("### 问题%d：%s%s\n%s" % (i, q.get("q", ""), "（原文较长，以下为与问题相关的段落）" if part else "",
                                             txt or "（无出处原文）"))
    wblocks = []
    for j, w in enumerate(wk, 1):
        txt, used, _ = doc.focused_text([], (w.get("q") or "") + (w.get("gap") or ""), per_w)
        wallowed[j] = set(used)
        wblocks.append("### 薄弱点%d：%s（认为原文缺：%s）\n%s" % (j, w.get("q", ""), w.get("gap", ""),
                                                          txt or "（全文检索未找到相关段落）"))

    def val(x):
        iss = []
        got = {r.get("no"): r for r in x.get("results", []) if isinstance(r, dict)}
        for i in range(1, len(qs) + 1):
            r = got.get(i)
            if not r:
                iss.append("缺少问题%d 的结果" % i)
                continue
            if r.get("answerable") not in ("能", "部分", "不能"):
                iss.append("问题%d answerable 只能取 能/部分/不能" % i)
            bad = [e for e in r.get("evidence") or [] if e not in allowed[i]]
            if bad:
                iss.append("问题%d 的 evidence %s 不在给出的原文中" % (i, bad[:3]))
            if r.get("answerable") in ("能", "部分") and not r.get("evidence"):
                iss.append("问题%d 标为能回答，但没有 evidence" % i)
        wgot = {r.get("no"): r for r in x.get("weak_results", []) or [] if isinstance(r, dict)}
        for j in range(1, len(wk) + 1):
            r = wgot.get(j)
            if not r:
                iss.append("缺少薄弱点%d 的结果（weak_results）" % j)
                continue
            if r.get("covered") not in ("已讲清", "部分", "没讲"):
                iss.append("薄弱点%d covered 只能取 已讲清/部分/没讲" % j)
            bad = [e for e in r.get("evidence") or [] if e not in wallowed[j]]
            if bad:
                iss.append("薄弱点%d 的 evidence %s 不在给出的原文中" % (j, bad[:3]))
            if r.get("covered") in ("已讲清", "部分") and not r.get("evidence"):
                iss.append("薄弱点%d 标为原文讲了，但没有 evidence" % j)
        return iss
    items = "\n\n".join(blocks)
    if wblocks:
        items += "\n\n========== 以下是薄弱点 ==========\n\n" + "\n\n".join(wblocks)
    r = llm.call_json("frame_verify", {"items": items}, target="questions", validator=val)
    if not r["ok"] or not r["data"]:
        log.warn("frame_verify_failed", "追问核对未通过，保留原判断：%s" % "；".join(r["issues"][:3]))
        return
    got = {x.get("no"): x for x in r["data"]["results"]}
    weak = []
    wgot = {x.get("no"): x for x in r["data"].get("weak_results") or []}
    for j, w in enumerate(wk, 1):
        x = wgot.get(j, {})
        if x.get("covered") == "已讲清":
            log.info("frame_weak_dropped", "薄弱点“%s”核对后原文已讲清（%s），删除" % (w.get("q", "")[:30], ",".join(x.get("evidence") or [])))
            continue
        if x.get("covered") == "部分" and x.get("gap"):
            w["gap"] = x["gap"]
        w["verified"] = x.get("covered")
        w["evidence"] = [e for e in x.get("evidence") or [] if e in wallowed[j]]
        weak.append(w)
    keep = []
    for i, q in enumerate(qs, 1):
        x = got.get(i, {})
        q["answerable_before_verify"] = q.get("answerable")
        q["answerable"] = x.get("answerable", q.get("answerable"))
        q["evidence"] = [e for e in x.get("evidence") or [] if e in allowed[i]]
        if x.get("answer"):
            q["basis"] = x["answer"]
        q.pop("missing", None)
        if x.get("missing") and q["answerable"] == "部分":
            q["missing"] = x["missing"]
        if q["answerable"] == "不能":
            weak.append({"q": q.get("q"), "gap": "核对原文后未找到回答" + ("：" + x["missing"] if x.get("missing") else "")})
            log.info("frame_question_moved", "问题“%s”核对后原文不能回答，移入薄弱点" % q.get("q", "")[:30])
        else:
            keep.append(q)
    d["questions"], d["weak_points"] = keep, weak
    d["verified"] = True


ADAPTIVE_RULES = """【本场景没有固定骨架：请结合汇报目的，从原文中提炼汇报的各章】
- units 就是汇报的各章（3—8 章），按汇报主线排序（先背景、问题或主题，再核心内容，最后结论、成效或下一步），前后衔接连贯
- 不要照抄原文目录：与汇报目的关系不大的章节（附录、编制依据、术语与缩略语、参考文献、填表说明、附件清单）不设章，放进 low_info；
  同一主题散在原文多处的合成一章（sections 列出各处）；原文一章内容过多、包含几个独立主题的，可以拆成两章
- name：听众看得懂的章名（用原文叫法或其概括，4—14 字）；summary：一句话说明这一章在汇报主线中的作用（承上启下）
- profile.core 写这次汇报的主线：要让听众接受的核心结论
- 篇幅大的内容不一定重要、篇幅小的不一定不讲：取舍以汇报目的为准；页数会按篇幅自动给出建议，规划时再按重要性调整
"""


def shell_text(scen):
    sh = scen.get("shell") or {}
    L = []
    for part, name in (("before", "骨架之前"), ("after", "骨架之后")):
        for m in sh.get(part) or []:
            L.append("- %s：%s（%s）%s" % (name, m["module"], m.get("covers", ""),
                                         "，必设" if m.get("required") else ("，原文有依据才设（原文没有就填 []，不要硬凑）" if m.get("optional") else "")))
    return "\n".join(L) or "（无）"


def run(wd, cfg, log, llm):
    brief = wd.brief()
    scen = load_scenario(brief["scenario"])
    doc = Doc(wd)
    und = read_json(wd.stage("understand", "understanding.json"))
    r = llm.call_json("frame", {
        "understanding": compact(und), "outline": outline_with_share(doc, und),
        "scenario": scen.get("display_name"), "audience": brief.get("audience", ""), "purpose": brief.get("purpose", ""),
        "skeleton_by": scen.get("skeleton_by", "主要内容"), "shell": shell_text(scen),
        "adaptive_rules": ADAPTIVE_RULES if scen.get("adaptive") else "",
        "lines": lines_text(doc, parallel_lines(doc, und)),
    }, target="all", validator=lambda d: frame_issues(d, doc, und, scen))
    if r["ok"] and r["data"]:
        d = normalize(r["data"], doc)
        d["fallback"] = False
    else:
        log.warn("frame_fallback", "骨架与追问清单未通过校验，使用确定性兜底：%s" % "；".join(r["issues"][:3]))
        d = fallback(doc, und, scen)
    d["residual_issues"] = [] if r["ok"] else r["issues"]
    if not d.get("fallback"):
        verify_questions(d, doc, llm, log)
    d["weak_points"] = dedupe_weak(d.get("weak_points"))[:5]
    body, share, low = body_info(doc, und)
    for u in d.get("units") or []:
        u["share"] = round(sum(share.get(k, 0) for k in top_keys(u["sections"])), 3)
    write_json(wd.stage("plan", "frame.json"), d)
    write_text(wd.stage("plan", "frame.md"), render(d, doc, scen))
    for q in d.get("questions") or []:
        log.gate("frame_question_answerable", q.get("q", "")[:40], q.get("answerable") != "不能", q.get("answerable"))
    return {"units": len(d.get("units") or []), "questions": len(d.get("questions") or []),
            "weak_points": len(d.get("weak_points") or []), "verified": bool(d.get("verified")),
            "fallback": d.get("fallback"), "llm_ok": r["ok"], "attempts": r["attempts"]}


def top_keys(ks):
    """去掉已被祖先章节包含的子章节，避免占比重复计算。"""
    ks = list(dict.fromkeys(ks))
    return [k for k in ks if not any(o != k and k.startswith(o + ".") for o in ks)]


def dedupe_weak(items, thr=0.6):
    """薄弱点去重：问法字符二元组重合度高的只保留第一条。"""
    def bg(t):
        t = "".join(ch for ch in t if ch.isalnum())
        return {t[i:i + 2] for i in range(len(t) - 1)}
    out, seen = [], []
    for w in items or []:
        b = bg((w.get("q") or "") + (w.get("gap") or ""))
        if any(b and len(b & o) / max(1, min(len(b), len(o))) >= thr for o in seen):
            continue
        seen.append(b); out.append(w)
    return out


def render(d, doc, scen):
    def lab(ks):
        return "、".join("%s %s" % (k.split("-S")[-1], doc.sections[k]["title"][:24]) for k in ks if k in doc.sections) or "—"
    pf = d.get("profile") or {}
    L = ["# 大纲骨架与追问清单%s" % ("（兜底结果）" if d.get("fallback") else ""), "",
         "- 文档是什么：%s" % pf.get("doc_kind", ""), "- 一句话核心：%s" % pf.get("core", ""),
         "- 听众拿什么衡量：%s" % "；".join(pf.get("judged_by") or []), "",
         "## 内容骨架（按“%s”取）" % scen.get("skeleton_by", ""), "",
         "| # | 单元 | 实现章节 | 概要章节 | 指标章节 | 占正文 |", "|---|---|---|---|---|---|"]
    for i, u in enumerate(d.get("units") or [], 1):
        L.append("| %d | %s | %s | %s | %s | %.0f%% |" % (i, u["name"], lab(u["sections"]), lab(u.get("overview_sections") or []),
                                                     lab(u.get("metric_sections") or []), (u.get("share") or 0) * 100))
    if d.get("shell"):
        L += ["", "## 外壳模块", "", "| 模块 | 原文章节 | 说明 |", "|---|---|---|"]
        for m in d["shell"]:
            L.append("| %s | %s | %s |" % (m.get("module"), lab(m.get("sections") or []), m.get("note", "")))
    sp = d.get("special") or {}
    if sp:
        L += ["", "## 特殊章节", ""]
        for x in sp.get("overview_impl") or []:
            L.append("- 概要 + 实现：%s ↔ %s（%s）" % (x.get("overview"), x.get("impl"), x.get("reason", "")))
        if sp.get("metric_chapters"):
            L.append("- 指标章节（分散到对应单元，结尾汇总）：%s" % lab(_keys(doc, sp["metric_chapters"])[0]))
        for x in sp.get("low_info") or []:
            L.append("- 低信息，不单独成页：%s（%s）" % (x.get("section"), x.get("reason", "")))
        for x in sp.get("parallel") or []:
            L.append("- 平行章节：%s（%s）" % ("、".join(x.get("sections") or []), x.get("reason", "")))
    L += ["", "## 汇报要讲清的问题%s" % ("（已逐题核对原文）" if d.get("verified") else ""), "",
          "| # | 主/次 | 问题 | 原文能否回答 | 出处 | 原文怎么回答 |", "|---|---|---|---|---|---|"]
    for i, q in enumerate(d.get("questions") or [], 1):
        L.append("| %d | %s | %s | %s | %s | %s%s |" % (i, q.get("level"), q.get("q"), q.get("answerable"),
                                                      lab(q.get("answer_sections") or []), q.get("basis", ""),
                                                      ("（缺：%s）" % q["missing"]) if q.get("missing") else ""))
    wk = d.get("weak_points") or []
    if wk:
        L += ["", "## 薄弱点（原文没讲清，不上屏，供汇报人准备答疑）", "", "| # | 专家可能怎么问 | 原文缺什么 |", "|---|---|---|"]
        L += ["| %d | %s | %s |" % (i, w.get("q", ""), w.get("gap", "")) for i, w in enumerate(wk, 1)]
    return "\n".join(L)
