"""
AI4S 学术研究 Agent 后端入口
技术栈：FastAPI + Uvicorn，向量库 Qdrant，对象存储 MinIO，任务队列 Celery/Redis。
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.api import api_router
from app.core.config import settings


def create_app() -> FastAPI:
    """
    关键设计决策：应用工厂模式，便于测试与 Celery worker 复用同一套依赖。
    """
    app = FastAPI(
        title="AI4S Academic Agent",
        description="面向科研人员的可验证、可复现 AI4S 学术研究 Agent",
        version=settings.prompt_version,
    )

    # 允许前端跨域访问；云端部署时可收窄为具体域名
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    return app


app = create_app()


@app.get("/")
async def root():
    return {"message": "AI4S Academic Agent API", "docs": "/docs"}
