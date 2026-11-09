"""其他只读检查（0.7.0-dev8）：封面、讲稿时长、敏感信息。结果写进《注意事项》的“其他提醒”，不改动成品。

  封面    落款项原文没找到、显示为占位（如“（待补充）”）；标题里混入“项目编号：…”“密级：…”
  讲稿    按每分钟 230 字估算总时长，比汇报时长多出或少于 25% 以上时提示（5 套真实成品都在范围内）
  敏感    手机号、身份证号、邮箱、IP 地址、密级标识（“机密★”“密级：”“内部资料”“不得外传”等）
          只认标识性写法；“涉密数据”“涉密事项”这类业务词不算（实测是正常内容）
"""
import re

CPM = 230           # 汇报语速：字/分钟
SENSITIVE = [
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("邮箱", re.compile(r"[\w.-]+@[\w-]+\.[A-Za-z]{2,}")),
    ("IP 地址", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("密级标识", re.compile(r"(?:绝密|机密|秘密)\s*[★☆]|密级\s*[：:]|内部资料|内部使用|不得外传|严禁外传|不宜公开")),
]
TITLE_JUNK = re.compile(r"(项目)?编号\s*[：:]\s*\S+?(?=[一-龥]{2})|密级\s*[：:]\s*\S{1,4}")


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            if not str(k).startswith("_"):
                yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def speech_chars(design):
    n = len((design.get("cover") or {}).get("notes") or "") + len(design.get("toc_notes") or "")
    n += sum(len(v or "") for v in (design.get("section_notes") or {}).values())
    n += len((design.get("closing") or {}).get("notes") or "")
    n += sum(len(p.get("notes") or "") for p in design.get("pages") or [])
    return n


def check(design, brief, slide_of, placeholder="（待补充）"):
    """返回 ([{"slide", "kind", "text"}], 讲稿信息)。"""
    out = []
    cv = design.get("cover") or {}
    if cv:
        miss = [k for k, v in cv.get("fields") or [] if not (v or "").strip() or (v or "").strip() == placeholder]
        if miss:
            out.append({"slide": 1, "kind": "cover", "text": "封面“%s”原文没有找到，现在显示“%s”，请补上" % ("”“".join(miss), placeholder)})
        m = TITLE_JUNK.search(cv.get("title") or "")
        if m:
            out.append({"slide": 1, "kind": "cover", "text": "封面标题里混入了“%s”，请核对标题" % m.group(0).strip()})
    # 讲稿时长
    n = speech_chars(design)
    dur = float(brief.get("duration_min") or 0)
    info = None
    if n and dur:
        est = n / float(CPM)
        info = {"chars": n, "minutes": round(est, 1), "duration": dur}
        if est > dur * 1.25 or est < dur * 0.75:
            out.append({"slide": None, "kind": "speech", "text": "讲稿约 %d 字，按正常语速约 %.0f 分钟，与汇报时长 %.0f 分钟%s较多" % (
                n, est, dur, "相比偏长" if est > dur else "相比偏短")})
    # 敏感信息：屏幕上的文字与讲稿
    seen = set()
    slides = [(None, cv)] + [(slide_of.get(p.get("no")), p) for p in design.get("pages") or []]
    for sl, obj in slides:
        texts = list(_strings(obj.get("spec") or {})) + [obj.get(k) or "" for k in ("title", "subtitle", "banner", "intro", "notes")
                                                   if k != "banner" or design.get("banner")]
        if obj is cv:
            texts += [v for _, v in cv.get("fields") or []]
        for t in texts:
            if not isinstance(t, str):
                continue
            for name, rx in SENSITIVE:
                for m in rx.finditer(t):
                    if name == "IP 地址" and not all(0 <= int(x) <= 255 for x in m.group(0).split(".")):
                        continue
                    key = (name, m.group(0))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"slide": sl or 1, "kind": "sensitive",
                                "text": "出现%s“%s”，对外汇报前确认是否可以公开" % (name, m.group(0))})
    return out, info
