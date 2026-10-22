"""API v1 路由聚合。"""
from fastapi import APIRouter
from app.api.v1.endpoints import papers, qa, graph, review, writing, drawing, reasoning, eval, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(papers.router)
api_router.include_router(qa.router)
api_router.include_router(graph.router)
api_router.include_router(review.router)
api_router.include_router(writing.router)
api_router.include_router(drawing.router)
api_router.include_router(reasoning.router)
api_router.include_router(eval.router)
