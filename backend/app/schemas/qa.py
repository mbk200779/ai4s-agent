from pydantic import BaseModel, Field
from typing import List, Optional


class QARequest(BaseModel):
    question: str = Field(..., description="用户问题")
    top_k: int = Field(default=8, description="检索片段数")
    rrf_k: int = Field(default=60, description="RRF 融合参数")


class Citation(BaseModel):
    paper_id: str
    arxiv_id: str
    title: str
    chunk_id: str
    chunk_text: str
    page: int
    paragraph: int = 0
    bbox: Optional[List[float]] = None
    score: float = 0.0


class QAResponse(BaseModel):
    answer: str
    rejected: bool = False
    reject_reason: Optional[str] = None
    citations: List[Citation] = Field(default_factory=list)
    invalid_citations: List[str] = Field(default_factory=list)
    provenance_key: Optional[str] = None
