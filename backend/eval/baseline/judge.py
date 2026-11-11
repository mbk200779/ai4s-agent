"""
评测判定：把"检索到没检索到"变成一个可复算的布尔值。

这个文件只有一件事要做，但它是整个评测的地基：
**把 gold 标注与检索结果对上的那一步，必须与索引版本无关。**

所以 gold 不标 chunk_id、也不标 chunk_index——那些值会随切分策略、重建批次变化，
标了就等于把评测集焊死在某一代索引上，一重建全部失效（13 的教训：chunk_index 会重排）。
gold 标的是 (arxiv_id, 页, 原文引句)，判定靠"这句原文在不在这一条片段里"。

匹配规则分两档，刻意不对称：
  - 长引句（>24 字符）允许模糊命中，取最长公共子串 ≥ max(24, 0.6×引句长度)。
    理由：PDF 抽取的连字符断词（"de- composition"）、ﬁ/ffi 连字、行内公式，
    会让同一个句子在不同版本里差几个字符，死等字符串相等会漏判。
  - 短引句（≤24 字符，多见于术语定位题）只认整串包含。
    理由：短串的模糊匹配几乎必然命中（"rank" 能匹配到 "ranking"），
    那一格指标就变成了噪声。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# 长引句的模糊阈值：绝对下限与相对下限取大
FUZZY_MIN_CHARS = 24
FUZZY_RATIO = 0.6
SHORT_QUOTE_CHARS = 24

_WS = re.compile(r"\s+")
# PDF 排版会在行尾断词：`de- composition`、`to- kens`、`ﬁne- tuned`。
# 引句是人按正常拼写写的，所以两侧都先"把断开的词接回去"，再比对。
# 接的时候连短横一起去掉（`selfattention`），因为 `self-attention` 与
# `self- attention` 在正文里也混用——两侧同规则，代价为零。
_HYPHEN_BREAK = re.compile(r"(?<=\w)[-‐‑]\s*(?=\w)")
# 连字（ﬁ/ﬂ/ﬀ/ﬃ/ﬄ）是 PDF 常见的字形替换，同一个词在作者眼里就是一个词
_LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
                            "\u00ad": ""})


def norm(text: str) -> str:
    """
    归一化：连字还原 → 接回断词 → 空白折叠 → 小写。

    只做这四件事。去标点看起来更"干净"，但会把 "dmodel = 512" 和
    "dmodel 512" 之间唯一的中介也删掉，反而更容易产生假命中；
    而大小写差异是真实存在的书写差异，统一掉没有副作用。
    """
    t = (text or "").translate(_LIGATURES)
    t = _HYPHEN_BREAK.sub("", t)
    return _WS.sub(" ", t).strip().lower()


def quote_hit(quote: str, text: str) -> bool:
    """这条片段里有没有这句话。"""
    q = norm(quote)
    t = norm(text)
    if not q or not t:
        return False
    if q in t:
        return True
    if len(q) <= SHORT_QUOTE_CHARS:
        return False
    block = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(
        0, len(q), 0, len(t)
    )
    return block.size >= max(FUZZY_MIN_CHARS, int(len(q) * FUZZY_RATIO))


def locate(gold: dict, results: list[dict]) -> int | None:
    """
    gold 这条证据在 top-k 里第一次出现在第几名（1 起），没出现返回 None。

    只认同一篇论文：不同论文里可能出现同样的套话（"we train on"），
    跨论文命中会把"召回对了"变成"召回巧合"。
    """
    aid = gold.get("arxiv_id")
    quote = gold.get("quote") or ""
    for i, r in enumerate(results, start=1):
        if aid and r.get("arxiv_id") != aid:
            continue
        if quote_hit(quote, r.get("text") or ""):
            return i
    return None


def score_question(record: dict, results: list[dict]) -> dict:
    """
    一道题的判定结果。results 需按排名有序，且带 text/arxiv_id。

    hit@k      : 任一 gold 证据落在前 k 名
    mrr@k      : 第一条命中的倒数名次
    coverage@k : gold 证据被覆盖的比例（多证据题才和 hit 不同）
    ndcg@k     : 二值相关性 nDCG，用来惩罚"命中了但排在第 5"
    """
    gold = record.get("gold") or []
    k = record.get("k", 5)
    ranks = [locate(g, results[:k]) for g in gold] if gold else []
    found = [r for r in ranks if r]
    hit = bool(found)
    mrr = (1.0 / min(found)) if found else 0.0
    coverage = (len(found) / len(gold)) if gold else 0.0
    dcg = sum(1.0 / _log2(r + 1) for r in found)
    idcg = sum(1.0 / _log2(i + 2) for i in range(min(len(gold) or 1, k)))
    return {
        "hit": hit,
        "mrr": round(mrr, 4),
        "coverage": round(coverage, 4),
        "ndcg": round(dcg / idcg, 4) if idcg else 0.0,
        "gold_ranks": ranks,
        "gold_total": len(gold),
    }


def _log2(x: float) -> float:
    from math import log2

    return log2(x)


def separation(rows: list[dict]) -> float:
    """
    阈值无关的拒答信号可分性：用 `top_raw / threshold` 把"可答"与"不可答"分开的 AUC。

    为什么要这个东西：任何一个"拒答准确率"都隐含一个阈值选择，而阈值是主观的
    （双路 0.03 ≈ 两路都进前 5，单路换成名次等价线 1/65，两个数没有可比性）。
    只要换个阈值，误拒率和正确拒答率就一起动，比出来的差异说不清是"信号强"还是
    "尺子松"。AUC 只问排序：不可答题的分数是不是系统性地低于可答题。
    0.5 = 完全没有信号，1.0 = 完美分开。

    用比值而不是原始分：不同策略的阈值相差一倍以上（点查按权重缩放），
    拿原始分跨策略比 AUC 等于拿两种货币相加。
    """
    pos = [r["ratio"] for r in rows if r["expect"] == "answer" and r.get("ratio") is not None]
    neg = [r["ratio"] for r in rows if r["expect"] == "refuse" and r.get("ratio") is not None]
    if not pos or not neg:
        return 0.0
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return round(wins / (len(pos) * len(neg)), 4)


def aggregate(rows: list[dict]) -> dict:
    """
    汇总一组题的指标。

    分母分三种，不能混：
      - 可答题（expect=answer）→ recall/mrr/ndcg/coverage 才有意义；
      - 不可答题（expect=refuse）→ 只看"有没有拒"；
      - 拒答准确率的两半必须分开报：不可答题的"正确拒答率"和可答题的"误拒率"，
        合成一个数会掩盖方向（一个把什么都拒了的系统也能有 50% 的"准确率"）。
    """
    ans = [r for r in rows if r["expect"] == "answer"]
    ref = [r for r in rows if r["expect"] == "refuse"]

    def _avg(items, key):
        return round(sum(i[key] for i in items) / len(items), 4) if items else 0.0

    recall = _avg(ans, "hit") if ans else 0.0
    misreject = round(sum(1 for r in ans if r["rejected"]) / len(ans), 4) if ans else 0.0
    correct_reject = round(sum(1 for r in ref if r["rejected"]) / len(ref), 4) if ref else 0.0
    total = len(ans) + len(ref)
    reject_acc = round(
        (sum(1 for r in ref if r["rejected"]) + sum(1 for r in ans if not r["rejected"])) / total, 4
    ) if total else 0.0
    return {
        "n": len(rows),
        "n_answer": len(ans),
        "n_refuse": len(ref),
        # 可答集检索质量
        "recall@5": round(recall, 4),
        "mrr@5": _avg(ans, "mrr"),
        "ndcg@5": _avg(ans, "ndcg"),
        "coverage@5": _avg(ans, "coverage"),
        # 拒答两侧分开
        "misreject_rate": misreject,
        "correct_refuse_rate": correct_reject,
        "reject_accuracy": reject_acc,
        # 阈值无关：拒答信号本身有多可分（0.5=没信号）
        "separation_auc": separation(rows),
    }
