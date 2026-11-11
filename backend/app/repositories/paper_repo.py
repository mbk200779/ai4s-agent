"""论文与论文版本 CRUD。"""
import sqlite3
from typing import Optional

from app.core.config import settings
from app.services.ingest.limits import MAX_ATTEMPTS as MAX_ATTEMPTS_SQL
from app.db import dump_json, load_json, new_id, reader, utc_now
from app.models import Paper, PaperVersion
from app.repositories.base import tx


class PaperRepository:
    """论文条目。同一课题内 arxiv_id 唯一；本地 PDF 用 upload_<hash> 充当 id。"""

    @staticmethod
    def upsert_paper(
        project_id: str,
        title: str,
        arxiv_id: str | None = None,
        authors: list[str] | None = None,
        year: int | None = None,
        venue: str | None = None,
        abstract: str | None = None,
        doi: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Paper:
        """
        课题内已有同一 arxiv_id 时更新元数据（标题可能因 arXiv 修订变化），
        否则新建。返回的 paper.id 是后续版本挂载的锚点。
        """
        now = utc_now()
        with tx(conn) as c:
            row = None
            if arxiv_id:
                row = c.execute(
                    "SELECT * FROM papers WHERE project_id = ? AND arxiv_id = ?",
                    (project_id, arxiv_id),
                ).fetchone()
            if row:
                c.execute(
                    """UPDATE papers SET title = ?, authors = ?, year = ?, venue = ?,
                       abstract = COALESCE(?, abstract), doi = COALESCE(?, doi), updated_at = ?
                       WHERE id = ?""",
                    (
                        title,
                        dump_json(authors or []),
                        year,
                        venue,
                        abstract or None,
                        doi,
                        now,
                        row["id"],
                    ),
                )
                pid = row["id"]
                created = row["created_at"]
            else:
                pid = new_id("pap")
                created = now
                c.execute(
                    """INSERT INTO papers
                       (id, project_id, arxiv_id, title, authors, year, venue, abstract, doi,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pid,
                        project_id,
                        arxiv_id,
                        title,
                        dump_json(authors or []),
                        year,
                        venue,
                        abstract,
                        doi,
                        now,
                        now,
                    ),
                )
        return Paper(
            id=pid,
            project_id=project_id,
            arxiv_id=arxiv_id,
            title=title,
            authors=authors or [],
            year=year,
            venue=venue,
            abstract=abstract,
            doi=doi,
            created_at=created,
            updated_at=now,
        )

    @staticmethod
    def get(paper_id: str) -> Optional[Paper]:
        with reader() as c:
            row = c.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return Paper.from_row(row) if row else None

    @staticmethod
    def get_by_arxiv(project_id: str, arxiv_id: str) -> Optional[Paper]:
        with reader() as c:
            row = c.execute(
                "SELECT * FROM papers WHERE project_id = ? AND arxiv_id = ?",
                (project_id, arxiv_id),
            ).fetchone()
        return Paper.from_row(row) if row else None

    @staticmethod
    def list_by_project(project_id: str) -> list[Paper]:
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM papers WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [Paper.from_row(r) for r in rows]

    @staticmethod
    def list_with_current_version(project_id: str) -> list[dict]:
        """课题下的论文 + 当前版本，供列表页一次取完，避免 N+1 查询。"""
        with reader() as c:
            rows = c.execute(
                """SELECT p.*, pv.id AS version_id, pv.version_label, pv.status AS version_status,
                          pv.page_count, pv.ingested_at
                     FROM papers p
                     LEFT JOIN paper_versions pv ON pv.paper_id = p.id AND pv.is_current = 1
                    WHERE p.project_id = ?
                    ORDER BY p.created_at DESC""",
                (project_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["authors"] = load_json(d.get("authors"), [])
            out.append(d)
        return out


class PaperVersionRepository:
    """论文版本。同一论文的 v1 / v7 各自独立，共享一个 paper 行。"""

    @staticmethod
    def create(
        paper_id: str,
        version_label: str,
        file_hash: str,
        pdf_s3_key: str | None = None,
        content_hash: str | None = None,
        page_count: int | None = None,
        pipeline_version: str | None = None,
        status: str = "pending",
        stage: str = "queued",
        task_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[PaperVersion, bool]:
        """
        返回 (版本, 是否新建)。
        (paper_id, version_label) 或 (paper_id, file_hash) 已存在时直接复用旧行，
        重复上传同一个 PDF 不会产生两条版本记录。
        """
        now = utc_now()
        pv = pipeline_version or settings.pipeline_version
        with tx(conn) as c:
            row = c.execute(
                "SELECT * FROM paper_versions WHERE paper_id = ? AND (version_label = ? OR file_hash = ?)",
                (paper_id, version_label, file_hash),
            ).fetchone()
            if row:
                return PaperVersion.from_row(row), False

            vid = new_id("ver")
            # 新版本入库后它就是「当前版本」，旧的降级
            c.execute("UPDATE paper_versions SET is_current = 0 WHERE paper_id = ?", (paper_id,))
            c.execute(
                """INSERT INTO paper_versions
                   (id, paper_id, version_label, file_hash, content_hash, ingested_at,
                    pipeline_version, pdf_s3_key, page_count, status, is_current, stage, task_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    vid,
                    paper_id,
                    version_label,
                    file_hash,
                    content_hash,
                    now,
                    pv,
                    pdf_s3_key,
                    page_count,
                    status,
                    stage,
                    task_id,
                ),
            )
        return (
            PaperVersion(
                id=vid,
                paper_id=paper_id,
                version_label=version_label,
                file_hash=file_hash,
                content_hash=content_hash,
                ingested_at=now,
                pipeline_version=pv,
                pdf_s3_key=pdf_s3_key,
                page_count=page_count,
                status=status,
                is_current=1,
                stage=stage,
                task_id=task_id,
            ),
            True,
        )

    @staticmethod
    def get(version_id: str) -> Optional[PaperVersion]:
        with reader() as c:
            row = c.execute("SELECT * FROM paper_versions WHERE id = ?", (version_id,)).fetchone()
        return PaperVersion.from_row(row) if row else None

    @staticmethod
    def get_by_label(paper_id: str, version_label: str) -> Optional[PaperVersion]:
        with reader() as c:
            row = c.execute(
                "SELECT * FROM paper_versions WHERE paper_id = ? AND version_label = ?",
                (paper_id, version_label),
            ).fetchone()
        return PaperVersion.from_row(row) if row else None

    @staticmethod
    def get_current(paper_id: str) -> Optional[PaperVersion]:
        with reader() as c:
            row = c.execute(
                "SELECT * FROM paper_versions WHERE paper_id = ? ORDER BY is_current DESC, ingested_at DESC LIMIT 1",
                (paper_id,),
            ).fetchone()
        return PaperVersion.from_row(row) if row else None

    @staticmethod
    def list_by_paper(paper_id: str) -> list[PaperVersion]:
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM paper_versions WHERE paper_id = ? ORDER BY ingested_at",
                (paper_id,),
            ).fetchall()
        return [PaperVersion.from_row(r) for r in rows]

    @staticmethod
    def mark_indexed(
        version_id: str,
        page_count: int | None = None,
        content_hash: str | None = None,
        status: str = "indexed",
    ) -> None:
        # 成功即清错误：上一次失败留下的 error_message 若不清，进度接口会一直
        # 带着一条陈旧的报错（stage=indexed 却有 error），前端会误判成"失败了"。
        sets = ["status = ?", "error_message = NULL", "error_trace = NULL"]
        args: list = [status]
        if page_count is not None:
            sets.append("page_count = ?")
            args.append(page_count)
        if content_hash is not None:
            sets.append("content_hash = ?")
            args.append(content_hash)
        args.append(version_id)
        with tx(None) as c:
            c.execute(f"UPDATE paper_versions SET {', '.join(sets)} WHERE id = ?", args)

    # ---------- 进度与断点 ----------
    # 允许被 advance 直接写的列。白名单而不是拼接调用方字段名，
    # 是为了杜绝"传个 column 字符串就能改任意列"的注入面。
    _PROGRESS_FIELDS = {
        "task_id",
        "stage",
        "progress_page",
        "parse_cursor",
        "index_cursor",
        "text_bytes",
        "chunk_count",
        "ref_count",
        "citation_count",
        "attempt",
        "error_message",
        "error_trace",
        "started_at",
        "finished_at",
        "page_count",
        "content_hash",
        "status",
        "reused_from",
    }

    @staticmethod
    def advance(
        version_id: str,
        conn: sqlite3.Connection | None = None,
        **fields: object,
    ) -> None:
        """
        定向更新进度列。解析主循环每批都要调一次，所以刻意只写变更列，
        不整行 UPDATE —— 整行更新会把 worker 与 API 的写放大成互锁。
        """
        unknown = set(fields) - PaperVersionRepository._PROGRESS_FIELDS
        if unknown:
            raise ValueError(f"不支持更新的进度字段: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        args = list(fields.values()) + [version_id]
        with tx(conn) as c:
            c.execute(f"UPDATE paper_versions SET {sets} WHERE id = ?", args)

    @staticmethod
    def mark_failed(version_id: str, message: str, trace: str | None = None) -> None:
        """终态失败：status 与 stage 同时置 failed，并留下完整错误上下文。"""
        now = utc_now()
        with tx(None) as c:
            c.execute(
                """UPDATE paper_versions
                      SET status = 'failed', stage = 'failed', error_message = ?,
                          error_trace = ?, finished_at = ?
                    WHERE id = ?""",
                (message[:2000], (trace or "")[:20000], now, version_id),
            )

    @staticmethod
    def reset_for_retry(version_id: str) -> None:
        """
        手动重跑：清掉错误与终态，但**保留 parse_cursor / index_cursor**，
        所以"失败了再点一次重试"是从断点继续，而不是从头再解析一遍。
        """
        with tx(None) as c:
            c.execute(
                """UPDATE paper_versions
                      SET status = 'pending', stage = 'queued', error_message = NULL,
                          error_trace = NULL, finished_at = NULL
                    WHERE id = ?""",
                (version_id,),
            )

    @staticmethod
    def find_reusable(
        file_hash: str, arxiv_id: str | None, version_label: str
    ) -> Optional[dict]:
        """
        找到可以"直接抄结果"的源版本：同一个 PDF 字节（file_hash 相同）、
        同一个 arxiv_id、同一个版本号 —— 三个都相同意味着 Qdrant 里的点 ID
        完全一致，复用不需要重算向量，也不需要改向量库。

        跨课题上传同一份 PDF 也走这条路（arxiv_id 由文件哈希派生，天然相同），
        复制的是 SQLite 行，题注与引用各自独立，不违反"论文必须归属课题"。
        """
        with reader() as c:
            row = c.execute(
                """SELECT pv.*, p.arxiv_id AS paper_arxiv_id, p.project_id AS project_id
                     FROM paper_versions pv
                     JOIN papers p ON p.id = pv.paper_id
                    WHERE pv.file_hash = ?
                      AND pv.status = 'indexed'
                      AND p.arxiv_id IS ?
                      AND pv.version_label = ?
                    ORDER BY pv.ingested_at
                    LIMIT 1""",
                (file_hash, arxiv_id, version_label),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def list_stuck(older_than_s: int = 90, limit: int = 20) -> list[PaperVersion]:
        """
        找出"已经开工但看起来没人管了"的版本：status 还是 pending、stage 停在进行中，
        且 started_at 早于 older_than_s 秒。

        为什么需要这个：Redis 作为 broker 时，worker 被强制杀死（SIGKILL / 容器重启）
        会让已投递但未 ack 的消息丢失——acks_late 只能保证"进程级优雅退出"时的重新投递，
        挡不住 docker restart 的 10 秒宽限期之后那一刀。所以必须有一个独立的巡检：
        把这类孤儿版本重新投递，靠解析游标从断点继续。

        只挑 started_at 非空的：还在排队、根本没被领取的版本不归它管，
        那些消息还在队列里，重复投递没有意义。
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_s)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with reader() as c:
            rows = c.execute(
                """SELECT * FROM paper_versions
                    WHERE status = 'pending'
                      AND stage IN ('downloading', 'parsing', 'structuring')
                      AND started_at IS NOT NULL
                      AND started_at <= ?
                      AND attempt < ?
                    ORDER BY started_at
                    LIMIT ?""",
                (cutoff, MAX_ATTEMPTS_SQL, limit),
            ).fetchall()
        return [PaperVersion.from_row(r) for r in rows]

    @staticmethod
    def get_by_task(task_id: str) -> Optional[PaperVersion]:
        with reader() as c:
            row = c.execute(
                "SELECT * FROM paper_versions WHERE task_id = ? ORDER BY ingested_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return PaperVersion.from_row(row) if row else None

    @staticmethod
    def resolve_chain(version_id: str) -> Optional[dict]:
        """
        版本 → 论文 → 课题。API 与迁移都需要这个反查，
        一次 join 完成，避免调用方自己拼。
        """
        with reader() as c:
            row = c.execute(
                """SELECT pv.*, p.title AS paper_title, p.arxiv_id AS paper_arxiv_id,
                          p.project_id AS project_id, pj.name AS project_name
                     FROM paper_versions pv
                     JOIN papers p ON p.id = pv.paper_id
                     JOIN projects pj ON pj.id = p.project_id
                    WHERE pv.id = ?""",
                (version_id,),
            ).fetchone()
        return dict(row) if row else None
