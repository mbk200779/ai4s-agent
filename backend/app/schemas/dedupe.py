"""去重（关系判定）相关请求/响应模型。"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DedupeDecideRequest(BaseModel):
    project_id: Optional[str] = None
    relation: str = Field(..., description="version_of | possible_duplicate | related")
    action: str = Field(..., description="keep_new | keep_both | link_only")
    confidence: float = 0.0
    peer_version_id: str = ""
    new_version_id: str = ""
    new_simhash: str = ""
    peer_simhash: str = ""
    new_label: str = ""
    peer_label: str = ""
    evidence: Dict[str, Any] = Field(default_factory=dict)
    suppress: bool = True


class DedupeDecideResponse(BaseModel):
    decision_id: str
    pair_key: str
    remembered_note: str = ""


class DedupeDecisionRow(BaseModel):
    id: str
    relation: str
    relation_label: str = ""
    action: str
    confidence: float = 0.0
    new_label: str = ""
    peer_label: str = ""
    new_paper_title: Optional[str] = None
    peer_paper_title: Optional[str] = None
    decided_by: str = "user"
    suppress: bool = True
    created_at: str = ""
    evidence: Dict[str, Any] = Field(default_factory=dict)
    new_version_id: str = ""
    peer_version_id: str = ""


class DedupeDecisionList(BaseModel):
    rows: List[DedupeDecisionRow] = Field(default_factory=list)
    total: int = 0


class DedupeBackfillResult(BaseModel):
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    versions: List[Dict[str, Any]] = Field(default_factory=list)
