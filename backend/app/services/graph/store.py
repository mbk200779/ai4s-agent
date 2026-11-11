"""图谱的读写：库内论文、版本、参考文献条目、正文分块。

单独一层是为了让 `service.py` 只做编排——构图逻辑不该夹着 SQL，
而且回填脚本（重抽参考文献 / 重跑匹配）要能复用同一批查询。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from app.db import reader, transaction
from app.repositories.reference_repo import ReferenceRepository


def library_papers(project_id: str) -> list[dict[str, Any]]:
    """
    课题内的论文 + 每篇的"代表版本"。

    代表版本取 is_current，没有则取最近完成的——图谱按 **论文** 画节点，
    不是按版本：Attention 的 v1 和 v7 是同一项研究，放两个节点会把
    "被两篇引用"变成"被一篇引用两次"，中心性全歪。
    """
    with reader() as c:
        rows = c.execute(
            """SELECT p.id, p.title, p.authors, p.year, p.arxiv_id,
                      pv.id AS version_id, pv.version_label, pv.status, pv.ref_count,
                      pv.chunk_count
                 FROM papers p
                 LEFT JOIN paper_versions pv ON pv.id = (
                      SELECT id FROM paper_versions
                       WHERE paper_id = p.id
                       ORDER BY is_current DESC, COALESCE(finished_at, '') DESC
                       LIMIT 1)
                WHERE p.project_id = ?
                ORDER BY p.created_at""",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def refs_of(version_id: str) -> list[dict[str, Any]]:
    with reader() as c:
        rows = c.execute(
            """SELECT id, ref_index, raw_text, parsed_title, parsed_authors,
                      parsed_year, matched_paper_id, match_score, match_method
                 FROM paper_refs WHERE version_id = ? ORDER BY ref_index""",
            (version_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["authors"] = json.loads(d.get("parsed_authors") or "[]")
        except Exception:  # noqa: BLE001 - 老数据可能是裸串
            d["authors"] = []
        out.append(d)
    return out


def body_chunks(version_id: str) -> list[dict[str, Any]]:
    """正文分块：引用标记只可能出现在正文里，公式/图表块不用扫。"""
    with reader() as c:
        rows = c.execute(
            """SELECT id, text, page, bbox, chunk_type, section_path
                 FROM chunks WHERE version_id = ?
                ORDER BY chunk_index""",
            (version_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_refs_and_citations(version_id: str, conn: sqlite3.Connection | None = None) -> None:
    """重抽参考文献前先清掉旧条目与指向它们的引用位置，避免 FK 悬空。"""
    def _run(c: sqlite3.Connection) -> None:
        c.execute(
            "DELETE FROM citations WHERE ref_id IN (SELECT id FROM paper_refs WHERE version_id = ?)",
            (version_id,),
        )
        c.execute("DELETE FROM paper_refs WHERE version_id = ?", (version_id,))

    if conn is not None:
        _run(conn)
    else:
        with transaction() as c:
            _run(c)


def write_refs(version_id: str, records: list[dict[str, Any]]) -> int:
    """
    写入重抽结果。字段名对齐 ReferenceRepository.bulk_create 的口径，
    编号式文献（[37]）的 ref_index 由记录自带，不能按顺序重编。
    """
    payload = [
        {
            "raw": r["raw"],
            "title": r.get("title"),
            "authors": r.get("authors") or [],
            "year": r.get("year"),
            "ref_index": r.get("index"),
        }
        for r in records
    ]
    return ReferenceRepository.bulk_create(version_id, payload)


def save_matches(version_id: str, pairs: list[tuple[int, str, float, str]]) -> int:
    """把匹配结果写回 paper_refs（按 ref_index 定位，避免依赖内部 id）。"""
    if not pairs:
        return 0
    with transaction() as c:
        for ref_index, paper_id, score, method in pairs:
            c.execute(
                """UPDATE paper_refs
                      SET matched_paper_id = ?, match_score = ?, match_method = ?
                    WHERE version_id = ? AND ref_index = ?""",
                (paper_id, score, method, version_id, ref_index),
            )
    return len(pairs)


def replace_citations(version_id: str, rows: list[dict[str, Any]]) -> int:
    from app.repositories.reference_repo import CitationRepository

    with transaction() as c:
        c.execute(
            "DELETE FROM citations WHERE ref_id IN (SELECT id FROM paper_refs WHERE version_id = ?)",
            (version_id,),
        )
    return CitationRepository.bulk_create(rows)


def ref_id_map(version_id: str) -> dict[int, str]:
    return {r.ref_index: r.id for r in ReferenceRepository.list_by_version(version_id)}


def version_pdf_key(version_id: str) -> Optional[str]:
    with reader() as c:
        row = c.execute(
            "SELECT pdf_s3_key FROM paper_versions WHERE id = ?", (version_id,)
        ).fetchone()
    return row["pdf_s3_key"] if row else None
