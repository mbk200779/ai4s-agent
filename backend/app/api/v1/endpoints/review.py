"""
综述生成接口：基于检索证据自动生成可验证的学术综述，支持 Markdown 导出。
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from app.schemas.review import ReviewRequest, ReviewResponse, ReviewExportRequest
from app.services.review import export as review_export
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

    文件名走 RFC 5987 双写（ASCII 兜底 + `filename*=UTF-8''`）：HTTP 头只能装
    latin-1，中文主题直接写进 `filename=` 会在编码响应头时抛 UnicodeEncodeError，
    整个请求变 500 —— 这是导出「点了没反应」的真实原因。
    """
    if not (req.markdown or "").strip():
        raise HTTPException(status_code=400, detail="markdown 不能为空")

    text = review_export.build_export(
        req.topic, req.markdown, [r.model_dump() for r in req.refs]
    )
    return Response(
        content=review_export.encode(text),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": review_export.content_disposition(req.topic),
            # 网关/浏览器按扩展名以外的方式缓存时，别把这篇的稿子发给下一篇
            "Cache-Control": "no-store",
        },
    )


@router.get("/download/{key}")
async def download_review(key: str):
    """通过 MinIO export_key 获取临时下载链接。"""
    service = ReviewService()
    url = service.get_export_url(key)
    if not url:
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return {"download_url": url}
