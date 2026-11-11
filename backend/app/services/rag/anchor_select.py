"""
把「chunk 级引用」落到「句子级引用」上。

LLM 只会说 `[引#3]`——它看到的是编号片段，不知道片段里第几句才是证据。
所以中间必然有一次"claim 句 ↔ 候选句"的匹配，这次匹配的质量决定了角标点下去
能不能落在对的那句话上。

为什么不用语义模型：
一次问答要匹配的 pair 是「claim 句 × 候选 chunk 的句子」，通常 4 句 × 6 句 = 24 对，
用 cross-encoder 打一轮 200ms 起，而收益很有限——claim 往往是原文句的改写或直引，
字面重合度已经足够。真正需要"语义"的那种（"训练用了多少 GPU" ↔ "8 NVIDIA P100"）
恰恰是字面完全不重合的，语义模型也未必救得回来，反而会被噪声带偏。
所以这里用**词面 + 数字/符号**的混合打分，把"数值类证据"单独加权：
论文里 `β2 = 0.98`、`BLEU 28.4`、`8×A100` 这种 token 一旦对上，几乎可以确定就是这句。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from app.services import anchors as anchor_lib

# 停用词只挡高频功能词。不要用大表：论文句子短，删太多词会让"重叠比例"失去分辨力。
_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was",
    "were", "be", "by", "with", "as", "at", "from", "that", "this", "these", "those",
    "it", "its", "we", "our", "they", "their", "which", "can", "will", "have", "has",
    "not", "but", "than", "then", "so", "such", "into", "over", "all", "any", "may",
    "的", "了", "是", "在", "和", "与", "及", "对", "为", "由", "从", "把", "被",
    "这", "那", "有", "也", "就", "都", "而", "或", "个", "上", "中", "下",
}

_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z\-]{1,}|"
    r"\d+(?:[.,]\d+)*(?:\s*[%％])?|"
    r"[α-ωΑ-Ωβγδεζηθικλμνξοπρστυφχψω]\s*[_^]?\s*\{?\d?",
)
# 数值/符号类 token：这是"硬证据"，权重单独给，不参与普通词的重叠比例
_NUMERIC_RE = re.compile(r"^\d|[α-ωΑ-Ω]")

_USED_PENALTY = 0.22


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text or ""):
        tok = m.group(0).strip().lower()
        tok = re.sub(r"\s+", "", tok)
        if not tok or tok in _STOP:
            continue
        if len(tok) == 1 and not _NUMERIC_RE.match(tok):
            continue
        out.append(tok)
    return out


def score(claim: str, sentence: str) -> float:
    """
    0~1。三项加权：
    - 词面覆盖率 0.5：claim 的词有多少出现在这句里（不是 Jaccard——
      claim 通常比原文短，用"claim 被覆盖了多少"才符合"这句能否支撑它"的问法）
    - 数值/符号重合 0.4：出现一个就接近满分，因为数值几乎不会巧合重合
    - 包含关系 0.1：整句被 claim 直接引用时给一点补偿
    """
    claim_tokens = tokenize(claim)
    sent_tokens = tokenize(sentence)
    if not claim_tokens or not sent_tokens:
        return 0.0
    sent_set = set(sent_tokens)
    hit = sum(1 for t in claim_tokens if t in sent_set)
    coverage = hit / len(claim_tokens)

    claim_nums = {t for t in claim_tokens if _NUMERIC_RE.match(t)}
    sent_nums = {t for t in sent_tokens if _NUMERIC_RE.match(t)}
    num_score = 0.0
    if claim_nums:
        num_score = len(claim_nums & sent_nums) / len(claim_nums)

    contains = 1.0 if sentence.strip() and sentence.strip().lower() in claim.lower() else 0.0
    return 0.5 * coverage + 0.4 * num_score + 0.1 * contains


def select(
    claim: str,
    chunk_text: str,
    doc: Any,
    *,
    used: Iterable[str] = (),
    min_score: float = 0.02,
) -> dict[str, Any] | None:
    """
    在 chunk 的锚点里挑一条最像"这句话的证据"的，返回锚点（附 `text`）。

    `used` 是同一篇答案里已经被别的句子用掉的 ref_id：一句原文常常同时支撑两个 claim
    （"用了 8 块 P100" 和 "训练了 3.5 天" 可能同出一句），这时第二个 claim 指向同一句是对的；
    但如果另一句也能支撑，就应该分开——排重让"一句一锚点"的观感更准确，
    同时也避免读者点三个角标落在同一处。
    惩罚系数取 0.22 而不是直接排除：宁可指向同一句，也不要指向无关句。
    """
    items = anchor_lib.items_of(doc)
    if not items:
        return None
    used_set = set(used)
    best: dict[str, Any] | None = None
    best_score = -1.0
    for item in items:
        text = anchor_lib.slice_text(chunk_text, item)
        if not text:
            continue
        s = score(claim, text)
        if item.get("ref_id") in used_set:
            s -= _USED_PENALTY
        # 同分时取靠前的句子：claim 通常先引用靠前的证据
        if s > best_score:
            best_score = s
            best = {**item, "text": text}
    if best is None or best_score < min_score:
        return None
    best["match_score"] = round(max(0.0, best_score), 4)
    return best


def select_in_text(
    claim: str, chunk_text: str, *, min_score: float = 0.02, max_len: int = 320
) -> dict[str, Any] | None:
    """
    没有句级锚点时的降级：直接在 chunk 原文上切句找最像的一句。

    为什么需要它：全量语料里仍有相当一部分 chunk 没有建立锚点
    （双栏对齐失败、扫描件、历史版本）。这些片段被引用时，
    如果直接判"无证据"，就等于把"定位能力不足"报成了"原文里没有"——
    07 之后这两种情况分属 needs_check 与 unsupported，混在一起会让
    置信分布失去意义（实测 ViT/BERT 的 unsupported 一度全来自这里）。

    返回的 `precision` 固定为 `block`：句子是现场切的，没有 rects，
    前端只能画块级高亮——这恰好也是它该有的诚实程度。
    """
    spans = anchor_lib.split_sentences(chunk_text or "", max_len=max_len)
    best: dict[str, Any] | None = None
    best_score = -1.0
    for s, e in spans:
        sent = (chunk_text or "")[s:e].strip()
        if not sent:
            continue
        s_score = score(claim, sent)
        if s_score > best_score:
            best_score = s_score
            best = {"start": s, "end": e, "text": sent}
    if best is None or best_score < min_score:
        return None
    return {
        **best,
        "ref_id": None,
        "kind": "sentence",
        "precision": "block",
        "rects": [],
        "bbox": None,
        "match_score": round(max(0.0, best_score), 4),
    }


def claim_around(answer: str, start: int, end: int, max_len: int = 320) -> str:
    """
    取答案中包住 [start, end) 的这个标记所在的那句话。

    没有这一步就只能拿"整段答案"去匹配，句子一多，所有角标都会指向同一句。

    为什么用切句器而不是"向前找句号"：
    朴素做法会在小数点上截断——`β2 = 0.98 [引#1]` 会被取成 `98`，
    于是 claim 只剩一个数字，匹配到哪句全看运气。这类错误在日志里表现为
    "claim 字段莫名其妙只有一个词"，很容易被当成模型问题查半天。
    """
    if not answer:
        return ""
    spans = anchor_lib.split_sentences(answer, max_len=max_len * 2)
    lo, hi = None, None
    for s, e in spans:
        if s <= start <= e:
            lo, hi = s, e
            break
    if lo is None:
        # 标记落在所有句子之外（例如答案末尾孤立一个角标）：退回最后一句
        lo, hi = spans[-1] if spans else (0, len(answer))

    # 去掉标记本身，否则 claim 里带着 [引#3] 会污染分词
    claim = (answer[lo:start] + answer[end:hi]).strip()
    if len(claim) > max_len:
        claim = claim[-max_len:]
    return claim
