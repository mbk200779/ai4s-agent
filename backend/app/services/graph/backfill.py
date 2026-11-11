"""
参考文献回填：从已存的 PDF 重抽条目，并重建正文引用位置。

给两类情况用：
- 老数据是在 LLM 抽取退化时入库的（条目被合并成一条、或按图注数字切碎），
  图谱的边全靠条目对应关系，这种数据必须重抽；
- 空态提示里的「重抽参考文献」按钮。

不重跑整个解析流程：正文 chunk、向量、指纹都不用动，只替换
`paper_refs` 与 `citations` 两张表里属于该版本的行，几秒完成。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Sequence

from app.services.graph import markers, refparse, store


def _rebuild_citations(version_id: str) -> dict[str, Any]:
    refs = store.refs_of(version_id)
    chunks = store.body_chunks(version_id)
    rows, stats = markers.citation_rows(chunks, refs)
    id_map = store.ref_id_map(version_id)
    payload = [
        {
            "ref_id": id_map[r["ref_index"]],
            "sentence": r["sentence"],
            "chunk_id": r["chunk_id"],
            "page": r["page"],
            "bbox": r["bbox"],
        }
        for r in rows
        if r["ref_index"] in id_map and r["sentence"]
    ]
    written = store.replace_citations(version_id, payload)
    return {"citations": written, **stats}


def rebuild_references(
    project_id: str,
    *,
    version_ids: Sequence[str] | None = None,
    force: bool = False,
    max_pages: int = 60,
    dry_run: bool = False,
    min_score: float = 0.4,
) -> dict[str, Any]:
    """
    force=False 时只处理"明显有问题"的版本：没有条目、或条目字段完整度低。
    判断标准放在这里而不是每一步都做，是为了让这个接口可以被反复调用而不
    把已经抽好的数据冲掉。
    """
    from app.services.storage import StorageService

    papers = store.library_papers(project_id)
    targets = [
        p for p in papers
        if p.get("version_id") and (version_ids is None or p["version_id"] in version_ids)
    ]

    storage = StorageService()
    results: list[dict[str, Any]] = []
    for paper in targets:
        version_id = paper["version_id"]
        existing = store.refs_of(version_id)
        before_q = refparse.quality(
            [
                refparse.RefRecord(
                    index=int(r["ref_index"]),
                    raw=r.get("raw_text") or "",
                    authors=r.get("authors") or [],
                    year=r.get("parsed_year"),
                    title=r.get("parsed_title") or "",
                )
                for r in existing
            ]
        )
        if existing and not force and before_q["score"] >= 0.6:
            results.append(
                {
                    "paper": paper.get("title"),
                    "version_id": version_id,
                    "skipped": True,
                    "reason": "现有条目质量已达标",
                    "before": before_q,
                }
            )
            continue

        key = store.version_pdf_key(version_id)
        if not key:
            results.append(
                {"paper": paper.get("title"), "version_id": version_id,
                 "skipped": True, "reason": "没有 PDF 存储键"}
            )
            continue

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                tmp_path = fh.name
            size = storage.download_to_file(key, tmp_path)
            records = refparse.extract(tmp_path, max_pages=max_pages)
            after_q = refparse.quality(records)

            if len(records) < 3 or after_q["score"] < min_score:
                results.append(
                    {
                        "paper": paper.get("title"),
                        "version_id": version_id,
                        "skipped": True,
                        "reason": f"重抽结果不可用（{len(records)} 条，质量 {after_q['score']}）",
                        "before": before_q,
                        "after": after_q,
                    }
                )
                continue

            if dry_run:
                results.append(
                    {
                        "paper": paper.get("title"),
                        "version_id": version_id,
                        "dry_run": True,
                        "bytes": size,
                        "before": before_q,
                        "after": after_q,
                        "sample": [r.to_dict() for r in records[:3]],
                    }
                )
                continue

            store.delete_refs_and_citations(version_id)
            written = store.write_refs(version_id, [r.to_dict() for r in records])
            cite = _rebuild_citations(version_id)
            results.append(
                {
                    "paper": paper.get("title"),
                    "version_id": version_id,
                    "refs": written,
                    "bytes": size,
                    "before": before_q,
                    "after": after_q,
                    "citations": cite,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单篇失败不该拖垮整批回填
            results.append(
                {
                    "paper": paper.get("title"),
                    "version_id": version_id,
                    "skipped": True,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    return {
        "project_id": project_id,
        "processed": len([r for r in results if not r.get("skipped")]),
        "skipped": len([r for r in results if r.get("skipped")]),
        "results": results,
    }
