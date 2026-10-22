from pydantic import BaseModel, Field
from typing import List, Optional


class ReviewRequest(BaseModel):
    topic: str = Field(..., description="综述主题或问题")
    arxiv_ids: Optional[List[str]] = Field(default=None, description="指定论文列表，为空则自动检索")
    top_k: int = Field(default=10, description="检索论文数")


class ReviewResponse(BaseModel):
    topic: str
    markdown: str
    rejected: bool = False
    reject_reason: Optional[str] = None
    invalid_citations: List[str] = Field(default_factory=list)
    referenced_arxiv_ids: List[str] = Field(default_factory=list)
    export_key: Optional[str] = None


class ReviewExportRequest(BaseModel):
    topic: str
    markdown: str
