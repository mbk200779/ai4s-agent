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
    # ---- 课题化后新增，全部可选：旧调用方只读上面几个字段，不受影响 ----
    project_id: Optional[str] = Field(default=None, description="论文归属的课题")
    paper_uuid: Optional[str] = Field(default=None, description="papers 表主键")
    version_id: Optional[str] = Field(default=None, description="paper_versions 表主键")
    # ---- 异步解析新增 ----
    stage: Optional[str] = Field(
        default=None,
        description="解析阶段：queued/downloading/parsing/structuring/indexed/reused/failed",
    )
    reused: Optional[bool] = Field(
        default=None, description="true 表示命中了 file_hash，直接复用已有解析结果"
    )
    progress_url: Optional[str] = Field(
        default=None, description="进度查询地址（轮询用，SSE 把 progress 换成 events）"
    )
    detail: Optional[str] = Field(default=None, description="复用/跳过时的人读说明")


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
