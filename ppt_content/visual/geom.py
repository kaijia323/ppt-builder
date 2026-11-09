"""成品几何质检（0.7.0-dev8）：渲染后直接读 PPTX，只读不改，结果写进《注意事项》的“版面问题”。

只保留在 5 套真实成品上零误报的两条规则：
  small_font  字号低于 10pt 的文字（≥6 字）。各版式的正常最小字号是 10pt，再小投影时看不清
  overlap     两个有文字的元素互相压住（重叠面积超过较小者的 30%）
（“同页重复标签”试过：表格、矩阵、前后对比里的重复是正常的，误报太多，不做。）
文字放不下、越出画布由渲染时的估算负责（render.py 的 hard 问题），这里不重复。
"""
import re

MIN_PT = 10.0
MIN_CHARS = 6
OVERLAP = 0.3


def _walk(shapes):
    for s in shapes:
        if s.shape_type == 6:          # group
            yield from _walk(s.shapes)
        else:
            yield s


def _extent(s):
    """文字实际占的横向范围（EMU）：单行短文字按估算字宽收窄，按对齐方式定位；多行或估不准时用整个框。"""
    from pptx.enum.text import PP_ALIGN
    from .prim import text_width
    E = 914400
    try:
        tf = s.text_frame
        w = 0.0
        for p in tf.paragraphs:
            pt = "".join(r.text for r in p.runs)
            sz = max([r.font.size.pt for r in p.runs if r.font.size] or [0])
            if not sz:
                return s.left, s.width
            w = max(w, text_width(pt, sz, any(r.font.bold for r in p.runs)))
        ww = int((w + (tf.margin_left + tf.margin_right) / E) * E)
        if ww >= s.width:
            return s.left, s.width
        al = tf.paragraphs[0].alignment
        if al == PP_ALIGN.CENTER:
            return s.left + (s.width - ww) // 2, ww
        if al == PP_ALIGN.RIGHT:
            return s.left + s.width - ww, ww
        return s.left, ww
    except Exception:      # noqa
        return s.left, s.width


def check(pptx_path, smap=None):
    """返回 [{"slide", "key", "kind", "text", "detail"}]；smap：第 n 张幻灯片对应的页面 key。"""
    from pptx import Presentation
    prs = Presentation(pptx_path)
    out = []
    for i, sl in enumerate(prs.slides, 1):
        key = smap[i - 1] if smap and i - 1 < len(smap) else None
        tx = []
        for s in _walk(sl.shapes):
            if not getattr(s, "has_text_frame", False) or not s.has_text_frame:
                continue
            t = s.text_frame.text.strip()
            if not t:
                continue
            tx.append((s, t))
            for p in s.text_frame.paragraphs:
                pt = "".join(r.text for r in p.runs)
                sz = [r.font.size.pt for r in p.runs if r.font.size and r.text.strip()]
                if sz and min(sz) < MIN_PT and len(re.sub(r"\s", "", pt)) >= MIN_CHARS:
                    out.append({"slide": i, "key": key, "kind": "small_font", "text": pt[:30],
                                "detail": "%.1fpt" % min(sz)})
                    break
        ext = [_extent(x[0]) for x in tx]
        for a in range(len(tx)):
            A, (al, aw) = tx[a][0], ext[a]
            for b in range(a + 1, len(tx)):
                B, (bl, bw) = tx[b][0], ext[b]
                ox = min(al + aw, bl + bw) - max(al, bl)
                oy = min(A.top + A.height, B.top + B.height) - max(A.top, B.top)
                if ox <= 0 or oy <= 0:
                    continue
                small = min(aw * A.height, bw * B.height) or 1
                if ox * oy / small > OVERLAP:
                    out.append({"slide": i, "key": key, "kind": "overlap", "text": tx[a][1][:15],
                                "detail": "与“%s”重叠" % tx[b][1][:15]})
    return out
