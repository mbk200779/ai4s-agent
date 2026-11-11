"""临时实验：NUMERIC_RECALL_LIMIT 取不同值时，规则注入会不会把原来的 gold 挤出前 5。

不参与交付，只用来给 docs/2.9 的「注入条数」一节提供依据。
"""
import functools
import importlib
import sys

sys.path.insert(0, "/app")

from app.services.rag import numeric_rules  # noqa: E402
from scripts.eval_rerank import judge, load_dataset  # noqa: E402

R = importlib.import_module("app.services.rag.retrieve")

orig = numeric_rules.recall
for limit in (8, 5, 3):
    numeric_rules.recall = functools.partial(orig, limit=limit)
    retr = R.RAGRetriever()
    hits = 0
    rows = []
    for item in load_dataset("rerank")["queries"]:
        out = retr.route(
            item["question"], search_text=item.get("search_text"), top_k=5,
            rerank=False, numeric=True,
        )
        v = judge(out.results, item)
        hits += v["hit"]
        rows.append((item["id"], "Y" if v["hit"] else "N", round(v["mrr"], 2)))
    n = len(rows)
    print(f"limit={limit}: recall@5={hits}/{n} = {hits / n:.1%}")
    print("   " + "  ".join(f"{i}{h}({m})" for i, h, m in rows))
