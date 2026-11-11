"""
版本差异摘要：告诉用户"这两个版本到底差在哪"。

为什么不能只给一句"相似度 84%"：
用户要做的是决定"要不要保留新版"。84% 这个数字回答不了"我上次改的那部分进去了没有"。
所以差异摘要必须落到可核对的东西上：多了哪些章节、多少段是新写的、
哪些数字被改了、摘要动没动。

这里**不做**行级 diff（论文正文的换行/断词在 PDF 抽取后毫无意义，
行级 diff 会产出几千行噪声）；做的是"段落级匹配 + 差异归类"。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.dedupe.fingerprint import (
    PARA_HAMMING_MAX,
    best_match_ratio,
    hamming,
    normalize,
    paragraph_hashes,
    section_titles,
    split_paragraphs,
    tokens,
)

_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s*%|\s*倍|\s*个百分点)?")


def _chunk_paras(chunks: list[Any]) -> list[str]:
    """把 chunk 还原成段落序列（chunk 本身就是"一小段连贯论述"，直接用）。"""
    paras: list[str] = []
    for c in chunks:
        text = normalize(getattr(c, "text", "") or "")
        if len(text) < 40:
            continue
        paras.extend(split_paragraphs(text))
    return paras


def _numbers(text: str) -> list[str]:
    """只保留"有信息量"的数字：带单位、带小数、或 ≥3 位。年份单独处理。"""
    out = []
    for m in _NUM_RE.finditer(text or ""):
        raw, unit = m.group(1), (m.group(2) or "").strip()
        digits = raw.replace(",", "")
        if unit:
            out.append(f"{digits}{unit}")
        elif "." in digits or len(digits) >= 3:
            if len(digits) == 4 and digits.startswith(("19", "20")):
                continue  # 年份
            out.append(digits)
    return out


def _snippet(text: str, n: int = 90) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n] + ("…" if len(t) > n else "")


def _match_pairs(base: list[str], target: list[str]) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """一对一贪心匹配，返回 (配上的 (base_i, target_i), 未配上的 base 下标, 未配上的 target 下标)。"""
    hb = paragraph_hashes(base)
    ht = paragraph_hashes(target)
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for i, h in enumerate(hb):
        best_j, best_d = -1, 65
        for j, h2 in enumerate(ht):
            if j in used:
                continue
            d = hamming(h, h2)
            if d < best_d:
                best_j, best_d = j, d
        if best_j >= 0 and best_d <= PARA_HAMMING_MAX:
            used.add(best_j)
            pairs.append((i, best_j))
    matched_base = {i for i, _ in pairs}
    matched_target = {j for _, j in pairs}
    return (
        pairs,
        [i for i in range(len(base)) if i not in matched_base],
        [j for j in range(len(target)) if j not in matched_target],
    )


def diff_versions(
    *,
    from_label: str,
    from_chunks: list[Any],
    from_pages: int | None,
    to_label: str,
    to_chunks: list[Any],
    to_pages: int | None,
    from_sections: list[str] | None = None,
    to_sections: list[str] | None = None,
    limit_examples: int = 4,
) -> dict[str, Any]:
    """
    生成差异摘要。`from` 是被对照的旧版，`to` 是新版（按版本号排序后传入，
    调用方负责排序，这里不猜谁新谁旧）。

    `from_sections` / `to_sections` 可显式传入章节标题：本次上传的 PDF 有
    `Fingerprint.sections`（按字号识别），而库内版本只有 chunk 的 section_path，
    两侧粒度并不总是等价——见下面的 comparability 判定。
    """
    base = _chunk_paras(from_chunks)
    target = _chunk_paras(to_chunks)
    pairs, base_only, target_only = _match_pairs(base, target)

    sec_a = list(from_sections) if from_sections else section_titles(
        [getattr(c, "section_path", None) for c in from_chunks]
    )
    sec_b = list(to_sections) if to_sections else section_titles(
        [getattr(c, "section_path", None) for c in to_chunks]
    )
    sec_a = section_titles(sec_a)
    sec_b = section_titles(sec_b)
    low_a = {s.lower() for s in sec_a}
    low_b = {s.lower() for s in sec_b}
    added = [s for s in sec_b if s.lower() not in low_a]
    removed = [s for s in sec_a if s.lower() not in low_b]

    # 章节可比性：两侧一边是"按字号识别出的标题"，一边是"chunk 归到的顶层段落"，
    # 粒度可以差一倍。这时如果还按集合差集去报"新增 6 个章节"，读起来就是
    # 一句话都不对——宁可说"这次不判"，也不要给一个看起来像结论的错结论。
    sections_comparable = True
    sections_note = ""
    if len(sec_a) >= 3 and len(sec_b) >= 3:
        if len(added) >= 0.6 * len(sec_b) or len(removed) >= 0.6 * len(sec_a):
            sections_comparable = False
            sections_note = (
                f"两侧章节识别粒度不同（本次 {len(sec_a)} 节 / 库内 {len(sec_b)} 节），"
                "章节增删不作判断"
            )

    # 段落内数字变化：只报"同一段里数字变了"的，避免整篇数字对不上时的噪声
    number_changes: list[dict[str, str]] = []
    for i, j in pairs:
        na, nb = _numbers(base[i]), _numbers(target[j])
        if not na or not nb:
            continue
        set_a, set_b = set(na), set(nb)
        gone = [x for x in na if x not in set_b]
        new = [x for x in nb if x not in set_a]
        if gone and new and len(gone) <= 4 and len(new) <= 4:
            number_changes.append(
                {
                    "from": ", ".join(dict.fromkeys(gone)),
                    "to": ", ".join(dict.fromkeys(new)),
                    "context": _snippet(target[j]),
                }
            )
        if len(number_changes) >= limit_examples:
            break

    chars_a = sum(len(x) for x in base)
    chars_b = sum(len(x) for x in target)
    matched_ratio = len(pairs) / max(1, len(target)) if target else 0.0

    highlights: list[str] = []
    if target_only:
        highlights.append(f"新增 {len(target_only)} 段（占新版 {len(target_only) / max(1, len(target)):.0%}）")
    if base_only:
        highlights.append(f"删去 {len(base_only)} 段（旧版有、新版没有）")
    if sections_comparable and added:
        highlights.append("新增章节：" + "、".join(added[:3]))
    if sections_comparable and removed:
        highlights.append("删去章节：" + "、".join(removed[:3]))
    if number_changes:
        highlights.append(f"{len(number_changes)} 处数字被修订")
    if not highlights:
        highlights.append("段落与章节几乎完全一致，差异在版式层面")

    return {
        "from": {
            "label": from_label,
            "pages": from_pages,
            "paragraphs": len(base),
            "chars": chars_a,
            "sections": sec_a,
            "chunks": len(from_chunks),
        },
        "to": {
            "label": to_label,
            "pages": to_pages,
            "paragraphs": len(target),
            "chars": chars_b,
            "sections": sec_b,
            "chunks": len(to_chunks),
        },
        "paragraphs": {
            "matched": len(pairs),
            "added": len(target_only),
            "removed": len(base_only),
            "matched_ratio": round(matched_ratio, 4),
        },
        "sections": {
            "added": added if sections_comparable else [],
            "removed": removed if sections_comparable else [],
            "kept": len(low_a & low_b),
            "comparable": sections_comparable,
            "note": sections_note,
        },
        "numbers": number_changes,
        "delta_chars": chars_b - chars_a,
        "highlights": highlights,
        "examples": {
            "added": [_snippet(target[j]) for j in target_only[:limit_examples]],
            "removed": [_snippet(base[i]) for i in base_only[:limit_examples]],
        },
    }


def render_diff_markdown(diff: dict[str, Any]) -> str:
    """把差异摘要渲染成一段 Markdown，供导出与文档引用。"""
    a, b = diff["from"], diff["to"]
    lines = [
        f"### 版本差异：{a['label']} → {b['label']}",
        "",
        f"- 页数：{a['pages']} → {b['pages']}",
        f"- 段落：{a['paragraphs']} → {b['paragraphs']}"
        f"（配对 {diff['paragraphs']['matched']}，新增 {diff['paragraphs']['added']}，删去 {diff['paragraphs']['removed']}）",
        f"- 正文字符：{a['chars']} → {b['chars']}（{diff['delta_chars']:+d}）",
        f"- 章节：保留 {diff['sections']['kept']}，新增 {len(diff['sections']['added'])}，删去 {len(diff['sections']['removed'])}",
    ]
    if diff["sections"]["added"]:
        lines.append(f"  - 新增：{'、'.join(diff['sections']['added'][:5])}")
    if diff["sections"]["removed"]:
        lines.append(f"  - 删去：{'、'.join(diff['sections']['removed'][:5])}")
    if diff["numbers"]:
        lines.append("")
        lines.append("| 旧值 | 新值 | 所在段落 |")
        lines.append("|---|---|---|")
        for n in diff["numbers"]:
            lines.append(f"| {n['from']} | {n['to']} | {n['context'][:60]} |")
    return "\n".join(lines)


def diff_from_texts(
    *,
    from_label: str,
    from_text: str,
    to_label: str,
    to_text: str,
    from_pages: int | None = None,
    to_pages: int | None = None,
) -> dict[str, Any]:
    """没有 chunk 时（例如上传前预检）用纯文本算差异。"""

    class _P:
        def __init__(self, text: str):
            self.text = text
            self.section_path = None

    fp = [_P(p) for p in split_paragraphs(from_text)]
    tp = [_P(p) for p in split_paragraphs(to_text)]
    return diff_versions(
        from_label=from_label,
        from_chunks=fp,
        from_pages=from_pages,
        to_label=to_label,
        to_chunks=tp,
        to_pages=to_pages,
    )
