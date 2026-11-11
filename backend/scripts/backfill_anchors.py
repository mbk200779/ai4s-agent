"""
给历史 chunk 补句子级锚点（不重新解析整篇、不重算向量）。

为什么需要这个脚本：
05 之前解析的文档，chunks 里只有块级 bbox，没有句子级锚点。让用户"重新上传一次"
是最差的解法——向量、参考文献、引用位置都要重算，而它们本来没错。
锚点是纯派生数据：拿原文 PDF 重新对齐一遍就能补出来，几秒钟一篇。

做法：
1. 用当前解析器把 PDF 逐页扫一遍，只取行文本 + 行矩形（不渲染、不走 OCR、不调 LLM）；
2. 按 chunk_index 顺序把库里已有的 chunk 文本对齐到行上——顺序必须单调，
   同一页的多个 chunk 依次往后消费行，避免"后一段把前一段的行又认一遍"；
3. 表格按 `table_id` 里的序号对回 pdfplumber 的表格，重建单元格锚点；
4. 写完立刻用 services/anchors.validate_document 自检，有 error 的不落库。

用法：
    python -m scripts.backfill_anchors --arxiv 1810.04805
    python -m scripts.backfill_anchors --arxiv 1810.04805 --dry-run
    python -m scripts.backfill_anchors --all --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from app.db import reader, transaction
from app.repositories import ChunkRepository, PaperVersionRepository
from app.services import anchors
from app.services.pdf_parser import PDFParser
from app.services.storage import StorageService


def _versions(arxiv_ids: list[str] | None, limit: int | None) -> list[dict]:
    sql = """
        SELECT v.id, p.arxiv_id, p.title, v.version_label, v.pdf_s3_key, v.status
          FROM paper_versions v JOIN papers p ON p.id = v.paper_id
         WHERE v.status = 'indexed'
    """
    args: list = []
    if arxiv_ids:
        sql += " AND p.arxiv_id IN (%s)" % ",".join("?" for _ in arxiv_ids)
        args.extend(arxiv_ids)
    sql += " ORDER BY v.ingested_at DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with reader() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _coverage(spans: list[tuple], total: int) -> float:
    if not spans or total <= 0:
        return 0.0
    return sum(max(0, e - s) for s, e, *_ in spans) / total


def _pdf_path(version: dict, override: str | None) -> Path:
    if override:
        return Path(override)
    key = version.get("pdf_s3_key")
    if not key:
        raise FileNotFoundError("版本没有 pdf_s3_key，请用 --pdf 指定本地文件")
    data = StorageService().get_pdf_bytes(key)
    tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
    tmp.write_bytes(data)
    return tmp


def _table_candidates(pdf_path: Path, page_no: int) -> list[dict]:
    parser = PDFParser(str(pdf_path))
    try:
        tables = parser.extract_tables_with_pdfplumber(page_no - 1, page_no - 1, with_cells=True)
    except Exception:
        return []
    return [t for t in tables if t["page"] == page_no]


def _match_table(chunks_text: str, candidates: list[dict]) -> dict | None:
    """
    把库里的表格 chunk 对回 pdfplumber 的表。

    不能只按序号对：历史数据里 table_id 常常是空的（旧解析器没写），
    而且同一页可能抽出好几张"表"，序号与建库时的顺序不一定一致。
    所以按文本前缀比：表格文本是解析器自己拼的，同一份 PDF 的同一张表
    前缀一定相同，不同表几乎不可能同前缀。
    """
    target = anchors._norm_view(chunks_text)[0][:40]
    if not target:
        return None
    for cand in candidates:
        if anchors._norm_view(cand.get("text") or "")[0][:40] == target:
            return cand
    # 退一步：按 token 重合度挑最像的，处理"同一张表被 pdfplumber 版本差异切得不同"的情况
    want = set(anchors._norm_view(chunks_text)[0].split())
    best, best_hit = None, 0.0
    for cand in candidates:
        got = set(anchors._norm_view(cand.get("text") or "")[0].split())
        if not got:
            continue
        hit = len(want & got) / max(1, len(want))
        if hit > best_hit:
            best, best_hit = cand, hit
    return best if best_hit >= 0.6 else None


def backfill_version(version: dict, pdf_path: Path, dry_run: bool) -> dict:
    started = time.time()
    chunks = ChunkRepository.list_by_version(version["id"])
    if not chunks:
        return {"arxiv_id": version["arxiv_id"], "skipped": "no chunks"}

    by_page: dict[int, list] = {}
    for ch in chunks:
        if ch.page:
            by_page.setdefault(int(ch.page), []).append(ch)

    parser = PDFParser(str(pdf_path))
    total_pages = parser._page_count()

    updates: list[tuple[str, str]] = []
    stats = {
        "arxiv_id": version["arxiv_id"],
        "title": version.get("title", "")[:48],
        "chunks": len(chunks),
        "eligible": len([c for c in chunks if c.page]),
        "anchored": 0,
        "text_miss": 0,
        "lines": 0,
        "table_miss": 0,
        "rejected": 0,
        "cells": 0,
        "errors": [],
        "warnings": 0,
    }

    for page in parser.iter_pages(0, total_pages - 1, render=False):
        page_no = page["page"]
        page_chunks = by_page.get(page_no)
        if not page_chunks:
            continue
        lines = page.get("lines") or []
        page_size = page.get("page_size")
        text_layer = bool(page.get("text_layer", True))

        text_chunks = sorted(
            (c for c in page_chunks if c.chunk_type in ("text", "reference")),
            key=lambda c: c.chunk_index,
        )
        cursor = 0
        for ch in text_chunks:
            if ch.chunk_type == "reference" and ch.page:
                # 参考文献一条一行：整段对齐后逐行出锚点，切句法是切不动的。
                # 注意用 references_lines 而不是 lines：解析器把文献区和正文分开收集，
                # 文献块不会进 page_lines，拿正文行去对文献文本必然是空。
                doc = anchors.build_line_document(
                    text=ch.text, lines=page.get("references_lines") or [], page=page_no,
                    page_size=page_size, text_layer=text_layer,
                )
                problems = anchors.validate_document(ch.text, doc, page_size=page_size)
                if any(p["level"] == "error" for p in problems) or not (doc.get("items") or []):
                    stats["text_miss"] += 1
                    continue
                updates.append((json.dumps(doc, ensure_ascii=False, separators=(",", ":")), ch.id))
                stats["anchored"] += 1
                stats["lines"] += len(doc.get("items") or [])
                continue

            spans = anchors.align_lines(ch.text, lines, start=cursor) if lines else []
            # 同一页的 chunk 并不保证按行顺序排列（标题/摘要/页眉会互相穿插），
            # 单调游标一撞车就会整段对不上。这里用"覆盖率"兜底：
            # 游标起点的结果覆盖不到六成，就从页首重来一次，取覆盖更多的那个。
            if lines and _coverage(spans, len(ch.text)) < 0.6:
                alt = anchors.align_lines(ch.text, lines, start=0)
                if _coverage(alt, len(ch.text)) > _coverage(spans, len(ch.text)):
                    spans = alt
            if not spans:
                stats["text_miss"] += 1
                continue
            lo, hi = spans[0][3], spans[-1][3]
            doc = anchors.build_document(
                text=ch.text,
                lines=lines[lo: hi + 1],
                page=page_no,
                page_size=page_size,
                text_layer=text_layer,
            )
            problems = anchors.validate_document(ch.text, doc, page_size=page_size)
            errors = [p for p in problems if p["level"] == "error"]
            stats["warnings"] += sum(1 for p in problems if p["level"] == "warning")
            if errors:
                stats["rejected"] += 1
                stats["errors"].append(
                    {"chunk_id": ch.id, "page": page_no, "why": errors[0]["msg"]}
                )
                continue
            items = doc.get("items") or []
            if not items:
                stats["text_miss"] += 1
                continue
            updates.append((json.dumps(doc, ensure_ascii=False, separators=(",", ":")), ch.id))
            stats["anchored"] += 1
            cursor = hi + 1

        tables = [c for c in page_chunks if c.chunk_type == "table"]
        if tables:
            candidates = _table_candidates(pdf_path, page_no)
            for ch in tables:
                cand = _match_table(ch.text, candidates)
                if not cand:
                    stats["table_miss"] += 1
                    continue
                text, cells = cand["text"], cand.get("cell_items") or []
                doc = anchors.build_cell_document(
                    text=text, cells=cells, page=page_no, page_size=page_size,
                    text_layer=text_layer,
                )
                problems = anchors.validate_document(text, doc, page_size=page_size)
                errors = [p for p in problems if p["level"] == "error"]
                if errors:
                    stats["rejected"] += 1
                    stats["errors"].append(
                        {"chunk_id": ch.id, "page": page_no, "why": errors[0]["msg"]}
                    )
                    continue
                updates.append((json.dumps(doc, ensure_ascii=False, separators=(",", ":")), ch.id))
                stats["anchored"] += 1
                stats["cells"] += len(doc.get("items") or [])

    if not dry_run and updates:
        with transaction() as c:
            c.executemany("UPDATE chunks SET anchors = ? WHERE id = ?", updates)

    stats["elapsed_s"] = round(time.time() - started, 1)
    stats["dry_run"] = dry_run
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给历史 chunk 补句子级锚点")
    ap.add_argument("--arxiv", action="append", default=None, help="只处理这些 arxiv_id，可重复")
    ap.add_argument("--all", action="store_true", help="处理全部 indexed 版本")
    ap.add_argument("--limit", type=int, default=None, help="最多处理几个版本")
    ap.add_argument("--pdf", default=None, help="指定本地 PDF（仅单篇时有效）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args(argv)

    if not args.arxiv and not args.all:
        ap.error("需要 --arxiv 或 --all")

    versions = _versions(args.arxiv, args.limit)
    if not versions:
        print("没有匹配的 indexed 版本")
        return 1

    print(f"待处理版本 {len(versions)} 个")
    failed = 0
    for v in versions:
        try:
            pdf_path = _pdf_path(v, args.pdf if len(versions) == 1 else None)
        except Exception as exc:
            print(f"  [skip] {v['arxiv_id']}: {exc}")
            failed += 1
            continue
        stats = backfill_version(v, pdf_path, args.dry_run)
        if "skipped" in stats:
            print(f"  [skip] {v['arxiv_id']}: {stats['skipped']}")
            continue
        ratio = stats["anchored"] / max(1, stats["eligible"])
        print(
            f"  {stats['arxiv_id']:<24} chunks={stats['chunks']:<4} "
            f"anchored={stats['anchored']:<4} ({ratio:.0%}) cells={stats['cells']:<4} "
            f"miss={stats['text_miss']}/{stats['table_miss']:<3} rejected={stats['rejected']:<3} "
            f"warn={stats['warnings']:<3} {stats['elapsed_s']}s"
        )
        for err in stats["errors"][:3]:
            print(f"      ! {err['page']}p {err['chunk_id']}: {err['why']}")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
