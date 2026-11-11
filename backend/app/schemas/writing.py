"""
写作辅助接口的请求/响应模型。

09 的响应体是「句子结构」而不是一段文本，这是整条链可追溯的前提：

    sections[].sentences[]  → 每句带 type（claim/bridge）、cite_nos（引#N）、chunk_ids
    references[]            → 每条带 ref_no（[K]）、paper_id、chunk_nos
    ref_map[]               → 引#N ↔ [K] ↔ chunk_id 的三级映射

前端点句子里的 `[引#N]` 时，直接用 `sentences[].evidence` 就能展开原文，
不需要再发一次请求——证据在生成时已经绑定好，界面只是把它展示出来。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WritingRequest(BaseModel):
    topic: str = Field(..., description="写作主题")
    n_sections: Optional[int] = Field(default=None, description="段落数，默认取 writing_sections 配置")
    arxiv_ids: Optional[List[str]] = Field(
        default=None, description="限定文献范围（为空则用整个库内可引文献）"
    )


class WritingSentence(BaseModel):
    """
    一句话 = 一个可追溯单元。

    `type` 只有两档：
    - `claim`  ：陈述结论，必须带引用；服务端会保证 `cite_nos` 非空（否则这句已被删）
    - `bridge` ：过渡/连接句，**无需引用**，界面上要显式标出来，
                 否则读者会以为作者漏标了引用
    """

    id: str
    section_id: str = ""
    order: int = 0
    type: str = "claim"
    text: str = ""
    cite_nos: List[int] = Field(default_factory=list)
    chunk_ids: List[str] = Field(default_factory=list)
    ref_nos: List[int] = Field(default_factory=list)
    evidence: List[Dict[str, Any]] = Field(
        default_factory=list, description="该句所引 chunk 的原文与位置，界面点引用直接展开"
    )
    bridge_cue: bool = False
    source: str = ""


class WritingSection(BaseModel):
    id: str
    no: int = 0
    title: str
    intent: str = ""
    query: str = ""
    sentences: List[WritingSentence] = Field(default_factory=list)
    placeholder: bool = False
    gap: str = ""
    evidence: List[int] = Field(default_factory=list, description="本段拿到的证据编号（含未被引用的）")
    evidence_candidates: int = Field(default=0, description="过 kind/长度过滤的候选证据数")
    evidence_low_relevance: int = Field(default=0, description="被相关性下限刷掉的证据数")
    dropped: List[Dict[str, Any]] = Field(default_factory=list)


class WritingReference(BaseModel):
    """
    一条参考文献 = 库里的一篇论文。

    `checked=true` 表示这条在 `papers` 表里重新查到了、标题一致、且有正文 chunk。
    真实场景里会有查不到的情况（论文被删、库重建），那时该条**不进列表**并计入
    `stats.refs_rejected`——宁可少一条，也不能留一条来路不明的文献。
    """

    ref_no: int
    paper_id: str
    title: str = ""
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    doi: str = ""
    arxiv_id: str = ""
    chunk_nos: List[int] = Field(default_factory=list)
    checked: bool = True
    issues: List[str] = Field(default_factory=list)


class WritingRefMapRow(BaseModel):
    n: int
    chunk_id: str = ""
    # 关系库 papers 表真实 id（与 `evidence[].paper_id` 同口径，可直接点开论文）
    paper_id: str = ""
    # chunk payload 里的 `{arxiv_id}::{version}` 复合键，仅备查，不要拿去查库
    chunk_paper_ref: str = ""
    arxiv_id: str = ""
    title: str = ""
    page: int = 0
    section: str = ""
    ref_no: Optional[int] = None
    used_in: List[str] = Field(default_factory=list)


class WritingConfig(BaseModel):
    version: str = ""
    prompt_version: str = ""
    fingerprint: str = ""
    thresholds: Dict[str, Any] = Field(default_factory=dict)


class WritingStats(BaseModel):
    sections: int = 0
    sentences: int = 0
    claim_sentences: int = 0
    bridge_sentences: int = 0
    cited_sentences: int = 0
    uncited_claim: int = 0
    placeholders: int = 0
    sentences_dropped: int = 0
    dropped_by_reason: Dict[str, int] = Field(default_factory=dict)
    refs: int = 0
    refs_rejected: int = 0
    fabricated_refs: int = 0
    evidence_chunks: int = 0
    papers: int = 0
    degraded: bool = False
    prompt_version: str = ""
    config_version: str = ""
    fingerprint: str = ""


class WritingResponse(BaseModel):
    topic: str
    markdown: str = ""
    sections: List[WritingSection] = Field(default_factory=list)
    references: List[WritingReference] = Field(default_factory=list)
    ref_map: List[WritingRefMapRow] = Field(default_factory=list)
    stats: WritingStats = Field(default_factory=WritingStats)
    config: Optional[WritingConfig] = None
    export_key: Optional[str] = None
    export_json_key: Optional[str] = None
    rejected: bool = False
    reject_reason: Optional[str] = None
    degraded: bool = False
    trace: Optional[Dict[str, Any]] = Field(
        default=None, description="规划/检索/校验各阶段诊断，供复盘"
    )


class WritingExportRequest(BaseModel):
    topic: str
    markdown: str
    fmt: str = Field(default="md", description="md | json；json 需要同时传 payload")


class WritingExportPayloadRequest(BaseModel):
    """导出机读版：前端把整份响应体回传，服务端原样落盘（不在服务端重建）。"""

    topic: str
    payload: Dict[str, Any]


# ---- 大纲接口保留（08 之前的入口，前端老页面/脚本还在用） -------------------


class OutlineRequest(BaseModel):
    topic: str = Field(..., description="论文主题")
    top_k: int = Field(default=8, description="参考片段数（保留字段）")


class OutlineResponse(BaseModel):
    topic: str
    outline: str
    papers: List[str] = Field(default_factory=list, description="大纲所依据的库内文献标题")
