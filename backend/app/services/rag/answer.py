"""
结构化生成协议的服务端一半：把「一段自由文本答案」变成「可校验的 claim + evidence」。

为什么这件事必须在服务端做，而不是让前端解析 `[引#3]`：
1. 前端拿不到 chunk 原文，也就无法判断某个角标指向的句子到底支不支持那句话；
2. "证据不足" 是**结论级**信息（哪句没被支撑、为什么），前端只能靠猜；
3. token 流里一旦漏出未校验的角标，用户就会点到一个可能指错的死链——
   这是协议层的错误，不能靠 UI 兜。

因此协议把答案拆成两半：
- **正文**（token 流）：只有自然语言，**不含任何角标**；
- **主张**（claim 事件）：每句一份，带 span（在正文里的字符区间）与 evidence 列表。

前端拿到 token 先把字打出来，claim 到达后再按 span 把角标"回填"到句末。
角标在正文里出现得比 claim 晚，但永远不会指向未校验的位置。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from app.services import anchors as anchor_lib
from app.services.rag import anchor_select, confidence as confidence_lib
from app.services.rag import semantic as semantic_lib

CITE_RE = re.compile(r"\[引#(\d+)\]")

# 证据成立的分数线：0.15 ≈ "claim 里至少有一小半词出现在原句，或数值对上了"。
# 低于它的匹配基本是停用词偶然重合，标 supported 会让"可信"这个词失去意义。
MIN_EVIDENCE_SCORE = 0.15

# 一条 claim 最多挂几条证据：再多读者也不会看，前端角标会挤成一排
MAX_EVIDENCE_PER_CLAIM = 4

# claim 句子切分上限。与 anchors.split_sentences 的默认值不同：
# 这里切的是**答案**，句子比论文正文短，切太碎会让角标满天飞。
MAX_CLAIM_LEN = 320


class CitationFilter:
    """
    流式过滤器：从 token 流里摘掉 `[引#N]`，并记下它在**清洗后文本**里的位置。

    约束是"token 流中不得出现未经校验的角标"，而校验必须等整段答案生成完
    （要拿 claim 句去和原文句做匹配，还得知道这句话什么时候结束）。也就是说，
    标记在流里出现的时候，它一定是未经校验的——唯一合规的做法是摘掉它，
    等 claim 事件带着 evidence 回来时再由前端按位置回填。

    实现上唯一的坑是标记可能被切在两块里到达（`...结论[引` + `#3]。`）。
    所以必须兜住"看起来像标记前缀"的尾巴：
    - `[` 开头的尾巴，若后续只由 `引#数字]` 组成且未闭合 → 扣住等下一块；
    - 普通方括号（`[1]`、`[a]`）不匹配这个形状，照常吐出，不影响正文。

    位置口径：`marks[i]["pos"]` 是"标记被删掉之后，它本该插入的那个下标"。
    同一个位置可能出现多条（一句话挂两个角标），顺序按到达顺序保留。
    """

    # 只认这三种形态：`[`、`[引`、`[引#12`（含未闭合的 `[引#`）
    _PREFIX_RE = re.compile(r"^\[(?:引(?:#\d*)?)?$")

    def __init__(self) -> None:
        self._buf = ""
        self.text = ""
        self.marks: list[dict[str, Any]] = []
        self.raw_len = 0

    def feed(self, delta: str) -> str:
        """吃一块增量，吐一块可安全上屏的文本（可能为空串）。"""
        if not delta:
            return ""
        self.raw_len += len(delta)
        self._buf += delta
        out: list[str] = []
        i = 0
        n = len(self._buf)
        while i < n:
            if self._buf[i] == "[":
                m = CITE_RE.match(self._buf, i)
                if m:
                    self.marks.append(
                        {"pos": len(self.text) + len(out), "index": int(m.group(1))}
                    )
                    i = m.end()
                    continue
                tail = self._buf[i:]
                if self._PREFIX_RE.match(tail) and len(tail) <= 12:
                    break  # 可能是被切断的标记，扣住等下一块
            out.append(self._buf[i])
            i += 1
        self._buf = self._buf[i:]
        chunk = "".join(out)
        self.text += chunk
        return chunk

    def flush(self) -> str:
        """
        收尾：把缓冲里剩下的东西吐出去。

        残余若仍是"标记前缀"形状（答案在 `[引#3` 处断流），直接丢弃——
        吐出去等于在流末尾留一个半截角标，比少几个字更糟。
        """
        rest = self._buf
        self._buf = ""
        if not rest:
            return ""
        if self._PREFIX_RE.match(rest):
            return ""
        self.text += rest
        return rest


def split_claims(text: str) -> list[tuple[int, int]]:
    """
    把正文切成 claim 区间。

    直接复用锚点模块的切句器：它已经处理了 `et al.`、`Fig. 3`、`0.98` 这些
    会让朴素正则切碎的情况。两处用同一套切句规则还有一个好处——
    前端把 claim span 当作角标插入点时，不会出现"句子边界和角标位置打架"。
    """
    if not text:
        return []
    return anchor_lib.split_sentences(text, max_len=MAX_CLAIM_LEN)


def _evidence(
    index: int,
    claim_text: str,
    sources: Sequence[Any],
    anchors_map: dict[str, Any],
    used: dict[str, list[str]],
    min_score: float,
) -> dict[str, Any]:
    """单条引用的校验结果。索引越界也算一条 evidence——保留下来才能让用户看见"这里出了问题"。"""
    if not (1 <= index <= len(sources)):
        return {
            "index": index,
            "status": "unsupported",
            "reason": f"引用编号越界（共 {len(sources)} 条片段）",
            "chunk_id": None,
            "arxiv_id": None,
            "title": None,
            "page": 0,
            "kind": None,
            "ref_id": None,
            "precision": None,
            "start": 0,
            "end": 0,
            "text": None,
            "rects": None,
            "bbox": None,
            "match_score": None,
        }

    src = sources[index - 1]
    doc = anchors_map.get(src.chunk_id)
    picked = None
    if doc and (doc.get("items") or []):
        picked = anchor_select.select(claim_text, src.text, doc, used=used.get(src.chunk_id, []))
        if picked:
            used.setdefault(src.chunk_id, []).append(picked["ref_id"])
    if picked is None and src.text:
        # 该片段没有句级锚点：退到"现场切句"的块级匹配。
        # 这一步不是锦上添花——没有它，"没建锚点"会被判成"原文里没有"，
        # 而这两件事在 07 的三档里分别是 needs_check 与 unsupported。
        picked = anchor_select.select_in_text(claim_text, src.text)

    score = float(picked.get("match_score") or 0.0) if picked else 0.0
    supported = bool(picked) and score >= min_score
    # 专有名词的"锚定"按整段算，不按这一句算：
    # 证据句常常是中段的一句话，不会重复模型名（"We mask 15% of tokens…" 里没有 BERT），
    # 按句判会把正确的证据全打成"换了对象"。按整段判仍然能抓出 TPU/GPU 这种真错配。
    chunk_lower = (src.text or "").lower()
    return {
        "index": index,
        "status": "supported" if supported else "unsupported",
        "entity_present": [
            t for t in confidence_lib.caps_terms(claim_text) if t.lower() in chunk_lower
        ],
        "reason": None if supported else (
            "该片段未建立句级锚点，且原文里找不到相近的句子" if not picked else
            f"证据句匹配度 {score:.2f} 低于阈值 {min_score:.2f}"
        ),
        "chunk_id": src.chunk_id,
        "arxiv_id": src.arxiv_id,
        "title": src.title,
        "page": src.page,
        "kind": src.kind,
        "ref_id": (picked or {}).get("ref_id"),
        "precision": (picked or {}).get("precision"),
        "start": int((picked or {}).get("start") or 0),
        "end": int((picked or {}).get("end") or 0),
        "text": (picked or {}).get("text"),
        "rects": (picked or {}).get("rects"),
        # 没有句级锚点时退回块级 bbox：定位仍然可用，只是粗
        "bbox": (picked or {}).get("bbox") or src.bbox,
        "match_score": round(score, 4) if picked else None,
        # 跨语言语义支持度（0~1），由 build_answer 第二遍批量填入；None = 未启用/失败
        "sem_score": None,
    }


def build_answer(
    text: str,
    marks: Sequence[dict[str, Any]],
    sources: Sequence[Any],
    anchors_map: dict[str, Any] | None = None,
    min_score: float = MIN_EVIDENCE_SCORE,
    *,
    query_type: str | None = None,
    top_raw_score: float | None = None,
    reject_threshold: float | None = None,
    retrieval_weak: bool = False,
) -> dict[str, Any]:
    """
    把「清洗后的正文 + 角标位置」组装成 claims / refs / 带标注文本 / 整体置信。

    `text` 与 `marks` 必须来自同一个 CitationFilter 实例——如果正文被别的东西
    改写过（截断、替换），span 会整体错位，前端就会把角标插到字中间。

    分档（07）在这里做而不是在出口处做：定档要用的信号（证据句重合度、
    锚点精度、片段类型、跨几篇论文）**只有这一层同时持有**。放到 API 层就得
    再查一次锚点、再比一次分，两处口径迟早会分叉。
    """
    anchors_map = anchors_map or {}
    th = confidence_lib.thresholds()
    # 整条回答覆盖几篇论文：single_paper 判据要用它，必须在逐句定档之前算好
    answer_papers = len({getattr(s, "arxiv_id", None) for s in sources} - {None})
    used: dict[str, list[str]] = {}
    claims: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []

    spans = [(s, e) for (s, e) in split_claims(text) if text[s:e].strip()]
    # 角标归属：按"最后一个起点 ≤ 角标位置"的句子来判，而不是区间包含。
    # 原因是切句器会把句末空白裁掉（span.end 落在句号后），而标记的位置通常在
    # 空白之后（`... 0.98 [引#1]`），用闭区间判会**一条都挂不上**——
    # 表现为"每个 claim 都 unsupported：这句话没有引用标注"，而 token 里的标记
    # 明明已经被摘掉记下来了。这个错很安静，只能在事件日志里看出来，所以在这里收口。
    buckets: list[list[dict[str, Any]]] = [[] for _ in spans]
    for m in marks:
        pos = int(m["pos"])
        owner = 0
        for i, (s, _e) in enumerate(spans):
            if s <= pos:
                owner = i
            else:
                break
        if buckets:
            buckets[owner].append(m)

    # ---- 第一遍：选证据。此时还不定档，因为要先把所有 (claim, 证据句) 收齐 ----
    staged: list[dict[str, Any]] = []
    for n, ((s, e), inside) in enumerate(zip(spans, buckets), 1):
        claim_text = text[s:e].strip()
        if not claim_text:
            continue
        evidence = [
            _evidence(int(m["index"]), claim_text, sources, anchors_map, used, min_score)
            for m in inside
        ][:MAX_EVIDENCE_PER_CLAIM]
        staged.append({"n": n, "s": s, "e": e, "text": claim_text,
                       "inside": inside, "evidence": evidence})

    # ---- 第二遍：跨语言语义核验（一次批量 embed，不是逐条）----
    # 逐条算的话每次都是一次前向，3~6 条就是 3~6 倍的固定开销；
    # 批量只多几十毫秒。这一层的存在理由见 services/rag/semantic.py 顶部。
    pairs: list[tuple[str, str]] = []
    where: list[tuple[int, int]] = []
    for ci, item in enumerate(staged):
        for ei, ev in enumerate(item["evidence"]):
            if ev.get("text"):
                pairs.append((item["text"], ev["text"]))
                where.append((ci, ei))
    if pairs:
        scores = semantic_lib.support_scores(pairs)
        for (ci, ei), sc in zip(where, scores):
            staged[ci]["evidence"][ei]["sem_score"] = sc

    # ---- 第三遍：定档 + 组装 ----
    for item in staged:
        n, s, e, claim_text = item["n"], item["s"], item["e"], item["text"]
        inside, evidence = item["inside"], item["evidence"]

        graded = confidence_lib.grade_claim(
            claim_text, evidence,
            query_type=query_type, retrieval_weak=retrieval_weak,
            top_raw_score=top_raw_score, reject_threshold=reject_threshold,
            answer_papers=answer_papers, th=th,
        )
        supported = any(ev["status"] == "supported" for ev in evidence)
        if not inside:
            status, reason = "unsupported", "这句话没有引用标注"
        elif supported:
            status, reason = "supported", None
        else:
            status, reason = "unsupported", "引用的片段里找不到能支撑这句话的句子"

        claim = {
            "claim_id": f"c{n}",
            "span": [s, e],
            "text": claim_text,
            # status 是 05/06 的证据级判定（有据/无据），level 是 07 的三档结论。
            # 两个都留着：status 表示"校验过没过"，level 表示"这句话能信到什么程度"，
            # 它们不是同一件事（有据但需核对 = supported + needs_check）。
            "status": status,
            "level": graded["level"],
            "level_label": graded["label"],
            "reason": reason,
            "missing": graded["missing"],
            "confidence": graded,
            "citations": evidence,
        }
        claims.append(claim)

        for ev in evidence:
            refs.append(
                {
                    "ordinal": len(refs),
                    "index": ev["index"],
                    "claim_id": claim["claim_id"],
                    "span": [s, e],
                    "chunk_id": ev["chunk_id"],
                    "arxiv_id": ev["arxiv_id"],
                    "page": ev["page"],
                    "ref_id": ev["ref_id"],
                    "text": ev["text"],
                    "start": ev.get("start", 0),
                    "end": ev.get("end", 0),
                    "bbox": ev["bbox"],
                    "rects": ev["rects"],
                    "precision": ev["precision"],
                    "claim": claim_text,
                    "match_score": ev["match_score"],
                    "status": ev["status"],
                    "level": graded["level"],
                    "has_anchor": bool(ev["ref_id"]),
                }
            )

    overall = confidence_lib.grade_answer(
        claims, sources=sources, query_type=query_type,
        top_raw_score=top_raw_score, reject_threshold=reject_threshold,
        retrieval_weak=retrieval_weak, rejected=False,
    )
    return {
        "claims": claims,
        "refs": refs,
        "annotated": annotate(text, marks, sources),
        # 只有 unsupported 的句子留在正文里，前端要显式标出来而不是藏起来
        "unsupported_claims": [c["claim_id"] for c in claims if c["status"] == "unsupported"],
        "confidence": overall,
        "claim_stats": confidence_lib.summarize(claims),
    }


def annotate(
    text: str, marks: Sequence[dict[str, Any]], sources: Sequence[Any]
) -> str:
    """
    生成"带完整出处"的文本：`[引#3: Attention Is All You Need p.7]`。

    用途是复制/导出/provenance 归档。界面上不显示这个长形态（太吵），
    但归档里必须是它——只存 `[引#3]` 的话，换个版本、换批片段就再也对不上了。
    """
    out: list[str] = []
    last = 0
    for m in sorted(marks, key=lambda x: int(x["pos"])):
        pos = max(last, min(int(m["pos"]), len(text)))
        out.append(text[last:pos])
        idx = int(m["index"])
        if 1 <= idx <= len(sources):
            src = sources[idx - 1]
            out.append(f"[引#{idx}: {src.title} p.{src.page}]")
        else:
            out.append("[无依据]")
        last = pos
    out.append(text[last:])
    return "".join(out)


def summarize(claims: Iterable[dict[str, Any]]) -> dict[str, int]:
    """给日志与前端角标用的计数（07 起多出三档的分别计数）。"""
    return confidence_lib.summarize(claims)
