"""参数化版式库（版式目录 v1.2 的第一批 22 个）。

每个版式 = 名称 + 适用关系 + 内容槽位（schema）+ 绘制函数。
模型只负责"选版式、往槽位里填从本页内容压缩来的短语"，坐标、字号、颜色、图标全部由代码决定。

槽位写法：S(n) 文字，不超过 n 个字宽（汉字 1，西文约 0.55）；S(n, opt=True) 可省略；
L(最少, 最多, 子项) 列表；E(...) 枚举；O({...}) 可省略的对象。
"""
import math
import re

from pptx.enum.dml import MSO_LINE_DASH_STYLE as DASH
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from .prim import (CH, CW, FONT, cur_font, NUM_FONT, X0, X1, Y0, Y1, add_icon, arrow, badge, box, chev, em_width, head_body,
                   icon_circle, line, panel, pick_icons, picture, pill, pill_width, rgb, text, text_width)


# ------------------------------------------------------------------ 槽位定义
class S:
    def __init__(self, n, opt=False):
        self.n, self.opt = n, opt


class L:
    def __init__(self, lo, hi, item, opt=False):
        self.lo, self.hi, self.item, self.opt = lo, hi, item, opt


class E:
    def __init__(self, *vals, opt=False):
        self.vals, self.opt = vals, opt


class O:
    def __init__(self, fields, opt=True):
        self.fields, self.opt = fields, opt


class B:
    def __init__(self, opt=False):
        self.opt = opt


def validate(spec, schema, path="spec"):
    """返回 (问题列表, 规整后的 spec)。"""
    iss = []
    if isinstance(schema, dict):
        if not isinstance(spec, dict):
            return ["%s 应为对象" % path], {}
        out = {}
        for k, sub in schema.items():
            v = spec.get(k)
            if v in (None, "", [], {}):
                if not getattr(sub, "opt", False):
                    iss.append("%s.%s 缺失" % (path, k))
                continue
            i2, out[k] = validate(v, sub, "%s.%s" % (path, k))
            iss += i2
        return iss, out
    if isinstance(schema, O):
        return validate(spec, schema.fields, path)
    if isinstance(schema, S):
        if not isinstance(spec, (str, int, float)):
            return ["%s 应为文字" % path], ""
        v = str(spec).strip()
        w = em_width(v.replace("【", "").replace("】", ""))
        # 略超（≤15% 且不超过 3 字）不打回：是否放得下由试排判断（实测模型常为 1 个字重试 3 次仍超）
        if w > schema.n + max(1.0, min(3.0, schema.n * 0.15)) + 0.01:
            iss.append("%s 太长：“%s” 约 %.0f 字，限 %d 字以内，请压缩" % (path, v, w, schema.n))
        return iss, v
    if isinstance(schema, E):
        v = str(spec).strip()
        if v not in schema.vals:
            iss.append("%s 只能取 %s" % (path, "/".join(schema.vals)))
        return iss, v
    if isinstance(schema, B):
        return [], bool(spec) and spec not in (0, "0", "false", "否")
    if isinstance(schema, L):
        if not isinstance(spec, list):
            return ["%s 应为列表" % path], []
        if len(spec) < schema.lo or len(spec) > schema.hi:
            iss.append("%s 有 %d 项，本版式要求 %d–%d 项" % (path, len(spec), schema.lo, schema.hi))
        out = []
        for i, it in enumerate(spec[:schema.hi]):
            i2, v = validate(it, schema.item, "%s[%d]" % (path, i))
            iss += i2
            out.append(v)
        return iss, out
    return [], spec


def example(schema):
    """生成给模型看的槽位示例。"""
    if isinstance(schema, dict):
        return {(k + ("?" if getattr(v, "opt", False) else "")): example(v) for k, v in schema.items()}
    if isinstance(schema, O):
        return example(schema.fields)
    if isinstance(schema, S):
        return "≤%d字" % schema.n
    if isinstance(schema, E):
        return "|".join(schema.vals)
    if isinstance(schema, B):
        return "true|false"
    if isinstance(schema, L):
        return ["%d–%d 项" % (schema.lo, schema.hi), example(schema.item)]
    return None


LAYOUTS = {}


def layout(lid, name, family, relation, use, schema, paged=False):
    def deco(fn):
        LAYOUTS[lid] = dict(id=lid, name=name, family=family, relation=relation, use=use, schema=schema, render=fn,
                            paged=paged)
        return fn
    return deco


# ------------------------------------------------------------------ 共用小件
def chips_flow(d, s, x, y, w, h, items, size=12, fill=None, color=None, line_c=None, ch=0.38, gap=0.08, center=False):
    """胶囊流式排列；放不下时缩小字号。返回实际占用高度。"""
    C = d.C
    for sz in (size, size - 1, size - 2, 10):
        rows, cur, cw = [], [], 0
        for t in items:
            pw = min(w, pill_width(t, sz, 0.3, 0.55))
            if cur and cw + gap + pw > w:
                rows.append((cur, cw))
                cur, cw = [], 0
            cur.append((t, pw))
            cw += (gap if len(cur) > 1 else 0) + pw
        if cur:
            rows.append((cur, cw))
        need = len(rows) * ch + (len(rows) - 1) * gap
        if need <= h + 0.01 or sz == 10:
            break
    if need > h + 0.05:
        from .prim import _deck
        if _deck():
            _deck().issue("text_overflow", " / ".join(items), "胶囊需要 %.2fin 高，只有 %.2fin" % (need, h))
    yy = y
    for row, rw in rows:
        xx = x + ((w - rw) / 2 if center else 0)
        for t, pw in row:
            pill(d, s, xx, yy, pw, ch, t, fill=fill or C["pale"], color=color or C["deep"], size=sz,
                 line=line_c if line_c is not None else C["border"])
            xx += pw + gap
        yy += ch + gap
    return need


def card(d, s, x, y, w, h, fill="FFFFFF", line_c=None, radius=0.06):
    box(s, x, y, w, h, fill=fill, line=line_c or d.C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=radius)


def tone_color(C, tone, k=0):
    return {"alt": C["accent_a"], "warn": C["accent_b"], "deep": C["deep"]}.get(tone or "", C["primary"])


def footer_strip(d, s, ft, y=6.02, h=0.48):
    C = d.C
    box(s, X0, y, CW, h, fill=C["pale"], line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.3)
    text(s, X0 + 0.2, y, CW - 0.4, h, [(ft.get("label", "") + "  ", {"bold": True, "color": C["deep"]}),
                                       (ft.get("text", ""), {})], size=12.5, align="center", valign="middle", fit=10.5,
         max_lines=1)


FOOTER = O({"label": S(6, opt=True), "text": S(64)})


# ================================================================== L39 纯文字要点
@layout("L39", "结论块 + 编号要点", "text", "文字要点（无明显结构）",
        "内容是几条并列判断、没有明显的结构关系时；也是兜底版式",
        {"lead": S(34), "sub": S(56, opt=True), "items": L(2, 6, {"head": S(10, opt=True), "body": S(48)})})
def r_points(d, s, sp):
    C = d.C
    if (sp.get("_k") or {}).get("v") == 1:
        return r_points_cards(d, s, sp)
    box(s, X0, Y0, 3.9, CH - 0.05, grad=(C["deep"], C["primary"]), grad_angle=315,
        shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
    add_icon(s, "format-quote-open", X0 + 0.3, Y0 + 0.3, 0.6, "FFFFFF")
    text(s, X0 + 0.3, Y0 + 1.05, 3.3, 2.1, sp["lead"], size=21, bold=True, color="FFFFFF", line_spacing=1.3, fit=15)
    if sp.get("sub"):
        box(s, X0 + 0.3, Y0 + 3.25, 0.6, 0.05, fill="FFFFFF")
        text(s, X0 + 0.3, Y0 + 3.45, 3.3, 1.4, sp["sub"], size=12.5, color=C["on_dark"], line_spacing=1.3, fit=10.5)
    items = sp["items"]
    n = len(items)
    x, w = 5.0, X1 - 5.0
    ih = (CH - 0.05) / n
    for k, it in enumerate(items):
        y = Y0 + k * ih
        text(s, x, y, 0.9, ih, "%02d" % (k + 1), size=min(30, 20 + ih * 8), bold=True, color=C["border"],
             font=NUM_FONT, valign="middle", check=False)
        head_body(d, s, x + 0.95, y + 0.08, w - 1.0, ih - 0.16, it.get("head"), it.get("body"), hsize=16, bsize=13)
        if k < n - 1:
            line(s, x + 0.95, y + ih, X1, y + ih, C["line"], w=0.75)


def r_points_cards(d, s, sp):
    """L39 变体 1：顶部结论条 + 两列编号卡片（同一套里第二次出现 L39 时用，避免兜底页长得一样）。"""
    C = d.C
    lh = 0.95 if sp.get("sub") else 0.72
    box(s, X0, Y0, CW, lh, fill=C["pale"], line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
    box(s, X0, Y0, 0.1, lh, fill=C["primary"])
    add_icon(s, "format-quote-open", X0 + 0.28, Y0 + lh / 2 - 0.2, 0.4, C["primary"])
    if sp.get("sub"):
        text(s, X0 + 0.85, Y0 + 0.06, CW - 1.05, 0.48, sp["lead"], size=17, bold=True, color=C["deep"], valign="middle",
             fit=13, max_lines=1)
        text(s, X0 + 0.85, Y0 + 0.52, CW - 1.05, 0.38, sp["sub"], size=12, color=C["gray"], valign="middle", fit=10,
             max_lines=1)
    else:
        text(s, X0 + 0.85, Y0, CW - 1.05, lh, sp["lead"], size=17, bold=True, color=C["deep"], valign="middle", fit=13,
             max_lines=1)
    items = sp["items"]
    n = len(items)
    cols = 2 if n >= 4 else 1
    rows = (n + cols - 1) // cols
    top = Y0 + lh + 0.22
    gx, gy = 0.25, 0.16
    cw = (CW - gx * (cols - 1)) / cols
    ch = (Y1 - top - gy * (rows - 1)) / rows
    for k, it in enumerate(items):
        r, c = (k // cols, k % cols) if cols == 2 else (k, 0)
        x, y = X0 + c * (cw + gx), top + r * (ch + gy)
        card(d, s, x, y, cw, ch, line_c=C["line"])
        sz = min(0.5, ch - 0.3)
        badge(d, s, x + 0.2, y + (ch - sz) / 2, sz, k + 1, fill=C["primary"] if k % 2 == 0 else C["deep"], fsize=13)
        head_body(d, s, x + sz + 0.38, y + 0.08, cw - sz - 0.55, ch - 0.16, it.get("head"), it.get("body"), hsize=15,
                  bsize=12.5)


# ================================================================== L05 多列图标卡 + 子项
@layout("L05", "多列图标卡 + 子项", "cards", "并列",
        "3–5 个并列的组成部分（建设任务、功能模块、能力项），每个下面有若干子项",
        {"columns": L(3, 5, {"title": S(8), "subtitle": S(14, opt=True), "items": L(0, 5, S(11), opt=True),
                             "tone": E("main", "alt", opt=True)}),
         "footer": FOOTER})
def r_columns(d, s, sp):
    from .knobs import split
    C = d.C
    K = sp.get("_k") or {}
    v, emph = K.get("v", 0), K.get("e")
    cols = sp["columns"]
    n = len(cols)
    gap = 0.18
    ws = split(K.get("w"), n, CW, gap)
    bottom = 5.9 if sp.get("footer") else 6.5
    icons = pick_icons([c["title"] + c.get("subtitle", "") for c in cols])
    x = X0
    for k, c in enumerate(cols):
        cw = ws[k]
        col = tone_color(C, c.get("tone"))
        alt = c.get("tone") == "alt"
        hot = emph == k
        if hot:
            box(s, x, Y0, cw, bottom - Y0, fill=C["pale"], line=col, lw=2, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
        else:
            card(d, s, x, Y0, cw, bottom - Y0)
        if v == 1:
            # 变体 1：整条色头（图标 + 标题在色头内，左对齐），子项为圆点清单
            box(s, x, Y0, cw, 0.66, fill=col, shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.1)
            add_icon(s, icons[k], x + 0.16, Y0 + 0.14, 0.38)
            text(s, x + 0.62, Y0, cw - 0.72, 0.66, c["title"], size=15, bold=True, color="FFFFFF", valign="middle",
                 fit=11.5, max_lines=1)
            yy = Y0 + 0.78
        elif v == 2:
            # 变体 2：左侧竖色条 + 大号序号 + 左对齐标题，子项为细线分隔的行
            box(s, x, Y0 + 0.12, 0.07, bottom - Y0 - 0.24, fill=col)
            text(s, x + 0.2, Y0 + 0.12, 0.9, 0.6, "%02d" % (k + 1), size=26, bold=True, color=C["border"] if not hot
                 else col, font=NUM_FONT, valign="middle", check=False)
            text(s, x + 0.2, Y0 + 0.72, cw - 0.35, 0.42, c["title"], size=15, bold=True,
                 color=col if (alt or hot) else C["deep"], valign="middle", fit=11.5, max_lines=1)
            box(s, x + 0.22, Y0 + 1.16, 0.5, 0.04, fill=col)
            yy = Y0 + 1.28
        else:
            box(s, x, Y0, cw, 0.08, fill=col)
            icon_circle(d, s, x + cw / 2 - 0.33, Y0 + 0.25, 0.66, icons[k],
                        *((C["accent_a"], "128A47") if alt else (None, None)))
            text(s, x + 0.1, Y0 + 0.98, cw - 0.2, 0.42, c["title"], size=15, bold=True,
                 color=col if (alt or hot) else C["deep"], align="center", valign="middle", fit=12, max_lines=1)
            yy = Y0 + 1.4
        if c.get("subtitle"):
            text(s, x + 0.12, yy, cw - 0.24, 0.34, c["subtitle"], size=10.5, color=C["gray"],
                 align="left" if v else "center", valign="middle", fit=9.5, max_lines=1)
            yy += 0.42
        items = c.get("items") or []
        if items:
            avail = bottom - 0.18 - yy
            ih = min(0.42 if v != 2 else 0.5, (avail - 0.1 * (len(items) - 1)) / len(items))
            for j, it in enumerate(items):
                iy = yy + j * (ih + 0.1)
                if v == 1:
                    box(s, x + 0.2, iy + ih / 2 - 0.05, 0.1, 0.1, fill=col, shape=MSO_SHAPE.OVAL)
                    text(s, x + 0.38, iy, cw - 0.5, ih, it, size=12, color=C["text"], valign="middle", fit=10,
                         max_lines=1)
                elif v == 2:
                    text(s, x + 0.2, iy, cw - 0.32, ih, it, size=12, color=C["text"], valign="middle", fit=10,
                         max_lines=1)
                    if j < len(items) - 1:
                        line(s, x + 0.2, iy + ih + 0.05, x + cw - 0.15, iy + ih + 0.05, C["line"], w=0.5)
                else:
                    box(s, x + 0.15, iy, cw - 0.3, ih, fill=C["accent_a_pale"] if alt else C["pale2"],
                        line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.15)
                    text(s, x + 0.2, iy, cw - 0.4, ih, it, size=12, color=C["text"], align="center", valign="middle",
                         fit=10, max_lines=1)
        x += cw + gap
    if sp.get("footer"):
        footer_strip(d, s, sp["footer"])


# ================================================================== L06 多栏面板（标题条 + 要点）
@layout("L06", "多栏能力面板", "panels", "并列",
        "2–4 个并列能力/方面，每个有 2–4 条“小标题 + 说明”",
        {"panels": L(2, 4, {"title": S(10), "items": L(1, 4, {"head": S(10), "body": S(36, opt=True)}),
                            "tone": E("main", "alt", "warn", opt=True)})})
def r_panels(d, s, sp):
    from .knobs import split
    C = d.C
    K = sp.get("_k") or {}
    v, emph = K.get("v", 0), K.get("e")
    ps = sp["panels"]
    n = len(ps)
    gap = 0.25
    ws = split(K.get("w"), n, CW, gap)
    icons = pick_icons([p["title"] for p in ps])
    x = X0
    for k, p in enumerate(ps):
        pw = ws[k]
        col = tone_color(C, p.get("tone"))
        hot = emph == k
        if hot:
            box(s, x, Y0, pw, CH - 0.05, fill=C["pale2"], line=col, lw=2, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
        else:
            card(d, s, x, Y0, pw, CH - 0.05)
        if v == 1:
            # 变体 1：白底标题 + 图标圆 + 标题下粗色线
            icon_circle(d, s, x + 0.2, Y0 + 0.14, 0.5, icons[k])
            text(s, x + 0.8, Y0 + 0.1, pw - 0.95, 0.58, p["title"], size=15, bold=True, color=col if hot else C["deep"],
                 valign="middle", fit=12, max_lines=1)
            box(s, x + 0.2, Y0 + 0.74, pw - 0.4, 0.05, fill=col)
            top = Y0 + 0.92
        elif v == 2:
            # 变体 2：左侧色条 + 序号 + 标题
            box(s, x, Y0, 0.1, CH - 0.05, fill=col, shape=MSO_SHAPE.RECTANGLE)
            text(s, x + 0.25, Y0 + 0.08, 0.7, 0.6, "%02d" % (k + 1), size=22, bold=True, color=col, font=NUM_FONT,
                 valign="middle", check=False)
            text(s, x + 0.9, Y0 + 0.08, pw - 1.05, 0.6, p["title"], size=15, bold=True, color=C["deep"],
                 valign="middle", fit=12, max_lines=1)
            line(s, x + 0.25, Y0 + 0.78, x + pw - 0.2, Y0 + 0.78, C["line"], w=0.75)
            top = Y0 + 0.9
        else:
            box(s, x, Y0, pw, 0.62, fill=col, shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.1)
            add_icon(s, icons[k], x + 0.22, Y0 + 0.12, 0.38)
            text(s, x + 0.7, Y0, pw - 0.8, 0.62, p["title"], size=15, bold=True, color="FFFFFF", valign="middle",
                 fit=12, max_lines=1)
            top = Y0 + 0.8
        items = p["items"]
        ih = (Y1 - 0.15 - top) / len(items)
        lx = x + (0.3 if v == 2 else 0.2)
        for j, it in enumerate(items):
            y = top + j * ih
            box(s, lx, y + 0.14, 0.12, 0.12, fill=col, shape=MSO_SHAPE.OVAL)
            head_body(d, s, lx + 0.22, y, pw - (lx - x) - 0.4, ih - 0.1, it["head"], it.get("body"), hsize=14, bsize=12,
                      hcolor=col if p.get("tone") in ("alt", "warn") else C["deep"])
            if j < len(items) - 1:
                line(s, lx, y + ih - 0.05, x + pw - 0.2, y + ih - 0.05, C["line"], w=0.5)
        x += pw + gap


# ================================================================== L09 双面板图标清单
@layout("L09", "双面板图标清单", "panels", "并列（二分）",
        "内容天然分成两组（如质量控制 / 安全保障、软件 / 数据），每组 3–5 条",
        {"panels": L(2, 2, {"title": S(12), "items": L(2, 5, {"head": S(10), "body": S(36, opt=True)})})})
def r_two_panels(d, s, sp):
    from .knobs import split
    C = d.C
    K = sp.get("_k") or {}
    v = K.get("v", 0)
    gap = 0.3
    ws = split(K.get("w"), 2, CW, gap)
    x = X0
    for k, p in enumerate(sp["panels"]):
        pw = ws[k]
        col = C["primary"] if k == 0 else C["accent_a"]
        if v == 2:
            # 变体 2（0.7.0-dev16）：浅色底 + 左侧色条，标题彩色字；内容起点与默认相同
            box(s, x, Y0, pw, CH - 0.05, fill=C["pale2"] if k == 0 else C["accent_a_pale"], line=C["line"], lw=0.75,
                shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.03)
            box(s, x, Y0 + 0.12, 0.09, CH - 0.29, fill=col)
            text(s, x + 0.25, Y0 + 0.14, pw - 0.5, 0.44, p["title"], size=15.5, bold=True, color=col, valign="middle",
                 fit=12, max_lines=1)
            top = Y0 + 0.7
        elif v == 1:
            # 变体 1：整条色头 + 白字标题，条目用编号圆标
            card(d, s, x, Y0, pw, CH - 0.05)
            box(s, x, Y0, pw, 0.6, fill=col, shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.08)
            text(s, x + 0.25, Y0, pw - 0.5, 0.6, p["title"], size=15.5, bold=True, color="FFFFFF", valign="middle",
                 fit=12, max_lines=1)
            top = Y0 + 0.75
        else:
            top = panel(d, s, x, Y0, pw, CH - 0.05, p["title"], color=col)
        items = p["items"]
        ih = (Y1 - 0.15 - top) / len(items)
        icons = pick_icons([it["head"] for it in items])
        for j, it in enumerate(items):
            y = top + j * ih
            sz = min(0.62, ih - 0.15)
            if v == 1:
                sz = min(0.46, ih - 0.2)
                badge(d, s, x + 0.25, y + (ih - sz) / 2, sz, j + 1, fill=col, fsize=12)
                if j < len(items) - 1:
                    line(s, x + 0.25, y + ih, x + pw - 0.25, y + ih, C["line"], w=0.5)
            else:
                icon_circle(d, s, x + 0.25, y + (ih - sz) / 2, sz, icons[j],
                            *((C["accent_a"], "128A47") if k == 1 else (None, None)))
            head_body(d, s, x + 0.4 + sz, y + 0.04, pw - 0.65 - sz, ih - 0.08, it["head"], it.get("body"),
                      hsize=14, bsize=12, hcolor=C["title"])
        x += pw + gap


# ================================================================== L03 问题 → 目标
@layout("L03", "问题 → 目标 双面板", "cause", "背景/因果",
        "背景页：政策要求 + 现存问题 → 建设目标",
        {"context_label": S(6, opt=True), "context": S(60, opt=True),
         "problems_label": S(6, opt=True), "problems": L(2, 4, {"head": S(10), "body": S(18, opt=True)}),
         "goals_label": S(6, opt=True), "goals": L(2, 4, {"head": S(12), "body": S(40, opt=True)})})
def r_problem_goal(d, s, sp):
    C = d.C
    v = (sp.get("_k") or {}).get("v", 0)        # 外观变体（0.7.0-dev16）：1 = 编号圆标、问题卡白底左色条，文字位置不变
    lw = 5.15
    y = Y0
    if sp.get("context"):
        top = panel(d, s, X0, y, lw, 1.15, sp.get("context_label") or "政策要求")
        text(s, X0 + 0.2, top - 0.05, lw - 0.4, 1.15 - (top - y) - 0.02, sp["context"], size=12, fit=10.5)
        y += 1.3
    top = panel(d, s, X0, y, lw, Y1 - y, sp.get("problems_label") or "现存问题", color=C["accent_b"])
    pr = sp["problems"]
    rows = (len(pr) + 1) // 2
    cols = 2 if len(pr) > 1 else 1
    gw = (lw - 0.4 - 0.2 * (cols - 1)) / cols
    gh = (Y1 - 0.15 - top - 0.18 * (rows - 1)) / rows
    icons = pick_icons([p["head"] for p in pr])
    for k, p in enumerate(pr):
        cx, cy = X0 + 0.2 + (k % cols) * (gw + 0.2), top + (k // cols) * (gh + 0.18)
        sz = min(0.6, gh - 0.2)
        if v == 1:
            box(s, cx, cy, gw, gh, fill="FFFFFF", line=C["accent_b_border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
            box(s, cx, cy + 0.08, 0.07, gh - 0.16, fill=C["accent_b"])
            badge(d, s, cx + 0.12 + sz * 0.1, cy + (gh - sz * 0.8) / 2, sz * 0.8, k + 1, fill=C["accent_b"], fsize=12)
        else:
            box(s, cx, cy, gw, gh, fill=C["accent_b_pale"], line=C["accent_b_border"],
                shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
            box(s, cx + 0.12, cy + (gh - sz) / 2, sz, sz, fill=C["accent_b"], shape=MSO_SHAPE.HEXAGON)
            add_icon(s, icons[k], cx + 0.12 + sz * 0.22, cy + (gh - sz) / 2 + sz * 0.22, sz * 0.56)
        head_body(d, s, cx + sz + 0.22, cy + 0.06, gw - sz - 0.3, gh - 0.12, p["head"], p.get("body"), hsize=13.5,
                  bsize=11, hcolor=C["accent_b_dark"])
    chev(s, X0 + lw + 0.18, (Y0 + Y1) / 2 - 0.35, 0.42, 0.7, C["light"])
    rx = X0 + lw + 0.78
    rw = X1 - rx
    top = panel(d, s, rx, Y0, rw, CH - 0.05, sp.get("goals_label") or "建设目标")
    gl = sp["goals"]
    ih = (Y1 - 0.15 - top) / len(gl)
    icons = pick_icons([g["head"] + g.get("body", "") for g in gl])
    for k, g in enumerate(gl):
        y = top + k * ih
        sz = min(0.7, ih - 0.2)
        if v == 1:
            badge(d, s, rx + 0.25 + sz * 0.1, y + 0.05 + sz * 0.1, sz * 0.8, k + 1, fsize=14)
        else:
            icon_circle(d, s, rx + 0.25, y + 0.05, sz, icons[k])
        head_body(d, s, rx + 0.45 + sz, y, rw - 0.65 - sz, ih - 0.1, g["head"], g.get("body"), hsize=15, bsize=12.5)


# ================================================================== L26 问题 → 对策 成对行
@layout("L26", "问题 → 对策 成对行", "compare", "对比/问题-对策",
        "重难点与应对、风险与措施、问题与解决办法",
        {"left_label": S(6, opt=True), "right_label": S(6, opt=True),
         "rows": L(2, 5, {"problem": S(14), "detail": S(26, opt=True), "solution": S(60)})})
def r_problem_solution(d, s, sp):
    C = d.C
    lw, gap = 4.4, 0.55
    rx = X0 + lw + gap
    rw = X1 - rx
    text(s, X0, Y0, lw, 0.32, sp.get("left_label") or "重难点", size=12, bold=True, color=C["gray"], align="center",
         valign="middle")
    text(s, rx, Y0, rw, 0.32, sp.get("right_label") or "应对措施", size=12, bold=True, color=C["gray"], align="center",
         valign="middle")
    rows = sp["rows"]
    top = Y0 + 0.4
    rh = (Y1 - top - 0.12 * (len(rows) - 1)) / len(rows)
    for k, r in enumerate(rows):
        y = top + k * (rh + 0.12)
        box(s, X0, y, lw, rh, fill=C["accent_b_pale"], line=C["accent_b_border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.1)
        sz = min(0.5, rh - 0.2)
        badge(d, s, X0 + 0.18, y + (rh - sz) / 2, sz, "%02d" % (k + 1), fill=C["accent_b"], fsize=12)
        head_body(d, s, X0 + sz + 0.3, y + 0.05, lw - sz - 0.42, rh - 0.1, r["problem"], r.get("detail"), hsize=14,
                  bsize=11, hcolor=C["accent_b_dark"])
        chev(s, X0 + lw + 0.14, y + rh / 2 - 0.2, 0.26, 0.4, C["accent_b"])
        card(d, s, rx, y, rw, rh, line_c=C["border"], radius=0.08)
        text(s, rx + 0.2, y, rw - 0.4, rh, r["solution"], size=12.5, valign="middle", fit=10.5, line_spacing=1.15,
             red=C["red"])


# ================================================================== L33 现状 vs 目标（按维度）
@layout("L33", "按维度逐行对照", "compare", "对比（现状 vs 目标 / 前后）",
        "同一组维度上的“现状 → 目标”或“以前 → 现在”对照",
        {"left_label": S(8), "right_label": S(8),
         "rows": L(2, 5, {"dim": S(6), "left_head": S(14), "left_body": S(30, opt=True), "right_head": S(16),
                          "right_body": S(36, opt=True)})})
def r_compare_rows(d, s, sp):
    C = d.C
    dw, lw, aw = 1.45, 4.2, 1.1
    lx = X0 + dw + 0.15
    rx = lx + lw + aw
    rw = X1 - rx
    pill(d, s, lx, Y0, lw, 0.42, sp["left_label"], fill=C["accent_b"], size=13.5)
    pill(d, s, rx, Y0, rw, 0.42, sp["right_label"], fill=C["primary"], size=13.5)
    rows = sp["rows"]
    top = Y0 + 0.55
    rh = (Y1 - top - 0.1 * (len(rows) - 1)) / len(rows)
    icons = pick_icons([r["dim"] for r in rows])
    for k, r in enumerate(rows):
        y = top + k * (rh + 0.1)
        box(s, X0, y, dw, rh, fill=C["deep"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.1)
        isz = min(0.42, rh * 0.4)
        add_icon(s, icons[k], X0 + (dw - isz) / 2, y + rh * 0.14, isz)
        text(s, X0, y + rh * 0.55, dw, rh * 0.4, r["dim"], size=13.5, bold=True, color="FFFFFF", align="center",
             valign="middle", fit=11, max_lines=1)
        box(s, lx, y, lw, rh, fill=C["accent_b_pale"], line=C["accent_b_border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.08)
        head_body(d, s, lx + 0.2, y + 0.06, lw - 0.4, rh - 0.12, r["left_head"], r.get("left_body"), hsize=14,
                  bsize=11.5, hcolor=C["accent_b_dark"])
        chev(s, lx + lw + 0.25, y + rh / 2 - 0.25, 0.6, 0.5, C["light"])
        box(s, rx, y, rw, rh, fill=C["pale"], line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        head_body(d, s, rx + 0.2, y + 0.06, rw - 0.4, rh - 0.12, r["right_head"], r.get("right_body"), hsize=14,
                  bsize=11.5)


# ================================================================== L12 泳道步骤
@layout("L12", "分阶段步骤泳道", "flow", "递进/流程（分阶段）",
        "技术路线、实施路径：1–3 个阶段，每阶段 2–5 个步骤；可在顶部列“针对的问题”",
        {"top": O({"label": S(8), "items": L(2, 5, S(10))}),
         "lanes": L(1, 3, {"name": S(6), "steps": L(2, 5, {"head": S(8), "body": S(32, opt=True)})})})
def r_lanes(d, s, sp):
    C = d.C
    v = (sp.get("_k") or {}).get("v", 0)        # 外观变体（0.7.0-dev16）：1 = 步骤卡彩色标题带；2 = 浅底步骤卡 + 左色条；文字位置不变
    y = Y0
    if sp.get("top"):
        t = sp["top"]
        text(s, X0, y, 1.5, 0.36, t["label"], size=12.5, bold=True, color=C["deep"], valign="middle", fit=10.5,
             max_lines=1)
        n = len(t["items"])
        iw = (CW - 1.6 - 0.15 * (n - 1)) / n
        for k, it in enumerate(t["items"]):
            pill(d, s, X0 + 1.6 + k * (iw + 0.15), y, iw, 0.36, it, fill=C["accent_b_pale"], color=C["accent_b_dark"],
                 size=12, line=C["accent_b_border"])
        y += 0.5
    lanes = sp["lanes"]
    lh = (Y1 - y - 0.15 * (len(lanes) - 1)) / len(lanes)
    for li, ln in enumerate(lanes):
        ly = y + li * (lh + 0.15)
        box(s, X0, ly, 0.5, lh, fill=C["primary"] if li % 2 == 0 else C["deep"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.12)
        text(s, X0, ly, 0.5, lh, ln["name"], size=13, bold=True, color="FFFFFF", vert=True, valign="middle",
             fit=10)
        box(s, X0 + 0.58, ly, CW - 0.58, lh, fill="FFFFFF" if v == 2 else C["pale2"], line=C["line"],
            shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
        st = ln["steps"]
        n = len(st)
        aw = 0.3
        sw = (CW - 0.58 - 0.3 - aw * (n - 1)) / n
        for k, it in enumerate(st):
            x = X0 + 0.73 + k * (sw + aw)
            card(d, s, x, ly + 0.15, sw, lh - 0.3, fill=C["pale"] if v == 2 else "FFFFFF", line_c=C["border"])
            if v == 2:
                box(s, x, ly + 0.15, 0.07, lh - 0.3, fill=C["primary"] if li % 2 == 0 else C["deep"])
            if v == 1:
                box(s, x, ly + 0.15, sw, 0.55, fill=C["primary"] if li % 2 == 0 else C["deep"],
                    shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.1)
                badge(d, s, x + 0.12, ly + 0.27, 0.32, k + 1, fill="FFFFFF", color=C["primary"] if li % 2 == 0 else C["deep"],
                      fsize=11)
            else:
                badge(d, s, x + 0.12, ly + 0.27, 0.32, k + 1, fill=C["accent_b"] if v == 2 else C["primary"], fsize=11)
            if v == 2:
                line(s, x + 0.15, ly + 0.68, x + sw - 0.15, ly + 0.68, C["primary"], w=1.25)
            text(s, x + 0.5, ly + 0.22, sw - 0.58, 0.42, it["head"], size=13.5, bold=True,
                 color="FFFFFF" if v == 1 else C["deep"], valign="middle", fit=11, max_lines=1)
            if it.get("body"):
                text(s, x + 0.12, ly + 0.7, sw - 0.24, lh - 0.95, it["body"], size=11.5, fit=10, line_spacing=1.15,
                     grow=13.5)
            if k < n - 1:
                chev(s, x + sw + 0.06, ly + lh / 2 - 0.16, 0.18, 0.32, C["mid"])


# ================================================================== L13 编号节点流程
@layout("L13", "编号节点流程", "flow", "递进/流程（单线）",
        "一条线的 3–6 个步骤（流程、方法、工作步骤）",
        {"steps": L(3, 6, {"head": S(10), "body": S(42, opt=True)}), "footer": FOOTER})
def r_process(d, s, sp):
    if (sp.get("_k") or {}).get("o") == "v":
        return r_process_vertical(d, s, sp)
    C = d.C
    st = sp["steps"]
    n = len(st)
    gap = 0.2
    sw = (CW - gap * (n - 1)) / n
    bottom = 5.9 if sp.get("footer") else 6.5
    # 说明卡高度按最长说明估算（够用即可，避免大片留白），再把整组在可用高度内垂直居中
    from .prim import lines_needed
    need = max([lines_needed([x.get("body", "")], 12.5, sw - 0.3) for x in st if x.get("body")] or [0])
    body_h = min(bottom - (Y0 + 1.5), max(1.1, need * 12.5 * 1.2 * 1.2 / 72 + 0.5))
    block = 1.3 + (body_h if need else 0.5)
    ny = Y0 + 0.2 + max(0, (bottom - Y0 - 0.2 - block) / 2 - 0.1)
    v = (sp.get("_k") or {}).get("v", 0)        # 外观变体（0.7.0-dev16）：2 = 大编号节点 + 实线 + 白底色条说明卡，文字位置不变
    line(s, X0 + sw / 2, ny + 0.33, X1 - sw / 2, ny + 0.33, C["primary"] if v == 2 else C["border"], w=2 if v == 2 else 1.5,
         dash=None if v == 2 else DASH.DASH)
    icons = pick_icons([x["head"] for x in st])
    for k, it in enumerate(st):
        cx = X0 + k * (sw + gap) + sw / 2
        box(s, cx - 0.36, ny - 0.03, 0.72, 0.72, fill="FFFFFF", shape=MSO_SHAPE.OVAL)
        if v == 2:
            badge(d, s, cx - 0.33, ny, 0.66, k + 1, fill=C["primary"] if k % 2 == 0 else C["deep"], fsize=20)
        else:
            icon_circle(d, s, cx - 0.33, ny, 0.66, icons[k])
            badge(d, s, cx + 0.18, ny - 0.1, 0.3, k + 1, fill=C["accent_b"], fsize=10)
        text(s, cx - sw / 2, ny + 0.78, sw, 0.42, it["head"], size=15, bold=True, color=C["deep"], align="center",
             valign="middle", fit=12, max_lines=1)
        if it.get("body"):
            y = ny + 1.3
            bh = body_h
            box(s, cx - sw / 2, y, sw, bh, fill="FFFFFF" if v == 2 else C["pale2"], line=C["border"] if v == 2 else C["line"],
                shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
            if v == 2:
                box(s, cx - sw / 2, y, sw, 0.07, fill=C["primary"] if k % 2 == 0 else C["deep"])
            text(s, cx - sw / 2 + 0.15, y + 0.12, sw - 0.3, bh - 0.24, it["body"], size=12.5, fit=10.5,
                 line_spacing=1.2, grow=14)
        if k < n - 1:
            chev(s, cx + sw / 2 + gap / 2 - 0.08, ny + 0.2, 0.16, 0.26, C["mid"])
    if sp.get("footer"):
        footer_strip(d, s, sp["footer"])


def r_process_vertical(d, s, sp):
    """竖排步骤：左侧编号节点 + 竖向连线，右侧“步骤名 | 说明”横条。步骤说明较长时用。"""
    C = d.C
    st = sp["steps"]
    n = len(st)
    bottom = 5.9 if sp.get("footer") else 6.5
    gap = 0.12
    rh = (bottom - Y0 - gap * (n - 1)) / n
    rh = min(rh, 1.1)
    top = Y0 + max(0, (bottom - Y0 - (rh * n + gap * (n - 1))) / 2)
    cx = X0 + 0.45
    line(s, cx, top + rh / 2, cx, top + (n - 1) * (rh + gap) + rh / 2, C["border"], w=1.5, dash=DASH.DASH)
    icons = pick_icons([x["head"] for x in st])
    hw = 2.6
    for k, it in enumerate(st):
        y = top + k * (rh + gap)
        sz = min(0.62, rh - 0.12)
        box(s, cx - sz / 2 - 0.03, y + (rh - sz) / 2 - 0.03, sz + 0.06, sz + 0.06, fill="FFFFFF", shape=MSO_SHAPE.OVAL)
        icon_circle(d, s, cx - sz / 2, y + (rh - sz) / 2, sz, icons[k])
        badge(d, s, cx + sz / 2 - 0.16, y + (rh - sz) / 2 - 0.08, 0.28, k + 1, fill=C["accent_b"], fsize=9.5)
        x = cx + 0.6
        box(s, x, y, X1 - x, rh, fill=C["pale2"] if k % 2 == 0 else "FFFFFF", line=C["line"],
            shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        box(s, x, y, 0.07, rh, fill=C["primary"])
        text(s, x + 0.2, y, hw - 0.2, rh, it["head"], size=15, bold=True, color=C["deep"], valign="middle", fit=12,
             max_lines=2)
        if it.get("body"):
            line(s, x + hw, y + 0.15, x + hw, y + rh - 0.15, C["border"], w=0.75)
            text(s, x + hw + 0.2, y + 0.04, X1 - x - hw - 0.35, rh - 0.08, it["body"], size=13, valign="middle",
                 fit=10.5, line_spacing=1.2)
    if sp.get("footer"):
        footer_strip(d, s, sp["footer"])


# ================================================================== L17 分层架构
@layout("L17", "分层架构带", "arch", "层级/架构",
        "总体架构、技术架构：2–5 层，layers 按自上而下排列（应用层在最上、基础设施在最下），每层若干组成；可有一条贯穿各层的竖向能力（安全、运维）",
        {"layers": L(2, 5, {"name": S(7), "items": L(1, 8, S(8)), "note": S(30, opt=True)}),
         "side": S(10, opt=True)})
def r_layers(d, s, sp):
    C = d.C
    layers = list(sp["layers"])
    # 约定自上而下（应用在上、基础设施在下）；模型按自下而上给出时自动翻转
    if len(layers) > 1 and (re.search(r"基础|设施|硬件|资源层", layers[0]["name"]) or
                            re.search(r"应用|用户|场景|展示|门户", layers[-1]["name"])):
        layers.reverse()
    n = len(layers)
    lab = 1.35
    side_w = 0.62 if sp.get("side") else 0
    bx = X0 + lab + 0.1
    bw = X1 - bx - (side_w + 0.1 if side_w else 0)
    gap = 0.12
    lh = (CH - 0.05 - gap * (n - 1)) / n
    for k, ly in enumerate(layers):
        y = Y0 + k * (lh + gap)
        box(s, X0, y, lab, lh, fill=C["deep"] if k % 2 == 0 else C["primary"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.08)
        text(s, X0 + 0.05, y, lab - 0.1, lh, ly["name"], size=14, bold=True, color="FFFFFF", align="center",
             valign="middle", fit=11)
        box(s, bx, y, bw, lh, fill=C["pale2"], line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
        ix0, iw_all = bx + 0.15, bw - 0.3
        if ly.get("note"):          # 说明放在层带左侧一栏，不占纵向空间
            nw = min(2.4, bw * 0.24)
            text(s, bx + 0.12, y + 0.06, nw - 0.1, lh - 0.12, ly["note"], size=11, color=C["gray"], valign="middle",
                 fit=9.5, line_spacing=1.1)
            line(s, bx + nw + 0.05, y + 0.12, bx + nw + 0.05, y + lh - 0.12, C["line"], w=0.75)
            ix0, iw_all = bx + nw + 0.15, bw - nw - 0.3
        items = ly["items"]
        m = len(items)
        ig = 0.12
        need = max(text_width(it, 11, True) for it in items) + 0.2
        per = m if (iw_all - ig * (m - 1)) / m >= need or m <= 4 else math.ceil(m / 2)
        rows = math.ceil(m / per)
        iw = (iw_all - ig * (per - 1)) / per
        ih = min(0.5, (lh - 0.24 - 0.08 * (rows - 1)) / rows)
        oy = y + (lh - (rows * ih + 0.08 * (rows - 1))) / 2
        for j, it in enumerate(items):
            r, c = j // per, j % per
            ix = ix0 + c * (iw + ig)
            iy = oy + r * (ih + 0.08)
            box(s, ix, iy, iw, ih, fill="FFFFFF" if k % 2 else C["primary"], line=C["border"] if k % 2 else None,
                shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.15)
            text(s, ix + 0.04, iy, iw - 0.08, ih, it, size=12.5, bold=True, color=C["deep"] if k % 2 else "FFFFFF",
                 align="center", valign="middle", fit=10, max_lines=2, line_spacing=1.0)
    if side_w:
        sx = X1 - side_w
        box(s, sx, Y0, side_w, CH - 0.05, fill=C["primary"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
        text(s, sx, Y0 + 0.1, side_w, CH - 0.25, sp["side"], size=14, bold=True, color="FFFFFF", vert=True,
             valign="middle", fit=11)


# ================================================================== L20 顶条 + 中心 + 底座
@layout("L20", "顶条 + 中心 + 底座", "arch", "层级（平台）",
        "平台/底座类：上方一组管理功能，中间 2–4 个能力中心，下方一个支撑底座",
        {"top": O({"title": S(8), "items": L(2, 6, S(8))}, opt=True),
         "centers": L(2, 4, {"title": S(10), "items": L(1, 6, S(6)), "note": S(24, opt=True)}),
         "base": O({"title": S(20), "text": S(40, opt=True)}, opt=True)})
def r_platform(d, s, sp):
    C = d.C
    y = Y0
    if sp.get("top"):
        t = sp["top"]
        box(s, X0, y, CW, 1.0, fill=C["pale2"], line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        text(s, X0 + 0.2, y, 2.0, 1.0, t["title"], size=15, bold=True, color=C["deep"], valign="middle", fit=12)
        n = len(t["items"])
        iw = (CW - 2.4 - 0.15 * (n - 1)) / n
        icons = pick_icons(t["items"])
        for k, it in enumerate(t["items"]):
            x = X0 + 2.3 + k * (iw + 0.15)
            card(d, s, x, y + 0.18, iw, 0.64, line_c=C["border"], radius=0.15)
            icon_circle(d, s, x + 0.1, y + 0.27, 0.46, icons[k])
            text(s, x + 0.6, y + 0.18, iw - 0.65, 0.64, it, size=12.5, bold=True, color=C["deep"], valign="middle",
                 fit=10, max_lines=2, line_spacing=1.0)
        y += 1.15
    base_h = 0.62 if sp.get("base") else 0
    ch_ = Y1 - y - (base_h + 0.15 if base_h else 0)
    cs = sp["centers"]
    n = len(cs)
    cw = (CW - 0.25 * (n - 1)) / n
    icons = pick_icons([c["title"] for c in cs])
    for k, c in enumerate(cs):
        x = X0 + k * (cw + 0.25)
        card(d, s, x, y, cw, ch_, radius=0.05)
        box(s, x, y, cw, 0.6, fill=C["primary"] if k % 2 == 0 else C["deep"], shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE,
            radius=0.1)
        add_icon(s, icons[k], x + 0.2, y + 0.11, 0.38)
        text(s, x + 0.68, y, cw - 0.78, 0.6, c["title"], size=15, bold=True, color="FFFFFF", valign="middle", fit=12,
             max_lines=1)
        per = 2 if cw < 3.2 else 3
        per = min(per, len(c["items"]))
        chw = (cw - 0.4 - 0.1 * (per - 1)) / per
        note_h = 0.5 if c.get("note") else 0
        rows = math.ceil(len(c["items"]) / per)
        avail = ch_ - 0.8 - note_h - 0.15
        ih = min(0.46, (avail - 0.1 * (rows - 1)) / rows)
        for j, it in enumerate(c["items"]):
            r, cc = j // per, j % per
            ix, iy = x + 0.2 + cc * (chw + 0.1), y + 0.8 + r * (ih + 0.1)
            box(s, ix, iy, chw, ih, fill=C["pale"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
            text(s, ix, iy, chw, ih, it, size=12, bold=True, color=C["deep"], align="center", valign="middle", fit=10,
                 max_lines=1)
        if note_h:
            text(s, x + 0.2, y + ch_ - note_h - 0.08, cw - 0.4, note_h, c["note"], size=11, color=C["gray"],
                 align="center", valign="middle", fit=9.5)
    if base_h:
        b = sp["base"]
        by = Y1 - base_h
        box(s, X0, by, CW, base_h, grad=(C["deep"], C["primary"]), grad_angle=0, shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.15)
        text(s, X0 + 0.2, by, CW - 0.4, base_h, [(b["title"] + "  ", {"bold": True, "size": 14}),
                                                  (b.get("text", ""), {"size": 12, "color": C["on_dark"]})],
             color="FFFFFF", align="center", valign="middle", fit=10, max_lines=1)


# ================================================================== L22 大数字 KPI（可带清单卡）
@layout("L22", "大数字卡 + 清单", "kpi", "量化",
        "有 2–4 个关键数字（成果数量、指标值、规模），可附各自的清单",
        {"kpis": L(2, 4, {"value": S(5), "unit": S(7), "label": S(14, opt=True), "items": L(0, 7, S(6), opt=True)}),
         "details": L(0, 3, {"title": S(8), "items": L(1, 4, S(26))}, opt=True)})
def r_kpi(d, s, sp):
    C = d.C
    v = (sp.get("_k") or {}).get("v", 0)        # 外观变体（0.7.0-dev16）：1 = 深色渐变数字卡、白字，文字位置不变
    ks = sp["kpis"]
    n = len(ks)
    gap = 0.25
    kw = (CW - gap * (n - 1)) / n
    det = sp.get("details") or []
    kh = 1.95 if det else CH - 0.05
    if det:                     # 胶囊多、卡片窄时加高数字卡（下方清单卡相应变矮）
        most = max(len(k.get("items") or []) for k in ks)
        per_row = max(1, int((kw - 0.4) / 0.66))
        rows_ = math.ceil(most / per_row) if most else 0
        head = 1.25 + (0.4 if any(k.get("label") for k in ks) else 0)
        kh = min(3.2, max(1.95, head + rows_ * 0.46 + 0.12))
    for k, kp in enumerate(ks):
        x = X0 + k * (kw + gap)
        if v == 1:
            box(s, x, Y0, kw, kh, grad=(C["deep"], C["primary"]), grad_angle=300, shape=MSO_SHAPE.ROUNDED_RECTANGLE,
                radius=0.06)
            col = "FFFFFF"
        else:
            card(d, s, x, Y0, kw, kh, fill=C["pale2"])
            col = C["accent_b"] if (k == n - 1 and n > 2) else C["primary"]
        ucol = "FFFFFF" if v == 1 else C["deep"]
        lcol = C["on_dark"] if v == 1 else C["gray"]
        vw = min(kw * 0.62, max(0.9, text_width(kp["value"], 54, True) + 0.15))
        if det and text_width(kp["unit"], 13, True) + 0.15 <= kw - vw - 0.4:
            text(s, x + 0.2, Y0 + 0.05, vw, 1.0, kp["value"], size=54, bold=True, color=col, font=NUM_FONT,
                 valign="middle", fit=30, max_lines=1)
            text(s, x + 0.25 + vw, Y0 + 0.25, kw - vw - 0.4, 0.6, kp["unit"], size=17, bold=True, color=ucol,
                 valign="middle", fit=12, max_lines=1)
            y = Y0 + 1.05
        elif det:                   # 卡片窄：单位放到数字下方
            text(s, x + 0.2, Y0 + 0.02, kw - 0.4, 0.85, kp["value"], size=46, bold=True, color=col, font=NUM_FONT,
                 valign="middle", fit=26, max_lines=1)
            text(s, x + 0.2, Y0 + 0.85, kw - 0.4, 0.36, kp["unit"], size=14, bold=True, color=ucol,
                 valign="middle", fit=11, max_lines=1)
            y = Y0 + 1.25
        else:
            text(s, x, Y0 + 0.4, kw, 1.4, kp["value"], size=72, bold=True, color=col, font=NUM_FONT, align="center",
                 valign="middle", fit=36, max_lines=1)
            text(s, x, Y0 + 1.8, kw, 0.5, kp["unit"], size=18, bold=True, color=ucol, align="center",
                 valign="middle", fit=13, max_lines=1)
            y = Y0 + 2.35
        if kp.get("label"):
            text(s, x + 0.2, y - (0.1 if det else 0), kw - 0.4, 0.36, kp["label"], size=11.5, color=lcol,
                 align="left" if det else "center", valign="middle", fit=10, max_lines=1)
            y += 0.4
        if kp.get("items"):
            chips_flow(d, s, x + 0.2, y, kw - 0.4, Y0 + kh - 0.15 - y, kp["items"], size=11.5, fill="FFFFFF",
                       center=not det)
    for k, dt in enumerate(det):
        dn = len(det)
        dw = (CW - gap * (dn - 1)) / dn
        x = X0 + k * (dw + gap)
        y = Y0 + kh + 0.2
        h = Y1 - y
        card(d, s, x, y, dw, h)
        box(s, x, y, dw, 0.08, fill=C["primary"])
        ic = pick_icons([dt["title"]])[0]
        isz = 0.56 if h > 2.0 else 0.44
        icon_circle(d, s, x + 0.25, y + 0.2, isz, ic)
        text(s, x + 0.35 + isz, y + 0.2, dw - 0.45 - isz, isz, dt["title"], size=15, bold=True, color=C["deep"],
             valign="middle", fit=12, max_lines=1)
        ly = y + 0.3 + isz
        text(s, x + 0.25, ly, dw - 0.45, y + h - 0.08 - ly, [[("·  ", {"color": C["primary"], "bold": True}), it]
                                                              for it in dt["items"]], size=12, fit=10,
             line_spacing=1.2 if h > 2.0 else 1.05)


# ================================================================== L29 交错里程碑时间轴
@layout("L29", "交错里程碑时间轴", "time", "时间",
        "实施计划、发展历程：3–8 个带日期的节点",
        {"milestones": L(3, 8, {"date": S(10), "text": S(18)}), "footer": FOOTER})
def r_timeline(d, s, sp):
    C = d.C
    ms = sp["milestones"]
    n = len(ms)
    pad = 0.95
    x0, x1 = X0 + pad, X1 - pad
    ly = 3.55 if sp.get("footer") else 3.85
    step = (x1 - x0) / max(1, n - 1)
    cwid = min(2.0, step * 1.8)
    box(s, x0 - 0.3, ly - 0.04, x1 - x0 + 0.6, 0.08, fill=C["border"])
    icons = pick_icons([m["text"] for m in ms])
    for k, m in enumerate(ms):
        x = x0 + k * step
        last = k == n - 1
        col = C["accent_b"] if last else C["primary"]
        box(s, x - 0.32, ly - 0.32, 0.64, 0.64, fill=col, shape=MSO_SHAPE.OVAL)
        add_icon(s, "flag-checkered" if last else icons[k], x - 0.18, ly - 0.18, 0.36)
        up = k % 2 == 0
        if up:
            cy, py = ly - 2.1, ly - 0.97
        else:
            cy, py = ly + 1.1, ly + 0.55
        box(s, x - cwid / 2, cy, cwid, 1.0, fill=C["accent_b_pale"] if last else C["pale2"],
            line=C["accent_b_border"] if last else C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.1)
        text(s, x - cwid / 2 + 0.05, cy, cwid - 0.1, 1.0, m["text"], size=12, bold=True,
             color=C["accent_b_dark"] if last else C["deep"], align="center", valign="middle", line_spacing=1.1,
             fit=10)
        pw = min(cwid, max(1.1, pill_width(m["date"], 12.5, 0.35)))
        pill(d, s, x - pw / 2, py, pw, 0.38, m["date"], fill=col, size=12.5)
        if up:
            line(s, x, py + 0.38, x, ly - 0.32, C["border"], w=1.0, dash=DASH.DASH)
        else:
            line(s, x, ly + 0.32, x, py, C["border"], w=1.0, dash=DASH.DASH)
    if sp.get("footer"):
        footer_strip(d, s, sp["footer"], y=5.95, h=0.52)


# ================================================================== L50 / L51 原文图片（0.6）
# 图片本身由代码按页面依据中的原图自动放入（spec._img），模型只填图题与要点；原图按原比例、原始清晰度嵌入。
IMAGE_LAYOUTS = ("L50", "L51")


def _img_or_placeholder(d, s, sp, x, y, w, h):
    img = sp.get("_img")
    if not img:
        box(s, x, y, w, h, fill=d.C["pale2"], line=d.C["border"], dash=DASH.DASH)
        text(s, x, y, w, h, "（原文图片）", size=14, color=d.C["gray"], align="center", valign="middle")
        return x, y, w, h
    return picture(d, s, img, x, y, w, h)


@layout("L50", "原图 + 要点", "image", "原文图示",
        "页面依据中有原文图片（总体技术路线、架构图、流程图、部署图等）且原图是竖图或方图（宽高比 < 1.8）：左侧原图，右侧 2—4 条读图要点；"
        "图片由程序放入，只填图题和要点",
        {"caption": S(24, opt=True), "points": L(2, 4, {"head": S(12), "body": S(44, opt=True)})})
def r_image_points(d, s, sp):
    C = d.C
    asp = float((sp.get("_img") or {}).get("aspect") or 1.3)
    ch = getattr(d, "ch", CH)                   # 不放底部横幅时内容区更高（0.7.0-dev23）
    cap_h = 0.32 if sp.get("caption") else 0
    ih = ch - cap_h
    iw = min(CW * 0.64, ih * asp)               # 图片尽量大：高度占满，宽度不超过内容区 64%
    x, y, w, h = _img_or_placeholder(d, s, sp, X0, Y0, iw, ih)
    if sp.get("caption"):
        text(s, X0, Y0 + ih + 0.02, iw, cap_h, sp["caption"], size=11, color=C["gray"], align="center", valign="middle")
    rx = X0 + iw + 0.35
    rw = X1 - rx
    pts = sp["points"]
    gap = 0.16
    ph = (ch - gap * (len(pts) - 1)) / len(pts)
    for k, p in enumerate(pts):
        yy = Y0 + k * (ph + gap)
        box(s, rx, yy, rw, ph, fill="FFFFFF", line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        box(s, rx, yy, 0.07, ph, fill=C["primary"])
        head_body(d, s, rx + 0.22, yy + 0.08, rw - 0.34, ph - 0.16, p["head"], p.get("body"), hsize=14, bsize=12,
                  hcolor=C["title"])


@layout("L51", "原图通栏", "image", "原文图示",
        "页面依据中有原文图片且原图是宽图（宽高比 ≥ 1.8，如横向流程、时序链、界面截图）：原图通栏放大，下方一行结论；"
        "图片由程序放入，只填图题和结论",
        {"caption": S(24, opt=True), "note": S(70, opt=True)})
def r_image_full(d, s, sp):
    C = d.C
    ch = getattr(d, "ch", CH)                   # 不放底部横幅时内容区更高（0.7.0-dev23）
    note_h = 0.5 if sp.get("note") else 0
    cap_h = 0.3 if sp.get("caption") else 0
    x, y, w, h = _img_or_placeholder(d, s, sp, X0, Y0, CW, ch - note_h - cap_h)
    if sp.get("caption"):
        text(s, X0, y + h + 0.04, CW, cap_h, sp["caption"], size=11, color=C["gray"], align="center", valign="middle")
    if sp.get("note"):
        ny = Y0 + ch - note_h
        box(s, X0, ny, CW, note_h - 0.04, fill=C["pale2"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.2)
        text(s, X0 + 0.25, ny, CW - 0.5, note_h - 0.04, sp["note"], size=13, color=C["text"], valign="middle", fit=11,
             max_lines=1)


# ================================================================== L42 表格（一页放下）
@layout("L42", "表格", "table", "映射/清单",
        "真正的行列数据（成果清单、功能清单、指标表）：3 列以上，或 2 列但超过 8 行；必须一页放下（最多 10 行），"
        "行多时先合并同类行（如每个类别一行、名称用顿号连接）。两列的短对照、各列互不对应的清单不要用表格",
        {"columns": L(2, 5, S(10)), "rows": L(1, 10, L(2, 5, S(30)))})
def r_table(d, s, sp, frame=None):
    """汇报 PPT 不把一张表拆成两页（0.6）：依次缩小字号与行高，仍放不下就报版面问题，由设计阶段合并行或换版式。"""
    C = d.C
    cols = sp["columns"]
    nc = len(cols)
    rows = [list(r) + [""] * (nc - len(r)) for r in sp["rows"]]
    widths = []
    for j in range(nc):
        mx = max([em_width(cols[j])] + [em_width(r[j]) for r in rows])
        widths.append(min(max(mx, 3.0), 26.0))
    tot = sum(widths)
    colw = [CW * w / tot for w in widths]
    from .prim import lines_needed
    from .prim import _deck

    def row_h(r, size, floor):
        n_ = max(lines_needed([v or " "], size, colw[j] - 0.18) for j, v in enumerate(r))
        return max(floor, n_ * size * 1.22 / 72 + 0.08)
    avail = CH - 0.2
    size, floor = 12.0, 0.4
    for size, floor in ((12.0, 0.4), (11.5, 0.36), (11.0, 0.34), (10.5, 0.3)):
        hs = [row_h(cols, size, floor)] + [row_h(r, size, floor) for r in rows]
        if sum(hs) <= avail:
            break
    if sum(hs) > avail and _deck():
        fit = 0
        acc = hs[0]
        for h_ in hs[1:]:
            if acc + h_ > avail:
                break
            acc += h_
            fit += 1
        _deck().issue("text_overflow", "表格共 %d 行" % len(rows),
                      "一页最多放下约 %d 行：请合并同类行（如每个类别一行、名称用顿号连接）或删去次要列" % max(1, fit))
    n = len(rows) + 1
    rh = min(0.6, avail / n)                    # 行少时适当加高，避免表格挤在页面上方
    hs = [max(h_, rh) for h_ in hs]
    tb = s.shapes.add_table(n, nc, Inches(X0), Inches(Y0), Inches(CW), Inches(sum(hs))).table
    for j in range(nc):
        tb.columns[j].width = Inches(colw[j])
    for j, h in enumerate(cols):
        _cell(tb.cell(0, j), h, C, size=size, bold=True, color="FFFFFF", fill=C["deep"])
    for i, r in enumerate(rows, 1):
        f = C["pale2"] if i % 2 == 0 else "FFFFFF"
        for j, v in enumerate(r):
            long_ = widths[j] > 8.5            # 按整列决定对齐，避免同列有的居中有的居左
            _cell(tb.cell(i, j), v, C, size=size, fill=f, bold=(j == 0 and nc > 2 and not long_),
                  color=C["deep"] if (j == 0 and not long_) else C["text"], align="left" if long_ else "center")
    # 第一列连续相同值合并
    i = 1
    while i < n:
        j = i
        while j + 1 < n and rows[j][0] == rows[i - 1][0] and rows[i - 1][0]:
            j += 1
        if j > i:
            v = rows[i - 1][0]
            tb.cell(i, 0).merge(tb.cell(j, 0))
            _cell(tb.cell(i, 0), v, C, size=size, fill="FFFFFF", bold=True, color=C["deep"])
        i = j + 1
    for r_ in range(n):
        tb.rows[r_].height = Inches(hs[r_])
    return 1


def _cell(cell, t, C, size=12, bold=False, color=None, fill="FFFFFF", align="center"):
    cell.text = ""
    p = cell.text_frame.paragraphs[0]
    p.alignment = {"center": PP_ALIGN.CENTER, "left": PP_ALIGN.LEFT}[align]
    r = p.add_run()
    r.text = t
    r.font.size, r.font.bold, r.font.name = Pt(size), bold, cur_font()
    r.font.color.rgb = rgb(color or C["text"])
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    cell.margin_left = cell.margin_right = Inches(0.08)
    cell.margin_top = cell.margin_bottom = Inches(0.02)
    cell.fill.solid()
    cell.fill.fore_color.rgb = rgb(fill)


# ================================================================== L40 待确认事项
@layout("L40", "待确认事项", "confirm", "待确认事项",
        "需评审确认、需补充、待明确的事项（只在原文确有此类内容时使用；原文已有明确安排的事项不要列入）",
        {"items": L(1, 6, {"item": S(14), "detail": S(40), "source": S(10, opt=True), "action": S(26),
                           "status": E("待确认", "待补充")})})
def r_confirm(d, s, sp):
    C = d.C
    cols = [("编号", X0, 0.75), ("事项", 1.6, 2.6), ("原文情况", 4.3, 3.45), ("出处", 7.85, 1.35),
            ("建议处理", 9.3, 2.2), ("状态", 11.6, 0.98)]
    box(s, X0, Y0, CW, 0.46, fill=C["deep"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.15)
    for t, x, w in cols:
        text(s, x, Y0, w, 0.46, t, size=12.5, bold=True, color="FFFFFF", align="center", valign="middle")
    items = sp["items"]
    top = Y0 + 0.58
    rh = min(1.0, (Y1 - top - 0.1 * (len(items) - 1)) / len(items))
    st = {"待确认": C["accent_b"], "待补充": C["light"]}
    for k, it in enumerate(items):
        y = top + k * (rh + 0.1)
        state = it["status"]
        fill = C["accent_b_pale"] if state == "待确认" else C["pale2"]
        box(s, X0, y, CW, rh, fill=fill, line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        badge(d, s, X0 + 0.15, y + rh / 2 - 0.23, 0.46, k + 1, fill=C["deep"], fsize=13)
        text(s, 1.6, y, 2.6, rh, it["item"], size=13.5, bold=True, color=C["title"], valign="middle", fit=11)
        text(s, 4.3, y, 3.45, rh, it["detail"], size=12, valign="middle", line_spacing=1.15, fit=10)
        text(s, 7.85, y, 1.35, rh, it.get("source") or "—", size=11.5, color=C["gray"], align="center",
             valign="middle", fit=9.5)
        text(s, 9.3, y, 2.2, rh, it["action"], size=12, color=C["deep"], valign="middle", line_spacing=1.15, fit=10)
        pill(d, s, 11.65, y + rh / 2 - 0.17, 0.88, 0.34, state, fill=st[state], size=11.5)


# ================================================================== L44 循环 / 闭环
@layout("L44", "循环闭环", "cycle", "循环/闭环",
        "首尾相接、持续迭代的 3–6 个环节（调优闭环、更新机制、PDCA）",
        {"center": S(6), "center_sub": S(10, opt=True), "steps": L(3, 6, {"head": S(6), "body": S(32)})})
def r_cycle(d, s, sp):
    C = d.C
    cx, cy, r = 3.75, 4.05, 1.85
    box(s, cx - r, cy - r, 2 * r, 2 * r, line=C["border"], lw=3, shape=MSO_SHAPE.OVAL)
    box(s, cx - 1.05, cy - 1.05, 2.1, 2.1, grad=(C["deep"], C["primary"]), grad_angle=315, shape=MSO_SHAPE.OVAL)
    paras = [[(sp["center"], {"size": 18, "bold": True, "color": "FFFFFF"})]]
    if sp.get("center_sub"):
        paras.append([(sp["center_sub"], {"size": 11, "color": C["on_dark"]})])
    text(s, cx - 1.0, cy - 1.0, 2.0, 2.0, paras, align="center", valign="middle", line_spacing=1.15, fit=12)
    st = sp["steps"]
    n = len(st)
    icons = pick_icons([x["head"] for x in st])
    for k in range(n):
        a = math.radians(-90 + (k + 0.5) * 360 / n)
        ax, ay = cx + r * math.cos(a), cy + r * math.sin(a)
        tri = box(s, ax - 0.17, ay - 0.15, 0.34, 0.3, fill=C["primary"], shape=MSO_SHAPE.ISOSCELES_TRIANGLE)
        tri.rotation = math.degrees(a) + 180
    for k, it in enumerate(st):
        a = math.radians(-90 + k * 360 / n)
        nx, ny = cx + r * math.cos(a), cy + r * math.sin(a)
        icon_circle(d, s, nx - 0.38, ny - 0.38, 0.76, icons[k])
        badge(d, s, nx + 0.18, ny - 0.46, 0.3, k + 1, fill=C["accent_b"], fsize=10)
        ca, sa = math.cos(a), math.sin(a)
        if ca > 0.2:
            lx, ly_, al = nx + 0.48, ny - 0.2, "left"
        elif ca < -0.2:
            lx, ly_, al = nx - 1.88, ny - 0.2, "right"
        else:
            lx, ly_, al = nx - 0.7, (ny - 0.85 if sa < 0 else ny + 0.42), "center"
        text(s, lx, ly_, 1.4, 0.4, it["head"], size=13.5, bold=True, color=C["deep"], align=al, valign="middle",
             fit=11, max_lines=1)
    rx, rw = 7.3, X1 - 7.3
    top = panel(d, s, rx, Y0, rw, CH - 0.05, sp.get("panel_title") or "各环节说明")
    ih = (Y1 - 0.15 - top) / n
    for k, it in enumerate(st):
        y = top + k * ih
        badge(d, s, rx + 0.25, y + (ih - 0.42) / 2, 0.42, k + 1, fill=C["primary"], fsize=12)
        text(s, rx + 0.82, y, 1.3, ih, it["head"], size=13.5, bold=True, color=C["title"], valign="middle", fit=11)
        text(s, rx + 2.15, y, rw - 2.35, ih, it["body"], size=12, valign="middle", line_spacing=1.15, fit=10)
        if k < n - 1:
            line(s, rx + 0.25, y + ih, rx + rw - 0.25, y + ih, C["line"], w=0.75)


# ================================================================== L45 中心辐射
@layout("L45", "中心辐射", "hub", "总分（一对多）",
        "一个平台/底座/核心支撑多个应用、智能体、系统（4–8 个）",
        {"center": S(8), "center_sub": S(16, opt=True), "nodes": L(3, 8, {"head": S(8), "body": S(11, opt=True)})})
def r_hub(d, s, sp):
    C = d.C
    cx, cy, rx_, ry_ = (X0 + X1) / 2, 4.05, 4.45, 1.95
    nodes = sp["nodes"]
    n = len(nodes)
    pos = [(cx + rx_ * math.cos(math.radians(-90 + k * 360 / n)), cy + ry_ * math.sin(math.radians(-90 + k * 360 / n)))
           for k in range(n)]
    for (x, y) in pos:
        line(s, cx, cy, x, y, C["border"], w=1.5, dash=DASH.DASH)
    box(s, cx - 1.6, cy - 1.6, 3.2, 3.2, fill=C["pale"], shape=MSO_SHAPE.OVAL)
    box(s, cx - 1.2, cy - 1.2, 2.4, 2.4, grad=(C["deep"], C["primary"]), grad_angle=315, shape=MSO_SHAPE.OVAL)
    paras = [[(sp["center"], {"size": 17, "bold": True, "color": "FFFFFF"})]]
    if sp.get("center_sub"):
        paras.append([(sp["center_sub"], {"size": 10.5, "color": C["on_dark"]})])
    text(s, cx - 1.1, cy - 1.1, 2.2, 2.2, paras, align="center", valign="middle", line_spacing=1.15, fit=11)
    cw, ch = 2.55, 0.78
    icons = pick_icons([nd["head"] for nd in nodes])
    for k, ((x, y), nd) in enumerate(zip(pos, nodes)):
        card(d, s, x - cw / 2, y - ch / 2, cw, ch, line_c=C["border"], radius=0.15)
        icon_circle(d, s, x - cw / 2 + 0.1, y - 0.28, 0.56, icons[k])
        if nd.get("body"):
            text(s, x - cw / 2 + 0.72, y - ch / 2 + 0.04, cw - 0.78, 0.36, nd["head"], size=13.5, bold=True,
                 color=C["deep"], valign="middle", fit=11, max_lines=1)
            text(s, x - cw / 2 + 0.72, y - 0.02, cw - 0.78, 0.36, nd["body"], size=10.5, color=C["gray"],
                 valign="middle", fit=9, max_lines=1)
        else:
            text(s, x - cw / 2 + 0.72, y - ch / 2, cw - 0.78, ch, nd["head"], size=14, bold=True, color=C["deep"],
                 valign="middle", fit=11)


# ================================================================== L46 勾选矩阵
@layout("L46", "勾选矩阵", "table", "映射（多对多）",
        "对象 × 维度 的适用关系（功能 × 角色、智能体 × 集成位置、需求 × 模块）",
        {"columns": L(2, 6, S(8)), "rows": L(2, 8, {"name": S(8), "marks": L(2, 6, B())}), "note": S(30, opt=True)})
def r_matrix(d, s, sp):
    C = d.C
    cols, rows = sp["columns"], sp["rows"]
    nc = len(cols)
    rw0 = 2.1
    cw = min(1.7, (CW - rw0) / nc)
    tw = rw0 + cw * nc
    x0 = X0 + (CW - tw) / 2
    hh = 0.5
    for j, c in enumerate(cols):
        box(s, x0 + rw0 + j * cw + 0.03, Y0, cw - 0.06, hh, fill=C["deep"] if j % 2 == 0 else C["primary"],
            shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.15)
        text(s, x0 + rw0 + j * cw + 0.05, Y0, cw - 0.1, hh, c, size=12, bold=True, color="FFFFFF", align="center",
             valign="middle", fit=9.5, max_lines=2, line_spacing=1.0)
    top = Y0 + hh + 0.12
    rh = min(0.56, (Y1 - 0.4 - top) / len(rows))
    for k, r in enumerate(rows):
        y = top + k * rh
        if k % 2 == 0:
            box(s, x0, y, tw, rh, fill=C["pale2"])
        text(s, x0 + 0.15, y, rw0 - 0.2, rh, r["name"], size=13, bold=True, color=C["title"], valign="middle",
             fit=10.5, max_lines=1)
        for j in range(nc):
            m = r["marks"][j] if j < len(r["marks"]) else False
            mx = x0 + rw0 + j * cw + cw / 2
            if m:
                box(s, mx - 0.13, y + rh / 2 - 0.13, 0.26, 0.26, fill=C["primary"], shape=MSO_SHAPE.OVAL)
            else:
                box(s, mx - 0.05, y + rh / 2 - 0.05, 0.1, 0.1, fill=C["line"], shape=MSO_SHAPE.OVAL)
    yb = top + len(rows) * rh
    line(s, x0, yb, x0 + tw, yb, C["border"], w=1)
    text(s, x0, yb + 0.08, tw, 0.3, "● 表示适用" + ("    " + sp["note"] if sp.get("note") else ""), size=10.5,
         color=C["gray"], valign="middle")


# ================================================================== L48 金句 / 大数字
@layout("L48", "金句 / 单一大数字", "kpi", "强调（舒缓页）",
        "一页只讲一个关键数字或一句核心判断",
        {"value": S(5), "unit": S(14), "statement": S(40), "sub": S(44, opt=True)})
def r_statement(d, s, sp):
    C = d.C
    box(s, X0, 1.3, CW, 5.2, fill=C["pale2"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
    text(s, X0 + 0.35, 1.8, 4.0, 2.4, sp["value"], size=140, bold=True, color=C["primary"], font=NUM_FONT,
         align="center", valign="middle", fit=60, max_lines=1)
    text(s, X0 + 0.35, 4.25, 4.0, 0.55, sp["unit"], size=17, bold=True, color=C["deep"], align="center",
         valign="middle", fit=12, max_lines=1)
    box(s, X0 + 4.7, 2.0, 0.04, 2.9, fill=C["border"])
    add_icon(s, "format-quote-open", X0 + 5.1, 1.95, 0.6, C["border"])
    text(s, X0 + 5.1, 2.65, 6.4, 1.6, sp["statement"], size=24, bold=True, color=C["title"], line_spacing=1.35,
         fit=16)
    if sp.get("sub"):
        text(s, X0 + 5.1, 4.35, 6.4, 0.9, sp["sub"], size=14, color=C["text"], line_spacing=1.25, fit=11)


# ================================================================== L52 问答
@layout("L52", "问答卡", "faq", "问答",
        "常见问题与回答（培训、评审答疑预案）",
        {"qa": L(2, 6, {"q": S(22), "a": S(58)})})
def r_faq(d, s, sp):
    C = d.C
    qa = sp["qa"]
    cols = 2 if len(qa) > 3 else 1
    rows = math.ceil(len(qa) / cols)
    cw = (CW - 0.25 * (cols - 1)) / cols
    ch = (CH - 0.05 - 0.15 * (rows - 1)) / rows
    for k, it in enumerate(qa):
        c, r = k % cols, k // cols
        x, y = X0 + c * (cw + 0.25), Y0 + r * (ch + 0.15)
        card(d, s, x, y, cw, ch)
        for (lab, col, yy) in (("Q", C["accent_b"], y + 0.18), ("A", C["primary"], y + ch * 0.5 + 0.02)):
            box(s, x + 0.2, yy, 0.44, 0.44, fill=col, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.2)
            text(s, x + 0.2, yy, 0.44, 0.44, lab, size=15, bold=True, color="FFFFFF", font=NUM_FONT, align="center",
                 valign="middle", margin=(0, 0, 0, 0), check=False)
        text(s, x + 0.78, y + 0.12, cw - 0.95, ch * 0.5 - 0.15, it["q"], size=14.5, bold=True, color=C["title"],
             valign="middle", fit=12)
        text(s, x + 0.78, y + ch * 0.5 - 0.05, cw - 0.95, ch * 0.5 - 0.05, it["a"], size=12, valign="middle",
             line_spacing=1.15, fit=10)


# ================================================================== L31 阶梯递进
@layout("L31", "阶梯递进", "time", "递进（能力等级）",
        "由浅入深的 3–5 个级别/阶段（L1–L5、初级到高级、分级响应），每级有说明和 2–4 条具体做法",
        {"levels": L(3, 5, {"name": S(10), "desc": S(40), "items": L(0, 4, S(26), opt=True)})})
def r_stairs(d, s, sp):
    """0.6 重做：阶梯只占上方一条（体现由浅入深），每级下方是等高的说明卡，不再留下大块三角空白。"""
    C = d.C
    lv = sp["levels"]
    n = len(lv)
    gap = 0.14
    w = (CW - gap * (n - 1)) / n
    band = 1.75                                   # 阶梯带高度
    base = Y0 + band
    hmin = 0.85
    shades = [C["mid"], C["light"], C["primary"], C["primary"], C["deep"], C["deep"]]
    for k, it in enumerate(lv):
        x = X0 + k * (w + gap)
        h = hmin + (band - hmin) * k / max(1, n - 1)
        y = base - h
        box(s, x, y, w, h, fill=shades[min(5, int(k * 6 / n))], shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.08)
        text(s, x + 0.12, y + 0.06, 0.6, 0.32, "L%d" % (k + 1), size=13, bold=True, color="FFFFFF", font=NUM_FONT,
             valign="middle", check=False)
        text(s, x + 0.1, base - 0.5, w - 0.2, 0.44, it["name"], size=15, bold=True, color="FFFFFF", align="center",
             valign="middle", fit=11, max_lines=1)
        cy = base + 0.14
        ch = Y1 - cy
        card(d, s, x, cy, w, ch, line_c=C["line"], radius=0.06)
        box(s, x, cy, w, 0.07, fill=shades[min(5, int(k * 6 / n))])
        items = it.get("items") or []
        from .prim import lines_needed
        dh = min(1.1, lines_needed([it["desc"]], 12.5, w - 0.32) * 12.5 * 1.3 / 72 + 0.12) if items else ch - 0.3
        text(s, x + 0.16, cy + 0.18, w - 0.32, dh, it["desc"], size=12.5, bold=bool(items), color=C["title"] if items else C["text"],
             line_spacing=1.15, valign="top", fit=10.5)
        if items:
            iy = cy + 0.18 + dh + 0.08
            ih = min(0.55, (Y1 - 0.12 - iy) / len(items))
            for j, t in enumerate(items):
                yy = iy + j * ih
                box(s, x + 0.2, yy + 0.12, 0.08, 0.08, fill=C["primary"], shape=MSO_SHAPE.OVAL)
                text(s, x + 0.36, yy, w - 0.52, ih, t, size=11.5, color=C["text"], valign="top", fit=10)
    line(s, X0, base + 0.02, X1, base + 0.02, C["deep"], w=1.2)


# ================================================================== L47 前后对比数字
@layout("L47", "前后对比数字", "kpi", "对比（量化前后）",
        "智能化/改造前后的效率、时长、数量对比（2–4 项，每项都要有原文给出的前后值）",
        {"items": L(2, 4, {"head": S(8), "before": S(5), "after": S(5), "body": S(30, opt=True)}),
         "before_label": S(4, opt=True), "after_label": S(5, opt=True)})
def r_before_after(d, s, sp):
    C = d.C
    it = sp["items"]
    n = len(it)
    gap = 0.25
    cw = (CW - gap * (n - 1)) / n
    icons = pick_icons([x["head"] for x in it])
    bl, al = sp.get("before_label") or "以往", sp.get("after_label") or "现在"
    for k, x_ in enumerate(it):
        x = X0 + k * (cw + gap)
        card(d, s, x, Y0, cw, CH - 0.05)
        box(s, x, Y0, cw, 0.09, fill=C["primary"])
        icon_circle(d, s, x + 0.3, Y0 + 0.3, 0.66, icons[k])
        text(s, x + 1.1, Y0 + 0.3, cw - 1.2, 0.66, x_["head"], size=16, bold=True, color=C["deep"], valign="middle",
             fit=12, max_lines=1)
        box(s, x + 0.3, Y0 + 1.3, cw - 0.6, 1.0, fill=C["muted_pale"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.1)
        text(s, x + 0.42, Y0 + 1.34, cw - 0.84, 0.3, bl, size=11, color=C["gray"], valign="middle")
        text(s, x + 0.42, Y0 + 1.6, cw - 0.84, 0.66, x_["before"], size=28, bold=True, color=C["muted"], align="right",
             valign="middle", fit=16, max_lines=1)
        chev(s, x + cw / 2 - 0.3, Y0 + 2.4, 0.6, 0.42, C["light"], down=True)
        box(s, x + 0.3, Y0 + 2.9, cw - 0.6, 1.0, fill=C["pale"], line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.1)
        text(s, x + 0.42, Y0 + 2.94, cw - 0.84, 0.3, al, size=11, color=C["deep"], bold=True, valign="middle")
        text(s, x + 0.42, Y0 + 3.2, cw - 0.84, 0.66, x_["after"], size=32, bold=True, color=C["primary"], align="right",
             valign="middle", fit=16, max_lines=1)
        if x_.get("body"):
            text(s, x + 0.3, Y0 + 4.0, cw - 0.6, 0.85, x_["body"], size=12, line_spacing=1.15, valign="middle",
                 fit=10)


def menu(ids=None):
    """给模型的版式清单（含槽位示例）。"""
    import json
    out = []
    for lid, L_ in LAYOUTS.items():
        if ids and lid not in ids:
            continue
        out.append("### %s %s（适用：%s）\n%s\n槽位：%s" % (lid, L_["name"], L_["relation"], L_["use"],
                                                       json.dumps(example(L_["schema"]), ensure_ascii=False)))
    return "\n\n".join(out)


def brief_menu(exclude=()):
    return "\n".join("- %s %s：%s（适用关系：%s）" % (lid, L_["name"], L_["use"], L_["relation"])
                     for lid, L_ in LAYOUTS.items() if lid not in exclude)


# ================================================================== 第二批：技术方案专用版式（0.5）
# L01 政策 / 依据时间轴
@layout("L01", "依据时间轴", "time", "时间（立项依据、政策脉络）",
        "立项依据、政策文件按时间排列（3–5 份文件，每份有日期、文号或名称、要点）",
        {"items": L(3, 5, {"date": S(10), "doc": S(18, opt=True), "title": S(24), "body": S(44, opt=True)})})
def r_policy(d, s, sp):
    C = d.C
    it = sp["items"]
    n = len(it)
    gap = 0.22
    cw = (CW - gap * (n - 1)) / n
    ly = Y0 + 0.95
    line(s, X0 + 0.2, ly, X1 - 0.2, ly, C["border"], w=2)
    icons = pick_icons([x["title"] for x in it])
    for k, x in enumerate(it):
        cx = X0 + k * (cw + gap) + cw / 2
        last = k == n - 1
        col = C["accent_b"] if last else C["primary"]
        pw = min(cw - 0.2, max(1.2, pill_width(x["date"], 13, 0.4)))
        pill(d, s, cx - pw / 2, Y0 + 0.05, pw, 0.4, x["date"], fill=col, size=13)
        icon_circle(d, s, cx - 0.3, ly - 0.3, 0.6, icons[k], *((C["accent_b"], "D9821A") if last else (None, None)))
        y = ly + 0.5
        card(d, s, X0 + k * (cw + gap), y, cw, Y1 - y)
        xx, ww = X0 + k * (cw + gap) + 0.18, cw - 0.36
        yy = y + 0.15
        if x.get("doc"):
            text(s, xx, yy, ww, 0.32, x["doc"], size=10.5, color=C["gray"], valign="middle", fit=9, max_lines=1)
            yy += 0.36
        text(s, xx, yy, ww, 0.75, x["title"], size=14, bold=True, color=C["deep"], fit=11.5, line_spacing=1.1)
        yy += 0.82
        if x.get("body"):
            text(s, xx, yy, ww, Y1 - 0.12 - yy, x["body"], size=12, fit=10, line_spacing=1.2, grow=13.5)


# L02 交错徽章叙述
@layout("L02", "交错徽章叙述", "cause", "背景/因果（现状—问题—需求）",
        "背景页的 2–4 段递进叙述（现状 → 问题 → 需求），每段一句粗体判断加一句说明",
        {"rows": L(2, 4, {"badge": S(8), "head": S(30), "body": S(70)})})
def r_badges(d, s, sp):
    C = d.C
    rows = sp["rows"]
    n = len(rows)
    rh = (CH - 0.05) / n
    cols = [C["primary"], C["accent_a"], C["accent_b"], C["deep"]]
    for k, r in enumerate(rows):
        y = Y0 + k * rh
        left = k % 2 == 0
        bw, bh = 1.75, min(1.3, rh - 0.12)
        bx = X0 if left else X1 - bw
        box(s, bx, y + (rh - bh) / 2, bw, bh, fill=cols[k % 4], shape=MSO_SHAPE.HEXAGON)
        text(s, bx + 0.12, y + (rh - bh) / 2, bw - 0.24, bh, r["badge"], size=15, bold=True, color="FFFFFF",
             align="center", valign="middle", fit=11, line_spacing=1.05)
        tx = X0 + bw + 0.35 if left else X0
        tw = CW - bw - 0.35
        text(s, tx, y + 0.12, tw, 0.45, r["head"], size=16, bold=True, color=cols[k % 4], valign="middle", fit=12.5,
             max_lines=1, align="left" if left else "right")
        text(s, tx, y + 0.6, tw, rh - 0.75, r["body"], size=13, line_spacing=1.25, fit=10.5, grow=14.5,
             align="left" if left else "right", red=C["red"])
        if k < n - 1:
            line(s, X0 + (bw + 0.35 if left else 0), y + rh - 0.02, X1 - (0 if left else bw + 0.35), y + rh - 0.02,
                 C["line"], w=0.75, dash=DASH.DASH)


# L15 带并行分支的管线
@layout("L15", "带并行分支的管线", "flow", "递进/流程（含并行分支、权重）",
        "处理链路中有一步是并行的多路（如三路检索、多源接入），各路可带权重或参数；可附 2–3 张特性卡",
        {"stages": L(3, 5, {"head": S(10), "items": L(1, 4, S(18)), "parallel": B(opt=True)}),
         "features": L(0, 3, {"head": S(12), "body": S(50)}, opt=True)})
def r_branch(d, s, sp):
    C = d.C
    v = (sp.get("_k") or {}).get("v", 0)        # 外观变体（0.7.0-dev16）：1 = 步骤标题浅色条、彩色字，文字位置不变
    st = sp["stages"]
    feats = sp.get("features") or []
    n = len(st)
    aw = 0.32
    top_h = 3.0 if feats else CH - 0.05
    weights = [1.35 if x.get("parallel") else 1.0 for x in st]
    unit = (CW - aw * (n - 1)) / sum(weights)
    x = X0
    for k, it in enumerate(st):
        w = unit * weights[k]
        if it.get("parallel"):
            box(s, x, Y0, w, top_h, fill=C["pale"], line=C["border"], dash=DASH.DASH,
                shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
            text(s, x + 0.1, Y0 + 0.08, w - 0.2, 0.4, it["head"], size=14, bold=True, color=C["deep"], align="center",
                 valign="middle", fit=11, max_lines=1)
            m = len(it["items"])
            bh = min(0.7, (top_h - 0.65 - 0.12 * (m - 1)) / m)
            oy = Y0 + 0.55 + ((top_h - 0.65) - (m * bh + 0.12 * (m - 1))) / 2
            for j, t in enumerate(it["items"]):
                by = oy + j * (bh + 0.12)
                card(d, s, x + 0.15, by, w - 0.3, bh, line_c=C["primary"])
                box(s, x + 0.15, by, 0.08, bh, fill=[C["primary"], C["accent_a"], C["accent_b"], C["deep"]][j % 4])
                text(s, x + 0.3, by, w - 0.5, bh, t, size=12.5, bold=True, color=C["deep"], valign="middle", fit=10,
                     line_spacing=1.05)
        else:
            card(d, s, x, Y0 + 0.1, w, top_h - 0.2, fill=C["pale2"] if v == 1 else "FFFFFF", line_c=C["border"])
            box(s, x, Y0 + 0.1, w, 0.5, fill="FFFFFF" if v == 1 else C["primary"], line=C["border"] if v == 1 else None,
                shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.1)
            if v == 1:
                box(s, x + 0.15, Y0 + 0.56, w - 0.3, 0.05, fill=C["primary"])
            text(s, x + 0.08, Y0 + 0.1, w - 0.16, 0.5, it["head"], size=13.5, bold=True,
                 color=C["deep"] if v == 1 else "FFFFFF", align="center", valign="middle", fit=10.5, max_lines=1)
            text(s, x + 0.15, Y0 + 0.72, w - 0.3, top_h - 0.95,
                 [[("·  ", {"color": C["primary"], "bold": True}), t] for t in it["items"]], size=12, fit=10,
                 line_spacing=1.25, grow=13.5)
        if k < n - 1:
            chev(s, x + w + 0.06, Y0 + top_h / 2 - 0.22, 0.2, 0.44, C["mid"])
        x += w + aw
    if feats:
        y = Y0 + top_h + 0.2
        fw = (CW - 0.25 * (len(feats) - 1)) / len(feats)
        icons = pick_icons([f["head"] for f in feats])
        for k, f in enumerate(feats):
            fx = X0 + k * (fw + 0.25)
            card(d, s, fx, y, fw, Y1 - y)
            icon_circle(d, s, fx + 0.2, y + 0.2, 0.56, icons[k])
            text(s, fx + 0.9, y + 0.2, fw - 1.0, 0.56, f["head"], size=14.5, bold=True, color=C["deep"],
                 valign="middle", fit=11.5, max_lines=1)
            text(s, fx + 0.2, y + 0.85, fw - 0.4, Y1 - y - 0.95, f["body"], size=12, fit=10, line_spacing=1.2,
                 grow=13.5, red=C["red"])


# L19 金字塔 + 说明卡
@layout("L19", "金字塔 + 说明卡", "arch", "层级（自上而下、由少到多）",
        "3–4 层的层级体系（如图谱—指标—术语、目标—任务—措施），右侧 1–3 张说明卡；可在塔底列来源",
        {"levels": L(3, 4, {"name": S(8), "desc": S(22)}), "cards": L(1, 3, {"title": S(10), "items": L(1, 3, S(28))}),
         "base_label": S(6, opt=True), "base_items": L(0, 4, S(8), opt=True)})
def r_pyramid(d, s, sp):
    C = d.C
    lv = sp["levels"]
    n = len(lv)
    pw = 5.3
    base_h = 0.75 if sp.get("base_items") else 0
    lh = (CH - 0.05 - base_h - 0.1 * (n - 1)) / n
    cols = [C["deep"], C["primary"], C["light"], C["mid"]]
    for k, it in enumerate(lv):
        y = Y0 + k * (lh + 0.1)
        w = pw * (0.45 + 0.55 * (k + 1) / n)
        x = X0 + (pw - w) / 2
        box(s, x, y, w, lh, fill=cols[k], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        text(s, x + 0.1, y + 0.06, w - 0.2, lh * 0.5, it["name"], size=16, bold=True, color="FFFFFF", align="center",
             valign="bottom", fit=12, max_lines=1)
        text(s, x + 0.15, y + lh * 0.5 + 0.02, w - 0.3, lh * 0.5 - 0.08, it["desc"], size=11, color=C["on_dark"],
             align="center", valign="top", fit=9.5)
    if base_h:
        by = Y1 - base_h + 0.1
        text(s, X0, by, 1.1, 0.4, sp.get("base_label") or "来源", size=11.5, bold=True, color=C["gray"],
             valign="middle")
        chips_flow(d, s, X0 + 1.1, by, pw - 1.1, 0.42, sp["base_items"], size=11.5, fill="FFFFFF")
    rx = X0 + pw + 0.35
    rw = X1 - rx
    cs = sp["cards"]
    ch_ = (CH - 0.05 - 0.2 * (len(cs) - 1)) / len(cs)
    for k, c in enumerate(cs):
        y = Y0 + k * (ch_ + 0.2)
        top = panel(d, s, rx, y, rw, ch_, c["title"], color=C["primary"] if k % 2 == 0 else C["deep"])
        text(s, rx + 0.25, top - 0.05, rw - 0.45, y + ch_ - top - 0.05,
             [[("·  ", {"color": C["primary"], "bold": True}), t] for t in c["items"]], size=12, fit=10,
             line_spacing=1.25, grow=13.5, red=C["red"])


# L21 两区部署拓扑
@layout("L21", "两区部署拓扑", "arch", "层级/部署（两个区域 + 隔离/连接）",
        "部署在两个网络区域（如互联网区 / 政务外网、生产区 / 测试区），中间有网闸、接口或单向传输",
        {"zones": L(2, 2, {"name": S(10), "desc": S(26, opt=True), "items": L(1, 5, {"head": S(12), "body": S(28, opt=True)})}),
         "link": O({"name": S(6), "desc": S(24, opt=True)}, opt=False), "notes": L(0, 3, S(44), opt=True)})
def r_topology(d, s, sp):
    C = d.C
    notes_h = 0.5 if sp.get("notes") else 0
    zh = CH - 0.05 - notes_h
    lw = 1.6
    zw = (CW - lw - 0.4) / 2
    for k, z in enumerate(sp["zones"]):
        x = X0 if k == 0 else X0 + zw + lw + 0.4
        col = C["accent_b"] if k == 0 else C["primary"]
        fill = C["accent_b_pale"] if k == 0 else C["pale2"]
        box(s, x, Y0 + 0.2, zw, zh - 0.2, fill=fill, line=col, lw=1.25, dash=DASH.DASH,
            shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
        pill(d, s, x + 0.25, Y0, pill_width(z["name"], 13.5, 0.5, 1.6), 0.42, z["name"], fill=col, size=13.5)
        y = Y0 + 0.62
        if z.get("desc"):
            text(s, x + 0.25, y, zw - 0.5, 0.36, z["desc"], size=11.5, color=C["accent_b_dark"] if k == 0 else C["deep"],
                 bold=True, valign="middle", fit=10, max_lines=1)
            y += 0.42
        its = z["items"]
        ih = (Y0 + zh - 0.15 - y - 0.12 * (len(its) - 1)) / len(its)
        for j, it in enumerate(its):
            iy = y + j * (ih + 0.12)
            card(d, s, x + 0.25, iy, zw - 0.5, ih, line_c=col)
            head_body(d, s, x + 0.4, iy + 0.04, zw - 0.8, ih - 0.08, it["head"], it.get("body"), hsize=13.5,
                      bsize=11.5, hcolor=C["accent_b_dark"] if k == 0 else C["deep"])
    lx = X0 + zw + 0.2
    lk = sp["link"]
    box(s, lx, Y0 + zh / 2 - 1.0, lw, 2.0, grad=(C["deep"], C["primary"]), grad_angle=90,
        shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.1)
    text(s, lx, Y0 + zh / 2 - 0.95, lw, 0.75, lk["name"], size=17, bold=True, color="FFFFFF", align="center",
         valign="middle", fit=12)
    box(s, lx + 0.35, Y0 + zh / 2 - 0.12, lw - 0.7, 0.36, fill="FFFFFF", shape=MSO_SHAPE.RIGHT_ARROW)
    if lk.get("desc"):
        text(s, lx + 0.08, Y0 + zh / 2 + 0.3, lw - 0.16, 0.65, lk["desc"], size=10.5, color=C["on_dark"],
             align="center", valign="middle", fit=9)
    if notes_h:
        text(s, X0, Y1 - notes_h + 0.08, CW, notes_h - 0.1,
             [[("注：", {"bold": True, "color": C["deep"]}), "；".join(sp["notes"])]], size=11, fit=9.5,
             valign="middle", max_lines=2)


# L23 构成等式 + 分类 + 方法
@layout("L23", "构成等式 + 分类 + 方法", "kpi", "量化 + 构成（总数 = 分项）",
        "一个总数由几部分构成（如 88 项 = 已有 54 + 新增 34），再列分类或计算/实施方法",
        {"total": O({"value": S(5), "label": S(10)}, opt=False), "parts": L(2, 3, {"value": S(5), "label": S(12)}),
         "note": S(30, opt=True), "categories": L(0, 6, {"name": S(8), "desc": S(18, opt=True)}, opt=True),
         "method": L(0, 5, {"head": S(8), "body": S(20, opt=True)}, opt=True)})
def r_equation(d, s, sp):
    C = d.C
    eq_h = 1.45
    box(s, X0, Y0, CW, eq_h, fill=C["pale2"], line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
    parts = sp["parts"]
    seq = [(sp["total"], C["deep"])] + [(p, [C["primary"], C["accent_a"], C["accent_b"]][k % 3]) for k, p in enumerate(parts)]
    n = len(seq)
    opw = 0.55
    cw = (CW - 0.6 - opw * (n - 1)) / n
    for k, (it, col) in enumerate(seq):
        x = X0 + 0.3 + k * (cw + opw)
        text(s, x, Y0 + 0.1, cw, 0.85, it["value"], size=44, bold=True, color=col, font=NUM_FONT, align="center",
             valign="middle", fit=26, max_lines=1)
        text(s, x, Y0 + 0.92, cw, 0.4, it["label"], size=12.5, color=C["text"], align="center", valign="middle",
             fit=10, max_lines=1)
        if k < n - 1:
            text(s, x + cw, Y0 + 0.2, opw, 0.7, "=" if k == 0 else "+", size=36, bold=True, color=C["gray"],
                 font=NUM_FONT, align="center", valign="middle", check=False)
    y = Y0 + eq_h + 0.12
    if sp.get("note"):
        text(s, X0, y, CW, 0.34, sp["note"], size=11.5, color=C["gray"], align="center", valign="middle", fit=10,
             max_lines=1)
        y += 0.4
    cats, meth = sp.get("categories") or [], sp.get("method") or []
    rows = (1 if cats else 0) + (1 if meth else 0)
    if not rows:
        return
    rh = (Y1 - y - 0.15 * (rows - 1)) / rows
    if cats:
        m = len(cats)
        cw2 = (CW - 0.18 * (m - 1)) / m
        cols = [C["primary"], C["accent_a"], C["accent_b"], C["deep"], C["light"], C["mid"]]
        for k, c in enumerate(cats):
            x = X0 + k * (cw2 + 0.18)
            box(s, x, y, cw2, 0.5, fill=cols[k % 6], shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.12)
            text(s, x, y, cw2, 0.5, c["name"], size=13.5, bold=True, color="FFFFFF", align="center", valign="middle",
                 fit=10.5, max_lines=1)
            card(d, s, x, y + 0.5, cw2, rh - 0.5, radius=0.04)
            if c.get("desc"):
                text(s, x + 0.1, y + 0.55, cw2 - 0.2, rh - 0.6, c["desc"], size=12, align="center", valign="middle",
                     fit=10, grow=13)
        y += rh + 0.15
    if meth:
        m = len(meth)
        aw = 0.3
        mw = (CW - 1.4 - aw * (m - 1)) / m
        text(s, X0, y, 1.3, rh, "方法", size=14, bold=True, color=C["deep"], valign="middle")
        for k, it in enumerate(meth):
            x = X0 + 1.4 + k * (mw + aw)
            card(d, s, x, y, mw, rh, line_c=C["border"])
            head_body(d, s, x + 0.12, y + 0.06, mw - 0.24, rh - 0.12, it["head"], it.get("body"), hsize=13.5,
                      bsize=11.5, align="center")
            if k < m - 1:
                chev(s, x + mw + 0.06, y + rh / 2 - 0.18, 0.18, 0.36, C["mid"])


# L24 彩色分类卡 + 指标条
@layout("L24", "彩色分类卡 + 指标条", "kpi", "量化（成果分类 + 关键指标）",
        "成果按 3–5 类列出（类名 + 数量 + 清单），下方一行 3–6 个关键技术指标",
        {"cats": L(3, 5, {"name": S(6), "count": S(5, opt=True), "items": L(1, 5, S(22))}),
         "title": S(12, opt=True), "kpis": L(0, 6, {"value": S(7), "label": S(10)}, opt=True)})
def r_cats(d, s, sp):
    C = d.C
    cats = sp["cats"]
    kp = sp.get("kpis") or []
    y = Y0
    if sp.get("title"):
        text(s, X0, y, CW, 0.36, sp["title"], size=14, bold=True, color=C["deep"], valign="middle", fit=11)
        y += 0.42
    kh = 1.05 if kp else 0
    ch_ = Y1 - y - (kh + 0.4 if kp else 0)
    n = len(cats)
    cw = (CW - 0.18 * (n - 1)) / n
    cols = [C["deep"], C["primary"], C["accent_a"], C["accent_b"], "7A63C9"]
    for k, c in enumerate(cats):
        x = X0 + k * (cw + 0.18)
        col = cols[k % 5]
        box(s, x, y, cw, 0.72, fill=col, shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.1)
        text(s, x + 0.15, y, cw - 1.0, 0.72, c["name"], size=15, bold=True, color="FFFFFF", valign="middle", fit=11,
             max_lines=1)
        if c.get("count"):
            text(s, x + cw - 1.0, y, 0.9, 0.72, c["count"], size=24, bold=True, color="FFFFFF", font=NUM_FONT,
                 align="right", valign="middle", fit=14, max_lines=1)
        box(s, x, y + 0.72, cw, ch_ - 0.72, fill="FFFFFF", line=col, shape=MSO_SHAPE.RECTANGLE)
        text(s, x + 0.15, y + 0.85, cw - 0.3, ch_ - 0.95,
             [[("·  ", {"color": col, "bold": True}), t] for t in c["items"]], size=12, fit=10, line_spacing=1.25,
             grow=13)
    if kp:
        ky = Y1 - kh
        text(s, X0, ky - 0.38, 3, 0.34, "关键技术指标", size=13.5, bold=True, color=C["deep"], valign="middle")
        m = len(kp)
        kw = (CW - 0.15 * (m - 1)) / m
        for k, it in enumerate(kp):
            x = X0 + k * (kw + 0.15)
            box(s, x, ky, kw, kh, fill=C["pale"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
            red = bool(re.search(r"[≥≤<>]", it["value"]))
            text(s, x + 0.05, ky + 0.05, kw - 0.1, 0.6, it["value"], size=24, bold=True,
                 color=C["red"] if red else C["primary"], font=NUM_FONT, align="center", valign="middle", fit=14,
                 max_lines=1)
            text(s, x + 0.05, ky + 0.62, kw - 0.1, 0.38, it["label"], size=11.5, color=C["text"], align="center",
                 valign="middle", fit=9.5, max_lines=1)


# L27 编码条 / 分段结构
@layout("L27", "编码条 / 分段结构", "structure", "构成（位段、字段、分段规则）",
        "编码规则、标识结构、字段组成：2–5 段，按位数比例画成色条，每段配说明",
        {"segments": L(2, 5, {"code": S(14), "label": S(8), "bits": S(6, opt=True), "desc": S(44)}),
         "total": S(12, opt=True), "notes": L(0, 3, {"head": S(10), "body": S(40)}, opt=True)})
def r_codebar(d, s, sp):
    C = d.C
    seg = sp["segments"]
    n = len(seg)
    cols = [C["deep"], C["primary"], C["accent_a"], C["accent_b"], C["light"]]
    bits = []
    for x in seg:
        m = re.search(r"\d+", x.get("bits") or "")
        bits.append(int(m.group()) if m else 4)
    minw = 1.45
    tot = float(sum(bits))
    gap = 0.06
    raw = [max(minw, (CW - gap * (n - 1)) * b / tot) for b in bits]
    sc = (CW - gap * (n - 1)) / sum(raw)
    ws = [w * sc for w in raw]
    y = Y0 + (0.45 if sp.get("total") else 0.1)
    if sp.get("total"):
        text(s, X0, Y0, CW, 0.38, sp["total"], size=13, bold=True, color=C["deep"], valign="middle", fit=11)
    x = X0
    for k, it in enumerate(seg):
        text(s, x, y, ws[k], 0.3, ("%s · %s" % (it["label"], it["bits"])) if it.get("bits") else it["label"],
             size=10.5, color=C["gray"], valign="middle", fit=9, max_lines=1)
        box(s, x, y + 0.32, ws[k], 0.72, fill=cols[k % 5])
        text(s, x + 0.05, y + 0.32, ws[k] - 0.1, 0.72, it["code"], size=20, bold=True, color="FFFFFF",
             font=NUM_FONT if re.fullmatch(r"[A-Za-z0-9 ]+", it["code"]) else FONT, align="center", valign="middle",
             fit=11, max_lines=1)
        x += ws[k] + gap
    cy = y + 1.25
    notes = sp.get("notes") or []
    nh = 1.15 if notes else 0
    ch_ = Y1 - cy - (nh + 0.15 if notes else 0)
    cw = (CW - 0.18 * (n - 1)) / n
    for k, it in enumerate(seg):
        x = X0 + k * (cw + 0.18)
        card(d, s, x, cy, cw, ch_)
        box(s, x + 0.2, cy + 0.22, 0.14, 0.14, fill=cols[k % 5], shape=MSO_SHAPE.OVAL)
        text(s, x + 0.42, cy + 0.1, cw - 0.55, 0.4, it["label"], size=14, bold=True, color=cols[k % 5],
             valign="middle", fit=11, max_lines=1)
        text(s, x + 0.2, cy + 0.55, cw - 0.4, ch_ - 0.65, it["desc"], size=12, fit=10, line_spacing=1.2, grow=13.5)
    if notes:
        y2 = Y1 - nh
        m = len(notes)
        nw = (CW - 0.18 * (m - 1)) / m
        for k, it in enumerate(notes):
            x = X0 + k * (nw + 0.18)
            box(s, x, y2, nw, nh, fill=C["pale"], line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
            head_body(d, s, x + 0.2, y2 + 0.06, nw - 0.4, nh - 0.12, it["head"], it["body"], hsize=13.5, bsize=11.5)


# L28 数字卡网格 + 特征清单
@layout("L28", "数字卡网格 + 特征清单", "kpi", "量化（多个数量 + 共同特征）",
        "资料、数据、资源盘点：2–6 张数字卡（数量 + 类别 + 说明），右侧列共同特征（格式、坐标、现势性等）",
        {"cards": L(2, 6, {"value": S(6), "label": S(10), "desc": S(40, opt=True)}),
         "side": O({"title": S(8), "items": L(1, 5, {"head": S(8), "body": S(26)})}, opt=True)})
def r_numgrid(d, s, sp):
    C = d.C
    cards = sp["cards"]
    side = sp.get("side")
    gw = CW - (4.1 if side else 0)
    n = len(cards)
    cols = 2 if n <= 4 else 3
    rows = math.ceil(n / cols)
    cw = (gw - 0.2 * (cols - 1)) / cols
    ch_ = (CH - 0.05 - 0.2 * (rows - 1)) / rows
    for k, c in enumerate(cards):
        x = X0 + (k % cols) * (cw + 0.2)
        y = Y0 + (k // cols) * (ch_ + 0.2)
        card(d, s, x, y, cw, ch_, line_c=C["border"])
        col = C["primary"] if k % 2 == 0 else C["accent_a"]
        pw = min(cw - 0.4, pill_width(c["label"], 12, 0.4, 1.0))
        vw = text_width(c["value"], 40, True) + 0.2
        if vw + pw + 0.5 <= cw:          # 数字与类别同一行
            text(s, x + 0.2, y + 0.1, cw - pw - 0.5, 0.85, c["value"], size=40, bold=True, color=col, font=NUM_FONT,
                 valign="middle", fit=22, max_lines=1)
            pill(d, s, x + cw - pw - 0.2, y + 0.33, pw, 0.38, c["label"], fill=col, size=12)
            dy = 1.0
        else:                            # 卡片窄：类别放到数字下方
            text(s, x + 0.2, y + 0.08, cw - 0.4, 0.8, c["value"], size=36, bold=True, color=col, font=NUM_FONT,
                 valign="middle", fit=20, max_lines=1)
            pill(d, s, x + 0.2, y + 0.9, pw, 0.36, c["label"], fill=col, size=11.5)
            dy = 1.36
        if c.get("desc"):
            text(s, x + 0.2, y + dy, cw - 0.4, ch_ - dy - 0.1, c["desc"], size=12, fit=10, line_spacing=1.2, grow=13.5)
    if side:
        rx = X0 + gw + 0.25
        rw = X1 - rx
        box(s, rx, Y0, rw, CH - 0.05, fill=C["pale2"], line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
        text(s, rx + 0.25, Y0 + 0.15, rw - 0.5, 0.42, side["title"], size=15, bold=True, color=C["deep"],
             valign="middle", fit=12)
        its = side["items"]
        ih = (CH - 0.8) / len(its)
        icons = pick_icons([i["head"] for i in its])
        for k, it in enumerate(its):
            y = Y0 + 0.7 + k * ih
            icon_circle(d, s, rx + 0.25, y + 0.05, 0.46, icons[k])
            head_body(d, s, rx + 0.85, y, rw - 1.05, ih - 0.08, it["head"], it["body"], hsize=13, bsize=11)


# L30 甘特表
@layout("L30", "甘特表", "time", "时间（并行阶段、月度进度）",
        "实施进度按月（或季度）排布：多个阶段/任务的起止时段，验收节点用星标；时段列 3–12 个",
        {"periods": L(3, 12, S(7)), "rows": L(2, 12, {"name": S(20), "start": S(7), "end": S(7),
                                                      "milestone": B(opt=True), "group": S(6, opt=True)}),
         "footnote": S(26, opt=True)})
def r_gantt(d, s, sp):
    C = d.C
    per = sp["periods"]
    rows = sp["rows"]
    np_, nr = len(per), len(rows)
    nw = 3.6
    pw = (CW - nw) / np_
    hh = 0.46
    foot = 0.85 if sp.get("footnote") else 0.45
    rh = min(0.48, (CH - 0.05 - hh - foot) / nr)
    box(s, X0, Y0, CW, hh, fill=C["deep"])
    text(s, X0, Y0, nw, hh, "阶段 / 任务", size=12.5, bold=True, color="FFFFFF", align="center", valign="middle")
    for j, p in enumerate(per):
        text(s, X0 + nw + j * pw, Y0, pw, hh, p, size=11.5, bold=True, color="FFFFFF", align="center",
             valign="middle", fit=9, max_lines=1)
    groups = []
    for r in rows:
        g = r.get("group") or ""
        if g and g not in groups:
            groups.append(g)
    palette = [C["primary"], C["accent_a"], C["light"], C["mid"], C["deep"]]
    gcol = {g: palette[k % 5] for k, g in enumerate(groups)}

    def idx(v, default):
        v = (v or "").strip()
        for j, p in enumerate(per):
            if v == p or (v and (v in p or p in v)):
                return j
        return default
    for i, r in enumerate(rows):
        y = Y0 + hh + i * rh
        box(s, X0, y, CW, rh, fill=C["pale2"] if i % 2 else "FFFFFF", line=C["line"], lw=0.5)
        ms = r.get("milestone")
        text(s, X0 + 0.12, y, nw - 0.2, rh, r["name"], size=12, bold=ms, color=C["accent_b_dark"] if ms else C["text"],
             valign="middle", fit=9.5, max_lines=1)
        a = idx(r.get("start"), 0)
        b = idx(r.get("end"), a)
        if b < a:
            a, b = b, a
        if ms:
            cx = X0 + nw + b * pw + pw / 2
            box(s, cx - 0.17, y + rh / 2 - 0.17, 0.34, 0.34, fill=C["accent_b"], shape=MSO_SHAPE.STAR_5_POINT)
        else:
            box(s, X0 + nw + a * pw + 0.04, y + 0.08, (b - a + 1) * pw - 0.08, rh - 0.16,
                fill=gcol.get(r.get("group") or "", C["primary"]), shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.2)
    for j in range(np_ + 1):
        x = X0 + nw + j * pw
        line(s, x, Y0 + hh, x, Y0 + hh + nr * rh, C["line"], w=0.5)
    ly = Y0 + hh + nr * rh + 0.12
    lx = X0
    for g in groups:
        box(s, lx, ly + 0.08, 0.32, 0.2, fill=gcol[g])
        text(s, lx + 0.38, ly, 1.6, 0.36, g, size=11, color=C["text"], valign="middle", fit=9, max_lines=1)
        lx += 0.4 + max(0.8, text_width(g, 11) + 0.3)
    if any(r.get("milestone") for r in rows):
        box(s, lx, ly + 0.03, 0.3, 0.3, fill=C["accent_b"], shape=MSO_SHAPE.STAR_5_POINT)
        text(s, lx + 0.36, ly, 1.2, 0.36, "验收节点", size=11, color=C["text"], valign="middle")
        lx += 1.6
    if sp.get("footnote"):
        text(s, X0, ly + 0.38, CW, 0.34, sp["footnote"], size=10.5, color=C["gray"], valign="middle", fit=9,
             max_lines=1)


# ================================================================== 第三批：评审高频结构（0.7）
# L53 范围边界 / 接口关系
@layout("L53", "范围边界与接口", "boundary", "边界/接口（本项目与周边系统、分包的分工）",
        "分包边界、系统对接：中间是本项目/本包承担的内容，两侧是 2–4 个相关方（其他分包、外部系统），"
        "写清它们负责什么（本项目不做）以及与本项目的接口",
        {"center": {"name": S(10), "items": L(2, 6, S(16))},
         "parties": L(2, 4, {"name": S(10), "scope": S(40), "interface": S(14, opt=True)}),
         "footer": FOOTER})
def r_boundary(d, s, sp):
    from .prim import lines_needed
    C = d.C
    ps = sp["parties"]
    bottom = 5.9 if sp.get("footer") else 6.5
    cw = 4.3
    cx0 = X0 + (CW - cw) / 2
    # 中间：本项目
    box(s, cx0, Y0, cw, bottom - Y0, grad=(C["deep"], C["primary"]), grad_angle=300,
        shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
    text(s, cx0 + 0.2, Y0 + 0.12, cw - 0.4, 0.5, sp["center"]["name"], size=17, bold=True, color="FFFFFF",
         align="center", valign="middle", fit=13, max_lines=1)
    box(s, cx0 + cw / 2 - 0.3, Y0 + 0.66, 0.6, 0.04, fill="FFFFFF")
    items = sp["center"]["items"]
    top = Y0 + 0.85
    ih = min(0.62, (bottom - 0.15 - top - 0.1 * (len(items) - 1)) / len(items))
    blk = len(items) * ih + (len(items) - 1) * 0.1
    top += max(0, (bottom - 0.15 - top - blk) / 2)
    for j, it in enumerate(items):
        y = top + j * (ih + 0.1)
        box(s, cx0 + 0.25, y, cw - 0.5, ih, fill="FFFFFF", alpha=0.16, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.2)
        text(s, cx0 + 0.35, y, cw - 0.7, ih, it, size=13, color="FFFFFF", align="center", valign="middle", fit=10.5,
             max_lines=2)
    # 两侧：相关方（左右交替，先左后右）；框高按内容，整组垂直居中
    sw = cx0 - X0 - 0.55
    for side, grp in (("L", ps[0::2]), ("R", ps[1::2])):
        if not grp:
            continue
        need = []
        for p in grp:
            ln = lines_needed([p["scope"]], 12.5, sw - 0.34)
            need.append(0.56 + ln * 12.5 * 1.25 / 72 + 0.2 + (0.5 if p.get("interface") else 0))
        avail = bottom - Y0
        g = 0.3
        scale = min(1.0, (avail - g * (len(grp) - 1)) / sum(need))
        hs = [max(1.2, h * scale) for h in need]
        tot = sum(hs) + g * (len(grp) - 1)
        if tot > avail:
            hs = [(avail - g * (len(grp) - 1)) / len(grp)] * len(grp)
            tot = avail
        y = Y0 + (avail - tot) / 2
        for p, gh in zip(grp, hs):
            x = X0 if side == "L" else cx0 + cw + 0.55
            card(d, s, x, y, sw, gh, line_c=C["border"])
            box(s, x, y, sw, 0.5, fill=C["pale"], shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.12)
            add_icon(s, "minus-circle-outline", x + 0.14, y + 0.11, 0.28, C["gray"])
            text(s, x + 0.5, y, sw - 0.65, 0.5, p["name"], size=14, bold=True, color=C["deep"], valign="middle",
                 fit=11, max_lines=1)
            ih2 = 0.42 if p.get("interface") else 0
            text(s, x + 0.17, y + 0.56, sw - 0.34, gh - 0.62 - ih2 - 0.06, p["scope"], size=12.5, color=C["text"],
                 fit=10, line_spacing=1.2)
            if p.get("interface"):
                box(s, x + 0.14, y + gh - ih2 - 0.1, sw - 0.28, ih2, fill=C["accent_a_pale"], line=C["accent_a"],
                    shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.3)
                text(s, x + 0.22, y + gh - ih2 - 0.1, sw - 0.44, ih2, [("接口  ", {"bold": True, "color": C["accent_a"]}),
                                                                   (p["interface"], {})],
                     size=11.5, color=C["text"], valign="middle", fit=9.5, max_lines=1)
            ly = y + gh / 2
            x1, x2 = (x + sw, cx0) if side == "L" else (x, cx0 + cw)
            line(s, min(x1, x2) + 0.05, ly, max(x1, x2) - 0.05, ly, C["accent_a"], w=1.75, dash=DASH.DASH,
                 head="triangle", tail="triangle")
            y += gh + g
    if sp.get("footer"):
        footer_strip(d, s, sp["footer"])


# L54 状态流转（含退回）
@layout("L54", "状态流转", "flow", "递进/流程（状态流转，含退回、分支）",
        "对象在几个状态之间流转（如 草稿→待审核→已发布→停用），有未通过退回、驳回重办等回路；3–6 个状态",
        {"states": L(3, 6, {"name": S(8), "body": S(30, opt=True)}),
         "returns": L(0, 2, {"from": S(8), "to": S(8), "label": S(12)}, opt=True),
         "rules": L(0, 3, S(40), opt=True)})
def r_states(d, s, sp):
    C = d.C
    st = sp["states"]
    n = len(st)
    gap = 0.55
    sw = (CW - gap * (n - 1)) / n
    rules = sp.get("rules") or []
    rets = sp.get("returns") or []
    from .prim import lines_needed
    mx = max([lines_needed([x.get("body", "")], 12, sw - 0.24) for x in st if x.get("body")] or [1])
    sh = 0.6 + max(0.9, mx * 12 * 1.25 / 72 + 0.3)
    # 同一目标、同一说明的多条退回（如“业务审核/合规审核 未通过退回草稿”）合并成一条回路，标签只写一次（0.7.0-dev8）
    groups = []
    for r in rets[:2]:
        g = next((g for g in groups if g["to"] == r.get("to") and g["label"] == r.get("label")), None)
        if g:
            g["froms"].append(r.get("from"))
        else:
            groups.append({"to": r.get("to"), "label": r.get("label"), "froms": [r.get("from")]})
    ret_h = 0.35 + 0.45 * len(groups) if rets else 0
    rule_h = min(1.1, max(0.7, Y1 - Y0 - sh - ret_h - 0.5)) if rules else 0
    used = sh + ret_h + (rule_h + 0.3 if rules else 0)
    sy = Y0 + max(0.1, (Y1 - Y0 - used) / 2)
    names = [x["name"] for x in st]

    def idx(nm):
        nm = (nm or "").strip()
        for i, x in enumerate(names):
            if nm and (nm == x or nm in x or x in nm):
                return i
        return None
    for k, it in enumerate(st):
        x = X0 + k * (sw + gap)
        last = k == n - 1
        col = C["primary"] if not last else C["deep"]
        box(s, x, sy, sw, 0.6, fill=col, shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.18)
        badge(d, s, x + 0.12, sy + 0.14, 0.32, k + 1, fill="FFFFFF", color=col, fsize=11)
        text(s, x + 0.5, sy, sw - 0.58, 0.6, it["name"], size=15, bold=True, color="FFFFFF", valign="middle", fit=11,
             max_lines=1)
        box(s, x, sy + 0.6, sw, sh - 0.6, fill=C["pale2"], line=C["line"], shape=MSO_SHAPE.RECTANGLE)
        if it.get("body"):
            text(s, x + 0.12, sy + 0.7, sw - 0.24, sh - 0.8, it["body"], size=12, fit=10, line_spacing=1.2)
        if k < n - 1:
            line(s, x + sw + 0.06, sy + 0.3, x + sw + gap - 0.06, sy + 0.3, C["primary"], w=2, tail="triangle")
    # 退回回路：从状态下方折线回到目标状态
    by = sy + sh
    for r_i, g in enumerate(groups):
        b = idx(g["to"])
        srcs = [a for a in (idx(f) for f in g["froms"]) if a is not None and a != b]
        if b is None or not srcs:
            continue
        xb = X0 + b * (sw + gap) + sw * (0.4 if r_i == 0 else 0.25)
        yy = by + 0.35 + r_i * 0.45
        xas = [X0 + a * (sw + gap) + sw * (0.6 if r_i == 0 else 0.75) for a in srcs]
        far = max(xas, key=lambda x: abs(x - xb))
        arrow(s, [(far, by), (far, yy), (xb, yy), (xb, by + 0.02)], C["accent_b"], w=1.75, dash=DASH.DASH)
        for xa in xas:
            if xa != far:                       # 其余来源只画竖线汇入同一条回路
                line(s, xa, by, xa, yy, C["accent_b"], w=1.75, dash=DASH.DASH)
        near = min(xas, key=lambda x: abs(x - xb))
        lw_ = min(abs(near - xb) - 0.2, pill_width(g["label"] or "", 11, 0.4))
        pill(d, s, (near + xb) / 2 - lw_ / 2, yy - 0.16, lw_, 0.32, g["label"] or "", fill="FFFFFF",
             color=C["accent_b_dark"], size=11, line=C["accent_b"])
    if rules:
        ry = by + ret_h + 0.3
        rh = rule_h
        if rh > 0.5:
            rw = (CW - 0.2 * (len(rules) - 1)) / len(rules)
            for k, t in enumerate(rules):
                x = X0 + k * (rw + 0.2)
                box(s, x, ry, rw, rh, fill=C["accent_b_pale"], line=C["accent_b_border"],
                    shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
                add_icon(s, "information-outline", x + 0.15, ry + rh / 2 - 0.16, 0.32, C["accent_b"])
                text(s, x + 0.55, ry, rw - 0.65, rh, t, size=12, color=C["text"], valign="middle", fit=10,
                     line_spacing=1.15)


# L55 问题 — 原因 — 对策
@layout("L55", "问题—原因—对策", "compare", "对比/因果（问题、成因、应对三栏逐行对应）",
        "重难点与应对、痛点分析：2–4 个问题，每个问题写清成因和对应措施，一行一个问题横着读",
        {"labels": O({"problem": S(6), "cause": S(6), "measure": S(6)}),
         "rows": L(2, 4, {"problem": S(14), "cause": S(34), "measure": S(40)})})
def r_problem_cause(d, s, sp):
    C = d.C
    lb = sp.get("labels") or {}
    rows = sp["rows"]
    n = len(rows)
    w1, w2 = 3.0, 3.85
    w3 = CW - w1 - w2 - 0.6
    xs = [X0, X0 + w1 + 0.3, X0 + w1 + w2 + 0.6]
    ws = [w1, w2, w3]
    heads = [lb.get("problem") or "问题", lb.get("cause") or "成因", lb.get("measure") or "应对措施"]
    cols = [C["accent_b"], C["mid"], C["primary"]]
    for k in range(3):
        pill(d, s, xs[k], Y0, ws[k], 0.42, heads[k], fill=cols[k], size=13.5)
    top = Y0 + 0.58
    rh = (Y1 - top - 0.15 * (n - 1)) / n
    icons = pick_icons([r["problem"] for r in rows])
    for i, r in enumerate(rows):
        y = top + i * (rh + 0.15)
        box(s, xs[0], y, w1, rh, fill=C["accent_b_pale"], line=C["accent_b_border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.08)
        sz = min(0.56, rh - 0.2)
        box(s, xs[0] + 0.15, y + (rh - sz) / 2, sz, sz, fill=C["accent_b"], shape=MSO_SHAPE.HEXAGON)
        add_icon(s, icons[i], xs[0] + 0.15 + sz * 0.22, y + (rh - sz) / 2 + sz * 0.22, sz * 0.56)
        text(s, xs[0] + sz + 0.28, y, w1 - sz - 0.38, rh, r["problem"], size=14, bold=True, color=C["accent_b_dark"],
             valign="middle", fit=11, max_lines=2)
        card(d, s, xs[1], y, w2, rh, line_c=C["line"])
        text(s, xs[1] + 0.15, y, w2 - 0.3, rh, r["cause"], size=12, color=C["text"], valign="middle", fit=10,
             line_spacing=1.15)
        box(s, xs[2], y, w3, rh, fill=C["pale"], line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        box(s, xs[2], y, 0.07, rh, fill=C["primary"])
        text(s, xs[2] + 0.2, y, w3 - 0.32, rh, r["measure"], size=12.5, color=C["text"], valign="middle", fit=10,
             line_spacing=1.15)
        for k in (0, 1):
            chev(s, xs[k] + ws[k] + 0.07, y + rh / 2 - 0.13, 0.16, 0.26, C["mid"])


# L56 分类树
@layout("L56", "分类树", "arch", "层级（总—分类—子类，树状分叉）",
        "分类目录、指标体系、知识分类：一个总类分成 2–5 个类别，每类 1–5 个子项",
        {"root": S(12), "root_sub": S(30, opt=True),
         "branches": L(2, 5, {"name": S(8), "leaves": L(1, 5, S(12))}), "footer": FOOTER})
def r_tree(d, s, sp):
    C = d.C
    br = sp["branches"]
    n = len(br)
    bottom = 5.9 if sp.get("footer") else 6.5
    lmax = max(len(b["leaves"]) for b in br)
    lh0 = 0.56
    tot = (0.62 if not sp.get("root_sub") else 0.86) + 0.55 + 0.55 + 0.2 + lmax * lh0 + (lmax - 1) * 0.12
    y_off = max(0, (bottom - Y0 - tot) / 2)
    Y0_ = Y0 + y_off
    rw = 4.2 if not sp.get("root_sub") else 5.2
    rx = X0 + (CW - rw) / 2
    rh = 0.62 if not sp.get("root_sub") else 0.86
    box(s, rx, Y0_, rw, rh, grad=(C["deep"], C["primary"]), grad_angle=0, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.2)
    text(s, rx + 0.2, Y0_ + 0.02, rw - 0.4, 0.58, sp["root"], size=17, bold=True, color="FFFFFF", align="center",
         valign="middle", fit=13, max_lines=1)
    if sp.get("root_sub"):
        text(s, rx + 0.2, Y0_ + 0.52, rw - 0.4, 0.32, sp["root_sub"], size=11, color=C["on_dark"], align="center",
             valign="middle", fit=9.5, max_lines=1)
    gap = 0.22
    bw = (CW - gap * (n - 1)) / n
    bus = Y0_ + rh + 0.3
    line(s, X0 + rx - X0 + rw / 2, Y0_ + rh, X0 + rx - X0 + rw / 2, bus, C["border"], w=1.5)
    line(s, X0 + bw / 2, bus, X1 - bw / 2, bus, C["border"], w=1.5)
    by = bus + 0.25
    for k, b in enumerate(br):
        x = X0 + k * (bw + gap)
        cx = x + bw / 2
        line(s, cx, bus, cx, by, C["border"], w=1.5)
        box(s, x, by, bw, 0.55, fill=C["primary"] if k % 2 == 0 else C["deep"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.18)
        text(s, x + 0.1, by, bw - 0.2, 0.55, b["name"], size=14.5, bold=True, color="FFFFFF", align="center",
             valign="middle", fit=11, max_lines=1)
        lv = b["leaves"]
        top = by + 0.75
        lh = min(lh0, (bottom - top - 0.12 * (len(lv) - 1)) / len(lv))
        sx = x + 0.22
        line(s, sx, by + 0.55, sx, top + (len(lv) - 1) * (lh + 0.12) + lh / 2, C["border"], w=1)
        for j, t in enumerate(lv):
            y = top + j * (lh + 0.12)
            line(s, sx, y + lh / 2, sx + 0.18, y + lh / 2, C["border"], w=1)
            box(s, sx + 0.18, y, bw - 0.4, lh, fill=C["pale2"], line=C["line"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
                radius=0.2)
            text(s, sx + 0.26, y, bw - 0.56, lh, t, size=12, color=C["text"], valign="middle", fit=10, max_lines=1)
    if sp.get("footer"):
        footer_strip(d, s, sp["footer"])


# L57 图标网格（6–9 项并列）
@layout("L57", "图标网格", "grid", "并列（6–9 项）",
        "6–9 个并列的场景、功能、数据类别（如七大智能场景、九类运维数据），每项一个名称和一句说明",
        {"items": L(6, 9, {"head": S(9), "body": S(28, opt=True)}), "footer": FOOTER})
def r_grid(d, s, sp):
    C = d.C
    v = (sp.get("_k") or {}).get("v", 0)        # 外观变体（0.7.0-dev16）：1 = 浅色底顶部色条、编号圆标，文字位置不变
    it = sp["items"]
    n = len(it)
    cols = 3 if n in (6, 9) else 4
    rows = (n + cols - 1) // cols
    bottom = 5.9 if sp.get("footer") else 6.5
    gx, gy = 0.2, 0.18
    cw = (CW - gx * (cols - 1)) / cols
    ch = (bottom - Y0 - gy * (rows - 1)) / rows
    icons = pick_icons([x["head"] for x in it])
    for k, x in enumerate(it):
        r, c = divmod(k, cols)
        # 最后一行不满时居中
        in_row = cols if r < rows - 1 else n - cols * (rows - 1)
        off = (cols - in_row) * (cw + gx) / 2 if r == rows - 1 else 0
        xx = X0 + off + c * (cw + gx)
        yy = Y0 + r * (ch + gy)
        sz = min(0.7, ch - 0.35)
        if v == 1:
            card(d, s, xx, yy, cw, ch, fill=C["pale2"], line_c=C["line"])
            box(s, xx, yy, cw, 0.07, fill=C["primary"] if k % 2 == 0 else C["deep"])
            badge(d, s, xx + 0.2 + sz * 0.12, yy + (ch - sz * 0.76) / 2, sz * 0.76, k + 1,
                  fill=C["primary"] if k % 2 == 0 else C["deep"], fsize=13)
        else:
            card(d, s, xx, yy, cw, ch, line_c=C["line"])
            icon_circle(d, s, xx + 0.2, yy + (ch - sz) / 2, sz, icons[k],
                        *((C["accent_a"], "128A47") if k % 3 == 1 else (None, None)))
        head_body(d, s, xx + sz + 0.35, yy + 0.08, cw - sz - 0.5, ch - 0.16, x["head"], x.get("body"), hsize=14.5,
                  bsize=11.5)
    if sp.get("footer"):
        footer_strip(d, s, sp["footer"])


# ================================================================== 分区组合（0.7）
# 一页分上下或左右两区，每区一种结构。区内块都接受方框 (x, y, w, h)，尺寸与字数上限按区域面积预先算好；
# 模型只选组合、按格子填内容。使用前提：一页里有两类性质不同的信息（如流程 + 各环节的做法、问题对策 + 成效指标）。
def zone_label(d, s, x, y, txt, col=None):
    if txt:
        pw = pill_width(txt, 12, 0.36, 1.0)
        pill(d, s, x, y, pw, 0.34, txt, fill=col or d.C["deep"], size=12)
        return 0.46
    return 0


def blk_hflow(d, s, x, y, w, h, steps, v=0):
    """横向步骤条：箭头色块 + 步骤名，下方一行短说明。v=1：描边浅底箭头、深色字（0.7.0-dev16，位置不变）。"""
    C = d.C
    n = len(steps)
    gap = 0.12
    sw = (w - gap * (n - 1)) / n
    has_body = any(st.get("body") for st in steps)
    hh = min(0.78, h if not has_body else h * 0.5)
    for k, st in enumerate(steps):
        xx = x + k * (sw + gap)
        shp = MSO_SHAPE.PENTAGON if k == 0 else MSO_SHAPE.CHEVRON
        if v == 1:
            box(s, xx, y, sw + (0.12 if k < n - 1 else 0), hh, fill=C["pale"], line=C["primary"], lw=1.25, shape=shp)
        else:
            box(s, xx, y, sw + (0.12 if k < n - 1 else 0), hh, fill=C["primary"] if k % 2 == 0 else C["deep"], shape=shp)
        text(s, xx + (0.12 if k else 0.08), y, sw - 0.3, hh, "%d  %s" % (k + 1, st["head"]), size=14, bold=True,
             color=C["deep"] if v == 1 else "FFFFFF", align="center", valign="middle", fit=10.5, max_lines=1)
        if st.get("body"):
            text(s, xx + 0.08, y + hh + 0.08, sw - 0.16, h - hh - 0.08, st["body"], size=13, color=C["text"],
                 align="center", fit=10, line_spacing=1.15)


def blk_cards(d, s, x, y, w, h, cols, v=0):
    """并列卡片：标题条 + 圆点条目。v=1：彩色标题带 + 白字 + 编号圆标（0.7.0-dev16，文字位置不变）。"""
    C = d.C
    n = len(cols)
    gap = 0.2
    cw = (w - gap * (n - 1)) / n
    icons = pick_icons([c["title"] for c in cols])
    for k, c in enumerate(cols):
        xx = x + k * (cw + gap)
        card(d, s, xx, y, cw, h)
        col = C["primary"] if k % 2 == 0 else C["accent_a"]
        if v == 1:
            box(s, xx, y, cw, 0.66, fill=col, shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.1)
            badge(d, s, xx + 0.22, y + 0.13, 0.4, k + 1, fill="FFFFFF", color=col, fsize=12)
        else:
            box(s, xx, y, 0.07, h, fill=col)
            icon_circle(d, s, xx + 0.2, y + 0.14, 0.44, icons[k],
                        *((C["accent_a"], "128A47") if k % 2 else (None, None)))
        text(s, xx + 0.72, y + 0.1, cw - 0.85, 0.52, c["title"], size=14.5, bold=True,
             color="FFFFFF" if v == 1 else C["deep"], valign="middle", fit=11, max_lines=1)
        items = c.get("items") or []
        if items:
            text(s, xx + 0.22, y + 0.72, cw - 0.38, h - 0.8, [[("·  ", {"color": C["primary"], "bold": True}), it]
                                                               for it in items], size=13, fit=10, line_spacing=1.45)


def blk_pairs(d, s, x, y, w, h, rows):
    """问题 → 对策 成对行。"""
    C = d.C
    n = len(rows)
    g = 0.12
    rh = (h - g * (n - 1)) / n
    lw = w * 0.36
    for i, r in enumerate(rows):
        yy = y + i * (rh + g)
        box(s, x, yy, lw, rh, fill=C["accent_b_pale"], line=C["accent_b_border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
            radius=0.1)
        badge(d, s, x + 0.14, yy + rh / 2 - 0.17, 0.34, i + 1, fill=C["accent_b"], fsize=11)
        text(s, x + 0.58, yy, lw - 0.66, rh, r["problem"], size=13, bold=True, color=C["accent_b_dark"],
             valign="middle", fit=10.5, max_lines=2)
        chev(s, x + lw + 0.08, yy + rh / 2 - 0.13, 0.16, 0.26, C["mid"])
        box(s, x + lw + 0.34, yy, w - lw - 0.34, rh, fill=C["pale2"], line=C["line"],
            shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.1)
        text(s, x + lw + 0.48, yy, w - lw - 0.6, rh, r["measure"], size=12.5, color=C["text"], valign="middle",
             fit=10, line_spacing=1.15)


def blk_kpis(d, s, x, y, w, h, kpis):
    """数字条：大数字 + 单位 + 说明，横排。"""
    C = d.C
    n = len(kpis)
    gap = 0.2
    kw = (w - gap * (n - 1)) / n
    for k, kp in enumerate(kpis):
        xx = x + k * (kw + gap)
        box(s, xx, y, kw, h, fill=C["pale"], line=C["border"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        col = C["accent_b"] if (k == n - 1 and n > 2) else C["primary"]
        vh = min(0.9, h * 0.55)
        text(s, xx + 0.1, y + 0.08, kw - 0.2, vh, [(kp["value"], {"font": NUM_FONT}), (" " + kp.get("unit", ""),
                                                                                     {"size": 15, "color": C["deep"]})],
             size=38, bold=True, color=col, align="center", valign="middle", fit=20, max_lines=1)
        text(s, xx + 0.12, y + 0.08 + vh, kw - 0.24, h - vh - 0.14, kp["label"], size=12, color=C["gray"],
             align="center", valign="top", fit=10, max_lines=2)


def blk_vsteps(d, s, x, y, w, h, steps):
    """竖向步骤：编号圆 + 竖线 + 步骤名与说明。"""
    C = d.C
    n = len(steps)
    g = 0.1
    rh = (h - g * (n - 1)) / n
    cx = x + 0.3
    line(s, cx, y + rh / 2, cx, y + (n - 1) * (rh + g) + rh / 2, C["border"], w=1.5, dash=DASH.DASH)
    for k, st in enumerate(steps):
        yy = y + k * (rh + g)
        sz = min(0.5, rh - 0.1)
        badge(d, s, cx - sz / 2, yy + (rh - sz) / 2, sz, k + 1, fill=C["primary"], fsize=13)
        box(s, x + 0.72, yy, w - 0.72, rh, fill=C["pale2"] if k % 2 == 0 else "FFFFFF", line=C["line"],
            shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.1)
        head_body(d, s, x + 0.88, yy + 0.04, w - 1.0, rh - 0.08, st["head"], st.get("body"), hsize=14, bsize=12)


def blk_list_panel(d, s, x, y, w, h, title, items, col=None):
    """要点面板：色头 + 图标条目。"""
    C = d.C
    col = col or C["accent_a"]
    card(d, s, x, y, w, h)
    box(s, x, y, w, 0.56, fill=col, shape=MSO_SHAPE.ROUND_2_SAME_RECTANGLE, radius=0.1)
    text(s, x + 0.2, y, w - 0.4, 0.56, title, size=15, bold=True, color="FFFFFF", valign="middle", fit=12, max_lines=1)
    n = len(items)
    top = y + 0.7
    ih = (y + h - 0.12 - top) / n
    icons = pick_icons([it["head"] for it in items])
    for j, it in enumerate(items):
        yy = top + j * ih
        sz = min(0.44, ih - 0.16)
        icon_circle(d, s, x + 0.18, yy + 0.06, sz, icons[j], col, "128A47" if col == C["accent_a"] else None)
        head_body(d, s, x + 0.3 + sz, yy, w - 0.45 - sz, ih - 0.06, it["head"], it.get("body"), hsize=13.5, bsize=12)
        if j < n - 1:
            line(s, x + 0.18, yy + ih - 0.03, x + w - 0.18, yy + ih - 0.03, C["line"], w=0.5)


@layout("L60", "上流程 + 下卡片", "combo", "分区：流程 + 并列（流程各环节的做法/产出）",
        "一页有两类信息：上面是 3–5 步的流程，下面是 2–4 组并列的做法、产出或保障措施（与流程不重复）",
        {"flow_label": S(8, opt=True), "flow": L(3, 5, {"head": S(8), "body": S(22, opt=True)}),
         "cards_label": S(8, opt=True), "cards": L(2, 4, {"title": S(10), "items": L(1, 3, S(22))})})
def r_combo_flow_cards(d, s, sp):
    C = d.C
    y = Y0
    y += zone_label(d, s, X0, y, sp.get("flow_label"))
    from .prim import lines_needed
    sw_ = (CW - 0.12 * (len(sp["flow"]) - 1)) / len(sp["flow"])
    bl = max([lines_needed([x.get("body", "")], 13, sw_ - 0.16) for x in sp["flow"] if x.get("body")] or [0])
    fh = 0.78 + (0.12 + bl * 13 * 1.2 / 72 + 0.05 if bl else 0)
    v = (sp.get("_k") or {}).get("v", 0)        # 外观变体（0.7.0-dev16）
    blk_hflow(d, s, X0, y, CW, fh, sp["flow"], v=v)
    y += fh + 0.22
    line(s, X0, y - 0.11, X1, y - 0.11, C["line"], w=0.75, dash=DASH.DASH)
    y += zone_label(d, s, X0, y, sp.get("cards_label"), C["accent_a"])
    blk_cards(d, s, X0, y, CW, Y1 - y, sp["cards"], v=v)


@layout("L61", "问题对策 + 成效指标", "combo", "分区：问题—对策 + 量化（成效、指标）",
        "一页有两类信息：2–4 个问题及对应措施，加 2–4 个量化的成效或指标（数字必须出自原文）",
        {"pairs_label": S(8, opt=True), "pairs": L(2, 4, {"problem": S(14), "measure": S(44)}),
         "kpis_label": S(8, opt=True), "kpis": L(2, 4, {"value": S(7), "unit": S(5, opt=True), "label": S(16)})})
def r_combo_pairs_kpis(d, s, sp):
    C = d.C
    kw = 3.5
    lx_w = CW - kw - 0.3
    y = Y0 + zone_label(d, s, X0, Y0, sp.get("pairs_label"), C["accent_b"])
    blk_pairs(d, s, X0, y, lx_w, Y1 - y, sp["pairs"])
    kx = X0 + lx_w + 0.3
    y2 = Y0 + zone_label(d, s, kx, Y0, sp.get("kpis_label"))
    ks = sp["kpis"]
    n = len(ks)
    g = 0.15
    kh = (Y1 - y2 - g * (n - 1)) / n
    for i, kp in enumerate(ks):
        blk_kpis(d, s, kx, y2 + i * (kh + g), kw, kh, [kp])


@layout("L62", "竖向步骤 + 要求面板", "combo", "分区：步骤 + 要点（流程与关键要求/保障）",
        "一页有两类信息：左边 3–5 个步骤（每步有说明），右边 2–4 条贯穿全程的关键要求、约束或保障（与步骤不重复）",
        {"steps": L(3, 5, {"head": S(10), "body": S(38, opt=True)}),
         "panel_title": S(10), "panel": L(2, 4, {"head": S(10), "body": S(30, opt=True)})})
def r_combo_steps_panel(d, s, sp):
    C = d.C
    pw = 4.1
    blk_vsteps(d, s, X0, Y0, CW - pw - 0.3, CH - 0.05, sp["steps"])
    blk_list_panel(d, s, X1 - pw, Y0, pw, CH - 0.05, sp["panel_title"], sp["panel"])
