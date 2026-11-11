"""
精排权重实验：纯精排、纯融合、以及两者的混合，谁在起作用？

背景：第一版把精排结果**完全替换**融合排序，结果 Recall@5 从 62.5% 掉到 43.8%。
原因不难猜——点查的正确答案往往是一句 "β2 = 0.98" 或一行表格，语义上并不"像答案"，
而融合分里已经编码了"参数名≈数值""实验设置段""表格"这些领域信号；
cross-encoder 是通用网页数据训出来的，它看不到这些信号，只会把"通篇在讲 Adam"的段落排前面。

所以这里不改结论去迁就模型，而是把两路分数按权重混合后再测：
    final = α · norm(rerank) + (1-α) · norm(fusion)
α=0 等于不开精排，α=1 等于第一版。中间那几档才是要选的。

另外测一个更保守的用法 `promote_only`：只允许精排把候选**提上来**补足前 k，
不允许它把融合已经排在前面的片段挤出去。适用于"融合在头部更可信"的场景。

用法：
    python -m scripts.tune_rerank_blend --dataset both --model Xenova/ms-marco-MiniLM-L-6-v2
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app.services.rag import RAGRetriever  # noqa: E402
from app.services.rag.rerank import Reranker, clear_cache  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from scripts.eval_rerank import (  # noqa: E402
    DATASETS,
    first_relevant_rank,
    is_relevant,
    judge,
    load_dataset,
)

ALPHAS = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]
# 规则召回的"证据加成"：数值/公式类问句的 gold 通常是一句 "β2 = 0.98" 或一列表格，
# cross-encoder 不认为它"像答案"，所以即使命中了也要靠这一项把它托住。
RULE_BONUS = 0.35


def norm(scores: list[float]) -> list[float]:
    lo, hi = min(scores), max(scores)
    span = (hi - lo) or 1.0
    return [(s - lo) / span for s in scores]


def evaluate_order(results, item) -> tuple[bool, float]:
    v = judge(results, item)
    return v["hit"], v["mrr"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["routing", "rerank", "both"], default="both")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--model", default="Xenova/ms-marco-MiniLM-L-6-v2")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    names = ["routing", "rerank"] if args.dataset == "both" else [args.dataset]
    retriever = RAGRetriever(RetrievalService())
    ranker = Reranker(enabled=True, model_name=args.model)
    clear_cache()

    # 先把每条问题的候选与两路分数算出来，后面所有 α 共用这一份数据
    samples = []
    for name in names:
        dataset = load_dataset(name)
        for item in dataset.get("queries", []):
            text = item.get("search_text") or item["question"]
            route = retriever.route(
                item["question"], search_text=text, top_k=args.n, rerank=False, numeric=True
            )
            cands = route.results[: args.n]
            if not cands:
                continue
            # 融合分必须在精排**之前**快照：apply 会把 r.score 改写成精排归一化分，
            # 之后读到的就不是融合分了（第一版踩过这个坑，导致每个 α 测出来都一样）。
            fusion = [c.score or 0.0 for c in cands]
            out = ranker.apply(text, cands, args.n)
            if not out.applied:
                print(f"!! {item['id']} 精排未生效：{out.degrade_reason}", file=sys.stderr)
                continue
            rr = {s["chunk_id"]: s["rerank_raw"] for s in (out.scores or [])}
            samples.append({
                "dataset": name,
                "rule_ids": {c.chunk_id for c in cands if (c.hit_source or "").startswith("rule")},
                "item": item,
                "cands": cands,
                "fusion": fusion,
                "rerank": [rr.get(c.chunk_id, -1e9) for c in cands],
            })

    print(f"==> {len(samples)} 条样本，模型 {args.model}", flush=True)

    rows = []
    for alpha in ALPHAS:
        hits = mrr = 0.0
        per_ds = {}
        for s in samples:
            fn = norm(s["fusion"])
            rn = norm(s["rerank"])
            order = sorted(
                range(len(s["cands"])),
                key=lambda i: alpha * rn[i] + (1 - alpha) * fn[i],
                reverse=True,
            )
            picked = [s["cands"][i] for i in order[: args.k]]
            hit, m = evaluate_order(picked, s["item"])
            hits += hit
            mrr += m
            d = per_ds.setdefault(s["dataset"], [0, 0.0, 0])
            d[0] += hit
            d[1] += m
            d[2] += 1
        rows.append({
            "alpha": alpha,
            "recall": round(hits / len(samples), 4),
            "mrr": round(mrr / len(samples), 4),
            "by_dataset": {
                k: {"recall": round(v[0] / v[2], 4), "mrr": round(v[1] / v[2], 4), "n": v[2]}
                for k, v in per_ds.items()
            },
        })

    # 两种"保头部"的用法。注意判定条件里**不能出现 gold**——
    # 第一版写成"前 k 里没有 gold 就换人"，那是拿答案做决策，指标必然虚高。
    def boundary(s):
        """融合前 k-1 名不动，第 k 名由精排决定：精排最高的候补比现任高就换。"""
        rn = norm(s["rerank"])
        fn = norm(s["fusion"])
        head = s["cands"][: args.k]
        head_ids = {c.chunk_id for c in head}
        rest = [
            (s["cands"][i], rn[i])
            for i in range(len(s["cands"]))
            if s["cands"][i].chunk_id not in head_ids
        ]
        rest.sort(key=lambda t: t[1], reverse=True)
        if not rest:
            return head
        incumbent_i = args.k - 1
        if rn[incumbent_i] < rest[0][1] - 1e-9:
            return head[:incumbent_i] + [rest[0][0]]
        return head

    def head2(s):
        """融合前 2 名不动，其余由精排排序。"""
        rn = norm(s["rerank"])
        head = s["cands"][:2]
        head_ids = {c.chunk_id for c in head}
        rest = [
            (s["cands"][i], rn[i])
            for i in range(len(s["cands"]))
            if s["cands"][i].chunk_id not in head_ids
        ]
        rest.sort(key=lambda t: t[1], reverse=True)
        return head + [c for c, _ in rest[: args.k - 2]]

    extra_policies = []
    for name, fn in (("boundary（只让精排决定第 k 名）", boundary), ("head2（前 2 名锁定）", head2)):
        hits = mrr = 0.0
        per_ds = {}
        for s in samples:
            picked = fn(s)
            hit, m = evaluate_order(picked, s["item"])
            hits += hit
            mrr += m
            d = per_ds.setdefault(s["dataset"], [0, 0.0, 0])
            d[0] += hit
            d[1] += m
            d[2] += 1
        extra_policies.append({
            "alpha": name,
            "recall": round(hits / len(samples), 4),
            "mrr": round(mrr / len(samples), 4),
            "by_dataset": {
                k: {"recall": round(v[0] / v[2], 4), "mrr": round(v[1] / v[2], 4), "n": v[2]}
                for k, v in per_ds.items()
            },
        })

    # 混合 + 规则证据加成：α 管语义排序，规则加成管"字面命中的硬证据"
    for bonus in (RULE_BONUS,):
        print(f"\n| 混合 α + 规则加成 {bonus} | Recall@{args.k} | MRR@{args.k} | 分数据集 Recall |")
        print("|---|---|---|---|")
        for alpha in (0.2, 0.3, 0.4, 0.5, 0.6):
            hits = mrr = 0.0
            per_ds = {}
            for s in samples:
                fn = norm(s["fusion"])
                rn = norm(s["rerank"])
                ids = [c.chunk_id for c in s["cands"]]
                score = [
                    alpha * rn[i] + (1 - alpha) * fn[i] + (bonus if ids[i] in s["rule_ids"] else 0.0)
                    for i in range(len(ids))
                ]
                order = sorted(range(len(ids)), key=lambda i: score[i], reverse=True)
                picked = [s["cands"][i] for i in order[: args.k]]
                hit, m = evaluate_order(picked, s["item"])
                hits += hit
                mrr += m
                d = per_ds.setdefault(s["dataset"], [0, 0.0, 0])
                d[0] += hit
                d[1] += m
                d[2] += 1
            extra = " / ".join(f"{k} {v[0] / v[2]:.0%}" for k, v in per_ds.items())
            print(f"| α={alpha} + 规则加成 | {hits / len(samples):.1%} | {mrr / len(samples):.3f} | {extra} |")

    print(f"\n| 配置 | Recall@{args.k} | MRR@{args.k} | 分数据集 Recall |")
    print("|---|---|---|---|")
    for r in rows:
        label = "融合原序 (α=0)" if r["alpha"] == 0 else (
            "纯精排 (α=1)" if r["alpha"] == 1.0 else f"混合 α={r['alpha']}"
        )
        bd = r["by_dataset"]
        extra = " / ".join(f"{k} {v['recall']:.0%}" for k, v in bd.items())
        print(f"| {label} | {r['recall']:.1%} | {r['mrr']:.3f} | {extra} |")
    for r in extra_policies:
        bd = r["by_dataset"]
        extra = " / ".join(f"{k} {v['recall']:.0%}" for k, v in bd.items())
        print(f"| {r['alpha']} | {r['recall']:.1%} | {r['mrr']:.3f} | {extra} |")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps({"model": args.model, "k": args.k, "rows": rows,
                        "policies": extra_policies},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
