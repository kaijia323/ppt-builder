"""第⑨步：渲染与视觉质检（检查点 3）。纯确定性，不调用模型。

产出（08_visual/）：
  <标题>.pptx          成品，全部为原生可编辑形状，讲稿写在备注里
  preview.pdf / sheet_*.png   预览（本机装有 LibreOffice 时生成）
  visual_report.json   每页的版式、状态与质检问题
  ../checkpoint3.md    交给用户确认的版面清单

视觉质检分三级（版式目录 v1.2 第四节）：
  硬问题：文字放不下、越出画布 —— 必须处理（通常是兜底页或用户强制的版式）
  软问题：相邻页同版式、兜底版式、单章只有一页 —— 提示
  不动：内容与数字（版面阶段不改内容）
"""
import os
import re
import shutil
import subprocess
import tempfile

from ..core import read_json, write_json, write_text, sha256_text
from . import engine
from .layouts import LAYOUTS
from .prim import THEME_NAMES


def safe_name(t):
    t = re.sub(r'[\\/:*?"<>|\r\n]+', "", t or "汇报").strip()
    return (t[:40] or "汇报")


def preview(pptx, outdir, log):
    """逐页转 PDF 再合并（LibreOffice 整套导出时 CJK 字形较多会错序），再生成缩略图。没有 LibreOffice 时跳过。"""
    from ..ingest import find_soffice
    soffice = find_soffice()
    if not soffice:
        return None, []
    try:
        from pptx import Presentation
        n = len(Presentation(pptx).slides)
        tmp = tempfile.mkdtemp()
        parts = []
        for i in range(n):
            p = Presentation(pptx)
            ids = p.slides._sldIdLst
            for j, sid in reversed(list(enumerate(list(ids)))):
                if j != i:
                    p.part.drop_rel(sid.rId)
                    ids.remove(sid)
            f = os.path.join(tmp, "s%03d.pptx" % (i + 1))
            p.save(f)
            parts.append(f)
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp] + parts,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=True)
        pdfs = [f[:-5] + ".pdf" for f in parts]
        out_pdf = os.path.join(outdir, "preview.pdf")
        _merge_pdfs(pdfs, out_pdf)
        sheets = _contact_sheets(pdfs, outdir)
        shutil.rmtree(tmp, ignore_errors=True)
        return out_pdf, sheets
    except Exception as e:  # 预览失败不影响成品
        log.warn("preview_failed", "预览生成失败（不影响 pptx）：%r" % e)
        return None, []


def _merge_pdfs(pdfs, out):
    if shutil.which("pdfunite"):
        subprocess.run(["pdfunite"] + pdfs + [out], check=True)
        return
    try:
        from pypdf import PdfWriter
    except ImportError:
        from pypdfium2 import PdfDocument
        dst = PdfDocument.new()
        for f in pdfs:
            src = PdfDocument(f)
            dst.import_pages(src)
        dst.save(out)
        return
    w = PdfWriter()
    for f in pdfs:
        w.append(f)
    w.write(out)


def _contact_sheets(pdfs, outdir, per=12, cols=3):
    try:
        import pypdfium2 as pdfium
        from PIL import Image
    except ImportError:
        return []
    ims = []
    for f in pdfs:
        doc = pdfium.PdfDocument(f)
        ims.append(doc[0].render(scale=0.55).to_pil().convert("RGB"))
    if not ims:
        return []
    w, h = ims[0].size
    out = []
    for b in range(0, len(ims), per):
        grp = ims[b:b + per]
        rows = (len(grp) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * w + (cols + 1) * 10, rows * h + (rows + 1) * 10), (226, 230, 238))
        for k, im in enumerate(grp):
            sheet.paste(im, (10 + (k % cols) * (w + 10), 10 + (k // cols) * (h + 10)))
        p = os.path.join(outdir, "sheet_%d.png" % (b // per + 1))
        sheet.save(p)
        out.append(p)
    return out


def soft_issues(design):
    iss = []
    prev = None
    for pg in design["pages"]:
        if pg["layout"] == prev:
            iss.append({"page": pg["no"], "kind": "same_layout", "msg": "与上一页版式相同（%s）" % pg["layout"]})
        prev = pg["layout"]
        if pg.get("status") == "fallback":
            iss.append({"page": pg["no"], "kind": "fallback", "msg": "首选与备选版式均未通过，使用兜底版式 L39"})
    if design.get("chapters"):
        for i, c in enumerate(design["chapters"], 1):
            n = sum(1 for pg in design["pages"] if pg["chapter"] == i)
            if n == 0:
                iss.append({"page": None, "kind": "empty_chapter", "msg": "第%d章“%s”没有内容页" % (i, c)})
    return iss


def run(wd, cfg, log):
    design = read_json(wd.stage("design", "design.json"))
    outdir = wd.stage("render")
    os.makedirs(outdir, exist_ok=True)
    name = safe_name(design["cover"]["title"] if design.get("cover") else "汇报") + ".pptx"
    path = os.path.join(outdir, name)
    d, smap = engine.build(design, None)
    try:
        d.save(path)
    except PermissionError:
        # 成品正被 PowerPoint/WPS 打开（Windows 不允许覆盖）：另存一份，不让整个阶段失败
        import time
        path = os.path.join(outdir, safe_name(design["cover"]["title"] if design.get("cover") else "汇报") + "_%s.pptx" % time.strftime("%H%M%S"))
        d.save(path)
        log.warn("pptx_locked", "原成品文件正被打开（PowerPoint/WPS），已另存为 %s；关闭旧文件后可删除它" % os.path.basename(path))
    HARD = ("text_overflow", "out_of_slide")
    hard = [dict(q) for q in d.qa if q["kind"] in HARD]
    soft = soft_issues(design)
    for q in d.qa:
        if q["kind"] not in HARD:
            no = next((pg["no"] for pg in design["pages"] if pg["key"] == q.get("tag")), None)
            soft.append({"page": no, "kind": q["kind"], "msg": q.get("detail") or q.get("text")})
    content = read_json(wd.stage("export", "content.json"), {}) or {}
    figs = {p.get("page_key"): p.get("figures") or [] for p in content.get("pages", []) if p.get("kind") == "content"}
    by_key = {}
    for q in hard:
        by_key.setdefault(q.get("tag"), []).append(q)
    pages = []
    for pg in design["pages"]:
        slides = [i + 1 for i, k in enumerate(smap) if k == pg["key"]]
        pages.append({"no": pg["no"], "key": pg["key"], "slides": slides, "number": pg["number"], "topic": pg["topic"],
                      "layout": pg["layout"], "layout_name": LAYOUTS[pg["layout"]]["name"], "status": pg.get("status"),
                      "relation": pg.get("relation", ""), "hard": by_key.get(pg["key"], [])})
    pdf, sheets = (None, [])
    if cfg.get("visual", {}).get("preview", True):
        pdf, sheets = preview(path, outdir, log)
    figure_rows = [{"no": pg["no"], "layout": pg["layout"], "captions": [f.get("caption") or f.get("block_id") for f in figs.get(pg["key"], [])],
                    "aspect": (figs.get(pg["key"]) or [{}])[0].get("aspect")}
                   for pg in design["pages"] if figs.get(pg["key"])]
    geo = []
    if cfg.get("visual", {}).get("geom_check", True):
        try:                         # 成品几何质检（0.7.0-dev8）：只读，结果进《注意事项》
            from .geom import check as geom_check
            geo = geom_check(path, smap)
        except Exception as e:      # noqa
            log.warn("geom_check_failed", "成品几何质检失败（不影响成品）：%r" % e)
    nums = []
    if cfg.get("visual", {}).get("number_check", True):
        try:                         # 数字一致性核对（0.7.0-dev8）：只读，结果进《注意事项》
            from .numcheck import check as num_check
            from ..core import read_jsonl
            src = [b.get("text") or "" for b in read_jsonl(wd.stage("ingest", "blocks.jsonl"))]
            nums = num_check(design, {p["no"]: p["slides"][0] for p in pages if p["slides"]}, src)
        except Exception as e:      # noqa
            log.warn("number_check_failed", "数字一致性核对失败（不影响成品）：%r" % e)
    misc, speech = [], None
    try:                             # 封面、讲稿时长、敏感信息（0.7.0-dev8）：只读，结果进《注意事项》
        from .misc_check import check as misc_check
        misc, speech = misc_check(design, wd.brief(), {p["no"]: p["slides"][0] for p in pages if p["slides"]},
                                  cfg.get("visual", {}).get("cover_placeholder", "（待补充）"))
    except Exception as e:          # noqa
        log.warn("misc_check_failed", "封面/讲稿/敏感信息检查失败（不影响成品）：%r" % e)
    rep = {"pptx": path, "slides": d.page, "theme": design["theme"], "pages": pages, "hard": hard, "soft": soft,
           "geom": geo, "numbers": nums, "misc": misc, "speech": speech, "figures": figure_rows,
           "preview_pdf": pdf, "sheets": sheets, "design_hash": sha256_text(str(design["pages"]))}
    write_json(os.path.join(outdir, "visual_report.json"), rep)
    md = render_checkpoint(rep, design)
    pend = wd.p("待核实清单.md")
    if os.path.exists(pend):
        n = sum(1 for ln in open(pend, encoding="utf-8").read().split("## 已自动修正")[0].splitlines()
                if ln.startswith("| 第") or ln.startswith("| 整套"))
        if n:
            md = md.replace("\n", "\n\n> 提醒：有 %d 处内容无法按原文自动修正，已汇总在工作目录顶层的《注意事项.md》；可直接在本环节说明怎么改。\n" % n, 1)
    write_text(wd.p("checkpoint3.md"), md)
    write_text(os.path.join(outdir, "讲稿.md"), speech_md(design, smap))
    write_json(wd.p("visual_answers_template.json"), {"decision": "approve", "_示例_修改": {
        "decision": "revise", "theme": "blue", "pages": {"5": {"layout": "L26"}, "7": {"feedback": "用户对第7页版面的意见原话"}}}})
    for q in hard:
        log.warn("visual_hard", "第%s张 %s：%s" % (q["page"], q["kind"], q["text"]), **q)
    return {"pptx": path, "slides": d.page, "hard_issues": len(hard), "soft_issues": len(soft),
            "preview": bool(pdf), "layouts_used": len({p["layout"] for p in pages})}


def speech_md(design, smap):
    """整套讲稿（按幻灯片顺序），便于汇报人通读、打印。"""
    by = {pg["key"]: pg for pg in design["pages"]}
    L = ["# 汇报讲稿", ""]
    seen = set()
    for i, k in enumerate(smap or [], 1):
        if k == "cover":
            L += ["## 第%d张 封面" % i, "", (design.get("cover") or {}).get("notes", ""), ""]
        elif k == "toc":
            L += ["## 第%d张 目录" % i, "", design.get("toc_notes", ""), ""]
        elif str(k).startswith("section"):
            L += ["## 第%d张 过渡页" % i, "", (design.get("section_notes") or {}).get(str(k)[7:], ""), ""]
        elif k == "closing":
            L += ["## 第%d张 结尾" % i, "", (design.get("closing") or {}).get("notes", ""), ""]
        elif k in by and k not in seen:
            seen.add(k)
            pg = by[k]
            L += ["## 第%d张 %s %s（%d 字）" % (i, pg.get("number", ""), pg.get("topic", ""), len(pg.get("notes") or "")), "",
                  pg.get("notes", ""), ""]
    return "\n".join(L)


def user_message(rep, design):
    """成品确认时原样转告用户的话（0.7.0-dev14）：只用幻灯片序号和大白话，不出现版式代号、页号、文件路径。"""
    sl = [p["slides"][0] for p in rep.get("pages") or [] if p.get("slides")]
    a, b, c = (sl[len(sl) // 4], sl[len(sl) // 2], sl[(3 * len(sl)) // 4]) if len(sl) >= 3 else (5, 8, 12)
    ex = ["“第%d张太挤了”" % a, "“第%d张标题改成……，删掉第三条”" % b, "“第%d张换成流程图（或表格、时间轴、卡片）的样子”" % c]
    redraw = [x["no"] for x in rep.get("figures") or [] if x.get("layout") not in ("L50", "L51")]   # 有原图、现在按文字重绘的页
    if redraw:
        f = next((p["slides"][0] for p in rep["pages"] if p["no"] == redraw[0] and p.get("slides")), None)
        if f:
            ex.append("“第%d张直接用原文的图”" % f)
    if design.get("template_info"):
        ex.append("“换回默认配色”“每页都放 Logo”")
    else:
        ex.append("“整体换成中国红”")
    cur = THEME_NAMES.get(rep.get("theme"), "模板配色" if rep.get("theme") == "template" else "")
    return ["## 转告用户（原样发给用户，不要改写）", "",
            "PPT 已生成，共 %d 张幻灯片，请打开看一下。需要留意的问题我整理在《注意事项.md》里。" % rep["slides"], "",
            "满意请回复“确认”；需要修改就直接说，页码按 PowerPoint 里的幻灯片序号，例如：" + "、".join(ex) + "。",
            "只会重做你点名的那几页。色调可选：%s%s。" % ("、".join(THEME_NAMES.values()), "（现在是%s）" % cur if cur else ""), ""]


def render_checkpoint(rep, design):
    L = ["# 成品确认", ""] + user_message(rep, design) + ["## 以下给智能体看（不要转给用户）", "",
         "成品：`%s`（共 %d 张幻灯片，配色 %s）" % (os.path.basename(rep["pptx"]), rep["slides"],
                                            "模板配色" if rep["theme"] == "template" else
                                            "%s（%s）" % (THEME_NAMES.get(rep["theme"], rep["theme"]), rep["theme"]))]
    if rep.get("preview_pdf"):
        L.append("预览：`08_visual/preview.pdf`，缩略总览：%s" % "、".join("`08_visual/%s`" % os.path.basename(x)
                                                                     for x in rep["sheets"]))
    else:
        L.append("（本机未安装 LibreOffice，未生成预览；请直接用 PowerPoint 打开成品查看）")
    L += ["", "## 逐页版式", "", "| 页 | 幻灯片 | 编号与主题 | 版式 | 状态 | 版面问题 |", "|---|---|---|---|---|---|"]
    st_txt = {"ok": "首选", "alt": "备选", "fallback": "**兜底**"}
    for p in rep["pages"]:
        L.append("| %d | %s | %s %s | %s %s | %s | %s |" % (
            p["no"], "、".join(map(str, p["slides"])), p["number"], p["topic"], p["layout"], p["layout_name"],
            st_txt.get(p["status"], p["status"]), "；".join("%s“%s”" % ("文字放不下" if q["kind"] == "text_overflow" else "越界",
                                                                       q["text"][:20]) for q in p["hard"]) or "—"))
    if rep["soft"]:
        L += ["", "## 提示", ""] + ["- %s%s" % ("第%d页：" % s["page"] if s["page"] else "", s["msg"]) for s in rep["soft"]]
    if rep.get("figures"):
        L += ["", "## 原文图片", "", "依据中有原文图片的页面。原图按原比例、原始清晰度嵌入；总体技术路线、架构、部署类图默认用原图，"
              "其余默认按文字重绘。需要换用原图时指定 `L50`（竖图或方图，配要点）或 `L51`（宽图通栏）；换回重绘时指定原来的版式。", "",
              "| 页 | 当前版式 | 原文图片 |", "|---|---|---|"]
        for f in rep["figures"]:
            L.append("| %d | %s%s | %s |" % (f["no"], f["layout"], "（原图）" if f["layout"] in ("L50", "L51") else "",
                                           "；".join(c for c in f["captions"] if c)))
    L += ["", "## 可选版式（智能体内部使用：用户说“换成××的样子”时按名称和用途对应代号；对不上就写进 feedback，不要让用户选代号）", ""] + [
        "- %s %s：%s" % (k, v["name"], v["use"]) for k, v in LAYOUTS.items()]
    L += ["", "## 如何回复", "",
          "- 确认：`{\"decision\": \"approve\"}`",
          "- 修改：`{\"decision\": \"revise\", \"pages\": {\"5\": {\"layout\": \"L26\"}, \"7\": {\"feedback\": \"版面意见原话\"}, "
          "\"8\": {\"edit\": \"内容意见原话\"}}, \"theme\": \"teal\"}`",
          "  - 页号用上表第一列；`edit` 是对内容的意见（改标题、删要点、补内容），会先改内容再重排该页；"
          "`feedback`/`layout` 是对版面的意见；也可改主题（`topic`）；`theme` 可选 blue / teal"
          + "（或 red/slate/green/purple/amber，也可直接写中文名：%s）" % "、".join(THEME_NAMES.values())
          + (" / template（模板配色）；`logo` 可选 cover（只放封面和结束页）/ all（每页右上角）/ none（不放）" if design.get("template_info") else ""),
          "- 只重做点名的页（讲稿同步重写）。写入 `visual_answers.json` 后再次运行 `run`。"]
    return "\n".join(L)


def checkpoint_state(wd):
    """检查点 3 的回答状态：None（无回答或已处理）/ ("approve", ans) / ("revise", ans)。"""
    ans = read_json(wd.p("visual_answers.json"))
    if not ans:
        return None
    h = sha256_text(str(sorted(ans.items(), key=lambda x: x[0])))
    done = read_json(wd.p("visual_answers.applied.json"), {}) or {}
    if done.get("hash") == h:
        return None
    return ans.get("decision"), ans, h


def mark_applied(wd, h, decision):
    from ..core import now_iso
    write_json(wd.p("visual_answers.applied.json"), {"hash": h, "decision": decision, "at": now_iso()})
