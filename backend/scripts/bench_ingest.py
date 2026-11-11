"""
长文档解析实测脚本：上传 → 轮询进度 → 记录关键时间点。

设计要点：
- 时间点从"POST 发出"开始算，包含网络与落盘，对应用户真实体感；
- 首屏可用时间取两个口径：进度条出现真实页数（stage=parsing 且 total_pages>0）、
  以及第一批 chunk 已入库（chunks>0，此时检索已经能命中前面几十页）；
- 二次上传单独计时，验证 file_hash 复用是否走的是"不入队"的路径。

用法（在 api 容器内执行）：
    python /app/scripts/bench_ingest.py --file /app/data/bench/textbook_541.pdf
"""
import argparse
import json
import os
import time
from typing import Any

import httpx

API = "http://localhost:8000"


def upload(client: httpx.Client, path: str) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    with open(path, "rb") as fh:
        resp = client.post(
            f"{API}/api/v1/papers/upload",
            files={"file": (os.path.basename(path), fh, "application/pdf")},
            timeout=900,
        )
    elapsed = (time.perf_counter() - t0) * 1000
    resp.raise_for_status()
    return resp.json(), elapsed


def watch(client: httpx.Client, task_id: str, interval: float = 0.5) -> dict[str, Any]:
    """轮询进度直到终态，记录各阶段首次出现的时间与每次跳变。"""
    started = time.perf_counter()
    timeline: list[dict[str, Any]] = []
    first_progress_ms = None
    first_chunk_ms = None
    last_stage = None
    last_page = -1
    last: dict[str, Any] = {}

    while True:
        try:
            snap = client.get(f"{API}/api/v1/ingest/tasks/{task_id}/progress", timeout=30).json()
        except Exception as exc:  # 任务太新时可能 404，短暂重试
            snap = {"stage": "unknown", "error": str(exc)}
        now_ms = (time.perf_counter() - started) * 1000

        stage = snap.get("stage")
        page = snap.get("page") or 0
        if stage != last_stage or page != last_page:
            timeline.append(
                {
                    "t_ms": round(now_ms),
                    "stage": stage,
                    "page": page,
                    "total_pages": snap.get("total_pages"),
                    "pct": snap.get("pct"),
                    "chunks": snap.get("chunks"),
                    "refs": snap.get("refs"),
                    "citations": snap.get("citations"),
                }
            )
            last_stage, last_page = stage, page

        if first_progress_ms is None and stage == "parsing" and (snap.get("total_pages") or 0) > 0:
            first_progress_ms = now_ms
        if first_chunk_ms is None and (snap.get("chunks") or 0) > 0:
            first_chunk_ms = now_ms

        last = snap
        if snap.get("done"):
            break
        if now_ms > 3_600_000:
            last = {**snap, "error": "bench timeout"}
            break
        time.sleep(interval)

    last["_timeline"] = timeline
    last["_first_progress_ms"] = None if first_progress_ms is None else round(first_progress_ms)
    last["_first_chunk_ms"] = None if first_chunk_ms is None else round(first_chunk_ms)
    last["_watch_ms"] = round((time.perf_counter() - started) * 1000)
    return last


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--skip-upload", action="store_true", help="只跑二次上传（复用）计时")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    size_mb = os.path.getsize(args.file) / 1048576
    result: dict[str, Any] = {"file": args.file, "size_mb": round(size_mb, 2)}

    with httpx.Client() as client:
        first_upload_ms = None
        if not args.skip_upload:
            body, first_upload_ms = upload(client, args.file)
            result["upload"] = body
            result["upload_ms"] = round(first_upload_ms)
            print(f"[upload] {first_upload_ms:.0f}ms  task={body.get('task_id')}  reused={body.get('reused')}")

            if body.get("reused"):
                result["watch"] = {"skipped": True}
            else:
                watched = watch(client, body["task_id"], args.interval)
                result["watch"] = watched
                print(
                    f"[watch] total={watched['_watch_ms']}ms  first_progress={watched['_first_progress_ms']}ms  "
                    f"first_chunk={watched['_first_chunk_ms']}ms  stage={watched.get('stage')} "
                    f"pages={watched.get('page')}/{watched.get('total_pages')} "
                    f"chunks={watched.get('chunks')} refs={watched.get('refs')} cites={watched.get('citations')}"
                )
                if watched.get("error"):
                    print(f"[error] {watched['error']}")

        # 二次上传：同一文件再传一次，验证命中 file_hash 后是否"不入队"
        body2, second_upload_ms = upload(client, args.file)
        result["upload_second"] = body2
        result["upload_second_ms"] = round(second_upload_ms)
        print(
            f"[upload-second] {second_upload_ms:.0f}ms  reused={body2.get('reused')}  "
            f"task={body2.get('task_id')}  detail={body2.get('detail')}"
        )

    if result.get("watch", {}).get("_timeline"):
        print("[timeline]")
        for row in result["watch"]["_timeline"]:
            print(
                f"  t={row['t_ms']:>7}ms stage={row['stage']:<12} page={row['page']:>4}/{row['total_pages']} "
                f"chunks={row['chunks']} refs={row['refs']} cites={row['citations']}"
            )

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"[json] {args.json_out}")


if __name__ == "__main__":
    main()
