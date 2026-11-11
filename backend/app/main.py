"""
AI4S 学术研究 Agent 后端入口
技术栈：FastAPI + Uvicorn，向量库 Qdrant，对象存储 MinIO，任务队列 Celery/Redis。
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.api import api_router
from app.core.config import settings
from app.db import init_db

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """
    关键设计决策：应用工厂模式，便于测试与 Celery worker 复用同一套依赖。
    """
    # 建表是幂等的（CREATE TABLE IF NOT EXISTS），放在进程启动时做，
    # 避免部署时漏跑迁移脚本导致接口 500。
    init_db()

    # 原型向量预热：查询理解里"规则判不准"时要拿小模型比一次余弦，
    # 原型句有 48 条，现编码约 1.5~2s。放后台线程做，启动不等它；
    # 预热完成前到达的请求会自动跳过小模型（trace 标 prototype_warming），
    # 所以既不会拖慢启动，也不会让请求去等预热。
    try:
        from app.services.rag import warmup_router

        warmup_router()
    except Exception as exc:  # 预热失败不阻断启动，分流会自动降级为纯规则
        logger.warning("router warmup skipped: %s", exc)

    # 精排模型预热：cross-encoder 首次加载要建 ONNX 会话（几秒），
    # 不预热的话第一条需要精排的请求会撞上超时预算直接降级，
    # 等于把这一步的效果浪费在冷启动上。同样是后台线程，启动不等。
    try:
        from app.services.rag import warmup_reranker

        warmup_reranker()
    except Exception as exc:
        logger.warning("rerank warmup skipped: %s", exc)

    # 语义核验模型预热（07）：多语言 MiniLM 首次加载要读盘建 ONNX 会话（~50s），
    # 不预热就等于第一条问答题挂在模型加载上。同样后台线程，且失败只降级
    # （定档退回纯字面分），不影响启动。
    try:
        import threading

        from app.services.rag import semantic

        threading.Thread(target=semantic.warm, name="semantic-warmup", daemon=True).start()
    except Exception as exc:
        logger.warning("semantic warmup skipped: %s", exc)

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

    # 索引版本号写错（比如显式指定了 idx-2.0.0-xxxxxxxx 而它不存在）是**调用方**的错误，
    # 不是服务端故障。统一转 400：否则问答接口会因为一个拼错的版本号返回 500，
    # 而 500 在监控里看起来像"检索挂了"，排查方向会被带偏。
    from app.services.index_version import IndexVersionError

    @app.exception_handler(IndexVersionError)
    async def _index_version_error(request, exc):  # noqa: ANN001
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


app = create_app()


@app.get("/")
async def root():
    return {"message": "AI4S Academic Agent API", "docs": "/docs"}
