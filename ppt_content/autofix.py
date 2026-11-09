"""审查问题的默认处理（0.7，取代原检查点 2 的逐条确认）。

核验与终审发现的问题按默认修改意见处理，不再停下来问用户：
- 标题/要点“原文不支持、部分支持、标题偏离”：调用模型按原文改写（只改有问题的部分）；改不了的要点删除，改不了的标题保留并列入待核实
- 要点重复：删除后出现的那条
- 整套层面、整页层面、数量口径不一致等无法自动改的问题：不改，写入《待核实清单》
用户仍可在成品确认环节用自然语言改任意页面。
"""
import re

from .checks import dwidth as em_width, numbers_not_in
from .core import write_text

FIX_CATS = ("claim_unsupported", "claim_partial", "title_drift")


def _evidence(doc, ids, limit=4000):
    out, n = [], 0
    for bid in ids or []:
        b = doc.block(bid)
        if not b:
            continue
        t = b["text"][:800]
        out.append("[%s] %s" % (bid, t))
        n += len(t)
        if n > limit:
            break
    return "\n".join(out)


def _current_text(pages, it):
    p = next((x for x in pages if x["no"] == it["page"]), None)
    if not p:
        return None
    if it["target"] == "title":
        return p.get("title")
    b = next((x for x in p.get("bullets") or [] if x.get("bid") == it["target"] or x.get("id") == it["target"]), None)
    return (b or {}).get("text") or it.get("text")


def decide(items, pages, doc, llm, log):
    """返回 (decisions, pending)：decisions 与 review_decisions.json 格式相同；pending 为待核实的问题。"""
    decisions, pending, seen_dup = {}, [], set()
    fix = []
    for it in items:
        if it["severity"] not in ("blocking", "review") or not isinstance(it["page"], int) or it["page"] <= 0:
            if it["severity"] in ("deck", "blocking", "review"):
                pending.append(it)
            continue
        tgt = str(it["target"])
        if it["category"] == "duplicate_bullet" and "-b" in tgt:
            m = re.search(r"相似度 ([\d.]+)", it.get("detail", ""))
            if m and float(m.group(1)) >= 0.8:          # 几乎相同才自动删；只是相近的列入待核实（可能讲的是不同事实）
                decisions[it["item_id"]] = {"decision": "drop"}
            else:
                pending.append(it)
            continue
        if it["category"] in FIX_CATS and (tgt == "title" or "-b" in tgt) and llm is not None:
            fix.append(it)
            continue
        pending.append(it)

    def one(it):
        claim = it.get("text") or ""
        ev = _evidence(doc, it.get("ids"))
        kind = "标题" if it["target"] == "title" else "要点"
        max_len = 30 if kind == "标题" else max(20, int(em_width(claim) * 1.15) + 4)

        def val(d):
            iss = []
            t = (d.get("text") or "").strip()
            if d.get("action") == "rewrite":
                if not t:
                    iss.append("rewrite 时 text 不能为空")
                elif em_width(t) > max_len + 2:
                    iss.append("太长：限 %d 字" % max_len)
                miss = numbers_not_in(t, [ev])
                if miss:
                    iss.append("数字 %s 不在原文依据中" % miss)
            return iss
        r = llm.call_json("fix_claim", {"kind": kind, "claim": claim, "problem": it.get("detail", ""), "evidence": ev or "（无）",
                                        "max_len": max_len}, target="%s-%s" % (it["item_id"], it["target"]), validator=val)
        return it, r
    for x in llm.map(one, fix, "fix_claim") if fix else []:
        if isinstance(x, Exception):
            continue
        it, r = x
        d = r.get("data") or {}
        if r["ok"] and d.get("action") == "rewrite" and (d.get("text") or "").strip() and d["text"].strip() != (it.get("text") or "").strip():
            decisions[it["item_id"]] = {"decision": "edit", "text": d["text"].strip()}
        elif r["ok"] and d.get("action") == "drop" and it["target"] != "title":
            decisions[it["item_id"]] = {"decision": "drop"}
        else:
            pending.append(it)
    for it in fix:
        if it["item_id"] not in decisions and it not in pending:
            pending.append(it)
    log.info("autofix", "审查问题按默认意见处理：改写 %d 条、删除 %d 条，待核实 %d 条" % (
        sum(1 for d in decisions.values() if d["decision"] == "edit"), sum(1 for d in decisions.values() if d["decision"] == "drop"),
        len(pending)))
    return decisions, pending


def pending_md(pending, decisions, items):
    L = ["# 待核实清单", "", "以下问题无法按原文自动修正，汇报前请核实（也可以在成品确认时直接说明怎么改）。", ""]
    if pending:
        L += ["| 页 | 问题 |", "|---|---|"]
        for it in pending:
            pg = ("第%d页" % it["page"]) if isinstance(it["page"], int) and it["page"] > 0 else "整套"
            L.append("| %s | %s |" % (pg, re.sub(r"\s+", " ", it.get("detail", ""))[:160]))
    else:
        L.append("（无）")
    done = [(items[i], d) for i, d in decisions.items() if i in items]
    if done:
        L += ["", "## 已自动修正", "", "| 页 | 原文字 | 处理 |", "|---|---|---|"]
        for it, d in done:
            L.append("| 第%s页 | %s | %s |" % (it["page"], (it.get("text") or "")[:60],
                                            ("改为：" + d["text"]) if d["decision"] == "edit" else "删除"))
    return "\n".join(L) + "\n"
