"""
检索分流接口：只看"问题怎么被理解、被派去哪条策略、召回了什么"，不生成答案。

为什么单独开一个端点：
1. 评测分流效果时必须把 LLM 摘掉——否则答案的随机性会盖住检索差异，
   根本分不清"召回变好了"还是"这次模型答得巧"；
2. 前端调试面板要能直接看 trace，不必为一个"解释"功能付一次 LLM 的钱；
3. 五条策略可以在同一条问题上并排跑（`force_type`），对比它们各自召回了什么。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from app.services.rag import RAGRetriever
from app.services.rag import numeric_rules as N
from app.services.rag import rules as R
from app.services.rag.rerank import Reranker, cache_stats, clear_cache
from app.services.rag.taxonomy import (
    NUMERIC_CELL_MULT,
    NUMERIC_RECALL_LIMIT,
    NUMERIC_REFERENCE_PENALTY,
    QueryType,
    TYPE_LABELS,
)

router = APIRouter(prefix="/rag", tags=["检索分流"])

_VALID_TYPES = {t.value for t in QueryType}


class RouteRequest(BaseModel):
    question: str = Field(..., description="用户问题")
    # 实际拿去检索的文本（中文问题通常是英文译文）。不传则同 question。
    # 单独暴露出来是为了复现：中文原句 + 译文 与 只用译文 的召回结果不一样，
    # 评测脚本能传，接口也应该能传，否则线上问题和评测结果对不上。
    search_text: Optional[str] = Field(default=None, description="检索文本（默认同 question）")
    top_k: int = Field(default=8, description="返回片段数")
    rrf_k: int = Field(default=60, description="RRF 参数")
    force_type: Optional[str] = Field(
        default=None, description="强制类型，用于并排对比五条策略"
    )
    # A/B 开关：留空跟随配置。评测脚本靠它跑"精排前 / 精排后"两遍。
    rerank: Optional[bool] = Field(default=None, description="是否精排，留空跟随 RERANK_ENABLED")
    numeric: Optional[bool] = Field(default=None, description="是否走数值规则召回，留空跟随配置")


class RouteHit(BaseModel):
    chunk_id: str
    arxiv_id: str
    title: str
    kind: str
    page: int
    section: str
    score: float
    raw_score: Optional[float] = None
    rerank_score: Optional[float] = None
    hit_source: Optional[str] = None
    text: str
    snippet: Optional[str] = None


class RouteResponse(BaseModel):
    routing: Dict[str, Any]
    hits: List[RouteHit]


@router.post("/route", response_model=RouteResponse)
async def route_question(req: RouteRequest):
    """
    只做查询理解 + 检索分流，返回 trace 与命中片段。
    不调用大模型，因此结果完全可复现——评测脚本靠它做分流前后的对比。
    """
    if req.force_type and req.force_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"未知类型 {req.force_type}，可选：{sorted(_VALID_TYPES)}",
        )
    retriever = RAGRetriever()
    result = retriever.route(
        req.question, search_text=req.search_text, top_k=req.top_k, rrf_k=req.rrf_k,
        force_type=req.force_type, rerank=req.rerank, numeric=req.numeric,
    )
    hits = [
        RouteHit(
            chunk_id=r.chunk_id,
            arxiv_id=r.arxiv_id,
            title=r.title,
            kind=r.kind,
            page=r.page,
            section=r.section,
            score=round(r.score, 4),
            raw_score=round(r.raw_score, 6) if r.raw_score is not None else None,
            rerank_score=r.rerank_score,
            hit_source=r.hit_source,
            text=(r.text or "")[:1200],
            snippet=r.snippet,
        )
        for r in result.results
    ]
    return RouteResponse(routing=result.trace, hits=hits)


@router.get("/rules")
async def list_rules():
    """
    规则表与原型句清单。给文档、前端调试面板和"分类器到底写了哪些判据"这个问题用。
    """
    return {
        "types": TYPE_LABELS,
        "rules": [
            {
                "id": r["id"],
                "type": r["type"],
                "weight": r["weight"],
                "note": r["note"],
                "pattern": r["pattern"],
            }
            for r in R.RULES
        ],
        "prototypes": {k: len(v) for k, v in R.PROTOTYPES.items()},
        "prototype_samples": R.PROTOTYPES,
    }


@router.get("/numeric-rules")
async def list_numeric_rules():
    """
    数值/公式规则召回表（本步的"期望输出 ②"）。

    单独开一个只读端点，而不是写死在文档里：规则表是代码里的数据，
    文档会过期，端点不会。前端调试面板也直接读它。
    """
    return {
        "rules": N.describe_rules(),
        "cell_mult": NUMERIC_CELL_MULT,
        "reference_penalty": NUMERIC_REFERENCE_PENALTY,
        "recall_limit": NUMERIC_RECALL_LIMIT,
    }


@router.get("/rerank/status")
async def rerank_status():
    """精排开关、模型加载状态、缓存水位与熔断剩余时间。运维与 A/B 前的自检用。"""
    return Reranker.status()


@router.post("/rerank/cache/clear")
async def rerank_cache_clear():
    """清空精排缓存。改完语料（又没换快照）想立刻看效果时用。"""
    return {"cleared": clear_cache(), **(cache_stats())}
