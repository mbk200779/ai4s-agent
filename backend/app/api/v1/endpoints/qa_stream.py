"""
流式问答出口（SSE）。

为什么是 SSE 而不是 WebSocket：
- 方向只有一个（服务端 → 浏览器），WebSocket 的全双工在这里是白付的复杂度；
- SSE 天生能穿反代（只要关掉缓冲），断线由浏览器自己重连；
- 用 POST + fetch 读流而不是 EventSource：EventSource 只支持 GET，
  问题文本要放 query string，既不好编码又会进访问日志。

事件帧格式：
    id: <seq>
    event: <stage|retrieval|token|claim|done|error>
    data: <一行 JSON，必带 trace_id>

心跳是 `: ping` 注释行——它不触发浏览器事件，只用于让反代/隧道认为连接还活着。

另外提供 `GET /qa/stream/{trace_id}`：把归档的事件日志原样读回来。
答辩时"重放这条链路"就是调这个接口，而不是再去问一次模型（模型不可复现，
事件日志才是当时到底发生了什么）。
"""
import json
from typing import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.schemas.qa import QARequest
from app.services.qa_stream_service import QAStreamService
from app.services.storage import StorageService

router = APIRouter(prefix="/qa", tags=["问答·流式"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx 默认会攒够 4KB 再发，流式会变成"一次性"。这条头是关掉它的开关。
    "X-Accel-Buffering": "no",
}


def _frame(event: str, data: dict) -> str:
    """一条 SSE 消息。data 必须是单行 JSON，换行会截断帧。"""
    body = json.dumps(data, ensure_ascii=False)
    return f"id: {data.get('seq', 0)}\nevent: {event}\ndata: {body}\n\n"


@router.post("/stream")
async def ask_stream(req: QARequest):
    """
    结构化生成协议：stage → retrieval → token×N → claim×N → done。

    这段是**同步生成器**交给 StreamingResponse：Starlette 会把它放进线程池逐条迭代，
    所以里面的阻塞调用（HTTP 模型、ONNX）不会占住事件循环——
    并发 5 个流式请求时，聊天页的其它接口照常响应。

    验收标准：
    1. 每个阶段转换都有 stage 事件，且 ms 是真实测量值（不是按时长比例估算）；
    2. token 流里不出现 `[引#N]`，角标只在 claim 事件里出现；
    3. 证据不足的 claim 也发（status=unsupported + reason），不删；
    4. 所有事件带同一个 trace_id，seq 从 1 连续递增；
    5. 结束时 done 事件带 trace_key，可据此回放整个过程。
    """
    if not settings.stream_enabled:
        raise HTTPException(status_code=503, detail="流式问答已关闭（STREAM_ENABLED=false）")

    service = QAStreamService()

    def gen() -> Iterator[str]:
        try:
            for kind, payload in service.stream(
                req.question,
                top_k=req.top_k,
                rrf_k=req.rrf_k,
                force_type=req.force_type,
                rerank=req.rerank,
                numeric=req.numeric,
                project_id=req.project_id,
                index_version=req.index_version,
            ):
                if kind == "__heartbeat__":
                    yield ": ping\n\n"
                    continue
                yield _frame(kind, payload)
        except Exception as exc:  # noqa: BLE001
            # 走到这里说明连服务层都没兜住（例如客户端断开后生成器被 close）。
            # 仍然发一条 error，让前端能把"流断了"和"答案很短"区分开。
            yield _frame(
                "error",
                {"trace_id": None, "seq": 0, "message": f"{type(exc).__name__}: {exc}"[:400]},
            )

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/stream/{trace_id}")
async def replay_stream(trace_id: str):
    """
    回放一次流式对话的完整事件日志（按 seq 原样返回）。

    轨迹是归档下来的**事实**：阶段耗时、精排是否降级、几个角标被摘掉、
    哪句判了 unsupported——这些都不在最终答案里，只在事件流里。
    """
    if not trace_id.startswith("tr_"):
        raise HTTPException(status_code=400, detail="trace_id 格式不正确")
    storage = StorageService()
    # key 里有日期，逐个候选日期找太笨；对象存储支持前缀列举，但这里只需要"最近"，
    # 所以用当天与前一天的候选路径（一次对话不可能跨更久的日期）。
    from datetime import datetime, timedelta

    tried = []
    for delta in (0, 1):
        day = datetime.utcnow() - timedelta(days=delta)
        key = f"qa/stream/{day.strftime('%Y/%m/%d')}/{trace_id}.jsonl"
        tried.append(key)
        try:
            raw = storage.get_object_bytes(storage.provenance_bucket, key)
        except Exception:
            continue
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        return {"trace_id": trace_id, "key": key, "count": len(events), "events": events}
    raise HTTPException(status_code=404, detail=f"未找到该 trace 的事件日志（尝试过 {tried}）")
