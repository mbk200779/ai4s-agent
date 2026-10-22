"""
写作辅助接口：基于文献证据生成论文大纲。
"""
from fastapi import APIRouter
from app.schemas.writing import OutlineRequest, OutlineResponse
from app.services.writing_service import WritingService

router = APIRouter(prefix="/writing", tags=["写作辅助"])


@router.post("/outline", response_model=OutlineResponse)
async def generate_outline(req: OutlineRequest):
    """
    验收标准：
    1. 按主题检索相关片段；
    2. 调用 LLM 生成结构化论文大纲；
    3. 大纲关键节点附带可溯源引用。
    """
    service = WritingService()
    outline = service.generate_outline(req.topic, top_k=req.top_k)
    return OutlineResponse(topic=req.topic, outline=outline)
