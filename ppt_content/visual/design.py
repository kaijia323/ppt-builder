"""第⑧步：视觉设计。模型只做选择，代码负责绘制。

1. 整套规划（design_deck，一次调用）：每个内容页归入目录的哪一章、页眉短主题、首选与备选版式。
   确定性校验：章序号单调、每章有页、主题长度、版式存在、相邻页不同版式。
2. 逐页填槽（design_page，并行）：把本页内容压缩成版式槽位里的短语。
   确定性校验（不合格带问题清单重写）：
     - 槽位结构、条目数量、每个槽位的字数上限；
     - 溯源：每段文字与本页内容（标题、要点、备注）的字词重合度，数字必须出现在本页内容中，【】关键词必须原样出现；
     - 试排：用渲染引擎实际排一次，有文字放不下就要求压缩。
   首选版式重试仍不合格 → 换备选版式 → 仍不合格则用确定性兜底版式（L39 结论块 + 编号要点）。
3. 检查点 3 的修改意见（visual_answers.json）：只重做用户点名的页（指定版式或给出意见），可整体换配色。
"""
import re

from ..checks import bigrams, norm, norm_loose, numbers_not_in
from ..core import load_scenario, read_json, write_json
from . import engine
from .layouts import IMAGE_LAYOUTS, LAYOUTS, brief_menu, menu, validate
from . import knobs
from .prim import THEMES, em_width

CONFIRM_RE = re.compile(r"(待|需|请).{0,12}(确认|明确|决定)|确认事项|待定事项")
HINT_LAYOUT = [("边界|分包|接口|对接", "L53"), ("状态|流转|退回|驳回", "L54"), ("原因|成因|根因", "L55"),
               ("分类|目录树|树状|体系树", "L56"), ("编码|位段|字段结构", "L27"), ("甘特", "L30"), ("拓扑|网闸|区域部署", "L21"), ("金字塔", "L19"),
               ("架构|分层|层级", "L17"), ("流程|步骤|路线|泳道", "L13"), ("时间轴|计划|里程碑", "L29"),
               ("对比|对照", "L33"), ("表格|清单", "L42"), ("数字|指标卡|KPI", "L22"), ("循环|闭环", "L44"),
               ("辐射|中心", "L45"), ("矩阵", "L46")]


# ------------------------------------------------------------------ 上下文
def load(wd):
    content = read_json(wd.stage("export", "content.json"))
    brief = wd.brief()
    scen = load_scenario(brief["scenario"])
    pages = [p for p in content["pages"] if p.get("kind") == "content"]
    for i, p in enumerate(pages, 1):
        p["_no"] = i
        p["_key"] = p.get("page_key") or "P%02d" % i
        for f in p.get("figures") or []:
            f["path"] = wd.p(*f["file"].split("/"))
    for p in pages:
        p["_figre"] = scen.get("figure_captions")
    synth_titles = [c.get("title") for c in content.get("meta", {}).get("chapters") or [] if c.get("synthesis")]
    for p in pages:
        p["_synth"] = bool(synth_titles) and p.get("chapter") is not None and \
            0 < p["chapter"] <= len(content["meta"]["chapters"]) and content["meta"]["chapters"][p["chapter"] - 1].get("synthesis")
    agenda = next((p for p in content["pages"] if p.get("role") == "agenda"), None)
    chapters = [b["text"] for b in agenda["bullets"]] if agenda else []
    # 0.6：规划已定章（每页带 chapter）时直接沿用，不再让模型重新分章；检查点 2 删页后空出的章去掉并重新编号
    if pages and all(isinstance(p.get("chapter"), int) and p["chapter"] >= 1 for p in pages) and chapters:
        # 检查点 2 中被挪动位置的页：前后两页同属一章时，随前后页归章
        seq = [p["chapter"] for p in pages]
        for i in range(1, len(seq) - 1):
            if seq[i] != seq[i - 1] and seq[i + 1] == seq[i - 1]:
                pages[i]["chapter"] = seq[i] = seq[i - 1]
        if any(seq[i] < seq[i - 1] for i in range(1, len(seq))):
            return content, brief, scen, pages, chapters       # 顺序被大幅调整：不沿用规划的章，交给版式规划重新分章
        used = []
        for p in pages:
            if p["chapter"] not in used:
                used.append(p["chapter"])
        remap = {old: i for i, old in enumerate(used, 1)}
        chapters = [chapters[o - 1] if o - 1 < len(chapters) else "第%d章" % o for o in used]
        for p in pages:
            p["_chapter"] = remap[p["chapter"]]
    return content, brief, scen, pages, chapters


def fixed_chapters(pages):
    return bool(pages) and all(p.get("_chapter") for p in pages)


def page_text(p):
    return "\n".join([p.get("title", "")] + [b["text"] for b in p.get("bullets", [])] +
                     [f["text"] for f in p.get("facts", []) or []] + [p.get("notes", "")])


def short_topic(title, n=12):
    t = re.split(r"[，,：:；;。]", title)[0]
    return t[:n]


# ------------------------------------------------------------------ 1. 整套规划
def deck_vars(content, pages, chapters, scen):
    rows = []
    for p in pages:
        vh = p.get("visual_hint") or {}
        fg = p.get("_fig")
        rows.append("第%d页%s｜%s\n  要点：%s\n  图示建议：%s %s%s" % (
            p["_no"], ("（第%d章，已定）" % p["_chapter"]) if p.get("_chapter") else "", p["title"],
            "；".join(b["text"] for b in p.get("bullets", [])), vh.get("type", ""), (vh.get("description") or "")[:160],
            ("\n  原文图片：%s（宽高比 %.2f）" % (fg.get("caption") or "未命名图片", fg.get("aspect") or 1.3)) if fg else ""))
    return {"title": content["meta"].get("title", ""), "scenario": scen.get("display_name", ""),
            "agenda": "\n".join("%d. %s" % (i, c) for i, c in enumerate(chapters, 1)) or "（无目录，chapter 一律填 1）",
            "pages": "\n".join(rows), "menu": brief_menu(exclude=() if scen.get("confirm_page", True) else ("L40",)),
            "layout_cap": layout_cap(len(pages)), "family_cap": family_cap(len(pages))}


# 外观相近的版式归为一类，整套合计次数受限（0.7）
FAMILIES = {"并列卡片": ("L05", "L06", "L09"), "横向流程": ("L12", "L13", "L15")}


SOFT_DECK = re.compile(r"最多 \d+ 次|最多 \d+ 页")


def layout_cap(n):
    return max(2, n // 9 + 1)


def family_cap(n):
    return max(4, -(-n // 4))


def deck_issues(d, pages, chapters, relax=0):
    iss = []
    got = {x.get("no"): x for x in d.get("pages", []) if isinstance(x, dict)}
    nch = max(1, len(chapters))
    prev_ch, prev_lay = 1, None
    used = set()
    for p in pages:
        x = got.get(p["_no"])
        if not x:
            iss.append("缺少第%d页" % p["_no"])
            continue
        if p.get("_chapter"):
            x["chapter"] = p["_chapter"]           # 规划已定章，以规划为准
        ch = x.get("chapter")
        if not isinstance(ch, int) or ch < 1 or ch > nch:
            iss.append("第%d页 chapter=%s 超出 1–%d" % (p["_no"], ch, nch))
        elif ch < prev_ch:
            iss.append("第%d页 chapter=%d 小于上一页的 %d（章序号只能不变或递增）" % (p["_no"], ch, prev_ch))
        else:
            prev_ch = ch
            used.add(ch)
        t = x.get("topic", "")
        if not t or em_width(t) > 13 or re.search(r"[，。；：！？,;:]", t):
            iss.append("第%d页 topic“%s”应为 4–12 字、无标点的名词短语" % (p["_no"], t))
        lay = x.get("layout")
        if lay not in LAYOUTS:
            iss.append("第%d页 layout=%s 不存在" % (p["_no"], lay))
        elif lay == prev_lay:
            iss.append("第%d页与上一页都用 %s，相邻页请换一个版式" % (p["_no"], lay))
        if lay in IMAGE_LAYOUTS and not default_image_figure(p):
            iss.append("第%d页不用原图版式 %s（原图版式由程序按图题分配，页面标“原文图片”且为总体/架构类图时才用）" % (p["_no"], lay))
        if lay == "L40" and not CONFIRM_RE.search(page_text(p)):
            iss.append("第%d页没有待确认类内容，不能用 L40" % p["_no"])
        if x.get("alt") in IMAGE_LAYOUTS and not default_image_figure(p):
            x["alt"] = None                         # 原图版式只由程序分配，模型不能把它填作备选
        if x.get("alt") and x["alt"] not in LAYOUTS:
            iss.append("第%d页 alt=%s 不存在" % (p["_no"], x["alt"]))
        prev_lay = lay
    # 同一版式不宜反复使用；分层架构只给真正的架构
    cnt = {}
    for x in got.values():
        cnt[x.get("layout")] = cnt.get(x.get("layout"), 0) + 1
    cap = layout_cap(len(pages)) + relax          # relax：被打回后逐次放宽重复上限（见 run）
    for lay, c in cnt.items():
        if lay not in ("L39",) and c > cap:
            iss.append("版式 %s 用了 %d 次，同一版式最多 %d 次，请为部分页面换用更贴合的版式" % (lay, c, cap))
    # 同类计数（0.7）：L05/L06 各用 4 次不违规，但听众看到的是 8 页卡片
    fcap = family_cap(len(pages)) + 2 * relax
    for fam, ids in FAMILIES.items():
        c = sum(cnt.get(x, 0) for x in ids)
        if c > fcap:
            iss.append("%s类版式（%s）合计用了 %d 次，最多 %d 次：其中一部分页面的内容如果是边界/接口、状态流转、问题—原因—对策、"
                       "分类树、多项并列（6–9 项）、对比、量化等关系，请换用对应版式" % (fam, "/".join(ids), c, fcap))
    # 备选也要守上限（0.7.0-dev8）：首选没通过时会换成备选，备选若是已用满的版式，实际次数就超了
    #（实测需求分析：L06 首选 3 次已满，又有 2 页以 L06 作备选落地，成品 5 页 L06）
    full = {lay for lay, c in cnt.items() if lay not in ("L39",) and c >= cap}
    full |= {x for fam, ids in FAMILIES.items() if sum(cnt.get(i, 0) for i in ids) >= fcap for x in ids}
    bad_alt = [x for x in got.values() if x.get("alt") in full and x.get("alt") != x.get("layout")]
    if bad_alt:
        iss.append("第 %s 页的备选版式（%s）已经用满（同一版式最多 %d 次），首选没通过时就会超出：请换一个不同的备选" % (
            "、".join(str(x.get("no")) for x in bad_alt), "、".join(sorted({x["alt"] for x in bad_alt})), cap))
    nc = sum(cnt.get(x, 0) for x in ("L60", "L61", "L62"))
    ccap = max(2, len(pages) // 3) + relax
    if nc > ccap:
        iss.append("分区组合版式（L60/L61/L62）用了 %d 页，最多 %d 页：只有一类信息的页面请换用单一结构版式" % (nc, ccap))
    for p in pages:
        x = got.get(p["_no"]) or {}
        if x.get("layout") in ("L17", "L20") and not re.search(r"架构|分层|层级|平台|底座|体系|[一二三四五六七八九两\d]层", page_text(p)):
            iss.append("第%d页内容不是分层架构或平台结构，不要用 %s" % (p["_no"], x["layout"]))
    if chapters:
        miss = [i for i in range(1, nch + 1) if i not in used]
        if miss:
            iss.append("第 %s 章没有任何页面，每章至少 1 页" % "、".join(map(str, miss)))
    return iss


def heuristic_plan(pages, chapters):
    """模型规划失败时的确定性兜底：按顺序平均分章，按图示建议选版式。"""
    out = []
    nch = max(1, len(chapters))
    n = len(pages)
    prev = None
    for i, p in enumerate(pages):
        ch = p.get("_chapter") or min(nch, 1 + i * nch // max(1, n))
        vh = (p.get("visual_hint") or {}).get("type", "") + (p.get("visual_hint") or {}).get("description", "")
        lay = next((l for kw, l in HINT_LAYOUT if re.search(kw, vh)), "L06" if len(p.get("bullets", [])) >= 4 else "L39")
        if lay == prev:
            lay = "L39" if lay != "L39" else "L06"
        prev = lay
        out.append({"no": p["_no"], "chapter": ch, "topic": short_topic(p["title"]), "layout": lay, "alt": "L39",
                    "relation": "（兜底规划）"})
    return {"pages": out}


# ------------------------------------------------------------------ 2. 逐页填槽
SKIP_KEYS = {"tone", "status", "_img", "_k"}                 # 枚举值，不是内容
LABEL_KEYS = {"title", "label", "name", "dim", "left_label", "right_label", "center", "center_sub",
              "context_label", "problems_label", "goals_label", "before_label", "after_label", "unit", "head",
              "subtitle"}


def walk_strings(o, path="spec", key=None):
    if isinstance(o, str):
        yield path, o, key
    elif isinstance(o, dict):
        for k, v in o.items():
            if k in SKIP_KEYS:
                continue
            yield from walk_strings(v, path + "." + k, k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk_strings(v, "%s[%d]" % (path, i), key)


META_INTRO = re.compile(r"^(本页|这一页|此页|下面|以下)")


def provenance_issues(spec, intro, src, title):
    """溯源检查。分三类槽位：
    - 标签类（面板标题、层名、维度名、单位等，起组织作用）：数字须有出处，用字一半以上见于本页内容即可；
    - 导语：允许连接词，字词重合度 ≥ 45%；
    - 事实类（说明、条目、措施、问答等）：字词重合度 ≥ 50%，数字须有出处。"""
    iss = []
    src_n = norm_loose(src)
    src_bg = bigrams(src)
    items = list(walk_strings(spec)) + ([("intro", intro, "intro")] if intro else [])
    for path, s, key in items:
        clean = s.replace("【", "").replace("】", "")
        if not clean.strip():
            continue
        miss = numbers_not_in(clean, [src])
        if miss:
            iss.append("%s 中的数字 %s 不在本页内容中：“%s”" % (path, miss, s))
        c = norm_loose(clean)
        if key in LABEL_KEYS or len(c) <= 4:
            bad = [ch for ch in c if ch not in src_n]
            if bad and len(bad) > len(c) / 2:
                iss.append("%s“%s”在本页内容中找不到依据" % (path, s))
            continue
        bg = bigrams(clean)
        cov = len(bg & src_bg) / float(len(bg) or 1)
        need = 0.45 if key == "intro" else 0.5
        if cov < need:
            iss.append("%s“%s”与本页内容重合太少（%.0f%%），只能压缩本页已有的表述，不能新写" % (path, s, cov * 100))
    ft = (spec.get("footer") or {}).get("text") if isinstance(spec.get("footer"), dict) else None
    if ft:
        A, Bg = bigrams(ft), bigrams(title)
        if len(A & Bg) / float(min(len(A), len(Bg)) or 1) >= 0.6:
            iss.append("footer 与底部结论横幅重复，页脚条应补充横幅没写的信息（或省略 footer）")
    if intro:
        if META_INTRO.match(intro):
            iss.append("intro 不要用“本页……”这类说明页面的话开头，直接陈述内容")
        if em_width(intro.replace("【", "").replace("】", "")) > 62:
            iss.append("intro 太长，限 60 字以内")
        if norm(intro.replace("【", "").replace("】", "")) == norm(title):
            iss.append("intro 不要照抄结论标题")
    return iss


def repair_intro(intro, src):
    """确定性修正（不打回重写）：【】里的词不在本页内容中就去掉括号；超过 3 处只保留前 3 处。"""
    src_n = norm_loose(src)
    k = [0]

    def rep(m):
        w = m.group(1)
        if norm_loose(w) not in src_n or k[0] >= 3:
            return w
        k[0] += 1
        return m.group(0)
    return re.sub(r"【([^】]*)】", rep, intro or "")


NUM_HEAD = re.compile(r"^\s*([≥≤<>约~＋+]?\s*[\d.,]+\s*[%‰]?\+?)\s*(\S.*)$")


def repair_spec(lid, spec):
    """确定性修正：大数字槽位里混入的量词/单位移到 unit（"7个" → value "7"，unit "个智能体"）。"""
    def fix(o):
        m = NUM_HEAD.match(o.get("value", "") or "")
        if m and not re.fullmatch(r"[\d.,%‰]+", m.group(2)):
            o["value"] = m.group(1).strip()
            u = o.get("unit", "")
            o["unit"] = u if u.startswith(m.group(2)) else m.group(2) + u
    if lid == "L22":
        for k in spec.get("kpis", []):
            fix(k)
    elif lid == "L48":
        fix(spec)
    return spec


def unsuitable(lid, spec):
    """填完后发现内容不适合该版式（不打回重写，直接换备选版式）。"""
    if lid == "L42" and len(spec.get("columns", [])) <= 2 and len(spec.get("rows", [])) <= 8:
        return "两列、不超过 8 行的短对照用表格版面单薄"
    if lid == "L42" and len(spec.get("rows", [])) <= 3:
        return "表格只有 %d 行，占一整页会空出半页" % len(spec.get("rows", []))
    if lid == "L42":
        # 各列是互不相关的清单、同一行没有对应关系时，会出现成片的占位“—”（实测：重难点页三列各列一组事实）
        cells = [str(c).strip() for r in spec.get("rows") or [] for c in (r if isinstance(r, list) else r.get("cells") or [])]
        if cells and sum(1 for c in cells if c in ("—", "-", "－", "")) >= 2:
            return "表格各列是互不对应的清单（出现空格/占位“—”），不是真正的行列数据"
    return None


def content_coverage(spec, intro, p):
    """版面用上了本页多少条内容（要点 + 屏上细节）：按二元组重合判断一条内容是否出现在某个槽位中。"""
    items = [b["text"] for b in p.get("bullets", [])] + [f["text"] for f in p.get("facts", []) or []]
    strs = [t for _, t, _ in walk_strings(spec)] + [intro or ""]
    pool = [bigrams(t) for t in strs if t]
    hit = 0
    for it in items:
        A = bigrams(it)
        if A and any(len(A & B) / float(len(A)) >= 0.45 for B in pool):
            hit += 1
            continue
        # 一条内容被拆进多个小槽位时，按整页字符串合并后判断
        allb = set().union(*pool) if pool else set()
        if A and len(A & allb) / float(len(A)) >= 0.7:
            hit += 1
    return hit, len(items)


COMBO_ZONES = {"L60": ("flow", "cards"), "L61": ("pairs", "kpis"), "L62": ("steps", "panel")}


def layout_rules(lid, spec, src):
    """个别版式的使用前提（确定性）。"""
    iss = []

    if lid in ("L22", "L48"):
        vals = [k.get("value", "") for k in spec.get("kpis", [])] if lid == "L22" else [spec.get("value", "")]
        for v in vals:
            if not re.search(r"\d", v or ""):
                iss.append("%s 的 value“%s”必须是数字（如 7、≥95%%），不是数字的内容请放到 unit 或换版式" % (lid, v))
    if lid == "L30":
        per = spec.get("periods") or []
        for r in spec.get("rows") or []:
            for k in ("start", "end"):
                v = (r.get(k) or "").strip()
                if v and not any(v == p or v in p or p in v for p in per):
                    iss.append("L30 第“%s”行的 %s=“%s”不在 periods 中，请填 periods 里的某个时段" % (r.get("name"), k, v))
    if lid in COMBO_ZONES:
        # 分区组合：两区讲的是两类不同信息，不能一区复述另一区（实测风险：卡片区把流程步骤换个说法再列一遍）
        za, zb = COMBO_ZONES[lid]
        A = bigrams(" ".join(t for _, t, _ in walk_strings(spec.get(za))))
        Bz = bigrams(" ".join(t for _, t, _ in walk_strings(spec.get(zb))))
        if A and Bz and len(A & Bz) / float(min(len(A), len(Bz))) >= 0.45:
            iss.append("%s 的两个分区内容重复（%s 与 %s 重合太多）：两区应是两类不同信息，内容只有一类时请换用单一结构的版式"
                       % (lid, za, zb))
    if lid == "L61":
        for k in spec.get("kpis") or []:
            if not re.search(r"\d", k.get("value", "") or ""):
                iss.append("L61 的 value“%s”必须是数字" % k.get("value"))
    if lid == "L54":
        names = [x.get("name", "") for x in spec.get("states") or []]
        for r in spec.get("returns") or []:
            for f in ("from", "to"):
                v = (r.get(f) or "").strip()
                if v and not any(v == n or v in n or n in v for n in names):
                    iss.append("L54 returns 的 %s=“%s”不是 states 中的状态名" % (f, v))
    if lid == "L40" and not CONFIRM_RE.search(src) and not re.search(r"待确认|待明确|待补充|需补充|未给出|未明确", src):
        iss.append("本页内容没有待确认/待补充事项，不能用 L40")
    return iss


def trial_issues(design, pg):
    d, _ = engine.build(dict(design, pages=[pg]), only={pg["key"]})
    out = []
    for q in d.qa:
        if q["kind"] == "text_overflow":
            out.append("版面放不下：“%s”（%s），请把这处文字压缩约三分之一" % (q["text"], q["detail"]))
        elif q["kind"] == "out_of_slide":
            out.append("版式绘制越界（%s），请减少条目数量" % q["detail"])
    return out, d.qa


# 系统级示意图（总体技术路线、架构、部署、拓扑）默认用原图：原图最权威，模型看不到图、按文字重绘可能与原图不一致
SYSTEM_FIG = re.compile(r"总体|技术路线|架构|部署|拓扑|框架|体系结构|总体设计|系统组成")


def image_layout_for(fig):
    return "L51" if float(fig.get("aspect") or 1.3) >= 1.8 else "L50"


def default_image_figure(p):
    """本页默认放原图的那张图（没有则 None）：分配给本页的图，且是系统级示意图。"""
    f = p.get("_fig")
    if not f or p.get("_synth"):                # 跨章归纳页（重难点/创新点）不默认放某一节的原图
        return None
    if SYSTEM_FIG.search(f.get("caption") or "") and f.get("score", 0) >= 5:
        return f
    fre = p.get("_figre")                       # 场景指定的默认放原图的图题（如系统操作培训的界面截图）
    return f if fre and re.search(fre, f.get("caption") or "") and f.get("score", 0) >= 2 else None


def assign_figures(pages):
    """每张原图只分给最相关的一页（实测：总体技术路线图因数字补引被三页同时引用，三页都放了同一张图）。"""
    cands = []
    for p in pages:
        for f in p.get("figures") or []:
            cands.append((f.get("score", 0), -p["_no"], p, f))
    cands.sort(key=lambda x: (-x[0], -x[1]))
    used = set()
    for sc, _, p, f in cands:
        if p.get("_fig") is not None or f.get("block_id") in used or sc < 1:
            continue
        p["_fig"] = f
        used.add(f.get("block_id"))


def fill_vars(p, entry, lid, chapter_name, feedback):
    L_ = LAYOUTS[lid]
    vh = p.get("visual_hint") or {}
    return {"page_no": p["_no"], "topic": entry["topic"], "chapter": chapter_name, "title": p["title"],
            "bullets": "\n".join("- " + b["text"] for b in p.get("bullets", [])) +
                       ("\n屏上细节（可直接用于图示，优先上屏）：\n" + "\n".join("- " + f["text"] for f in p["facts"])
                        if p.get("facts") else ""),
            "notes": (p.get("notes") or "")[:1500], "visual_hint": "%s %s" % (vh.get("type", ""), vh.get("description", "")),
            "layout_spec": menu([lid]), "layout_id": lid, "feedback": feedback or ""}


def auto_points(p):
    """确定性兜底：L39，要点按“：”拆成小标题与说明。"""
    items = []
    for b in p.get("bullets", [])[:6]:
        t = b["text"]
        m = re.match(r"^(.{2,10})[：:](.+)$", t)
        items.append({"head": m.group(1), "body": m.group(2)} if m else {"body": t})
    while len(items) < 2:
        items.append({"body": p["title"]})
    return {"lead": p["title"], "items": items}


def fill_page(llm, log, base_design, p, entry, chapters, forced=None, user_feedback=None):
    chap_name = chapters[entry["chapter"] - 1] if chapters and 1 <= entry["chapter"] <= len(chapters) else ""
    src = page_text(p)
    tried = []
    candidates = [c for c in ([forced] if forced else [entry["layout"], entry.get("alt")]) if c in LAYOUTS]
    for lid in [c for c in candidates if c and c in LAYOUTS]:
        if lid in tried:
            continue
        tried.append(lid)
        pg = {"key": p["_key"], "chapter": entry["chapter"], "number": entry["number"], "topic": entry["topic"],
              "layout": lid, "banner": p["title"], "notes": p.get("notes", "")}
        holder = {}

        def validator(data, lid=lid, pg=pg, holder=holder):
            if data.get("layout") not in (None, lid):
                data["layout"] = lid
            iss, spec = validate(data.get("spec") or {}, LAYOUTS[lid]["schema"])
            spec = repair_spec(lid, spec)
            if lid in IMAGE_LAYOUTS:
                fig = p.get("_fig") or ((p.get("figures") or [None])[0] if forced else None)
                if fig:
                    spec["_img"] = {k: fig.get(k) for k in ("path", "fmt", "aspect", "px", "caption", "block_id")}
                    spec.pop("caption", None)          # 图题用原文图题，由程序填写
                else:
                    iss.append("本页没有分配到原文图片，不能用 %s" % lid)
            intro = repair_intro((data.get("intro") or "").strip(), src)
            iss += provenance_issues(spec, intro, src, p["title"])
            if lid in IMAGE_LAYOUTS and spec.get("_img", {}).get("caption"):
                spec["caption"] = spec["_img"]["caption"][:30]
            iss += layout_rules(lid, spec, src)
            # 版面过空：本页内容丰富（要点 + 屏上细节 ≥300 字），版面槽位却只放了不到 120 字（实测阶梯版式只放了 3 条短句）
            have = sum(len(b["text"]) for b in p.get("bullets", [])) + sum(len(f["text"]) for f in p.get("facts", []) or [])
            used_chars = sum(len(t) for _, t, _ in walk_strings(spec))
            if have >= 300 and used_chars < 120 and lid not in IMAGE_LAYOUTS:
                hit, tot = content_coverage(spec, intro, p)
                iss.append("版面只放了约 %d 字、用上本页 %d/%d 条内容，过于单薄：把本页的具体事实（数字、组成、步骤）放进槽位；"
                           "这个版式放不下时会改用备选版式" % (used_chars, hit, tot))
            if not iss:
                # 外观开关（0.7）：程序按内容决定；带开关放不下时去掉不等宽/竖排再试，仍放不下才打回
                kn = knobs.decide(lid, spec, p["title"] + entry["topic"], entry.get("_occ", 0))
                if kn:
                    spec["_k"] = kn
                pg2 = dict(pg, spec=spec, intro=intro)
                t_iss, qa = trial_issues(base_design, pg2)
                if t_iss and kn and ("w" in kn or "o" in kn):
                    kn2 = {k: v for k, v in kn.items() if k not in ("w", "o")}
                    spec2 = dict(spec)
                    spec2.pop("_k", None)
                    if kn2:
                        spec2["_k"] = kn2
                    pg3 = dict(pg, spec=spec2, intro=intro)
                    t2, qa2 = trial_issues(base_design, pg3)
                    if not t2:
                        pg2, t_iss, qa = pg3, t2, qa2
                iss += t_iss
                holder["pg"], holder["qa"] = pg2, qa
            return iss
        fb = ("用户对本页版面的意见（必须落实）：" + user_feedback) if user_feedback else ""
        r = llm.call_json("design_page", fill_vars(p, entry, lid, chap_name, fb), target=p["_key"] + "_" + lid,
                          validator=validator)
        if r["ok"] and holder.get("pg") and unsuitable(lid, holder["pg"]["spec"]) and lid != candidates[-1]:
            log.warn("design_switch", "%s 版式 %s：%s，改用备选" % (p["_key"], lid, unsuitable(lid, holder["pg"]["spec"])))
            continue
        if r["ok"] and holder.get("pg"):
            out = holder["pg"]
            out.update(status="ok" if lid == candidates[0] else "alt", issues=[], tried=tried)
            return out
        log.warn("design_page_failed", "%s 版式 %s 未通过：%s" % (p["_key"], lid, "；".join(r["issues"][:3])),
                 page=p["_key"], layout=lid, issues=r["issues"][:10])
    # 兜底
    spec = auto_points(p)
    iss, spec = validate(spec, LAYOUTS["L39"]["schema"])
    pg = {"key": p["_key"], "chapter": entry["chapter"], "number": entry["number"], "topic": entry["topic"],
          "layout": "L39", "banner": p["title"], "notes": p.get("notes", ""), "intro": "", "spec": spec,
          "status": "fallback", "issues": iss, "tried": tried}
    log.warn("design_fallback", "%s 使用兜底版式 L39" % p["_key"], page=p["_key"])
    return pg


# ------------------------------------------------------------------ 编号、封面、结束页
def number_pages(entries, chapters):
    cnt = {}
    for e in entries:
        cnt[e["chapter"]] = cnt.get(e["chapter"], 0) + 1
    seen = {}
    for e in entries:
        ch = e["chapter"]
        seen[ch] = seen.get(ch, 0) + 1
        if not chapters:
            e["number"] = str(e["no"])
        elif cnt[ch] == 1:
            e["number"] = str(ch)
        else:
            e["number"] = "%d.%d" % (ch, seen[ch])


def frame_design(content, brief, scen, chapters, cfg):
    vc = cfg.get("visual", {})
    meta = content["meta"]
    cv = meta.get("cover") or {}
    closing_pg = next((p for p in content["pages"] if p.get("role") == "closing"), None)
    vals = dict(cv.get("fields") or {})
    vals.update(brief.get("cover_fields") or {})
    ph = vc.get("cover_placeholder", "（待补充）")
    # 原文封面用的叫法不同（申报单位/实施单位等）时按同义项取值，落款用原文叫法
    syn = {"建设单位": ["建设单位", "申报单位", "委托单位", "依托部门", "业主单位"],
           "承建单位": ["承建单位", "实施单位", "协作单位"]}
    fields = []
    for k in vc.get("cover_fields", []):
        if vals.get(k):
            fields.append([k, vals[k]])
            continue
        alt = next((a for a in syn.get(k, []) if vals.get(a)), None)
        fields.append([alt, vals[alt]] if alt else [k, ph])
    kind = cv.get("doc_kind")
    sub = brief.get("subtitle") or ((kind + "汇报") if kind else meta.get("doc_type"))
    real = [[k, v] for k, v in fields if v != ph and k != "日期"]
    return {"theme": brief.get("theme") or vc.get("theme", "blue"),
            "cover": {"title": brief.get("title") or meta.get("title", ""), "subtitle": sub,
                      "tag": vc.get("cover_tag") or scen.get("display_name"), "fields": fields},
            "chapters": chapters, "toc": bool(chapters),
            "section_pages": bool(vc.get("section_pages", True) and chapters),
            "section_min_pages": int(vc.get("section_min_pages", 2)),
            "section_style": vc.get("section_style", "hero"),
            "banner": bool(vc.get("banner", False)),          # 底部结论横幅（0.7.0-dev23 起默认不放）
            "closing": None if closing_pg is None else {"head": vc.get("closing_head", "汇报完毕"),
                                                       "sub": closing_pg.get("title"), "fields": real}}


# ------------------------------------------------------------------ 套用户模板（0.7.0-dev9）
def template_info(wd, brief, log):
    """按 brief 的 template / logo 取主色、字体、Logo；结果缓存在 08_visual/template/，模板或 Logo 文件变了才重取。"""
    import hashlib
    import os
    from . import template
    tp, lp = brief.get("template"), brief.get("logo")
    if not tp and not lp:
        return None
    sig = []
    for f in (tp, lp):
        sig.append(hashlib.sha1(open(f, "rb").read()).hexdigest() if f and os.path.exists(f) else str(f))
    out_dir = wd.stage("design", "template")
    cache = read_json(os.path.join(out_dir, "template.json"))
    if cache and cache.get("sig") == sig:
        return cache
    info = template.extract(tp, out_dir, lp)
    info["sig"] = sig
    info["file"] = os.path.basename(tp) if tp else None
    write_json(os.path.join(out_dir, "template.json"), info)
    log.info("template_extracted", "模板：主色 %s（%s），字体 %s，Logo %s" % (
        info.get("primary"), info.get("color_source"), info.get("font"),
        (info.get("logo") or {}).get("source") or "无"), notes=info.get("notes"))
    return info


def apply_template(wd, base, brief, revise, prev, log):
    """配色/字体/Logo 的取值顺序：成品确认时的新意见 → 上一版成品的设置 → 运行参数（brief）。
    revise.theme：内置色调（blue/teal/red/slate/green/purple/amber 或中文名）或 template（模板配色）；revise.logo：cover / all / none。"""
    info = template_info(wd, brief, log)
    rv = revise or {}
    from .prim import theme_key
    theme = theme_key(rv.get("theme")) if rv.get("theme") else None     # “中国红”“red”都可以（0.7.0-dev15）
    base["theme"] = theme_key(base.get("theme")) or "blue"
    if theme not in list(THEMES) + ["template"]:
        theme = (prev or {}).get("theme") if prev else None
    if theme is None:
        theme = "template" if info and info.get("palette") else base["theme"]
    if theme == "template" and info and info.get("palette"):
        base["theme"], base["palette"] = "template", info["palette"]
    elif theme in THEMES:
        base["theme"] = theme
    if info:
        base["font"] = info.get("font")
        pos = rv.get("logo") if rv.get("logo") in ("cover", "all", "none") else (
            ((prev or {}).get("logo") or {}).get("pos") if prev and (prev or {}).get("logo") else brief.get("logo_pos") or "cover")
        if info.get("logo"):
            base["logo"] = dict(info["logo"], pos=pos)
        base["template_info"] = {k: info.get(k) for k in ("file", "primary", "color_source", "font", "notes")}
        base["template_info"].update(logo_source=(info.get("logo") or {}).get("source"), logo_pos=pos if info.get("logo") else None,
                                     applied=base["theme"] == "template")


# ------------------------------------------------------------------ 主流程
def run_speech(wd, cfg, log, llm):
    """只重写讲稿（不重排版式）：python run.py speech。"""
    from . import speech
    content, brief, scen, pages, chapters = load(wd)
    out_path = wd.stage("design", "design.json")
    design = read_json(out_path)
    sp = speech.run(llm, log, design, content, scen, cfg)
    write_json(out_path, design)
    return sp


def edit_page_content(llm, log, p, instruction):
    """成品确认（0.7）：按用户意见改一页的标题、要点和屏上细节（就地修改 p）。原有条目保留出处；新增条目标为“用户修改，未核验”。"""
    from ..checks import numbers_not_in
    quotes = [pv.get("quote", "") for it in (p.get("bullets") or []) + (p.get("facts") or []) for pv in it.get("provenance") or []]
    src = page_text(p) + "\n" + "\n".join(dict.fromkeys(q for q in quotes if q))
    olds = {b["id"]: b for b in p.get("bullets") or []}
    oldf = {f["id"]: f for f in p.get("facts") or []}

    def val(d):
        iss = []
        if not (d.get("title") or "").strip():
            iss.append("title 不能为空")
        allt = [d.get("title", "")] + [x.get("text", "") for x in (d.get("bullets") or []) + (d.get("facts") or []) if isinstance(x, dict)]
        miss = numbers_not_in(" ".join(allt), [src, instruction, p.get("title", "")])
        if miss:
            iss.append("数字 %s 既不在原文依据中，也不在用户意见中" % miss)
        return iss
    r = llm.call_json("page_edit", {"instruction": instruction, "title": p.get("title", ""),
                                    "bullets": "\n".join("[%s] %s" % (b["id"], b["text"]) for b in p.get("bullets") or []) or "（无）",
                                    "facts": "\n".join("[%s] %s" % (f["id"], f["text"]) for f in p.get("facts") or []) or "（无）",
                                    "source": src[:6000] or "（无）"}, target=p["_key"], validator=val)
    d = r.get("data") or {}
    if not r["ok"] or not d:
        log.warn("page_edit_failed", "%s 按意见修改内容未成功，保留原内容：%s" % (p["_key"], "；".join(r["issues"][:2])))
        return False

    def merge(items, old, kind):
        out = []
        for k, x in enumerate(items or [], 1):
            if not isinstance(x, dict) or not (x.get("text") or "").strip():
                continue
            o = old.get(x.get("from") or "")
            if o:
                ch = x["text"].strip() != o["text"]
                q = dict(o, text=x["text"].strip(), user_edited=bool(o.get("user_edited")) or ch)
                if ch and kind == "b":
                    q["status"] = dict(o.get("status") or {}, state="user_edited_unverified")
                out.append(q)
            else:
                out.append({"id": "%s-%s%d" % (p["_key"], "e" if kind == "b" else "g", k), "text": x["text"].strip(),
                            "user_edited": True, "user_added": True, "provenance": [],
                            "status": {"state": "user_added_unverified"}})
        return out
    p["title"] = d["title"].strip()
    p["bullets"] = merge(d.get("bullets"), olds, "b")
    if d.get("facts") is not None:
        p["facts"] = merge(d.get("facts"), oldf, "f")
    p["user_edited"] = True
    log.info("page_edited", "%s 已按用户意见修改内容" % p["_key"], instruction=instruction)
    return True


def run(wd, cfg, log, llm, revise=None):
    content, brief, scen, pages, chapters = load(wd)
    assign_figures(pages)
    out_path = wd.stage("design", "design.json")
    prev = read_json(out_path) if revise else None
    base = frame_design(content, brief, scen, chapters, cfg)
    apply_template(wd, base, brief, revise, prev, log)
    if prev and revise:
        plan = prev["_plan"]
    else:
        # 重复上限（单版式、同类、分区组合）是“软约束”：第 1 次严格，之后每被打回一次放宽一档；
        # 三次仍只差软约束时直接采用模型的规划（比确定性兜底规划好），只有硬性问题（缺页、章序、版式不存在等）才兜底
        tries = [0]

        def deck_validator(d):
            tries[0] += 1
            return deck_issues(d, pages, chapters, relax=tries[0] - 1)
        r = llm.call_json("design_deck", deck_vars(content, pages, chapters, scen), target="deck", validator=deck_validator)
        if tries[0] > 1 and r["ok"]:
            log.warn("design_cap_relaxed", "版式重复上限被打回 %d 次后放宽 %d 档通过" % (tries[0] - 1, tries[0] - 1),
                     relax=tries[0] - 1)
        if not r["ok"] and r.get("data") and r["issues"] and all(SOFT_DECK.search(x) for x in r["issues"]):
            log.warn("design_cap_relaxed", "版式规划只剩重复上限问题，采用模型规划：%s" % "；".join(r["issues"][:2]),
                     issues=r["issues"])
            r = dict(r, ok=True)
        if r["ok"]:
            plan = r["data"]
        else:
            log.warn("design_deck_fallback", "整套版式规划未通过校验，改用确定性规划：%s" % "；".join(r["issues"][:3]))
            plan = heuristic_plan(pages, chapters)
            if not chapters:
                for x in plan["pages"]:
                    x["chapter"] = 1
    entries = sorted([dict(x) for x in plan["pages"]], key=lambda x: x["no"])
    fixed = {p["_no"]: p["_chapter"] for p in pages if p.get("_chapter")}
    for e in entries:
        if e["no"] in fixed:
            e["chapter"] = fixed[e["no"]]
    number_pages(entries, chapters)
    by_no = {e["no"]: e for e in entries}
    if not (prev and revise):
        for p in pages:
            f = default_image_figure(p)
            e = by_no.get(p["_no"])
            if f and e and e.get("layout") not in IMAGE_LAYOUTS:
                e["alt"] = e.get("layout")          # 按文字重绘的版式作备选，检查点 3 可换回
                e["layout"] = image_layout_for(f)
                log.info("design_original_figure", "%s 使用原图“%s”（备选：%s）" % (p["_key"], f.get("caption"), e["alt"]))

    seen_lay = {}
    for e in entries:                          # 同一版式在整套中第几次出现（外观变体轮换用）
        e["_occ"] = seen_lay.get(e["layout"], 0)
        seen_lay[e["layout"]] = e["_occ"] + 1
    todo = pages
    forced, ufb = {}, {}
    if prev and revise:
        req = revise.get("pages") or {}
        keys = {}
        for k, v in req.items():
            no = int(re.sub(r"\D", "", str(k)) or 0)
            if no:
                keys[no] = v or {}
        todo = [p for p in pages if p["_no"] in keys]
        edited = False
        for p in todo:
            v = keys[p["_no"]]
            if (v.get("edit") or "").strip():          # 内容意见：先改内容，再重排这一页
                edited = edit_page_content(llm, log, p, v["edit"].strip()) or edited
            if v.get("layout") in LAYOUTS:
                forced[p["_no"]] = v["layout"]
            if v.get("feedback"):
                ufb[p["_no"]] = v["feedback"]
            if v.get("topic"):
                by_no[p["_no"]]["topic"] = v["topic"]
    if prev and revise and edited:
        # 改过的内容写回 content.json（导出稿与讲稿都以它为准）
        cp = {p["_key"]: p for p in pages}
        for q in content["pages"]:
            src = cp.get(q.get("page_key"))
            if src is not None and src.get("user_edited"):
                for k in ("title", "bullets", "facts", "user_edited"):
                    q[k] = src.get(k)
        write_json(wd.stage("export", "content.json"), content)
    res = llm.map(lambda p: fill_page(llm, log, base, p, by_no[p["_no"]], chapters, forced.get(p["_no"]),
                                      ufb.get(p["_no"])), todo, "design")
    filled = {}
    for p, x in zip(todo, res):
        if isinstance(x, Exception):
            raise x
        filled[p["_no"]] = x
    if prev and revise:
        old = {pg["no"]: pg for pg in prev["pages"]}
        for p in pages:
            if p["_no"] not in filled:
                filled[p["_no"]] = dict(old[p["_no"]], topic=by_no[p["_no"]]["topic"])
    out_pages = []
    for p in pages:
        pg = filled[p["_no"]]
        e = by_no[p["_no"]]
        pg.update(no=p["_no"], chapter=e["chapter"], number=e["number"], topic=e["topic"],
                  relation=e.get("relation", ""))
        out_pages.append(pg)
    # L39（含兜底页）按整套出现顺序轮换外观；换后试排放不下就保持原样
    k39 = 0
    for pg in out_pages:
        if pg.get("layout") != "L39":
            continue
        want = k39 % 2
        k39 += 1
        cur = (pg.get("spec") or {}).get("_k") or {}
        if cur.get("v", 0) == want:
            continue
        spec2 = dict(pg["spec"], _k=dict(cur, v=want))
        if not trial_issues(base, dict(pg, spec=spec2))[0]:
            pg["spec"] = spec2
    design = dict(base, pages=out_pages, _plan={"pages": entries})
    # 讲稿（0.6）：版式定稿后对着最终屏上文字生成；检查点 3 修改时只重写被修改的页
    from . import speech
    sp = speech.run(llm, log, design, content, scen, cfg, only={p["_key"] for p in todo} if (prev and revise) else None)
    if prev and revise:
        for pg in design["pages"]:
            if pg["key"] not in {p["_key"] for p in todo} and not pg.get("notes"):
                pg["notes"] = next((o.get("notes") for o in prev["pages"] if o["key"] == pg["key"]), "")
    write_json(out_path, design)
    st = {}
    for pg in out_pages:
        st[pg["status"]] = st.get(pg["status"], 0) + 1
    lay = {}
    for pg in out_pages:
        lay[pg["layout"]] = lay.get(pg["layout"], 0) + 1
    return {"pages": len(out_pages), "status": st, "layouts_used": len(lay), "layout_counts": lay, "speech": sp,
            "theme": design["theme"], "revised_pages": len(todo) if revise else None}
