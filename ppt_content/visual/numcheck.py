"""数字一致性核对（0.7.0-dev8）：只读不改，结果写进《注意事项》“汇报前请核实”。

只做规则判断得准的三类，不调用模型：
  1. 构成等式（L23“总数 = 分项 + 分项”）：分项相加应等于总数
  2. 文字里的合计：
     “合计27763人：市级21人、县级67人……” / “分别投入21、67、1145、26500人，合计27763人”——逐项列全的，相加应等于合计；
     “总投资X万元，其中A a万元、B b万元”——“其中”可能只列一部分，只在分项之和超过总数时提示
  3. 关键数跨页一致：总投资、总预算、建设周期等，整套 PPT 里只能有一个值（前面带“上期、一期、子系统、其中、年度、累计”等限定的不算）
容差：按显示精度，每个数最多差半个末位（如 1027 万元与 1027.42 万元都算对），带“约”的不核对。
（实测：立项方案 PPT 写“合计拟投入27763人：市级21人、县级67人、镇级1145人、村级26500人”，四项之和为 27733——
 原文表格的合计含省级 30 人（30+21+67+1145+26500=27763），PPT 漏了省级却沿用了总数。）
出处标注（0.7.0-dev9）：回原文核对同一合计——原文就对不上的标“原文如此”；原文里没有这个合计数或原文能对上的，标“PPT 可能写错”。
"""
import re

NUM = r"(\d+(?:,\d{3})*(?:\.\d+)?)"
UNIT = r"(亿元|万元|元|万人次|人次|人月|人天|人日|人|项|个月|个|套|条|台|家|户|月|年|天|小时|GB|TB|PB|公里|千米|km|万)"
MONEY = {"亿元": 1e4, "万元": 1.0, "元": 1e-4, "万": 1.0}
PERIOD = {"年": 12.0, "个月": 1.0, "月": 1.0}
KEY_TERMS = {"总投资": "money", "投资总额": "money", "项目总投资": "money", "总预算": "money", "预算总额": "money",
             "项目预算": "money", "建设周期": "period", "建设工期": "period", "实施周期": "period"}
QUALIFIER = re.compile(r"上期|一期|二期|三期|前期|子系统|分项|其中|年度|累计|本包|包\d|每|单项|各")


def _val(num, unit):
    v = float(num.replace(",", ""))
    if unit in MONEY:
        return v * MONEY[unit], "money"
    return v, unit


def _half(num, unit):
    """该数按显示精度可能的最大四舍五入误差（换算到统一单位）。"""
    d = len(num.split(".")[1]) if "." in num else 0
    return 0.5 * 10 ** (-d) * (MONEY.get(unit, 1.0))


def _close(total, parts, total_raw, parts_raw):
    """total_raw / parts_raw：(数字文字, 单位)。容差 = 各数显示精度的半个末位之和（1027 与 1027.42 都算对）。"""
    s = sum(parts)
    tol = _half(*total_raw) + sum(_half(*r) for r in parts_raw) + 1e-9
    return abs(s - total) <= tol, s


def _fmt(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


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


def _items_after(seg, unit_kind):
    """“：市级21人、县级67人……”里依次取数（单位与总数同类）。"""
    out = []
    for m in re.finditer(NUM + r"\s*" + UNIT, seg):
        v, k = _val(m.group(1), m.group(2))
        if k == unit_kind:
            out.append((v, (m.group(1), m.group(2))))
    return out


N0 = r"\d+(?:,\d{3})*(?:\.\d+)?"
PAT_AFTER = re.compile(r"(?P<w>合计|总计|共计|总投资|投资总额|总预算|预算总额)[^\d，。；,;：:\n]{0,6}?(?P<n>" + N0 + r")\s*(?P<u>"
                       + UNIT[1:-1] + r")\s*(?P<sep>[：:]|[，,]?\s*其中)")
PAT_BEFORE = re.compile(r"(?P<list>" + N0 + r"(?:\s*[、，,和及]\s*" + N0 + r"){1,8})\s*(?P<u1>" + UNIT[1:-1]
                        + r")[^\d。；;\n]{0,4}?(?P<w>合计|共计|总计|共)\s*(?P<n>" + N0 + r")\s*(?P<u2>" + UNIT[1:-1] + r")")


def _u(kind, unit):
    return "万元" if kind == "money" else unit


def text_sums(t):
    """返回 [(说明, 原文片段, 合计数字)]：文字里合计与分项对不上的地方。"""
    out = []
    for m in PAT_AFTER.finditer(t):          # 合计在前：合计 N ：a、b、c / N，其中 a、b
        total, kind = _val(m.group("n"), m.group("u"))
        seg = re.split(r"[。；;\n]", t[m.end():], 1)[0]
        items = _items_after(seg, kind)
        if len(items) < 2:
            continue
        if "约" in m.group(0) or "约" in seg:
            continue
        ok, s = _close(total, [v for v, _ in items], (m.group("n"), m.group("u")), [r for _, r in items])
        partial = "其中" in m.group("sep")
        if ok or (partial and s <= total):
            continue
        frag = "%s%s%s" % (m.group("w"), m.group("n"), m.group("u"))
        out.append(("“%s”%s（%s%s）%s" % (frag, "小于其中所列各项之和" if partial else "与所列各项之和", _fmt(s),
                                        _u(kind, m.group("u")), "" if partial else "不一致"), m.group(0), m.group("n")))
    for m in PAT_BEFORE.finditer(t):         # 合计在后：分别 a、b、c 单位，合计 N 单位
        nums = re.findall(N0, m.group("list"))
        parts = [_val(n, m.group("u1")) for n in nums]
        total, kind = _val(m.group("n"), m.group("u2"))
        if len(nums) < 2 or any(k != kind for _, k in parts):
            continue
        if "约" in m.group(0):
            continue
        ok, s = _close(total, [v for v, _ in parts], (m.group("n"), m.group("u2")), [(n, m.group("u1")) for n in nums])
        if not ok:
            out.append(("“%s”：各项之和为%s%s，与合计不一致" % (m.group(0)[:40], _fmt(s), _u(kind, m.group("u2"))),
                        m.group(0), m.group("n")))
    return out


def key_numbers(t):
    """[(关键词, 类别, 值, 原文片段)]"""
    out = []
    for term, cls in KEY_TERMS.items():
        for m in re.finditer(term + r"[^\d，。；,;\n]{0,8}?" + NUM + r"\s*" + UNIT, t):
            if QUALIFIER.search(t[max(0, m.start() - 6):m.start()]):
                continue
            num, unit = m.group(1), m.group(2)
            v = float(num.replace(",", ""))
            if cls == "money" and unit in MONEY:
                out.append((term, cls, v * MONEY[unit], m.group(0)))
            elif cls == "period" and unit in PERIOD:
                out.append((term, cls, v * PERIOD[unit], m.group(0)))
    return out


def _norm(t):
    return re.sub(r"[\s,，]", "", t or "")


class _Source:
    """原文：判断问题是原文本身就有（原文如此），还是 PPT 与原文不一致（0.7.0-dev9）。"""

    def __init__(self, texts):
        self.ok = bool(texts)
        self.all = _norm("\n".join(texts or []))
        self.bad = set()
        for t in texts or []:
            for _, _, n in text_sums(t):
                self.bad.add(_norm(n))

    def sum_tag(self, total):
        if not self.ok:
            return ""
        n = _norm(total)
        if n in self.bad:
            return "（原文如此，请核对原文）"
        if n not in self.all:
            return "（原文中没有这个合计数，PPT 可能写错）"
        return "（原文这里能对上，PPT 可能写错）"

    def has(self, frag):
        nums = re.findall(N0, frag)
        return not self.ok or all(_norm(x) in self.all for x in nums)


def check(design, slide_of, source=None):
    """design：design.json；slide_of：页号 → 幻灯片序号；source：原文段落文字（可选，用于标注问题出处）。
    返回 [{"slide", "kind", "text", "origin"}]，origin：source（原文如此）/ ppt（PPT 与原文不一致）/ None。"""
    src = _Source(source)
    out = []
    keys = {}
    for pg in design.get("pages") or []:
        sl = slide_of.get(pg.get("no"))
        sp = pg.get("spec") or {}
        if pg.get("layout") == "L23" and isinstance(sp.get("total"), dict):
            tv = re.match(r"\s*" + NUM + r"\s*" + UNIT + r"?", sp["total"].get("value") or "")
            ps = [re.match(r"\s*" + NUM + r"\s*" + UNIT + r"?", x.get("value") or "") for x in sp.get("parts") or []]
            if tv and ps and all(ps):
                tot, k0 = _val(tv.group(1), tv.group(2) or "")
                vals = [_val(x.group(1), x.group(2) or "") for x in ps]
                if all(k == k0 for _, k in vals):
                    ok, s = _close(tot, [v for v, _ in vals], (tv.group(1), tv.group(2) or ""),
                                   [(x.group(1), x.group(2) or "") for x in ps])
                    if not ok:
                        tag = src.sum_tag(tv.group(1))
                        out.append({"slide": sl, "kind": "sum", "text": "“%s %s”与各分项之和（%s）不一致%s" % (
                            sp["total"].get("label", ""), sp["total"].get("value", ""), _fmt(s), tag),
                            "origin": "source" if "原文如此" in tag else ("ppt" if tag else None)})
            lab = sp["total"].get("label") or ""
            for term, cls in KEY_TERMS.items():
                if term in lab and not QUALIFIER.search(lab):
                    for kn in key_numbers(term + (sp["total"].get("value") or "")):
                        keys.setdefault(kn[0], []).append((sl, kn[2], kn[3]))
        texts = [(pg.get("banner") or "") if design.get("banner") else "", pg.get("intro") or ""] + list(_strings(sp))
        seen = set()
        for t in texts:
            for desc, frag, total in text_sums(t):
                if frag not in seen:
                    seen.add(frag)
                    tag = src.sum_tag(total)
                    out.append({"slide": sl, "kind": "sum", "text": desc + tag,
                                "origin": "source" if "原文如此" in tag else ("ppt" if tag else None)})
            for term, cls, v, frag in key_numbers(t):
                keys.setdefault(term if cls == "money" and term not in ("项目预算",) else term, []).append((sl, v, frag))
    # 跨页：同一关键词出现不同的值
    merged = {}
    for term, rows in keys.items():
        grp = "总投资" if KEY_TERMS[term] == "money" else "建设周期"
        merged.setdefault(grp, []).extend(rows)
    for grp, rows in merged.items():
        vals = []
        for sl, v, frag in rows:
            if not any(abs(v - x) <= max(abs(x) * 0.01, 0.01) for x, _ in vals):
                vals.append((v, []))
            for x, lst in vals:
                if abs(v - x) <= max(abs(x) * 0.01, 0.01):
                    lst.append((sl, frag))
                    break
        if len(vals) > 1:
            desc = "；".join("第%s张写“%s”" % ("、".join(str(s) for s in sorted({s for s, _ in lst if s})), lst[0][1])
                            for _, lst in vals)
            miss = [lst[0][1] for _, lst in vals if not src.has(lst[0][1])]
            tail = ("；其中“%s”在原文中找不到，PPT 可能写错" % "”“".join(miss)) if miss and src.ok else ""
            out.append({"slide": None, "kind": "key", "text": "%s前后不一致：%s%s" % (grp, desc, tail),
                        "origin": "ppt" if miss and src.ok else None})
    return out
