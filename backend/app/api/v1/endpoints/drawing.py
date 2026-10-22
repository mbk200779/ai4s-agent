"""
绘图辅助接口：生成 matplotlib / mermaid 代码并渲染，返回 PNG/SVG 下载链接。
"""
from fastapi import APIRouter
from app.schemas.drawing import DrawingRequest, DrawingResponse
from app.services.drawing_service import DrawingService

router = APIRouter(prefix="/drawing", tags=["绘图辅助"])


@router.post("/generate", response_model=DrawingResponse)
async def generate_drawing(req: DrawingRequest):
    """
    验收标准：
    1. 同输入返回同一份缓存图片（cache_key 一致）；
    2. 图表代码/图片中携带数据来源标注；
    3. 同时返回 PNG 与 SVG 下载链接。
    """
    service = DrawingService()
    return service.generate(req)
