"""第 1 层：文档解析与切块（纯脚本，不调用模型）。

支持：docx / pdf / pptx / xlsx / xlsm / csv / md / txt / html / htm
旧格式与 WPS：doc / ppt / xls / wps / dps / et / rtf / odt / odp / ods —— 通过 LibreOffice 转换后解析

每个文本单元（block）获得稳定 ID：
  段落      D1-S3.2-P05     （文档1，第3.2节，第5段）
  表格行    D1-S3.2-T1-R03  （第1个表格第3行）
  幻灯片    D2-S4-P02       （pptx 每页作为一节，S 编号=页码）
"""
import csv
import os
import re
import shutil
import subprocess

from .checks import norm
from .core import PipelineError, EXIT_CONFIG, sha256_file, write_json, write_jsonl, write_text

CONVERT = {".doc": "docx", ".wps": "docx", ".rtf": "docx", ".odt": "docx",
           ".ppt": "pptx", ".dps": "pptx", ".odp": "pptx",
           ".xls": "xlsx", ".et": "xlsx", ".ods": "xlsx"}
NATIVE = {".docx", ".pdf", ".pptx", ".xlsx", ".xlsm", ".csv", ".md", ".markdown", ".txt", ".html", ".htm"}

# 无标题样式时的编号式标题识别
_HEAD_PATTERNS = [
    (1, re.compile(r"^第[一二三四五六七八九十百\d]+[章部分篇]\s*\S")),
    (1, re.compile(r"^[一二三四五六七八九十]+[、.．]\s*\S")),
    (2, re.compile(r"^[（(][一二三四五六七八九十]+[)）]\s*\S")),
    (None, re.compile(r"^(\d+(?:\.\d+){0,3})[\s、.．]\s*[^\d\s]")),
]


def is_tabular_line(s):
    """无句末标点、由 3 个以上短字段以空格分隔的行，视为表格行。"""
    if re.search(r"[。；;！!？?，,]", s):
        return False
    toks = s.split()
    return len(toks) >= 3 and all(len(t) <= 12 for t in toks)


# "1.1研究目标"：带点的编号后直接接汉字（不能是"万/元/年"等单位，避免把"3.5万元"当标题）
_DOTTED_CJK = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,3})(?=[\u4e00-\u9fa5])(?![万亿千百元米年月日个倍])")


def guess_heading(text):
    t = text.strip()
    if not t or len(t) > 40 or re.search(r"[。；;！!？?，,]$", t):   # 冒号结尾可以是标题（如"一、论文概况："）
        return None
    for lvl, pat in _HEAD_PATTERNS:
        m = pat.match(t)
        if m:
            if lvl is None:
                lvl = min(4, m.group(1).count(".") + 1)
            return lvl
    m = _DOTTED_CJK.match(t)
    if m:
        return min(4, m.group(1).count(".") + 1)
    return None


def is_layout_table(t):
    """版式表格：只有一列（每行只有一个格子），常见于开题报告、申报书、任务书等表格式模板。
    这类表格的格子里装的是正文，应按段落解析，而不是当作数据表。"""
    try:
        for r in t.rows:
            uniq = {id(c._tc) for c in r.cells}
            if len(uniq) != 1:
                return False
        return len(t.rows) > 0
    except Exception:
        return False


class DocBuilder:
    """按出现顺序收集 block，自动维护章节编号与段落计数。"""

    def __init__(self, doc_id):
        self.doc_id = doc_id
        self.blocks = []
        self.counters = [0, 0, 0, 0, 0, 0]
        self.sec = "0"
        self.path = []
        self.p_count = {}
        self.t_count = {}
        self.cur_table = None

    def heading(self, text, level, page=None):
        level = max(1, min(6, int(level)))
        self.counters[level - 1] += 1
        for i in range(level, 6):
            self.counters[i] = 0
        self.sec = ".".join(str(c) for c in self.counters[:level])   # 跳级标题用 0 占位，如 0.1，保证不重号
        self.path = self.path[:level - 1] + [text.strip()]
        self._add("heading", text, page, level=level)

    def set_section(self, sec, title, page=None):
        """用于幻灯片/工作表：直接指定节号。"""
        self.sec = str(sec)
        self.path = [title.strip()] if title else ["第%s页" % sec]
        self._add("heading", title or ("第%s页" % sec), page, level=1)

    def _add(self, btype, text, page, level=None, table=None, row=None, cells=None):
        text = (text or "").strip()
        if not text:
            return
        if btype == "table_row":
            bid = "%s-S%s-T%d-R%02d" % (self.doc_id, self.sec, table, row)
        else:
            n = self.p_count.get(self.sec, 0) + 1
            self.p_count[self.sec] = n
            bid = "%s-S%s-P%02d" % (self.doc_id, self.sec, n)
        b = {"id": bid, "doc": self.doc_id, "type": btype, "section": self.sec,
             "section_path": list(self.path), "page": page, "text": text}
        if level:
            b["level"] = level
        if cells is not None:
            b["cells"] = cells
        self.blocks.append(b)

    def para(self, text, page=None, btype="para"):
        self._add(btype, text, page)

    def table(self, rows, page=None, caption=None):
        """rows: list[list[str]]，首行视作表头。每行转成"列名: 值; ..."文本。"""
        rows = [[(c or "").strip().replace("\n", " ") for c in r] for r in rows if r and any((c or "").strip() for c in r)]
        if not rows:
            return
        t = self.t_count.get(self.sec, 0) + 1
        self.t_count[self.sec] = t
        header = rows[0]
        if caption:
            self._add("caption", caption, page)
        self._add("table_row", "表头：" + " | ".join(header), page, table=t, row=0, cells=header)
        for i, r in enumerate(rows[1:], 1):
            if len(header) == len(r) and any(header):
                txt = "；".join("%s：%s" % (h or "列%d" % (j + 1), v) for j, (h, v) in enumerate(zip(header, r)) if v)
            else:
                txt = " | ".join(r)
            self._add("table_row", txt, page, table=t, row=i, cells=r)


# ---------------------------------------------------------------- docx 文本（保留上下标）
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SUP = str.maketrans("0123456789+-−=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿ")
_SUB = str.maketrans("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎")
FIGURE_NOTE = "〔原文此处为图片，PPT 中需插入原图〕"


def _script(text, mode):
    """上标/下标文字转为 Unicode 上下标；无法转换的字符用 ^(…) / _(…) 表示，避免 10⁵ 变成 105。"""
    tab = _SUP if mode == "superscript" else _SUB
    t = text.translate(tab)
    if any(ch.isascii() and not ch.isspace() for ch in t):
        return ("^(%s)" if mode == "superscript" else "_(%s)") % text
    return t


def ptext(p_el):
    """段落文字（python-docx 的 p.text 会丢掉上下标格式）。"""
    out = []
    for r in p_el.iter(_W + "r"):
        va = r.find(_W + "rPr/" + _W + "vertAlign")
        mode = va.get(_W + "val") if va is not None else None
        buf = []
        for el in r:
            tag = el.tag.split("}")[-1]
            if tag == "t":
                buf.append(el.text or "")
            elif tag == "tab":
                buf.append("\t")
            elif tag in ("br", "cr"):
                buf.append("\n")
        t = "".join(buf)
        out.append(_script(t, mode) if t and mode in ("superscript", "subscript") else t)
    return "".join(out)


def has_drawing(p_el):
    return p_el.find(".//" + _W + "drawing") is not None or p_el.find(".//" + _W + "pict") is not None


def ctext(cell):
    """单元格文字。有底色但没有文字的格子记为 ■：甘特图、勾选表靠底色表达信息（G30）。"""
    t = "\n".join(ptext(pp._p) for pp in cell.paragraphs)
    if not t.strip():
        shd = cell._tc.find(_W + "tcPr/" + _W + "shd")
        fill = (shd.get(_W + "fill") or "").upper() if shd is not None else ""
        if fill and fill not in ("AUTO", "FFFFFF", "NONE"):
            return "■"
    return t


# ---------------------------------------------------------------- parsers
def parse_docx(path, b, warnings):
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = docx.Document(path)
    has_style_heading = any((p.style is not None and p.style.name and
                             (p.style.name.lower().startswith("heading") or p.style.name.startswith("标题") or p.style.name == "Title"))
                            for p in d.paragraphs)
    mode = "style" if has_style_heading else "pattern"
    stats = {"layout_tables": 0, "cell_headings": 0}
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = ptext(child).strip()
            if not text:
                if has_drawing(child):
                    b.para(FIGURE_NOTE, btype="caption")
                continue
            sname = (p.style.name if p.style is not None else "") or ""
            lvl = None
            if mode == "style":
                m = re.match(r"(?:heading|标题)\s*(\d)", sname, re.I)
                if m:
                    lvl = int(m.group(1))
                elif sname == "Title":
                    lvl = 1
            else:
                lvl = guess_heading(text)
            if lvl:
                b.heading(text, lvl)
            elif "list" in sname.lower() or "列表" in sname:
                b.para(text, btype="list")
            elif sname.lower().startswith("caption") or sname.startswith("题注"):
                b.para(text, btype="caption")
            else:
                b.para(text)
        elif tag == "tbl":
            t = Table(child, d)
            if is_layout_table(t):
                flow_layout_table(t, d, b, stats)
                continue
            rows = []
            for r in t.rows:
                cells, prev = [], None
                for c in r.cells:
                    if prev is not None and c._tc is prev:   # 合并单元格去重
                        continue
                    prev = c._tc
                    cells.append(ctext(c))
                rows.append(cells)
            b.table(rows)
    # 页眉页脚通常无内容价值，不解析
    if stats["layout_tables"]:
        warnings.append("有 %d 个单列版式表格（表格式模板），已按正文段落解析，识别出 %d 个格内标题" % (stats["layout_tables"], stats["cell_headings"]))
    return {"heading_mode": mode, "layout_tables": stats["layout_tables"], "cell_headings": stats["cell_headings"]}


def flow_layout_table(t, d, b, stats):
    """把单列版式表格的格子内容当作正文：识别格内标题（挂在当前章节下面）、图题、嵌套表格。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    stats["layout_tables"] += 1
    base = len(b.path)   # 当前章节深度，格内标题挂在它下面
    offset = [None]      # 以格内第一个标题的层级为一级（如格内直接从"1.1"开始，则 1.1 视为一级）
    seen = set()
    for r in t.rows:
        for c in r.cells:
            if id(c._tc) in seen:
                continue
            seen.add(id(c._tc))
            for el in c._tc.iterchildren():
                tag = el.tag.split("}")[-1]
                if tag == "p":
                    p = Paragraph(el, c)
                    text = ptext(el).strip()
                    if not text:
                        if has_drawing(el):
                            b.para(FIGURE_NOTE, btype="caption")
                        continue
                    sname = (p.style.name if p.style is not None else "") or ""
                    lvl = guess_heading(text)
                    if lvl is None and ("list" in sname.lower() or "列表" in sname) and len(text) <= 40 \
                            and el.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr") is not None \
                            and not re.search(r"[，,；;：:]$", text):
                        lvl = 1          # 自动编号的短列表段落（编号不在文字里），视为格内一级标题
                    if lvl:
                        if offset[0] is None:
                            offset[0] = lvl - 1
                        rel = max(1, lvl - offset[0])
                        # 格内标题挂在外层章节下面，且最多比当前层级深一级（避免跳级产生 S2.0.1 这样的章节号）
                        b.heading(text.rstrip("。"), min(6, base + rel, len(b.path) + 1))
                        stats["cell_headings"] += 1
                    elif re.match(r"^[图表]\s*\d", text) and len(text) <= 80:
                        b.para(text, btype="caption")
                    else:
                        b.para(text)
                elif tag == "tbl":
                    nt = Table(el, c)
                    if is_layout_table(nt):
                        flow_layout_table(nt, d, b, stats)
                    else:
                        rows = []
                        for rr in nt.rows:
                            cells, prev = [], None
                            for cc in rr.cells:
                                if prev is not None and cc._tc is prev:
                                    continue
                                prev = cc._tc
                                cells.append(ctext(cc))
                            rows.append(cells)
                        b.table(rows)


def parse_pdf(path, b, warnings, cfg):
    import pdfplumber
    min_chars = cfg["ingest"].get("min_chars_per_page", 20)
    low_pages = []
    n_pages = 0
    tabular_lines = [0]
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, 1):
            try:
                tables = page.find_tables()
            except Exception:
                tables = []
            bboxes = [t.bbox for t in tables]

            def outside(obj, bboxes=bboxes):
                if obj.get("object_type") != "char":
                    return True
                x, y = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
                return not any(bx0 <= x <= bx1 and top <= y <= bot for bx0, top, bx1, bot in bboxes)
            try:
                text = page.filter(outside).extract_text() or ""
            except Exception:
                text = page.extract_text() or ""
            if len(re.sub(r"\s", "", text)) < min_chars and not tables:
                low_pages.append(pno)
            # 合并行 -> 段落：行尾是句末标点或下一行像标题时断开
            buf = []
            for line in text.split("\n"):
                s = line.strip()
                if not s:
                    continue
                if re.fullmatch(r"[-—\s]*\d+[-—\s]*|第\s*\d+\s*页.*|\d+\s*/\s*\d+", s):
                    continue  # 页码
                lvl = guess_heading(s)
                if lvl:
                    if buf:
                        b.para("".join(buf), page=pno)
                        buf = []
                    b.heading(s, lvl, page=pno)
                    continue
                if is_tabular_line(s):          # 无边框表格的行：单独成段，不与上下文拼接
                    if buf:
                        b.para("".join(buf), page=pno)
                        buf = []
                    b.para(s, page=pno, btype="table_row_text")
                    tabular_lines[0] += 1
                    continue
                buf.append(s)
                if re.search(r"[。！？；!?;:：]$", s):
                    b.para("".join(buf), page=pno)
                    buf = []
            if buf:
                b.para("".join(buf), page=pno)
            for t in tables:
                try:
                    b.table(t.extract(), page=pno)
                except Exception as e:
                    warnings.append("第%d页表格解析失败: %r" % (pno, e))
    ratio = len(low_pages) / float(n_pages or 1)
    if tabular_lines[0]:
        warnings.append("有 %d 行疑似无边框表格，已按行单独保留（未能识别列名，请检查 %s.md）" % (tabular_lines[0], b.doc_id))
    info = {"tabular_lines": tabular_lines[0], "pages": n_pages, "low_text_pages": low_pages, "low_text_ratio": round(ratio, 3), "heading_mode": "pattern"}
    if low_pages:
        warnings.append("疑似扫描/图片页：%s（共%d页）" % (low_pages[:30], len(low_pages)))
    return info


def parse_pptx(path, b, warnings):
    from pptx import Presentation
    prs = Presentation(path)
    n = 0
    for sno, slide in enumerate(prs.slides, 1):
        n += 1
        title = None
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip()
        b.set_section(sno, title or "第%d页" % sno, page=sno)

        def walk(shapes):
            for sh in shapes:
                if sh.shape_type == 6 and hasattr(sh, "shapes"):   # group
                    walk(sh.shapes)
                    continue
                if slide.shapes.title is not None and sh.shape_id == slide.shapes.title.shape_id:
                    continue
                if getattr(sh, "has_table", False) and sh.has_table:
                    rows = [[c.text for c in r.cells] for r in sh.table.rows]
                    b.table(rows, page=sno)
                elif getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                    for para in sh.text_frame.paragraphs:
                        t = "".join(r.text for r in para.runs).strip()
                        if t:
                            b.para(t, page=sno, btype="list" if para.level > 0 else "para")
        walk(slide.shapes)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            nt = slide.notes_slide.notes_text_frame.text.strip()
            if nt:
                b.para(nt, page=sno, btype="note")
    return {"pages": n, "heading_mode": "slide"}


def parse_xlsx(path, b, warnings, cfg):
    import openpyxl
    maxr = cfg["ingest"].get("max_rows_per_sheet", 300)
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    for si, ws in enumerate(wb.worksheets, 1):
        b.set_section(si, "工作表：" + ws.title)
        rows = []
        for r in ws.iter_rows(values_only=True):
            vals = ["" if v is None else _cell(v) for v in r]
            if any(vals):
                rows.append(vals)
            if len(rows) > maxr:
                warnings.append("工作表 %s 超过 %d 行，已截断" % (ws.title, maxr))
                break
        if rows:
            width = max(len(r) for r in rows)
            # 去掉全空列
            keep = [j for j in range(width) if any(j < len(r) and r[j] for r in rows)]
            rows = [[r[j] if j < len(r) else "" for j in keep] for r in rows]
            b.table(rows)
    return {"sheets": len(wb.worksheets), "heading_mode": "sheet"}


def _cell(v):
    if isinstance(v, float):
        return ("%.6f" % v).rstrip("0").rstrip(".")
    return str(v).strip()


def parse_csv(path, b, warnings, cfg):
    maxr = cfg["ingest"].get("max_rows_per_sheet", 300)
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        raise PipelineError("无法识别 CSV 编码: %s" % path)
    rows = list(csv.reader(text.splitlines()))
    if len(rows) > maxr + 1:
        warnings.append("CSV 超过 %d 行，已截断" % maxr)
        rows = rows[:maxr + 1]
    b.set_section(1, "表格：" + os.path.basename(path))
    b.table(rows)
    return {"heading_mode": "sheet"}


def _read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def parse_md(path, b, warnings):
    text = _read_text(path)
    in_code = False
    buf, tbl = [], []

    def flush():
        if buf:
            b.para(" ".join(buf))
            buf.clear()

    def flush_tbl():
        if tbl:
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in tbl
                    if not re.fullmatch(r"\|?\s*:?-{2,}.*", r.strip())]
            b.table(rows)
            tbl.clear()
    for line in text.splitlines():
        s = line.rstrip()
        if s.strip().startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code:
            buf.append(s.strip())
            continue
        if s.strip().startswith("|"):
            flush()
            tbl.append(s)
            continue
        flush_tbl()
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            flush()
            b.heading(m.group(2), len(m.group(1)))
        elif re.match(r"^\s*([-*+]|\d+[.)])\s+", s):
            flush()
            b.para(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", s), btype="list")
        elif not s.strip():
            flush()
        else:
            buf.append(s.strip())
    flush()
    flush_tbl()
    return {"heading_mode": "markdown"}


def parse_txt(path, b, warnings):
    text = _read_text(path)
    for para in re.split(r"\n\s*\n|\n(?=\S)", text):
        s = para.strip().replace("\n", "")
        if not s:
            continue
        lvl = guess_heading(s)
        if lvl:
            b.heading(s, lvl)
        else:
            b.para(s)
    return {"heading_mode": "pattern"}


def parse_html(path, b, warnings):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_read_text(path), "html.parser")
    for t in soup(["script", "style", "nav", "footer"]):
        t.decompose()
    body = soup.body or soup
    for el in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "pre", "blockquote"]):
        if el.find_parent("table") and el.name != "table":
            continue
        if el.name == "li" and el.find_parent("li"):
            pass
        if el.name[0] == "h" and len(el.name) == 2:
            b.heading(el.get_text(" ", strip=True), int(el.name[1]))
        elif el.name == "table":
            rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in el.find_all("tr")]
            b.table(rows)
        else:
            b.para(el.get_text(" ", strip=True), btype="list" if el.name == "li" else "para")
    return {"heading_mode": "html"}


def find_soffice():
    """LibreOffice：先找 PATH，再找 Windows / macOS 的默认安装位置（Windows 安装后通常不在 PATH 中）。"""
    for c in (shutil.which("soffice"), shutil.which("libreoffice"),
              r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if c and os.path.exists(c):
            return c
    return None


def convert_legacy(path, outdir, warnings):
    ext = os.path.splitext(path)[1].lower()
    target = CONVERT[ext]
    soffice = find_soffice()
    if not soffice:
        raise PipelineError("%s 需要 LibreOffice 转换为 %s，但未找到 soffice。请安装 LibreOffice 或先手动另存为 %s"
                            % (os.path.basename(path), target, target), EXIT_CONFIG)
    os.makedirs(outdir, exist_ok=True)
    r = subprocess.run([soffice, "--headless", "--convert-to", target, "--outdir", outdir, path],
                       capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    out = os.path.join(outdir, os.path.splitext(os.path.basename(path))[0] + "." + target)
    if r.returncode != 0 or not os.path.exists(out):
        raise PipelineError("LibreOffice 转换失败: %s\n%s" % (path, (r.stderr or r.stdout)[-1000:]))
    warnings.append("已用 LibreOffice 将 %s 转换为 %s" % (ext, target))
    return out


# ---------------------------------------------------------------- stage
def run(wd, cfg, log, files=None, allow_low_text=False):
    brief = wd.brief()
    files = files or brief.get("inputs") or []
    if not files:
        raise PipelineError("没有输入文件", EXIT_CONFIG)
    docs, all_blocks = [], []
    for i, f in enumerate(files, 1):
        doc_id = "D%d" % i
        src = os.path.abspath(f)
        if not os.path.exists(src):
            raise PipelineError("输入文件不存在: %s" % src, EXIT_CONFIG)
        ext = os.path.splitext(src)[1].lower()
        warnings = []
        parse_path = src
        if ext in CONVERT:
            parse_path = convert_legacy(src, wd.stage("ingest", "converted"), warnings)
            pext = "." + CONVERT[ext]
        elif ext in NATIVE:
            pext = ext
        else:
            raise PipelineError("不支持的文件类型: %s（支持 %s 及 %s）" % (ext, " ".join(sorted(NATIVE)), " ".join(sorted(CONVERT))), EXIT_CONFIG)
        b = DocBuilder(doc_id)
        if pext == ".docx":
            info = parse_docx(parse_path, b, warnings)
        elif pext == ".pdf":
            info = parse_pdf(parse_path, b, warnings, cfg)
        elif pext == ".pptx":
            info = parse_pptx(parse_path, b, warnings)
        elif pext in (".xlsx", ".xlsm"):
            info = parse_xlsx(parse_path, b, warnings, cfg)
        elif pext == ".csv":
            info = parse_csv(parse_path, b, warnings, cfg)
        elif pext in (".md", ".markdown"):
            info = parse_md(parse_path, b, warnings)
        elif pext == ".txt":
            info = parse_txt(parse_path, b, warnings)
        else:
            info = parse_html(parse_path, b, warnings)

        blocks = b.blocks
        chars = sum(len(x["text"]) for x in blocks)
        garbled = sum(len(re.findall(r"[�-]", x["text"])) for x in blocks)
        types = {}
        for x in blocks:
            types[x["type"]] = types.get(x["type"], 0) + 1
        dup_ids = len(blocks) - len({x["id"] for x in blocks})
        meta = {"id": doc_id, "file": os.path.basename(src), "path": src, "ext": ext, "sha256": sha256_file(src),
                "size_bytes": os.path.getsize(src), "blocks": len(blocks), "chars": chars, "block_types": types,
                "headings": types.get("heading", 0), "garbled_chars": garbled, "warnings": warnings}
        meta.update(info)
        if parse_path != src:
            meta["parse_path"] = parse_path
        # 关卡：文本量、乱码、扫描件、重复ID
        ok_text = chars >= 10
        log.gate("ingest_text_present", doc_id, ok_text, None if ok_text else "几乎没有可读文本", chars=chars)
        ok_garble = garbled <= max(5, chars * 0.01)
        log.gate("ingest_garbled", doc_id, ok_garble, None if ok_garble else "乱码字符过多", garbled=garbled)
        log.gate("ingest_unique_ids", doc_id, dup_ids == 0, None if dup_ids == 0 else "存在重复ID %d" % dup_ids)
        if meta.get("low_text_ratio") is not None:
            ok_scan = meta["low_text_ratio"] <= cfg["ingest"].get("scanned_ratio_fail", 0.5)
            log.gate("ingest_not_scanned", doc_id, ok_scan, None if ok_scan else "疑似扫描件", ratio=meta["low_text_ratio"])
            if not ok_scan and not allow_low_text:
                raise PipelineError("%s 疑似扫描件（%.0f%% 页面无文字），请先 OCR 后再输入，或加 --allow-low-text 继续"
                                    % (meta["file"], meta["low_text_ratio"] * 100), EXIT_CONFIG)
        if not ok_text:
            raise PipelineError("%s 未抽取到有效文本" % meta["file"], EXIT_CONFIG)
        if meta["headings"] == 0:
            warnings.append("未识别到任何标题，章节结构可能不准确")
        # 输入预检（0.7.0-dev8）：会影响成品的输入问题，在大纲确认时提醒用户（只提示，不拦截）
        pre = []
        if meta["headings"] == 0 and chars >= 5000:
            pre.append("《%s》没有识别到章节标题，大纲按段落推断，章节划分可能不准" % meta["file"])
        lr = meta.get("low_text_ratio")
        if lr is not None and lr >= cfg["ingest"].get("precheck_low_text", 0.15):
            pre.append("《%s》约 %.0f%% 的页面没有文字（扫描页或整页图片），这些页的内容不会进入 PPT" % (meta["file"], lr * 100))
        if not ok_garble:
            pre.append("《%s》有较多乱码字符（约 %d 个），相关内容可能提炼不准" % (meta["file"], garbled))
        meta["precheck"] = pre
        for w in warnings:
            log.warn("ingest_warning", "%s: %s" % (meta["file"], w))
        log.info("doc_parsed", "%s -> %s：%d 个单元，%d 字，%d 个标题" % (meta["file"], doc_id, len(blocks), chars, meta["headings"]),
                 doc=meta)
        docs.append(meta)
        all_blocks += blocks
        write_text(wd.stage("ingest", "%s.md" % doc_id), render_doc_md(meta, blocks))

    tot = sum(m["chars"] for m in docs)
    if docs and tot < cfg["ingest"].get("precheck_min_chars", 2000):
        docs[0].setdefault("precheck", []).append("原文合计只有约 %d 字，内容偏少，PPT 能展开的页数有限" % tot)
    # 原文图片（0.6）：原样取出，挂到图片占位段落上
    from . import media
    nfig = media.attach(all_blocks, [dict(m, path=m.get("parse_path") or m["path"]) for m in docs],
                        wd.stage("ingest", "media"), "01_ingest/media", log)
    if nfig:
        log.info("media_attached", "从原文取出图片 %d 张" % nfig)
    chunks = make_chunks(all_blocks, cfg)
    write_json(wd.stage("ingest", "docs.json"), docs)
    write_jsonl(wd.stage("ingest", "blocks.jsonl"), all_blocks)
    write_json(wd.stage("ingest", "chunks.json"), chunks)
    sizes = [c["chars"] for c in chunks]
    pre = [x for m in docs for x in m.get("precheck") or []]
    for x in pre:
        log.warn("input_precheck", x)
    summary = {"docs": len(docs), "blocks": len(all_blocks), "chunks": len(chunks), "precheck": pre,
               "chunk_chars_min": min(sizes) if sizes else 0, "chunk_chars_max": max(sizes) if sizes else 0,
               "chunk_chars_avg": int(sum(sizes) / len(sizes)) if sizes else 0}
    return summary


def make_chunks(blocks, cfg):
    target = int(cfg["ingest"].get("chunk_chars", 2500))
    nctx = int(cfg["ingest"].get("context_blocks", 1))
    chunks = []
    by_doc = {}
    for bl in blocks:
        by_doc.setdefault(bl["doc"], []).append(bl)
    for doc, bls in by_doc.items():
        cur, cur_chars, cur_top = [], 0, None
        idx = {bl["id"]: i for i, bl in enumerate(bls)}

        def emit():
            if not cur:
                return
            first = idx[cur[0]["id"]]
            ctx = [x["id"] for x in bls[max(0, first - nctx):first]]
            # 上文中优先带上当前章节标题链
            chunks.append({"id": "%s-C%03d" % (doc, len([c for c in chunks if c["doc"] == doc]) + 1),
                           "doc": doc, "block_ids": [x["id"] for x in cur], "context_ids": ctx,
                           "section_paths": sorted({" > ".join(x["section_path"]) for x in cur if x["section_path"]}),
                           "chars": sum(len(x["text"]) for x in cur)})
        for bl in bls:
            top = bl["section"].split(".")[0]
            new_top = top != cur_top and bl["type"] == "heading" and bl.get("level", 9) <= 2
            if cur and (cur_chars + len(bl["text"]) > target or (new_top and cur_chars > target * 0.4)):
                emit()
                cur, cur_chars = [], 0
            cur.append(bl)
            cur_chars += len(bl["text"])
            cur_top = top
        emit()
    return chunks


def render_doc_md(meta, blocks):
    lines = ["<!-- %s 解析结果（供人工检查），每行前缀为单元 ID -->" % meta["file"], ""]
    for bl in blocks:
        if bl["type"] == "heading":
            lines.append("\n%s %s  `[%s]`" % ("#" * min(6, bl.get("level", 1)), bl["text"], bl["id"]))
        else:
            tag = {"table_row": "表", "table_row_text": "表(无边框)", "list": "•", "note": "备注", "caption": "题注"}.get(bl["type"], "")
            lines.append("`[%s]` %s%s" % (bl["id"], (tag + " ") if tag else "", bl["text"]))
    return "\n".join(lines) + "\n"


def load_blocks(wd):
    from .core import read_jsonl
    return {b["id"]: b for b in read_jsonl(wd.stage("ingest", "blocks.jsonl"))}


def block_text_norm_index(blocks):
    return {k: norm(v["text"]) for k, v in blocks.items()}
