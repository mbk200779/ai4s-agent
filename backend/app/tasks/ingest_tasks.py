"""
长文档异步解析任务。

与旧的 parse_and_index_pdf 相比，这里**没有 chord**：
旧实现按 30 页拆成 18 个并行子任务再用 chord 合并，有两个致命问题：
1. chord 的中间结果要经过 broker（Redis），每个子任务都把该段的全文和
   全部页面图塞进返回值，541 页的一次解析能让 Redis 多出几百 MB 的临时键；
2. 并行拆页之后进度无主——18 个子任务各跑各的，"现在到第几页"没有单一答案，
   断点也无处安放（子任务粒度是页范围，重启后不知道哪些范围已完成）。

改成单任务顺序批处理后，进度、断点、内存三者都变成一行行的确定事实。
并行度由"同时处理几个文档"来提供（调 worker 的 -c），而不是"把一本书切开"。
"""
import traceback

from app.repositories import PaperVersionRepository
from app.services.ingest.limits import MAX_ATTEMPTS
from app.services.ingest.pipeline import IngestMemoryError, run_ingest, sweep_work_dirs
from app.tasks.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="app.tasks.ingest.ingest_document",
    # Celery 的重试次数比业务上限少 1：第一次执行不算重试
    max_retries=MAX_ATTEMPTS - 1,
    default_retry_delay=20,
    # 软超时给足：541 页在 CPU 上跑 embedding 需要几分钟；
    # 硬超时留一点余量，主要用来兜住"卡在网络/模型上"的僵死任务。
    soft_time_limit=3600,
    time_limit=4200,
)
def ingest_document(self, version_id: str, force: bool = False) -> dict:
    """
    解析一个版本。可重入：重复投递不会产生重复数据（游标 + 幂等 upsert）。
    """
    try:
        return run_ingest(version_id, task_id=self.request.id, force=force)
    except IngestMemoryError as exc:
        # 内存超限已被 pipeline 记成终态，这里不再重试
        return {"version_id": version_id, "status": "failed", "error": str(exc)}
    except Exception as exc:
        traceback.print_exc()
        current = PaperVersionRepository.get(version_id)
        if current and current.status == "failed":
            # 已到尝试上限，pipeline 已写终态，重试没有意义
            return {
                "version_id": version_id,
                "status": "failed",
                "error": current.error_message,
                "attempt": current.attempt,
            }
        if self.request.retries >= self.max_retries:
            PaperVersionRepository.mark_failed(
                version_id, f"{type(exc).__name__}: {exc}", traceback.format_exc()
            )
            return {"version_id": version_id, "status": "failed", "error": str(exc)}
        raise self.retry(exc=exc)


def enqueue(version_id: str, force: bool = False) -> str:
    """
    投递解析任务并回写 task_id。
    task_id 由 Celery 生成，必须落库——进度查询是按 task_id 查的，
    而用户手里只有上传响应里的那个 id。
    """
    result = ingest_document.delay(version_id=version_id, force=force)
    PaperVersionRepository.advance(version_id, task_id=result.id, stage="queued")
    return result.id


@celery_app.task(name="app.tasks.ingest.recover_stuck")
def recover_stuck(older_than_s: int = 90) -> dict:
    """
    巡检：把"开工了但没人管"的版本重新投递。

    触发点有两个——worker 启动时立刻跑一次，以及 beat 每 10 分钟跑一次。
    两者都需要：启动时跑能救回上次崩溃留下的孤儿；
    定时跑能救回"任务被 broker 丢掉但 worker 还活着"的情况（比如 Redis 重启）。

    重复投递是安全的：ingest_document 幂等，且从 parse_cursor 继续。

    顺手扫一遍工作目录：被 SIGKILL 或版本行已删的版本会留下 raw.pdf，
    几十 MB 一个，不扫就只增不减。
    """
    sweeps = sweep_work_dirs(keep_s=3600)
    if sweeps:
        print(f"[ingest] 清理孤儿工作目录：{', '.join(sweeps)}", flush=True)

    stuck = PaperVersionRepository.list_stuck(older_than_s=older_than_s)
    recovered = []
    for version in stuck:
        task_id = enqueue(version.id, force=False)
        recovered.append(
            {"version_id": version.id, "resume_from_page": version.parse_cursor, "task_id": task_id}
        )
        print(
            f"[ingest] 巡检接管孤儿版本 {version.id}：从第 {version.parse_cursor} 页续跑",
            flush=True,
        )
    return {"stuck": len(stuck), "recovered": recovered, "swept": sweeps}
