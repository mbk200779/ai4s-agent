"""
锚点数据校验：把「高亮框是否真的框住了那句话」变成一条可重复执行的命令。

四层校验，逐层加重：

L1 结构      —— JSON 能不能读、ref_id 是否唯一、start/end 是否越界、rects 是否 4 个数。
L2 一致性    —— 每条的字符区间切出来的文本必须非空，且相邻锚点不重叠、按顺序。
L3 版面约束  —— 矩形必须在页面内，且**不能占满整页**（"不允许整页高亮"是硬约束）。
L4 内容对齐  —— （--deep）回原文 PDF 取该矩形内的词，与锚点句做词面重合度比较。
                这一层才真正回答"框住的是不是这句话"，前两层只能保证"框看起来合法"。

退出码：0 全通过；1 有 error；2 参数/环境问题。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from app.db import reader
from app.repositories import ChunkRepository
from app.services import anchors
from app.services.storage import StorageService

TOKEN_RE = __import__("re").compile(r"[A-Za-z]{2,}|\d+(?:[.,]\d+)*")


def _versions(arxiv_ids: list[str] | None, limit: int | None) -> list[dict]:
    sql = """
        SELECT v.id, p.arxiv_id, p.title, v.pdf_s3_key, v.chunk_count
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


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(text or "")}


def deep_check(pdf_path: Path, page: int, text: str, item: dict, min_overlap: float) -> dict | None:
    """
    L4：把矩形里的词读出来，和锚点句比词面重合度。

    为什么用"矩形内取词"而不是精确坐标比对：
    PDF 里没有"这句话的官方框"，能拿到的只有字形位置。矩形内取词再比文本，
    等价于问"读者按这个框去看，看到的是不是这句话"，这正是要验的东西。
    """
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        if page < 1 or page > len(doc):
            return {"ok": False, "why": f"页码 {page} 超出 PDF 范围"}
        pg = doc.load_page(page - 1)
        want = _tokens(anchors.slice_text(text, item))
        if not want:
            return None
        got: set[str] = set()
        for rect in item.get("rects") or [item.get("bbox")]:
            if not rect:
                continue
            area = fitz.Rect(rect)
            for word in pg.get_text("words", clip=area):
                got.add(str(word[4]).lower().strip(".,;:()[]"))
        overlap = len(want & got) / len(want)
        return {
            "ok": overlap >= min_overlap,
            "overlap": round(overlap, 3),
            "ref_id": item.get("ref_id"),
            "why": None if overlap >= min_overlap else f"框内只覆盖了 {overlap:.0%} 的词",
        }
    finally:
        doc.close()


def check_version(version: dict, sample: int, min_overlap: float) -> dict:
    started = time.time()
    chunks = ChunkRepository.list_by_version(version["id"])
    report = {
        "arxiv_id": version["arxiv_id"],
        "title": (version.get("title") or "")[:48],
        "version_id": version["id"],
        "chunks": len(chunks),
        "with_anchors": 0,
        "no_anchors": 0,
        "items": 0,
        "errors": [],
        "warnings": 0,
        "precisions": {},
        "deep": {"checked": 0, "failed": []},
    }
    for ch in chunks:
        doc = ch.anchors
        if not doc or not (doc.get("items") or []):
            report["no_anchors"] += 1
            continue
        report["with_anchors"] += 1
        items = doc.get("items") or []
        report["items"] += len(items)
        for item in items:
            key = str(item.get("precision") or "unknown")
            report["precisions"][key] = report["precisions"].get(key, 0) + 1

        problems = anchors.validate_document(ch.text, doc, page_size=doc.get("page_size"))
        for p in problems:
            entry = {
                "chunk_id": ch.id,
                "page": ch.page,
                "chunk_type": ch.chunk_type,
                "code": p["code"],
                "msg": p["msg"],
            }
            if p["level"] == "error":
                report["errors"].append(entry)
            else:
                report["warnings"] += 1
                if len(report["errors"]) == 0 and report["warnings"] <= 3:
                    report["errors"].append({**entry, "level": "warning"})

    if sample and version.get("pdf_s3_key"):
        try:
            data = StorageService().get_pdf_bytes(version["pdf_s3_key"])
        except Exception as exc:
            report["deep"]["error"] = f"取 PDF 失败：{exc}"
            return report
        tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
        tmp.write_bytes(data)
        try:
            checked = 0
            for ch in chunks:
                items = (ch.anchors or {}).get("items") or []
                for item in items:
                    if checked >= sample:
                        break
                    res = deep_check(tmp, int(ch.page or 0), ch.text, item, min_overlap)
                    if res is None:
                        continue
                    checked += 1
                    if not res["ok"]:
                        report["deep"]["failed"].append(
                            {"chunk_id": ch.id, "page": ch.page, **res}
                        )
                if checked >= sample:
                    break
            report["deep"]["checked"] = checked
        finally:
            tmp.unlink(missing_ok=True)

    report["elapsed_s"] = round(time.time() - started, 1)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验句子级锚点数据")
    ap.add_argument("--arxiv", action="append", default=None, help="只校验这些 arxiv_id")
    ap.add_argument("--limit", type=int, default=None, help="最多校验几个版本")
    ap.add_argument("--sample", type=int, default=0, help="每篇抽样做 L4 内容对齐（0 关闭）")
    ap.add_argument("--min-overlap", type=float, default=0.5, help="L4 词面重合度阈值")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--strict", action="store_true", help="warning 也算失败")
    args = ap.parse_args(argv)

    versions = _versions(args.arxiv, args.limit)
    if not versions:
        print("没有匹配的 indexed 版本")
        return 2

    reports = []
    print(f"{'arxiv_id':<24}{'chunks':>7}{'anchored':>9}{'items':>7}{'err':>5}{'warn':>5}{'cov':>7}  precisions")
    for v in versions:
        rep = check_version(v, args.sample, args.min_overlap)
        reports.append(rep)
        cov = rep["with_anchors"] / max(1, rep["chunks"])
        print(
            f"{rep['arxiv_id']:<24}{rep['chunks']:>7}{rep['with_anchors']:>9}{rep['items']:>7}"
            f"{len([e for e in rep['errors'] if e.get('level') != 'warning']):>5}"
            f"{rep['warnings']:>5}{cov:>6.0%}  {rep['precisions']}"
        )
        for err in rep["errors"][:5]:
            print(f"    - [{err['code']}] p{err['page']} {err['chunk_id']}: {err['msg']}")
        deep = rep["deep"]
        if deep.get("checked"):
            print(
                f"    deep: 抽样 {deep['checked']} 条，"
                f"不重合 {len(deep['failed'])} 条"
            )
            for bad in deep["failed"][:3]:
                print(f"      ! p{bad['page']} {bad['ref_id']}: {bad['why']}")

    total_err = sum(len([e for e in r["errors"] if e.get("level") != "warning"]) for r in reports)
    total_deep = sum(len(r["deep"]["failed"]) for r in reports)
    total_warn = sum(r["warnings"] for r in reports)
    print(
        f"\n合计：error={total_err} warning={total_warn}"
        + (f" deep_failed={total_deep}" if args.sample else "")
    )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"reports": reports, "generated_at": time.strftime("%FT%T")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 已写入 {args.json_out}")

    if total_err or total_deep:
        return 1
    if args.strict and total_warn:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
