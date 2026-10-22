"""
Celery 应用配置：长文档解析与 arXiv 追新均走异步队列，不阻塞请求。
"""
from celery import Celery
from app.core.config import settings

# 关键设计决策：使用 Redis 作为 broker 与 backend，任务结果保留 1 小时，
# 避免结果堆积；任务默认 acks_late=True，worker 崩溃时可重新派发。
celery_app = Celery(
    "ai4s_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.tasks"],
)

celery_app.conf.update(
    result_expires=3600,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        # 每 6 小时拉取 cs.AI 最近 1 天论文
        "arxiv-csai-update": {
            "task": "app.tasks.tasks.fetch_arxiv_category_updates",
            "schedule": 21600.0,
            "args": ("cs.AI", 1, 50),
        },
    },
)
