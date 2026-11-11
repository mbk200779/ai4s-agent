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
    include=["app.tasks.tasks", "app.tasks.ingest_tasks", "app.tasks.index_tasks"],
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
    # ---- 长文档解析的内存护栏 ----
    # 每个子进程跑完 **1 个**任务就换新。
    #
    # 这个值从 8 降到 3 又降到 1，是被实测逼出来的：单篇文档内内存是平的
    # （541 页样张 28 批全程 3.15~3.26GB），但跨文档会台阶式上涨——
    # 做完 BERT 再做 ViT 时子进程已经到 4.4~5.9GB，后面几篇直接被
    # RSS_SOFT_LIMIT_MB 判成失败。原因是 fastembed 的 ONNX arena 与 fitz 缓存
    # 只增不退（malloc 碎片归还不了 OS），这不是代码里少写一句 free 能修的。
    #
    # 代价是每篇文档要重载一次 embedding 模型（约 12s）。批量导入 50 篇多付 10 分钟，
    # 换来的是"每篇都跑在同一个内存基线上"，比"从第 4 篇开始随机失败"可预测得多。
    worker_max_tasks_per_child=1,
    worker_max_memory_per_child=4_600_000,  # KB
    beat_schedule={
        # 每 6 小时拉取 cs.AI 最近 1 天论文
        "arxiv-csai-update": {
            "task": "app.tasks.tasks.fetch_arxiv_category_updates",
            "schedule": 21600.0,
            "args": ("cs.AI", 1, 50),
        },
        # 每 10 分钟巡检一次"开工了但没人管"的解析版本。
        # Redis 作为 broker 时，worker 被强杀会丢掉未 ack 的消息（见 PaperVersionRepository.list_stuck），
        # 所以必须有个独立巡检把孤儿版本按断点重新投递。
        "ingest-recover-stuck": {
            "task": "app.tasks.ingest.recover_stuck",
            "schedule": 600.0,
        },
    },
)


@celery_app.on_after_finalize.connect
def _init_metadata_db(**kwargs):
    """
    worker 进程不会走 FastAPI 的 create_app，也就不会执行 init_db。
    定时追新这类入口第一次写库时会撞上「no such table: projects」，
    所以在这里补一次（建表语句是幂等的）。
    """
    from app.db import init_db

    init_db()

    # 启动即巡检：上次崩溃/被强杀留下的孤儿版本不必等人来点"续跑"
    from app.tasks.ingest_tasks import recover_stuck

    try:
        recover_stuck.delay(older_than_s=30)
    except Exception:
        # broker 暂时不可用不该阻止 worker 启动，等 beat 的定时巡检来兜
        import traceback

        traceback.print_exc()
