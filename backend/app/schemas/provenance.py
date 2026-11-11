"""
Provenance 日志 schema：每条结论必须可追溯到原始片段。
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
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
    # 查询理解与检索分流的完整 trace（类型、规则命中、策略、每一跳、召回统计）。
    # 存进 provenance 的意义：事后复盘"为什么这次答错了"时，
    # 需要知道当时判成什么类型、走的是哪条策略，而不是只能看最终片段列表。
    routing: Dict[str, Any] = Field(default_factory=dict, description="检索分流 trace")
    # 每个角标最终落在哪一句、哪个矩形。事后复盘"高亮错了"时，
    # 需要能回答"当时选中的是 ref_id=s4，坐标 [x0,y0,x1,y1]"。
    answer_refs: List[Dict[str, Any]] = Field(
        default_factory=list, description="角标 → 句子锚点（逐处）"
    )
    # 流式链路的 trace_id。provenance 是"结果"，事件流是"过程"，
    # 两者靠这个 id 对得上：qa/stream/<日期>/<trace_id>.jsonl 就是同一次对话的完整事件日志。
    trace_id: Optional[str] = Field(default=None, description="流式链路 trace_id")
    # 07：整条回答的三档置信 + 当时的阈值版本/指纹。
    # 存阈值而不只存档位，是为了半年后还能回答"为什么这句当时算已验证"——
    # 只存 "level=verified" 的话，阈值一调就再也解释不通了。
    confidence: Dict[str, Any] = Field(default_factory=dict, description="三档置信与阈值版本")
    # 12：三类不确定性状态（空检索 / 证据冲突 / 表格未识别）的判定结果。
    # 存判定结果的同时也存了当时的 `version` 与 `rules`：
    # 半年后回头问"当时为什么判成空检索"，光有结论没有判据是解释不通的。
    uncertainty: Dict[str, Any] = Field(
        default_factory=dict, description="不确定性状态（empty/conflict/unreadable）"
    )
