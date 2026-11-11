#!/usr/bin/env python3
"""
问答"有没有输出"的体检脚本（不需要浏览器，不打 LLM 以外的东西）。

用法（容器内）：
    docker exec ai4s-api python /app/eval/baseline/check_qa_output.py [--json]

四类判据，缺一条都算不通过：
  1. 可答 · 点查   答案非空 + 至少 1 条角标落到具体句子
  2. 可答 · 对比   同上（对比型最容易退化成"证据不足"）
  3. 库里没有      必须给出结构化空检索卡（uncertainty.empty），而不是一句"无法回答。"
  4. 空问题/闲聊   不报错、有回复，且不进入检索分支

为什么单独立这个脚本：评测基线回答的是"好不好"（Recall / 引用准确率），
这个脚本回答的是"有没有"（页面会不会白屏）。答辩现场先跑这个，10 秒能看出链路通不通，
再去跑 make eval 看质量。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")

from app.services.qa_service import QAService  # noqa: E402

CASES = [
    {
        "name": "可答 · 点查",
        "question": "β2 的取值是多少？",
        "expect": "answer",
    },
    {
        "name": "可答 · 对比",
        "question": "Transformer 相比 RNN 的核心改进是什么？",
        "expect": "answer",
    },
    {
        "name": "库里没有",
        "question": "RAG 如何缓解大模型幻觉？",
        "expect": "empty",
    },
]


def check(service: QAService, case: dict) -> dict:
    result = service.ask(case["question"], top_k=5)
    answer = (result.get("answer") or "").strip()
    refs = result.get("answer_refs") or []
    empty = (result.get("uncertainty") or {}).get("empty") or {}
    stats = result.get("claim_stats") or {}
    level = (result.get("confidence") or {}).get("level")

    if case["expect"] == "answer":
        ok = bool(answer) and len(refs) > 0 and not empty
        why = (
            ""
            if ok
            else f"答案 {len(answer)} 字 / 角标 {len(refs)} 条 / empty={empty.get('reason')}"
        )
    else:
        # 空检索必须给结构化状态：有 title/detail/scope，且带得出下一步（改写建议或最近片段）
        ok = bool(empty) and bool(empty.get("title")) and bool(empty.get("detail"))
        ok = ok and bool(empty.get("suggestions") or empty.get("nearest"))
        why = "" if ok else f"没给空检索卡（answer={answer[:40]!r}）"

    return {
        "name": case["name"],
        "question": case["question"],
        "ok": ok,
        "why": why,
        "answer": answer[:80],
        "refs": len(refs),
        "level": level,
        "claims": stats.get("claims", 0),
        "verified": stats.get("verified", 0),
        "empty_reason": empty.get("reason"),
        "reject_reason": result.get("reject_reason"),
        "total_ms": ((result.get("routing") or {}).get("recall") or {}).get("latency_ms"),
    }


def main() -> int:
    service = QAService()
    rows = [check(service, c) for c in CASES]
    failed = [r for r in rows if not r["ok"]]

    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"[{mark}] {r['name']:10s} conf={str(r['level']):11s} refs={r['refs']} "
                  f"claims={r['claims']} verified={r['verified']} empty={r['empty_reason']}")
            print(f"        Q: {r['question']}")
            if r["answer"]:
                print(f"        A: {r['answer']}")
            if r["reject_reason"]:
                print(f"        reason: {r['reject_reason']}")
            if not r["ok"]:
                print(f"        !! {r['why']}")
        print(f"\n{len(rows) - len(failed)}/{len(rows)} 通过")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
