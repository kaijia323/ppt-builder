"""原文图片（0.6）：从 Word 中原样取出图片，挂到对应的“图片占位”段落上，供视觉阶段放进 PPT。

- 不压缩、不转格式：PNG/JPEG 原样保存；EMF/WMF 矢量图原样保存（PowerPoint 原生支持，放大不失真）
- 位置：与解析阶段相同的遍历规则（正文段落、单列版式表格内的段落；数据表格内的图不计），按出现顺序与
  文本为“〔原文此处为图片…〕”的段落一一对应
- 图题：紧随其后（或紧挨其前）的“图 X-Y ……”段落
- 旧工作目录（解析时还没有取图）：视觉阶段自动补取，段落ID 不变
"""
import io
import os
import re

from .core import read_jsonl, write_jsonl

FIGURE_NOTE = "〔原文此处为图片，PPT 中需插入原图〕"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_V = "{urn:schemas-microsoft-com:vml}"
CAPTION_RE = re.compile(r"^\s*图\s*[\dA-Za-z一二三四五六七八九十]+([\-－—.．]\d+)*")


def _ptext(p_el):
    return "".join(t.text or "" for t in p_el.iter(_W + "t")).strip()


def _has_drawing(p_el):
    return p_el.find(".//" + _W + "drawing") is not None or p_el.find(".//" + _W + "pict") is not None


def _is_layout_table(tbl_el):
    for tr in tbl_el.findall(_W + "tr"):
        if len(tr.findall(_W + "tc")) != 1:
            return False
    return True


def _figure_paragraphs(body):
    """与 ingest.parse_docx 相同口径的“图片段落”：正文中、单列版式表格中没有文字只有图片的段落。"""
    out = []

    def walk(el):
        for ch in el.iterchildren():
            tag = ch.tag.split("}")[-1]
            if tag == "p":
                if _has_drawing(ch) and not _ptext(ch):
                    out.append(ch)
            elif tag == "tbl":
                if _is_layout_table(ch):
                    for tr in ch.findall(_W + "tr"):
                        for tc in tr.findall(_W + "tc"):
                            walk(tc)
            elif tag in ("sdt", "sdtContent"):
                walk(ch)
    walk(body)
    return out


def image_format(blob, ext):
    if len(blob) > 44 and blob[40:44] == b" EMF":
        return "emf"
    if blob[:4] in (b"\xd7\xcd\xc6\x9a", b"\x01\x00\x09\x00", b"\x02\x00\x09\x00"):
        return "wmf"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg"
    if blob[:3] == b"GIF":
        return "gif"
    if blob[:2] == b"BM":
        return "bmp"
    return (ext or "bin").lower().lstrip(".")


def pixel_size(blob, fmt):
    if fmt in ("emf", "wmf"):
        return None                    # 矢量图：没有像素分辨率的问题
    try:
        from PIL import Image
        return list(Image.open(io.BytesIO(blob)).size)
    except Exception:
        return None


def extract_docx_figures(path):
    """按出现顺序返回 [{blob, fmt, px, aspect}]（一个段落有多张图时取面积最大的一张）。"""
    import docx
    d = docx.Document(path)
    rels = d.part.related_parts
    figs = []
    for p in _figure_paragraphs(d.element.body):
        best = None
        for blip in p.iter(_A + "blip"):
            rid = blip.get(_R + "embed") or blip.get(_R + "link")
            part = rels.get(rid)
            if part is None or not hasattr(part, "blob"):
                continue
            blob = part.blob
            fmt = image_format(blob, os.path.splitext(str(part.partname))[1])
            ext = p.find(".//" + _WP + "extent")
            aspect = None
            if ext is not None and int(ext.get("cy") or 0):
                aspect = int(ext.get("cx")) / float(int(ext.get("cy")))
            px = pixel_size(blob, fmt)
            if aspect is None and px:
                aspect = px[0] / float(px[1] or 1)
            area = (px[0] * px[1]) if px else len(blob)
            if best is None or area > best["_area"]:
                best = {"blob": blob, "fmt": fmt, "px": px, "aspect": round(aspect or 1.33, 3), "_area": area}
        if best is None:
            for im in p.iter(_V + "imagedata"):
                part = rels.get(im.get(_R + "id"))
                if part is not None and hasattr(part, "blob"):
                    blob = part.blob
                    fmt = image_format(blob, os.path.splitext(str(part.partname))[1])
                    px = pixel_size(blob, fmt)
                    best = {"blob": blob, "fmt": fmt, "px": px, "aspect": round(px[0] / float(px[1]), 3) if px else 1.33, "_area": 0}
                    break
        figs.append(best)
    return figs


def _caption_for(blocks, i):
    b = blocks[i]
    for j in (i + 1, i + 2, i - 1):
        if 0 <= j < len(blocks) and blocks[j]["section"] == b["section"] and blocks[j]["text"] != FIGURE_NOTE:
            t = blocks[j]["text"].strip()
            if CAPTION_RE.match(t) and len(t) <= 60:
                return t
    return ""


def attach(blocks, docs, media_dir, rel_dir, log=None):
    """把图片挂到占位段落上（就地修改 blocks），返回挂上的数量。"""
    os.makedirs(media_dir, exist_ok=True)
    n = 0
    for doc in docs:
        path = doc.get("path") or ""
        if not path.lower().endswith(".docx") or not os.path.exists(path):
            continue
        holders = [i for i, b in enumerate(blocks) if b.get("doc") == doc["id"] and b.get("text") == FIGURE_NOTE]
        if not holders:
            continue
        try:
            figs = extract_docx_figures(path)
        except Exception as e:      # noqa
            if log:
                log.warn("media_extract_failed", "%s 图片提取失败：%r" % (doc.get("file"), e))
            continue
        if len(figs) != len(holders) and log:
            log.warn("media_count_mismatch", "%s：原文图片段落 %d 个，占位段落 %d 个，按顺序对应前 %d 个"
                     % (doc.get("file"), len(figs), len(holders), min(len(figs), len(holders))))
        for i, f in zip(holders, figs):
            if not f:
                continue
            b = blocks[i]
            fn = "%s.%s" % (b["id"], f["fmt"])
            with open(os.path.join(media_dir, fn), "wb") as fh:
                fh.write(f["blob"])
            b["image"] = {"file": rel_dir + "/" + fn, "fmt": f["fmt"], "px": f["px"], "aspect": f["aspect"]}
            cap = _caption_for(blocks, i)
            if cap:
                b["caption"] = cap
            n += 1
    return n


def ensure(wd, log=None):
    """旧工作目录补取图片（视觉阶段调用）：有占位段落但还没有图片信息时，从原文 Word 中补取。"""
    from .core import read_json
    bp = wd.stage("ingest", "blocks.jsonl")
    blocks = read_jsonl(bp)
    if not any(b.get("text") == FIGURE_NOTE and "image" not in b for b in blocks):
        return 0
    docs = read_json(wd.stage("ingest", "docs.json"), []) or []
    docs = docs.get("docs", docs) if isinstance(docs, dict) else docs
    n = attach(blocks, docs, wd.stage("ingest", "media"), "01_ingest/media", log)
    if n:
        write_jsonl(bp, blocks)
        if log:
            log.info("media_attached", "从原文补取图片 %d 张" % n)
    return n
