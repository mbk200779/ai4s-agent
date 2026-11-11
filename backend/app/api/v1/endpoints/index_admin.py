"""
索引版本管理接口：重建、切流、回滚。

为什么这些接口不放在 /qa 下面：它们改的是"系统读哪一份数据"，
不是某一次问答的参数。混在问答接口里，权限与审计都没地方放。
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.services import index_rebuild, index_version
from app.services.rag.strategies import invalidate_corpus
from app.services.vector_store import VectorStoreService

router = APIRouter(prefix="/index", tags=["索引版本"])


class RebuildRequest(BaseModel):
    target_version: Optional[str] = Field(
        default=None, description="目标版本；留空 = 用当前配置算出来的版本（改完配置直接重建）"
    )
    project_id: Optional[str] = Field(default=None, description="只重建某个课题；留空 = 全库")
    batch_size: Optional[int] = Field(default=None, description="一批编码多少条，影响内存与吞吐")
    background: bool = Field(default=True, description="后台执行；false 时同步跑完再返回（仅测试用）")
    note: str = ""


class RoutingRequest(BaseModel):
    pipeline_version: str
    percent: int = Field(default=0, description="0 = 全量切到该版本；0<p<100 = 灰度")
    stable_version: Optional[str] = Field(default=None, description="灰度外回落到哪一版；留空取当前版本")
    project_id: Optional[str] = Field(default=None, description="给单个课题钉版本")
    note: str = ""


def _summary() -> Dict[str, Any]:
    """
    版本列表 + 当前路由 + 当前配置签名。界面只要这一份就够渲染整个面板。

    先 `ensure_registry`：版本注册是在"第一次有人建 VectorStoreService"时懒执行的，
    而这个面板可能比第一次问答更早被打开（部署完先来看一眼）。
    不在这里兜一下，面板会显示"零个版本"，看起来像功能没生效。
    """
    from qdrant_client import QdrantClient

    index_version.ensure_registry(QdrantClient(url=settings.qdrant_url))

    versions = index_version.list_versions()
    routing = index_version.routing_rows()
    try:
        sig = index_version.current_signature()
        current = index_version.version_of(sig)
    except Exception:
        sig, current = {}, None
    return {
        "versions": versions,
        "routing": routing,
        "signature": sig,
        "current_version": current,
        "write_version": index_version.write_version(),
        "keep_versions": settings.index_keep_versions,
    }


@router.get("/versions")
async def list_versions() -> Dict[str, Any]:
    return _summary()


@router.get("/versions/{pipeline_version}")
async def version_detail(pipeline_version: str) -> Dict[str, Any]:
    try:
        return index_version.detail(pipeline_version)
    except index_version.IndexVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/rebuild")
async def rebuild(req: RebuildRequest) -> Dict[str, Any]:
    """
    创建重建任务。默认丢给 Celery；broker 不可用时退化成进程内线程。

    为什么允许退化而不是直接报错：重建是运维动作，常常发生在"已经在救火"的时候，
    这时候 Redis 挂了还拒绝执行，等于把唯一的修复手段也锁掉了。
    代价是这个任务会跟着 API 进程一起死——所以响应里明确带 `executor` 字段，
    让调用方知道它在哪跑。
    """
    try:
        build = index_rebuild.start_build(
            target_version=req.target_version,
            project_id=req.project_id,
            batch_size=req.batch_size,
            note=req.note,
        )
    except index_version.IndexVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if build.get("reused"):
        return {"build": build, "executor": "existing", "reused": True}

    executor = "inline"
    if req.background:
        executor = "celery"
        try:
            from app.tasks.index_tasks import rebuild_index

            rebuild_index.delay(build["id"])
        except Exception:
            executor = "thread"
            import threading

            threading.Thread(
                target=index_rebuild.run_build, args=(build["id"],),
                name=f"rebuild-{build['id']}", daemon=True,
            ).start()
    else:
        build = index_rebuild.run_build(build["id"])
    return {"build": build, "executor": executor, "stats": index_rebuild.stats(build)}


@router.get("/builds")
async def list_builds(limit: int = Query(default=20, ge=1, le=100)) -> Dict[str, Any]:
    rows = index_rebuild.list_builds(limit)
    return {"builds": [{**b, "stats": index_rebuild.stats(b)} for b in rows]}


@router.get("/builds/{build_id}")
async def build_detail(build_id: str) -> Dict[str, Any]:
    b = index_rebuild.get_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail=f"未知的 build：{build_id}")
    total = int(b.get("total_chunks") or 0)
    done = int(b.get("done_chunks") or 0)
    return {
        "build": b,
        "stats": index_rebuild.stats(b),
        "progress": round(done / total, 4) if total else None,
    }


@router.post("/builds/{build_id}/cancel")
async def cancel(build_id: str) -> Dict[str, Any]:
    try:
        return {"build": index_rebuild.cancel_build(build_id)}
    except index_version.IndexVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/routing")
async def set_routing(req: RoutingRequest) -> Dict[str, Any]:
    try:
        if req.project_id:
            result = index_version.set_project(
                req.project_id, req.pipeline_version, percent=req.percent or 100, note=req.note,
            )
        else:
            result = index_version.set_global(
                req.pipeline_version, percent=req.percent,
                stable=req.stable_version, note=req.note,
            )
    except index_version.IndexVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 切流后立刻丢掉旧语料缓存：不等 60 秒 TTL，
    # 否则"切过去了"与"对比/综述还在用上一版语料"会同时为真。
    invalidate_corpus()
    return {"routing": result, "effective": _summary()["routing"]}


@router.delete("/routing/{project_id}")
async def clear_project_routing(project_id: str) -> Dict[str, Any]:
    index_version.clear_project(project_id)
    invalidate_corpus()
    return {"cleared": project_id, "routing": index_version.routing_rows()}


@router.post("/rollback")
async def rollback(to: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    try:
        result = index_version.rollback(to)
    except index_version.IndexVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    invalidate_corpus()
    return {"routing": result}


@router.delete("/versions/{pipeline_version}")
async def drop_version(pipeline_version: str) -> Dict[str, Any]:
    """
    删掉一个版本的 collection。

    三条拒删：还在服务（global 或任一课题命中它）、是 legacy（它的 collection
    就是基础名，删了等于把版本化之前的数据一起删）、正在构建中。
    删除是**不可逆**的，所以宁可多拦一次。
    """
    row = index_version.get(pipeline_version)
    if not row:
        raise HTTPException(status_code=404, detail=f"未知的版本：{pipeline_version}")
    if row["origin"] == "legacy":
        raise HTTPException(status_code=400, detail="legacy 版本与基础 collection 共用存储，不能删除")
    if row["status"] == "building":
        raise HTTPException(status_code=400, detail="该版本正在构建，先取消再删")
    for r in index_version.routing_rows():
        if r["pipeline_version"] == pipeline_version or r.get("stable_version") == pipeline_version:
            raise HTTPException(
                status_code=400,
                detail=f"该版本仍在切流规则里（{r['scope']}:{r['project_id'] or '全局'}），先切走再删",
            )
    if pipeline_version == index_version.write_version():
        raise HTTPException(status_code=400, detail="该版本是当前写路径，先切流再删")

    collection = row["collection"]
    vs = VectorStoreService(collection=collection, pipeline_version=pipeline_version)
    try:
        vs.client.delete_collection(collection_name=collection)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除 collection 失败：{exc}")
    index_version.update_version(pipeline_version, status="retired", retired_at=index_version.utc_now())
    invalidate_corpus(collection)
    # 登记行保留（status=retired）："这一版存在过、什么时候删的"是审计信息，
    # 删掉行会让历史答案的 provenance 里的版本号变成悬空引用。
    return {"retired": pipeline_version, "collection": collection}
