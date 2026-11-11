"""参考文献条目（PaperRef）与正文引用位置（Citation）CRUD。"""
import re
import sqlite3
from typing import Any, Optional

from app.db import dump_json, load_json, new_id, reader, utc_now
from app.models import Citation, PaperRef
from app.repositories.base import tx


def _norm_title(s: str | None) -> str:
    """标题归一化：只保留字母数字与汉字，比较时忽略标点、大小写、空格差异。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", (s or "").lower()).strip()


# 原文条目通常自带编号："[12] ..." / "12. ..." / "12) ..."
_LEADING_NUM = re.compile(r"^\s*\[?(\d{1,3})[\].、)]\s*")


def _ref_index_of(raw: str, fallback: int) -> int:
    """
    优先用原文条目自带的编号，而不是列表顺序。
    LLM 结构化时可能调整条目顺序，若按顺序编号，正文里的 [10] 会指到错的那条。
    """
    m = _LEADING_NUM.match(raw or "")
    if m:
        n = int(m.group(1))
        if 1 <= n <= 300:
            return n
    return fallback


class ReferenceRepository:
    """论文自己列出的参考文献条目；matched_paper_id 指向课题内已入库的论文。"""

    @staticmethod
    def bulk_create(
        version_id: str,
        refs: list[dict[str, Any]],
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """
        refs 来自 BibliographyExtractor，字段是 id/title/authors/year + 原始文本。
        ref_index 取条目顺序（1 起），与正文里的 [12] 对得上。
        """
        if not refs:
            return 0

        rows = []
        used_index: set[int] = set()
        for idx, ref in enumerate(refs, start=1):
            raw = (ref.get("raw") or ref.get("text") or "").strip()
            title = (ref.get("title") or "").strip() or None
            authors = ref.get("authors") or []
            if isinstance(authors, str):
                authors = [a.strip() for a in authors.split(",") if a.strip()]
            year = ref.get("year")
            try:
                year = int(str(year)[:4]) if year else None
            except (TypeError, ValueError):
                year = None
            # 条目完全没有可用信息就跳过，避免脏行占住 ref_index
            if not raw and not title:
                continue
            if not raw:
                raw = " ".join(filter(None, [title, ", ".join(authors), str(year or "")]))

            ref_index = _ref_index_of(raw, int(ref.get("ref_index") or idx))
            # 原文分栏时同一个编号可能抽到两次，顺延到最近的空位而不是覆盖
            while ref_index in used_index:
                ref_index += 1
            used_index.add(ref_index)

            rows.append(
                (
                    new_id("ref"),
                    version_id,
                    ref_index,
                    raw,
                    title,
                    dump_json(authors),
                    year,
                )
            )

        if not rows:
            return 0

        with tx(conn) as c:
            c.execute("DELETE FROM paper_refs WHERE version_id = ?", (version_id,))
            c.executemany(
                """INSERT INTO paper_refs
                   (id, version_id, ref_index, raw_text, parsed_title, parsed_authors, parsed_year)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    @staticmethod
    def list_by_version(version_id: str) -> list[PaperRef]:
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM paper_refs WHERE version_id = ? ORDER BY ref_index",
                (version_id,),
            ).fetchall()
        return [PaperRef.from_row(r) for r in rows]

    @staticmethod
    def get(ref_id: str) -> Optional[PaperRef]:
        with reader() as c:
            row = c.execute("SELECT * FROM paper_refs WHERE id = ?", (ref_id,)).fetchone()
        return PaperRef.from_row(row) if row else None

    @staticmethod
    def get_by_index(version_id: str, ref_index: int) -> Optional[PaperRef]:
        with reader() as c:
            row = c.execute(
                "SELECT * FROM paper_refs WHERE version_id = ? AND ref_index = ?",
                (version_id, ref_index),
            ).fetchone()
        return PaperRef.from_row(row) if row else None

    @staticmethod
    def match_paper(
        ref_id: str,
        paper_id: str,
        score: float = 1.0,
        method: str = "title_fuzzy",
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """把某条参考文献关联到课题内的论文；未匹配时 matched_paper_id 保持 NULL。"""
        with tx(conn) as c:
            cur = c.execute(
                """UPDATE paper_refs
                   SET matched_paper_id = ?, match_score = ?, match_method = ?
                   WHERE id = ?""",
                (paper_id, score, method, ref_id),
            )
            return cur.rowcount > 0

    @staticmethod
    def unmatched(version_id: str) -> list[PaperRef]:
        """未匹配的条目，后续「一键补库」从这批里挑。"""
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM paper_refs WHERE version_id = ? AND matched_paper_id IS NULL ORDER BY ref_index",
                (version_id,),
            ).fetchall()
        return [PaperRef.from_row(r) for r in rows]

    @staticmethod
    def match_to_project(
        version_id: str, project_id: str, conn: sqlite3.Connection | None = None
    ) -> int:
        """
        把该版本的参考文献条目关联到课题内已入库的论文。
        匹配顺序：原文含 arXiv ID（最可靠）→ 归一化标题完全相等 → 标题互相包含。
        匹配不上的保持 NULL，不猜——错误关联比没有关联更糟。
        """
        with tx(conn) as c:
            # 读也必须走同一个连接：外层事务尚未提交时，另开连接看不到刚写入的行
            refs = c.execute(
                """SELECT id, raw_text, parsed_title FROM paper_refs
                    WHERE version_id = ? AND matched_paper_id IS NULL""",
                (version_id,),
            ).fetchall()
            papers = c.execute(
                "SELECT id, arxiv_id, title FROM papers WHERE project_id = ?", (project_id,)
            ).fetchall()

            if not refs or not papers:
                return 0

            index = [(p["id"], _norm_title(p["title"]), (p["arxiv_id"] or "")) for p in papers]
            updates: list[tuple[str, float, str, str]] = []

            for ref in refs:
                raw = ref["raw_text"] or ""
                pid = score = None
                method = None

                for paper_id, _nt, aid in index:
                    if aid and len(aid) >= 6 and aid in raw:
                        pid, score, method = paper_id, 1.0, "arxiv_id"
                        break

                if not pid:
                    nt = _norm_title(ref["parsed_title"])
                    if len(nt) >= 8:
                        for paper_id, title_norm, _aid in index:
                            if nt == title_norm:
                                pid, score, method = paper_id, 0.95, "title_exact"
                                break
                        if not pid:
                            for paper_id, title_norm, _aid in index:
                                if len(title_norm) >= 12 and (nt in title_norm or title_norm in nt):
                                    pid, score, method = paper_id, 0.8, "title_fuzzy"
                                    break

                if pid:
                    updates.append((pid, score, method, ref["id"]))

            if not updates:
                return 0

            c.executemany(
                """UPDATE paper_refs
                   SET matched_paper_id = ?, match_score = ?, match_method = ?
                   WHERE id = ?""",
                updates,
            )
        return len(updates)


class CitationRepository:
    """正文中某句话引用了某条参考文献的位置记录。"""

    @staticmethod
    def create(
        ref_id: str,
        sentence: str,
        chunk_id: str | None = None,
        page: int | None = None,
        bbox: list[float] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Citation:
        cid = new_id("cit")
        now = utc_now()
        with tx(conn) as c:
            c.execute(
                """INSERT INTO citations (id, ref_id, chunk_id, page, bbox, sentence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    ref_id,
                    chunk_id,
                    page,
                    dump_json(bbox) if bbox else None,
                    sentence,
                    now,
                ),
            )
        return Citation(
            id=cid,
            ref_id=ref_id,
            sentence=sentence,
            chunk_id=chunk_id,
            page=page,
            bbox=bbox,
            created_at=now,
        )

    @staticmethod
    def bulk_create(
        items: list[dict[str, Any]], conn: sqlite3.Connection | None = None
    ) -> int:
        """批量写入，迁移/重解析时用。"""
        if not items:
            return 0
        now = utc_now()
        rows = [
            (
                new_id("cit"),
                it["ref_id"],
                it.get("chunk_id"),
                it.get("page"),
                dump_json(it["bbox"]) if it.get("bbox") else None,
                it.get("sentence", ""),
                now,
            )
            for it in items
            if it.get("ref_id") and it.get("sentence")
        ]
        if not rows:
            return 0
        with tx(conn) as c:
            c.executemany(
                """INSERT INTO citations (id, ref_id, chunk_id, page, bbox, sentence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    @staticmethod
    def list_by_ref(ref_id: str) -> list[Citation]:
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM citations WHERE ref_id = ? ORDER BY page, id", (ref_id,)
            ).fetchall()
        return [Citation.from_row(r) for r in rows]

    @staticmethod
    def list_by_chunk(chunk_id: str) -> list[Citation]:
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM citations WHERE chunk_id = ? ORDER BY page", (chunk_id,)
            ).fetchall()
        return [Citation.from_row(r) for r in rows]

    @staticmethod
    def provenance(citation_id: str) -> Optional[dict[str, Any]]:
        """
        单条引用的完整溯源：哪篇论文、哪个版本、第几页、哪个框、哪句话。
        直接查 v_citation_provenance 视图，五张表的 join 在库里完成。
        """
        with reader() as c:
            row = c.execute(
                "SELECT * FROM v_citation_provenance WHERE citation_id = ?", (citation_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["bbox"] = load_json(d.get("bbox"), None)
        return d

    @staticmethod
    def provenance_by_sentence(version_id: str, keyword: str) -> list[dict[str, Any]]:
        """按句子关键词在该版本内反查全部引用位置。"""
        with reader() as c:
            rows = c.execute(
                """SELECT * FROM v_citation_provenance
                    WHERE version_id = ? AND sentence LIKE ?
                    ORDER BY page""",
                (version_id, f"%{keyword}%"),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["bbox"] = load_json(d.get("bbox"), None)
            out.append(d)
        return out
