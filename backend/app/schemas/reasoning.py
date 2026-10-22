"""
跨文献推理 schema：多跳问答、对比、矛盾发现。
"""
from pydantic import BaseModel, Field
from typing import List, Optional


class ReasoningStep(BaseModel):
    """单条推理步骤。"""
    step: int = Field(..., description="步骤序号")
    sub_question: str = Field(..., description="子问题")
    conclusion: str = Field(..., description="该步结论")
    citation_ids: List[str] = Field(default_factory=list, description="引用的 chunk_id 列表")
    sources: List[dict] = Field(default_factory=list, description="该步 grounding 的原文片段")


class Contradiction(BaseModel):
    """矛盾/不一致点。"""
    claim_a: str
    claim_b: str
    chunk_id_a: str
    chunk_id_b: str
    explanation: str


class CrossPaperReasoningRequest(BaseModel):
    query: str
    max_hops: int = Field(default=3, ge=1, le=5)
    top_k_per_hop: int = Field(default=8)


class CrossPaperReasoningResponse(BaseModel):
    query: str
    answer: str
    rejected: bool = False
    reject_reason: Optional[str] = None
    reasoning_chain: List[ReasoningStep] = Field(default_factory=list)
    contradictions: List[Contradiction] = Field(default_factory=list)
    sources: List[dict] = Field(default_factory=list)
    papers_involved: List[str] = Field(default_factory=list, description="涉及的 paper_id 列表")
    provenance_key: Optional[str] = None


class ComparisonRequest(BaseModel):
    topic: str
    arxiv_ids: List[str]
    top_k: int = 8


class ComparisonResponse(BaseModel):
    topic: str
    comparison: str
    contradictions: List[Contradiction]
    sources: List[dict]
    provenance_key: Optional[str] = None
