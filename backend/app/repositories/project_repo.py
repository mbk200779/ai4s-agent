"""课题（Project）CRUD。"""
import sqlite3
from typing import Optional

from app.db import new_id, reader, utc_now
from app.models import Project
from app.repositories.base import default_project_id, tx


class ProjectRepository:
    """课题是数据模型的最小单位：论文必须挂在某个课题下，没有全局裸存。"""

    @staticmethod
    def create(
        name: str,
        research_question: str | None = None,
        created_by: str = "system",
        conn: sqlite3.Connection | None = None,
    ) -> Project:
        now = utc_now()
        pid = new_id("proj")
        with tx(conn) as c:
            c.execute(
                """INSERT INTO projects
                   (id, name, research_question, status, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?, ?)""",
                (pid, name, research_question, created_by, now, now),
            )
        return Project(
            id=pid,
            name=name,
            research_question=research_question,
            status="active",
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def get(project_id: str) -> Optional[Project]:
        with reader() as c:
            row = c.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return Project.from_row(row) if row else None

    @staticmethod
    def get_by_name(name: str) -> Optional[Project]:
        with reader() as c:
            row = c.execute(
                "SELECT * FROM projects WHERE name = ? ORDER BY created_at LIMIT 1", (name,)
            ).fetchone()
        return Project.from_row(row) if row else None

    @staticmethod
    def get_or_create_default(conn: sqlite3.Connection | None = None) -> Project:
        """
        上传接口不传 project_id 时使用。旧前端零改动也能继续工作，
        但落库时仍然带着一个真实课题，不会出现「无课题论文」。
        """
        with tx(conn) as c:
            pid = default_project_id(c)
            row = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return Project.from_row(row)

    @staticmethod
    def list(limit: int = 50, status: str | None = None) -> list[Project]:
        sql = "SELECT * FROM projects"
        args: list = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with reader() as c:
            rows = c.execute(sql, args).fetchall()
        return [Project.from_row(r) for r in rows]

    @staticmethod
    def update(
        project_id: str,
        name: str | None = None,
        research_question: str | None = None,
        status: str | None = None,
    ) -> bool:
        sets, args = [], []
        if name is not None:
            sets.append("name = ?")
            args.append(name)
        if research_question is not None:
            sets.append("research_question = ?")
            args.append(research_question)
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if not sets:
            return False
        sets.append("updated_at = ?")
        args.append(utc_now())
        args.append(project_id)
        with tx(None) as c:
            cur = c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", args)
            return cur.rowcount > 0

    @staticmethod
    def stats(project_id: str) -> dict:
        """课题概览：论文数 / 版本数 / chunk 数 / 引用数，供接口与自检使用。"""
        with reader() as c:
            row = c.execute(
                """SELECT
                     (SELECT COUNT(*) FROM papers WHERE project_id = ?)             AS papers,
                     (SELECT COUNT(*) FROM paper_versions pv
                        JOIN papers p ON p.id = pv.paper_id
                       WHERE p.project_id = ?)                                      AS versions,
                     (SELECT COUNT(*) FROM chunks ck
                        JOIN paper_versions pv ON pv.id = ck.version_id
                        JOIN papers p ON p.id = pv.paper_id
                       WHERE p.project_id = ?)                                      AS chunks,
                     (SELECT COUNT(*) FROM paper_refs r
                        JOIN paper_versions pv ON pv.id = r.version_id
                        JOIN papers p ON p.id = pv.paper_id
                       WHERE p.project_id = ?)                                      AS refs""",
                (project_id, project_id, project_id, project_id),
            ).fetchone()
        return dict(row) if row else {}
