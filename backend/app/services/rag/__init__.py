"""
查询理解、检索分流与精排（RAG routing + rerank）。

对外暴露的名字：
  RAGRetriever  —— 分流器 + 规则召回 + 精排，QA 与评测脚本都用它
  QueryRouter   —— 查询理解（规则 + 本地原型分类器）
  QueryPlan     —— 检索计划
  QueryType     —— 五种对外类型 + 一种内部兜底
  Reranker      —— cross-encoder 精排（带降级、缓存与熔断）
  warmup_*      —— 进程启动时的后台预热
"""
from app.services.rag.query import QueryPlan, QueryRouter
from app.services.rag.rerank import Reranker, RerankOutcome, warmup_reranker
from app.services.rag.retrieve import RAGRetriever, RouteResult, retrieve
from app.services.rag.taxonomy import QueryType, label_of, strategy_of


def warmup_router() -> None:
    """
    进程启动后在后台把原型向量建好。

    不预热会怎样：第一条"规则判不准"的问句要么同步等 2 秒建完原型（破 300ms 约束），
    要么这次不用小模型（trace 标 degraded）。两种都不理想。
    预热是后台线程，不阻塞启动；预热完成前到达的请求会自动跳过小模型，
    所以不会出现"请求去等预热"的情况。
    """
    QueryRouter()._warm_prototypes_async()


__all__ = [
    "RAGRetriever",
    "RouteResult",
    "QueryRouter",
    "QueryPlan",
    "QueryType",
    "Reranker",
    "RerankOutcome",
    "label_of",
    "strategy_of",
    "retrieve",
    "warmup_router",
    "warmup_reranker",
]
