"""
索引重建的 Celery 入口。

单独一个文件而不是塞进 `tasks.tasks`：那边的任务是"入库/追新"，
worker 配置里 `worker_max_tasks_per_child=1` 是给长文档解析定的；
重建同属重任务，放同一个队列即可，但入口分开后日志里一眼能看出
"这次重启是解析导致的还是重建导致的"。
"""
from app.tasks.celery_app import celery_app


@celery_app.task(name="app.tasks.index.rebuild", bind=True)
def rebuild_index(self, build_id: str) -> dict:
    """
    执行或续跑一次重建。**幂等**：同一个 build_id 重复投递不会重复写点
    （点 ID 由 chunk 派生，upsert 是覆盖），也不会把游标推错
    （游标只在批次成功后前进，且只跟着 build 行走）。
    """
    from app.services import index_rebuild

    return index_rebuild.run_build(build_id) or {}
