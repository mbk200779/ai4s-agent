"""
SQLite 连接管理：建库、事务、只读查询。

为什么是 SQLite 而不是 Postgres：
- 本项目的结构化元数据量很小（课题几十个、论文几百篇、chunk 十万级），
  SQLite 单文件足够，且不需要额外容器与端口；
- API 与 Celery Worker 在同一台机器上，共享一个挂载卷即可；
- 迁移到云服务器时把这个文件一起打包带走，比 dump/restore 简单。
并发写入靠 WAL + busy_timeout 撑住，两个进程同时写也不会互相锁死。
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.core.config import settings

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ---------- 时间与 ID ----------
def utc_now() -> str:
    """统一的时间戳格式：ISO-8601 UTC，秒级精度，可直接按字符串排序。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    """带前缀的主键，便于日志与调试时一眼看出是哪类实体。"""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------- JSON 字段 ----------
def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_json(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default if default is not None else []
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else []


# ---------- 连接 ----------
def _connect() -> sqlite3.Connection:
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """建表（幂等）。API 启动时、Celery worker 启动时、迁移脚本开头都会调用。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _connect()
    try:
        try:
            conn.executescript(sql)
        except sqlite3.OperationalError:
            # 已存在的旧库不会因为 CREATE TABLE IF NOT EXISTS 而多出新列，
            # 而 schema.sql 里针对新列的索引会在这里报 no such column。
            # 这不是错误，是"该补列了"的信号：补完再整体重跑一遍即可。
            _ensure_columns(conn)
            conn.executescript(sql)
        _ensure_columns(conn)
        _backfill_stage(conn)
    finally:
        conn.close()


# 新增列一律写在这里，并在 schema.sql 的 CREATE TABLE 里同步一份。
# 顺序与类型必须一致，否则新库/老库会跑出两种形状。
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    # 05：句子级锚点。历史库没有这列时补 NULL，前端据此提示"未建立精细锚点"，
    # 不需要重解析整篇——重解析只是让锚点从无到有，旧数据本身仍然可用。
    "chunks": [
        ("anchors", "TEXT"),
    ],
    "paper_versions": [
        ("task_id", "TEXT"),
        ("stage", "TEXT NOT NULL DEFAULT 'queued'"),
        ("progress_page", "INTEGER NOT NULL DEFAULT 0"),
        ("parse_cursor", "INTEGER NOT NULL DEFAULT 0"),
        ("index_cursor", "INTEGER NOT NULL DEFAULT 0"),
        ("text_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ("chunk_count", "INTEGER NOT NULL DEFAULT 0"),
        ("ref_count", "INTEGER NOT NULL DEFAULT 0"),
        ("citation_count", "INTEGER NOT NULL DEFAULT 0"),
        ("attempt", "INTEGER NOT NULL DEFAULT 0"),
        ("error_message", "TEXT"),
        ("error_trace", "TEXT"),
        ("started_at", "TEXT"),
        ("finished_at", "TEXT"),
        ("reused_from", "TEXT"),
    ],
}


def _backfill_stage(conn: sqlite3.Connection) -> None:
    """
    老库的历史行没有 stage 值，加列时统一落到默认值 'queued'，
    于是出现 status=indexed 而 stage=queued 这种自相矛盾的行——
    进度接口会把一本早已解析完的论文报成"排队中"。

    这里按 status 对齐 stage：解析已完成的行，阶段也必须是终态。
    幂等、只扫一遍小表，所以每次启动都跑。

    注意两条语句都带 stage 条件，重复执行不会产生任何写入，
    所以 api 与 worker 同时启动、同时跑这段代码也不会互相踩。
    """
    # 首次启动时表可能还没建出来（executescript 走了异常分支），此时直接跳过
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_versions'"
    ).fetchone()
    if not table:
        return

    # busy/locked 一定要重试：api 与 worker 是同时启动的，
    # 一个在写 WAL、另一个正好在建表，这里撞上就不该静默放弃——
    # 一旦放弃，库里就会留下 status=indexed / stage=queued 的矛盾行，
    # 前端把已完成的论文显示成"排队中"，排查起来毫无线索。
    for attempt in range(4):
        try:
            conn.execute(
                "UPDATE paper_versions SET stage = 'indexed' "
                "WHERE status = 'indexed' AND (stage IS NULL OR stage <> 'indexed')"
            )
            conn.execute(
                "UPDATE paper_versions SET stage = 'failed' "
                "WHERE status = 'failed' AND (stage IS NULL OR stage <> 'failed')"
            )
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if attempt == 3:
                raise
            time.sleep(0.3 * (attempt + 1))


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """
    手写迁移：对比 PRAGMA table_info 与实际需要，缺哪列补哪列。
    不用 Alembic 是因为这张库只有几张表、迁移动作只有"加列"一种，
    引一整套迁移框架反而要多维护一个版本目录和一条构建链。
    返回本次实际新增的列名，便于启动日志里打出来。
    """
    added: list[str] = []
    for table, columns in _ADDED_COLUMNS.items():
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns:
            if name in present:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added.append(f"{table}.{name}")
    return added


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 立刻拿写锁，避免两个写入方在提交时才冲突。"""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def reader() -> Iterator[sqlite3.Connection]:
    """只读查询，不开显式事务。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
