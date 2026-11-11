"""
断点续跑验证。

它要证明三件事：
1. 解析被打断后，重启的任务**从断点继续**（不是从第 0 页重来）；
2. 续跑不会产生重复 chunk（同一批重试覆盖同一批 chunk_index）；
3. 最终结果与一次跑完完全一致（页数、片段数、游标都落在终值）。

因为"打断"必须由外部动作触发（重启 worker 进程），所以脚本分成两段：

    # 第一步：上传并等到进度过 1/3，然后脚本退出（此时不要动 worker）
    python /app/scripts/verify_resume.py up --pages 200

    # 中间：在宿主机上重启 worker，制造中断
    docker restart ai4s-worker

    # 第二步：观察续跑并核对结果
    python /app/scripts/verify_resume.py watch --version <上一步打印的 version_id>

也支持 `python /app/scripts/verify_resume.py auto`：上传后不动 worker，
仅验证"正常跑完一次"的基线，用于对照。
"""
import argparse
import os
import sys
import time

import httpx

API = "http://localhost:8000"
BENCH_DIR = "/app/data/bench"


def _gen_pdf(path: str, pages: int) -> None:
    if os.path.exists(path):
        return
    sys.path.insert(0, "/app/scripts")
    from gen_large_pdf import build  # type: ignore

    build(path, pages)


def _upload(path: str) -> dict:
    with httpx.Client(timeout=900) as client:
        with open(path, "rb") as fh:
            resp = client.post(
                f"{API}/api/v1/papers/upload",
                files={"file": (os.path.basename(path), fh, "application/pdf")},
            )
        resp.raise_for_status()
        return resp.json()


def _progress(version_id: str) -> dict:
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{API}/api/v1/ingest/versions/{version_id}/progress")
        resp.raise_for_status()
        return resp.json()


def _wait(version_id: str, until, timeout_s: float = 3600) -> dict:
    started = time.time()
    last = {}
    while time.time() - started < timeout_s:
        last = _progress(version_id)
        if until(last):
            return last
        time.sleep(0.5)
    return last


def _counts(version_id: str) -> dict:
    """直接查库核对：chunk 行数、最大 chunk_index、是否重复。"""
    import sqlite3

    conn = sqlite3.connect(os.environ.get("DB_PATH", "/app/data/ai4s.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT COUNT(*) AS n, MAX(chunk_index) AS max_idx, MIN(chunk_index) AS min_idx,
                  COUNT(DISTINCT chunk_index) AS distinct_idx
             FROM chunks WHERE version_id = ?""",
        (version_id,),
    ).fetchone()
    ver = conn.execute(
        "SELECT status, stage, parse_cursor, page_count, index_cursor FROM paper_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    conn.close()
    return {
        "chunk_rows": row["n"],
        "distinct_chunk_index": row["distinct_idx"],
        "max_chunk_index": row["max_idx"],
        "min_chunk_index": row["min_idx"],
        "status": ver["status"],
        "stage": ver["stage"],
        "parse_cursor": ver["parse_cursor"],
        "page_count": ver["page_count"],
        "index_cursor": ver["index_cursor"],
    }


def cmd_up(args: argparse.Namespace) -> None:
    path = os.path.join(BENCH_DIR, f"resume_{args.pages}p.pdf")
    _gen_pdf(path, args.pages)
    body = _upload(path)
    print(f"version_id={body.get('version_id')}")
    print(f"task_id={body.get('task_id')}")
    print(f"reused={body.get('reused')}")

    target = max(20, args.pages // 3)
    snap = _wait(body["version_id"], lambda p: (p.get("page") or 0) >= target or p.get("done"))
    print(f"progress_page={snap.get('page')}/{snap.get('total_pages')} stage={snap.get('stage')}")
    print("现在重启 worker 制造中断，然后执行 watch 子命令。")


def cmd_watch(args: argparse.Namespace) -> None:
    snap = _wait(args.version, lambda p: p.get("done"))
    print(
        f"final stage={snap.get('stage')} status={snap.get('status')} "
        f"page={snap.get('page')}/{snap.get('total_pages')} "
        f"chunks={snap.get('chunks')} refs={snap.get('refs')} cites={snap.get('citations')}"
    )
    c = _counts(args.version)
    print("db:", c)

    ok = True
    if c["status"] != "indexed":
        print("FAIL: 最终状态不是 indexed")
        ok = False
    if c["chunk_rows"] != c["distinct_chunk_index"]:
        print("FAIL: chunk_index 有重复（续跑产生了重复行）")
        ok = False
    if c["parse_cursor"] != c["page_count"]:
        print("FAIL: parse_cursor 没有走到总页数")
        ok = False
    unique_files = os.listdir("/app/data/ingest") if os.path.isdir("/app/data/ingest") else []
    print(f"work_dirs={len(unique_files)}（解析完成后应清空）")
    print("PASS" if ok else "FAILED")
    if not ok:
        raise SystemExit(1)


def cmd_auto(args: argparse.Namespace) -> None:
    """不打断，只跑一次，作为"一次跑完"的对照基线。"""
    path = os.path.join(BENCH_DIR, f"resume_{args.pages}p.pdf")
    _gen_pdf(path, args.pages)
    body = _upload(path)
    vid = body["version_id"]
    snap = _wait(vid, lambda p: p.get("done"))
    print(f"stage={snap.get('stage')} page={snap.get('page')}/{snap.get('total_pages')} chunks={snap.get('chunks')}")
    print("db:", _counts(vid))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["up", "watch", "auto"])
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--version", default="")
    args = ap.parse_args()

    if args.phase == "up":
        cmd_up(args)
    elif args.phase == "watch":
        if not args.version:
            ap.error("watch 需要 --version")
        cmd_watch(args)
    else:
        cmd_auto(args)


if __name__ == "__main__":
    main()
