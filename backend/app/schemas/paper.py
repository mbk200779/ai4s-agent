from pydantic import BaseModel, Field
from typing import List, Optional


class PaperMeta(BaseModel):
    """论文元数据结构：唯一键为 arxiv_id+version。"""
    arxiv_id: str = Field(..., description="arXiv 唯一标识，例如 2401.12345")
    version: str = Field(default="v1", description="论文版本号")
    title: str = Field(default="", description="标题")
    authors: List[str] = Field(default_factory=list, description="作者列表")
    abstract: str = Field(default="", description="摘要")
    pdf_url: Optional[str] = Field(default=None, description="PDF 在线地址")
    s3_key: Optional[str] = Field(default=None, description="MinIO 中原始 PDF 的 key")
    body_hash: Optional[str] = Field(default=None, description="正文内容 hash，用于去重")
    published: Optional[str] = Field(default=None, description="发表/发布日期")


class PaperUploadResponse(BaseModel):
    paper_id: str
    arxiv_id: str
    version: str
    task_id: Optional[str] = None
    message: str = "论文已接收，后台异步解析中"


class PaperQueryRequest(BaseModel):
    query: str
    top_k: int = 10
    rrf_k: int = 60


class RetrievedChunk(BaseModel):
    paper_id: str
    arxiv_id: str
    version: str = "v1"
    title: str
    kind: str = "paragraph"
    text: str
    section: str
    page: int
    paragraph: int = 0
    score: float
    bbox: Optional[List[float]] = None
    latex: Optional[str] = None
    caption: Optional[str] = None


class PaperQueryResponse(BaseModel):
    query: str
    chunks: List[RetrievedChunk]
