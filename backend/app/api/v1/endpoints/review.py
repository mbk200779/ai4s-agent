"""
综述生成接口：基于检索证据自动生成可验证的学术综述，支持 Markdown 导出。
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.schemas.review import ReviewRequest, ReviewResponse, ReviewExportRequest
from app.services.review_service import ReviewService

router = APIRouter(prefix="/review", tags=["综述"])


@router.post("/generate", response_model=ReviewResponse)
async def generate_review(req: ReviewRequest):
    """
    验收标准：
    1. 按主题或指定论文列表检索片段；
    2. 调用带固定 prompt 版本的 LLM 生成 markdown 综述；
    3. 每句带 [引#N]，无效引用替换为 [无依据]；
    4. 返回 Markdown 与导出 key。
    """
    service = ReviewService()
    result = service.generate(req.topic, arxiv_ids=req.arxiv_ids, top_k=req.top_k)
    return ReviewResponse(**result)


@router.post("/export")
async def export_review(req: ReviewExportRequest):
    """
    导出综述 Markdown：直接返回文件流，Content-Disposition 触发浏览器下载。
    """
    service = ReviewService()
    if not req.markdown:
        raise HTTPException(status_code=400, detail="markdown 不能为空")

    filename = f"{req.topic.replace(' ', '_')}.md"
    return StreamingResponse(
        iter([req.markdown.encode("utf-8")]),
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/download/{key}")
async def download_review(key: str):
    """通过 MinIO export_key 获取临时下载链接。"""
    service = ReviewService()
    url = service.get_export_url(key)
    if not url:
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return {"download_url": url}
