"""
综述生成探针：跑一次完整流水线，把 Markdown 与结构化结果落盘。

用途有三个，都指向同一件事——**结果要能被别人复核**：
1. 生成 `docs/` 里引用的真实综述片段（-out / -json）；
2. 调 prompt 或阈值后对比前后差异（同一主题跑两次，diff 两个 md）；
3. 答辩/评审现场复现：「这份综述是这条命令跑出来的」。

用法：
    docker exec -i ai4s-api python scripts/review_probe.py --topic "预训练-微调范式是否优于从头训练"
    docker exec    ai4s-api python scripts/review_probe.py --topic "..." \
        --out /app/eval/review_sample.md --json /app/eval/review_sample.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.review import ReviewPipeline  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--arxiv-ids", nargs="*", default=None)
    ap.add_argument("--out", default=None, help="综述 Markdown 落盘路径")
    ap.add_argument("--json", dest="json_out", default=None, help="结构化结果落盘路径")
    args = ap.parse_args()

    t0 = time.time()
    result = ReviewPipeline().generate(args.topic, arxiv_ids=args.arxiv_ids)
    elapsed = time.time() - t0

    print(f"\n主题：{args.topic}")
    print(f"耗时：{elapsed:.1f}s   淘汰：{result['rejected']}")
    if result["rejected"]:
        print(f"原因：{result.get('reject_reason')}")
        return 1

    stats = result["stats"]
    print(
        "规模：{paper_count} 篇 / {raw_claims} 条 claim → {cluster_count} 簇"
        "（支持 {support}、反对 {oppose}、有条件 {conditional}）；"
        "仅单篇支持 {single_paper} 簇".format(**stats)
    )
    print(
        "冲突：确认 {conflicts_confirmed}、否定 {conflicts_rejected}、"
        "待复核 {conflicts_pending}".format(**stats)
    )
    print(
        f"约束：无引用被删句 {stats['dropped_sentences']} 句、"
        f"抹平分歧违规 {stats['violations']} 句、降级 {stats['degraded']}"
    )
    cfg = result["config"]
    print(f"版本：prompt {cfg['prompt_version']} · 配置 {cfg['version']}/{cfg['fingerprint']}")

    print("\n观点簇：")
    for row in result["claims"]:
        mark = "（仅单篇支持）" if row["strength"] == "single_paper" else ""
        print(f"  {row['id']} [{row['stance_label']}] {row['paper_count']} 篇{mark} — {row['statement'][:56]}")
    if result["conflicts"]:
        print("\n冲突：")
        for e in result["conflicts"]:
            print(
                f"  {e['id']} {e['clusters']} {e['type']} confirmed={e['confirmed']} — {e['reason'][:70]}"
            )

    if args.out:
        Path(args.out).write_text(result["markdown"], encoding="utf-8")
        print(f"\nMarkdown → {args.out}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON → {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
