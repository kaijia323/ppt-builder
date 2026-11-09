"""确定性校验工具：不依赖模型，结果可复现。模型的判断不能推翻这里的结论。"""
import ast
import difflib
import re
import unicodedata

_PUNCT_MAP = str.maketrans({
    "，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")", "！": "!", "？": "?",
    "“": '"', "”": '"', "‘": "'", "’": "'", "、": ",", "【": "[", "】": "]", "《": "<", "》": ">",
    "～": "~", "—": "-", "–": "-", "－": "-", "／": "/", "％": "%", "＋": "+", "×": "x",
})


def norm(s):
    """用于比对的规范化：全角转半角、统一标点、去空白、小写。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.translate(_PUNCT_MAP)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def norm_loose(s):
    """更宽松：再去掉所有标点，仅保留文字与数字。"""
    s = norm(s)
    return re.sub(r"[^\w%.]", "", s)


# ---------------------------------------------------------------- numbers
# 注意：不能用 \w 做边界——Python 中汉字属于 \w，会导致"由800ms"中的 800 抽不出来
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(%|‰)?")
_CN_UNIT = {"万": 1e4, "亿": 1e8, "千": 1e3, "百万": 1e6}


def extract_numbers(s):
    """抽出文本中的阿拉伯数字（含千分位、小数、百分号），返回规范化字符串集合。
    例：'1,200万元，提升40%' -> {'1200', '40%'}；年份/编号同样会被抽出（保守策略）。"""
    out = set()
    if not s:
        return out
    t = sup_to_caret(str(s))
    t = unicodedata.normalize("NFKC", t)
    for m in _NUM_RE.finditer(t):
        ip = m.group(1).replace(",", "")
        dp = m.group(2) or ""
        if dp:
            dp = dp.rstrip("0")
            if dp == ".":
                dp = ""
        val = ip.lstrip("0") or "0"
        out.add(val + dp + (m.group(3) or ""))
    return out


_SUPD = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUBD = "₀₁₂₃₄₅₆₇₈₉"


def sup_to_caret(s):
    """10⁵ → 10^5、CO₂ → CO_2：否则 NFKC 会把 10⁵ 变成 105，数字比对时与“10^5”“1×10^5”对不上。"""
    s = re.sub("[⁻]?[%s]+" % _SUPD, lambda m: "^" + m.group(0).replace("⁻", "-").translate(str.maketrans(_SUPD, "0123456789")), s)
    return re.sub("[%s]+" % _SUBD, lambda m: "_" + m.group(0).translate(str.maketrans(_SUBD, "0123456789")), s)


def caret_to_sup(s):
    """展示用：把 10^5、10^(5)、10^-3 统一写成 10⁵、10⁻³（同一份稿件写法一致）。"""
    return re.sub(r"(?<=\d)\^\(?(-?\d{1,3})\)?", lambda m: m.group(1).replace("-", "⁻").translate(str.maketrans("0123456789", _SUPD)), s or "")


_CN_DIGITS = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def cn_small_numbers(text):
    """原文中的中文小数字（一～十、两）对应的阿拉伯数字：PPT 中把"三类"写成"3类"不算编造。"""
    return {v for k, v in _CN_DIGITS.items() if k in (text or "")}


def enum_counts(text):
    """文本自身列举的项数（“关系图谱、时空全景、业务全景3模块”中的 3）：数出来的个数不是编造的数字。"""
    out = set()
    for seg in re.split(r"[，,。；;：:（）()]", text or ""):
        items = [x for x in re.split(r"[、/]|和|与|及", seg) if x.strip()]
        if len(items) >= 2:
            out.add(str(len(items)))
    return out


def numbers_not_in(claim_text, evidence_texts, extra_allowed=()):
    """claim 中出现、但证据与额外允许集合中都没有的数字。"""
    ev = set()
    for t in evidence_texts:
        ev |= extract_numbers(t)
        ev |= cn_small_numbers(t)
    allowed = ev | set(extra_allowed)
    # 百分号容错：证据里 40%，claim 写 40 也算；反之不算
    allowed_bare = {a.rstrip("%") for a in allowed}
    missing = []
    for n in extract_numbers(claim_text):
        if n in allowed:
            continue
        if not n.endswith("%") and n in allowed_bare:
            continue
        missing.append(n)
    return sorted(missing)


# ---------------------------------------------------------------- quotes
def quote_match(quote, source_text):
    """返回 (status, score)：exact / loose / fail，score 为最佳模糊匹配度（用于日志诊断）。"""
    q, s = norm(quote), norm(source_text)
    if not q:
        return "fail", 0.0
    if q in s:
        return "exact", 1.0
    ql, sl = norm_loose(quote), norm_loose(source_text)
    if ql and ql in sl:
        return "loose", 1.0
    return "fail", round(best_window_ratio(q, s), 3)


def best_window_ratio(q, s):
    if not q or not s:
        return 0.0
    n = len(q)
    if len(s) <= n:
        return difflib.SequenceMatcher(None, q, s).ratio()
    best = 0.0
    step = max(1, n // 4)
    for i in range(0, len(s) - n + 1, step):
        r = difflib.SequenceMatcher(None, q, s[i:i + n]).ratio()
        if r > best:
            best = r
    return best


# ---------------------------------------------------------------- similarity
def bigrams(s):
    s = norm_loose(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def jaccard(a, b):
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


def dup_pairs(pages, threshold=0.6, min_chars=12):
    """跨页疑似重复的要点（bigram 包含度 ≥ threshold）：同一事实在两页各讲一遍（G38）。"""
    items = [(p["no"], b["id"], b["text"]) for p in pages for b in p.get("bullets", []) if len(norm_loose(b.get("text", ""))) >= min_chars]
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][0] == items[j][0]:
                continue
            A, B = bigrams(items[i][2]), bigrams(items[j][2])
            sim = len(A & B) / float(min(len(A), len(B)) or 1)      # 包含度：短的一条大部分被长的一条覆盖
            if sim >= threshold:
                out.append({"a": items[i][1], "b": items[j][1], "pages": [items[i][0], items[j][0]], "sim": round(sim, 2),
                            "text_a": items[i][2], "text_b": items[j][2]})
    return out


_CNUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_COUNT_RE = re.compile(r"([0-9]{1,3}|[一二两三四五六七八九十])\s*(大|个|类|项|套|种|家|级)?([\u4e00-\u9fa5]{2,6})")


def count_claims(text):
    """抽取"数量＋名词"（五大智能体、7个智能体、3个能力中心）：返回 [(名词前缀, 数量)]，前缀取 3—5 字以容忍后缀差异。"""
    out = []
    for m in _COUNT_RE.finditer(text or ""):
        n = int(m.group(1)) if m.group(1).isdigit() else _CNUM.get(m.group(1))
        if not n or n > 100:
            continue
        noun = m.group(3)
        for L in (3, 4, 5):
            if len(noun) >= L:
                out.append((noun[:L], n))
    return out


def count_conflicts(pages):
    """跨页数量不一致（G47）：同一名词在不同页面出现不同数量，如第3页"五大智能体"、第15页"7个智能体"。"""
    seen = {}
    for p in pages:
        for t in [p.get("title", "")] + [b.get("text", "") for b in p.get("bullets", [])]:
            for noun, n in count_claims(t):
                seen.setdefault(noun, {}).setdefault(n, set()).add(p["no"])
    out, used = [], set()
    for noun in sorted(seen, key=len, reverse=True):
        nums = seen[noun]
        if len(nums) < 2:
            continue
        pages_all = set().union(*nums.values())
        if len(pages_all) < 2 or any(noun in u for u in used):
            continue
        used.add(noun)
        out.append({"noun": noun, "counts": {str(k): sorted(v) for k, v in nums.items()}})
    return out


def similarity(a, b):
    return difflib.SequenceMatcher(None, norm_loose(a), norm_loose(b)).ratio()


# ---------------------------------------------------------------- lexicon
def overstatements(text, evidence_texts, words):
    """输出里出现、但证据里没有的程度词/绝对化用词。"""
    ev = norm("".join(evidence_texts))
    t = norm(text)
    return [w for w in words if norm(w) in t and norm(w) not in ev]


def dropped_hedges(claim, quote, hedges):
    """原文有限定词（预计/计划/约…），但 claim 中一个都没有 -> 可能把计划写成了事实。"""
    q, c = norm(quote), norm(claim)
    in_q = [h for h in hedges if norm(h) in q]
    if in_q and not any(norm(h) in c for h in hedges):
        return in_q
    return []


# ---------------------------------------------------------------- calculations
_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
            ast.Pow, ast.USub, ast.UAdd, ast.Constant, ast.Mod)


def safe_eval(expr):
    expr = unicodedata.normalize("NFKC", str(expr)).replace("×", "*").replace("÷", "/").replace(",", "")
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError("表达式含不允许的元素: %s" % type(node).__name__)
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("表达式只能包含数字")
    return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})


def check_calculation(calc, fact_numbers):
    """校验衍生计算：表达式可执行、结果与声明一致（按声明的小数位四舍五入）、操作数来自被引事实。"""
    issues = []
    try:
        val = safe_eval(calc.get("expr", ""))
    except Exception as e:
        return ["表达式无法计算: %s (%s)" % (calc.get("expr"), e)], None
    try:
        claimed = float(str(calc.get("result")).replace(",", "").rstrip("%"))
    except Exception:
        return ["声明结果不是数字: %s" % calc.get("result")], val
    s = str(calc.get("result")).rstrip("%")
    decimals = len(s.split(".")[1]) if "." in s else 0
    if abs(round(val, decimals) - claimed) > 0.5 * 10 ** (-decimals) + 1e-9:
        issues.append("计算结果不符：%s = %.4f，声明为 %s" % (calc.get("expr"), val, calc.get("result")))
    operands = extract_numbers(re.sub(r"[()]", " ", str(calc.get("expr", ""))))
    trivial = {"100", "1000", "10", "2", "1", "0", "12", "60", "24"}  # 单位换算常数
    bare = {f.rstrip("%") for f in fact_numbers}
    for o in operands:
        if o not in bare and o not in trivial:
            issues.append("操作数 %s 不在被引事实中" % o)
    return issues, val


def fmt_number(v, decimals):
    return ("%." + str(decimals) + "f") % v if decimals else str(int(round(v)))


# ---------------------------------------------------------------- misc
def dwidth(s):
    """显示宽度（按汉字计）：汉字/全角字符记 1，ASCII 字母数字符号与上下标记 0.5，结果向上取整。
    长度上限按显示宽度判断：CD8⁺、DeepLabv3+、IFN-γ 这类写法不会被当成同样多的汉字。"""
    w = 0.0
    for ch in str(s or ""):
        o = ord(ch)
        if o < 0x80 or ch in "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₀₁₂₃₄₅₆₇₈₉₊₋αβγδεκμσ–":
            w += 0.5
        elif unicodedata.east_asian_width(ch) in ("F", "W"):
            w += 1
        else:
            w += 0.5
    return int(w + 0.999)


def split_sentences(text):
    parts = re.split(r"(?<=[。！？；!?;])\s*|\n+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def truncate(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"
