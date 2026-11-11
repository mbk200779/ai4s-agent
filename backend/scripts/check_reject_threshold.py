"""拒答口径对照：同一条问句在分流前后各自的最佳原始分与阈值。

专门查一件事：分流后顶层的 raw_score 与阈值不在同一个量纲上，
会让"原本能答"的问题被误拒。这个脚本把两侧并排打出来，
谁的阈值更严一目了然。
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.retrieval_service import RetrievalService
from app.services.rag import RAGRetriever
from app.services.rag.taxonomy import RRF_MIN_SCORE

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval", "routing_queries.json")
with open(path, encoding="utf-8") as f:
    data = json.load(f)
items = data["queries"] if isinstance(data, dict) else data

retrieval = RetrievalService()
rag = RAGRetriever(retrieval)

print(f"{'id':<16}{'type':<9}{'legacy':>10}{'thr':>7}{'routed_raw':>12}{'thr':>8}  判定")
worse = 0
for it in items:
    q = it["question"]
    name = it.get("id", "?")
    legacy = retrieval.search(q, top_k=8)
    fusion = legacy["fusion"]
    l_top = max((r.score for r in fusion), default=0.0)
    l_pass = l_top >= RRF_MIN_SCORE

    route = rag.route(q, top_k=8)
    trace = route.trace
    r_raw = trace["recall"].get("top_raw_score")
    r_thr = route.reject_threshold
    r_pass = r_raw is None or r_thr is None or r_raw >= r_thr

    mark = "OK"
    if l_pass and not r_pass:
        mark = "!! 分流后误拒"
        worse += 1
    elif not l_pass and r_pass:
        mark = "分流后转可答"

    print(
        f"{name:<16}{trace['type']:<9}{l_top:>10.4f}{RRF_MIN_SCORE:>7.3f}"
        f"{(r_raw if r_raw is not None else 0):>12.4f}{(r_thr if r_thr is not None else 0):>8.4f}  {mark}"
    )

print(f"\n分流后新增误拒：{worse} 条")
