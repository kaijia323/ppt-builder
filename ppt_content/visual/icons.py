"""SVG 图标 → PowerPoint 原生矢量形状（custGeom），可在 PowerPoint 中改色、缩放。
图标库：Material Design Icons（Pictogrammers Free License，见 icons/LICENSE），只随技能附带用到的子集。"""
import math
import os
import re

from lxml import etree
from pptx.oxml.ns import qn
from pptx.util import Inches

ICON_DIRS = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")]

_TOK = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def _arc_to_cubic(x1, y1, rx, ry, phi, fa, fs, x2, y2):
    """SVG 端点圆弧 → 若干三次贝塞尔（标准换算）。"""
    if rx == 0 or ry == 0:
        return [(x1, y1, x2, y2, x2, y2)]
    phi = math.radians(phi)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0, num / den)) if den else 0
    if fa == fs:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dt > 0:
        dt -= 2 * math.pi
    elif fs and dt < 0:
        dt += 2 * math.pi
    n = max(1, int(math.ceil(abs(dt) / (math.pi / 2))))
    d = dt / n
    out = []
    for i in range(n):
        a1, a2 = t1 + i * d, t1 + (i + 1) * d
        k = 4 / 3 * math.tan((a2 - a1) / 4)
        p = []
        for (x, y) in [(math.cos(a1) - k * math.sin(a1), math.sin(a1) + k * math.cos(a1)),
                       (math.cos(a2) + k * math.sin(a2), math.sin(a2) - k * math.cos(a2)),
                       (math.cos(a2), math.sin(a2))]:
            x, y = x * rx, y * ry
            p += [cp * x - sp * y + cx, sp * x + cp * y + cy]
        out.append(tuple(p))
    return out


def parse_path(d):
    """返回子路径列表，每个子路径为 [('M',x,y) | ('L',x,y) | ('C',x1,y1,x2,y2,x,y) | ('Z',)]（绝对坐标）。"""
    toks = _TOK.findall(d)
    i, cmd = 0, None
    x = y = sx = sy = 0.0
    lc = None   # 上一个控制点（S/T 用）
    paths, cur = [], []

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v
    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
        rel = cmd.islower()
        C = cmd.upper()
        if C == "Z":
            cur.append(("Z",))
            x, y = sx, sy
            lc = None
            continue
        if C == "M":
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            if cur:
                paths.append(cur)
            cur = [("M", nx, ny)]
            x, y, sx, sy = nx, ny, nx, ny
            cmd = "l" if rel else "L"
            lc = None
        elif C == "L":
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            cur.append(("L", nx, ny))
            x, y, lc = nx, ny, None
        elif C == "H":
            nx = num()
            nx = x + nx if rel else nx
            cur.append(("L", nx, y))
            x, lc = nx, None
        elif C == "V":
            ny = num()
            ny = y + ny if rel else ny
            cur.append(("L", x, ny))
            y, lc = ny, None
        elif C in "CS":
            if C == "C":
                x1, y1 = num(), num()
                if rel:
                    x1, y1 = x + x1, y + y1
            else:
                x1, y1 = (2 * x - lc[0], 2 * y - lc[1]) if lc and lc[2] == "C" else (x, y)
            x2, y2, nx, ny = num(), num(), num(), num()
            if rel:
                x2, y2, nx, ny = x + x2, y + y2, x + nx, y + ny
            cur.append(("C", x1, y1, x2, y2, nx, ny))
            lc = (x2, y2, "C")
            x, y = nx, ny
        elif C in "QT":
            if C == "Q":
                qx, qy = num(), num()
                if rel:
                    qx, qy = x + qx, y + qy
            else:
                qx, qy = (2 * x - lc[0], 2 * y - lc[1]) if lc and lc[2] == "Q" else (x, y)
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            cur.append(("C", x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y), nx + 2 / 3 * (qx - nx), ny + 2 / 3 * (qy - ny), nx, ny))
            lc = (qx, qy, "Q")
            x, y = nx, ny
        elif C == "A":
            rx, ry, phi, fa, fs = num(), num(), num(), num(), num()
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            for c in _arc_to_cubic(x, y, rx, ry, phi, int(fa), int(fs), nx, ny):
                cur.append(("C",) + c)
            x, y, lc = nx, ny, None
    if cur:
        paths.append(cur)
    return paths


def load(name):
    for d in ICON_DIRS:
        p = os.path.join(d, name + ".svg")
        if os.path.exists(p):
            svg = open(p, encoding="utf-8").read()
            vb = re.search(r'viewBox="([^"]+)"', svg)
            vb = [float(v) for v in vb.group(1).split()] if vb else [0, 0, 24, 24]
            ds = re.findall(r'<path[^>]*\sd="([^"]+)"', svg)
            return vb, ds
    if name != "checkbox-blank-circle-outline":
        return load("checkbox-blank-circle-outline")
    raise FileNotFoundError(name)


def add_icon(slide, name, x, y, size, color="FFFFFF"):
    """在 (x,y) 放一个 size×size 英寸的矢量图标。"""
    vb, ds = load(name)
    sp = slide.shapes.add_shape(1, Inches(x), Inches(y), Inches(size), Inches(size))
    spPr = sp._element.spPr
    prst = spPr.find(qn("a:prstGeom"))
    spPr.remove(prst)
    S = 1000
    sx, sy = S / vb[2], S / vb[3]
    cust = etree.Element(qn("a:custGeom"))
    for tag in ("a:avLst", "a:gdLst", "a:ahLst", "a:cxnLst"):
        etree.SubElement(cust, qn(tag))
    rect = etree.SubElement(cust, qn("a:rect"))
    for k in ("l", "t", "r", "b"):
        rect.set(k, "0")
    lst = etree.SubElement(cust, qn("a:pathLst"))
    path = etree.SubElement(lst, qn("a:path"))
    path.set("w", str(S))
    path.set("h", str(S))

    def pt(parent, px, py):
        e = etree.SubElement(parent, qn("a:pt"))
        e.set("x", str(int(round((px - vb[0]) * sx))))
        e.set("y", str(int(round((py - vb[1]) * sy))))
    for d in ds:
        for sub in parse_path(d):
            for seg in sub:
                if seg[0] == "M":
                    pt(etree.SubElement(path, qn("a:moveTo")), seg[1], seg[2])
                elif seg[0] == "L":
                    pt(etree.SubElement(path, qn("a:lnTo")), seg[1], seg[2])
                elif seg[0] == "C":
                    e = etree.SubElement(path, qn("a:cubicBezTo"))
                    pt(e, seg[1], seg[2]); pt(e, seg[3], seg[4]); pt(e, seg[5], seg[6])
                elif seg[0] == "Z":
                    etree.SubElement(path, qn("a:close"))
    spPr.insert(1, cust)
    sp.fill.solid()
    from pptx.dml.color import RGBColor
    sp.fill.fore_color.rgb = RGBColor.from_string(color)
    sp.line.fill.background()
    etree.SubElement(spPr, qn("a:effectLst"))
    return sp
