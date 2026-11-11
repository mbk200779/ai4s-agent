"""API v1 路由聚合。"""
from fastapi import APIRouter
from app.api.v1.endpoints import (
    papers, qa, qa_stream, rag, graph, review, writing, drawing, reasoning, eval, health, ingest,
    dedupe, index_admin, library, recommend,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(papers.router)
# 去重预检独立成路由：它只读不写，和"真正入库"分开
api_router.include_router(dedupe.router)
api_router.include_router(ingest.router)
api_router.include_router(qa.router)
# 流式出口与一次性 /qa/ask 并存：前者是默认链路，后者是降级与评测基准
api_router.include_router(qa_stream.router)
api_router.include_router(rag.router)
api_router.include_router(graph.router)
api_router.include_router(review.router)
api_router.include_router(writing.router)
api_router.include_router(drawing.router)
api_router.include_router(reasoning.router)
api_router.include_router(eval.router)
# 索引版本管理（重建 / 切流 / 回滚）
api_router.include_router(index_admin.router)
# 论文库管理（清单 / 单篇删除 / 批量删除）
api_router.include_router(library.router)
# 库外推荐（空检索时去 arXiv 找可补进库的论文）
api_router.include_router(recommend.router)
