"""
查询理解与检索分流：类型定义、策略元信息与全局参数。

代码范围里写的是 `services/rag/query.ts` / `retrieve.ts`，本项目是 Python 栈
（FastAPI + Celery），所以按 `services/rag/query.py` / `retrieve.py` 等价落地，
文件名与职责一一对应。

设计要点：**六种类型，五种对外可见**。
point / compare / survey / nav / chitchat 是需求里的五类；
`generic` 是内部兜底类，不对用户暴露——它对应的策略就是分流之前那套
「dense + sparse + RRF」，等价于旧路径。有了它，"分类器判不出来"或
"判错但置信度低"就退回原行为，而不是被硬塞进某个会改变召回形态的策略里。
这条兜底是「不得因为分流导致原有单篇问答效果下降」的工程保证。
"""
from enum import Enum
from typing import Any


class QueryType(str, Enum):
    """查询类型。前五种对外可见，generic 为兜底。"""

    POINT = "point"
    COMPARE = "compare"
    SURVEY = "survey"
    NAV = "nav"
    CHITCHAT = "chitchat"
    GENERIC = "generic"


TYPE_LABELS: dict[str, str] = {
    QueryType.POINT: "点查",
    QueryType.COMPARE: "跨文对比",
    QueryType.SURVEY: "综述",
    QueryType.NAV: "原文定位",
    QueryType.CHITCHAT: "闲聊",
    QueryType.GENERIC: "通用兜底",
}


# ---------------- 分类器的预算与阈值 ----------------

# 硬约束：分类（含小模型）总耗时不得超过 300ms。
# 这里的 300 是"包括取 embedding 在内"的墙钟时间，不是模型本身的时间。
CLASSIFY_BUDGET_MS = 300

# 小模型（本地 embedding 原型分类器）单独的时间预算。
# 留 100ms 给前面的规则和后面的组装，超了就只保留规则结果。
PROTOTYPE_BUDGET_MS = 200

# 规则置信度高于此值就不再动小模型：规则能判准的，没必要再多花 20~40ms。
STRONG_CONF = 0.82

# 规则 + 小模型都低于此值 → 退 generic（不改行为）。
MIN_CONF = 0.45

# 规则一条都没命中、结论完全由小模型给出时的额外门槛。
# 没有规则"背书"的判断必须更硬：相似度够高、且明显领先第二名，才允许改检索行为。
# 否则一句"这篇论文的预训练任务是什么"会被原型里"这两篇有什么不同"的句子带成对比题。
PROTO_ONLY_MIN_SIM = 0.80
PROTO_ONLY_MIN_MARGIN = 0.02

# LLM 兜底分类默认关闭。实测一次 JSON 分类要 400~900ms，远超 300ms 预算，
# 一旦打开就是"每个模糊问句都慢 1 秒"。留着开关是给本地部署小模型（vLLM 等）的，
# 那种环境 20ms 就能返回。生产默认走规则 + 本地原型分类器。
ROUTER_LLM_TIEBREAK = False
ROUTER_LLM_TIMEOUT_MS = 900

# 拒答阈值（RRF 原始分）：与分流前一致。0.03 的实际含义是"最佳片段必须被两路同时召回"，
# 因为 RRF(60) 下两路都命中约 2/61≈0.033，只中一路约 0.016。
# 各策略的权重分配不同，阈值按权重和缩放，语义保持不变（见 strategies.weighted_threshold）。
RRF_MIN_SCORE = 0.03


# ---------------- 各策略的默认参数 ----------------

# point：表格与实验设置段优先，稀疏(BM25)权重大于稠密。
POINT_SPARSE_WEIGHT = 0.62        # 稀疏分在融合里的权重（其余给稠密）
POINT_TABLE_BOOST = 0.15          # kind=table 加成（弱提示：附录超参表几乎都是表格）
POINT_VALUE_ASSIGN_BOOST = 0.50   # 片段里出现"参数名 ≈ 具体值"（最强信号）
POINT_EXPERIMENT_BOOST = 0.30     # 命中"实验设置/超参"内容特征
POINT_NUMBER_BOOST = 0.12         # 片段里出现问句点名的那个数值
POINT_REFERENCE_PENALTY = 0.35    # 参考文献/附录片段降权
POINT_CHART_BOOST = 0.60          # 问题点名了某个图表编号，且片段含该编号
POINT_CANDIDATES = 40             # 每路先取多少候选再重排

# compare：多跳。先定位论文，再逐篇受限召回，最后成对输出。
COMPARE_MAX_PAPERS = 4
COMPARE_PER_PAPER_MIN = 2         # 每篇至少给几条，保证 LLM 能看到双方
COMPARE_PAIRWISE = True           # 输出按 A1,B1,A2,B2 交错，避免"先看完 A 再看 B"

# survey：广度召回 + 去重，聚类交给步骤 08。
SURVEY_BREADTH = 4                # 召回量 = top_k * 4
SURVEY_PER_SECTION_CAP = 2        # 同一篇同一节最多留几条
SURVEY_OUTPUT_FACTOR = 2          # 输出量 = top_k * 2

# nav：全文精确匹配。
NAV_SCAN_LIMIT = 5000             # 全量扫描的片段上限，超了退化为"稀疏召回 + 字面校验"
NAV_MIN_MATCH_CHARS = 8           # 字面命中的最短长度，太短（如 "the"）没有区分度
NAV_CONTEXT_CHARS = 120           # 命中位置前后各取多少字符作为 matched_snippet

# ---------------- 精排（cross-encoder） ----------------
# 融合召回负责"都进来了"，精排负责"排得对"。
# N=20 → cross-encoder → 前 5，是从经验里定下来的：
# 候选少于 20 时精排没有腾挪空间（好片段本来就在前 3，改不动）；
# 多于 20 时延迟线性上涨而 Recall 基本不再提升（见 2.9 文档的候选数实验）。
RERANK_TOP_N = 20                 # 融合侧取多少条候选进精排
RERANK_TOP_K = 5                  # 精排结论取前几条
RERANK_TIMEOUT_MS = 1500          # 单次精排的墙钟预算，超时即降级（右尾样本实测 1200ms）
RERANK_CACHE_SIZE = 512           # 精排缓存条数（key = query_hash + index_snapshot_id）
RERANK_CACHE_TTL_S = 900
RERANK_COOLDOWN_S = 30            # 超时/失败后的熔断窗口，避免每条请求都白等一次
# 精排的排序目标随策略变：点查/定位是"找那一条"，对比/综述是"每篇都别丢"。
RERANK_MODE_BY_TYPE: dict[str, str] = {
    QueryType.POINT: "flat",
    QueryType.NAV: "flat",
    QueryType.GENERIC: "flat",
    QueryType.COMPARE: "coverage",
    QueryType.SURVEY: "coverage",
}

# ---------------- 数值/公式规则召回 ----------------
NUMERIC_RECALL_LIMIT = 8          # 规则命中最多注入几条，多了会冲淡融合候选
NUMERIC_CELL_MULT = 1.6           # 命中表格单元格形态时的系数（形态是相关性证据，不是类型偏好）
NUMERIC_REFERENCE_PENALTY = 0.5   # 参考文献片段里同样的串是引用别人的数据，按比例打折
# 规则召回只允许在"融合候选已出现的论文"内部找证据。
# 符号是多义的（β2 在 Transformer 是 0.98、在光纤论文是 −20 ps/nm/km），
# 关掉它 = 允许跨论文注入，多论文语料下会把别篇的数值当成本文结论。
NUMERIC_PAPER_SCOPE = True

# 语料画像（每篇论文的标题 + 代表性正文）缓存时间。
# 对比/综述/定位三条策略都要用它（实体消解、覆盖度统计、全量扫描），
# 所以按模块级缓存复用，别每条请求都 scroll 一遍。
CORPUS_CACHE_TTL_S = 60


# ---------------- 策略元信息（写进 trace，前端直接显示） ----------------

STRATEGY_META: dict[str, dict[str, Any]] = {
    QueryType.POINT: {
        "name": "point_boost_sparse",
        "label": "表格与实验设置段优先 + 稀疏召回加权",
    },
    QueryType.COMPARE: {
        "name": "compare_multi_hop",
        "label": "多跳：先定位各文对应章节，再成对召回",
    },
    QueryType.SURVEY: {
        "name": "survey_breadth",
        "label": "广度召回 + 按论文/章节去重（聚类交给步骤 08）",
    },
    QueryType.NAV: {
        "name": "nav_exact_match",
        "label": "全文精确匹配 + 段落定位",
    },
    QueryType.CHITCHAT: {
        "name": "chitchat_direct",
        "label": "不检索，直接作答",
    },
    QueryType.GENERIC: {
        "name": "legacy_rrf",
        "label": "分流前行为：dense + sparse + RRF（兜底）",
    },
}


def label_of(query_type: str) -> str:
    return TYPE_LABELS.get(query_type, str(query_type))


def strategy_of(query_type: str) -> dict[str, Any]:
    return STRATEGY_META.get(query_type, STRATEGY_META[QueryType.GENERIC])
