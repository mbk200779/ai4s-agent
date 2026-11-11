#!/usr/bin/env python3
"""
30 题置信分布统计（07）。

只做三件事，但对"三档到底有没有用"是决定性的：

1. **分布**：overall / 按题型各有多少 verified、needs_check、unsupported。
   如果某个题型长期只有 needs_check，说明那一类的判据还缺东西，而不是"模型不行"。
2. **缺什么**：把 `missing` 的 code 频次拉出来。这是"需核对"的可执行部分——
   用户看到"12 条缺表格"就知道该去翻 Table，而不是把整段重读一遍。
3. **口径校验**：三条硬约束在数据上是否成立——
   - rejected 只出现在"零片段"时（不是"分低"）；
   - 每条 needs_check / unsupported 都带 missing 或 reason（不得静默降级）；
   - 每条 claim 都有 level（没有默认值兜底）。

跑法（容器内）：
    docker exec ai4s-api python -m scripts.eval_confidence --out eval/confidence_dist
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

import requests

DEFAULT_DATASET = Path("/app/eval/confidence_queries.json")
LEVELS = ("verified", "needs_check", "unsupported")


def load_dataset(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ask(api: str, question: str, top_k: int) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{api}/api/v1/qa/ask",
            json={"question": question, "top_k": top_k},
            timeout=180,
        )
        body = resp.json()
        elapsed = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "latency_ms": round(elapsed, 1)}
        body["latency_ms"] = round(elapsed, 1)
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


def ask_inprocess(service, question: str, top_k: int) -> Dict[str, Any]:
    """
    不经过 HTTP 直接调服务。

    为什么需要它：换模型（`LITELLM_MODEL=dashscope/qwen-max`）只能通过进程环境变量，
    而容器里的环境变量在创建时就固定了，`docker restart` 不会重读 compose。
    用 `docker exec -e LITELLM_MODEL=...` 起一个临时进程，能换模型又不动线上容器。
    """
    t0 = time.perf_counter()
    try:
        body = service.ask(question, top_k=top_k)
        body["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


def normalize(item: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    conf = body.get("confidence") or {}
    claims = body.get("claims") or []
    level = conf.get("level")
    return {
        "id": item["id"],
        "category": item.get("category", "unknown"),
        "question": item["question"],
        "level": level if conf.get("applicable", True) else "not_applicable",
        "score": conf.get("score"),
        "rejected": bool(body.get("rejected")),
        "reject_reason": body.get("reject_reason"),
        # HTTP 返回 citations（按 chunk 去重的引用清单），进程内返回 sources
        "n_sources": len(body.get("citations") or body.get("sources") or []),
        "counts": conf.get("counts") or {},
        "missing": conf.get("missing") or [],
        "notes": conf.get("notes") or [],
        "signals": conf.get("signals") or {},
        "latency_ms": body.get("latency_ms"),
        "claims": [
            {
                "claim_id": c.get("claim_id"),
                "level": c.get("level"),
                "text": (c.get("text") or "")[:120],
                "missing": [m["code"] for m in (c.get("missing") or [])],
                "status": c.get("status"),
                "reason": c.get("reason"),
                "locate": (c.get("confidence") or {}).get("locate"),
                "signals": (c.get("confidence") or {}).get("signals") or {},
            }
            for c in claims
        ],
        "error": body.get("error"),
    }


def pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.0f}%" if total else "-"


def build_report(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    ok = [r for r in rows if not r.get("error")]
    total = len(ok)
    by_level = Counter(r["level"] for r in ok)

    lines = [
        "# 07 · 分级置信：30 题分布统计",
        "",
        f"- 数据集：`{meta.get('version', '-')}`（{len(rows)} 题）",
        *([f"- 运行说明：{meta['note']}"] if meta.get("note") else []),
        f"- 题面：`backend/eval/confidence_queries.json`",
        f"- 统计口径：`/api/v1/qa/ask` 返回的 `confidence.level`，按题计（一题一档）",
        "",
        "## 总体分布",
        "",
        "| 档位 | 题数 | 占比 | 含义 |",
        "|---|---|---|---|",
    ]
    labels = {
        "verified": "已验证：原文有直接证据，可据此判定",
        "needs_check": "需核对：有相关证据，但需人工核对",
        "unsupported": "证据不足：没有找到能支撑的原文",
        "not_applicable": "不适用：闲聊/直答，不涉及文献证据",
    }
    for lv in ("verified", "needs_check", "unsupported", "not_applicable"):
        lines.append(f"| {lv} | {by_level.get(lv, 0)} | {pct(by_level.get(lv, 0), total)} | {labels[lv]} |")
    lines.append(f"| **合计** | **{total}** | 100% | 失败 {len(rows) - total} 题 |")

    # 按题型
    lines += ["", "## 按题型", "", "| 类别 | 题数 | verified | needs_check | unsupported | 不适用 |", "|---|---|---|---|---|---|"]
    cats = defaultdict(list)
    for r in ok:
        cats[r["category"]].append(r)
    for cat, items in sorted(cats.items()):
        c = Counter(x["level"] for x in items)
        lines.append(
            f"| {cat} | {len(items)} | {c.get('verified', 0)} | {c.get('needs_check', 0)} | "
            f"{c.get('unsupported', 0)} | {c.get('not_applicable', 0)} |"
        )

    # 缺什么
    codes = Counter()
    code_text: Dict[str, str] = {}
    for r in ok:
        for m in r["missing"]:
            codes[m["code"]] += m.get("claims", 1)
            code_text.setdefault(m["code"], m["text"])
    lines += ["", "## 「需核对」缺什么（按句子计）", "", "| 缺项 | 句数 | 说明 |", "|---|---|---|"]
    if codes:
        for code, n in codes.most_common():
            lines.append(f"| `{code}` | {n} | {code_text.get(code, '')} |")
    else:
        lines.append("| - | 0 | 本次没有 needs_check 句子 |")

    # 口径校验
    rejected = [r for r in ok if r["rejected"]]
    bad_reject = [r for r in rejected if r["n_sources"] > 0]
    silent = []
    for r in ok:
        for c in r["claims"]:
            if c["level"] in ("needs_check", "unsupported") and not c["missing"] and not c["reason"]:
                silent.append(f"{r['id']}:{c['claim_id']}")
    missing_level = [
        f"{r['id']}:{c['claim_id']}" for r in ok for c in r["claims"] if not c["level"]
    ]
    lines += [
        "",
        "## 口径校验",
        "",
        "| 约束 | 结果 |",
        "|---|---|",
        f"| 完整拒答只在「零片段」时出现 | {'通过' if not bad_reject else '失败：' + str([r['id'] for r in bad_reject])} |",
        f"| 需核对/证据不足必带缺项或理由（不得静默降级） | {'通过' if not silent else '失败：' + ', '.join(silent[:5])} |",
        f"| 每条 claim 都有 level（无默认兜底） | {'通过' if not missing_level else '失败：' + ', '.join(missing_level[:5])} |",
    ]

    # 明细
    lines += ["", "## 逐题明细", "", "| ID | 类别 | 档位 | 分 | 片段 | 已验证/需核对/不足 | 主要缺项 | 题目 |", "|---|---|---|---|---|---|---|---|"]
    for r in ok:
        c = r["counts"]
        top = "、".join(m["code"] for m in r["missing"][:2]) or "-"
        q = r["question"].replace("|", "\\|")
        lines.append(
            f"| {r['id']} | {r['category']} | {r['level']} | {r['score'] if r['score'] is not None else '-'} | "
            f"{r['n_sources']} | {c.get('verified', 0)}/{c.get('needs_check', 0)}/{c.get('unsupported', 0)} | {top} | {q} |"
        )

    lat = [r["latency_ms"] for r in ok if r.get("latency_ms")]
    if lat:
        lines += [
            "",
            "## 延迟",
            "",
            f"- 平均 {statistics.mean(lat):.0f}ms · 中位 {statistics.median(lat):.0f}ms "
            f"· P90 {sorted(lat)[int(len(lat) * 0.9) - 1]:.0f}ms · 最大 {max(lat):.0f}ms",
            "- 含跨语言语义核验（每答 1~6 对，一次批量 embed，约 30~90ms）",
        ]

    errs = [r for r in rows if r.get("error")]
    if errs:
        lines += ["", "## 失败题目", ""]
        for r in errs:
            lines.append(f"- `{r['id']}` {r['question']} → {r['error']}")

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--out", default="eval/confidence_dist")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--inprocess", action="store_true", help="不经 HTTP，直接调 QAService（用于换模型跑）")
    ap.add_argument("--note", default="", help="写进报告头部的说明（例如本次用的模型）")
    args = ap.parse_args()

    service = None
    if args.inprocess:
        from app.services.qa_service import QAService

        service = QAService()

    meta = load_dataset(Path(args.dataset))
    if args.note:
        meta["note"] = args.note
    questions = meta["questions"]
    print(f"==> {len(questions)} 题", file=sys.stderr)

    rows: List[Dict[str, Any]] = []
    for i, item in enumerate(questions, 1):
        body = (
            ask_inprocess(service, item["question"], args.top_k)
            if service is not None
            else ask(args.api, item["question"], args.top_k)
        )
        row = normalize(item, body)
        rows.append(row)
        lv = row.get("level") or ("ERROR" if row.get("error") else "-")
        print(f"[{i:02d}/{len(questions)}] {item['id']:<4} {lv:<15} {row.get('latency_ms')}ms", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(
        json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = build_report(rows, meta)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n已写入 {out.with_suffix('.md')} / {out.with_suffix('.json')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
