"""
索引重建：把库里已有的 chunk 重新 embedding 进一个新版本的 collection。

为什么能"不重灌"：chunk 的**文本**在关系库（`chunks.text`），向量只是它的一个
派生视图。换切分策略要重解析，但换 embedding 模型、换送进模型的截断长度，
只需要把同一批文本重新编码一遍——这是分钟级，不是小时级。

三条硬要求以及它们的实现位置：

- **可中断**：每处理完一批就落一次游标（`index_builds.cursor_version/offset`）。
  中断点落在"向量已写、游标未落"之间，重跑会重做那一批——点 ID 由
  (arxiv_id, version, kind, chunk_index) 派生，重写是幂等的，不会产生重复点。
- **可续跑**：续跑只认游标，不认"已完成多少条"。累计量推不出下一批该读哪儿。
- **重建期间查询不受影响**：写入的是一个**新建的** collection，
  查询仍然读旧的那个。两者互不相干，切流之前新版本对线上完全不可见。
"""
from __future__ import annotations

import resource
import time
from typing import Any, Callable

from app.core.config import settings
from app.db import dump_json, load_json, new_id, reader, transaction, utc_now
from app.repositories import ChunkRepository
from app.services import index_version
from app.services.vector_store import VectorStoreService

# DB 里存的是"展示类型"，索引 payload 里存的是"片段类型"。二者是双射但不同名，
# 因为 `chunks.chunk_type` 是给前端选渲染方式的（text/table/figure/…），
# 而 `payload.kind` 是给检索与未识别判定用的（paragraph/table/…）。
# 反查时漏了这一步，重建出来的 payload 会让 figure 段落退化成 paragraph——
# 症状是"表格未识别"提示在重建后全部消失，且没有任何报错。
KIND_FROM_TYPE = {
    "text": "paragraph",
    "table": "table",
    "figure": "figure",
    "formula": "formula",
    "reference": "reference",
}

DEFAULT_BATCH = 64
_ACTIVE = ("queued", "running", "cancelling")


# ---------------------------------------------------------------------------
# 资源观测
# ---------------------------------------------------------------------------

def _peak_rss_mb() -> float | None:
    """
    进程峰值 RSS（VmHWM），不是当前占用。

    重建这类任务看"现在多少"没有意义：峰值出现在某一次最大批次的编码中间，
    事后回读早就掉下来了。VmLib 读 VmHWM 不需要 psutil，容器里也拿得到。
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        return None
    return None


def _cpu_s() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return round(usage.ru_utime + usage.ru_stime, 2)


# ---------------------------------------------------------------------------
# build 行
# ---------------------------------------------------------------------------

def _update(build_id: str, **fields: Any) -> None:
    allowed = {
        "status", "cursor_version", "cursor_offset", "total_chunks", "done_chunks",
        "failed_chunks", "points_written", "batch_size", "elapsed_s", "peak_rss_mb",
        "cpu_s", "error_message", "options", "finished_at",
    }
    cols = [(k, v) for k, v in fields.items() if k in allowed]
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k, _ in cols)
    vals = [v if not isinstance(v, (dict, list)) else dump_json(v) for _, v in cols]
    with transaction() as c:
        c.execute(
            f"UPDATE index_builds SET {sets}, updated_at = ? WHERE id = ?",
            (*vals, utc_now(), build_id),
        )


def get_build(build_id: str) -> dict[str, Any] | None:
    with reader() as c:
        row = c.execute("SELECT * FROM index_builds WHERE id = ?", (build_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["options"] = load_json(out.get("options"), {})
    return out


def list_builds(limit: int = 20) -> list[dict[str, Any]]:
    with reader() as c:
        rows = c.execute(
            "SELECT * FROM index_builds ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["options"] = load_json(item.get("options"), {})
        out.append(item)
    return out


def _find_active(target: str, project_id: str | None) -> dict[str, Any] | None:
    """同一个目标版本已有在跑的构建时直接复用它，避免两个任务写同一个 collection。"""
    with reader() as c:
        rows = c.execute(
            f"SELECT id FROM index_builds WHERE target_version = ? "
            f"AND (project_id IS ? OR project_id = ?) AND status IN ({','.join('?' * len(_ACTIVE))}) "
            f"ORDER BY created_at DESC LIMIT 1",
            (target, project_id, project_id, *_ACTIVE),
        ).fetchone()
    return get_build(rows["id"]) if rows else None


# ---------------------------------------------------------------------------
# 作用域
# ---------------------------------------------------------------------------

def _scope(project_id: str | None) -> list[dict[str, Any]]:
    """
    要重建哪些版本的 chunk。count 用真实行数而不是 `paper_versions.chunk_count`：
    后者是解析时累加出来的，重试/续跑过的文档有可能偏大，而重建的进度分母
    一旦错了，进度条会先跑满再继续涨。
    """
    sql = """
        SELECT pv.id AS version_id, pv.version_label, p.arxiv_id, p.title,
               COUNT(c.id) AS n
          FROM paper_versions pv
          JOIN papers p ON p.id = pv.paper_id
          LEFT JOIN chunks c ON c.version_id = pv.id
         WHERE pv.status = 'indexed'
    """
    args: list[Any] = []
    if project_id:
        sql += " AND p.project_id = ?"
        args.append(project_id)
    sql += " GROUP BY pv.id HAVING n > 0 ORDER BY p.created_at, pv.version_label"
    with reader() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _paragraph_ordinals(version_id: str) -> dict[int, int]:
    """
    chunk_index → 该页内第几段。

    `chunks` 表里没有 paragraph 列（它只在索引 payload 里有），
    而溯源界面显示的是"第 3 页第 2 段"。重建时按"同页内出现的顺序"重新推一遍：
    排序键与解析时一致，所以结果与原始 payload 相同。
    不推这一列的话，重建后所有段号都会变成 0——页码还在，但"第几段"没了。
    """
    with reader() as c:
        rows = c.execute(
            "SELECT chunk_index, page FROM chunks WHERE version_id = ? ORDER BY chunk_index",
            (version_id,),
        ).fetchall()
    seen: dict[Any, int] = {}
    out: dict[int, int] = {}
    for row in rows:
        page = row["page"]
        out[row["chunk_index"]] = seen.get(page, 0)
        seen[page] = seen.get(page, 0) + 1
    return out


def _to_fragments(rows: list[Any], ordinals: dict[int, int]) -> list[dict[str, Any]]:
    frags: list[dict[str, Any]] = []
    for row in rows:
        frag: dict[str, Any] = {
            "kind": KIND_FROM_TYPE.get(row.chunk_type or "text", "paragraph"),
            "text": row.text,
            "page": row.page or 0,
            "paragraph": ordinals.get(row.chunk_index, 0),
            "section": row.section_path or "",
        }
        if row.bbox:
            frag["bbox"] = row.bbox
        if row.latex:
            frag["latex"] = row.latex
        frags.append(frag)
    return frags


# ---------------------------------------------------------------------------
# 启动 / 取消 / 执行
# ---------------------------------------------------------------------------

def start_build(
    *,
    target_version: str | None = None,
    project_id: str | None = None,
    batch_size: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    """
    登记并排队一次重建。默认目标是"当前配置算出来的版本"，
    也就是**改完配置直接跑重建**，不用手填版本号。
    """
    sig = index_version.current_signature()
    target = target_version or index_version.version_of(sig)
    if index_version.get(target) is None:
        index_version.register(
            pipeline_version=target, signature=sig, note=note or "重建任务创建",
        )

    running = _find_active(target, project_id)
    if running:
        return {**running, "reused": True}

    scope = _scope(project_id)
    total = sum(int(s["n"]) for s in scope)
    build_id = new_id("ib")
    now = utc_now()
    batch = max(1, min(512, int(batch_size or settings.index_batch_size)))
    with transaction() as c:
        c.execute(
            """INSERT INTO index_builds
               (id, target_version, project_id, status, cursor_version, cursor_offset,
                total_chunks, done_chunks, failed_chunks, points_written, batch_size,
                elapsed_s, options, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', NULL, 0, ?, 0, 0, 0, ?, 0, ?, ?, ?)""",
            (
                build_id, target, project_id, total, batch,
                dump_json({"note": note, "versions": len(scope)}), now, now,
            ),
        )
    index_version.update_version(target, build_id=build_id, status="building")
    return get_build(build_id) or {}


def cancel_build(build_id: str) -> dict[str, Any]:
    """
    请求取消。真正停下来发生在**批边界**——不是立刻杀线程。

    为什么不做成"立刻停"：正在编码的那一批已经把一半点写进 collection 了，
    强行打断会留下"写了一半的批次"，而游标还停在这批之前。重跑能修好，
    但中间那段时间里这个 collection 是不完整的，如果它恰好已被切流就会漏召回。
    让它在批与批之间停，任何时刻 collection 都是"游标之前全有、之后全无"。
    """
    b = get_build(build_id)
    if not b:
        raise index_version.IndexVersionError(f"未知的 build：{build_id}")
    if b["status"] not in _ACTIVE:
        return b
    _update(build_id, status="cancelling")
    return get_build(build_id) or b


def run_build(
    build_id: str,
    *,
    max_batches: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """
    执行（或续跑）一次重建，返回最终的 build 行。

    `max_batches` 只给测试用：跑 N 批就返回，游标停在 N 批之后，
    等价于"进程在这里被杀掉"。续跑调同一个函数、同一个 build_id 即可。
    这不是"暂停"功能——真正的暂停需要人工恢复，而这里要验证的是崩溃后能接上。
    """
    build = get_build(build_id)
    if not build:
        raise index_version.IndexVersionError(f"未知的 build：{build_id}")
    if build["status"] == "done":
        return build
    # cancelled / failed 也允许再跑：取消只停手，不清游标——
    # "中断"与"放弃"是两件事，把取消做成不可恢复，重来就得从第 0 条开始，
    # 那正好抵消了重建最大的好处（不用重灌）。
    if build["status"] == "cancelling":
        # 取消信号在"还没开始跑"的时候到达。这里必须直接收尾：
        # 下面那句 status="running" 会把它覆盖掉，结果是"点了取消却跑完了"——
        # 这个 bug 不报错，只是不生效，最难查。
        _update(build_id, status="cancelled", finished_at=utc_now())
        return get_build(build_id) or build

    target = build["target_version"]
    collection = index_version.collection_for(target)
    row = index_version.get(target) or {}
    if row.get("collection"):
        collection = row["collection"]

    # 显式传 collection：**不能让它去问路由**。重建要写"目标版本"，
    # 而此刻路由还指着旧版本，问路由会写回正在服务的那个 collection。
    store = VectorStoreService(collection=collection, pipeline_version=target)

    started = time.time()
    cpu0 = _cpu_s()
    _update(build_id, status="running")

    scope = _scope(build["project_id"])
    total = sum(int(s["n"]) for s in scope)
    if total and total != int(build["total_chunks"] or 0):
        _update(build_id, total_chunks=total)

    done = int(build["done_chunks"] or 0)
    points = int(build["points_written"] or 0)
    batches = 0
    # 断点：跳过游标之前的版本；当前版本从 offset 处继续
    seen_cursor = False
    for item in scope:
        vid = item["version_id"]
        if not seen_cursor and build.get("cursor_version"):
            if vid != build["cursor_version"]:
                continue
            seen_cursor = True
        offset = int(build["cursor_offset"] or 0) if vid == build.get("cursor_version") else 0
        ordinals = _paragraph_ordinals(vid)
        while True:
            fresh = get_build(build_id) or {}
            if fresh.get("status") in ("cancelling", "cancelled"):
                # 批边界停：此刻 collection 里"游标之前全有、之后全无"，是一致的半成品
                _update(
                    build_id, status="cancelled", finished_at=utc_now(),
                    elapsed_s=round(time.time() - started, 1),
                    peak_rss_mb=_peak_rss_mb(), cpu_s=round(_cpu_s() - cpu0, 2),
                )
                return get_build(build_id) or {}

            rows = ChunkRepository.list_range(vid, offset, int(build["batch_size"] or DEFAULT_BATCH))
            if not rows:
                break
            frags = _to_fragments(rows, ordinals)
            written = 0
            if frags:
                written = store.upsert_fragment_batch(
                    arxiv_id=item["arxiv_id"],
                    version=item["version_label"],
                    title=item["title"],
                    body_hash="",
                    fragments=frags,
                    # 点 ID 的序号必须与解析时同一套：取这一批第一个 chunk 的真实
                    # chunk_index 当偏移，而不是"已经处理了多少条"。两者在 chunk_index
                    # 有空洞时会分叉，分叉的后果是重建出来的点与 SQLite 对不上，
                    # 溯源时 resolve_point_ids 一片空白。
                    index_offset=rows[0].chunk_index,
                )
            offset += len(rows)
            done += len(rows)
            points += written
            batches += 1
            _update(
                build_id,
                cursor_version=vid, cursor_offset=offset,
                done_chunks=done, points_written=points,
                elapsed_s=round(time.time() - started, 1),
                peak_rss_mb=_peak_rss_mb(),
                cpu_s=round(_cpu_s() - cpu0, 2),
            )
            if on_progress:
                on_progress({
                    "build_id": build_id, "done": done, "total": total,
                    "points": points, "collection": collection,
                    "elapsed_s": round(time.time() - started, 1),
                })
            if max_batches and batches >= max_batches:
                # 测试钩子：等价于进程此刻被杀——游标已落，状态留在 running
                return get_build(build_id) or {}

    # 收尾：点数以 collection 实际计数为准，不信自己累加的值
    actual = 0
    papers = 0
    try:
        actual = store.client.count(collection_name=collection, exact=True).count
        papers = len({s["arxiv_id"] for s in scope})
    except Exception:
        actual = points

    _update(
        build_id, status="done", finished_at=utc_now(),
        elapsed_s=round(time.time() - started, 1),
        peak_rss_mb=_peak_rss_mb(), cpu_s=round(_cpu_s() - cpu0, 2),
        points_written=actual,
    )
    index_version.update_version(
        target,
        status="ready", points=actual, chunk_count=done, papers=papers,
        built_at=utc_now(), collection=collection,
    )
    from app.services.rag.strategies import invalidate_corpus

    invalidate_corpus(collection)
    return get_build(build_id) or {}


def stats(build: dict[str, Any]) -> dict[str, Any]:
    """给接口/文档用的派生量：吞吐、每千点耗时、内存增量。"""
    done = int(build.get("done_chunks") or 0)
    elapsed = float(build.get("elapsed_s") or 0) or 0.0
    return {
        "done_chunks": done,
        "elapsed_s": elapsed,
        "chunks_per_s": round(done / elapsed, 2) if elapsed else None,
        "s_per_1k_chunks": round(elapsed / done * 1000, 1) if done else None,
        "peak_rss_mb": build.get("peak_rss_mb"),
        "cpu_s": build.get("cpu_s"),
        "batch_size": build.get("batch_size"),
        "points_written": build.get("points_written"),
    }
