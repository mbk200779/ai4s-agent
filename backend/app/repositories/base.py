"""Repository 公共工具：事务复用、行映射、缺省课题。"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.core.config import settings
from app.db import new_id, transaction, utc_now
from app.models import Project


@contextmanager
def tx(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """
    允许两种调用方式：
    - repo.xxx(...)            自己开事务，适合单条写入；
    - with transaction() as c: repo.xxx(..., conn=c)   复用外层事务，
      迁移脚本批量写入时才能保证「一篇论文的所有行要么全成要么全滚」。
    """
    if conn is not None:
        yield conn
    else:
        with transaction() as own:
            yield own


def default_project_id(conn: sqlite3.Connection) -> str:
    """取或建默认课题。所有未显式指定课题的写入都落到这里。"""
    row = conn.execute(
        "SELECT id FROM projects WHERE name = ? ORDER BY created_at LIMIT 1",
        (settings.default_project_name,),
    ).fetchone()
    if row:
        return row["id"]

    now = utc_now()
    pid = new_id("proj")
    conn.execute(
        """INSERT INTO projects (id, name, research_question, status, created_by, created_at, updated_at)
           VALUES (?, ?, ?, 'active', 'system', ?, ?)""",
        (
            pid,
            settings.default_project_name,
            "旧数据迁移与未指定课题的上传默认归入此课题",
            now,
            now,
        ),
    )
    return pid


def row_to_project(row: sqlite3.Row) -> Project:
    return Project.from_row(row)
