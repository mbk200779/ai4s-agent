"""
绘图辅助接口：生成 matplotlib / mermaid 代码并渲染，返回 PNG/SVG 下载链接。
"""
from fastapi import APIRouter, HTTPException, Response
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


@router.get("/figure/{key:path}")
async def get_figure(key: str):
    """流式代理 figures bucket 对象。

    presigned URL 在容器内用 localhost 签名不可达，与 /papers/{id}/pdf 同理，
    改用后端流式代理，前端可直接 <img src> / <a download>。
    """
    service = DrawingService()
    try:
        data, content_type = service.storage.get_figure_bytes(key)
    except Exception:
        raise HTTPException(status_code=404, detail="图片不存在")
    media = content_type or "application/octet-stream"
    return Response(content=data, media_type=media)
