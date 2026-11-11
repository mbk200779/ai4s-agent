#!/usr/bin/env python3
"""
旧数据迁移：把 Qdrant 里的「隐式论文」搬进课题级关系模型。

背景：改造前，论文靠 `arxiv_id::version` 这个字符串隐式存在，
元数据在 paper_meta collection，正文在 chunk collection，没有任何一张表
表达「课题 → 论文 → 版本 → 片段 → 引用」的关系。这个脚本负责补齐这层结构。

用法：
    # 默认 dry-run：只统计影响面与异常行，不写库
    python scripts/migrate_to_projects.py

    # 真正写入（幂等，重复跑不会产生重复行）
    python scripts/migrate_to_projects.py --apply

    # 指定课题名、把报告另存一份
    python scripts/migrate_to_projects.py --project "我的课题" --report /tmp/migrate.txt

已知偏差：
- file_hash 本应是 PDF 字节的 sha256，但旧数据只存了正文 hash（body_hash）。
  迁移时用 body_hash 顶替，语义变成「内容指纹」。这不影响去重，
  但如果后续要按文件字节去重，需要用原 PDF 重算。
- citations 无法迁移：旧解析管线没有抽取过引用位置，表会是空的。
  必须对论文重新解析（或重跑 merge 任务）才会产生 citation 行。
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdrant_client import QdrantClient

from app.core.config import settings
from app.db import init_db, new_id, transaction, utc_now
from app.repositories import (
    ChunkRepository,
    CitationRepository,
    PaperRepository,
    PaperVersionRepository,
    ProjectRepository,
    ReferenceRepository,
)


def scroll_all(client: QdrantClient, collection: str, batch: int = 512):
    """分页读取整个 collection；chunk collection 可能有十万级点，不能一次全拉。"""
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            yield p
        if offset is None:
            break


def collect_plan(client: QdrantClient) -> dict:
    """
    扫描两个 collection，组装成迁移计划。
    dry-run 与 apply 共用同一份计划，保证「报告里说的」和「实际写的」是同一批数据。
    """
    anomalies: list[dict] = []
    papers: dict[tuple[str, str], dict] = {}
    chunks_by_key: dict[tuple[str, str], list] = defaultdict(list)

    # ---------- 1. 论文元数据 ----------
    for point in scroll_all(client, settings.qdrant_paper_meta_collection):
        payload = point.payload or {}
        arxiv_id = (payload.get("arxiv_id") or "").strip()
        version = (payload.get("version") or "v1").strip()
        if not arxiv_id:
            anomalies.append({"type": "meta_missing_arxiv_id", "detail": f"point={point.id}"})
            continue
        title = (payload.get("title") or "").strip()
        if not title:
            anomalies.append(
                {"type": "meta_missing_title", "detail": f"{arxiv_id}::{version}"}
            )
            title = arxiv_id

        key = (arxiv_id, version)
        if key in papers:
            anomalies.append(
                {"type": "duplicate_meta_key", "detail": f"{arxiv_id}::{version}"}
            )
            continue

        papers[key] = {
            "arxiv_id": arxiv_id,
            "version": version,
            "title": title,
            "authors": payload.get("authors") or [],
            "abstract": payload.get("abstract") or "",
            "body_hash": payload.get("body_hash") or "",
            "pdf_s3_key": payload.get("pdf_s3_key") or "",
            "refs": payload.get("structured_references") or [],
        }

    # ---------- 2. 正文片段 ----------
    for point in scroll_all(client, settings.qdrant_collection):
        payload = point.payload or {}
        arxiv_id = (payload.get("arxiv_id") or "").strip()
        version = (payload.get("version") or "v1").strip()
        text = (payload.get("text") or "").strip()
        if not arxiv_id:
            anomalies.append({"type": "chunk_missing_arxiv_id", "detail": f"point={point.id}"})
            continue
        if not text:
            anomalies.append(
                {"type": "chunk_empty_text", "detail": f"{arxiv_id}::{version} point={point.id}"}
            )
            continue
        key = (arxiv_id, version)
        if key not in papers:
            # 有正文没元数据：向量库里能检索到，但不知道属于哪篇论文的哪个版本
            anomalies.append(
                {"type": "orphan_chunk", "detail": f"{arxiv_id}::{version} point={point.id}"}
            )
            continue
        chunks_by_key[key].append(
            {
                "text": text,
                "page": payload.get("page"),
                "bbox": payload.get("bbox"),
                "section": payload.get("section") or "",
                "kind": payload.get("kind", "paragraph"),
                "latex": payload.get("latex"),
                "qdrant_point_id": str(point.id),
            }
        )

    # ---------- 3. 参考文献条目体检 ----------
    for key, paper in papers.items():
        for i, ref in enumerate(paper["refs"], start=1):
            if not (ref.get("title") or ref.get("raw") or ref.get("text")):
                anomalies.append(
                    {"type": "ref_missing_content", "detail": f"{key[0]}::{key[1]} ref#{i}"}
                )

    return {"papers": papers, "chunks": chunks_by_key, "anomalies": anomalies}


def summarize(plan: dict) -> dict:
    papers = plan["papers"]
    chunks = plan["chunks"]
    total_chunks = sum(len(v) for v in chunks.values())
    total_refs = sum(len(p["refs"]) for p in papers.values())
    empty_chunk_papers = [k for k in papers if not chunks.get(k)]

    # 预估能从正文里抽出多少条引用位置（dry-run 也要给出影响面）
    estimated_citations = 0
    try:
        from app.services.citation_extractor import extract_from_chunks

        for chs in chunks.values():
            estimated_citations += sum(
                1 for i in extract_from_chunks(chs) if i.get("ref_index")
            )
    except Exception:
        estimated_citations = 0

    return {
        "papers": len(papers),
        "versions": len(papers),
        "chunks": total_chunks,
        "refs": total_refs,
        "citations": estimated_citations,
        "anomalies": len(plan["anomalies"]),
        "papers_without_chunks": empty_chunk_papers,
    }


def print_report(plan: dict, mode: str, project_label: str, applied: dict | None = None) -> str:
    s = summarize(plan)
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(f"课题化迁移报告（{mode}）")
    lines.append("=" * 68)
    lines.append(f"目标课题           : {project_label}")
    lines.append(f"扫描论文版本       : {s['versions']}")
    lines.append(f"计划写入 chunk     : {s['chunks']}")
    lines.append(f"计划写入参考文献   : {s['refs']}")
    lines.append(f"可抽取引用位置     : {s['citations']}  （由 chunk 文本现场抽取）")
    lines.append(f"异常行             : {s['anomalies']}")

    if s["papers_without_chunks"]:
        lines.append("")
        lines.append("⚠ 有元数据但没有任何 chunk 的版本（检索不到，建议重新解析）：")
        for aid, ver in s["papers_without_chunks"][:10]:
            lines.append(f"    - {aid}::{ver}")

    if applied:
        lines.append("")
        lines.append("实际写入：")
        lines.append(
            f"    论文 {applied['papers']} 新建 / 版本 {applied['versions']} 新建 / "
            f"复用 {applied['reused']}"
        )
        lines.append(
            f"    chunk {applied['chunks']} / 参考文献 {applied['refs']} / 引用位置 {applied['citations']}"
        )

    if plan["anomalies"]:
        lines.append("")
        lines.append("异常明细（最多 30 条）：")
        by_type: dict[str, int] = defaultdict(int)
        for a in plan["anomalies"]:
            by_type[a["type"]] += 1
        for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"    [{t}] × {n}")
        lines.append("")
        seen = set()
        shown = 0
        for a in plan["anomalies"]:
            if a["type"] in seen and by_type[a["type"]] > 3:
                continue
            lines.append(f"    - {a['type']}: {a['detail']}")
            seen.add(a["type"])
            shown += 1
            if shown >= 30:
                lines.append("    ...（剩余从略）")
                break
    lines.append("=" * 68)
    return "\n".join(lines)


def apply_plan(plan: dict, project_name: str, derive_citations: bool = True) -> dict:
    """把计划写进 SQLite。整批放在一个事务里，中途失败不会留下半篇文章。"""
    stats = {"papers": 0, "versions": 0, "reused": 0, "chunks": 0, "refs": 0, "citations": 0}

    init_db()
    with transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM projects WHERE name = ? LIMIT 1", (project_name,)
        ).fetchone()
        if existing:
            project_id = existing["id"]
        else:
            pid = new_id("proj")
            now = utc_now()
            conn.execute(
                """INSERT INTO projects (id, name, research_question, status, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', 'migration', ?, ?)""",
                (pid, project_name, "由旧数据迁移脚本创建", now, now),
            )
            project_id = pid

        for (arxiv_id, version), meta in plan["papers"].items():
            paper = PaperRepository.upsert_paper(
                project_id=project_id,
                arxiv_id=arxiv_id,
                title=meta["title"],
                authors=meta["authors"],
                abstract=meta["abstract"] or None,
                conn=conn,
            )
            stats["papers"] += 1

            # 迁移时拿不到 PDF 字节，用 body_hash 顶替 file_hash（见文件头「已知偏差」）
            version_row, created = PaperVersionRepository.create(
                paper_id=paper.id,
                version_label=version,
                file_hash=meta["body_hash"] or f"{arxiv_id}::{version}",
                content_hash=meta["body_hash"] or None,
                pdf_s3_key=meta["pdf_s3_key"] or None,
                status="indexed",
                conn=conn,
            )
            if created:
                stats["versions"] += 1
            else:
                stats["reused"] += 1
                continue  # 已迁移过，不重复写 chunk / refs

            stats["chunks"] += ChunkRepository.bulk_create(
                version_row.id, plan["chunks"].get((arxiv_id, version), []), conn=conn
            )
            stats["refs"] += ReferenceRepository.bulk_create(
                version_row.id, meta["refs"], conn=conn
            )
            # 参考文献回连课题内已入库论文
            ReferenceRepository.match_to_project(version_row.id, project_id, conn=conn)

            # 旧数据没有 citation 行，但正文里的 [12] 标记还在 chunk 文本里。
            # 现场抽一遍，就能立刻支持「从引用反查页码与句子」，
            # 不必等整篇 PDF 重新解析（那条路要跑 LLM，代价高得多）。
            if derive_citations:
                from app.db import load_json
                from app.services.citation_extractor import extract_from_chunks

                # 读也走同一个 conn：外层事务还没提交，另开连接读不到刚写的行
                stored = conn.execute(
                    """SELECT id, text, page, bbox, chunk_type FROM chunks
                        WHERE version_id = ? ORDER BY chunk_index""",
                    (version_row.id,),
                ).fetchall()
                ref_map = {
                    r["ref_index"]: r["id"]
                    for r in conn.execute(
                        "SELECT id, ref_index FROM paper_refs WHERE version_id = ?",
                        (version_row.id,),
                    )
                }
                items = []
                for item in extract_from_chunks(
                    [
                        {
                            "id": ck["id"],
                            "text": ck["text"],
                            "page": ck["page"],
                            # bbox 在库里是 JSON 字符串，这里要还原成数组再往下传，
                            # 否则 citations 写入时会被二次编码成字符串套字符串
                            "bbox": load_json(ck["bbox"], None),
                            "chunk_type": ck["chunk_type"],
                        }
                        for ck in stored
                    ]
                ):
                    rid = ref_map.get(item["ref_index"]) if item["ref_index"] else None
                    if not rid:
                        continue
                    items.append(
                        {
                            "ref_id": rid,
                            "chunk_id": item["chunk_id"],
                            "page": item["page"],
                            "bbox": item["bbox"],
                            "sentence": item["sentence"],
                        }
                    )
                stats["citations"] += CitationRepository.bulk_create(items, conn=conn)

        stats["project_id"] = project_id
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="把 Qdrant 旧数据迁移到课题级关系模型")
    parser.add_argument("--apply", action="store_true", help="真正写入数据库（默认只 dry-run）")
    parser.add_argument(
        "--project", default=settings.default_project_name, help="迁移到哪个课题（不存在则创建）"
    )
    parser.add_argument("--report", default=None, help="把报告另存为文件")
    parser.add_argument(
        "--no-derive-citations",
        action="store_true",
        help="不从 chunk 文本补抽引用位置（默认会抽）",
    )
    args = parser.parse_args()

    client = QdrantClient(url=settings.qdrant_url)
    print(f"[1/3] 扫描 Qdrant：{settings.qdrant_paper_meta_collection} / {settings.qdrant_collection}")
    plan = collect_plan(client)
    print(f"[2/3] 组装迁移计划：{len(plan['papers'])} 个版本，{summarize(plan)['chunks']} 个 chunk")

    applied = None
    if args.apply:
        print("[3/3] 写入 SQLite ...")
        applied = apply_plan(plan, args.project, derive_citations=not args.no_derive_citations)
        mode = "APPLY · 已写入"
    else:
        print("[3/3] 未加 --apply，仅输出影响面")
        mode = "DRY-RUN · 未写入任何数据"

    report = print_report(plan, mode, args.project, applied)
    print(report)

    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"报告已保存：{args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
