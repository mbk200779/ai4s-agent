"""
精排 A/B 评测：同一批问题，同一份候选，只改"排不排、用哪个模型"。

为什么必须固定候选集再比：
精排的作用是把**已经在候选池里的**正确片段提到前面。
如果每次重新召回，差异就同时包含"召回变了"和"排序变了"，
最后只能说"整体好了一点"，没法回答"精排到底值不值这 500ms"。

所以流程是：
  1. 用 rerank=False / numeric=False 跑一遍，拿到融合 top N=20（每个问题固定下来）；
  2. 四种配置各排一次，只在"排序 + 截断到 5"上不同：
       fusion   融合原序（= 精排前）
       numeric  融合 + 数值规则召回，仍不精排
       rerank   融合候选 → cross-encoder → top5（不开规则召回）
       full     融合 + 规则召回 → cross-encoder → top5
  3. 指标：Recall@5 / MRR@5 / 论文覆盖@5 / gold 是否在 20 候选里（天花板）。

"gold_in_20" 这一列是给结论兜底的：如果它已经很低，说明问题在召回不在排序，
精排再怎么调也没用——这条能防止把召回的问题算到精排头上。

用法：
    python -m scripts.eval_rerank --dataset both --k 5 --n 20
    python -m scripts.eval_rerank --models Xenova/ms-marco-MiniLM-L-6-v2,jinaai/jina-reranker-v1-turbo-en
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from app.services.rag import RAGRetriever  # noqa: E402
from app.services.rag.rerank import Reranker, clear_cache  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402

DATASETS = {
    "routing": Path("/app/eval/routing_queries.json"),
    "rerank": Path("/app/eval/rerank_queries.json"),
}
DEFAULT_MODELS = ["Xenova/ms-marco-MiniLM-L-6-v2", "jinaai/jina-reranker-v1-turbo-en"]


def load_dataset(name: str) -> dict:
    with open(DATASETS[name], "r", encoding="utf-8") as f:
        return json.load(f)


def is_relevant(result, item: dict) -> bool:
    """与 eval_routing.py 保持同一套判定，避免两份评测给出口径不一的相关性。"""
    for g in item.get("gold") or []:
        if result.arxiv_id == g["arxiv_id"] and int(result.page) == int(g["page"]):
            return True
    if result.arxiv_id in (item.get("gold_papers") or []):
        return True
    return False


def first_relevant_rank(results, item: dict) -> int:
    for idx, r in enumerate(results, 1):
        if is_relevant(r, item):
            return idx
    return 0


def judge(results, item: dict) -> dict:
    gold = item.get("gold") or []
    gold_papers = item.get("gold_papers") or []
    if not gold and not gold_papers:
        return {"hit": len(results) == 0, "mrr": 1.0 if not results else 0.0, "coverage": None}
    rank = first_relevant_rank(results, item)
    papers = {r.arxiv_id for r in results}
    coverage = (
        round(len(set(gold_papers) & papers) / len(gold_papers), 3) if gold_papers else None
    )
    return {"hit": bool(rank), "mrr": round(1.0 / rank, 3) if rank else 0.0, "coverage": coverage}


def collect_candidates(retriever: RAGRetriever, items: list[dict], n: int) -> list[dict]:
    """
    固定候选集：关掉精排与规则召回，取融合 top N。

    注意这里必须用 **同一段 search_text** 作为精排问句——
    真实链路里精排读的就是它，评测里换成原句会高估或低估精排。
    """
    out = []
    for item in items:
        text = item.get("search_text") or item["question"]
        route = retriever.route(
            item["question"], search_text=text, top_k=n, rerank=False, numeric=False
        )
        out.append({
            "item": item,
            "text": text,
            "type": route.trace["type"],
            "candidates": route.results[:n],
            "cursor": route.results,
        })
    return out


def run_config(name: str, cands: list[dict], k: int, reranker: Reranker | None, retriever):
    """一种配置跑完全部问题，返回逐条结果与汇总。"""
    rows = []
    for c in cands:
        item = c["item"]
        started = time.perf_counter()
        if reranker is None:
            results = c["candidates"][:k]
            degraded = False
        else:
            outcome = reranker.apply(c["text"], c["candidates"], k)
            results = outcome.results
            degraded = outcome.degraded
        verdict = judge(results, item)
        rows.append({
            "id": item["id"],
            "type": c["type"],
            **verdict,
            "degraded": degraded,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        })
    n = len(rows)
    cov = [r["coverage"] for r in rows if r["coverage"] is not None]
    return {
        "config": name,
        "n": n,
        "recall": round(sum(1 for r in rows if r["hit"]) / n, 4),
        "mrr": round(sum(r["mrr"] for r in rows) / n, 4),
        "coverage": round(sum(cov) / len(cov), 4) if cov else None,
        "latency_ms": round(sum(r["latency_ms"] for r in rows) / n, 1),
        "degraded": sum(1 for r in rows if r["degraded"]),
        "rows": rows,
    }


def summarize(cands: list[dict], k: int) -> dict:
    """候选池天花板：gold 在 N 候选里 = 精排最多能做到多少。"""
    total = len(cands)
    in_pool = sum(1 for c in cands if first_relevant_rank(c["candidates"], c["item"]))
    return {
        "n": total,
        "gold_in_pool": round(in_pool / total, 4) if total else 0.0,
        "pool": len(cands[0]["candidates"]) if cands else 0,
    }


def render_md(dataset: str, ceiling: dict, results: list[dict], k: int) -> str:
    lines = [
        f"# 精排 A/B 评测 — {dataset} 数据集",
        "",
        f"- 候选池：融合 top {ceiling['pool']}；gold 在候选池里的比例 **{ceiling['gold_in_pool']:.1%}**"
        f"（这是精排的天花板，超过它说明问题在召回不在排序）",
        f"- 指标按 top {k} 计算；同一批问题、同一份候选，唯一变量是「排不排、用哪个模型」",
        "",
        "| 配置 | Recall@%d | MRR@%d | 论文覆盖@%d | 平均耗时 | 降级条数 |" % (k, k, k),
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        cov = f"{r['coverage']:.3f}" if r["coverage"] is not None else "—"
        lines.append(
            f"| {r['config']} | {r['recall']:.1%} | {r['mrr']:.3f} | {cov} | "
            f"{r['latency_ms']} ms | {r['degraded']}/{r['n']} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_rows(dataset: str, results: list[dict], cands: list[dict]) -> str:
    """逐条对照：哪几条被精排救回来、哪几条被精排推下去。"""
    ids = [c["item"]["id"] for c in cands]
    head = "| ID | 类型 | 候选池 | " + " | ".join(r["config"] for r in results) + " |"
    sep = "|---|---|---|" + "---|" * len(results)
    lines = [f"### {dataset} 逐条对照（✓ 命中 / ✗ 未命中，括号内为 MRR）", "", head, sep]
    ok = {}
    for r in results:
        ok[r["config"]] = {row["id"]: row for row in r["rows"]}
    for c in cands:
        i = c["item"]["id"]
        pool = "✓" if first_relevant_rank(c["candidates"], c["item"]) else "✗"
        cells = []
        for r in results:
            row = ok[r["config"]][i]
            cells.append(f"{'✓' if row['hit'] else '✗'} ({row['mrr']:.2f})")
        lines.append(f"| {i} | {c['type']} | {pool} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["routing", "rerank", "both"], default="both")
    ap.add_argument("--k", type=int, default=5, help="评估截断（Recall@k）")
    ap.add_argument("--n", type=int, default=20, help="候选池大小")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--md-out", default=None)
    args = ap.parse_args()

    names = ["routing", "rerank"] if args.dataset == "both" else [args.dataset]
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    retrieval = RetrievalService()
    retriever = RAGRetriever(retrieval)

    payload = {"models": models, "k": args.k, "n": args.n, "datasets": {}}
    md_parts = ["# 精排 A/B 评测", ""]
    md_parts.append(
        f"- 候选池 = 融合 top {args.n}；评估截断 k = {args.k}；"
        f"模型：{', '.join(models)}"
    )
    md_parts.append("- 基线 `fusion` = 分流后的融合原序（步骤 03 的产出），不含精排、不含规则召回")
    md_parts.append("")

    for name in names:
        dataset = load_dataset(name)
        items = dataset.get("queries", [])
        print(f"==> {name}: {len(items)} 条（{dataset.get('version')}）", flush=True)

        cands = collect_candidates(retriever, items, args.n)
        ceiling = summarize(cands, args.k)

        results = [run_config("fusion", cands, args.k, None, retriever)]

        # 规则召回单独作为一档：它和精排是两个独立改动，收益必须分开看
        rule_rows = []
        for c in cands:
            route = retriever.route(
                c["item"]["question"], search_text=c["text"], top_k=args.k,
                rerank=False, numeric=True,
            )
            verdict = judge(route.results, c["item"])
            rule_rows.append({"id": c["item"]["id"], "type": c["type"], **verdict,
                              "degraded": False, "latency_ms": route.trace["total_ms"]})
        rule_n = len(rule_rows)
        rule_cov = [r["coverage"] for r in rule_rows if r["coverage"] is not None]
        results.append({
            "config": "numeric",
            "n": rule_n,
            "recall": round(sum(1 for r in rule_rows if r["hit"]) / rule_n, 4),
            "mrr": round(sum(r["mrr"] for r in rule_rows) / rule_n, 4),
            "coverage": round(sum(rule_cov) / len(rule_cov), 4) if rule_cov else None,
            "latency_ms": round(sum(r["latency_ms"] for r in rule_rows) / rule_n, 1),
            "degraded": 0,
            "rows": rule_rows,
        })

        for m in models:
            clear_cache()
            ranker = Reranker(enabled=True, model_name=m)
            label = "rerank:" + m.split("/")[-1]
            results.append(run_config(label, cands, args.k, ranker, retriever))
            # full = 规则召回注入 → 精排（走完整链路，含缓存）
            clear_cache()
            full_rows = []
            for c in cands:
                route = retriever.route(
                    c["item"]["question"], search_text=c["text"], top_k=args.k,
                    rerank=True, numeric=True,
                )
                verdict = judge(route.results, c["item"])
                rr = route.trace["rerank"]
                full_rows.append({
                    "id": c["item"]["id"], "type": c["type"], **verdict,
                    "degraded": rr["degraded"],
                    "latency_ms": rr["latency_ms"],
                })
            fn = len(full_rows)
            fcov = [r["coverage"] for r in full_rows if r["coverage"] is not None]
            results.append({
                "config": "full:" + m.split("/")[-1],
                "n": fn,
                "recall": round(sum(1 for r in full_rows if r["hit"]) / fn, 4),
                "mrr": round(sum(r["mrr"] for r in full_rows) / fn, 4),
                "coverage": round(sum(fcov) / len(fcov), 4) if fcov else None,
                "latency_ms": round(sum(r["latency_ms"] for r in full_rows) / fn, 1),
                "degraded": sum(1 for r in full_rows if r["degraded"]),
                "rows": full_rows,
            })

        payload["datasets"][name] = {"ceiling": ceiling, "results": results}
        md_parts.append(render_md(f"{name}（{dataset.get('version')}）", ceiling, results, args.k))
        md_parts.append(render_rows(name, results, cands))

        for r in results:
            print(f"   {r['config']:38s} R@{args.k}={r['recall']:.1%} "
                  f"MRR={r['mrr']:.3f} cov={r['coverage']} {r['latency_ms']}ms deg={r['degraded']}",
                  flush=True)

    md = "\n".join(md_parts)
    print(md)

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"==> JSON 写入 {args.json_out}", file=sys.stderr)
    if args.md_out:
        Path(args.md_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md_out).write_text(md, encoding="utf-8")
        print(f"==> 报告写入 {args.md_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
