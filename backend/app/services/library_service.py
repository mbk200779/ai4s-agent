"""
论文库管理：列出库内全部论文、按篇或批量删除。

为什么单独一套列表接口，不复用 `GET /papers/`：
那个接口读的是 Qdrant 的 meta 集合，只能反映「向量建成了的论文」，
而库管理要回答的是「数据库里到底存了什么」——包括解析失败、正在解析、
以及只有 SQLite 行没有向量点的半成品。以 SQLite 的 papers 表为准，
向量与对象存储只作为要一起清掉的附属物。

删除一次要清四处，少清一处就留一种残骸：
1. `papers` 行 —— 外键 ON DELETE CASCADE 带走 versions / chunks / refs /
   citations / fingerprints；
2. Qdrant 的 chunk 点 —— 不清就「删了还能检索到」，答案里会引用一篇已删除的论文；
3. Qdrant 的 meta 点 —— 不清则去重判定仍能命中它，上传同文件会秒回「已解析」；
4. MinIO 里的 PDF 原件 —— 不清则后台一直占着空间。
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

from app.db.connection import load_json, reader, transaction

# 版本还在流水线上的 stage：这些状态下 worker 正在写 chunks，
# 此时删库会出现「行删了、worker 又把点写回来」的幽灵向量。
RUNNING_STAGES = {"queued", "downloading", "parsing", "structuring", "indexing"}

# 已经收尾的状态：只有这几种才算「不再变化」，可以安全删除
SETTLED_STATUS = {"indexed", "failed", "duplicate"}
SETTLED_STAGES = {"indexed", "failed", "duplicate", "reused", "skipped"}


class PaperNotFound(Exception):
    """请求里有不存在的 paper_id。整批拒绝，不做部分删除。"""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"论文不存在: {', '.join(missing)}")


class PapersBusy(Exception):
    """选中的论文还在解析，删除会让 worker 与删除互相打架。"""

    def __init__(self, blocked: list[dict[str, Any]]):
        self.blocked = blocked
        super().__init__("有论文正在解析，暂不能删除")


def _is_running(status: str | None, stage: str | None) -> bool:
    """
    判断一个版本是否还在流水线上。

    不能只看 status：复用命中的版本是 status=pending + stage=reused，
    它实际上已经可用，按 status 判会把它误当成「正在解析」拒掉；
    也不能只看 stage：老数据的 stage 可能是空串而 status=indexed。
    """
    s = (status or "").strip()
    g = (stage or "").strip()
    if s in SETTLED_STATUS or g in SETTLED_STAGES:
        return False
    return g in RUNNING_STAGES or s == "pending"


def _status_label(status: str | None, stage: str | None) -> tuple[str, str]:
    """把 (status, stage) 归成一句话，给界面直接显示。返回 (kind, label)。"""
    s = (status or "").strip()
    g = (stage or "").strip()
    if _is_running(s, g):
        return "running", {
            "queued": "排队中",
            "downloading": "下载中",
            "parsing": "解析中",
            "structuring": "结构化中",
            "indexing": "建索引中",
        }.get(g, "解析中")
    if s == "failed":
        return "failed", "解析失败"
    if s == "duplicate":
        return "duplicate", "重复稿"
    if s == "indexed" or g in ("indexed", "reused"):
        return "ready", "已入库" if g != "reused" else "复用入库"
    return "unknown", "状态未知"


def list_papers(
    *,
    project_id: str | None = None,
    query: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """
    返回库内论文清单与汇总。一次查两张表在内存里拼接，避免 N+1。

    为什么不用 LEFT JOIN 一次查完：一篇论文可能有很多版本，
    join 会返回多行，还得在 SQL 里做「取当前版本」的子查询；
    两个小查询加字典映射更直白，两百篇以内没有性能问题。
    """
    where, params = [], []
    if project_id:
        where.append("p.project_id = ?")
        params.append(project_id)
    if query:
        like = f"%{query.strip()}%"
        where.append("(p.title LIKE ? OR p.arxiv_id LIKE ? OR p.authors LIKE ?)")
        params.extend([like, like, like])
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    with reader() as c:
        rows = c.execute(
            f"""SELECT p.id, p.project_id, p.arxiv_id, p.title, p.authors, p.year,
                       p.venue, p.doi, p.abstract, p.created_at, p.updated_at,
                       prj.name AS project_name, prj.status AS project_status
                  FROM papers p
                  LEFT JOIN projects prj ON prj.id = p.project_id
                  {clause}
                 ORDER BY p.created_at DESC, p.id""",
            params,
        ).fetchall()
        papers = [dict(r) for r in rows]

        versions: list[dict[str, Any]] = []
        if papers:
            ids = [p["id"] for p in papers]
            ph = ",".join("?" * len(ids))
            versions = [
                dict(r)
                for r in c.execute(
                    f"""SELECT id, paper_id, version_label, status, stage, page_count,
                               chunk_count, ref_count, citation_count, ingested_at,
                               finished_at, pdf_s3_key, pipeline_version
                          FROM paper_versions
                         WHERE paper_id IN ({ph})
                         ORDER BY is_current DESC, ingested_at DESC""",
                    ids,
                ).fetchall()
            ]

    by_paper: dict[str, list[dict[str, Any]]] = {}
    for v in versions:
        by_paper.setdefault(v["paper_id"], []).append(v)

    items: list[dict[str, Any]] = []
    for p in papers[: max(1, limit)]:
        vs = by_paper.get(p["id"], [])
        current = vs[0] if vs else None
        kind, label = _status_label(
            current.get("status") if current else None,
            current.get("stage") if current else None,
        )
        # 有任一个版本在跑就整篇不可删：worker 可能正在往这一篇里写
        running = next((v for v in vs if _is_running(v.get("status"), v.get("stage"))), None)
        items.append(
            {
                "id": p["id"],
                "title": p["title"],
                "authors": load_json(p.get("authors"), []),
                "arxiv_id": p.get("arxiv_id"),
                "year": p.get("year"),
                "venue": p.get("venue"),
                "doi": p.get("doi"),
                "has_abstract": bool((p.get("abstract") or "").strip()),
                "project_id": p["project_id"],
                "project_name": p.get("project_name") or "（课题已删除）",
                "stored_at": p["created_at"],
                "updated_at": p["updated_at"],
                "version_id": current.get("id") if current else None,
                "version_label": current.get("version_label") if current else None,
                "version_count": len(vs),
                "pages": current.get("page_count") if current else None,
                "chunks": current.get("chunk_count") if current else 0,
                "refs": current.get("ref_count") if current else 0,
                "citations": current.get("citation_count") if current else 0,
                "ingested_at": current.get("ingested_at") if current else None,
                "pipeline_version": current.get("pipeline_version") if current else None,
                "status": kind,
                "status_label": label,
                "stage": current.get("stage") if current else None,
                "has_pdf": bool(current.get("pdf_s3_key")) if current else False,
                "deletable": running is None,
                "blocked_reason": (
                    f"正在{_status_label(running.get('status'), running.get('stage'))[1]}，完成后才能删除"
                    if running
                    else None
                ),
            }
        )

    summary = {
        "papers": len(papers),
        "ready": sum(1 for i in items if i["status"] == "ready"),
        "running": sum(1 for i in items if i["status"] == "running"),
        "failed": sum(1 for i in items if i["status"] == "failed"),
        "chunks": sum(int(i["chunks"] or 0) for i in items),
        "pages": sum(int(i["pages"] or 0) for i in items),
        "versions": len(versions),
        "projects": len({i["project_id"] for i in items}),
        "without_pdf": sum(1 for i in items if not i["has_pdf"]),
    }

    projects: dict[str, dict[str, Any]] = {}
    for i in items:
        rec = projects.setdefault(
            i["project_id"],
            {"id": i["project_id"], "name": i["project_name"], "papers": 0},
        )
        rec["papers"] += 1

    return {
        "items": items,
        "summary": summary,
        "projects": sorted(projects.values(), key=lambda x: (-x["papers"], x["name"])),
    }


def _versions_of(conn, paper_ids: Sequence[str]) -> list[dict[str, Any]]:
    ph = ",".join("?" * len(paper_ids))
    rows = conn.execute(
        f"""SELECT pv.id, pv.paper_id, pv.version_label, pv.status, pv.stage,
                   pv.pdf_s3_key, pv.chunk_count, p.arxiv_id
              FROM paper_versions pv
              JOIN papers p ON p.id = pv.paper_id
             WHERE pv.paper_id IN ({ph})""",
        list(paper_ids),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_papers(paper_ids: Iterable[str]) -> dict[str, Any]:
    """
    删除一批论文，返回「删了什么、清了多少附属物」。

    三条硬规则：
    1. id 里有一个不存在 → 整批 404，不做部分删除（用户看到的清单和实际不符时，
       最不该发生的事是「以为删了 5 篇，其实删了 3 篇」）；
    2. 有论文在解析 → 整批 409，把还在跑的列出来（见 RUNNING_STAGES 的注释）；
    3. 向量点是 (arxiv_id, version) 级共享的，别的课题还挂着同一版本时**不删点**，
       只删本课题的行——否则会顺手把另一个课题的检索打死。
    """
    from app.services.storage import StorageService
    from app.services.vector_store import VectorStoreService

    t0 = time.perf_counter()
    ids = [p for p in dict.fromkeys(paper_ids) if p]
    if not ids:
        raise ValueError("paper_ids 不能为空")

    with reader() as c:
        ph = ",".join("?" * len(ids))
        found = {
            r["id"]: dict(r)
            for r in c.execute(
                f"SELECT id, arxiv_id, title, project_id FROM papers WHERE id IN ({ph})", ids
            ).fetchall()
        }
        missing = [i for i in ids if i not in found]
        if missing:
            raise PaperNotFound(missing)
        versions = _versions_of(c, ids)

        blocked: list[dict[str, Any]] = []
        for pid in ids:
            busy = next(
                (
                    v
                    for v in versions
                    if v["paper_id"] == pid and _is_running(v.get("status"), v.get("stage"))
                ),
                None,
            )
            if busy:
                blocked.append(
                    {
                        "id": pid,
                        "title": found[pid]["title"],
                        "stage": busy.get("stage"),
                        "status_label": _status_label(busy.get("status"), busy.get("stage"))[1],
                    }
                )
        if blocked:
            raise PapersBusy(blocked)

        # 同一 (arxiv_id, version) 是否被别的论文行引用 —— 决定能不能删向量点
        shared_vectors: set[tuple[str, str]] = set()
        # 同一 pdf_s3_key 是否被别的版本引用 —— 决定能不能删原件
        shared_pdfs: set[str] = set()
        for v in versions:
            other = c.execute(
                """SELECT COUNT(*) FROM paper_versions pv
                     JOIN papers p ON p.id = pv.paper_id
                    WHERE p.arxiv_id = ? AND pv.version_label = ? AND pv.paper_id NOT IN (%s)"""
                % ph,
                [v["arxiv_id"], v["version_label"], *ids],
            ).fetchone()[0]
            if other:
                shared_vectors.add((v["arxiv_id"], v["version_label"]))
            if v.get("pdf_s3_key"):
                refs = c.execute(
                    f"SELECT COUNT(*) FROM paper_versions WHERE pdf_s3_key = ? AND paper_id NOT IN ({ph})",
                    [v["pdf_s3_key"], *ids],
                ).fetchone()[0]
                if refs:
                    shared_pdfs.add(v["pdf_s3_key"])

    vs = VectorStoreService()
    storage = StorageService()
    warnings: list[str] = []
    removed = {"vectors": 0, "metas": 0, "pdfs": 0, "versions": 0, "chunks": 0}
    deleted: list[dict[str, Any]] = []

    for pid in ids:
        mine = [v for v in versions if v["paper_id"] == pid]
        try:
            for v in mine:
                key = (v["arxiv_id"], v["version_label"])
                if key not in shared_vectors:
                    n = vs.delete_chunks(v["arxiv_id"], v["version_label"])
                    removed["vectors"] += n
                    if vs.delete_paper_meta(v["arxiv_id"], v["version_label"]):
                        removed["metas"] += 1
                else:
                    warnings.append(
                        f"《{found[pid]['title'][:30]}》的向量与其它课题共用，只删条目、保留向量"
                    )
                if v.get("pdf_s3_key") and v["pdf_s3_key"] not in shared_pdfs:
                    # 只有真的存在过才计入「清理了 N 个原件」，
                    # 否则报给用户的数字里混着「本来就不在」的条目
                    if storage.delete_pdf(v["pdf_s3_key"]):
                        removed["pdfs"] += 1
        except Exception as exc:  # noqa: BLE001 - 向量清理失败就别删行，否则留幽灵
            warnings.append(f"《{found[pid]['title'][:30]}》清理索引失败，已跳过：{exc}")
            continue

        with transaction() as c:
            # 先删挂在这个版本上的引用行靠外键级联，这里显式删 papers 即可
            c.execute("DELETE FROM papers WHERE id = ?", (pid,))
        removed["versions"] += len(mine)
        removed["chunks"] += sum(int(v.get("chunk_count") or 0) for v in mine)
        deleted.append(
            {
                "id": pid,
                "title": found[pid]["title"],
                "arxiv_id": found[pid].get("arxiv_id"),
                "versions": len(mine),
            }
        )

    remaining = list_papers()
    return {
        "deleted": deleted,
        "deleted_count": len(deleted),
        "requested": len(ids),
        "removed": removed,
        "warnings": warnings,
        "summary": remaining["summary"],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
