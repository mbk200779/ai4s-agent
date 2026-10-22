from typing import Any, Dict, List
from pydantic import BaseModel, Field


class EvalReport(BaseModel):
    """评测报告结构。"""

    id: str
    timestamp: str
    checks: Dict[str, Any]
    key: str | None = None


class EvalListResponse(BaseModel):
    """评测报告列表。"""

    reports: List[Dict[str, Any]] = Field(default_factory=list)
