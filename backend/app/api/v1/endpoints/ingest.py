"""
解析进度查询与断点续跑。

为什么同时提供轮询与 SSE：
- 轮询（GET .../progress）简单、无状态，任何代理/网关都穿得过去，
  前端 tab 切回来时补一次就能对齐，是默认方案；
- SSE（GET .../events）在页面稳定停留时更省请求，也能立刻感知阶段变化。
两者读的是同一份 SQLite 快照，所以不存在"两种口径"的问题。
"""
import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.repositories import PaperVersionRepository
from app.services.ingest import progress as progress_service
from app.services.ingest.limits import PROGRESS_POLL_MS
from app.tasks.ingest_tasks import enqueue

router = APIRouter(prefix="/ingest", tags=["解析进度"])

# 心跳间隔。解析一批 20 页要 15~30 秒，这段时间快照不变、没有 data 可推；
# 客户端只能靠「多久没收到消息」判活，没有心跳就会把正常静默当成断线。
HEARTBEAT_SECONDS = 10.0


@router.get("/tasks/{task_id}/progress")
async def get_task_progress(task_id: str):
    """
    按 task_id 查进度。上传响应里返回的就是这个 id，前端拿到后直接轮询。
    """
    prog = progress_service.load_by_task(task_id)
    if not prog:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return prog.to_dict()


@router.get("/versions/{version_id}/progress")
async def get_version_progress(version_id: str):
    """
    按 version_id 查进度。用于"刷新页面后重新挂上正在跑的那个任务"，
    以及上传秒回后前端只知道版本、还没记住 task_id 的窗口期。
    """
    prog = progress_service.load_by_version(version_id)
    if not prog:
        raise HTTPException(status_code=404, detail=f"版本不存在: {version_id}")
    return prog.to_dict()


@router.get("/tasks/{task_id}/events")
async def stream_task_progress(task_id: str, max_seconds: int = 1800):
    """
    SSE 推流：服务端每 PROGRESS_POLL_MS 读一次快照，有变化就推，跑到终态自动收尾。

    超时（默认 30 分钟）也会主动结束连接，避免用户关掉页面后服务端还挂着协程。
    """
    if not progress_service.load_by_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    async def event_stream():
        last = None
        waited = 0.0
        last_emit = 0.0
        step = PROGRESS_POLL_MS / 1000.0
        while waited < max_seconds:
            prog = progress_service.load_by_task(task_id)
            if prog is None:
                yield "event: error\ndata: {\"detail\":\"任务已不存在\"}\n\n"
                return
            payload = prog.to_dict()
            snapshot = json.dumps(payload, ensure_ascii=False)
            if snapshot != last:
                yield f"data: {snapshot}\n\n"
                last = snapshot
                last_emit = waited
            if payload["done"]:
                yield "event: done\ndata: {}\n\n"
                return
            # 心跳：只证明「连接还活着」，不携带数据，客户端据此更新活跃时间，
            # 于是只有真正断线才会被判定为断线。顺带避免代理按空闲超时切连接。
            if waited - last_emit >= HEARTBEAT_SECONDS:
                yield "event: ping\ndata: {}\n\n"
                last_emit = waited
            await asyncio.sleep(step)
            waited += step
        yield "event: timeout\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 不缓冲，否则 SSE 会被攒成一坨
        },
    )


@router.post("/versions/{version_id}/resume")
async def resume_ingest(version_id: str):
    """
    失败/中断后手动续跑。

    关键点：reset_for_retry 会清掉错误与终态，但**保留 parse_cursor**，
    所以这里是从断点继续，而不是把 541 页从头再解析一遍。
    """
    version = PaperVersionRepository.get(version_id)
    if not version:
        raise HTTPException(status_code=404, detail=f"版本不存在: {version_id}")

    if version.status == "indexed" and version.stage in ("indexed", "reused"):
        prog = progress_service.load_by_version(version_id)
        return {
            "version_id": version_id,
            "status": "indexed",
            "resumed": False,
            "message": "该版本已完成解析，无需续跑",
            "progress": prog.to_dict() if prog else None,
        }

    PaperVersionRepository.reset_for_retry(version_id)
    task_id = enqueue(version_id)
    return {
        "version_id": version_id,
        "status": "queued",
        "resumed": True,
        "resume_from_page": version.parse_cursor,
        "task_id": task_id,
    }
