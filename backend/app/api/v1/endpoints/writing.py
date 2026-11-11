"""
写作辅助接口。

两个入口：
- `POST /writing/draft`  生成带句级溯源的初稿（09 的主路径）
- `POST /writing/outline` 只出大纲（老入口，前端历史页面与脚本还在用）

导出三个口：
- `POST /writing/export`       人读的 Markdown（含「引#N → [K]」映射表）
- `POST /writing/export/json`  机读的完整结构（含 chunk_id，供二次处理）
- `GET  /writing/download/{key}` 取 MinIO 临时链接
"""
import json
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.writing import (
    OutlineRequest,
    OutlineResponse,
    WritingExportPayloadRequest,
    WritingExportRequest,
    WritingRequest,
    WritingResponse,
)
from app.services.writing import WritingPipeline
from app.services.writing_service import WritingService

router = APIRouter(prefix="/writing", tags=["写作辅助"])

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _attachment(filename: str) -> dict[str, str]:
    """
    生成 `Content-Disposition`。

    中文主题直接塞 `filename=` 会 500：HTTP 头按 latin-1 编码，星例在
    `v.encode("latin-1")` 处抛 `UnicodeEncodeError`——用户看到的是
    「Internal Server Error」，前端不检查状态码就把这段错误文本当 `.md` 存下来。
    所以给两个名字：ASCII 兜底名走 `filename=`（老客户端认），
    真实名按 RFC 5987 百分号编码走 `filename*=`（现代浏览器优先用这个）。
    """
    stem, dot, ext = filename.rpartition(".")
    if not dot:  # 没有扩展名
        stem, ext = filename, ""
    ascii_stem = _UNSAFE_NAME_RE.sub("_", stem).strip("._-") or "draft"
    ascii_name = f"{ascii_stem}.{ext}" if ext else ascii_stem
    return {
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
        )
    }


@router.post("/draft", response_model=WritingResponse)
async def generate_draft(req: WritingRequest):
    """
    验收标准：
    1. 每句带 `type`（claim/bridge）与 `cite_nos`/`chunk_ids`；
    2. 过渡句标 `bridge`（无需引用），伪装成 bridge 的结论句会被改判并删；
    3. References 只由库内 `papers` 表生成，正文里的库外 arXiv/DOI 会被删掉；
    4. 无证据段落返回 `placeholder + gap`，不产出编造正文；
    5. 响应体带 `ref_map`，导出 md/json 都保留映射。
    """
    pipeline = WritingPipeline()
    result = pipeline.generate(req.topic, n_sections=req.n_sections, arxiv_ids=req.arxiv_ids)
    return WritingResponse(**result)


@router.post("/export")
async def export_markdown(req: WritingExportRequest):
    if not req.markdown:
        raise HTTPException(status_code=400, detail="markdown 不能为空")
    filename = f"{req.topic.replace(' ', '_')}.md"
    return StreamingResponse(
        iter([req.markdown.encode("utf-8")]),
        media_type="text/markdown",
        headers=_attachment(filename),
    )


@router.post("/export/json")
async def export_json(req: WritingExportPayloadRequest):
    """
    机读导出。前端把整份响应体（sections/sentences/references/ref_map）回传，
    服务端原样落盘——**不做字段裁剪**，否则"导出后映射就断了"。
    """
    if not req.payload:
        raise HTTPException(status_code=400, detail="payload 不能为空")
    body = json.dumps(req.payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"{req.topic.replace(' ', '_')}.trace.json"
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers=_attachment(filename),
    )


@router.get("/download/{key:path}")
async def download(key: str):
    service = WritingService()
    url = service.get_export_url(key)
    if not url:
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return {"download_url": url}


@router.post("/outline", response_model=OutlineResponse)
async def generate_outline(req: OutlineRequest):
    service = WritingService()
    outline, papers = service.generate_outline(req.topic)
    return OutlineResponse(topic=req.topic, outline=outline, papers=papers)
