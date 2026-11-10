"""讲稿（0.6）：版式定稿后，对着每张幻灯片最终的屏上文字生成汇报人照着念的讲稿。

为什么放在排版之后：写作阶段生成的备注早于排版和检查点 2，屏上内容后来被压缩、改标题、删要点，备注却不跟着改
（实测：标题已删去“跨年度唯一”，备注里还在讲）。标杆讲稿是对着成品页写的，讲的顺序就是屏幕上的阅读顺序。

- 内容页：每页一次模型调用（并行），给最终屏上文字（按阅读顺序）、本页原文、字数上限、在整套中的位置
- 封面、目录、章节过渡页、结尾：固定格式的开场、转场、收尾语，按模板生成（这类话本来就是套话，模板最稳定）
- 字数：全场按“时长 × 每分钟字数”分配，扣除结构页后按各页用时分到内容页
"""
import re

from ..checks import bigrams, numbers_not_in
from .layouts import IMAGE_LAYOUTS

SPEECH_META = re.compile(r"本页|这一页|此页|该页|本张|从各章(中)?提炼|在原文中(找到|有)|原文(中)?(写|提到|指出)|回答[“\"「].{0,40}[”\"」]|按(时间|顺序|时间顺序|.{0,6}顺序)讲|重点(讲|介绍|说明)|"
                         r"可(以)?强调|建议(讲|说|强调|提及)|讲者|汇报人(可|应|需)")
QA_OPEN = re.compile(r"^.{0,24}(靠的是|为什么|怎么保证|如何保证|是什么？|怎么做？)")
NEXT_TALK = re.compile(r"(下面|接下来|下一页|随后)(看|讲|介绍|汇报)")
# 本部分没有过渡页时，提示词要求讲稿开头用“下面汇报第X部分：……”，这是唯一放行的转场句
SECTION_OPEN = re.compile(r"(下面|接下来)汇报第[一二三四五六七八九十0-9]+部分")


def slide_lines(pg, banner=True):
    """一页最终的屏上文字，按阅读顺序（标题 → 导语 → 版面各槽位 → 底部横幅）。"""
    from .design import walk_strings
    L = ["标题：%s %s" % (pg.get("number", ""), pg.get("topic", ""))]
    if pg.get("intro"):
        L.append("导语：" + re.sub(r"[【】]", "", pg["intro"]))
    spec = pg.get("spec") or {}
    if pg.get("layout") in IMAGE_LAYOUTS and spec.get("_img"):
        L.append("原文图片：%s" % (spec["_img"].get("caption") or "（原图）"))
    for _, t, _ in walk_strings(spec):
        t = re.sub(r"[【】]", "", t).strip()
        if t and (len(L) == 0 or L[-1] != "- " + t):      # 相邻重复的标签（如编码条上下两处“位置码”）只留一个
            L.append("- " + t)
    if banner and pg.get("banner"):           # 不放底部横幅时（0.7.0-dev23 起默认）不算屏上文字
        L.append("底部结论：" + pg["banner"])
    return L


def page_source(p, limit=6000):
    """本页原文（要点与屏上细节所引段落，去重）。"""
    seen, out, n = set(), [], 0
    for it in (p.get("bullets") or []) + (p.get("facts") or []):
        for pv in it.get("provenance") or []:
            q = pv.get("quote", "")
            if q and q not in seen:
                seen.add(q)
                out.append(q)
                n += len(q)
                if n > limit:
                    return "\n".join(out)
    return "\n".join(out)


def budget(design, content, cfg):
    """每个内容页的讲稿字数上限。"""
    wc = cfg.get("write", {})
    cpm = float(wc.get("notes_chars_per_minute", 270))
    dur = float(content["meta"].get("duration_min") or 15)
    pages = design["pages"]
    struct = 100 + 80 + 30 + 20 * len(design.get("section_notes") or {})
    total = max(len(pages) * 120, dur * cpm - struct)
    mins = [float(next((p["minutes"] for p in content["pages"] if p.get("page_key") == pg["key"]), 0.75) or 0.75) for pg in pages]
    tm = sum(mins) or 1.0
    return {pg["key"]: int(total * m / tm) for pg, m in zip(pages, mins)}


def speech_issues(d, slide_txt, src, cap, title, banner=""):
    iss = []
    t = (d.get("notes") or "").strip()
    sents = [x for x in re.split(r"[。！？]", t) if x.strip()]
    if banner and sents:
        A, B = bigrams(sents[-1]), bigrams(banner)
        if A and B and len(A & B) / float(min(len(A), len(B))) >= 0.7:
            iss.append("最后一句只是把底部结论原句又念了一遍：用自己的话说出这一页的意义或要达到的指标")
    if len(t) < cap * 0.6:
        iss.append("讲稿过短（%d 字），本页至少 %d 字（篇幅上限 %d 字）" % (len(t), int(cap * 0.6), cap))
    if len(t) > cap * 1.12:
        n_s = len([x for x in re.split(r"[。！？]", t) if x.strip()])
        iss.append("讲稿过长（%d 字、%d 句），本页最多 %d 字：减到 %d 句左右，每句不超过 32 字；并列项只说名称和一句话要点，删去屏上没有的细节"
                   % (len(t), n_s, int(cap * 1.12), max(4, int(cap * 0.85 / 32))))
    m = SPEECH_META.search(t)
    if m:
        iss.append("讲稿里有对本页的说明或给汇报人的提示（“%s”）：直接讲内容" % m.group(0))
    if QA_OPEN.search(t):
        iss.append("开头不要用设问或回答问题的口吻（如“……靠的是……”）：用“××方面”“从××看”“××采用××”点明话题")
    if any(not SECTION_OPEN.match(t[m.start():]) for m in NEXT_TALK.finditer(t)):
        iss.append("内容页不写“下面看/接下来讲”之类的转场（转场在章节过渡页）：最后一句收束本页的结论、意义或指标")
    tt = re.sub(r"^[\d.]+\s*", "", title)
    if len(tt) >= 10 and t.startswith(tt[:10]):
        iss.append("开头不要照念标题，用话题引入")
    miss = numbers_not_in(t, [slide_txt, src])
    if miss:
        iss.append("讲稿中的数字 %s 不在屏幕和本页原文中" % miss)
    items = [x[2:] for x in slide_txt.splitlines() if x.startswith("- ") and len(x) > 8]
    if items:
        B = bigrams(t)
        hit = sum(1 for x in items if bigrams(x) and len(bigrams(x) & B) / float(len(bigrams(x))) >= 0.4)
        if hit < 0.4 * len(items):
            iss.append("屏幕上的内容讲到的不足一半（%d/%d 项）：按屏幕阅读顺序把各项讲到" % (hit, len(items)))
    return iss


def structural_notes(design, content, scen):
    """封面、目录、章节过渡页、结尾的讲稿（模板）。"""
    meta = content.get("meta", {})
    cv = design.get("cover") or {}
    fields = dict((k, v) for k, v in cv.get("fields") or [])
    ph = "（待补充）"
    who = fields.get("承建单位") or fields.get("实施单位") or fields.get("协作单位") or fields.get("汇报单位") or ""
    who = "" if (not who or ph in who) else who
    kind = cv.get("subtitle") or meta.get("doc_type") or "方案"
    kind = re.sub(r"^[—\-\s]+", "", kind).replace("汇报", "") or "方案"
    title = cv.get("title") or meta.get("title", "")
    if who:
        names = [re.sub(r"(股份)?有限(责任)?公司$|信息技术$|科技$|技术$", "", n.strip()) for n in re.split(r"\s{2,}|、|，|,|；", who.strip()) if n.strip()]
        names = [re.sub(r"(信息技术|科技|技术|信息)$", "", n) for n in names]       # 口头称呼：去掉公司类型后缀
        who_s = names[0] if len(names) == 1 else "和".join(["、".join(names[:-1]), names[-1]]) + "联合体"
        cover = "各位专家、各位领导，大家好！我代表%s，就“%s”的%s向各位专家作汇报，恳请批评指正。" % (who_s, title, kind)
    else:
        cover = "各位专家、各位领导，大家好！下面就“%s”的%s向各位专家作汇报，恳请批评指正。" % (title, kind)
    ch = design.get("chapters") or []
    cn = "一二三四五六七八九十"
    num = "两" if len(ch) == 2 else (cn[len(ch) - 1] if 0 < len(ch) <= 10 else str(len(ch)))
    toc = ("本次汇报分为%s个部分：%s。" % (num, "、".join(ch))) if ch else ""
    sec = {str(i): "下面汇报第%s部分：%s。" % (cn[i - 1] if i <= 10 else i, c) for i, c in enumerate(ch, 1)}
    closing = "以上是本次汇报的主要内容，恳请各位专家批评指正，谢谢！"
    return cover, toc, sec, closing


def run(llm, log, design, content, scen, cfg, only=None):
    """为 design 中的页面生成讲稿（就地写入 pg["notes"]）；only：只生成这些 key（检查点 3 修改的页）。"""
    cover, toc, sec, closing = structural_notes(design, content, scen)
    if design.get("cover") is not None:
        design["cover"]["notes"] = cover
    design["toc_notes"] = toc
    # 有过渡页的章才放“下面汇报第×部分”；没有过渡页的章，转场句放在该章第一页讲稿开头
    min_pages = int(design.get("section_min_pages", 2))
    cnt = {}
    for pg in design["pages"]:
        cnt[pg["chapter"]] = cnt.get(pg["chapter"], 0) + 1
    design["section_notes"] = {k: v for k, v in sec.items() if cnt.get(int(k), 0) >= min_pages}
    if design.get("closing") is not None:
        design["closing"]["notes"] = closing
    caps = budget(design, content, cfg)
    cp = {p.get("page_key"): p for p in content["pages"] if p.get("kind") == "content"}
    pages = design["pages"]
    chapters = design.get("chapters") or []

    def one(i):
        pg = pages[i]
        p = cp.get(pg["key"], {})
        lines = slide_lines(pg, bool(design.get("banner")))
        slide_txt = "\n".join(lines)
        src = page_source(p)
        cap = max(110, caps.get(pg["key"], 200))
        first_in_ch = i == 0 or pages[i - 1]["chapter"] != pg["chapter"]
        last_in_ch = i == len(pages) - 1 or pages[i + 1]["chapter"] != pg["chapter"]
        pos = []
        if chapters and pg.get("chapter"):
            pos.append("第%d部分“%s”" % (pg["chapter"], chapters[pg["chapter"] - 1]))
        if first_in_ch and str(pg.get("chapter")) not in design["section_notes"] and chapters:
            pos.append("本部分没有过渡页，讲稿开头先用一句“%s”" % sec.get(str(pg["chapter"]), ""))
        if last_in_ch:
            pos.append("本部分最后一页")
        v = {"slide": slide_txt, "source": src or "（无）", "cap": cap, "low": int(cap * 0.7), "target": int(cap * 0.85),
             "sentences": max(4, int(cap * 0.85 / 32)),
             "minutes": round(cap / float(cfg.get("write", {}).get("notes_chars_per_minute", 270)), 1),
             "position": "；".join(pos) or "（无）", "audience": content["meta"].get("audience", ""),
             "prev": pages[i - 1]["topic"] if i else "（开场）", "next": pages[i + 1]["topic"] if i < len(pages) - 1 else "（结尾）"}
        r = llm.call_json("speech", v, target=pg["key"],
                          validator=lambda d: speech_issues(d, slide_txt, src, cap, "%s %s" % (pg.get("number", ""), pg.get("topic", "")),
                                                           pg.get("banner", "") if design.get("banner") else ""))
        txt = ((r["data"] or {}).get("notes") or "").strip()
        log.gate("speech_clean", pg["key"], r["ok"], None if r["ok"] else (r["issues"] or [""])[0])
        done[0] += 1
        log.info("speech_done", "讲稿 %s 完成（%d/%d）" % (pg["key"], done[0], len(idx)))
        return i, txt
    idx = [i for i, pg in enumerate(pages) if only is None or pg["key"] in only]
    done = [0]
    res = llm.map(one, idx, "speech")
    n_ok = 0
    for x in res:
        if isinstance(x, Exception) or not x:
            continue
        i, txt = x
        if txt:
            pages[i]["notes"] = txt
            n_ok += 1
    return {"speech_pages": len(idx), "speech_written": n_ok,
            "speech_chars": sum(len(pg.get("notes") or "") for pg in pages) + len(cover) + len(toc) +
            sum(len(v) for v in design["section_notes"].values()) + len(closing)}
