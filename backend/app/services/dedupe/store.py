"""
指纹与判定记录的存取。

两个设计点：

1. **指纹按 version 存**，但判定记录按 `pair_key`（两个指纹的无序对）存。
   因为"以后不再重复打扰"要跟内容走，不能跟 version_id 走——重新上传同一份
   文件会产生新的 version_id，内容却没变，用 version_id 做键就永远记不住。

2. **判定记录一次写一行，UNIQUE(project_id, pair_key)**。
   同一对内容在同一个课题里只该有一条决定；用户改主意了就 UPSERT 覆盖，
   而不是堆一串互相矛盾的记录（那样"上次选了什么"就说不清了）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import new_id, reader, utc_now
from app.repositories.base import tx
from app.services.dedupe.similarity import PeerFingerprint

_PEER_SQL = """
SELECT
    pv.id            AS version_id,
    pv.paper_id      AS paper_id,
    pv.version_label AS version_label,
    pv.page_count    AS page_count,
    pv.status        AS status,
    pv.ingested_at   AS ingested_at,
    p.id             AS paper_id_dup,
    p.title          AS paper_title,
    p.authors        AS authors,
    p.year           AS year,
    p.project_id     AS project_id,
    f.simhash        AS simhash,
    f.minhash        AS minhash,
    f.shingle_count  AS shingle_count,
    f.para_hashes    AS para_hashes,
    f.abstract_emb   AS abstract_emb,
    f.lead_emb       AS lead_emb,
    f.abstract_head  AS abstract_head,
    f.lead_head      AS lead_head,
    f.body_chars     AS body_chars,
    f.section_count  AS section_count,
    f.source         AS source
FROM paper_versions pv
JOIN papers p          ON p.id = pv.paper_id
JOIN paper_fingerprints f ON f.version_id = pv.id
WHERE pv.status = 'indexed'
"""


def list_peers(
    project_id: str,
    *, exclude_version_id: str | None = None, include_other_projects: bool = False
) -> list[PeerFingerprint]:
    """
    列出可比对的既有版本（默认只看本课题）。

    为什么默认不跨课题：去重提示是**打扰**，跨课题提示会让两个无关项目互相弹窗。
    跨课题的同一份文件在入口处已经由 file_hash 复用秒回了，不需要在这里再问一遍。
    """
    sql = _PEER_SQL
    args: list[Any] = []
    if not include_other_projects:
        sql += " AND p.project_id = ?"
        args.append(project_id)
    if exclude_version_id:
        sql += " AND pv.id <> ?"
        args.append(exclude_version_id)
    with reader() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("paper_id_dup", None)
        out.append(PeerFingerprint.from_row(d))
    return out


def save_fingerprint(version_id: str, row: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    payload = {"version_id": version_id, "created_at": utc_now(), **row}
    cols = ", ".join(payload.keys())
    marks = ", ".join("?" for _ in payload)
    with tx(conn) as c:
        c.execute(
            f"INSERT INTO paper_fingerprints ({cols}) VALUES ({marks}) "
            "ON CONFLICT(version_id) DO UPDATE SET "
            + ", ".join(f"{k}=excluded.{k}" for k in payload if k != "version_id"),
            tuple(payload.values()),
        )


def get_fingerprint(version_id: str) -> dict[str, Any] | None:
    with reader() as c:
        row = c.execute(
            "SELECT * FROM paper_fingerprints WHERE version_id = ?", (version_id,)
        ).fetchone()
    return dict(row) if row else None


def pair_key(simhash_a: str, simhash_b: str) -> str:
    """无序对：小的在前。同一对内容无论谁先上传，键都一样。"""
    a, b = (simhash_a or ""), (simhash_b or "")
    lo, hi = (a, b) if a <= b else (b, a)
    return f"{lo}:{hi}"


def get_decision(project_id: str, key: str) -> dict[str, Any] | None:
    with reader() as c:
        row = c.execute(
            "SELECT * FROM dedupe_decisions WHERE project_id = ? AND pair_key = ?",
            (project_id, key),
        ).fetchone()
    return dict(row) if row else None


def save_decision(
    *,
    project_id: str,
    key: str,
    relation: str,
    action: str,
    confidence: float,
    evidence: dict[str, Any],
    new_simhash: str = "",
    peer_simhash: str = "",
    new_version_id: str = "",
    peer_version_id: str = "",
    new_label: str = "",
    peer_label: str = "",
    suppress: bool = True,
    decided_by: str = "user",
    conn: sqlite3.Connection | None = None,
) -> str:
    did = new_id("dd")
    now = utc_now()
    with tx(conn) as c:
        c.execute(
            """INSERT INTO dedupe_decisions
               (id, project_id, pair_key, relation, action, confidence, evidence,
                new_simhash, peer_simhash, new_version_id, peer_version_id,
                new_label, peer_label, suppress, decided_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id, pair_key) DO UPDATE SET
                 relation=excluded.relation,
                 action=excluded.action,
                 confidence=excluded.confidence,
                 evidence=excluded.evidence,
                 new_version_id=COALESCE(NULLIF(excluded.new_version_id,''), new_version_id),
                 peer_version_id=COALESCE(NULLIF(excluded.peer_version_id,''), peer_version_id),
                 new_label=COALESCE(NULLIF(excluded.new_label,''), new_label),
                 peer_label=COALESCE(NULLIF(excluded.peer_label,''), peer_label),
                 suppress=excluded.suppress,
                 decided_by=excluded.decided_by,
                 created_at=excluded.created_at""",
            (
                did,
                project_id,
                key,
                relation,
                action,
                confidence,
                json.dumps(evidence, ensure_ascii=False),
                new_simhash,
                peer_simhash,
                new_version_id,
                peer_version_id,
                new_label,
                peer_label,
                1 if suppress else 0,
                decided_by,
                now,
            ),
        )
    return did


def list_decisions(project_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """最近的判定记录，带两侧标题便于界面直接展示。"""
    with reader() as c:
        rows = c.execute(
            """SELECT d.*,
                      pn.title AS new_paper_title, pn.arxiv_id AS new_arxiv_id,
                      pp.title AS peer_paper_title, pp.arxiv_id AS peer_arxiv_id
               FROM dedupe_decisions d
               LEFT JOIN paper_versions vn ON vn.id = d.new_version_id
               LEFT JOIN papers pn          ON pn.id = vn.paper_id
               LEFT JOIN paper_versions vp ON vp.id = d.peer_version_id
               LEFT JOIN papers pp          ON pp.id = vp.paper_id
               WHERE d.project_id = ?
               ORDER BY d.created_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["evidence"] = json.loads(d.get("evidence") or "{}")
        except (TypeError, ValueError):
            d["evidence"] = {}
        out.append(d)
    return out


def find_by_file_hash(project_id: str, file_hash: str) -> dict[str, Any] | None:
    """
    按字节哈希找同一个文件（同一课题内）。

    这是判重里唯一"零判断"的分支：内容一字不差，不存在"像不像"的问题。
    也正因为如此，它必须排在关系判定之前——否则系统会为一份已经入过库的
    文件，认认真真地算一遍相似度，然后问用户"要不要保留"。
    """
    if not file_hash:
        return None
    with reader() as c:
        row = c.execute(
            """SELECT pv.id AS version_id, pv.version_label, pv.status,
                      p.id AS paper_id, p.title AS paper_title, p.arxiv_id
                 FROM paper_versions pv
                 JOIN papers p ON p.id = pv.paper_id
                WHERE p.project_id = ? AND pv.file_hash = ?
                ORDER BY pv.ingested_at DESC LIMIT 1""",
            (project_id, file_hash),
        ).fetchone()
    return dict(row) if row else None
