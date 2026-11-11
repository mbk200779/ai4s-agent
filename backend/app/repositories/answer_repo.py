"""回答（Answer）与检索快照 CRUD。"""
import sqlite3
from typing import Any, Optional

from app.db import dump_json, load_json, new_id, reader, utc_now
from app.models import Answer
from app.repositories.base import tx


class AnswerRepository:
    """
    claims 的每一段长这样：
        {"text": "...", "citations": ["cit_xxx", "cit_yyy"], "confidence": 0.82}
    引用存的是 citations 表的主键而不是 "[3]" 这种编号——
    编号会随参考文献顺序变化而错位，主键不会。
    """

    @staticmethod
    def create_snapshot(
        project_id: str | None,
        query: str,
        chunk_ids: list[str],
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """记录一次检索命中的 chunk 序列，用于「同一问题重跑结果为什么不同」。"""
        sid = new_id("snap")
        with tx(conn) as c:
            c.execute(
                """INSERT INTO retrieval_snapshots (id, project_id, query, chunk_ids, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (sid, project_id, query, dump_json(chunk_ids), utc_now()),
            )
        return sid

    @staticmethod
    def create(
        project_id: str,
        question: str,
        claims: list[dict[str, Any]] | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        retrieval_snapshot_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Answer:
        aid = new_id("ans")
        now = utc_now()
        with tx(conn) as c:
            c.execute(
                """INSERT INTO answers
                   (id, project_id, question, claims, model_version, prompt_version,
                    retrieval_snapshot_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    aid,
                    project_id,
                    question,
                    dump_json(claims or []),
                    model_version,
                    prompt_version,
                    retrieval_snapshot_id,
                    now,
                ),
            )
        return Answer(
            id=aid,
            project_id=project_id,
            question=question,
            claims=claims or [],
            model_version=model_version,
            prompt_version=prompt_version,
            retrieval_snapshot_id=retrieval_snapshot_id,
            created_at=now,
        )

    @staticmethod
    def get(answer_id: str) -> Optional[Answer]:
        with reader() as c:
            row = c.execute("SELECT * FROM answers WHERE id = ?", (answer_id,)).fetchone()
        return Answer.from_row(row) if row else None

    @staticmethod
    def list_by_project(project_id: str, limit: int = 50) -> list[Answer]:
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM answers WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [Answer.from_row(r) for r in rows]

    @staticmethod
    def claims_with_provenance(answer_id: str) -> list[dict[str, Any]]:
        """
        把 claims 里的 citation id 展开成完整的溯源信息，
        前端据此可以「点句子 → 跳到 PDF 那一页那一框」。
        """
        answer = AnswerRepository.get(answer_id)
        if not answer:
            return []

        with reader() as c:
            out: list[dict[str, Any]] = []
            for claim in answer.claims:
                cit_ids = claim.get("citations") or []
                if not cit_ids:
                    out.append({**claim, "sources": []})
                    continue
                placeholders = ",".join("?" for _ in cit_ids)
                rows = c.execute(
                    f"SELECT * FROM v_citation_provenance WHERE citation_id IN ({placeholders})",
                    cit_ids,
                ).fetchall()
                sources = []
                for r in rows:
                    d = dict(r)
                    d["bbox"] = load_json(d.get("bbox"), None)
                    sources.append(d)
                out.append({**claim, "sources": sources})
        return out

    @staticmethod
    def snapshot(answer_id: str) -> Optional[dict[str, Any]]:
        with reader() as c:
            row = c.execute(
                """SELECT s.* FROM retrieval_snapshots s
                     JOIN answers a ON a.retrieval_snapshot_id = s.id
                    WHERE a.id = ?""",
                (answer_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["chunk_ids"] = load_json(d.get("chunk_ids"), [])
        return d
