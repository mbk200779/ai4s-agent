"""
file_hash 命中的解析结果复用。

为什么放在上传接口而不是 worker 里：
"二次上传秒级返回"的秒，是从 POST 到响应体写回浏览器的秒。如果先进队列再由
worker 发现"这是重复文件"，前端仍要等一轮排队+调度，实测 2~8 秒。
所以判重必须发生在**入队之前**：命中就直接复制已有结果并当场返回。

为什么可以只复制 SQLite 行、完全不碰 Qdrant：
可复用的前提是 (arxiv_id, version) 也相同，而 Qdrant 的点 ID 是
md5("frag::<arxiv>::<version>::<kind>::<idx>")，这三个值相同就代表向量已经在库里、
ID 完全一致。复制的只是关系库中的归属与定位信息，向量本身直接共享。
"""
from typing import Any

from app.db import new_id, reader, transaction, utc_now

# 一次复制这么多行就落一次事务，避免整本书几万行都攒在一个事务里把 WAL 撑大
_COPY_BATCH = 2000


def clone_version(src_version_id: str, dst_version_id: str) -> dict[str, Any]:
    """
    把 src 版本的 chunk / paper_refs / citations 复制到 dst 版本。

    三张表的 ID 都必须重新生成（不同版本不能共享主键），但以下三样必须原样保留：
    - chunks.chunk_index：与 Qdrant 点 ID 里的序号对齐
    - chunks.qdrant_point_id：向量库里的点就是它
    - paper_refs.ref_index：正文里的 [12] 指向它
    引用行的 ref_id / chunk_id 则要做一次 ID 映射，否则会指到源版本的行上。
    """
    from app.db import reader

    now = utc_now()
    stats = {"chunks": 0, "refs": 0, "citations": 0}

    with reader() as c:
        chunks = c.execute(
            "SELECT * FROM chunks WHERE version_id = ? ORDER BY chunk_index", (src_version_id,)
        ).fetchall()
        refs = c.execute(
            "SELECT * FROM paper_refs WHERE version_id = ? ORDER BY ref_index", (src_version_id,)
        ).fetchall()
        citations = c.execute(
            """SELECT c.* FROM citations c
                 JOIN paper_refs r ON r.id = c.ref_id
                WHERE r.version_id = ?""",
            (src_version_id,),
        ).fetchall()

        ref_id_map = {r["id"]: new_id("ref") for r in refs}
        chunk_id_map = {ch["id"]: new_id("chk") for ch in chunks}
        chunk_index_map = {ch["id"]: ch["chunk_index"] for ch in chunks}

    chunk_rows = [
        (
            chunk_id_map[ch["id"]],
            dst_version_id,
            ch["chunk_index"],
            ch["page"],
            ch["bbox"],
            ch["section_path"],
            ch["chunk_type"],
            ch["table_id"],
            ch["latex"],
            ch["text"],
            ch["token_count"],
            ch["qdrant_point_id"],
            now,
        )
        for ch in chunks
    ]
    ref_rows = [
        (
            ref_id_map[r["id"]],
            dst_version_id,
            r["ref_index"],
            r["raw_text"],
            r["parsed_title"],
            r["parsed_authors"],
            r["parsed_year"],
            r["matched_paper_id"],
            r["match_score"],
            r["match_method"],
        )
        for r in refs
    ]
    citation_rows = [
        (
            new_id("cit"),
            ref_id_map[c["ref_id"]],
            chunk_id_map.get(c["chunk_id"]) if c["chunk_id"] else None,
            c["page"],
            c["bbox"],
            c["sentence"],
            now,
        )
        for c in citations
    ]

    with transaction() as conn:
        # 复用是"重新生成一份完整结果"，所以先清空目标版本可能存在的残留
        conn.execute("DELETE FROM chunks WHERE version_id = ?", (dst_version_id,))
        conn.execute("DELETE FROM paper_refs WHERE version_id = ?", (dst_version_id,))
        for i in range(0, len(chunk_rows), _COPY_BATCH):
            conn.executemany(
                """INSERT INTO chunks
                   (id, version_id, chunk_index, page, bbox, section_path, chunk_type,
                    table_id, latex, text, token_count, qdrant_point_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                chunk_rows[i : i + _COPY_BATCH],
            )
        if ref_rows:
            conn.executemany(
                """INSERT INTO paper_refs
                   (id, version_id, ref_index, raw_text, parsed_title, parsed_authors,
                    parsed_year, matched_paper_id, match_score, match_method)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ref_rows,
            )
        if citation_rows:
            conn.executemany(
                """INSERT INTO citations (id, ref_id, chunk_id, page, bbox, sentence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                citation_rows,
            )

        stats["chunks"] = len(chunk_rows)
        stats["refs"] = len(ref_rows)
        stats["citations"] = len(citation_rows)

        conn.execute(
            """UPDATE paper_versions
                  SET status = 'indexed', stage = 'reused', progress_page = COALESCE(page_count, 0),
                      parse_cursor = COALESCE(page_count, 0), index_cursor = ?,
                      chunk_count = ?, ref_count = ?, citation_count = ?,
                      finished_at = ?, error_message = NULL, error_trace = NULL,
                      reused_from = ?
                WHERE id = ?""",
            (
                len(chunk_rows),
                stats["chunks"],
                stats["refs"],
                stats["citations"],
                now,
                src_version_id,
                dst_version_id,
            ),
        )

    return stats


def copy_reference_matches(src_version_id: str, dst_version_id: str) -> int:
    """
    复用后 reference 的 matched_paper_id 可能指向**源课题**里的论文。
    跨课题复用时这会让"他文引用了你不拥有的论文"，所以在目标课题里重新匹配一遍。
    """
    from app.repositories import ReferenceRepository

    with reader() as c:
        project_row = c.execute(
            """SELECT p.project_id AS pid FROM paper_versions pv
                 JOIN papers p ON p.id = pv.paper_id WHERE pv.id = ?""",
            (dst_version_id,),
        ).fetchone()
    if not project_row:
        return 0
    return ReferenceRepository.match_to_project(dst_version_id, project_row["pid"])
