"""
论文相关接口：上传 PDF、arXiv 导入、列出论文、检索 chunk。
"""
import hashlib
from typing import List
from fastapi import APIRouter, UploadFile, File, HTTPException
from app.schemas.paper import (
    PaperMeta,
    PaperUploadResponse,
    PaperQueryRequest,
    PaperQueryResponse,
    RetrievedChunk,
)
from app.services.storage import StorageService
from app.services.vector_store import VectorStoreService
from app.services.retrieval_service import RetrievalService, RetrievalResult
from app.services.arxiv_client import ArxivClient
from app.tasks.tasks import parse_and_index_pdf

router = APIRouter(prefix="/papers", tags=["论文"])


def _normalize_arxiv_id(raw: str) -> str:
    """移除多余空格与版本号，保留纯 arxiv id。"""
    return raw.strip().split("v")[0]


@router.post("/upload", response_model=PaperUploadResponse)
async def upload_pdf(
    file: UploadFile = File(...),
    arxiv_id: str | None = None,
    version: str = "v1",
):
    """
    上传 PDF，后端异步解析并建索引。
    验收标准：
    1. 接收 multipart/form-data 的 PDF；
    2. 原稿写入 MinIO，chunk 向量与 paper_meta 写入 Qdrant；
    3. 派发 Celery 任务，HTTP 不阻塞解析。
    """
    content = await file.read()
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")

    body_hash = hashlib.sha256(content).hexdigest()
    # 若未提供 arxiv_id，使用 upload hash 作为占位 id
    arxiv_id = _normalize_arxiv_id(arxiv_id) if arxiv_id else f"upload_{body_hash[:16]}"
    s3_key = f"{arxiv_id}/{version}/paper.pdf"

    storage = StorageService()
    storage.upload_pdf(s3_key, content)

    task = parse_and_index_pdf.delay(
        arxiv_id=arxiv_id,
        version=version,
        title=file.filename or "",
        s3_key=s3_key,
    )
    return PaperUploadResponse(
        paper_id=f"{arxiv_id}::{version}",
        arxiv_id=arxiv_id,
        version=version,
        task_id=task.id,
    )


@router.post("/arxiv/import", response_model=PaperUploadResponse)
async def import_arxiv(arxiv_id: str):
    """
    通过 arXiv ID 拉取并索引论文。
    验收标准：
    1. 校验 arxiv_id 格式；
    2. 从 arXiv 下载 PDF 并上传 MinIO；
    3. 派发解析任务并返回 task_id。
    """
    arxiv_id = _normalize_arxiv_id(arxiv_id)
    client = ArxivClient()
    meta = client.fetch_by_id(arxiv_id)
    if not meta:
        raise HTTPException(status_code=404, detail="未找到该 arXiv 论文")

    pdf_bytes = client.download_pdf(meta["pdf_url"])
    s3_key = f"{meta['arxiv_id']}/{meta['version']}/paper.pdf"
    storage = StorageService()
    storage.upload_pdf(s3_key, pdf_bytes)

    task = parse_and_index_pdf.delay(
        arxiv_id=meta["arxiv_id"],
        version=meta["version"],
        title=meta["title"],
        s3_key=s3_key,
        authors=meta.get("authors", []),
        abstract=meta.get("abstract", ""),
        doi=meta.get("doi"),
    )
    return PaperUploadResponse(
        paper_id=f"{meta['arxiv_id']}::{meta['version']}",
        arxiv_id=meta["arxiv_id"],
        version=meta["version"],
        task_id=task.id,
    )


@router.get("/", response_model=List[dict])
async def list_papers():
    """列出已索引论文元数据（去重）。"""
    vs = VectorStoreService()
    return vs.list_papers(limit=200)


@router.post("/search", response_model=PaperQueryResponse)
async def search_papers(req: PaperQueryRequest):
    """
    跨文献检索：dense + sparse + RRF。
    验收标准：
    1. 接受 query/top_k/rrf_k；
    2. 返回带 paper_id、arXiv ID、页码、段落、bbox 的原文片段；
    3. 结果按 RRF 融合分数降序排列。
    """
    retrieval = RetrievalService()
    result = retrieval.search(req.query, top_k=req.top_k, rrf_k=req.rrf_k)
    chunks = [
        RetrievedChunk(
            paper_id=r.paper_id,
            arxiv_id=r.arxiv_id,
            version=r.version,
            title=r.title,
            kind=r.kind,
            text=r.text,
            section=r.section,
            page=r.page,
            paragraph=r.paragraph,
            score=r.score,
            bbox=r.bbox,
            latex=r.latex,
            caption=r.caption,
        )
        for r in result["fusion"]
    ]
    return PaperQueryResponse(query=req.query, chunks=chunks)


@router.post("/retrieve")
async def retrieve_papers(req: PaperQueryRequest):
    """
    检索层原始输出：同时返回 dense、sparse、RRF fusion 三路结果，便于调试与评测。
    """
    retrieval = RetrievalService()
    return retrieval.search(req.query, top_k=req.top_k, rrf_k=req.rrf_k)


@router.get("/{arxiv_id}/pdf-url")
async def get_pdf_url(arxiv_id: str):
    """
    获取论文 PDF 的临时访问链接；若本地 MinIO 不存在则回退 arXiv 官方 PDF。
    前端溯源高亮需要该 URL 配合 pdf.js 渲染。
    """
    arxiv_id = _normalize_arxiv_id(arxiv_id)
    vs = VectorStoreService()
    meta = vs.get_paper_meta_by_arxiv_id(arxiv_id)
    storage = StorageService()
    if meta and meta.get("pdf_s3_key"):
        url = storage.get_presigned_url(storage.papers_bucket, meta["pdf_s3_key"], expires=3600)
        if url:
            return {"arxiv_id": arxiv_id, "pdf_url": url, "source": "minio"}
    return {
        "arxiv_id": arxiv_id,
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        "source": "arxiv",
    }
