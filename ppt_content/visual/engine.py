"""渲染引擎：把设计稿（design.json）画成 pptx。纯确定性，不调用模型。

设计稿结构：
{
  "theme": "blue",
  "cover": {"title", "subtitle", "tag", "fields": [[名称, 值], ...]},
  "chapters": ["背景与目标", ...],
  "pages": [{"key": "P01", "chapter": 1, "number": "1", "topic": "建设背景与目标", "layout": "L03",
             "intro": "...【关键词】...", "spec": {...}, "banner": "结论句", "notes": "讲稿"}],
  "closing": {"head", "sub", "fields"},
  "section_pages": true
}
"""
from . import structural
from .layouts import LAYOUTS
from .layouts import IMAGE_LAYOUTS
from .prim import CH, W, Y0, Y1_NOBANNER, Deck, banner, header, intro, logo, notes, stretch


def chapter_label(design, idx):
    ch = design.get("chapters") or []
    if 1 <= idx <= len(ch):
        return "%02d %s" % (idx, ch[idx - 1])
    return ""


def render_page(d, design, pg):
    L_ = LAYOUTS[pg["layout"]]
    title = ("%s  %s" % (pg.get("number", ""), pg.get("topic", ""))).strip()
    chap = chapter_label(design, pg.get("chapter", 0))
    # 底部结论横幅（0.7.0-dev23 起默认不放）：结论已在标题下的导语里，横幅与导语重复；
    # 不放时内容区向下扩展：原图版式直接按更高的区域排（图更大更清楚），其他版式排好后纵向拉伸
    show = bool(design.get("banner", False))
    native = pg["layout"] in IMAGE_LAYOUTS
    marks = []

    def frame(pi):
        s2 = d.new(pg.get("key"))
        header(d, s2, title + "（续）", chap)
        intro(d, s2, "接上页")
        marks.append((s2, len(s2.shapes)))
        if show:
            banner(d, s2, pg.get("banner", ""))
        notes(s2, pg.get("notes", ""))
        return s2

    s = d.new(pg.get("key"))
    header(d, s, title, chap)
    intro(d, s, pg.get("intro", ""))
    marks.insert(0, (s, len(s.shapes)))
    d.ch = (Y1_NOBANNER - Y0) if (not show and native) else CH
    if L_["paged"]:
        L_["render"](d, s, pg["spec"], frame=frame)
    else:
        L_["render"](d, s, pg["spec"])
    d.ch = CH
    if show:
        banner(d, s, pg.get("banner", ""))
    elif not native:
        for sl, n0 in marks:
            stretch(sl, n0)
    notes(s, pg.get("notes", ""))


def build(design, out_path=None, only=None):
    """返回 (Deck, 页面映射)。only：只渲染指定 key 的内容页（设计阶段试排用）。"""
    d = Deck(design.get("theme", "blue"), design.get("palette"), design.get("font"), design.get("logo"))
    every = bool(d.logo and d.logo.get("pos") == "all")
    pages = design["pages"]
    if only is not None:
        for pg in pages:
            if pg.get("key") in only:
                render_page(d, design, pg)
        return d, None
    slide_map = []          # 第 n 张幻灯片 → 页面 key / 结构页
    cv = design.get("cover")
    if cv:
        s0 = d.new("cover")
        structural.cover(d, s0, cv.get("title", ""), cv.get("subtitle"), cv.get("tag"), cv.get("fields"))
        if not every:          # 每页都放时 Logo 在母版里，封面不另贴（0.7.0-dev19）；无 Logo 时什么也不做
            logo(d, s0, W - 0.6, 0.45, 0.62, max_w=2.2, card=True)
        notes(s0, cv.get("notes", ""))
        slide_map.append("cover")
    chapters = design.get("chapters") or []
    if chapters and design.get("toc", True):
        s0 = d.new("toc")
        structural.toc(d, s0, chapters)
        if every and not d.logo_master:
            logo(d, s0, W - 0.53, 0.40, 0.45, max_w=1.3)
        notes(s0, (design.get("toc_notes") or ""))
        slide_map.append("toc")
    cur = 0
    for pg in pages:
        ch = pg.get("chapter", 0)
        if design.get("section_pages", True) and chapters and ch and ch != cur:
            cur = ch
            items = ["%s  %s" % (p.get("number", ""), p.get("topic", "")) for p in pages if p.get("chapter") == ch]
            # 只有一页的章不加过渡页（避免过渡页比内容页还多）
            if len(items) >= int(design.get("section_min_pages", 2)):
                s0 = d.new("section%d" % ch)
                style = design.get("section_style") or "hero"      # 默认整页蓝底大号章号（与技术方案一致）；toc 为目录式
                if style == "toc":
                    structural.section_toc(d, s0, ch, chapters[ch - 1], chapters, items)
                else:
                    structural.section(d, s0, ch, chapters[ch - 1], items)
                # 章节过渡页不放 Logo（0.7.0-dev18）
                notes(s0, (design.get("section_notes") or {}).get(str(ch), ""))
                slide_map.append("section%d" % ch)
        before = d.page
        render_page(d, design, pg)
        slide_map += [pg.get("key")] * (d.page - before)
    cl = design.get("closing")
    if cl is not None:
        s0 = d.new("closing")
        structural.closing(d, s0, cl.get("head", "汇报完毕"), cl.get("sub"), cl.get("fields"))
        if not every:          # 同封面：每页都放时结束页不另贴
            logo(d, s0, W - 0.6, 0.45, 0.62, max_w=2.2, card=True)
        notes(s0, cl.get("notes", ""))
        slide_map.append("closing")
    if out_path:
        d.save(out_path)
    return d, slide_map
