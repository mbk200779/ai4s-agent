"""
Provenance 日志 schema：每条结论必须可追溯到原始片段。
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime


class ProvenanceSource(BaseModel):
    """单条引用来源。"""
    chunk_id: str
    paper_id: str
    arxiv_id: str
    version: str
    title: str
    kind: str
    page: int
    paragraph: int = 0
    bbox: Optional[List[float]] = None
    text: str
    score: float


class ProvenanceRecord(BaseModel):
    """一次 QA 问答的 provenance 记录。"""
    query: str
    answer: str
    rejected: bool = False
    reject_reason: Optional[str] = None
    sources: List[ProvenanceSource]
    invalid_citations: List[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    answer_ref_map: Dict[str, str] = Field(default_factory=dict, description="[引#N] -> chunk_id 映射")
    reasoning_chain: List[Dict[str, Any]] = Field(default_factory=list, description="跨文献推理链")
