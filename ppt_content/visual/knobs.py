"""版式外观开关（0.7）：同一版式按内容自动换外观，不让模型选。

开关写在 spec["_k"]，由程序在填槽之后、试排之前决定；没有依据时不打开，外观与 0.6 相同。
  v  样式变体：同一版式在整套中第 2、3 次出现时换一种装饰（色条位置、标题样式），结构与配色不变
  w  不等宽：各栏文字量差别大时按字数分宽度（目的只是放得下、不留空，不表示重要性），宽窄比 ≤ 1.7
  e  突出一项：只有标题或底部结论点名了其中恰好一项时才突出
  o  方向：流程步骤说明较长、横排每栏太窄时改竖排

试排放不下时去掉 w / o 再试一次（见 design.fill_page）。
"""
import math
import re

from .prim import em_width

# 0.7.0-dev16：常用版式补变体（L09/L12/L13 到 3 种；L03 问题→目标、L22 大数字卡、L57 图标网格、L15 并行管线、L60 流程+卡片各 2 种），
# 都只换装饰、不缩小文字区域；第一次出现仍是默认样子
VARIANTS = {"L05": 3, "L06": 3, "L09": 3, "L13": 3, "L39": 2, "L12": 3, "L03": 2, "L22": 2, "L57": 2, "L15": 2, "L60": 2}


def _len(o):
    if isinstance(o, str):
        return em_width(o.replace("【", "").replace("】", ""))
    if isinstance(o, dict):
        return sum(_len(v) for k, v in o.items() if not k.startswith("_") and k != "tone")
    if isinstance(o, list):
        return sum(_len(v) for v in o)
    return 0


def _norm(t):
    return re.sub(r"[\s，。、；：:（）()“”\"]", "", t or "")


def weights(groups, ratio=1.6, lo=0.78, hi=1.32):
    """各组文字量差别明显（最多/最少 ≥ ratio）时返回宽度权重，否则 None。"""
    L = [max(1.0, _len(g)) for g in groups]
    if len(L) < 2 or max(L) / min(L) < ratio:
        return None
    m = sum(L) / len(L)
    w = [min(hi, max(lo, math.sqrt(x / m))) for x in L]
    if max(w) / min(w) > 1.7:
        w = [min(x, min(w) * 1.7) for x in w]
    return [round(x, 3) for x in w]


def emphasis(titles, signal):
    """标题/结论点名了恰好一项 → 该项下标。"""
    sig = _norm(signal)
    hit = [i for i, t in enumerate(titles) if len(_norm(t)) >= 2 and _norm(t) in sig]
    return hit[0] if len(hit) == 1 and len(titles) >= 3 else None


def decide(lid, spec, signal="", occ=0):
    k = {}
    if lid in VARIANTS and occ:
        k["v"] = occ % VARIANTS[lid]
    if lid == "L05":
        cols = spec.get("columns") or []
        w = weights(cols)
        if w:
            k["w"] = w
        e = emphasis([c.get("title", "") for c in cols], signal)
        if e is not None:
            k["e"] = e
    elif lid == "L06":
        ps = spec.get("panels") or []
        w = weights([p.get("items") for p in ps])
        if w:
            k["w"] = w
        e = emphasis([p.get("title", "") for p in ps], signal)
        if e is not None:
            k["e"] = e
    elif lid == "L09":
        ps = spec.get("panels") or []
        w = weights([p.get("items") for p in ps], ratio=1.7, lo=0.85, hi=1.18)
        if w:
            k["w"] = w
    elif lid == "L13":
        st = spec.get("steps") or []
        bodies = [em_width(x.get("body", "")) for x in st]
        mean = sum(bodies) / float(len(bodies) or 1)
        # 横排每栏宽 ≈ 11.8/n 英寸；说明平均 ≥ 30 字且 4 步以上时栏太窄、字被压小，改竖排；
        # 同一版式第二次出现时（变体 1）也用竖排，换个样子
        if len(st) <= 5 and ((len(st) >= 4 and mean >= 30) or k.get("v") == 1):
            k["o"] = "v"
    return k


def split(widths_or_none, n, total, gap):
    """按权重分配 n 栏宽度，返回各栏宽度列表。"""
    w = widths_or_none if (widths_or_none and len(widths_or_none) == n) else [1.0] * n
    avail = total - gap * (n - 1)
    s = float(sum(w))
    return [avail * x / s for x in w]
