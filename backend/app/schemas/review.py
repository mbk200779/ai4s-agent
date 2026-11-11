"""
综述接口的请求/响应模型。

08 的响应体是「三件套」：
- `markdown`  ：给人读的综述正文（结构由服务端渲染，含立场、证据强度、冲突块）；
- `claims`    ：claim 表（一行 = 一个观点簇，答"这条结论由哪几篇支撑"）；
- `conflicts` ：冲突边清单（含被 LLM 否掉的那些，答"为什么这里不算矛盾"）。

三者共用一套证据编号 `refs`（1..N，对应 `refs[].n`），所以正文里的 `[引#N]`
和表里的行、冲突块里的引文指向同一个片段，点哪里都能落到同一页。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    topic: str = Field(..., description="综述主题或问题")
    arxiv_ids: Optional[List[str]] = Field(default=None, description="指定论文列表，为空则自动检索")
    top_k: int = Field(default=10, description="检索论文数（保留字段；证据池大小由 review_pool_* 配置控制）")


class ClaimPaper(BaseModel):
    arxiv_id: str
    title: str = ""


class ClaimEvidence(BaseModel):
    """一条 claim 的原文依据。`ref` 是全局证据编号，正文里的 [引#N] 就是它。"""

    claim_id: str = ""
    statement: str = ""
    quote: str = ""
    arxiv_id: str = ""
    title: str = ""
    page: int = 0
    section: str = ""
    kind: str = ""
    chunk_id: str = ""
    ref: Optional[int] = None
    quote_score: float = 1.0


class ClaimConflictLink(BaseModel):
    """claim 表里"这条结论和谁冲突"的指针。`with_cluster` 不用 `with`：后者是 TS/JS 保留字。"""

    id: str
    with_cluster: str = ""
    type: str = ""
    type_label: str = ""
    confirmed: Optional[bool] = None
    reason: str = ""


class ClaimRow(BaseModel):
    """
    claim 表的一行 = 一个观点簇。

    `strength` 只有两档：`multi_paper` / `single_paper`。
    单篇支持不是"低质量"，而是"证据就这么多"，必须显示出来——
    读者据此决定要不要自己去查第二篇。
    """

    id: str
    statement: str
    axis: str = ""
    stance: str = ""
    stance_label: str = ""
    strength: str = "single_paper"
    paper_count: int = 0
    claim_count: int = 0
    papers: List[ClaimPaper] = Field(default_factory=list)
    conditions: List[str] = Field(default_factory=list)
    evidence: List[ClaimEvidence] = Field(default_factory=list)
    refs: List[int] = Field(default_factory=list)
    sentence_count: int = 0
    conflicts: List[ClaimConflictLink] = Field(default_factory=list)
    note: str = ""


class ReviewConflict(BaseModel):
    """
    冲突边。

    `confirmed` 是三态而不是布尔：`true` 已确认、`false` 经复核否定、`null` 规则召回未复核。
    压成两态会把"没核实"和"核实过不算冲突"混为一谈，读者无法判断该不该信。
    """

    id: str
    clusters: List[str] = Field(default_factory=list)
    axis: str = ""
    type: str = ""
    type_label: str = ""
    confirmed: Optional[bool] = None
    reason: str = ""
    rule: Dict[str, Any] = Field(default_factory=dict)
    papers: List[ClaimPaper] = Field(default_factory=list)
    source: str = "rule"


class ReviewRef(BaseModel):
    n: int
    chunk_id: str = ""
    arxiv_id: str = ""
    paper_id: str = ""
    title: str = ""
    page: int = 0
    section: str = ""
    kind: str = ""
    hit_source: str = ""


class ReviewConfig(BaseModel):
    version: str = ""
    prompt_version: str = ""
    fingerprint: str = ""
    thresholds: Dict[str, Any] = Field(default_factory=dict)


class ReviewStats(BaseModel):
    paper_count: int = 0
    raw_claims: int = 0
    cluster_count: int = 0
    support: int = 0
    oppose: int = 0
    conditional: int = 0
    single_paper: int = 0
    conflicts_confirmed: int = 0
    conflicts_pending: int = 0
    conflicts_rejected: int = 0
    dropped_sentences: int = 0
    violations: int = 0
    degraded: bool = False


class ReviewResponse(BaseModel):
    topic: str
    markdown: str = ""
    rejected: bool = False
    reject_reason: Optional[str] = None
    invalid_citations: List[str] = Field(default_factory=list)
    referenced_arxiv_ids: List[str] = Field(default_factory=list)
    export_key: Optional[str] = None
    claims: List[ClaimRow] = Field(default_factory=list)
    conflicts: List[ReviewConflict] = Field(default_factory=list)
    refs: List[ReviewRef] = Field(default_factory=list)
    stats: ReviewStats = Field(default_factory=ReviewStats)
    config: Optional[ReviewConfig] = None
    degraded: bool = False
    trace: Optional[Dict[str, Any]] = Field(
        default=None, description="抽/聚/判各阶段的诊断信息，供排障与答辩复盘"
    )


class ReviewExportRequest(BaseModel):
    """
    导出请求。

    `refs` 是可选的：页面内 `[引#N]` 能点开，导出的文件不能，所以导出的文件
    需要自带一份编号对照。老客户端不传也能导出，只是没有附录。
    """

    topic: str
    markdown: str
    refs: List[ReviewRef] = Field(default_factory=list)
