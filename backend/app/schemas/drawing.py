from typing import Any, List, Optional
from pydantic import BaseModel, Field


class SourceInfo(BaseModel):
    """数据出处信息。"""

    arxiv_id: Optional[str] = Field(default=None, description="来源 arXiv ID")
    title: Optional[str] = Field(default=None, description="论文标题")
    page: Optional[int] = Field(default=None, description="页码")
    paragraph: Optional[int] = Field(default=None, description="段落号")
    text: Optional[str] = Field(default=None, description="片段原文摘要")


class DrawingRequest(BaseModel):
    """绘图辅助请求。"""

    chart_type: str = Field(
        ...,
        description="图表类型：matplotlib_line / matplotlib_bar / matplotlib_scatter / matplotlib_histogram / mermaid_flowchart / mermaid_sequence / mermaid_mindmap",
    )
    title: str = Field(..., description="图表标题")
    description: str = Field(default="", description="数据/结论描述或绘图需求")
    data: Optional[Any] = Field(default=None, description="结构化数据，如 {'x': [...], 'y': [...]} 或 mermaid 节点列表")
    query: Optional[str] = Field(default=None, description="可选：从文献库检索支撑片段")
    sources: List[str] = Field(default_factory=list, description="显式数据出处 arXiv ID 列表")
    top_k: int = Field(default=6, description="检索片段数")
    formats: List[str] = Field(default_factory=lambda: ["png", "svg"], description="导出格式，支持 png / svg")


class DrawingResponse(BaseModel):
    """绘图辅助响应。"""

    chart_type: str
    title: str
    code: str
    code_language: str
    image_url_png: Optional[str] = Field(default=None, description="PNG 图片 presigned URL")
    image_url_svg: Optional[str] = Field(default=None, description="SVG 图片 presigned URL")
    sources: List[SourceInfo]
    cache_key: str
    provenance_key: str
    rejected: bool = False
    reject_reason: Optional[str] = None
