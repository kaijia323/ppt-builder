"""视觉基础元件：设计令牌、原生形状绘制、文字测量与版面质检记录。

全部用 python-pptx 原生形状绘制，输出在 PowerPoint 中可直接编辑。
设计令牌取自两套视觉标杆（版式目录 v1.2 第一节）。

文字测量：没有字体渲染引擎，按字符类别估算宽度（汉字 1em，西文约 0.55em），
足以判断"是否换行、是否溢出"。每个文本框绘制时都会估算，放不下时：
  - 指定了 fit（最小字号）则逐级缩小字号；
  - 缩到最小仍放不下，记一条质检问题（Deck.qa），设计阶段据此要求模型压缩文字。
"""
import math
import os
import re
import threading

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from .icons import add_icon as _add_icon

W, H = 13.333, 7.5
FONT = "Microsoft YaHei"
NUM_FONT = "Arial"
# 内容区（有导语时）
X0, X1, Y0, Y1 = 0.75, 12.58, 1.55, 6.55
CW, CH = X1 - X0, Y1 - Y0
# 不放底部结论横幅时（0.7.0-dev23 起默认）内容区下沿：版式仍按 Y1 排，排好后整体纵向拉伸到这里（字号不变、只变宽松）
Y1_NOBANNER = 7.05

THEMES = {
    "blue": dict(deep="1552B8", primary="2574DB", light="4A90E2", mid="6E9BD8", pale="EEF4FC", pale2="F5F8FD",
                 border="8DB6EE", line="C9D8EE", accent_a="1BAA5A", accent_a_pale="E8F6EE", accent_b="F39A1E",
                 accent_b_pale="FFF6EC", accent_b_dark="9A5A08", accent_b_border="F5C78A", red="E0201B",
                 text="333333", title="1F1F1F", gray="7A8599", muted="8A94A6", muted_pale="F2F4F7",
                 grad=("1A5FC8", "2F80E4"), cover=("0E3E96", "2F80E4"), on_dark="DCE8FA"),
    "teal": dict(deep="0B5C68", primary="0E7F8E", light="2FA3AF", mid="6DBAC2", pale="EAF5F6", pale2="F4FAFA",
                 border="8CCBD1", line="C6E2E5", accent_a="3A8F4B", accent_a_pale="EAF4EC", accent_b="E8962E",
                 accent_b_pale="FDF4E8", accent_b_dark="8F5A12", accent_b_border="F2C88F", red="D23B2E",
                 text="333333", title="1F1F1F", gray="74868A", muted="8A9699", muted_pale="F2F5F5",
                 grad=("0D6E7B", "1F9AA7"), cover=("08454E", "1A8F9C"), on_dark="D8EEF0"),
    # 以下 5 套（0.7.0-dev15）由 template.palette_from(主色) 生成后固定写在这里：以湛蓝为基准换色相，白字可读性、语义色撞色已按护栏处理
    "red": dict(deep="93121E", primary="B8202F", light="E14B5E", mid="D76F78", pale="FCEEEF", pale2="FDF5F5", border="ED8E96", line="EEC9CC", accent_a="1BAA5A", accent_a_pale="E8F6EE", accent_b="F39A1E", accent_b_pale="FFF6EC", accent_b_dark="9A5A08", accent_b_border="F5C78A", red="C45A00", text="333333", title="1F1F1F", gray="7A8599", muted="8A94A6", muted_pale="F2F4F7", grad=('C61C26', 'E23145'), cover=('870E10', 'E23145'), on_dark="FADCDE"),
    "slate": dict(deep="32465B", primary="46607A", light="6F90AD", mid="869BB1", pale="F2F5F8", pale2="F7F9FB", border="ABBDD0", line="D5DBE2", accent_a="1BAA5A", accent_a_pale="E8F6EE", accent_b="F39A1E", accent_b_pale="FFF6EC", accent_b_dark="9A5A08", accent_b_border="F5C78A", red="E0201B", text="333333", title="1F1F1F", gray="7A8599", muted="8A94A6", muted_pale="F2F4F7", grad=('4B6788', '5D82A7'), cover=('30435D', '5D82A7'), on_dark="E5EBF1"),
    "green": dict(deep="115537", primary="1F7A52", light="28A068", mid="3DAD7D", pale="EFFBF6", pale2="F6FCFA", border="95E6C3", line="CCEBDE", accent_a="2574DB", accent_a_pale="EEF4FC", accent_b="F39A1E", accent_b_pale="FFF6EC", accent_b_dark="9A5A08", accent_b_border="F5C78A", red="E0201B", text="333333", title="1F1F1F", gray="7A8599", muted="8A94A6", muted_pale="F2F4F7", grad=('186E4B', '1F905D'), cover=('0E4A34', '1F905D'), on_dark="DFF7ED"),
    "purple": dict(deep="472C83", primary="5E3FA3", light="8267C5", mid="9782C4", pale="F3F1F9", pale2="F8F7FB", border="B3A0DB", line="D8D0E7", accent_a="1BAA5A", accent_a_pale="E8F6EE", accent_b="F39A1E", accent_b_pale="FFF6EC", accent_b_dark="9A5A08", accent_b_border="F5C78A", red="E0201B", text="333333", title="1F1F1F", gray="7A8599", muted="8A94A6", muted_pale="F2F4F7", grad=('613BA7', '7352C1'), cover=('49287C', '7352C1'), on_dark="E8E2F4"),
    "amber": dict(deep="7F4D0B", primary="A66917", light="CC7B1C", mid="CE8D34", pale="FCF6EE", pale2="FDFAF5", border="F0C68B", line="EFDFC8", accent_a="1BAA5A", accent_a_pale="E8F6EE", accent_b="2574DB", accent_b_pale="EEF4FC", accent_b_dark="1552B8", accent_b_border="8DB6EE", red="E0201B", text="333333", title="1F1F1F", gray="7A8599", muted="8A94A6", muted_pale="F2F4F7", grad=('87580F', 'BB7013'), cover=('593C06', 'BB7013'), on_dark="FBEEDB"),
}

# 色调中文名（用户看到、说出来的名字）；用户说中文名或英文键都能识别
THEME_NAMES = {"blue": "湛蓝", "teal": "青碧", "red": "中国红", "slate": "墨蓝", "green": "松绿", "purple": "黛紫", "amber": "琥珀"}


def theme_key(name):
    """“中国红”“red”“红色”都识别成 red；识别不了返回 None。"""
    n = (name or "").strip()
    if n in THEMES or n == "template":
        return n
    for k, v in THEME_NAMES.items():
        if n and (n == v or n in v or v in n):
            return k
    alias = {"蓝": "blue", "青": "teal", "绿": "green", "红": "red", "紫": "purple", "金": "amber", "黄": "amber", "灰": "slate", "黑": "slate"}
    for ch, k in alias.items():
        if ch in n:
            return k
    return None

_CTX = threading.local()      # 每个线程各自的当前 Deck（设计阶段并行试排）


def cur_font():
    d = getattr(_CTX, "deck", None)
    return getattr(d, "font", FONT) if d else FONT


class Deck:
    def __init__(self, theme="blue", palette=None, font=None, logo=None):
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Inches(W), Inches(H)
        self.blank = self.prs.slide_layouts[6]
        # 套用户模板（0.7.0-dev9）：palette / font / logo 只在给了模板时才有，否则与以前完全相同
        self.C = palette if palette else THEMES.get(theme, THEMES["blue"])
        self.theme = "template" if palette else (theme if theme in THEMES else "blue")
        self.font = font or FONT
        self.logo = logo if logo and logo.get("path") and logo.get("pos") in ("cover", "all") else None
        # 每页都放 Logo 时（0.7.0-dev18）：Logo 只放一次在“内容页”版式里（母版方式），内容页、目录页用这个版式；
        # 封面、过渡页、结束页用空白版式，不放 Logo（0.7.0-dev19）
        self.logo_master = None
        if self.logo and self.logo.get("pos") == "all" and os.path.exists(self.logo["path"]):
            self.logo_master = self._logo_layout()
        self.page = 0
        self.ch = CH            # 当前页内容区高度（原图版式在不放底部横幅时用更高的区域，0.7.0-dev23）
        self.qa = []            # [{page, kind, text, detail}]
        self.tag = None         # 当前页的业务标识（如 P05），写进质检记录
        _CTX.deck = self

    def new(self, tag=None):
        self.page += 1
        self.tag = tag
        _CTX.deck = self
        plain = tag in ("cover", "closing") or str(tag or "").startswith("section")
        return self.prs.slides.add_slide(self.blank if plain or not self.logo_master else self.logo_master)

    def _logo_layout(self):
        """把“仅标题”版式清空占位符、改名“内容页（含 Logo）”，右上角放一次 Logo。
        python-pptx 不能直接往版式加图：先放在临时页上，再把图片挪进版式并重建图片关系。"""
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT
        lay = self.prs.slide_layouts[5]
        tree = lay.shapes._spTree
        for sp in list(tree):
            if sp.tag.endswith("}sp") and sp.find(".//{*}ph") is not None and sp.find(".//{*}ph").get("type") == "title":
                tree.remove(sp)
        lay._element.cSld.set("name", "内容页（含 Logo）")
        self.blank._element.cSld.set("name", "封面、过渡页（无 Logo）")
        tmp = self.prs.slides.add_slide(self.blank)
        x, y, w, hh = logo_box(self.logo, W - 0.53, 0.40, 0.45, max_w=1.3)
        pic = tmp.shapes.add_picture(self.logo["path"], Inches(x), Inches(y), Inches(w), Inches(hh))
        pic.name = "Logo"
        ids = [int(e.get("id")) for e in tree.iter() if e.tag.endswith("}cNvPr") and str(e.get("id", "")).isdigit()]
        pic._element.nvPicPr.cNvPr.set("id", str(max(ids + [1]) + 1))      # 版式内形状编号不重复，PowerPoint 打开不提示修复
        blip = pic._element.blipFill.blip
        blip.rEmbed = lay.part.relate_to(tmp.part.related_part(blip.rEmbed), RT.IMAGE)
        tree.append(pic._element)
        sid = self.prs.slides._sldIdLst[-1]
        self.prs.part.drop_rel(sid.rId)
        self.prs.slides._sldIdLst.remove(sid)
        return lay

    def issue(self, kind, txt, detail=""):
        self.qa.append({"page": self.page, "tag": self.tag, "kind": kind, "text": txt[:60], "detail": detail})

    def save(self, path):
        self.prs.save(path)


def _deck():
    return getattr(_CTX, "deck", None)


# ------------------------------------------------------------------ 文字测量
_WIDE = re.compile(r"[⺀-鿿豈-﫿＀-￯　-〿‘-‟…—]")


def em_width(s):
    """字符串宽度（单位 em）。"""
    w = 0.0
    for ch in s:
        if _WIDE.match(ch):
            w += 1.0
        elif ch == " ":
            w += 0.3
        elif ch.isupper() or ch.isdigit():
            w += 0.62
        elif ch in "il.,:;'|!":
            w += 0.3
        else:
            w += 0.52
    return w


def text_width(s, pt, bold=False):
    return em_width(s) * pt / 72.0 * (1.04 if bold else 1.0)


def plain(content):
    """把 text() 的 content 统一成段落字符串列表（去掉【】标记）。"""
    if isinstance(content, str):
        paras = content.split("\n")
    elif isinstance(content, list) and content and isinstance(content[0], list):
        paras = ["".join(x if isinstance(x, str) else x[0] for x in p) for p in content]
    elif isinstance(content, list):
        paras = ["".join(x if isinstance(x, str) else x[0] for x in content)]
    else:
        paras = [str(content)]
    return [re.sub(r"[【】]", "", p) for p in paras]


def lines_needed(paras, pt, width, bold=False):
    n = 0
    for p in paras:
        tw = text_width(p, pt, bold)
        n += max(1, math.ceil(tw / max(width, 0.1) - 0.02))
    return n


def fits(paras, pt, w, h, bold=False, line_spacing=1.15, max_lines=None):
    n = lines_needed(paras, pt, w, bold)
    if max_lines and n > max_lines:
        return False
    return n * pt * 1.2 * line_spacing / 72.0 <= h + 0.02


def orphan(paras, pt, w, bold=False):
    """末行只剩一个字（单字成行）。"""
    for p in paras:
        tw = text_width(p, pt, bold)
        if tw > w:
            n = math.ceil(tw / w - 0.02)
            if tw - (n - 1) * w < 1.3 * pt / 72.0:
                return True
    return False


def fit_size(paras, w, h, size, min_size, bold=False, line_spacing=1.15, max_lines=None):
    s = size
    while s > min_size and not fits(paras, s, w, h, bold, line_spacing, max_lines):
        s -= 0.5
    ok = fits(paras, s, w, h, bold, line_spacing, max_lines)
    if ok and orphan(paras, s, w, bold):
        t = s - 0.5
        while t >= min_size:
            if fits(paras, t, w, h, bold, line_spacing, max_lines) and not orphan(paras, t, w, bold):
                return t, True
            t -= 0.5
    return s, ok


# ------------------------------------------------------------------ 基础绘制
def rgb(h):
    return RGBColor.from_string(h)


def _alpha(parent, alpha):
    for clr in parent.iter(qn("a:srgbClr")):
        a = etree.SubElement(clr, qn("a:alpha"))
        a.set("val", str(int(alpha * 100000)))


def _bounds(x, y, w, h, what):
    d = _deck()
    if d and (x < -0.01 or y < -0.01 or x + w > W + 0.01 or y + h > H + 0.01):
        d.issue("out_of_slide", what, "(%.2f,%.2f,%.2f,%.2f)" % (x, y, w, h))


def box(s, x, y, w, h, fill=None, line=None, lw=0.75, shape=MSO_SHAPE.RECTANGLE, radius=None, dash=None, alpha=None,
        grad=None, grad_angle=0):
    _bounds(x, y, w, h, "形状")
    sp = s.shapes.add_shape(shape, Inches(x), Inches(y), Inches(max(w, 0.01)), Inches(max(h, 0.01)))
    if grad:
        sp.fill.gradient()
        sp.fill.gradient_angle = grad_angle
        st = sp.fill.gradient_stops
        st[0].color.rgb, st[0].position = rgb(grad[0]), 0
        st[1].color.rgb, st[1].position = rgb(grad[1]), 1.0
    elif fill:
        sp.fill.solid()
        sp.fill.fore_color.rgb = rgb(fill)
        if alpha is not None:
            _alpha(sp.fill._xPr, alpha)
    else:
        sp.fill.background()
    if line:
        sp.line.color.rgb = rgb(line)
        sp.line.width = Pt(lw)
        if dash:
            sp.line.dash_style = dash
    else:
        sp.line.fill.background()
    if radius is not None and shape in (MSO_SHAPE.ROUNDED_RECTANGLE, MSO_SHAPE.ROUND_1_RECTANGLE,
                                        MSO_SHAPE.ROUND_2_SAME_RECTANGLE):
        sp.adjustments[0] = radius
    etree.SubElement(sp._element.spPr, qn("a:effectLst"))
    sp.text_frame.text = ""
    return sp


def text(s, x, y, w, h, content, size=13, color="333333", bold=False, align="left", valign="top", font=FONT,
         line_spacing=1.15, vert=False, margin=(0.04, 0.02, 0.04, 0.02), red="E0201B", fit=None, max_lines=None,
         check=True, grow=None):
    """content：字符串（\\n 分段，【】标红加粗），或 [(文字, {size,color,bold,font}) ...]，或段落列表。
    fit：允许缩小到的最小字号；放不下时逐级缩小，仍放不下记质检问题。"""
    if font == FONT:
        font = cur_font()
    _bounds(x, y, w, h, "文本框")
    if vert and isinstance(content, str):
        # 竖排：逐字分段堆叠，不用 eaVert（LibreOffice 导出 PDF 时竖排字形会与同页横排相同汉字冲突）
        content = [[ch] for ch in content if ch.strip()]
        line_spacing = min(line_spacing, 1.0)
        align = "center"
    elif isinstance(content, str) and "\n" in content:
        content = [[ln] for ln in content.split("\n")]
    uw, uh = w - margin[0] - margin[2], h - margin[1] - margin[3]
    paras = plain(content)
    if grow and grow > size and paras and any(p.strip() for p in paras):
        # 版面有空余时放大字号（不超过 grow），避免大片留白里的小字
        g = grow
        while g > size:
            if fits(paras, g, uw, uh * 0.92, bold, line_spacing, max_lines) and not orphan(paras, g, uw, bold):
                size = g
                break
            g -= 0.5
    if check and paras and any(p.strip() for p in paras):
        if fit:
            size, ok = fit_size(paras, uw, uh, size, fit, bold, line_spacing, max_lines)
        else:
            ok = fits(paras, size, uw, uh, bold, line_spacing, max_lines)
        if not ok and _deck():
            _deck().issue("text_overflow", " / ".join(paras), "%.1fpt 需要 %d 行，框高 %.2fin 宽 %.2fin" % (
                size, lines_needed(paras, size, uw, bold), h, w))
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(max(w, 0.05)), Inches(max(h, 0.05)))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left, tf.margin_top, tf.margin_right, tf.margin_bottom = [Inches(m) for m in margin]
    tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}[valign]
    plist = content if isinstance(content, list) and content and isinstance(content[0], list) else [content]
    for i, para in enumerate(plist):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}[align]
        p.line_spacing = line_spacing
        if isinstance(para, list):
            runs = []
            for it in para:
                runs += _parse(it, red) if isinstance(it, str) else [it]
        else:
            runs = _parse(para, red)
        for t, opt in runs:
            r = p.add_run()
            r.text = t
            f = r.font
            f.name = opt.get("font", font)
            f.size = Pt(opt.get("size", size))
            f.bold = opt.get("bold", bold)
            f.color.rgb = rgb(opt.get("color", color))
            rpr = r._r.get_or_add_rPr()
            ea = rpr.find(qn("a:ea"))
            if ea is None:
                ea = etree.SubElement(rpr, qn("a:ea"))
            ea.set("typeface", cur_font())
    return tb


def _parse(s, red):
    out = []
    for part in re.split(r"(【[^】]*】)", s):
        if not part:
            continue
        if part.startswith("【"):
            out.append((part[1:-1], {"color": red, "bold": True}))
        else:
            out.append((part, {}))
    return out


def line(s, x1, y1, x2, y2, color, w=1.0, dash=None, head=None, tail=None, alpha=None):
    c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = rgb(color)
    c.line.width = Pt(w)
    if dash:
        c.line.dash_style = dash
    ln = c.line._get_or_add_ln()
    if alpha is not None:
        _alpha(ln, alpha)
    for tag, kind in (("a:headEnd", head), ("a:tailEnd", tail)):
        if kind:
            e = etree.SubElement(ln, qn(tag))
            e.set("type", kind)
            e.set("w", "med")
            e.set("len", "med")
    return c


def arrow(s, pts, color, w=1.25, dash=None):
    """折线箭头：最后一段带箭头。"""
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        line(s, x1, y1, x2, y2, color, w=w, dash=dash, tail="triangle" if i == len(pts) - 2 else None)


def add_icon(s, name, x, y, size, color="FFFFFF"):
    return _add_icon(s, name, x, y, size, color)


# ------------------------------------------------------------------ 常用元件
def pill(d, s, x, y, w, h, content, fill, color="FFFFFF", size=12, bold=True, line=None, fit=9.5):
    box(s, x, y, w, h, fill=fill, line=line, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.5)
    text(s, x, y, w, h, content, size=size, color=color, bold=bold, align="center", valign="middle",
         margin=(0.04, 0, 0.04, 0), fit=fit, max_lines=1)


def pill_width(t, size=12, pad=0.32, min_w=0.6):
    return max(min_w, text_width(t, size, True) + pad)


def icon_circle(d, s, x, y, size, icon, c1=None, c2=None):
    C = d.C
    box(s, x, y, size, size, grad=(c1 or C["light"], c2 or C["deep"]), grad_angle=45, shape=MSO_SHAPE.OVAL)
    add_icon(s, icon, x + size * 0.22, y + size * 0.22, size * 0.56)


def badge(d, s, x, y, size, num, fill=None, color="FFFFFF", fsize=None):
    box(s, x, y, size, size, fill=fill or d.C["primary"], shape=MSO_SHAPE.OVAL)
    text(s, x, y, size, size, str(num), size=fsize or max(9, size * 30), bold=True, color=color, align="center",
         valign="middle", margin=(0, 0, 0, 0), font=NUM_FONT, check=False)


def chev(s, x, y, w, h, fill, down=False):
    sp = box(s, x, y, w, h, fill=fill, shape=MSO_SHAPE.CHEVRON)
    if down:
        sp.rotation = 90
    return sp


def panel(d, s, x, y, w, h, title=None, color=None, fill="FFFFFF"):
    """白底细边框面板 + 左上角色块标签。返回内容起始 y。"""
    C = d.C
    box(s, x, y, w, h, fill=fill, line=C["line"], lw=0.75, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.03)
    if not title:
        return y + 0.18
    tw = min(w - 0.36, pill_width(title, 13.5, 0.4))
    box(s, x + 0.18, y + 0.16, tw, 0.4, fill=color or C["primary"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
    text(s, x + 0.18, y + 0.16, tw, 0.4, title, size=13.5, bold=True, color="FFFFFF", align="center", valign="middle",
         fit=10.5, max_lines=1)
    return y + 0.7


def head_body(d, s, x, y, w, h, head, body, hsize=14, bsize=12, hcolor=None, gap=0.04, min_b=10.5, align="left"):
    """标题 + 说明。空间够时分两段（标题一行、说明在下）；不够时合成一段（“标题：说明”），共用全部高度。"""
    C = d.C
    m = 0.08          # 文本框左右内边距合计
    if head and body:
        hh = min(0.42, max(0.3, h * 0.4))
        bh = h - hh - gap
        two = fits([head], hsize - 2.5, w - m, hh, True, 1.15, 1) and bh > 0.2 and \
            fits([body], min_b, w - m, bh - 0.04, False, 1.15)
        if two:
            text(s, x, y, w, hh, head, size=hsize, bold=True, color=hcolor or C["deep"], valign="middle",
                 fit=hsize - 2.5, max_lines=1, align=align)
            text(s, x, y + hh + gap, w, bh, body, size=bsize, color=C["text"], fit=min_b, line_spacing=1.15,
                 align=align, grow=bsize + 2)
        else:
            text(s, x, y, w, h, [[(head + "：", {"bold": True, "color": hcolor or C["deep"]}), (body, {})]],
                 size=bsize, color=C["text"], valign="middle", fit=min(min_b, 10), line_spacing=1.12, align=align)
    elif head:
        text(s, x, y, w, h, head, size=hsize, bold=True, color=hcolor or C["deep"], valign="middle", fit=hsize - 3,
             align=align)
    elif body:
        text(s, x, y, w, h, body, size=bsize, color=C["text"], valign="middle", fit=min_b, line_spacing=1.15,
             align=align, grow=bsize + 2)


# ------------------------------------------------------------------ 页面骨架
def header(d, s, title, chapter):
    C = d.C
    if not d.logo_master:                          # 母版放 Logo 时不铺整页白底（会盖住版式里的 Logo），页面背景本来就是白色
        box(s, 0, 0, W, H, fill="FFFFFF")
    lw = 0
    tw = 8.6
    if d.logo and d.logo.get("pos") == "all":      # 每页右上角小 Logo，章节名与页码向左让位（0.7.0-dev9）
        if d.logo_master:
            lw = logo_box(d.logo, W - 0.53, 0.40, 0.45, max_w=1.3)[2] + 0.2
        else:
            lw = logo(d, s, W - 0.53, 0.40, 0.45, max_w=1.3, align="right") + 0.2
        # 章节名右对齐、整体左移后，标题区收窄到章节名左侧，长标题缩字而不压住章节名
        chap_left = 12.8 - lw - text_width("%s   %02d" % (chapter, d.page), 12) - 0.15
        tw = max(6.0, min(8.6, chap_left - 0.55 - 0.1))
    text(s, 0.55, 0.26, tw, 0.72, title, size=28, bold=True, color=C["title"], valign="middle", fit=20, max_lines=1)
    text(s, 9.0 - lw, 0.40, 3.8, 0.45, [(chapter, {"color": C["gray"]}), ("   %02d" % d.page,
                                                                         {"color": C["primary"], "bold": True})],
         size=12, align="right", valign="middle", fit=9, max_lines=1)


def logo_box(lg, x, y, h, max_w=1.6):
    """右边缘在 x 的 Logo 位置：(左, 上, 宽, 高)，与 logo() 不垫底时一致。"""
    asp = float(lg.get("aspect") or 2.5)
    w = min(max_w, h * asp)
    hh = w / asp
    return x - w, y + (h - hh) / 2, w, hh


def logo(d, s, x, y, h, max_w=1.6, align="right", card=False):
    """放 Logo：高 h、按原比例，不超过 max_w；align=right 时 x 是右边缘。card：深色背景上垫白色圆角底。返回占用宽度。"""
    import os
    lg = d.logo
    if not lg or not os.path.exists(lg.get("path", "")):
        return 0
    asp = float(lg.get("aspect") or 2.5)
    w = min(max_w, h * asp)
    hh = w / asp
    pad = 0.1 if card else 0
    x0 = x - w - pad if align == "right" else x + pad
    if card:
        box(s, x0 - pad, y + (h - hh) / 2 - pad, w + 2 * pad, hh + 2 * pad, fill="FFFFFF",
            shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.18)
    s.shapes.add_picture(lg["path"], Inches(x0), Inches(y + (h - hh) / 2), Inches(w), Inches(hh))
    return w + 2 * pad


def intro(d, s, content, y=1.0):
    if content:
        text(s, 0.75, y, 11.9, 0.42, content, size=14, color=d.C["text"], red=d.C["red"], valign="middle", fit=12,
             max_lines=1)


def banner(d, s, content):
    C = d.C
    box(s, 0, 6.72, W, 0.78, shape=MSO_SHAPE.ROUND_1_RECTANGLE, radius=0.5, grad=C["grad"], grad_angle=0)
    text(s, 0.6, 6.78, W - 1.2, 0.66, content, size=18, color="FFFFFF", align="center", valign="middle", fit=14,
         max_lines=1)


def stretch(s, n0, y_to=Y1_NOBANNER):
    """把本页第 n0 个之后的形状（版式画的部分）从内容区 [Y0, Y1] 纵向拉伸到 [Y0, y_to]（0.7.0-dev23，去掉底部横幅后用）。
    只放大位置与高度、不缩小，字号不变，所以不会新增放不下或重叠；圆形、图标、小方块、图片、组合保持原尺寸只移动中心，
    避免被拉成椭圆；表格同时放大行高；连接线两端按同样比例移动。内容区以上（标题、导语）不动。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    E = 914400.0
    k = (y_to - Y0) / (Y1 - Y0)
    top0 = Y0 * E

    def ny(v):
        return int(top0 + (v - top0) * k)
    for sh in list(s.shapes)[n0:]:
        try:
            if hasattr(sh, "begin_y") and sh.shape_type in (MSO_SHAPE_TYPE.LINE, None) and hasattr(sh, "end_y"):
                if min(sh.begin_y, sh.end_y) >= top0 - 0.02 * E:
                    sh.begin_y, sh.end_y = ny(sh.begin_y), ny(sh.end_y)
                continue
        except Exception:      # noqa  不是连接线
            pass
        if sh.top is None or sh.height is None or sh.top < top0 - 0.02 * E:
            continue
        w, h = sh.width or 0, sh.height or 0
        keep = sh.shape_type in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.GROUP) or \
            (abs(w - h) < 0.03 * E and w < 1.3 * E)
        if keep:
            sh.top = int(ny(sh.top + h / 2.0) - h / 2.0)
        else:
            sh.top = ny(sh.top)
            sh.height = int(h * k)
            if getattr(sh, "has_table", False) and sh.has_table:
                for r in sh.table.rows:
                    r.height = int(r.height * k)


def notes(s, txt):
    if txt:
        s.notes_slide.notes_text_frame.text = txt


def network(s, cx, cy, r, color="FFFFFF", alpha_line=0.35, alpha_dot=0.85):
    pts = [(cx, cy)]
    for ang, rr in [(-70, 0.95), (-20, 1.0), (25, 0.9), (80, 0.85), (140, 1.0), (195, 0.9), (250, 1.05)]:
        a = math.radians(ang)
        pts.append((cx + r * rr * math.cos(a), cy + r * rr * math.sin(a)))
    for p in pts[1:]:
        line(s, cx, cy, p[0], p[1], color, w=1.0, alpha=alpha_line)
    for i in range(1, len(pts)):
        q = pts[i + 1] if i + 1 < len(pts) else pts[1]
        line(s, pts[i][0], pts[i][1], q[0], q[1], color, w=0.75, alpha=alpha_line * 0.6)
    for k, (px, py) in enumerate(pts):
        rr = 0.13 if k == 0 else 0.06
        box(s, px - rr, py - rr, 2 * rr, 2 * rr, fill=color, shape=MSO_SHAPE.OVAL, alpha=alpha_dot)
    for rr in (r * 0.35, r * 0.6, r * 0.85):
        o = box(s, cx - rr, cy - rr, 2 * rr, 2 * rr, line=color, lw=0.5, shape=MSO_SHAPE.OVAL)
        _alpha(o.line._get_or_add_ln(), 0.18)


def wave(s, color="FFFFFF", alpha=0.12, y0=6.4):
    for k, (amp, a) in enumerate([(0.35, alpha), (0.22, alpha * 0.8)]):
        fb = s.shapes.build_freeform(Inches(0), Inches(y0 + 0.25 * k))
        steps = 24
        pts = []
        for i in range(steps + 1):
            x = W * i / steps
            yy = y0 + 0.25 * k + amp * math.sin(math.pi * (i / steps) * 1.6 + k)
            pts.append((Inches(x), Inches(yy)))
        fb.add_line_segments(pts[1:], close=False)
        fb.add_line_segments([(Inches(W), Inches(H)), (Inches(0), Inches(H))], close=True)
        sp = fb.convert_to_shape()
        sp.fill.solid()
        sp.fill.fore_color.rgb = rgb(color)
        _alpha(sp.fill._xPr, a)
        sp.line.fill.background()


def pick_icons(texts, used=None):
    """按文字确定性选图标，同页不重复。"""
    from .icon_map import FALLBACK_ICONS, ICON_KEYWORDS
    used = set(used or [])
    out = []
    fb = iter(FALLBACK_ICONS * 3)
    for t in texts:
        name = None
        for kw, ic in ICON_KEYWORDS:
            if ic not in used and re.search(kw, t or "", re.I):
                name = ic
                break
        if not name:
            name = next(fb)
            while name in used:
                name = next(fb)
        used.add(name)
        out.append(name)
    return out


# ------------------------------------------------------------------ 原文图片（0.6）
def _emf_image_part(slide, blob, fmt):
    """python-pptx 不认 EMF 扩展名（会当成 WMF）：按原格式建图片部件，矢量图原样嵌入、放大不失真。"""
    from pptx.parts.image import Image, ImagePart

    class _Vec(Image):
        @property
        def ext(self):
            return fmt

        @property
        def content_type(self):
            return "image/x-emf" if fmt == "emf" else "image/x-wmf"
    img = _Vec(blob, "image.%s" % fmt)
    return ImagePart.new(slide.part.package, img)


def picture(d, s, img, x, y, w, h, border=True):
    """把原文图片按原比例放进 (x, y, w, h) 框内居中，不拉伸、不裁切、不重新压缩。
    img: {"path", "fmt", "aspect", "px"}。返回实际占用的 (x, y, w, h)。"""
    import os
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    path = img.get("path") or ""
    if not os.path.exists(path):
        d.issue("image_missing", os.path.basename(path), "原文图片文件不存在")
        return x, y, w, h
    asp = float(img.get("aspect") or 1.33)
    if w / h > asp:
        pw, ph = h * asp, h
    else:
        pw, ph = w, w / asp
    px_, py_ = x + (w - pw) / 2, y + (h - ph) / 2
    fmt = img.get("fmt")
    if fmt in ("emf", "wmf"):
        with open(path, "rb") as f:
            part = _emf_image_part(s, f.read(), fmt)
        rId = s.part.relate_to(part, RT.IMAGE)
        pic = s.shapes._add_pic_from_image_part(part, rId, Inches(px_), Inches(py_), Inches(pw), Inches(ph))
        s.shapes._recalculate_extents()
    else:
        s.shapes.add_picture(path, Inches(px_), Inches(py_), Inches(pw), Inches(ph))
        px = img.get("px")
        # 显示尺寸下低于约 110 像素/英寸会发虚（投影时更明显），提示用户
        if px and px[0] / pw < 110:
            d.issue("image_low_res", os.path.basename(path), "原图 %d×%d 像素，在本页显示为 %.1f 英寸宽，可能不够清晰" % (px[0], px[1], pw))
    if border:
        box(s, px_ - 0.04, py_ - 0.04, pw + 0.08, ph + 0.08, fill=None, line=d.C["border"], lw=0.75)
    return px_, py_, pw, ph
