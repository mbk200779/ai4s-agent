"""
可溯源问答接口：基于检索片段生成答案，强制引用、校验、无支撑拒答，并写 provenance 日志。

03 版新增：返回 `routing`（查询理解 + 检索分流 trace），
调用方可以传 `force_type` 复现某条策略的检索结果。

05 版新增：引用精确到句。响应里多了两类东西——
- `answer_refs`：答案里第 k 个角标 → 哪个 chunk 的哪一句（含 rects，可直接画框）；
- `anchor_index`：chunk_id → 该片段全部锚点，阅读器要拿它做同页多处引用的排队高亮。
"""
from fastapi import APIRouter

from app.schemas.qa import Anchor, AnswerRef, Citation, QARequest, QAResponse
from app.services.qa_service import QAService

router = APIRouter(prefix="/qa", tags=["问答"])


@router.post("/ask", response_model=QAResponse)
async def ask(req: QARequest):
    """
    验收标准：
    1. 每句答案带 [引#片段ID]；
    2. 引用 ID 不存在则替换为 [无依据] 并返回 invalid_citations；
    3. 检索无支撑或置信度过低时拒答而非编造；
    4. provenance 日志写入 MinIO，含 paper_id/page/bbox/chunk_id/原文/分数/分流 trace；
    5. 响应里的 routing 说明"这条问题被判成什么类型、走了哪条策略"；
    6. 每个角标都能落到 chunk 内的具体一句（ref_id + rects），落不到时 has_anchor=false，
       前端据此提示"该文档未建立精细锚点"，而不是整页高亮。
    """
    service = QAService()
    result = service.ask(
        req.question,
        top_k=req.top_k,
        rrf_k=req.rrf_k,
        force_type=req.force_type,
        rerank=req.rerank,
        numeric=req.numeric,
        project_id=req.project_id,
        index_version=req.index_version,
    )

    refs = result.get("answer_refs") or []
    # 同一 chunk 可能被引用多次，取第一次的锚点作为"这个片段的代表句"
    first_ref: dict[str, AnswerRef] = {}
    for ref in refs:
        first_ref.setdefault(ref["chunk_id"], AnswerRef(**ref))

    def _anchor_of(hit: AnswerRef) -> Anchor | None:
        """
        由 answer_ref 的字段还原 Anchor。

        不能再从 `hit.anchor` 取：AnswerRef 是给前端消费的扁平结构，
        它不认识 qa_service 内部的 `anchor` 子对象（pydantic 会把它当多余字段丢掉）。
        这里按字段重建，顺带保证"角标拿到的锚点"和"answer_refs 里的锚点"逐字一致。
        """
        if not hit or not hit.ref_id or not hit.rects:
            return None
        return Anchor(
            ref_id=hit.ref_id,
            kind="cell" if hit.precision == "cell" else "sentence",
            precision=hit.precision or "sentence",
            start=hit.start,
            end=hit.end,
            rects=[[float(v) for v in r] for r in hit.rects],
            bbox=hit.bbox,
        )

    citations = []
    for s in result.get("sources", []):
        hit = first_ref.get(s["chunk_id"])
        citations.append(
            Citation(
                paper_id=s["paper_id"],
                arxiv_id=s["arxiv_id"],
                title=s["title"],
                chunk_id=s["chunk_id"],
                chunk_text=s["text"],
                page=s["page"],
                paragraph=s.get("paragraph", 0),
                bbox=s.get("bbox"),
                score=s["score"],
                rerank_score=s.get("rerank_score"),
                hit_source=s.get("hit_source"),
                snippet=s.get("snippet"),
                ref_id=(hit.ref_id if hit else None),
                anchor=_anchor_of(hit) if hit else None,
                precision=(hit.precision if hit else None),
                has_anchors=s["chunk_id"] in (result.get("anchor_index") or {}),
            )
        )

    return QAResponse(
        answer=result["answer"],
        rejected=result["rejected"],
        reject_reason=result.get("reject_reason"),
        citations=citations,
        invalid_citations=result.get("invalid_citations", []),
        provenance_key=result.get("provenance_key"),
        routing=result.get("routing"),
        answer_refs=[AnswerRef(**r) for r in refs],
        anchor_index=result.get("anchor_index") or {},
        claims=result.get("claims") or [],
        claim_stats=result.get("claim_stats") or {},
        confidence=result.get("confidence"),
        uncertainty=result.get("uncertainty"),
    )
