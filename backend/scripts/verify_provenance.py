#!/usr/bin/env python3
"""
课题级数据模型自检。

验证目标（对应需求的验收标准）：
    新建一个 Project，同一篇论文的两个版本（v1 / v7），
    能从任意一条引用反查到「哪篇论文的哪个版本、第几页、哪一句」。

默认用合成数据跑，不依赖 LLM 与真实 PDF，可在 CI 里秒级复现。
加 --live 则走真实上传接口（需要一台跑着的 api）。

用法：
    python scripts/verify_provenance.py
    python scripts/verify_provenance.py --keep     # 保留验证数据，便于手工查看
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db
from app.repositories import (
    ChunkRepository,
    CitationRepository,
    PaperRepository,
    PaperVersionRepository,
    ProjectRepository,
    ReferenceRepository,
)

PASS, FAIL = "✅ PASS", "❌ FAIL"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL}  {label}" + (f"  → {detail}" if detail else ""))
    return ok


def run() -> bool:
    print("=" * 70)
    print("课题级数据模型自检")
    print("=" * 70)

    init_db()
    ok = True

    # ---------- 1. 建课题 ----------
    print("\n[1] 新建课题")
    project = ProjectRepository.create(
        name="自检课题 · Attention 溯源",
        research_question="Self-Attention 的复杂度与归纳偏置如何影响后续工作？",
        created_by="verify_script",
    )
    ok &= _check("Project 已创建", bool(project.id), f"{project.id} / {project.name}")

    # ---------- 2. 同一篇论文，两个版本 ----------
    print("\n[2] 同一篇论文的 v1 与 v7")
    paper = PaperRepository.upsert_paper(
        project_id=project.id,
        arxiv_id="1706.03762",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
        venue="NeurIPS",
    )
    v1, v1_new = PaperVersionRepository.create(
        paper_id=paper.id, version_label="v1", file_hash="sha256:v1:aaaa", pdf_s3_key="1706.03762/v1/paper.pdf"
    )
    v7, v7_new = PaperVersionRepository.create(
        paper_id=paper.id, version_label="v7", file_hash="sha256:v7:bbbb", pdf_s3_key="1706.03762/v7/paper.pdf"
    )
    ok &= _check("Paper 归属课题", paper.project_id == project.id, paper.project_id)
    ok &= _check("v1 与 v7 属于同一 paper", v1.paper_id == v7.paper_id == paper.id, paper.id)
    ok &= _check("两个版本各自独立", v1.id != v7.id, f"{v1.id} / {v7.id}")
    ok &= _check(
        "新版本成为 current，旧版本保留",
        PaperVersionRepository.get_current(paper.id).id == v7.id
        and len(PaperVersionRepository.list_by_paper(paper.id)) == 2,
        "versions=2",
    )

    # ---------- 3. 两个版本各自的 chunk ----------
    print("\n[3] 版本级 chunk（同一段落内容不同，验证不串版本）")
    v1_chunks = [
        {
            "text": "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.",
            "page": 2,
            "bbox": [72.0, 180.5, 523.1, 214.9],
            "section": "1 Introduction",
            "kind": "paragraph",
        },
    ]
    v7_chunks = [
        {
            "text": "We propose the Transformer, a model architecture eschewing recurrence and instead relying entirely on an attention mechanism [1].",
            "page": 2,
            "bbox": [72.0, 176.2, 523.1, 210.4],
            "section": "1 Introduction",
            "kind": "paragraph",
        },
        {
            "text": "Table 1: Maximum path lengths, per-layer complexity and minimum number of sequential operations.",
            "page": 4,
            "bbox": [110.0, 300.0, 500.0, 360.0],
            "section": "4 Why Self-Attention",
            "kind": "table",
            "index": 1,
        },
    ]
    n1 = ChunkRepository.bulk_create(v1.id, v1_chunks)
    n7 = ChunkRepository.bulk_create(v7.id, v7_chunks)
    ok &= _check("v1 chunk 写入", n1 == 1, f"{n1} 条")
    ok &= _check("v7 chunk 写入", n7 == 2, f"{n7} 条")
    ok &= _check(
        "chunk 不跨版本污染",
        ChunkRepository.count_by_version(v1.id) == 1
        and ChunkRepository.count_by_version(v7.id) == 2,
        f"v1={ChunkRepository.count_by_version(v1.id)} v7={ChunkRepository.count_by_version(v7.id)}",
    )

    # ---------- 4. 参考文献 ----------
    print("\n[4] 参考文献条目 + 匹配")
    refs = ReferenceRepository.bulk_create(
        v7.id,
        [
            {
                "title": "Long Short-Term Memory",
                "authors": ["Sepp Hochreiter", "Jürgen Schmidhuber"],
                "year": 1997,
                "raw": "Hochreiter, S. and Schmidhuber, J. Long Short-Term Memory. Neural Computation, 1997.",
            },
            {
                "title": "Neural Machine Translation by Jointly Learning to Align and Translate",
                "authors": ["Dzmitry Bahdanau"],
                "year": 2015,
                "raw": "Bahdanau, D. et al. Neural Machine Translation by Jointly Learning to Align and Translate. ICLR 2015.",
            },
        ],
    )
    ok &= _check("参考文献写入", refs == 2, f"{refs} 条")
    ref1 = ReferenceRepository.get_by_index(v7.id, 1)
    ok &= _check("ref_index 与正文 [1] 对得上", ref1 is not None, f"ref_index={ref1.ref_index}")

    # ---------- 5. 引用位置 ----------
    print("\n[5] 正文引用位置（哪一页、哪个框、哪一句）")
    target_chunk = ChunkRepository.list_by_version(v7.id)[0]
    citation = CitationRepository.create(
        ref_id=ref1.id,
        sentence="We propose the Transformer, a model architecture eschewing recurrence and instead relying entirely on an attention mechanism [1].",
        chunk_id=target_chunk.id,
        page=target_chunk.page,
        bbox=target_chunk.bbox,
    )
    ok &= _check("Citation 写入", bool(citation.id), citation.id)

    # ---------- 6. 反查整条链 ----------
    print("\n[6] 从一条引用反查：论文 / 版本 / 页码 / 句子")
    prov = CitationRepository.provenance(citation.id)
    if not prov:
        ok &= _check("反查命中", False, "v_citation_provenance 返回空")
        return ok

    print("\n  ┌─ 反查结果 ─────────────────────────────────────────────")
    print(f"  │ 课题      : {prov['project_name']}  ({prov['project_id']})")
    print(f"  │ 论文      : {prov['paper_title']}  (arXiv:{prov['paper_arxiv_id']})")
    print(f"  │ 版本      : {prov['version_label']}   入库于 {prov['ingested_at']}")
    print(f"  │ 页码      : p.{prov['page']}")
    print(f"  │ 位置框    : {prov['bbox']}")
    print(f"  │ 章节      : {prov['section_path']}")
    print(f"  │ 参考文献  : [{prov['ref_index']}] {prov['ref_raw_text'][:56]}...")
    print(f"  │ 所在句子  : {prov['sentence'][:66]}...")
    print("  └────────────────────────────────────────────────────────")

    ok &= _check("反查到论文", prov["paper_title"] == "Attention Is All You Need")
    ok &= _check("反查到版本", prov["version_label"] == "v7", prov["version_label"])
    ok &= _check("反查到页码", prov["page"] == 2, f"p.{prov['page']}")
    ok &= _check("反查到句子", "[1]" in prov["sentence"] and "Transformer" in prov["sentence"])
    ok &= _check("反查到课题", prov["project_id"] == project.id)

    # ---------- 7. 反向检查：v1 不应出现这条引用 ----------
    print("\n[7] 版本隔离：v1 查不到 v7 的引用")
    v1_hits = CitationRepository.provenance_by_sentence(v1.id, "Transformer")
    ok &= _check("v1 无该引用", len(v1_hits) == 0, f"{len(v1_hits)} 条")

    # ---------- 8. 约束检查 ----------
    print("\n[8] 约束：论文不得脱离课题裸存")
    from app.db import reader

    with reader() as c:
        try:
            c.execute(
                """INSERT INTO papers (id, project_id, arxiv_id, title, created_at, updated_at)
                   VALUES ('pap_illegal', 'proj_not_exist', 'x', 'x', 'now', 'now')"""
            )
            orphan_blocked = False
        except Exception:
            orphan_blocked = True
    ok &= _check("外键拦截无课题论文", orphan_blocked)

    print("\n" + "=" * 70)
    print(f"总结论：{PASS if ok else FAIL}")
    print("=" * 70)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留验证数据（默认也保留，脚本本身不清理）")
    parser.parse_args()
    return 0 if run() else 1


if __name__ == "__main__":
    sys.exit(main())
