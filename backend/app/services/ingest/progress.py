"""
解析进度的读写与对外视图。

为什么进度放在 SQLite 而不是 Celery 的 result backend：
1. 用户刷新页面后还要能看到进度，Celery result 只保留 1 小时且按 task_id 才能查；
2. 断点续跑需要"上次解析到第几页"这个持久事实，它必须和 chunk 在同一个事务体系里；
3. worker 崩溃后 result 可能已经丢了，但版本行上的进度不会。

SSE 与轮询读的是同一份数据，所以两种前端接法不会打架。
"""
from dataclasses import dataclass
from typing import Any, Optional

from app.db import reader, utc_now
from app.services.ingest import limits

# stage 是"过程"，status 是"结论"。两个都要给前端：
# 例如 status=pending + stage=parsing 表示"还在跑，已解析到第 120 页"。
STAGE_LABELS = {
    "queued": "排队中",
    "downloading": "下载原文",
    "parsing": "解析正文",
    "structuring": "抽取文献与引用",
    "indexed": "已完成",
    "duplicate": "内容重复，已跳过",
    "reused": "复用已有解析结果",
    "failed": "失败",
}

# 每个阶段的进度锚点，用来让进度条在"没有页数概念"的阶段也能动
_STAGE_PCT = {
    "queued": 1,
    "downloading": 4,
    "parsing": 0,      # 按页数算
    "structuring": 96,
    "indexed": 100,
    "duplicate": 100,
    "reused": 100,
    "failed": 100,
}

TERMINAL_STAGES = {"indexed", "duplicate", "reused", "failed"}


@dataclass
class IngestProgress:
    """对外的进度快照，字段与前端 IngestProgress 类型一一对应。"""

    task_id: Optional[str] = None
    version_id: Optional[str] = None
    paper_id: Optional[str] = None
    project_id: Optional[str] = None
    title: Optional[str] = None
    stage: str = "queued"
    stage_label: str = "排队中"
    status: str = "pending"
    page: int = 0
    total_pages: Optional[int] = None
    pct: int = 0
    chunks: int = 0
    refs: int = 0
    citations: int = 0
    attempt: int = 0
    elapsed_ms: Optional[int] = None
    eta_ms: Optional[int] = None
    error: Optional[str] = None
    reused_from: Optional[str] = None
    done: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "version_id": self.version_id,
            "paper_id": self.paper_id,
            "project_id": self.project_id,
            "title": self.title,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "status": self.status,
            "page": self.page,
            "total_pages": self.total_pages,
            "pct": self.pct,
            "chunks": self.chunks,
            "refs": self.refs,
            "citations": self.citations,
            "attempt": self.attempt,
            "attempt_limit": limits.MAX_ATTEMPTS,
            "elapsed_ms": self.elapsed_ms,
            "eta_ms": self.eta_ms,
            "error": self.error,
            "reused_from": self.reused_from,
            "done": self.done,
            "updated_at": self.updated_at,
        }


def _pct_of(stage: str, page: int, total: Optional[int]) -> int:
    """把 stage + 页数换算成 0..100，避免前端各自实现一套阶段权重。"""
    if stage == "parsing":
        if not total:
            return min(90, 5 + page // 10)  # 还没拿到总页数时的保守估计
        # 解析阶段占 4..95，剩下的留给 structuring，避免卡在 99 不动
        return min(95, 4 + int(91 * page / total))
    return _STAGE_PCT.get(stage, 0)


def _parse_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").timestamp()
    except ValueError:
        return None


def from_row(row) -> IngestProgress:
    """把 paper_versions 的一行 + 论文标题翻译成进度快照。"""
    keys = set(row.keys())
    stage = row["stage"] if "stage" in keys else "queued"
    status = row["status"] if "status" in keys else "pending"
    page = int(row["progress_page"] or 0) if "progress_page" in keys else 0
    total = row["page_count"] if "page_count" in keys else None
    started = _parse_ts(row["started_at"] if "started_at" in keys else None)
    finished = _parse_ts(row["finished_at"] if "finished_at" in keys else None)

    elapsed_ms = None
    if started:
        end = finished or _parse_ts(utc_now())
        if end:
            elapsed_ms = max(0, int((end - started) * 1000))

    eta_ms = None
    if stage == "parsing" and total and page > 0 and elapsed_ms:
        per_page = elapsed_ms / page
        eta_ms = int(per_page * max(0, total - page))

    return IngestProgress(
        task_id=row["task_id"] if "task_id" in keys else None,
        version_id=row["id"],
        paper_id=row["paper_id"] if "paper_id" in keys else None,
        project_id=row["project_id"] if "project_id" in keys else None,
        title=row["paper_title"] if "paper_title" in keys else None,
        stage=stage,
        stage_label=STAGE_LABELS.get(stage, stage),
        status=status,
        page=page,
        total_pages=total,
        pct=_pct_of(stage, page, total),
        chunks=int(row["chunk_count"] or 0) if "chunk_count" in keys else 0,
        refs=int(row["ref_count"] or 0) if "ref_count" in keys else 0,
        citations=int(row["citation_count"] or 0) if "citation_count" in keys else 0,
        attempt=int(row["attempt"] or 0) if "attempt" in keys else 0,
        elapsed_ms=elapsed_ms,
        eta_ms=eta_ms,
        error=row["error_message"] if "error_message" in keys else None,
        reused_from=row["reused_from"] if "reused_from" in keys else None,
        done=stage in TERMINAL_STAGES,
        updated_at=row["finished_at"] or row["started_at"] or row["ingested_at"] or "",
    )


_VERSION_SQL = """
SELECT pv.*, p.title AS paper_title, p.project_id AS project_id
  FROM paper_versions pv
  JOIN papers p ON p.id = pv.paper_id
"""


def load_by_task(task_id: str) -> Optional[IngestProgress]:
    with reader() as c:
        row = c.execute(_VERSION_SQL + " WHERE pv.task_id = ? LIMIT 1", (task_id,)).fetchone()
    return from_row(row) if row else None


def load_by_version(version_id: str) -> Optional[IngestProgress]:
    with reader() as c:
        row = c.execute(_VERSION_SQL + " WHERE pv.id = ? LIMIT 1", (version_id,)).fetchone()
    return from_row(row) if row else None
