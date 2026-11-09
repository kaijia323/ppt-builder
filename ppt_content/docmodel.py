"""文档模型：基于 01_ingest 的解析结果，提供目录、带段落ID的全文、按章节取原文等能力。

章节键（section key）格式：D1-S2.4.1（文档1第2.4.1节）。D1-S0 表示第一个标题之前的内容。
用户在检查点补充的内容以 USER-G1 这样的"虚拟段落"参与写作与引用。
"""
from collections import OrderedDict

from .core import read_json, read_jsonl


def sec_key(b):
    return "%s-S%s" % (b["doc"], b["section"])


def level_of(sec):
    return 0 if sec == "0" else len(sec.split("."))



TABLE_ROW_CAP = 100

class Doc:
    def __init__(self, wd):
        self.wd = wd
        self.blocks = read_jsonl(wd.stage("ingest", "blocks.jsonl"))
        self.by_id = {b["id"]: b for b in self.blocks}
        self.docs = {d["id"]: d for d in read_json(wd.stage("ingest", "docs.json"), [])}
        self.user_blocks = {b["id"]: b for b in read_json(wd.p("user_blocks.json"), [])}
        self.sections = OrderedDict()
        for b in self.blocks:
            k = sec_key(b)
            s = self.sections.get(k)
            if s is None:
                s = self.sections[k] = {"key": k, "doc": b["doc"], "sec": b["section"], "level": level_of(b["section"]),
                                        "title": None, "path": b.get("section_path") or [], "block_ids": [], "chars": 0,
                                        "content_chars": 0}
            if b["type"] == "heading" and s["title"] is None:
                s["title"] = b["text"]
            s["block_ids"].append(b["id"])
            s["chars"] += len(b["text"])
            if b["type"] != "heading":
                # 篇幅按字数计，表格行按行计（每行至多 TABLE_ROW_CAP 字）：超长明细表（如预算功能点清单）不应按字数挤占页数
                s["content_chars"] += min(len(b["text"]), TABLE_ROW_CAP) if b["type"] == "table_row" else len(b["text"])
        for s in self.sections.values():
            if s["title"] is None:
                s["title"] = "（标题前内容）" if s["sec"] == "0" else (s["path"][-1] if s["path"] else s["key"])

    # ------------------------------------------------------------ basic
    def title_numbers(self, extra_titles=()):
        """文档标题/文件名中的数字（如"包4""（一）"中的数字），模型在要点中使用时不算编造。"""
        from .checks import extract_numbers
        nums = set()
        for d in self.docs.values():
            nums |= extract_numbers(d.get("file", ""))
        for t in extra_titles:
            nums |= extract_numbers(t or "")
        return nums

    def subtree_text(self, key):
        return "\n".join(self.by_id[b]["text"] for k, s in self.sections.items() if in_subtree(k, key) for b in s["block_ids"])

    @property
    def total_chars(self):
        return sum(len(b["text"]) for b in self.blocks)

    def block(self, bid):
        return self.by_id.get(bid) or self.user_blocks.get(bid)

    def has_block(self, bid):
        return bid in self.by_id or bid in self.user_blocks

    def subtree_chars(self, key):
        return sum(s["content_chars"] for k, s in self.sections.items() if in_subtree(k, key))

    def key_sections(self, max_level=2):
        """需要被"逐章理解"覆盖的章节：一、二级章节中自身或下级含正文的。"""
        out = []
        for k, s in self.sections.items():
            if 1 <= s["level"] <= max_level and self.subtree_chars(k) > 0:
                out.append(k)
        return out

    def valid_section(self, key):
        return key in self.sections

    def normalize_key(self, key, default_doc="D1"):
        """容错：S2.1 / 2.1 / D1-S2.1 / [D1-S2.1] → D1-S2.1。"""
        k = str(key).strip().strip("[]【】 ")
        if k.startswith("USER-"):
            return k
        if "-S" in k:
            return k
        k = k.lstrip("Ss")
        return "%s-S%s" % (default_doc, k)

    # ------------------------------------------------------------ renderings
    def outline_text(self, notes=None, with_chars=True):
        """目录：每行 [章节键] 标题（字数）[—— 备注]。"""
        lines = []
        cur_doc = None
        for k, s in self.sections.items():
            if s["doc"] != cur_doc:
                cur_doc = s["doc"]
                lines.append("【文档 %s：%s】" % (cur_doc, self.docs.get(cur_doc, {}).get("file", "")))
            if s["level"] == 0 and s["content_chars"] == 0:
                continue
            ind = "  " * max(0, s["level"] - 1)
            extra = ("（%d字）" % self.subtree_chars(k)) if with_chars else ""
            note = (" —— " + notes[k]) if notes and notes.get(k) else ""
            lines.append("%s[%s] %s%s%s" % (ind, k, s["title"], extra, note))
        return "\n".join(lines)

    def full_text(self, keys=None):
        """带段落ID的全文；keys 不为空时只输出这些章节（含下级）。"""
        lines = []
        cur_doc = None
        for k, s in self.sections.items():
            if keys is not None and not any(in_subtree(k, x) for x in keys):
                continue
            if s["doc"] != cur_doc:
                cur_doc = s["doc"]
                lines.append("\n======== 文档 %s：%s ========" % (cur_doc, self.docs.get(cur_doc, {}).get("file", "")))
            if s["level"] > 0:
                lines.append("\n%s [%s] %s" % ("#" * min(6, s["level"] + 1), k, s["title"]))
            for bid in s["block_ids"]:
                b = self.by_id[bid]
                if b["type"] == "heading":
                    continue
                tag = {"table_row": "（表）", "table_row_text": "（表）", "note": "（备注）", "caption": "（题注）", "list": ""}.get(b["type"], "")
                lines.append("[%s]%s %s" % (bid, tag, b["text"]))
        return "\n".join(lines)

    def path_str(self, key):
        s = self.sections.get(key)
        if not s:
            return key
        return " > ".join(s["path"]) if s["path"] else s["title"]

    def resolve(self, keys):
        """章节键或段落ID → 有序、去重的段落ID列表（不含标题段落）。"""
        want = []
        for k in keys:
            if self.has_block(k):
                if k not in want:
                    want.append(k)
                continue
            for sk, s in self.sections.items():
                if in_subtree(sk, k):
                    for bid in s["block_ids"]:
                        if self.by_id[bid]["type"] != "heading" and bid not in want:
                            want.append(bid)
        order = {b["id"]: i for i, b in enumerate(self.blocks)}
        user = [b for b in want if b in self.user_blocks]
        return sorted([b for b in want if b in order], key=lambda x: order[x]) + user

    def focused_text(self, keys, query, max_chars, scope=None):
        """按问题取原文（0.6）：所给章节放得下就全给；放不下时按与问题的词语重合度挑段落，
        保持原文顺序，避免像按篇幅截断那样把最具体的小节（往往在章节末尾）截掉。
        keys 为空时在 scope（默认全部正文）里检索。返回 (文本, 段落ID列表, 是否摘取)。"""
        from .checks import bigrams
        ids = self.resolve(keys) if keys else [b["id"] for b in self.blocks if b.get("type") != "heading"
                                               and (not scope or any(in_subtree(sec_key(b), k) for k in scope))]
        total = sum(len(self.block(b)["text"]) for b in ids)
        if keys and total <= max_chars:
            t, used, _ = self.source_text(keys, max_chars * 1.1)
            return t, used, False
        Q = bigrams(query)
        def score(bid):
            t = self.block(bid)["text"]
            B = bigrams(t)
            return len(Q & B) / float(max(1, len(Q))) + (0.02 if len(t) > 40 else 0)
        ranked = sorted(ids, key=lambda b: -score(b))
        pick, n = set(), 0
        for b in ranked:
            ln = len(self.block(b)["text"])
            if n + ln > max_chars and pick:
                continue
            if keys or score(b) >= 0.1:
                pick.add(b)
                n += ln
        sel = [b for b in ids if b in pick]
        if not sel:
            return "", [], True
        t, used, _ = self.source_text(sel, max_chars * 1.2)
        return t, used, True

    def source_text(self, keys, max_chars):
        """按章节分组输出原文：每组带章节路径，便于模型理解上下文。返回 (文本, 段落ID列表, 是否截断)。
        超长时按各依据章节的篇幅比例分配字数（每个依据至少保留开头部分），而不是只保留排在前面的章节（0.3.0）。"""
        ids = self.resolve(keys)
        total = sum(len(self.block(b)["text"]) for b in ids)
        keep = set(ids)
        truncated = False
        if total > max_chars and len(keys) > 1:
            truncated = True
            groups = []
            for k in keys:
                g = [b for b in self.resolve([k]) if b in keep and all(b not in x for x in groups)]
                groups.append(g)
            sizes = [sum(len(self.block(b)["text"]) for b in g) for g in groups]
            keep = set()
            floor = min(3000, max_chars // max(1, len(keys)))
            for g, sz in zip(groups, sizes):
                quota = max(floor, int(max_chars * sz / float(total or 1)))
                n = 0
                for b in g:
                    ln = len(self.block(b)["text"])
                    if n + ln > quota and n > 0:
                        break
                    keep.add(b)
                    n += ln
        lines, used, n, cur = [], [], 0, None
        for bid in ids:
            if bid not in keep:
                continue
            b = self.block(bid)
            if bid in self.user_blocks:
                k = "USER"
                header = "\n## 用户在检查点补充的内容"
            else:
                k = sec_key(b)
                header = "\n## [%s] %s" % (k, self.path_str(k))
            if n + len(b["text"]) > max_chars * 1.05 and used:
                truncated = True
                break
            if k != cur:
                lines.append(header)
                cur = k
            tag = "（表）" if b.get("type") in ("table_row", "table_row_text") else ""
            lines.append("[%s]%s %s" % (bid, tag, b["text"]))
            used.append(bid)
            n += len(b["text"])
        return "\n".join(lines), used, truncated


def in_subtree(key, parent):
    """key 是否等于 parent 或位于其下级（D1-S5.1 包含 D1-S5.1.4，不包含 D1-S5.10）。"""
    return key == parent or key.startswith(parent + ".")
