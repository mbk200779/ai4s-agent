"""
检索层可复现性评测脚本：验证 dense + sparse 双路召回与 RRF 融合稳定性。
用法：
    python scripts/eval_retrieval.py "你的查询"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.retrieval_service import RetrievalService


def main(query: str):
    svc = RetrievalService()
    result = svc.search(query, top_k=5, rrf_k=60)

    print(f"Query: {query}\n")
    print(f"Dense hits: {len(result['dense'])}")
    print(f"Sparse hits: {len(result['sparse'])}")
    print(f"Fusion hits: {len(result['fusion'])}\n")

    print("=== Fusion Top-5 ===")
    for idx, r in enumerate(result["fusion"], 1):
        print(
            f"{idx}. [{r.kind}] {r.title} (p.{r.page}, para.{r.paragraph}, "
            f"bbox={r.bbox}, score={r.score:.4f})\n   {r.text[:160]}..."
        )

    # 运行两次验证稳定性
    result2 = svc.search(query, top_k=5, rrf_k=60)
    ids1 = [r.text for r in result["fusion"]]
    ids2 = [r.text for r in result2["fusion"]]
    print("\n复现性:", "PASS" if ids1 == ids2 else "FAIL")


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "machine learning"
    main(q)
