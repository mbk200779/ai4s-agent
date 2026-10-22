from pydantic import BaseModel, Field
from typing import List, Optional


class GraphNode(BaseModel):
    id: str
    label: str
    arxiv_id: str
    title: str
    influential_citation_count: int = 0
    citation_count: int = 0
    influence_score: float = 0.0
    source_url: str
    group: Optional[str] = None


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str = "cites"
    weight: float = 0.0
    is_perfunctory: bool = False


class GraphRequest(BaseModel):
    seed_arxiv_id: str = Field(..., description="中心论文 arxiv_id")
    depth: int = Field(default=1, ge=1, le=2, description="引用传播深度")
    filter_perfunctory: bool = Field(default=True, description="是否过滤应付式引用")


class GraphResponse(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]
