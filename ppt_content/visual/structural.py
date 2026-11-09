"""结构页：封面（S1）、目录（S2）、章节过渡页（S3）、结束页（S4）。"""
from pptx.enum.shapes import MSO_SHAPE

from .prim import (FONT, H, NUM_FONT, W, box, line, network, pill, pill_width, text, wave, fit_size, plain)


def _split_title(t, max_em=17):
    """长标题在括号或"与/和"附近断成两行，避免孤字。"""
    from .prim import em_width
    if em_width(t) <= max_em:
        return [t]
    for sep in ("（", "(", "——", "：", "与", "和", "及"):
        i = t.find(sep, int(len(t) * 0.35))
        if 0 < i < len(t) - 2:
            return [t[:i], t[i:]]
    mid = len(t) // 2
    return [t[:mid], t[mid:]]


def cover(d, s, title, subtitle=None, tag=None, fields=None):
    C = d.C
    box(s, 0, 0, W, H, grad=C["cover"], grad_angle=315)
    network(s, 11.0, 3.1, 1.75)
    wave(s)
    if tag:
        pill(d, s, 0.85, 1.55, pill_width(tag, 12, 0.5, 1.6), 0.38, tag, fill="FFFFFF", color=C["deep"], size=12)
    lines = _split_title(title)
    text(s, 0.85, 2.1, 9.6, 1.45, [[ln] for ln in lines], size=30, bold=True, color="FFFFFF", line_spacing=1.2, fit=22)
    if subtitle:
        text(s, 0.85, 3.62, 8.6, 0.6, "——  " + subtitle, size=22, color="FFFFFF", fit=16, max_lines=1)
    if fields:
        line(s, 0.85, 4.45, 5.6, 4.45, "FFFFFF", w=0.75, alpha=0.5)
        text(s, 0.85, 4.6, 8, 0.4 * len(fields) + 0.2, [["%s：%s" % (k, v)] for k, v in fields], size=13,
             color="FFFFFF", line_spacing=1.35)


def toc(d, s, chapters):
    C = d.C
    if not d.logo_master:                  # 母版放 Logo 时不铺整页白底（0.7.0-dev18）
        box(s, 0, 0, W, H, fill="FFFFFF")
    box(s, 0, 0, 4.3, H, grad=C["cover"], grad_angle=315)
    text(s, 1.05, 2.75, 3, 0.9, "目 录", size=40, bold=True, color="FFFFFF")
    text(s, 1.08, 3.65, 3, 0.4, "C O N T E N T S", size=14, color="FFFFFF")
    n = len(chapters)
    cols = 1 if n <= 3 else 2
    rows = (n + cols - 1) // cols
    cw = 7.9 if cols == 1 else 3.85
    ch, gx = 0.92, 0.3
    gy = 0.32 if rows <= 4 else 0.18
    ch = ch if rows <= 4 else 0.8
    y0 = (H - rows * ch - (rows - 1) * gy) / 2
    for i, t in enumerate(chapters):
        col, row = (i % cols, i // cols)
        x, y = 4.95 + col * (cw + gx), y0 + row * (ch + gy)
        box(s, x, y, cw, ch, fill=C["pale2"], line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
        box(s, x + 0.2, y + (ch - 0.56) / 2, 0.56, 0.56, grad=(C["light"], C["deep"]), grad_angle=45,
            shape=MSO_SHAPE.OVAL)
        text(s, x + 0.2, y + (ch - 0.56) / 2, 0.56, 0.56, "%02d" % (i + 1), size=15, bold=True, color="FFFFFF",
             align="center", valign="middle", font=NUM_FONT, margin=(0, 0, 0, 0), check=False)
        text(s, x + 0.95, y, cw - 1.05, ch, t, size=16, bold=True, color=C["title"], valign="middle", fit=12)


def section(d, s, num, title, items):
    C = d.C
    box(s, 0, 0, W, H, grad=C["cover"], grad_angle=315)
    network(s, 10.6, 3.4, 1.7)
    wave(s)
    text(s, 0.85, 1.75, 4, 0.5, "PART", size=18, color="FFFFFF", bold=True)
    text(s, 0.75, 2.1, 5, 1.5, "%02d" % num, size=96, bold=True, color="FFFFFF", font=NUM_FONT, margin=(0.05, 0, 0, 0),
         check=False)
    text(s, 0.85, 3.65, 8.6, 0.8, title, size=36, bold=True, color="FFFFFF", fit=26, max_lines=1)
    if items:
        items = items[:7]
        text(s, 0.88, 4.6, 8, 0.36 * len(items) + 0.2, [[it] for it in items], size=14, color="FFFFFF",
             line_spacing=1.4, fit=11)


def closing(d, s, head="汇报完毕", sub="恳请各位专家批评指正！", fields=None):
    C = d.C
    box(s, 0, 0, W, H, grad=C["cover"], grad_angle=315)
    network(s, 10.6, 3.2, 1.8)
    wave(s)
    text(s, 0.9, 2.3, 8, 1.0, head, size=44, bold=True, color="FFFFFF", fit=30, max_lines=1)
    if sub:
        text(s, 0.9, 3.35, 8.5, 0.7, sub, size=26, color="FFFFFF", fit=18, max_lines=1)
    if fields:
        text(s, 0.92, 4.5, 7, 0.4 * len(fields) + 0.2, [["%s：%s" % (k, v)] for k, v in fields], size=13,
             color="FFFFFF", line_spacing=1.35)


def section_toc(d, s, num, title, chapters, items):
    """过渡页（目录式，0.7）：左侧章号与章名，右侧全部章节、当前章高亮并列出本章各页。
    每张过渡页高亮的章不同，兼作进度提示；章数 ≥4 时默认用这种样式。"""
    C = d.C
    box(s, 0, 0, W, H, fill="FFFFFF")
    box(s, 0, 0, 4.6, H, grad=C["cover"], grad_angle=315)
    network(s, 3.6, 6.2, 1.0)
    text(s, 0.85, 1.9, 3.5, 0.5, "PART", size=18, color="FFFFFF", bold=True)
    text(s, 0.75, 2.3, 3.6, 1.5, "%02d" % num, size=88, bold=True, color="FFFFFF", font=NUM_FONT, margin=(0.05, 0, 0, 0),
         check=False)
    text(s, 0.85, 3.85, 3.5, 1.4, title, size=26, bold=True, color="FFFFFF", fit=18, line_spacing=1.2)
    n = len(chapters)
    cur = num - 1
    sub = (items or [])[:6]
    x = 5.3
    rh = 0.56
    sub_h = 0.36 * len(sub) + (0.12 if sub else 0)
    tot = n * rh + (n - 1) * 0.1 + sub_h
    y = max(0.5, (H - tot) / 2)
    for i, t in enumerate(chapters):
        on = i == cur
        if on:
            box(s, x, y, 7.2, rh, grad=(C["deep"], C["primary"]), grad_angle=0, shape=MSO_SHAPE.ROUNDED_RECTANGLE,
                radius=0.2)
        text(s, x + 0.2, y, 0.8, rh, "%02d" % (i + 1), size=18 if on else 15, bold=True,
             color="FFFFFF" if on else C["border"], valign="middle", font=NUM_FONT, check=False)
        text(s, x + 1.0, y, 6.0, rh, t, size=18 if on else 15, bold=on, color="FFFFFF" if on else C["muted"],
             valign="middle", fit=12, max_lines=1)
        y += rh + 0.1
        if on and sub:
            text(s, x + 1.0, y - 0.02, 6.0, sub_h, [[it] for it in sub], size=12.5, color=C["text"], line_spacing=1.35,
                 fit=10.5)
            line(s, x + 0.55, y, x + 0.55, y + sub_h - 0.12, C["border"], w=1.25)
            y += sub_h
