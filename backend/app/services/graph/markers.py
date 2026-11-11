"""
正文引用标记 → 条目编号，并统计"提及几处"。

边权为什么要数提及次数，而不是"有没有引用"：
引言里点一次名和整节都在对照它，是两种强度。只用 0/1 会把
"顺手引用"和"真的建立在此基础上"画成一样粗的线。

两种标记都要能落到条目上：
- 编号式 `[12]`：直接就是 ref_index；
- 作者-年份 `(Vaswani et al., 2017)`：按（姓氏, 年份）反查条目，
  允许 ±1 年的偏差（引用年份常写成正式出版年，和 arXiv 年份差一年）。
落不上的标记单独计数——它是"匹配失败率"之外的另一条口径，
说明的是正文那一侧的可解析性。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.services.citation_extractor import extract_from_chunks
from app.services.graph.match import _surname, normalize

_MARKER_RE = re.compile(
    r"[（(]\s*([A-Za-z\u00c0-\u024f'’\-]+)[^,，;；]{0,40}?[,，]\s*((?:19|20)\d{2})[a-z]?"
)


def _surname_index(refs: Iterable[dict[str, Any]]) -> dict[str, list[tuple[int, int | None]]]:
    """姓氏 → [(ref_index, year)]。年份可能缺失，所以给 None 而不是丢掉整条。"""
    index: dict[str, list[tuple[int, int | None]]] = {}
    for ref in refs:
        authors = ref.get("authors") or []
        year = ref.get("year") or ref.get("parsed_year")
        if not authors:
            continue
        sn = _surname(authors[0])
        if not sn:
            continue
        index.setdefault(sn, []).append((int(ref["ref_index"]), year))
    return index


def _resolve_author_year(
    marker: str, index: dict[str, list[tuple[int, int | None]]]
) -> int | None:
    m = _MARKER_RE.search(marker or "")
    if not m:
        return None
    surname = normalize(m.group(1)).replace(" ", "")
    year = int(m.group(2))
    hits = index.get(surname) or []
    if not hits:
        return None
    # 年份最近的优先；同一年有多个候选就取编号最小的（条目顺序通常就是时间顺序）
    best = min(hits, key=lambda h: (abs((h[1] or year) - year), h[0]))
    if best[1] is not None and abs(best[1] - year) > 1:
        return None
    return best[0]


def citation_rows(
    chunks: list[dict[str, Any]], refs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    每条正文标记落一行（供回填写 citations 表）。已经按 (句子, 编号) 去过重，
    同一句里 `[11]` 出现两次只算一处——数的是"用了几个地方"，不是"打了几个字符"。
    """
    items = extract_from_chunks(chunks)
    index = _surname_index(refs)
    known = {int(r["ref_index"]) for r in refs}

    rows: list[dict[str, Any]] = []
    unresolved = 0
    for item in items:
        idx = item.get("ref_index")
        if idx is None:
            idx = _resolve_author_year(item.get("raw_marker") or "", index)
            if idx is None:
                unresolved += 1
                continue
        if idx not in known:
            # 编号超出条目范围：多半是章节号/公式号被当成了引用标记
            unresolved += 1
            continue
        rows.append(
            {
                "ref_index": int(idx),
                "sentence": (item.get("sentence") or "").strip()[:500],
                "chunk_id": item.get("chunk_id"),
                "page": item.get("page"),
                "bbox": item.get("bbox"),
            }
        )

    stats = {
        "markers": len(items),
        "resolved": len(rows),
        "resolved_refs": len({r["ref_index"] for r in rows}),
        "unresolved": unresolved,
    }
    return rows, stats


def outline(
    chunks: list[dict[str, Any]], refs: list[dict[str, Any]]
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """返回 ({ref_index: {count, sentence, page}}, 统计)，用于边权。"""
    rows, stats = citation_rows(chunks, refs)
    hits: dict[int, dict[str, Any]] = {}
    for row in rows:
        slot = hits.setdefault(
            row["ref_index"], {"count": 0, "sentence": "", "page": None}
        )
        slot["count"] += 1
        if not slot["sentence"]:
            slot["sentence"] = row["sentence"][:240]
            slot["page"] = row["page"]
    stats = {
        "markers": stats["markers"],
        "resolved": stats["resolved"],
        "refs_with_markers": len(hits),
        "unresolved": stats["unresolved"],
    }
    return hits, stats
