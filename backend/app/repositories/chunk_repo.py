"""文本块（Chunk）CRUD。"""
import sqlite3
import threading
from typing import Any, Iterable, Optional

from app.db import dump_json, load_json, new_id, reader, utc_now
from app.models import Chunk
from app.repositories.base import tx

# 向量点 ID → chunk 行 ID 的进程级缓存。
#
# 为什么需要它：检索结果里的 `hit.id` 是 Qdrant 的点 ID（uuid5），而锚点、引用记录、
# provenance 全挂在 chunks.id（chk_xxx）上。两者不是一回事，检索层却只拿得到前者。
# 不建这层映射，`anchors_map()` 按检索结果去查永远是空的——表现是"每篇论文都没锚点"，
# 而这种"静默地全空"比报错更难查。
# 语料是"入库后不再变"的，所以缓存常驻；新入库的 chunk 走 miss 分支补查一次。
_POINT_MAP: dict[str, str] = {}
_POINT_MAP_LOCK = threading.Lock()


def _pid_key(pid: str) -> str:
    """
    点 ID 的规范形式：去连字符 + 小写。

    同一个 UUID 有三副面孔：Qdrant 回包 `8c161695-777e-...`、
    入库写的 `uuid4().hex` 无连字符形式、以及小写/大写混写。
    它们在语义上是同一个点，在字符串比较里却是三个值。
    """
    return str(pid or "").replace("-", "").lower()

# 解析器里的 kind 与对外暴露的 chunk_type 不是一套词汇：
# paragraph/chapter 在溯源语境下都是正文，统一归为 text。
_KIND_MAP = {
    "paragraph": "text",
    "chunk": "text",
    "chapter": "text",
    "text": "text",
    "table": "table",
    "figure": "figure",
    "formula": "formula",
    "reference": "reference",
}


def estimate_tokens(text: str) -> int:
    """
    粗略 token 估算：英文约 4 字符/token，中文约 1.6 字符/token。
    不引入 tiktoken 是为了不给镜像再多一个依赖；这个值只用于预算与排序，
    不参与计费，误差可以接受。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(cjk / 1.6 + other / 4))


def _anchors_json(doc: Any) -> Optional[str]:
    """锚点文档序列化；没有锚点时写 NULL（原因见 ingest/pipeline.py 同名函数）。"""
    if not doc or not doc.get("items"):
        return None
    return dump_json(doc)


class ChunkRepository:
    """chunk 是溯源的最小定位单元：version + page + bbox + section_path。"""

    @staticmethod
    def bulk_create(
        version_id: str,
        fragments: list[dict[str, Any]],
        conn: sqlite3.Connection | None = None,
        replace: bool = True,
        start_index: int = 0,
    ) -> int:
        """
        把一个版本的片段写入。

        replace=True 时先清空旧 chunk，保证重复解析（改了解析器后重跑）不会重复堆积。
        citations.chunk_id 是 ON DELETE SET NULL，删 chunk 不会连带删掉引用记录。

        长文档分批写入时用 replace=False + start_index：
        start_index 是这一批在 chunk_index 上的起点，与 Qdrant 点 ID 里的序号对齐，
        批次之间不会互相覆盖；写之前先删掉本批区间内的旧行，
        使得"同一批重试"天然幂等。
        """
        if not fragments:
            return 0

        now = utc_now()
        rows = []
        for idx, frag in enumerate(fragments, start=start_index):
            text = (frag.get("text") or "").strip()
            if not text:
                continue
            kind = frag.get("kind", "paragraph")
            rows.append(
                (
                    new_id("chk"),
                    version_id,
                    idx,
                    frag.get("page"),
                    dump_json(frag["bbox"]) if frag.get("bbox") else None,
                    frag.get("section_path") or frag.get("section") or "",
                    _KIND_MAP.get(kind, "text"),
                    frag.get("table_id") or (f"Table {frag['index']}" if kind == "table" and frag.get("index") else None),
                    frag.get("latex"),
                    text,
                    estimate_tokens(text),
                    frag.get("qdrant_point_id"),
                    _anchors_json(frag.get("anchors")),
                    now,
                )
            )

        if not rows:
            return 0

        with tx(conn) as c:
            if replace:
                c.execute("DELETE FROM chunks WHERE version_id = ?", (version_id,))
            else:
                # 只清本批区间：重试同一批时不会留下重复行，也不会碰到别的批次
                c.execute(
                    "DELETE FROM chunks WHERE version_id = ? AND chunk_index >= ? AND chunk_index < ?",
                    (version_id, start_index, start_index + len(fragments)),
                )
            c.executemany(
                """INSERT INTO chunks
                   (id, version_id, chunk_index, page, bbox, section_path, chunk_type,
                    table_id, latex, text, token_count, qdrant_point_id, anchors, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        # 新写入的 chunk 立刻进点 ID 缓存：否则刚解析完的论文第一次被检索到时，
        # 每条命中都要多一次 DB 补查，而且并发下会重复查同一批。
        with _POINT_MAP_LOCK:
            for row in rows:
                if row[11]:
                    _POINT_MAP[_pid_key(str(row[11]))] = row[0]
        return len(rows)

    @staticmethod
    def latest_version_id(arxiv_id: str) -> Optional[str]:
        """某篇论文最近一次解析成功的版本。阅读器直接按 arxiv_id 找锚点，不关心版本。"""
        with reader() as c:
            row = c.execute(
                """
                SELECT v.id FROM paper_versions v
                JOIN papers p ON p.id = v.paper_id
                WHERE p.arxiv_id = ? AND v.status = 'indexed'
                ORDER BY v.ingested_at DESC LIMIT 1
                """,
                (arxiv_id,),
            ).fetchone()
        return row["id"] if row else None

    @staticmethod
    def anchor_profile(arxiv_id: str) -> dict[str, Any]:
        """
        文档级锚点概览：有多少 chunk、多少有锚点、精度分布、扫描页数。

        存在的意义是让"这个文档能不能精确定位"变成一个**可查询的事实**，
        而不是等用户点了角标、看到高亮不出来才知道。前端在打开阅读器之前
        就能据此提示「该文档未建立精细锚点」。
        """
        import json as _json

        version_id = ChunkRepository.latest_version_id(arxiv_id)
        if not version_id:
            return {
                "arxiv_id": arxiv_id,
                "version_id": None,
                "chunks": 0,
                "with_anchors": 0,
                "coverage": 0.0,
                "precisions": {},
                "ocr_chunks": 0,
                "cell_chunks": 0,
                "pages": [],
                "has_anchors": False,
            }
        with reader() as c:
            rows = c.execute(
                "SELECT page, chunk_type, anchors FROM chunks WHERE version_id = ?",
                (version_id,),
            ).fetchall()

        precisions: dict[str, int] = {}
        with_anchors = 0
        ocr_chunks = 0
        cell_chunks = 0
        no_page = 0
        eligible = 0
        pages: set[int] = set()
        for row in rows:
            # 结构化的参考文献条目（bibliography 抽取出来的）没有页码，
            # 它们本来就不该有版面锚点，算进覆盖率只会把数字压低到没有意义。
            if not row["page"]:
                no_page += 1
                continue
            eligible += 1
            raw = row["anchors"]
            if not raw:
                continue
            try:
                doc = _json.loads(raw)
            except ValueError:
                continue
            items = doc.get("items") or []
            if not items:
                continue
            with_anchors += 1
            pages.add(int(row["page"] or 0))
            if doc.get("precision") == "ocr" or not doc.get("text_layer", True):
                ocr_chunks += 1
            if row["chunk_type"] == "table":
                cell_chunks += 1
            for item in items:
                key = str(item.get("precision") or "unknown")
                precisions[key] = precisions.get(key, 0) + 1

        total = len(rows)
        return {
            "arxiv_id": arxiv_id,
            "version_id": version_id,
            "chunks": total,
            # eligible = 有页码、理论上可锚定的片段（排除结构化文献条目）
            "eligible": eligible,
            "no_page": no_page,
            "with_anchors": with_anchors,
            "coverage": round(with_anchors / eligible, 4) if eligible else 0.0,
            "precisions": precisions,
            "ocr_chunks": ocr_chunks,
            "cell_chunks": cell_chunks,
            "pages": sorted(p for p in pages if p),
            "has_anchors": with_anchors > 0,
        }

    @staticmethod
    def resolve_point_ids(point_ids: Iterable[str]) -> dict[str, str]:
        """
        把 Qdrant 点 ID 批量换成 chunks.id。命中不了的保留原样（调用方自己决定怎么用）。

        走"先查缓存、剩下的批量补一次"的策略而不是逐条查：
        一次检索最多 20 条候选，逐条查就是 20 次 SQLite 往返；
        而语料规模固定，补查只发生在新入库的第一批查询上。

        **UUID 的两种写法必须当作同一个 ID**（14 实测踩到）：
        Qdrant 回包里的点 ID 是带连字符的 `8c161695-777e-...`，
        而入库时写进 `chunks.qdrant_point_id` 的是 `uuid4().hex` 的无连字符形式。
        按原串比对必然全部落空，于是 chunk_id 退化成 point_id——
        不报错，只是之后按 chunk_id 查锚点、查片段全部查不到，
        表现为"引用能显示、框不到原句"，且引用准确率无从复核（评测里连被引片段都找不回来）。
        所以比较与缓存都按"去连字符 + 小写"归一化，回填时再用调用方给的原始串做 key。
        """
        ids = [str(p) for p in point_ids if p]
        if not ids:
            return {}
        out: dict[str, str] = {}
        missing: list[str] = []
        with _POINT_MAP_LOCK:
            for pid in ids:
                hit = _POINT_MAP.get(_pid_key(pid))
                if hit:
                    out[pid] = hit
                else:
                    missing.append(pid)

        if missing:
            keys = list({_pid_key(p) for p in missing})
            placeholders = ",".join("?" for _ in keys)
            with reader() as c:
                rows = c.execute(
                    "SELECT id, qdrant_point_id FROM chunks "
                    f"WHERE lower(replace(qdrant_point_id, '-', '')) IN ({placeholders})",
                    keys,
                ).fetchall()
            by_key = {
                _pid_key(row["qdrant_point_id"]): row["id"]
                for row in rows
                if row["qdrant_point_id"]
            }
            with _POINT_MAP_LOCK:
                _POINT_MAP.update(by_key)
            for pid in missing:
                hit = by_key.get(_pid_key(pid))
                if hit:
                    out[pid] = hit
        return out

    @staticmethod
    def get(chunk_id: str) -> Optional[Chunk]:
        with reader() as c:
            row = c.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        return Chunk.from_row(row) if row else None

    @staticmethod
    def anchors_map(chunk_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """
        一次取回多条 chunk 的锚点文档。

        为什么单独开一个查询而不是让调用方逐条 get()：
        一次问答最多 8 条引用，逐条查就是 8 次往返；而锚点只在"拼引用"这一步用得到，
        放进 RetrievalResult 会让每条检索路径都背着它走。
        返回里显式保留 None，让调用方能区分"没有锚点"与"chunk 不存在"。
        """
        import json as _json

        ids = [cid for cid in dict.fromkeys(chunk_ids) if cid]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with reader() as c:
            rows = c.execute(
                f"SELECT id, anchors FROM chunks WHERE id IN ({placeholders})", ids
            ).fetchall()
        out: dict[str, dict[str, Any] | None] = {cid: None for cid in ids}
        for row in rows:
            raw = row["anchors"]
            if not raw:
                continue
            try:
                out[row["id"]] = _json.loads(raw)
            except ValueError:
                out[row["id"]] = None
        return out

    @staticmethod
    def get_by_index(version_id: str, chunk_index: int) -> Optional[Chunk]:
        with reader() as c:
            row = c.execute(
                "SELECT * FROM chunks WHERE version_id = ? AND chunk_index = ?",
                (version_id, chunk_index),
            ).fetchone()
        return Chunk.from_row(row) if row else None

    @staticmethod
    def list_by_version(
        version_id: str, chunk_type: str | None = None, limit: int | None = None
    ) -> list[Chunk]:
        sql = "SELECT * FROM chunks WHERE version_id = ?"
        args: list = [version_id]
        if chunk_type:
            sql += " AND chunk_type = ?"
            args.append(chunk_type)
        sql += " ORDER BY chunk_index"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with reader() as c:
            rows = c.execute(sql, args).fetchall()
        return [Chunk.from_row(r) for r in rows]

    @staticmethod
    def count_by_version(version_id: str) -> int:
        with reader() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE version_id = ?", (version_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def list_range(
        version_id: str,
        offset: int,
        limit: int,
        chunk_type: str | None = None,
    ) -> list[Chunk]:
        """
        按 chunk_index 顺序取一个窗口。
        引用抽取必须分窗而不是一次 list_by_version：
        541 页教材有近万个 chunk，一次读进内存就是几十 MB 的常驻峰值。
        """
        sql = "SELECT * FROM chunks WHERE version_id = ?"
        args: list = [version_id]
        if chunk_type:
            sql += " AND chunk_type = ?"
            args.append(chunk_type)
        sql += " ORDER BY chunk_index LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with reader() as c:
            rows = c.execute(sql, args).fetchall()
        return [Chunk.from_row(r) for r in rows]

    @staticmethod
    def find_by_qdrant_point(point_id: str) -> Optional[Chunk]:
        """检索结果只带 Qdrant point id，用它可以立刻换成结构化 chunk。"""
        with reader() as c:
            row = c.execute(
                "SELECT * FROM chunks WHERE qdrant_point_id = ? LIMIT 1", (point_id,)
            ).fetchone()
        return Chunk.from_row(row) if row else None

    @staticmethod
    def search_text(version_id: str, keyword: str, limit: int = 20) -> list[Chunk]:
        """版本内的朴素子串检索，用于「这句话出自哪一段」这类小范围定位。"""
        with reader() as c:
            rows = c.execute(
                "SELECT * FROM chunks WHERE version_id = ? AND text LIKE ? ORDER BY chunk_index LIMIT ?",
                (version_id, f"%{keyword}%", limit),
            ).fetchall()
        return [Chunk.from_row(r) for r in rows]

    @staticmethod
    def load_bbox(chunk: Chunk) -> list[float] | None:
        """bbox 在 from_row 阶段已经反序列化成 list，这里只做 None 兜底。"""
        return chunk.bbox if chunk.bbox else None
