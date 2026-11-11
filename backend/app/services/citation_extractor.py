"""
正文引用位置抽取：从段落文本里找出 [12] / [12, 13] / (Vaswani et al., 2017)
这类标记，切出所在句子，供 citations 表落库。

关键设计决策：
- 只做「编号 → 句子」的正则抽取，不调用 LLM。引用标记格式高度规整，
  正则的召回率已经够用，而且可以在 Celery 里零成本跑完全文；
- 编号型引用（[12]）能精确映射到 ref_index；作者-年份型（Vaswani et al., 2017）
  只能留原文，由 match_method='author_year' 标记，后续再按标题/作者做模糊匹配；
- bbox 取段落级（chunk 的 bbox），不是句子级。句子级框需要在 merge 阶段
  重新打开 PDF 用 page.search_for(sentence)，成本高、且 merge 时已经拿到
  分片结果没有 page 对象。字段结构先留好，精度后续可提升而不改表。
"""
import re
from typing import Any, Optional

# [12] / [12, 13] / [12-14] / [12–14]，允许全角逗号
NUMERIC_CITE = re.compile(r"\[(\d{1,3}(?:\s*[,，]\s*\d{1,3}|\s*[-–—]\s*\d{1,3})*)\]")

# (Vaswani et al., 2017) / (Vaswani & Shazeer, 2017) / (Vaswani, 2017)
AUTHOR_YEAR = re.compile(
    r"[（(]\s*([A-Z][A-Za-z'\-]+(?:\s+(?:et\s+al\.?|and|&)\s+[A-Z][A-Za-z'\-]+)?"
    r"(?:\s+et\s+al\.?)?)\s*[,，]\s*(19|20)\d{2}\s*[)）]"
)

SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def split_sentences(text: str) -> list[str]:
    """按句末标点切句；不做语言检测，中英混排在同一个正则里覆盖。"""
    parts = SENTENCE_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


def expand_indices(raw: str) -> list[int]:
    """把 "12, 13" 或 "12-14" 展开成 [12, 13] / [12, 13, 14]，并过滤离谱值。"""
    out: list[int] = []
    for chunk in re.split(r"[,，]", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d{1,3})\s*[-–—]\s*(\d{1,3})$", chunk)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            # 防御：区间过大通常是版面噪声（如 [1-999]），跳过
            if 0 <= hi - lo <= 30:
                out.extend(range(lo, hi + 1))
            continue
        if chunk.isdigit():
            out.append(int(chunk))
    return [i for i in dict.fromkeys(out) if 1 <= i <= 500]


def extract_from_text(
    text: str,
    page: Optional[int] = None,
    bbox: Optional[list[float]] = None,
    chunk_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    返回 [{ref_index, sentence, page, bbox, chunk_id, match_method}]。
    ref_index 为 None 表示作者-年份型，只能靠后续模糊匹配。
    """
    results: list[dict[str, Any]] = []
    for sentence in split_sentences(text):
        for m in NUMERIC_CITE.finditer(sentence):
            for idx in expand_indices(m.group(1)):
                results.append(
                    {
                        "ref_index": idx,
                        "sentence": sentence,
                        "page": page,
                        "bbox": bbox,
                        "chunk_id": chunk_id,
                        "match_method": "numeric",
                    }
                )
        if not NUMERIC_CITE.search(sentence):
            for m in AUTHOR_YEAR.finditer(sentence):
                results.append(
                    {
                        "ref_index": None,
                        "sentence": sentence,
                        "page": page,
                        "bbox": bbox,
                        "chunk_id": chunk_id,
                        "match_method": "author_year",
                        "raw_marker": m.group(0),
                    }
                )
    return results


def extract_from_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    遍历正文 chunk（不含公式/图表，它们的文本里不会有规范引用标记），
    同一句里重复的编号只保留一次。
    """
    seen: set[tuple[str, int | None]] = set()
    out: list[dict[str, Any]] = []
    for ch in chunks:
        if ch.get("chunk_type") not in ("text", None):
            continue
        for item in extract_from_text(
            ch.get("text", ""),
            page=ch.get("page"),
            bbox=ch.get("bbox"),
            chunk_id=ch.get("id"),
        ):
            key = (item["sentence"], item["ref_index"])
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out
