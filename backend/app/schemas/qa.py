from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class QARequest(BaseModel):
    question: str = Field(..., description="用户问题")
    top_k: int = Field(default=8, description="检索片段数")
    rrf_k: int = Field(default=60, description="RRF 融合参数")
    # 评测与调试用：强制指定检索类型，跳过分类器。不传则自动判断。
    force_type: Optional[str] = Field(
        default=None,
        description="强制检索类型（point/compare/survey/nav/chitchat/generic），留空自动分类",
    )
    # A/B 开关：不传则跟随服务端配置（RERANK_ENABLED / NUMERIC_RECALL_ENABLED）。
    # 有了它，"精排到底有没有用"可以在同一份语料、同一批问题上跑两遍得出结论，
    # 而不是靠改代码、重启、再比一遍。
    rerank: Optional[bool] = Field(default=None, description="是否走 cross-encoder 精排，留空跟随配置")
    numeric: Optional[bool] = Field(default=None, description="是否走数值/公式规则召回，留空跟随配置")
    # ---- 13 索引版本化 ----
    # 这两个参数只影响"读哪一版索引"，不影响语料内容本身。
    # 都不传时按全局灰度比例分桶——也就是生产环境的默认行为。
    project_id: Optional[str] = Field(
        default=None,
        description="课题 id：命中课题级切流规则时优先于全局灰度比例",
    )
    index_version: Optional[str] = Field(
        default=None,
        description="显式指定索引版本（idx-…）：评测、排查与强制 A/B 用，压过所有切流规则",
    )


class Anchor(BaseModel):
    """一条句子级锚点。区间是相对 chunk.text 的字符偏移，rects 是页面坐标。"""

    ref_id: str
    kind: str = "sentence"          # sentence | cell | block
    precision: str = "sentence"     # sentence | cell | block | ocr
    start: int = 0
    end: int = 0
    rects: List[List[float]] = Field(default_factory=list)
    bbox: Optional[List[float]] = None


class AnchorDoc(BaseModel):
    """一个 chunk 的锚点文档（对应 chunks.anchors 列）。"""

    v: int = 1
    page: int = 0
    page_size: Optional[List[float]] = None
    # False 表示这页没有文本层，坐标来自 OCR —— 前端要画虚框并标注「OCR 定位」
    text_layer: bool = True
    precision: Optional[str] = None
    kind: Optional[str] = None
    items: List[Anchor] = Field(default_factory=list)


class AnswerRef(BaseModel):
    """
    答案里第 ordinal 个角标最终落到了哪一句。

    与 citations 的区别：citations 是"检索到了哪些片段"（去重、按片段的），
    answer_refs 是"每个角标指向哪一句"（不去重、按出现顺序的）。
    同一片段被引用两次时两者数量不同，所以必须分开。
    """

    ordinal: int = 0
    index: int = 0
    chunk_id: str
    arxiv_id: Optional[str] = None
    page: int = 0
    ref_id: Optional[str] = None
    # 锚点在 chunk.text 里的字符区间。前端用它校验"取到的这句"和"库里那句"是同一句
    # （换版本、重新清洗后 offset 会变，对不上就该退回页级定位而不是框错地方）。
    start: int = 0
    end: int = 0
    text: Optional[str] = None
    bbox: Optional[List[float]] = None
    rects: Optional[List[List[float]]] = None
    precision: Optional[str] = None
    claim: Optional[str] = None
    match_score: Optional[float] = None
    # 这个角标所属那句话的置信档（verified / needs_check / unsupported）
    level: Optional[str] = None
    has_anchor: bool = False


class MissingItem(BaseModel):
    """
    「缺什么」的一条。code 是稳定标识（前端据此决定图标/筛选），text 是给人看的。

    为什么要有 code：文案会改、会翻译，但"缺表格"这个事实不会变；
    统计"30 题里有多少条缺表格"只能靠 code，不能靠字符串匹配。
    """

    code: str
    text: str
    claims: int = 1


class ClaimEvidence(BaseModel):
    """一条证据：某个 [引#N] 落到哪一句、命中多少分。"""

    index: int
    status: str = "unsupported"
    reason: Optional[str] = None
    chunk_id: Optional[str] = None
    arxiv_id: Optional[str] = None
    title: Optional[str] = None
    page: int = 0
    kind: Optional[str] = None
    ref_id: Optional[str] = None
    precision: Optional[str] = None
    text: Optional[str] = None
    rects: Optional[List[List[float]]] = None
    bbox: Optional[List[float]] = None
    match_score: Optional[float] = None
    # 跨语言语义支持度（0~1）。None 表示语义核验未启用或失败——
    # 这个字段与 match_score 一起决定档位，暴露出来才能解释"为什么这句是需核对"。
    sem_score: Optional[float] = None


class Locate(BaseModel):
    """
    定位精度：sentence/cell 能画到句，block/line 只能画到一段。

    它和服务档位分开：块级定位不影响"这句话有没有原文支持"，
    只影响"点角标能不能框到那一句"。混进置信里会让锚点覆盖率低的语料
    整体降档，把"定位粗糙"说成"证据不足"。
    """

    precision: Optional[str] = None
    note: Optional[str] = None


class ClaimConfidence(BaseModel):
    """句子级置信。signals 是定档依据的原值——保留它，档位才是可复核的。"""

    level: str
    label: str
    score: float = 0.0
    missing: List[MissingItem] = Field(default_factory=list)
    locate: Optional[Locate] = None
    signals: Dict[str, Any] = Field(default_factory=dict)


class Claim(BaseModel):
    """一句话 + 它的证据 + 它的置信档。"""

    claim_id: str
    span: List[int] = Field(default_factory=list)
    text: str = ""
    # status：证据校验过没过（05/06 的口径）；level：这句话能信到什么程度（07）
    status: str = "unsupported"
    level: str = "unsupported"
    level_label: str = "证据不足"
    reason: Optional[str] = None
    missing: List[MissingItem] = Field(default_factory=list)
    confidence: Optional[ClaimConfidence] = None
    citations: List[ClaimEvidence] = Field(default_factory=list)


class ConfidenceConfig(BaseModel):
    """阈值版本块：改了阈值，这里必须跟着变，否则复盘时分不清用的是哪套线。"""

    version: str = ""
    prompt_version: str = ""
    fingerprint: str = ""
    thresholds: Dict[str, Any] = Field(default_factory=dict)
    labels: Dict[str, str] = Field(default_factory=dict)
    descriptions: Dict[str, str] = Field(default_factory=dict)


class Confidence(BaseModel):
    """
    整条回答的置信档。

    `applicable=false` 专门留给闲聊/直答：那种回答没有句子要校验，
    塞进三档里任何一档都是错的（说"已验证"会把这个词稀释掉）。
    """

    applicable: bool = True
    level: Optional[str] = None
    label: str = ""
    description: str = ""
    score: float = 0.0
    counts: Dict[str, int] = Field(default_factory=dict)
    missing: List[MissingItem] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    signals: Dict[str, Any] = Field(default_factory=dict)
    config: Optional[ConfidenceConfig] = None


class ScopePaper(BaseModel):
    """已检索范围里的一篇论文。空检索时"我到底搜了什么"就靠它回答。"""

    arxiv_id: str = ""
    title: str = ""


class EmptyTerm(BaseModel):
    """一个检索关键词在库内的命中情况。`chunks=0` 那一行就是空检索的原因。"""

    term: str
    chunks: int = 0
    hit: bool = False


class EmptyQuery(BaseModel):
    """
    这次检索的实际输入。

    原文与检索文本分开存：中文问句是先翻成英文再检索的，
    用户看到"关键词没命中"时，得能分辨是**自己那句话**的词没有，还是**译文**的词没有。
    """

    question: str = ""
    search_text: str = ""
    translated: bool = False
    type: str = ""
    strategy: Optional[str] = None
    terms: List[EmptyTerm] = Field(default_factory=list)
    zh_terms: List[str] = Field(default_factory=list)
    top_raw_score: Optional[float] = None
    threshold: Optional[float] = None
    # 同段覆盖：best_chunk_terms 是"单段最多同时命中几个关键词"，
    # rare 是区分度最高的两个词，rare_missing 表示它们都不在库里。
    # 这几个数是"为什么判成空检索"的原始依据，必须原样带出去。
    coverage: Dict[str, Any] = Field(default_factory=dict)


class EmptyScope(BaseModel):
    papers: int = 0
    chunks: int = 0
    by_kind: Dict[str, int] = Field(default_factory=dict)
    titles: List[ScopePaper] = Field(default_factory=list)


class Suggestion(BaseModel):
    """一条改写建议。`query` 必须是真的能提交的问句——按钮点下去要有事发生。"""

    id: str
    kind: str
    label: str
    query: str
    why: str = ""


class EmptyState(BaseModel):
    """
    空检索。

    `blocking=true` 表示"这次没有生成答案"，与"生成了但内容为空"是两件事，
    前端据此决定是替换正文还是加一条提示。
    """

    reason: str
    title: str
    detail: str
    blocking: bool = True
    scope: EmptyScope = Field(default_factory=EmptyScope)
    query: EmptyQuery = Field(default_factory=EmptyQuery)
    # 最近邻：它们的身份是"系统看到了什么"，不是证据。分开放，界面上也分开画。
    nearest: List[Dict[str, Any]] = Field(default_factory=list)
    suggestions: List[Suggestion] = Field(default_factory=list)
    action: Dict[str, Any] = Field(default_factory=dict)
    version: str = ""


class ConflictPaper(BaseModel):
    arxiv_id: str = ""
    title: str = ""
    page: int = 0


class ConflictQuote(BaseModel):
    """冲突一边的原文片段。必须带页码：不给页码的"并列展示"等于让用户去猜。"""

    chunk_id: str = ""
    arxiv_id: str = ""
    title: str = ""
    page: int = 0
    text: str = ""


class ConflictSide(BaseModel):
    """冲突的一边：结论 + 它出自哪几篇、哪几页。"""

    value: str = ""
    unit: str = ""
    conclusion: str = ""
    papers: List[ConflictPaper] = Field(default_factory=list)
    quotes: List[ConflictQuote] = Field(default_factory=list)


class Conflict(BaseModel):
    """
    证据冲突。

    `not_judged` 是这个结构里最重要的一句：明写"系统不判断哪边对"。
    没有它，界面上一左一右两栏天然会被读成"左对右错"。
    """

    id: str
    kind: str
    type: str
    type_label: str
    dimension: str
    probe: str = ""
    sides: List[ConflictSide] = Field(default_factory=list)
    not_judged: str = ""
    why: str = ""
    # 同名不同义的那些：**不**构成冲突，但必须说，否则用户会以为系统漏了
    ambiguous: List[Dict[str, Any]] = Field(default_factory=list)
    action: Dict[str, Any] = Field(default_factory=dict)


class UnreadableItem(BaseModel):
    """
    表格/图表未识别的一条。

    `cited=true` 表示这条残缺片段被本次回答引用了——那是"你刚看到的结论可能不准"，
    与"顺带一提这一段没解析好"不是同一件事，所以排序和配色都要分开。
    """

    id: str
    kind: str
    title: str
    detail: str
    severity: str = "info"
    cited: bool = False
    chunk_id: str = ""
    arxiv_id: str = ""
    paper_title: str = ""
    page: int = 0
    chunk_kind: str = ""
    evidence: Dict[str, Any] = Field(default_factory=dict)
    action: Dict[str, Any] = Field(default_factory=dict)


class Uncertainty(BaseModel):
    """
    一次回答的三类不确定性状态。

    `checked` 记录每类**检测了没有**：冲突检测只在问句带符号、或已有 claim 时才跑。
    "没检测"与"检测了没发现"是两件事，界面上不能都写成"已检查"。
    """

    version: str = ""
    states: List[str] = Field(default_factory=list)
    empty: Optional[EmptyState] = None
    conflicts: List[Conflict] = Field(default_factory=list)
    unreadable: List[UnreadableItem] = Field(default_factory=list)
    checked: Dict[str, Any] = Field(default_factory=dict)
    # 判定条件原样带给前端：界面要能回答"你凭什么判成空检索"
    rules: List[Dict[str, Any]] = Field(default_factory=list)


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
    # 精排分（0~1）。和 score 分开：score 可能是融合归一化+策略加成，
    # 精排分只表示"这条片段对这个问题有多相关"。
    rerank_score: Optional[float] = None
    # fusion / rule:xxx / fusion+rule —— 这条引用是怎么来的
    hit_source: Optional[str] = None
    # 原文定位（nav）命中字面时给出的上下文片段，前端可直接高亮
    snippet: Optional[str] = None
    # 本片段里被答案实际引用的那一句（同一片段被引用多次时取第一次）。
    # 与 ref_id/precision 一起，构成"角标 → 原句"的最小信息。
    ref_id: Optional[str] = None
    anchor: Optional[Anchor] = None
    precision: Optional[str] = None
    # 该片段是否有句子级锚点；False 时前端提示「该文档未建立精细锚点」
    has_anchors: bool = False


class QAResponse(BaseModel):
    answer: str
    rejected: bool = False
    reject_reason: Optional[str] = None
    citations: List[Citation] = Field(default_factory=list)
    invalid_citations: List[str] = Field(default_factory=list)
    provenance_key: Optional[str] = None
    # 查询理解 + 检索分流的完整 trace。前端据此显示"这条问题被判成了什么类型、
    # 走了哪条策略、命中了几篇论文"，也让"分流确实生效了"变成可观察的事实。
    routing: Optional[Dict[str, Any]] = Field(default=None, description="检索分流 trace")
    # 逐处锚点（按角标出现顺序）与按 chunk 去重的锚点文档（阅读器排队高亮要用全量）
    answer_refs: List[AnswerRef] = Field(default_factory=list)
    anchor_index: Dict[str, AnchorDoc] = Field(default_factory=dict)
    # 07：逐句 claims（与流式 claim 事件同一结构）与整体三档置信。
    # 一次性接口也带这两个字段，前端两条链路才能共用一套标记样式。
    claims: List[Claim] = Field(default_factory=list)
    claim_stats: Dict[str, int] = Field(default_factory=dict)
    confidence: Optional[Confidence] = None
    # 12：三类不确定性状态。判定全部在服务端完成，前端只负责按它渲染——
    # 前端拿不到"库内几篇、关键词命中几次、同符号取过几个值"，也就无从自行猜测。
    uncertainty: Optional[Uncertainty] = None
