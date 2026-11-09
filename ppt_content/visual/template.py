"""套用户模板（0.7.0-dev9）：只取模板的主色、正文字体、Logo，版式结构不变。不调用模型。

只有运行时给了 --template / --logo 才生效；不给时成品与以前完全一致。

取主色
  1. 模板主题配色不是 Office 默认值 → 用主题的强调色 1
  2. 是默认值（实测两份真实模板都是：真正的配色直接画在形状上）→ 统计幻灯片、版式、母版里形状实际填充的颜色，
     取用得最多的“醒目色”（饱和度 ≥0.35、明度 0.2–0.7；同色相合并计数，组内取次数多、饱和、明度适中的那个）
  3. 都取不到 → 用默认蓝色，并说明原因
由主色生成整套配色：以默认蓝色配色为基准，换成模板主色的色相、按饱和度比例缩放，各档明度不变
  （主色恰好是默认蓝 2574DB 时，生成的配色与默认蓝色完全相同）。
  绿/橙/红三种语义色保持不变；主色与其中某个撞色时（红色系、橙色系、绿色系模板）把撞色的那个换掉。
  护栏：主色上的白字对比度 ≥4.5:1，不够就加深。
取字体：正文实际用得最多的中文字体；只认常见字体，生僻字体回退微软雅黑（对方电脑没装会被替换，版面估算失准）。
取 Logo：用户直接给图片最可靠；否则先找母版/版式里的图片，再找“多页同一角落重复出现的同一张小图”；找不到不放。
"""
import colorsys
import hashlib
import io
import os
import re
import zipfile
from collections import Counter

from .prim import THEMES

OFFICE_DEFAULT_ACCENT1 = {"4472C4", "4F81BD", "5B9BD5", "156082", "0F6FC6", "4A66AC", "E48312"}
COMMON_FONTS = {
    "微软雅黑": "Microsoft YaHei", "Microsoft YaHei": "Microsoft YaHei", "Microsoft YaHei UI": "Microsoft YaHei",
    "宋体": "SimSun", "SimSun": "SimSun", "黑体": "SimHei", "SimHei": "SimHei", "等线": "DengXian", "DengXian": "DengXian",
    "仿宋": "FangSong", "FangSong": "FangSong", "仿宋_GB2312": "FangSong", "楷体": "KaiTi", "KaiTi": "KaiTi",
    "思源黑体": "Source Han Sans CN", "Source Han Sans CN": "Source Han Sans CN", "Noto Sans CJK SC": "Noto Sans CJK SC",
    "苹方": "PingFang SC", "PingFang SC": "PingFang SC",
}
DEFAULT_FONT = "Microsoft YaHei"
CJK = re.compile(r"[\u4e00-\u9fa5]")


# ------------------------------------------------------------------ 颜色工具
def _hls(h):
    r, g, b = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    return colorsys.rgb_to_hls(r, g, b)


def _hex(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def _lum(hx):
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = [int(hx[i:i + 2], 16) for i in (0, 2, 4)]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b="FFFFFF"):
    la, lb = sorted([_lum(a), _lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hue_dist(a, b):
    d = abs(a - b) % 1.0
    return min(d, 1 - d)


def brandlike(hx):
    h, l, s = _hls(hx)
    return s >= 0.35 and 0.2 <= l <= 0.7


# ------------------------------------------------------------------ 读模板
def _theme(z):
    names = sorted(n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml$", n))
    if not names:
        return {}, {}
    x = z.read(names[0]).decode("utf-8", "ignore")
    cols = {}
    cs = re.search(r"<a:clrScheme[^>]*>(.*?)</a:clrScheme>", x, re.S)
    if cs:
        for m in re.finditer(r"<a:(dk1|lt1|dk2|lt2|accent\d)>(.*?)</a:\1>", cs.group(1), re.S):
            v = re.search(r'(?:srgbClr val|lastClr)="([0-9A-Fa-f]{6})"', m.group(2))
            if v:
                cols[m.group(1)] = v.group(1).upper()
    fonts = {}
    for k in ("major", "minor"):
        g = re.search(r"<a:%sFont>(.*?)</a:%sFont>" % (k, k), x, re.S)
        if g:
            ea = re.search(r'<a:ea typeface="([^"]*)"', g.group(1))
            hans = re.search(r'<a:font script="Hans" typeface="([^"]*)"', g.group(1))
            fonts[k] = (ea.group(1) if ea and ea.group(1) else "") or (hans.group(1) if hans else "")
    return cols, fonts


def _parts(z, kinds=("slides/slide", "slideLayouts/slideLayout", "slideMasters/slideMaster")):
    pat = re.compile(r"ppt/(%s)\d+\.xml$" % "|".join(kinds))
    return sorted(n for n in z.namelist() if pat.match(n))


def sample_fills(z):
    """形状实际填充色计数（只算 spPr 里的纯色填充）。"""
    c = Counter()
    for n in _parts(z):
        x = z.read(n).decode("utf-8", "ignore")
        for sp in re.findall(r"<p:spPr>(.*?)</p:spPr>", x, re.S):
            for v in re.findall(r'<a:solidFill>\s*<a:srgbClr val="([0-9A-Fa-f]{6})"', sp):
                c[v.upper()] += 1
    return c


def pick_primary(fills):
    """醒目色按色相分组（色相差 <0.04 算一组），取总次数最多的一组；组内代表色按
    次数 × 饱和度 × 接近中等明度 打分（浅色衬底、深色描边用得再多也不当主色）。"""
    groups = []
    for hx, n in fills.most_common():
        if not brandlike(hx):
            continue
        h, l, s = _hls(hx)
        for g in groups:
            if _hue_dist(h, g["h"]) < 0.04:
                g["n"] += n
                g["items"].append((hx, n, l, s))
                break
        else:
            groups.append({"h": h, "n": n, "items": [(hx, n, l, s)]})
    if not groups:
        return None
    g = max(groups, key=lambda g: g["n"])
    return max(g["items"], key=lambda it: it[1] * it[3] * (1 - abs(it[2] - 0.5)))[0]


def sample_font(z, theme_fonts):
    c = Counter()
    for n in _parts(z, ("slides/slide",)):
        x = z.read(n).decode("utf-8", "ignore")
        for f in re.findall(r'<a:ea typeface="([^"+][^"]*)"', x):
            c[f] += 3
        for f in re.findall(r'<a:latin typeface="([^"+][^"]*)"', x):
            if CJK.search(f) or f in COMMON_FONTS:
                c[f] += 1
    for k in ("minor", "major"):
        if theme_fonts.get(k):
            c[theme_fonts[k]] += 1
    for f, _ in c.most_common():
        if f in COMMON_FONTS:
            return COMMON_FONTS[f], None
    top = c.most_common(1)[0][0] if c else None
    return DEFAULT_FONT, (top if top and top not in COMMON_FONTS else None)


def _rels(z, part):
    d, b = os.path.split(part)
    rp = "%s/_rels/%s.rels" % (d, b)
    if rp not in z.namelist():
        return {}
    x = z.read(rp).decode("utf-8", "ignore")
    out = {}
    for m in re.finditer(r'<Relationship [^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', x):
        out[m.group(1)] = os.path.normpath(os.path.join(d, m.group(2))).replace("\\", "/")
    for m in re.finditer(r'<Relationship [^>]*Target="([^"]+)"[^>]*Id="([^"]+)"', x):
        out.setdefault(m.group(2), os.path.normpath(os.path.join(d, m.group(1))).replace("\\", "/"))
    return out


def _pics(z, part):
    """[(媒体路径, x, y, w, h)]，单位 EMU。"""
    x = z.read(part).decode("utf-8", "ignore")
    rel = _rels(z, part)
    out = []
    for p in re.findall(r"<p:pic>(.*?)</p:pic>", x, re.S):
        e = re.search(r'r:embed="([^"]+)"', p)
        off = re.search(r'<a:off x="(-?\d+)" y="(-?\d+)"', p)
        ext = re.search(r'<a:ext cx="(\d+)" cy="(\d+)"', p)
        if e and off and ext and rel.get(e.group(1)) in z.namelist():
            out.append((rel[e.group(1)], int(off.group(1)), int(off.group(2)), int(ext.group(1)), int(ext.group(2))))
    return out


def _corner(x, y, w, h, W, H):
    cx, cy = x + w / 2.0, y + h / 2.0
    return (cx < W * 0.3 or cx > W * 0.7) and (cy < H * 0.25 or cy > H * 0.75)


def find_logo(z):
    """返回 (媒体路径, 来源说明) 或 (None, 原因)。"""
    prs = z.read("ppt/presentation.xml").decode("utf-8", "ignore")
    m = re.search(r'<p:sldSz cx="(\d+)" cy="(\d+)"', prs)
    W, H = (int(m.group(1)), int(m.group(2))) if m else (12192000, 6858000)
    small = W * H * 0.06
    cand = []
    for part in _parts(z, ("slideMasters/slideMaster", "slideLayouts/slideLayout")):
        for media, x, y, w, h in _pics(z, part):
            if w * h <= small and _corner(x, y, w, h, W, H):
                cand.append((w * h, media))
    if cand:
        return min(cand)[1], "模板母版"
    slides = _parts(z, ("slides/slide",))
    if len(slides) >= 3:
        seen = Counter()
        where = {}
        for part in slides:
            got = set()
            for media, x, y, w, h in _pics(z, part):
                if w * h <= small and _corner(x, y, w, h, W, H):
                    hsh = hashlib.sha1(z.read(media)).hexdigest()
                    got.add(hsh)
                    where[hsh] = media
            seen.update(got)
        if seen:
            hsh, n = seen.most_common(1)[0]
            if n >= max(3, 0.4 * len(slides)):
                return where[hsh], "模板中 %d/%d 页同一角落的图片" % (n, len(slides))
    return None, "模板里没有找到 Logo（母版里没有图片，各页角落也没有重复出现的小图）"


# ------------------------------------------------------------------ 生成配色
def palette_from(primary, base="blue"):
    """以默认配色为基准，换色相、按饱和度比例缩放；返回 (配色, 调整说明列表)。"""
    B = dict(THEMES[base])
    notes = []
    p = primary.upper()
    h, l, s = _hls(p)
    k = 0
    while contrast(p) < 4.5 and k < 30:
        l -= 0.02
        p = _hex(h, l, s)
        k += 1
    if k:
        notes.append("模板主色 #%s 偏浅，白字看不清，已加深为 #%s" % (primary.upper(), p))
    bh, bl, bs = _hls(B["primary"])
    ratio = s / bs if bs else 1.0

    def conv(hx):
        h2, l2, s2 = _hls(hx)
        return _hex(h + (h2 - bh), l2, min(1.0, s2 * ratio))
    out = dict(B)
    for key in ("deep", "light", "mid", "pale", "pale2", "border", "line", "on_dark"):
        out[key] = conv(B[key])
    out["primary"] = p
    out["grad"] = tuple(conv(x) for x in B["grad"])
    out["cover"] = tuple(conv(x) for x in B["cover"])
    # 深色必须比主色深
    dh, dl, ds = _hls(out["deep"])
    if dl >= l - 0.06:
        out["deep"] = _hex(h, l - 0.1, ds)

    # 承载白字的颜色（主色、深色、浅色、中间色、横幅与封面渐变）不得比默认配色里对应的颜色更难读：
    # 黄、橙、绿在同样明度下看起来亮得多，按对比度逐级加深（默认蓝色配色本身不受影响）
    def guard(hx, ref):
        target = contrast(ref) * 0.97
        hh, ll, ss = _hls(hx)
        k2 = 0
        while contrast(hx) < target and k2 < 40:
            ll -= 0.015
            hx = _hex(hh, ll, ss)
            k2 += 1
        return hx, k2 > 0
    changed = False
    for key in ("deep", "light", "mid"):
        out[key], c = guard(out[key], B[key])
        changed |= c
    for key in ("grad", "cover"):
        pair = [guard(x, r) for x, r in zip(out[key], B[key])]
        out[key] = tuple(x for x, _ in pair)
        changed |= any(c for _, c in pair)
    if changed:
        notes.append("模板色系偏亮，横幅、标签等白字底色已相应加深，保证文字看得清")
    # 语义色撞色处理
    blue = THEMES["blue"]
    if _hue_dist(h, _hls(B["red"])[0]) < 0.05:
        out["red"] = "C45A00"
        notes.append("模板主色是红色系，“标红强调”改用深橙色，避免和主色混在一起")
    if _hue_dist(h, _hls(B["accent_b"])[0]) < 0.05:
        for key in ("accent_b", "accent_b_pale", "accent_b_dark", "accent_b_border"):
            out[key] = {"accent_b": blue["primary"], "accent_b_pale": blue["pale"], "accent_b_dark": blue["deep"],
                        "accent_b_border": blue["border"]}[key]
        notes.append("模板主色是橙黄色系，辅助橙色改用蓝色")
    if _hue_dist(h, _hls(B["accent_a"])[0]) < 0.05:
        out["accent_a"], out["accent_a_pale"] = blue["primary"], blue["pale"]
        notes.append("模板主色是绿色系，辅助绿色改用蓝色")
    return out, notes


# ------------------------------------------------------------------ 入口
def extract(template_path, out_dir, logo_path=None):
    """返回 {"palette", "font", "primary", "color_source", "logo", "notes"}；Logo 图片复制到 out_dir。"""
    info = {"palette": None, "font": DEFAULT_FONT, "primary": None, "color_source": None, "logo": None, "notes": []}
    z = None
    if template_path:
        try:
            z = zipfile.ZipFile(template_path)
        except Exception as e:      # noqa
            info["notes"].append("模板《%s》打不开（%r），已用默认配色" % (os.path.basename(template_path), e))
    if z is not None:
        cols, tfonts = _theme(z)
        a1 = cols.get("accent1")
        prim, src = None, None
        if a1 and a1 not in OFFICE_DEFAULT_ACCENT1 and brandlike(a1):
            prim, src = a1, "模板主题配色"
        else:
            prim = pick_primary(sample_fills(z))
            src = "模板页面上用得最多的颜色" if prim else None
        if prim:
            pal, notes = palette_from(prim)
            info.update(palette=pal, primary=pal["primary"], color_source=src)
            info["notes"] += notes
        else:
            info["notes"].append("模板里没有取到醒目的主色（页面以黑白灰为主），已用默认配色")
        font, odd = sample_font(z, tfonts)
        info["font"] = font
        if odd:
            info["notes"].append("模板字体“%s”不常见，对方电脑可能没有，已改用微软雅黑" % odd)
    os.makedirs(out_dir, exist_ok=True)
    if logo_path:
        if os.path.exists(logo_path):
            ext = os.path.splitext(logo_path)[1].lower() or ".png"
            dst = os.path.join(out_dir, "logo" + ext)
            with open(logo_path, "rb") as f, open(dst, "wb") as g:
                g.write(f.read())
            info["logo"] = {"path": dst, "source": "用户提供的图片"}
        else:
            info["notes"].append("Logo 图片不存在：%s" % logo_path)
    elif z is not None:
        media, why = find_logo(z)
        if media:
            ext = os.path.splitext(media)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp"):
                dst = os.path.join(out_dir, "logo" + ext)
                with open(dst, "wb") as g:
                    g.write(z.read(media))
                info["logo"] = {"path": dst, "source": why}
            else:
                info["notes"].append("模板里的 Logo 是 %s 格式，暂不支持，请单独提供 png/jpg 图片" % ext)
        else:
            info["notes"].append(why + "；需要的话可以单独提供一张 Logo 图片")
    if info["logo"]:
        try:
            from PIL import Image
            with Image.open(info["logo"]["path"]) as im:
                info["logo"]["aspect"] = im.size[0] / float(im.size[1] or 1)
        except Exception:      # noqa
            info["logo"]["aspect"] = 2.5
    return info
