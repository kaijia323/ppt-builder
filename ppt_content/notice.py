"""交付整理（0.7.0-dev7）：工作目录顶层只放成果 PPT 和《注意事项.md》，其余过程文件在 _过程文件/。

《注意事项.md》把原来分散在 待核实清单、检查点 1 提醒、成品确认版面问题里的内容合成一份，按重要程度排列：
  一、汇报前请核实          核验后无法按原文自动修正的问题（同类合并）
  二、原文有、PPT 没讲的内容  原文篇幅大却没安排页面、场景要求的模块原文没有、专家可能问却没有页面回答
  三、版面问题              文字放不下、越出画布
  四、程序已自动修改的地方    便于抽查（最多列 10 条，其余见 _过程文件/待核实清单.md）
只读已有结果，不调用模型，不改动 PPT 内容。
"""
import os
import re
import shutil
import time

from .core import read_json, write_text

NOTICE = "注意事项.md"
MAX_FIXED = 10


_SLIDE = {}          # 页号 → 幻灯片序号（build 时按 visual_report 填写；没有成品时按页号显示）


def _pages_text(nos):
    nos = [n for n in nos if isinstance(n, int) and n > 0]
    unit = "页"
    if _SLIDE and nos and all(n in _SLIDE for n in nos):
        nos, unit = [_SLIDE[n] for n in nos], "张幻灯片"
    nos = sorted(set(nos))
    if not nos:
        return "整套"
    runs, s = [], nos[0]
    for a, b in zip(nos, nos[1:] + [None]):
        if b != a + 1:
            runs.append("%d" % s if s == a else ("%d、%d" % (s, a) if a == s + 1 else "%d—%d" % (s, a)))
            s = b
    return "第%s%s" % ("、".join(runs), unit)


def _parse_pending_md(path):
    """旧工作目录没有 pending.json 时，从《待核实清单.md》表格读回。"""
    pend, fixed = [], []
    if not os.path.exists(path):
        return pend, fixed
    head, _, tail = open(path, encoding="utf-8").read().partition("## 已自动修正")
    for ln in head.splitlines():
        m = re.match(r"\|\s*(第(\d+)页|整套)\s*\|\s*(.+?)\s*\|\s*$", ln)
        if m:
            pend.append({"page": int(m.group(2)) if m.group(2) else 0, "detail": m.group(3)})
    for ln in tail.splitlines():
        m = re.match(r"\|\s*第(\d+)页\s*\|\s*(.*?)\s*\|\s*(.+?)\s*\|\s*$", ln)
        if m:
            fixed.append({"page": int(m.group(1)), "text": m.group(2), "action": m.group(3)})
    return pend, fixed


def _load_pending(wd):
    d = read_json(wd.stage("export", "pending.json"))
    if d is not None:
        return d.get("pending") or [], d.get("fixed") or []
    return _parse_pending_md(wd.p("待核实清单.md"))


UNCITED = re.compile(r"重要性高的章节\s*\S+\s+(.+?)\s*没有任何一页引用")


def _group(pending):
    """同类问题合并：把引号内的具体文字替换掉后相同的算一类。"""
    groups = {}
    for it in pending:
        det = re.sub(r"\s+", " ", it.get("detail") or "").strip()
        key = re.sub(r"“[^”]*”", "“…”", det)
        g = groups.setdefault(key, {"pages": [], "detail": det, "n": 0})
        g["pages"].append(it.get("page"))
        g["n"] += 1
    out = []
    for key, g in groups.items():
        whole = all(not (isinstance(p, int) and p > 0) for p in g["pages"])
        text = key if g["n"] > 1 else g["detail"]
        out.append((0 if whole else 1, min([p for p in g["pages"] if isinstance(p, int) and p > 0] or [0]),
                    "%s：%s" % (_pages_text(g["pages"]), text)))
    out.sort()
    return [x[2] for x in out]


def _plan_reminders(wd):
    try:
        from .export import load_ctx
        from .plan import checkpoint_reminders
        plan, und, scen, brief, doc = load_ctx(wd)
        rem = checkpoint_reminders(plan, und, doc, scen, brief, limit=10)
    except Exception:      # noqa  旧工作目录或缺少中间结果时不提示这一类
        return []
    # 只留与成品内容有关的：场景建议、兜底分页属于规划过程，确认大纲时已提示；输入预检另列在“其他提醒”
    return [r for r in rem if not r.startswith("这份文档更像") and "程序兜底" not in r
            and not r.startswith("《") and not r.startswith("原文合计只有")]


def _precheck(wd):
    docs = read_json(wd.stage("ingest", "docs.json"), []) or []
    return [x for d in docs for x in d.get("precheck") or []]


def build(wd, pptx_name=None, slides=None, final=False):
    """final：成品已确认、过程文件即将清理（0.7.0-dev25）——自动修改清单全部列出，不再引用过程文件，结尾提示改为“已完成”。"""
    pending, fixed = _load_pending(wd)
    uncited = []
    rest = []
    for it in pending:
        m = UNCITED.search(it.get("detail") or "")
        if m:
            uncited.append(m.group(1).strip())
        else:
            rest.append(it)
    rem = _plan_reminders(wd)
    missing = list(rem)
    told = " ".join(rem)
    extra = [t for t in dict.fromkeys(uncited) if t not in told]
    if extra:
        missing.append("以下重要章节没有被任何一页引用：%s" % "、".join("“%s”" % t for t in extra[:6])
                       + ("等 %d 处" % len(extra) if len(extra) > 6 else ""))
    rep = read_json(wd.stage("render", "visual_report.json"), {}) or {}
    _SLIDE.clear()
    _SLIDE.update({p["no"]: p["slides"][0] for p in rep.get("pages") or [] if p.get("slides")})
    layout = []
    for p in rep.get("pages") or []:
        for q in p.get("hard") or []:
            layout.append("%s：%s“%s”" % (_pages_text([p["no"]]), "文字放不下" if q.get("kind") == "text_overflow" else "内容越出页面",
                                       (q.get("text") or "")[:20]))
    for g in rep.get("geom") or []:
        if g.get("kind") == "small_font":
            layout.append("第%d张幻灯片：字号偏小（%s），投影时可能看不清：“%s”" % (g["slide"], g.get("detail", ""), g.get("text", "")[:20]))
        elif g.get("kind") == "overlap":
            layout.append("第%d张幻灯片：文字互相压住：“%s”%s" % (g["slide"], g.get("text", ""), g.get("detail", "")))

    L = ["# 注意事项", ""]
    if pptx_name:
        L.append("成品：`%s`%s。以下是汇报前需要留意的地方，按重要程度排列。" % (pptx_name, "（共 %d 张幻灯片）" % slides if slides else ""))
    sp = rep.get("speech")
    if sp:
        L += ["", "讲稿在每页备注里，约 %d 字，按正常语速约 %.0f 分钟（汇报时长 %.0f 分钟）。" % (sp["chars"], sp["minutes"], sp["duration"])]
    numl = ["%s：%s" % ("第%d张幻灯片" % n["slide"] if n.get("slide") else "整套", n["text"])
            for n in rep.get("numbers") or []]
    secs = []
    if rest or numl:
        secs.append(("汇报前请核实", ["- " + x for x in numl + _group(rest)]))
    if missing:
        secs.append(("原文有、PPT 中没有讲的内容（专家可能追问）", ["- " + x for x in missing]))
    if layout:
        secs.append(("版面问题", ["- " + x for x in layout]))
    other = []
    ti = (read_json(wd.stage("design", "design.json"), {}) or {}).get("template_info")
    if ti:                           # 套用户模板（0.7.0-dev9）：写明套了什么、从哪里取的
        pos = {"cover": "放在封面和结束页", "all": "内容页右上角都放（放在母版里，封面、过渡页、结束页不放；要换 Logo 在“视图→幻灯片母版”里改一处即可）", "none": "按要求不放"}.get(ti.get("logo_pos") or "", "")
        parts = []
        if ti.get("file"):
            parts.append(("已套用模板《%s》：主色 #%s（取自%s）" % (ti["file"], ti["primary"], ti.get("color_source")))
                         if ti.get("applied") else "模板《%s》的配色没有使用" % ti["file"])
        parts.append("字体 %s" % ti.get("font"))
        parts.append("Logo %s（来源：%s）" % (pos, ti["logo_source"]) if ti.get("logo_source") else "没有 Logo")
        other.append("整套：" + "，".join(parts) + "。不满意可以说“换回默认配色”“去掉 Logo”“每页都放 Logo”")
        other += ["整套：" + n for n in ti.get("notes") or []]
    other += ["%s：%s" % ("第%d张幻灯片" % m["slide"] if m.get("slide") else "整套", m["text"]) for m in rep.get("misc") or []]
    other += ["原文：" + x for x in _precheck(wd)]
    if other:
        secs.append(("其他提醒", ["- " + x for x in other]))
    if fixed:
        merged = {}
        for f in fixed:            # 同一处文字在多页被同样处理，合并成一行
            merged.setdefault(((f.get("text") or "")[:60], f.get("action") or ""), []).append(f.get("page"))
        rows = ["| 位置 | 原文字 | 处理 |", "|---|---|---|"]
        lim = len(merged) if final else MAX_FIXED
        for (t, act), pgs in list(merged.items())[:lim]:
            rows.append("| %s | %s | %s |" % (_pages_text(pgs), t, act))
        if len(merged) > lim:
            rows += ["", "其余 %d 条见 `%s待核实清单.md`。" % (len(merged) - MAX_FIXED, "" if wd.legacy else "_过程文件/")]
        secs.append(("程序已自动修改的地方（请抽查）", rows))
    if not secs:
        L += ["", "未发现需要特别留意的问题。"]
    cn = "一二三四五六"
    for i, (t, body) in enumerate(secs):
        L += ["", "## %s、%s" % (cn[i], t), ""] + body
    if final:
        L += ["", "---", "", "成品已确认，生成过程中的中间文件已清理。之后需要修改，可直接在 PowerPoint 里改，或重新生成。"]
    else:
        L += ["", "---", "", "需要修改时直接说明（如“第5张幻灯片标题改成……”“第8张换个版式”），会只重做点名的页。"]
    return "\n".join(L) + "\n", {"verify": len(rest) + len(numl), "missing": len(missing), "layout": len(layout),
                                  "fixed": len(fixed)}


def finalize(wd, log, cfg):
    """成品确认后（0.7.0-dev25）：重写最终版《注意事项.md》，删除 _过程文件/，工作目录只剩成果 PPT 和《注意事项.md》。
    output.keep_process_files 为 true 或旧版工作目录（过程文件在顶层）时不清理。返回是否已清理。"""
    import shutil
    if wd.legacy or (cfg.get("output") or {}).get("keep_process_files") or os.environ.get("PPTC_KEEP_PROCESS_FILES"):
        return False
    rep = read_json(wd.stage("render", "visual_report.json"), {}) or {}
    names = sorted(f for f in os.listdir(wd.top) if f.lower().endswith(".pptx"))
    if not names:                      # 顶层没有成品时不清理，避免把唯一的成果删掉
        log.warn("cleanup_skipped", "工作目录顶层没有成品 PPT，过程文件未清理")
        return False
    md, _ = build(wd, names[-1], rep.get("slides"), final=True)
    write_text(wd.deliver(NOTICE), md)
    log.info("cleanup", "成品已确认，清理过程文件：%s" % wd.root)
    left = []
    shutil.rmtree(wd.root, onerror=lambda fn, path, exc: left.append(path))
    if left:              # 个别文件被占用（如预览 PDF 正开着）：删不掉的留下，不影响成果
        try:
            os.makedirs(wd.root, exist_ok=True)
            with open(os.path.join(wd.root, "未能删除的文件.txt"), "w", encoding="utf-8") as f:
                f.write("以下文件被其他程序占用，未能删除，关闭后可手动删除本文件夹：\n" + "\n".join(left))
        except Exception:      # noqa
            pass
    return True


def deliver(wd, log, with_pptx=True):
    """把成果 PPT 复制到工作目录顶层，并写《注意事项.md》。返回顶层成果文件列表。"""
    files = []
    rep = read_json(wd.stage("render", "visual_report.json"), {}) or {}
    name, slides = None, rep.get("slides")
    src = rep.get("pptx")
    if with_pptx and src and os.path.exists(src):
        name = re.sub(r"_\d{6}\.pptx$", ".pptx", os.path.basename(src))
        dst = wd.deliver(name)
        if os.path.abspath(dst) != os.path.abspath(src):
            try:
                shutil.copyfile(src, dst)
            except PermissionError:
                # 顶层成品正被 PowerPoint/WPS 打开：另存一份，不让整个阶段失败
                dst = wd.deliver(name[:-5] + "_%s.pptx" % time.strftime("%H%M%S"))
                shutil.copyfile(src, dst)
                log.warn("pptx_locked", "成品文件正被打开（PowerPoint/WPS），已另存为 %s；关闭旧文件后可删除它" % os.path.basename(dst))
            name = os.path.basename(dst)
        files.append(dst)
    md, stat = build(wd, name, slides)
    write_text(wd.deliver(NOTICE), md)
    files.append(wd.deliver(NOTICE))
    log.info("deliver", "交付整理完成：%s" % "、".join(os.path.basename(f) for f in files), **stat)
    return files
