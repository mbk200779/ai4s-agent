"""
规则表：查询类型的可解释判据 + 槽位抽取 + 本地原型分类器的原型句。

为什么规则先行：五类问题的**表层形态**差异很大（"β2 设为多少" vs "这几篇有何差异"），
正则能覆盖绝大多数，而且 0 延迟、可解释、可复现——trace 里直接把命中的规则列出来，
用户能看到"为什么被判成点查"。规则判不准时才花钱调小模型。

规则表只做两件事：给每个类型打分、给出命中证据。不做任何 IO。
"""
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.rag.taxonomy import QueryType

# ---------------------------------------------------------------------------
# 规则表：每条 = 一个可解释的判据
#   id     : 写进 trace 的稳定标识
#   type   : 命中后给哪个类型加分
#   weight : 加分权重（强判据 0.8~1.0，弱判据 0.3~0.5）
#   note   : 给前端/trace 看的人话解释
# ---------------------------------------------------------------------------
RULES: list[dict[str, Any]] = [
    # ---------------- 闲聊 ----------------
    {"id": "chitchat-greet", "type": QueryType.CHITCHAT, "weight": 1.0,
     "pattern": r"^\s*(你好|您好|hi|hello|hey|在吗|嗨|哈喽)[\s!！。.~]*$",
     "note": "纯问候语"},
    {"id": "chitchat-thanks", "type": QueryType.CHITCHAT, "weight": 0.55,
     "pattern": r"(谢谢|感谢|多谢|thanks|thank you)", "note": "致谢"},
    {"id": "chitchat-self", "type": QueryType.CHITCHAT, "weight": 0.9,
     "pattern": r"(你是谁|你叫什么|你是什么|你能做什么|你会什么|怎么用|如何使用你|介绍一下你自己|who are you)",
     "note": "询问助手自身能力"},
    {"id": "chitchat-ack", "type": QueryType.CHITCHAT, "weight": 0.7,
     "pattern": r"^\s*(嗯+|好的|好|ok|okay|收到|明白了?)[\s!！。.]*$", "note": "简短应答"},

    # ---------------- 点查 ----------------
    {"id": "point-beta", "type": QueryType.POINT, "weight": 0.95,
     "pattern": r"(β|beta|Β)\s*[12１２]|β\s*=|epsilon|ϵ", "note": "点名 Adam 的 β1/β2/ε"},
    {"id": "point-hyper", "type": QueryType.POINT, "weight": 0.75,
     "pattern": r"(超参|超参数|hyper-?parameter|学习率|learning rate|\blr\b|warmup|dropout|"
                r"batch ?size|weight decay|权重衰减|梯度裁剪|gradient clip|clip ?norm|"
                r"训练步数|迭代次数|epoch)",
     "note": "超参数/训练配置名词"},
    {"id": "point-hardware", "type": QueryType.POINT, "weight": 0.7,
     "pattern": r"(GPU|显卡|显存|TPU|\bP100\b|\bV100\b|\bA100\b|\bH100\b|\bH800\b|硬件配置|机器配置)",
     "note": "硬件/设备型号"},
    {"id": "point-metric", "type": QueryType.POINT, "weight": 0.55,
     "pattern": r"(BLEU|F1|ROUGE|准确率|精度|accuracy|precision|recall|困惑度|perplexity|"
                r"FLOPs?|参数量|parameters? ?count)",
     "note": "具体指标数值"},
    {"id": "point-chart", "type": QueryType.POINT, "weight": 0.6,
     "pattern": r"(图|表|公式|方程|式)\s*[\(（]?\s*\d|Figure\s*\d|Fig\.?\s*\d|Table\s*\d|"
                r"Algorithm\s*\d|Eq\.?\s*[\(（]?\s*\d",
     "note": "点名了图表/公式编号"},
    {"id": "point-value", "type": QueryType.POINT, "weight": 0.45,
     "pattern": r"(是多少|设为多少|设置?为多少|取值|数值|等于多少|多少(个|层|头|块|步|次|维|条)?|"
                r"几个|阈值|系数|比例|百分比|百分之)",
     "note": "在问一个具体取值"},
    {"id": "point-dim", "type": QueryType.POINT, "weight": 0.4,
     "pattern": r"(维度|d_model|d_ff|头数|层数|embedding ?size|hidden ?size|输入分辨率|分辨率)",
     "note": "结构维度参数"},

    # ---------------- 跨文对比 ----------------
    {"id": "compare-diff", "type": QueryType.COMPARE, "weight": 0.9,
     "pattern": r"(有何差异|有什么差异|差异|区别|不同之处|不同点|异同|对比|比较|相比|"
                r"优劣|优缺点|哪个更好|孰优孰劣|异同点)",
     "note": "显式的比较措辞"},
    {"id": "compare-multi", "type": QueryType.COMPARE, "weight": 0.8,
     "pattern": r"(这两篇|三篇|四篇|几篇|这几篇|多篇|两篇论文|跨论文|跨文献|跨 ?paper|"
                r"它们之间|这几项工作)",
     "note": "指向多篇文档"},
    {"id": "compare-relation", "type": QueryType.COMPARE, "weight": 0.6,
     "pattern": r"(继承|演进|演化|演变|沿用|借鉴|改进自|基于.{0,8}(改进|扩展)|"
                r"与.{0,24}的(关系|关联|联系|差异|区别))",
     "note": "问两件事物之间的关系"},
    {"id": "compare-with-in", "type": QueryType.COMPARE, "weight": 0.7,
     "pattern": r"(与|和|跟).{0,30}(差异|不同|区别|异同|差别)",
     "note": "A 与 B 的差异（跨文档）"},
    {"id": "compare-vs", "type": QueryType.COMPARE, "weight": 0.65,
     "pattern": r"(\bvs\.?\b|\bversus\b|和.{0,20}(相比|对比|有什么不同))", "note": "vs / 和…相比"},

    # ---------------- 综述 ----------------
    {"id": "survey-word", "type": QueryType.SURVEY, "weight": 1.0,
     "pattern": r"(综述|研究现状|研究进展|发展脉络|发展趋势|文献综述|技术路线|"
                r"\bsurvey\b|state[ -]of[ -]the[ -]art|文献回顾)",
     "note": "综述类措辞"},
    {"id": "survey-enumerate", "type": QueryType.SURVEY, "weight": 0.7,
     "pattern": r"(有哪些(方法|工作|研究|技术|路线|方案|思路|变体|做法)|主要(方法|工作|研究|贡献|变体)|"
                r"相关工作|相关研究|都有什么(方法|工作)|可以分成几类|分成哪几类)",
     "note": "要求枚举多种方法"},
    {"id": "survey-summarize", "type": QueryType.SURVEY, "weight": 0.5,
     "pattern": r"(总结(一下|下)?|归纳|梳理|分类|全景|概览|整体情况|大致情况)", "note": "要求归纳梳理"},

    # ---------------- 原文定位 ----------------
    {"id": "nav-where", "type": QueryType.NAV, "weight": 0.95,
     "pattern": r"(哪一?页|第几页|在哪(里|一页|一段|一节)|何处|哪个小节|第几节)",
     "note": "直接问位置"},
    {"id": "nav-original", "type": QueryType.NAV, "weight": 0.8,
     "pattern": r"(原文|原句|原话|哪句话|哪一段|哪句|逐字|一字不差|引用自)", "note": "要求回到原文原句"},
    {"id": "nav-which-paper", "type": QueryType.NAV, "weight": 0.85,
     "pattern": r"(哪篇|哪个论文|哪几篇|什么论文).{0,8}(提到|出现|讨论|写到|写了)",
     "note": "问某个说法出自哪一篇"},
    {"id": "nav-mention", "type": QueryType.NAV, "weight": 0.75,
     "pattern": r"((有没有|是否|是否曾)(提到|出现|说到|写了|讨论过)|提到(了)?吗|出现过吗)",
     "note": "核查某说法是否出现"},
    {"id": "nav-source", "type": QueryType.NAV, "weight": 0.7,
     "pattern": r"(出自|引自|见于|出自哪)", "note": "问出处"},
    # 单独一条"原文/原句 + 提到/出现"：这是最没有歧义的定位问法，
    # 不给它高权重的话，问句里的技术词（warmup、dropout）会把 point 的分抬起来，
    # 结果要去问小模型才能确认，平白多花 20ms。
    {"id": "nav-original-mention", "type": QueryType.NAV, "weight": 1.0,
     "pattern": r"(原文|原句|原话|正文).{0,12}(有没有|是否|提到|出现|写了|写到)",
     "note": "明确要求回到原文核查某说法"},
]

_COMPILED: list[tuple[dict[str, Any], re.Pattern[str]]] = [
    (r, re.compile(r["pattern"], re.IGNORECASE)) for r in RULES
]

# 学术信号词：出现任意一个，就说明这不是纯闲聊（"你好，这篇论文的 β2 是多少" 不能判成闲聊）。
_ACADEMIC_HINTS = re.compile(
    r"(论文|文献|paper|文章|模型|方法|实验|数据|训练|网络|算法|机制|"
    r"attention|transformer|bert|gpt|vit|cnn|rnn|lstm|embedding|"
    r"学习|梯度|损失|优化|收敛|推理|泛化|标注|评测|指标|"
    r"参数|层|维度|矩阵|向量|概率|分布|函数|公式|定理|引理)",
    re.IGNORECASE,
)


@dataclass
class RuleHit:
    """一条规则的命中记录，直接进 trace。"""

    rule_id: str
    type: str
    weight: float
    note: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_id,
            "type": self.type,
            "weight": self.weight,
            "note": self.note,
            "evidence": self.evidence,
        }


@dataclass
class RuleResult:
    """规则阶段的输出：每类得分 + 命中明细。"""

    scores: dict[str, float] = field(default_factory=dict)
    hits: list[RuleHit] = field(default_factory=list)

    @property
    def top(self) -> tuple[str, float]:
        if not self.scores:
            return QueryType.GENERIC, 0.0
        best = max(self.scores, key=lambda k: self.scores[k])
        return best, self.scores[best]

    @property
    def second_score(self) -> float:
        ordered = sorted(self.scores.values(), reverse=True)
        return ordered[1] if len(ordered) > 1 else 0.0


def classify_by_rules(question: str) -> RuleResult:
    """
    纯规则打分。同一个类型命中多条规则时权重相加（最多算 3 条，避免长问句被刷分），
    每条规则至多记一次命中。
    """
    scores: dict[str, float] = {}
    hits: list[RuleHit] = []
    per_type_count: dict[str, int] = {}

    academic = bool(_ACADEMIC_HINTS.search(question))

    for rule, pattern in _COMPILED:
        m = pattern.search(question)
        if not m:
            continue
        # 统一用字符串值当键：QueryType 是 str 枚举，混用 .value 与枚举实例
        # 会让同一个类型在 dict 里出现两份，得分被拆散、排序全乱。
        qtype = rule["type"].value
        weight = float(rule["weight"])

        # 闲聊规则遇到学术信号要压权：否则"你好，帮我看看这篇论文"会被判成闲聊。
        if qtype == QueryType.CHITCHAT and academic:
            weight *= 0.15

        # 每条规则对同一类型最多贡献 3 次，防止超长问句靠数量堆分。
        used = per_type_count.get(qtype, 0)
        if used >= 3:
            continue
        per_type_count[qtype] = used + 1

        scores[qtype] = scores.get(qtype, 0.0) + weight
        hits.append(
            RuleHit(
                rule_id=rule["id"],
                type=qtype,
                weight=round(weight, 3),
                note=rule["note"],
                evidence=m.group(0)[:40],
            )
        )

    return RuleResult(scores=scores, hits=hits)


def confidence_of(top: float, second: float) -> float:
    """
    置信度只看"第一名比第二名强多少"，不看绝对值：
      只有一类命中      → 0.95
      两类分数接近      → 0.50
    这个数不参与除阈值以外的任何决策，只用于 trace 展示和是否调小模型的判断。
    """
    if top <= 0:
        return 0.0
    denom = top + second
    if denom <= 0:
        return 0.0
    return round(min(0.95, 0.5 + 0.45 * ((top - second) / denom)), 3)


# ---------------------------------------------------------------------------
# 槽位抽取：策略要用到的结构化信息
# ---------------------------------------------------------------------------

_CHART_RE = re.compile(
    r"(?:Figure|Fig\.?|Table|Algorithm|Eq\.?)\s*[\(（]?(\d+[\.\d]*)"
    r"|(?:图|表|公式|方程|式|算法)\s*[\(（]?\s*(\d+[\.\d]*)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)(?![A-Za-z0-9])")
_QUOTED_RE = re.compile(r"[\"“「『']([^\"”」』']{2,120})[\"”」』']")
_LATIN_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9\-]{1,24})\b")
_ALL_SCOPE_RE = re.compile(r"(这些论文|所有论文|全部论文|这几篇|这几项|所有文献|全部文献)")

# 拉丁词里明显不是实体的高频词，参与实体消解只会引入噪声。
_STOP_TOKENS = {
    "the", "a", "an", "of", "in", "on", "at", "for", "with", "and", "or", "is", "are",
    "was", "were", "be", "to", "from", "by", "as", "that", "this", "these", "those",
    "what", "which", "who", "how", "why", "when", "where", "does", "do", "did",
    "figure", "fig", "table", "eq", "section", "chapter", "algorithm", "paper",
    "than", "vs", "versus", "between", "compare", "difference", "method", "model",
    "can", "could", "should", "would", "about", "please", "give", "show", "explain",
}


def extract_chart_refs(question: str) -> list[str]:
    """抽出问题点名的图表/公式编号，统一成 'figure 2' / 'table 3' 这种小写形式。"""
    refs: list[str] = []
    for m in _CHART_RE.finditer(question):
        num = m.group(1) or m.group(2)
        if not num:
            continue
        head = m.group(0).strip().lower()
        if head.startswith(("figure", "fig")):
            kind = "figure"
        elif head.startswith("table"):
            kind = "table"
        elif head.startswith(("algorithm", "算法")):
            kind = "algorithm"
        else:
            kind = "equation"
        refs.append(f"{kind} {num}")
    # 去重但保序
    return list(dict.fromkeys(refs))


# 超参词表：点查策略要用它去匹配"参数名 ≈ 数值"这种赋值形态
# （`β2 = 0.98`、`warmup_steps = 4000`、`dropout: 0.1`）。
# 这种形态比"这一段是表格"精确得多——附录里的超参大表也含这些词，
# 但只有正文的实验设置段才会写成"参数名 = 具体值"。
_HYPER_TERMS: list[tuple[str, str]] = [
    (r"β\s*1|beta\s*1", "beta1"),
    (r"β\s*2|beta\s*2", "beta2"),
    (r"β|beta(?!\s*[12])", "beta"),
    (r"ϵ|ε|epsilon", "epsilon"),
    (r"learning\s*rate|学习率|\blr\b", "learning rate"),
    (r"warm\s*-?\s*up|预热", "warmup"),
    (r"dropout", "dropout"),
    (r"batch\s*size|批量大小", "batch size"),
    (r"weight\s*decay|权重衰减", "weight decay"),
    (r"gradient\s*clip|clip\s*norm|梯度裁剪", "gradient clip"),
    (r"label\s*smoothing|标签平滑", "label smoothing"),
    (r"epoch", "epoch"),
    (r"training\s*steps|训练步数|steps", "steps"),
    (r"hidden\s*size|hidden\s*dim", "hidden size"),
    (r"\bd_model\b|model\s*dimension", "d_model"),
    (r"\bd_ff\b|ffn", "d_ff"),
    (r"attention\s*heads?|头数", "heads"),
    (r"\blayers?\b|层数", "layers"),
    (r"\brank\b|\br\s*=", "rank"),
    (r"\balpha\b|\bα\b", "alpha"),
]
_HYPER_COMPILED = [(re.compile(p, re.I), name) for p, name in _HYPER_TERMS]


def extract_hyper_terms(question: str) -> list[str]:
    """抽问句里点到的超参名，返回规范化名字（beta2 / warmup / dropout …）。"""
    found: list[str] = []
    for pat, name in _HYPER_COMPILED:
        if pat.search(question):
            found.append(name)
    return list(dict.fromkeys(found))


def extract_quoted(question: str) -> list[str]:
    """抽出引号里的片段——nav 用它做精确匹配的锚点。"""
    return [m.group(1).strip() for m in _QUOTED_RE.finditer(question) if m.group(1).strip()]


def extract_numbers(question: str) -> list[str]:
    """
    抽出问句里的"有意义的数值"。

    两个过滤都是踩过坑加的：
      - 不允许紧贴字母数字（`(?<![A-Za-z0-9])`），否则 "beta2"/"β2"/"vit16" 里的那个 2
        会被当成用户关心数值 2，然后"片段里出现 2"就给加成——几乎每段都有数字，
        这个加成等于给所有结果发同等奖励，纯粹稀释信号；
      - 单个数字不要，除非带小数点。问"β2 是 0.98 还是 0.999"时有用的是 0.98/0.999。
    """
    out: list[str] = []
    for m in _NUMBER_RE.finditer(question):
        num = m.group(1)
        if len(num) >= 2 or "." in num:
            out.append(num)
    return list(dict.fromkeys(out))


def extract_entity_tokens(question: str) -> list[str]:
    """
    抽出可能的实体（论文名/模型名/方法名）。

    只取拉丁词 + 全大写缩写，不做词形还原：实体消解靠后面跟语料画像做子串/词元匹配，
    这里多给几个候选没坏处，漏掉才是问题。
    """
    tokens: list[str] = []
    for m in _LATIN_TOKEN_RE.finditer(question):
        tok = m.group(1)
        low = tok.lower()
        if low in _STOP_TOKENS or len(low) < 2:
            continue
        # 纯数字混排（如 16x16）不当作实体
        if not re.search(r"[A-Za-z]", tok):
            continue
        tokens.append(tok)
    return list(dict.fromkeys(tokens))


def wants_all_papers(question: str) -> bool:
    """'这几篇论文' 这类指代：无法消解到具体论文时，就按语料全集处理。"""
    return bool(_ALL_SCOPE_RE.search(question))


# ---------------------------------------------------------------------------
# 本地原型分类器（"小模型"）用的原型句
# ---------------------------------------------------------------------------
# 每个类型给 8 条中英混合原型，覆盖口语与书面两种问法。
# 向量在首次使用时算一次并缓存，之后每条查询只需要算 1 条 query 向量（~20ms），
# 这就是"规则 + 小模型"里小模型那一路的全部成本。
PROTOTYPES: dict[str, list[str]] = {
    QueryType.POINT: [
        "β2 设为多少",
        "Adam 优化器的 beta1 和 beta2 各是多少",
        "学习率 warmup 步数是多少",
        "训练用了什么型号的 GPU",
        "模型的 dropout 比例是多少",
        "参数量大概多少个",
        "Table 3 里最佳配置的 BLEU 分数是多少",
        "how many training steps did they use",
        "what batch size was used for training",
        "what is the dimension of the model",
    ],
    QueryType.COMPARE: [
        "这两篇论文的方法有什么差异",
        "BERT 和 Transformer 有什么不同",
        "对比一下两个模型的优缺点",
        "这两项工作的异同点是什么",
        "A 与 B 的继承关系是什么",
        "哪种方法在长文本上更好",
        "compare the two approaches in terms of efficiency",
        "how does this model differ from the previous one",
    ],
    QueryType.SURVEY: [
        "这个方向有哪些主要方法",
        "综述一下大模型对齐的研究进展",
        "相关工作都有哪些",
        "梳理一下这条技术路线的发展脉络",
        "目前主流方案可以分成几类",
        "give me an overview of existing approaches",
        "what are the main research directions in this area",
    ],
    QueryType.NAV: [
        "这句话出自哪一页",
        "原文里有没有提到 multi-head attention",
        "哪个小节讨论了位置编码",
        "这句话在第几段",
        "论文里是不是说过 label smoothing",
        "where is this sentence located in the paper",
        "which page mentions the warmup schedule",
    ],
    QueryType.CHITCHAT: [
        "你好",
        "谢谢你的帮助",
        "你是谁",
        "你能做什么",
        "怎么使用这个系统",
        "hello there",
        "thanks a lot",
        "what can you do",
    ],
    QueryType.GENERIC: [
        "监督学习是什么意思",
        "解释一下注意力机制",
        "这篇论文讲了什么",
        "为什么会出现过拟合",
        "what is self-attention",
        "explain the transformer architecture",
    ],
}
