"""
检索分流评测：同一批问题在"分流前"与"分流后"的召回对比。

为什么要有这个脚本：
需求里有一条硬约束——"不得因为分流导致原有单篇问答效果下降"。
这句话必须能被度量，否则改完只能说"看起来还行"。所以这里做两件事：

  1. 基线不是另写一套代码，而是**同一份 `RetrievalService.search()`**（分流前唯一路径）；
  2. 分流侧走 `RAGRetriever.route()`，两者用同一个 search_text、同一个 top_k，
     唯一变量就是"有没有分流"。这样差值才能归因到分流本身。

关键设计：评测指标只看**召回**，不看答案。
因为答案是 LLM 生成的，温度、措辞、运气都会盖住检索差异；
把 LLM 摘掉，才能单独判断"分流是否让该被召回的证据进入了上下文"。

指标：
  hit@k       top_k 里是否出现 gold（论文 + 页码）片段
  MRR         第一个 gold 片段的倒数排名
  coverage    gold 论文的覆盖率（对比题专用：两篇都要召回）
  type_ok     期望类型是否判断正确（只对分流侧有意义）

用法：
    python /app/scripts/eval_routing.py                     # 默认跑全部，输出表格
    python /app/scripts/eval_routing.py --json-out /app/data/bench/routing.json
    python /app/scripts/eval_routing.py --mode baseline      # 只看分流前
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from app.services.rag import RAGRetriever  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402

DATASET = Path(os.getenv("ROUTING_DATASET", "/app/eval/routing_queries.json"))


def load_dataset() -> dict:
    with open(DATASET, "r", encoding="utf-8") as f:
        return json.load(f)


def is_relevant(result, item: dict) -> bool:
    """
    单条结果算不算"相关"。

    两种 gold 混用，是因为不同题型的正确答案形态不一样：
      - 点查/定位题有唯一的正确页 → gold 用 (论文, 页码)；
      - 对比/综述题的正确答案是"这两篇都要出现"，页码反而是次要的
        （同一事实在不同版本里可能落在相邻页）→ gold 用 gold_papers。
    只看页码会让对比题几乎必然判错，只看论文又会让点查题失去区分度。
    """
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
    """
    判定一条查询的结果。三个量：
      hit      第一条相关结果是否进入 top_k；没有任何 gold 的题（闲聊）反过来看"是否确实没召回"
      mrr      第一条相关结果的倒数排名
      coverage gold_papers 的覆盖率（对比/综述题专用）
    """
    gold = item.get("gold") or []
    gold_papers = item.get("gold_papers") or []
    papers = {r.arxiv_id for r in results}

    if not gold and not gold_papers:
        return {"hit": len(results) == 0, "mrr": 1.0 if not results else 0.0, "coverage": None}

    rank = first_relevant_rank(results, item)
    coverage = (
        round(len(set(gold_papers) & papers) / len(gold_papers), 3) if gold_papers else None
    )
    return {"hit": bool(rank), "mrr": round(1.0 / rank, 3) if rank else 0.0, "coverage": coverage}


def run_baseline(retrieval: RetrievalService, item: dict, top_k: int) -> list:
    """分流前的唯一路径：等权 RRF top-k。"""
    text = item.get("search_text") or item["question"]
    return retrieval.search(text, top_k=top_k)["fusion"]


def run_routed(retriever: RAGRetriever, item: dict, top_k: int):
    text = item.get("search_text") or item["question"]
    return retriever.route(item["question"], search_text=text, top_k=top_k)


def evaluate(items, retrieval, retriever, top_k: int, mode: str):
    rows = []
    for item in items:
        gold = item.get("gold") or []
        row = {"id": item["id"], "question": item["question"], "expect_type": item.get("expect_type")}

        if mode in ("both", "baseline"):
            t0 = time.perf_counter()
            base = run_baseline(retrieval, item, top_k)
            verdict = judge(base, item)
            row["baseline"] = {
                **verdict,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "papers": sorted({r.arxiv_id for r in base}),
                "top": [
                    {"arxiv_id": r.arxiv_id, "page": r.page, "kind": r.kind, "title": r.title[:40]}
                    for r in base[:3]
                ],
            }

        if mode in ("both", "routed"):
            t0 = time.perf_counter()
            route = run_routed(retriever, item, top_k)
            results = route.results
            verdict = judge(results, item)
            papers = sorted({r.arxiv_id for r in results})
            row["routed"] = {
                **verdict,
                "type": route.trace["type"],
                "type_ok": route.trace["type"] == item.get("expect_type") if item.get("expect_type") else None,
                "method": route.trace["classifier"]["method"],
                "classify_ms": route.trace["classifier"]["latency_ms"],
                "strategy": route.trace["strategy"]["name"],
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "papers": papers,
                "pairs": sum(
                    1 for h in route.trace["strategy"]["hops"] if h.get("pair_detail")
                ),
                "top": [
                    {"arxiv_id": r.arxiv_id, "page": r.page, "kind": r.kind, "title": r.title[:40]}
                    for r in results[:3]
                ],
            }
        rows.append(row)
    return rows


def summarize(rows, mode: str) -> dict:
    def agg(side):
        vals = [r[side] for r in rows if side in r]
        if not vals:
            return {}
        n = len(vals)
        hits = sum(1 for v in vals if v["hit"])
        mrr = sum(v["mrr"] for v in vals) / n
        lat = sum(v["latency_ms"] for v in vals) / n
        cov = [v["coverage"] for v in vals if v.get("coverage") is not None]
        type_ok = [v["type_ok"] for v in vals if v.get("type_ok") is not None]
        clo = [v["classify_ms"] for v in vals if v.get("classify_ms") is not None]
        return {
            "n": n,
            "hit_rate": round(hits / n, 4),
            "mrr": round(mrr, 4),
            "avg_latency_ms": round(lat, 1),
            "coverage": round(sum(cov) / len(cov), 4) if cov else None,
            "type_acc": round(sum(1 for x in type_ok if x) / len(type_ok), 4) if type_ok else None,
            "avg_classify_ms": round(sum(clo) / len(clo), 1) if clo else None,
            "max_classify_ms": round(max(clo), 1) if clo else None,
        }

    out = {"mode": mode}
    if mode in ("both", "baseline"):
        out["baseline"] = agg("baseline")
    if mode in ("both", "routed"):
        out["routed"] = agg("routed")
    if mode == "both":
        b, r = out["baseline"], out["routed"]
        out["delta"] = {
            "hit_rate": round(r["hit_rate"] - b["hit_rate"], 4),
            "mrr": round(r["mrr"] - b["mrr"], 4),
            "avg_latency_ms": round(r["avg_latency_ms"] - b["avg_latency_ms"], 1),
        }
    return out


def render_md(rows, summary, top_k: int, dataset_meta: dict) -> str:
    lines = [
        "# 检索分流评测：分流前 vs 分流后",
        "",
        f"- 数据集：{dataset_meta.get('version', 'unknown')}（{len(rows)} 条）",
        f"- top_k = {top_k}；基线 = `RetrievalService.search()`（分流前唯一路径），"
        f"分流侧 = `RAGRetriever.route()`",
        "- 指标只看召回：hit@k 表示 gold（论文+页码）是否进入 top_k，与答案文本无关",
        "",
        "## 汇总",
        "",
        "| 侧 | hit@k | MRR | 平均耗时 | 论文覆盖(对比题) | 类型准确率 | 分类耗时均值/峰值 |",
        "|---|---|---|---|---|---|---|",
    ]
    for side, name in (("baseline", "分流前"), ("routed", "分流后")):
        s = summary.get(side)
        if not s:
            continue
        lines.append(
            f"| {name} | {s['hit_rate']:.1%} ({int(s['hit_rate'] * s['n'])}/{s['n']}) | {s['mrr']:.3f} | "
            f"{s['avg_latency_ms']} ms | {s['coverage'] if s['coverage'] is not None else '—'} | "
            f"{s['type_acc'] if s['type_acc'] is not None else '—'} | "
            f"{s['avg_classify_ms'] if s['avg_classify_ms'] is not None else '—'} / "
            f"{s['max_classify_ms'] if s['max_classify_ms'] is not None else '—'} ms |"
        )
    if "delta" in summary:
        d = summary["delta"]
        lines.append(
            f"| **差值** | **{d['hit_rate']:+.1%}** | **{d['mrr']:+.3f}** | {d['avg_latency_ms']:+.1f} ms | | | |"
        )

    lines += [
        "",
        "## 逐条对比",
        "",
        "| ID | 问题 | 期望类型 | 分流后判定 | 分流前 hit | 分流后 hit | MRR 前→后 | 召回论文数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        b, t = r.get("baseline"), r.get("routed")
        q = r["question"].replace("|", "\\|")
        q = q if len(q) <= 28 else q[:27] + "…"
        exp = r.get("expect_type") or "—"
        got = f"{t['type']} ({t['method']})" if t else "—"
        bh = "✓" if b and b["hit"] else ("✗" if b else "—")
        th = "✓" if t and t["hit"] else ("✗" if t else "—")
        mrr = f"{b['mrr']:.2f} → {t['mrr']:.2f}" if b and t else "—"
        pc = f"{len(t['papers'])}" if t else "—"
        lines.append(f"| {r['id']} | {q} | {exp} | {got} | {bh} | {th} | {mrr} | {pc} |")

    fails = [
        r for r in rows
        if r.get("routed") and not r["routed"]["hit"]
    ]
    if fails:
        lines += ["", "## 未命中的条目（看细节用）", ""]
        for r in fails:
            t = r["routed"]
            lines.append(
                f"- **{r['id']}** {r['question']} → 策略 `{t['strategy']}`，"
                f"top3 = {[(x['arxiv_id'][:12], x['page']) for x in t['top']]}"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["both", "baseline", "routed"], default="both")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--md-out", default=None)
    args = ap.parse_args()

    dataset = load_dataset()
    items = dataset.get("queries", [])
    print(f"==> 载入 {len(items)} 条评测问题（{dataset.get('version')}）", flush=True)

    retrieval = RetrievalService()
    retriever = RAGRetriever(retrieval) if args.mode in ("both", "routed") else None

    rows = evaluate(items, retrieval, retriever, args.top_k, args.mode)
    summary = summarize(rows, args.mode)
    md = render_md(rows, summary, args.top_k, dataset)

    print(md)

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n==> JSON 已写入 {args.json_out}", file=sys.stderr)
    if args.md_out:
        Path(args.md_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md_out).write_text(md, encoding="utf-8")
        print(f"==> 报告已写入 {args.md_out}", file=sys.stderr)

    if args.mode == "both":
        d = summary["delta"]
        ok = d["hit_rate"] >= 0 and d["mrr"] >= -0.02
        print(
            f"\n==> 结论：hit@k {d['hit_rate']:+.1%}、MRR {d['mrr']:+.3f}，"
            f"{'未下降，约束满足' if ok else '出现下降，需要回看未命中清单'}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
