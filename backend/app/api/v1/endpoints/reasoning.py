"""
跨文献推理接口：多跳问答、跨文献对比、矛盾发现。
"""
from fastapi import APIRouter
from app.schemas.reasoning import (
    CrossPaperReasoningRequest,
    CrossPaperReasoningResponse,
    ComparisonRequest,
    ComparisonResponse,
)
from app.services.reasoning_service import ReasoningService

router = APIRouter(prefix="/reason", tags=["跨文献推理"])


@router.post("/ask", response_model=CrossPaperReasoningResponse)
async def multi_hop_ask(req: CrossPaperReasoningRequest):
    """
    多跳跨文献问答。
    验收标准：
    1. 能跨≥3 篇文献综合回答；
    2. 推理链每步 grounding 到原文片段；
    3. 发现的矛盾显式标出。
    """
    service = ReasoningService()
    result = service.multi_hop_reason(
        req.query, max_hops=req.max_hops, top_k_per_hop=req.top_k_per_hop
    )
    return CrossPaperReasoningResponse(
        query=req.query,
        answer=result["answer"],
        rejected=result["rejected"],
        reject_reason=result.get("reject_reason"),
        reasoning_chain=result.get("reasoning_chain", []),
        contradictions=result.get("contradictions", []),
        sources=result.get("sources", []),
        papers_involved=result.get("papers_involved", []),
        provenance_key=result.get("provenance_key"),
    )


@router.post("/compare", response_model=ComparisonResponse)
async def compare_papers(req: ComparisonRequest):
    """跨文献对比。"""
    service = ReasoningService()
    result = service.compare_papers(req.topic, req.arxiv_ids, top_k=req.top_k)
    return ComparisonResponse(**result)


@router.post("/contradictions")
async def detect_contradictions(req: CrossPaperReasoningRequest):
    """发现与查询相关的文献矛盾点。"""
    service = ReasoningService()
    result = service.detect_contradictions(req.query, top_k=req.top_k_per_hop)
    return result
