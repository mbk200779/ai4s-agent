"""
流式问答探针：跑一条问题，把整条事件流原样存下来，再回放核对。

交付物里的「一次完整对话的原始事件日志」就是这个脚本产出的：
- `<out>.sse`    —— 服务端原始 SSE 帧（一字未改，含 id/event/data 行）
- `<out>.jsonl`  —— 规范化事件（每行一条，按 seq 排序）
- `<out>.md`     —— 人读摘要：阶段耗时、token 数、claim 支持率、角标落点

为什么要单独"回放一遍"：
归档日志是这次对话唯一的客观记录。如果回放拿不到同样的条数，
说明归档链路（MinIO 写入 / GET /qa/stream/{trace_id}）有问题，
而那种问题在界面上完全看不见——只会在答辩现场翻车。

用法：
    python -m scripts.stream_probe --question "β2 的取值是多少？"
    python -m scripts.stream_probe --question "..." --out ../backend/eval/stream_trace
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

API = os.environ.get("API_BASE", "http://localhost:8000")


def _post_stream(payload: dict, timeout: float = 180.0):
    """逐帧读 SSE。返回 (events, raw_text, ttfb_ms)。"""
    req = urllib.request.Request(
        f"{API}/api/v1/qa/stream",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    started = time.perf_counter()
    events: list[dict] = []
    raw_lines: list[str] = []
    first_byte_ms: float | None = None
    event_name = ""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n")
            if first_byte_ms is None:
                first_byte_ms = round((time.perf_counter() - started) * 1000, 1)
            if line.startswith(": "):
                raw_lines.append(line)          # 心跳，不进结构化事件
                continue
            raw_lines.append(line)
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                events.append({"event": event_name, "data": data})
    return events, "\n".join(raw_lines), first_byte_ms or 0.0


def _get_json(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _summary_md(question: str, events: list[dict], ttfb: float) -> str:
    stages = [e for e in events if e["event"] == "stage"]
    tokens = [e for e in events if e["event"] == "token"]
    claims = [e for e in events if e["event"] == "claim"]
    done = next((e["data"] for e in events if e["event"] == "done"), {})
    retrieval = next((e["data"] for e in events if e["event"] == "retrieval"), {})

    lines = [f"# 流式对话事件摘要", "", f"- 问题：{question}",
             f"- trace_id：`{done.get('trace_id', '?')}`",
             f"- 首字节：{ttfb} ms，总时长：{done.get('timings', {}).get('total_ms')} ms",
             f"- 事件总数：{len(events)}（stage {len(stages)} / token {len(tokens)} / claim {len(claims)}）",
             f"- 首 token：{done.get('timings', {}).get('ttft_ms')} ms", ""]
    lines += ["## 阶段", "", "| 阶段 | 相位 | 耗时 ms | 关键信息 |", "| --- | --- | --- | --- |"]
    for e in stages:
        d = e["data"]
        detail = d.get("detail") or {}
        brief = ", ".join(
            f"{k}={v}" for k, v in detail.items()
            if k in ("type_label", "hits", "injected", "applied", "degraded", "chars", "stripped_markers", "translated")
        )
        lines.append(f"| {d.get('label')} | {d.get('phase')} | {d.get('ms')} | {brief} |")

    lines += ["", "## 检索命中", ""]
    for c in (retrieval.get("chunks") or []):
        lines.append(
            f"- #{c['index']} `{c['arxiv_id']}` p.{c['page']} · {c['kind']} · "
            f"score={c['score']} · anchors={'Y' if c['has_anchors'] else 'N'}"
        )

    lines += ["", "## 主张与证据", "", "| claim | 状态 | 正文 | 证据 |", "| --- | --- | --- | --- |"]
    for c in claims:
        d = c["data"]
        ev = "; ".join(
            f"#{x['index']} p.{x['page']} {x['ref_id'] or '-'} ({x['status']}, {x['match_score']})"
            for x in d.get("citations") or []
        ) or "（无）"
        lines.append(f"| {d['claim_id']} | {d['status']} | {d['text'][:40]} | {ev} |")

    stats = done.get("claim_stats") or {}
    lines += ["", f"支持率：{stats.get('supported', 0)}/{stats.get('claims', 0)}", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default="β2 的取值是多少？")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--out", default="eval/stream_trace")
    args = ap.parse_args()

    events, raw, ttfb = _post_stream({"question": args.question, "top_k": args.top_k})
    if not events:
        print("没有收到任何事件", file=sys.stderr)
        return 1

    done = next((e["data"] for e in events if e["event"] == "done"), None)
    if done is None:
        print("事件流里没有 done：", json.dumps(events[-1], ensure_ascii=False)[:400], file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(f"{args.out}.sse", "w", encoding="utf-8") as fh:
        fh.write(raw + "\n")
    with open(f"{args.out}.jsonl", "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    # 回放核对：归档条数必须与实时事件数一致（done 那条的 trace_key 是自引用，允许缺）
    replay_note = "未归档（STREAM_LOG_ENABLED=false？）"
    if done.get("trace_key"):
        try:
            replay = _get_json(f"/api/v1/qa/stream/{done['trace_id']}")
            replay_note = f"回放 {replay['count']} 条 / 实时 {len(events)} 条 → {'一致' if replay['count'] == len(events) else '不一致'}"
        except Exception as exc:  # noqa: BLE001
            replay_note = f"回放失败：{exc}"

    with open(f"{args.out}.md", "w", encoding="utf-8") as fh:
        fh.write(_summary_md(args.question, events, ttfb))
        fh.write(f"\n归档核对：{replay_note}\n")

    print(f"trace_id = {done['trace_id']}")
    print(f"事件条数 = {len(events)}（stage/retrieval/token/claim/done）")
    print(f"首字节 {ttfb} ms，首 token {done['timings'].get('ttft_ms')} ms，总 {done['timings'].get('total_ms')} ms")
    print(f"claims = {done['claim_stats']}")
    print(f"归档：{done.get('trace_key')}；{replay_note}")
    print(f"产出：{args.out}.sse / .jsonl / .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
