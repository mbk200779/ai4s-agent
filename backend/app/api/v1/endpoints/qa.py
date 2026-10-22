"""
可溯源问答接口：基于检索片段生成答案，强制引用、校验、无支撑拒答，并写 provenance 日志。
"""
from fastapi import APIRouter
from app.schemas.qa import QARequest, QAResponse, Citation
from app.services.qa_service import QAService

router = APIRouter(prefix="/qa", tags=["问答"])


@router.post("/ask", response_model=QAResponse)
async def ask(req: QARequest):
    """
    验收标准：
    1. 每句答案带 [引#片段ID]；
    2. 引用 ID 不存在则替换为 [无依据] 并返回 invalid_citations；
    3. 检索无支撑或置信度过低时拒答而非编造；
    4. provenance 日志写入 MinIO，含 paper_id/page/bbox/chunk_id/原文/分数。
    """
    service = QAService()
    result = service.ask(req.question, top_k=req.top_k, rrf_k=req.rrf_k)

    citations = [
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
        )
        for s in result.get("sources", [])
    ]

    return QAResponse(
        answer=result["answer"],
        rejected=result["rejected"],
        reject_reason=result.get("reject_reason"),
        citations=citations,
        invalid_citations=result.get("invalid_citations", []),
        provenance_key=result.get("provenance_key"),
    )
