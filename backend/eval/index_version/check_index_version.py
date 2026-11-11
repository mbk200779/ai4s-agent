"""
13 · 索引 pipeline 版本化的离线 + 在线校验。

跑法（容器内）：
    MSYS_NO_PATHCONV=1 docker exec ai4s-api python /app/eval/index_version/check_index_version.py
    MSYS_NO_PATHCONV=1 docker exec ai4s-api python /app/eval/index_version/check_index_version.py --live
    # 真跑一次"中断→续跑"（约 4 分钟，会真的写向量，慎在生产库上跑）
    MSYS_NO_PATHCONV=1 docker exec ai4s-api python /app/eval/index_version/check_index_version.py --rebuild

四段：
  1. 签名与命名 —— 版本号是否确定、是否随配置变化、collection 名是否可自解释
  2. 切流规则 —— 显式 > 项目 > 比例 > 默认，以及"写路径不跟灰度"
  3. 断点与幂等 —— 游标字段往返、已完成/待取消的短路、点 ID 与解析时同一套
  4. 在线 trace —— 每次问答都能回答"这次读的是哪一版、为什么"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request

# 脚本按 `python /app/eval/...` 跑时 sys.path[0] 是脚本目录，`app` 包不在里面。
# 容器里的工作目录就是 /app，补一次即可（本地用 `python -m` 跑也不受影响）。
sys.path.insert(0, "/app")

from app.core.config import settings
from app.db import reader
from app.services import index_rebuild as ir
from app.services import index_version as iv

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 1. 签名与命名
# ---------------------------------------------------------------------------
def test_signature() -> None:
    print("\n=== 1. 签名与命名 ===")
    sig = iv.current_signature()
    check("签名只含会改变向量的字段",
          set(sig) == {"schema", "splitter", "dense_model", "sparse_model", "embed_max_chars"},
          str(sorted(sig)))
    check("不含批次/并发度这类无关项", "batch_size" not in sig and "pages_per_batch" not in sig)

    v1, v2 = iv.version_of(sig), iv.version_of(dict(sig))
    check("同配置 → 同版本号（确定性）", v1 == v2, v1)
    check("版本号可自解释：idx-<管线>-<hash8>",
          v1.startswith("idx-1.0.0-") and len(v1) == len("idx-1.0.0-") + 8, v1)

    changed = dict(sig, embed_max_chars=sig["embed_max_chars"] + 1)
    check("改截断长度 → 版本号变化（最容易漏的一项）", iv.version_of(changed) != v1,
          f"{v1} vs {iv.version_of(changed)}")

    changed2 = dict(sig, dense_model=sig["dense_model"] + "-x")
    check("换 embedding 模型 → 版本号变化", iv.version_of(changed2) != v1)

    coll = iv.collection_for(v1)
    check("collection 名 = 基础名__版本号", coll == f"{settings.qdrant_collection}__{v1}", coll)
    check("legacy 版本仍指向基础 collection（存量不搬家）",
          iv.collection_for(iv.LEGACY_VERSION) == settings.qdrant_collection)

    fp = iv.fingerprint(sig)
    check("fingerprint 是 12 位（够长到不撞，够短到能读）", len(fp) == 12 and
          fp == hashlib.sha1(json.dumps(sig, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode()).hexdigest()[:12], fp)


# ---------------------------------------------------------------------------
# 2. 切流规则
# ---------------------------------------------------------------------------
def _snapshot_routing() -> list[dict]:
    return iv.routing_rows()


def _restore_routing(rows: list[dict]) -> None:
    """把切流表恢复原样。校验脚本不该改变线上行为——除了它自己要测的那几行。"""
    from app.db import transaction

    with transaction() as c:
        c.execute("DELETE FROM index_routing")
    for r in rows:
        with transaction() as c:
            c.execute(
                """INSERT INTO index_routing
                   (id, scope, project_id, pipeline_version, stable_version, percent,
                    enabled, note, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], r["scope"], r["project_id"], r["pipeline_version"],
                 r.get("stable_version"), r["percent"], r["enabled"], r.get("note"),
                 r["updated_at"]),
            )
    iv._invalidate()


def test_routing() -> None:
    print("\n=== 2. 切流规则 ===")
    saved = _snapshot_routing()
    versions = {v["pipeline_version"]: v for v in iv.list_versions()}
    servable = [p for p, v in versions.items() if v["status"] in ("active", "ready")]
    if len(servable) < 2:
        check("至少两个可服务版本（重建过才有得切）", False, f"只有 {servable}")
        _restore_routing(saved)
        return
    old, new = servable[0], servable[1]

    try:
        iv.set_global(old, percent=0, note="eval")
        r = iv.resolve(project_id="proj_eval")
        check("无灰度时 → default → 全量那一版",
              r["rule"] == "default" and r["pipeline_version"] == old, r["rule"])
        check("trace 里带 collection 与候选版本",
              bool(r["collection"]) and len(r["candidates"]) >= 2,
              f"{r['collection']} candidates={len(r['candidates'])}")

        # 灰度：找一个一定会被抽中的 key 和一个一定抽不中的
        iv.set_global(new, percent=30, stable=old, note="eval 灰度")
        hit = next((f"k{i}" for i in range(200) if iv._bucket(f"k{i}") < 30), None)
        miss = next((f"k{i}" for i in range(200) if iv._bucket(f"k{i}") >= 30), None)
        rh = iv.resolve(project_id=hit)
        rm = iv.resolve(project_id=miss)
        check("灰度命中 → 新版（rule=percent）",
              rh["rule"] == "percent" and rh["pipeline_version"] == new,
              f"{hit}@{rh['bucket']} → {rh['pipeline_version']}")
        check("灰度未命中 → 稳定版",
              rm["pipeline_version"] == old, f"{miss}@{rm['bucket']} → {rm['pipeline_version']}")
        check("分桶是确定的（同一个 key 永远同桶）",
              iv._bucket(hit) == iv._bucket(hit) and iv.resolve(project_id=hit)["bucket"] == rh["bucket"])

        check("写路径不跟灰度（否则 A/B 里混进'旧版缺论文'）",
              iv.write_version() == old, f"write={iv.write_version()} percent=30")

        # 项目规则压过灰度：用那个"抽不中"的 key 钉新版
        iv.set_project(miss, new, note="eval 项目规则")
        rp = iv.resolve(project_id=miss)
        check("项目规则压过灰度比例",
              rp["rule"] == "project" and rp["pipeline_version"] == new, rp["detail"])
        iv.clear_project(miss)
        check("清掉项目规则后回到灰度判断",
              iv.resolve(project_id=miss)["rule"] == "percent")

        # 显式指定压过一切
        re_ = iv.resolve(project_id=miss, explicit=old)
        check("显式指定压过项目规则与灰度",
              re_["rule"] == "explicit" and re_["pipeline_version"] == old)
        try:
            iv.resolve(explicit="idx-9.9.9-deadbeef")
            check("未知版本 → 抛错（不静默降级）", False)
        except iv.IndexVersionError:
            check("未知版本 → 抛错（不静默降级）", True)

        # 回滚
        iv.rollback()
        rb = iv.resolve(project_id=hit)
        check("回滚回到 stable 版且撤掉灰度",
              rb["pipeline_version"] == old and rb["percent"] == 0, rb["pipeline_version"])
        actives = [v["pipeline_version"] for v in iv.list_versions() if v["status"] == "active"]
        check("全量切流后只有一个 active（两个 active = 默认路径不确定）",
              len(actives) == 1, str(actives))
    finally:
        _restore_routing(saved)


# ---------------------------------------------------------------------------
# 3. 断点与幂等
# ---------------------------------------------------------------------------
def test_cursor() -> None:
    print("\n=== 3. 断点与幂等 ===")
    builds = ir.list_builds(limit=20)
    check("builds 表里有记录（重建真的跑过）", bool(builds), f"{len(builds)} 条")
    if not builds:
        return
    done = next((b for b in builds if b["status"] == "done"), None)
    if done:
        check("已完成的 build：done_chunks == total_chunks",
              done["done_chunks"] == done["total_chunks"],
              f"{done['done_chunks']}/{done['total_chunks']}")
        check("已完成的 build 记了点数与耗时",
              (done["points_written"] or 0) > 0 and (done["elapsed_s"] or 0) > 0,
              f"points={done['points_written']} elapsed={done['elapsed_s']}s peak={done['peak_rss_mb']}MB")
        again = ir.run_build(done["id"])
        check("对已完成的 build 再调一次 → 立刻返回，不重灌", again["status"] == "done",
              "同一 build_id 重复投递是安全的")

    partial = next((b for b in builds if b["done_chunks"] and b["done_chunks"] < b["total_chunks"]), None)
    if partial:
        check("中断过的 build 留有游标（续跑的唯一依据）",
              partial["cursor_version"] is not None and partial["cursor_offset"] > 0,
              f"cursor={partial['cursor_version']}@{partial['cursor_offset']}")
    else:
        print("  [SKIP] 没有中断过的 build（用 --rebuild 跑一次中断/续跑）")

    # 点 ID 必须与解析时同一套，否则重建出来的点与 SQLite 对不上、溯源全空
    from app.services.ingest.pipeline import _point_id
    from app.services.vector_store import VectorStoreService

    sample = _point_id("1706.03762", "v7", "paragraph", 12)
    check("点 ID 与解析管线同一套（frag::…::kind::idx）",
          sample == hashlib.md5(b"frag::1706.03762::v7::paragraph::12").hexdigest(),
          sample[:12] + "…")
    check("两组点 ID 方案不混用（chunk:: 是废弃方案）",
          VectorStoreService._chunk_point_id("1706.03762", "v7", 12) != sample)


def test_rebuild_cycle() -> None:
    """真跑一次：起一个 build → 跑 1 批（等价于此时进程被杀）→ 取消 → 续跑到完成。"""
    print("\n=== 3b. 中断 → 续跑（真实写向量，约 3~4 分钟）===")
    b = ir.start_build(note="eval 中断续跑")
    bid = b["id"]
    print(f"  build {bid} total={b['total_chunks']}")
    ir.run_build(bid, max_batches=1)
    mid = ir.get_build(bid)
    check("跑 1 批后停在 running，游标已落",
          mid["status"] == "running" and mid["done_chunks"] < mid["total_chunks"]
          and (mid["cursor_offset"] or 0) > 0,
          f"done={mid['done_chunks']}/{mid['total_chunks']} cursor@{mid['cursor_offset']}")
    first = mid["done_chunks"]

    ir.cancel_build(bid)
    ir.run_build(bid)
    cancelled = ir.get_build(bid)
    check("取消后在批边界停下（不是立刻杀线程）", cancelled["status"] == "cancelled",
          f"done={cancelled['done_chunks']}")
    check("取消不清游标（再跑一次就是续跑）", (cancelled["cursor_offset"] or 0) > 0)

    ir.run_build(bid)
    final = ir.get_build(bid)
    check("续跑跑到完成", final["status"] == "done" and final["done_chunks"] == final["total_chunks"],
          f"{final['done_chunks']}/{final['total_chunks']}")
    check("续跑没有重复写点（点数 == chunk 数）",
          final["points_written"] == final["done_chunks"],
          f"points={final['points_written']} chunks={final['done_chunks']}")
    print(f"  实测：{final['elapsed_s']}s，峰值 RSS {final['peak_rss_mb']}MB，"
          f"CPU {final['cpu_s']}s，batch={final['batch_size']}，首批 {first} 条")


# ---------------------------------------------------------------------------
# 4. 在线：trace 里能不能看出读的是哪一版
# ---------------------------------------------------------------------------
def ask(question: str, base: str, **extra) -> dict:
    body = json.dumps({"question": question, "top_k": 6, **extra}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/v1/qa/ask", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def test_live(base: str) -> None:
    print("\n=== 4. 在线 trace ===")
    versions = {v["pipeline_version"]: v for v in iv.list_versions()}
    d = ask("β2 设为多少", base)
    idx = (d.get("routing") or {}).get("index") or {}
    check("响应里有 routing.index", bool(idx), str(list(idx))[:80])
    pv = idx.get("pipeline_version")
    check("pipeline_version 是已登记的版本", pv in versions, str(pv))
    check("collection 与版本表一致",
          idx.get("collection") == (versions.get(pv) or {}).get("collection"),
          f"{idx.get('collection')}")
    check("带命中规则，能解释'为什么是这一版'",
          idx.get("rule") in ("default", "project", "percent", "explicit"), str(idx.get("rule")))
    # legacy 版本的签名**必须为空**：那批向量是用哪套参数编的已经不知道了，
    # 补一个"看起来像"的签名比留空更糟——它会让人以为版本号可以互相比。
    if idx.get("origin") == "legacy":
        check("legacy 版本不伪造签名（签名不可考就留空）",
              not idx.get("signature"), "origin=legacy, signature={}")
    else:
        check("带配置签名（这一版是哪套参数）",
              bool((idx.get("signature") or {}).get("dense_model")),
              str((idx.get("signature") or {}).get("dense_model")))
    check("检索本身仍然正常（不是靠空结果过测试）",
          ((d.get("routing") or {}).get("recall") or {}).get("returned", 0) > 0,
          f"returned={((d.get('routing') or {}).get('recall') or {}).get('returned')}")

    # 显式指定一个不存在的版本：必须是 400，而不是 500
    try:
        ask("β2 设为多少", base, index_version="idx-9.9.9-deadbeef")
        check("未知版本 → 400", False, "居然成功了")
    except urllib.error.HTTPError as exc:
        check("未知版本 → 400（调用方的错别算成服务故障）", exc.code == 400, str(exc.code))


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="打 HTTP 校验 trace")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--rebuild", action="store_true", help="真跑一次中断/续跑（会写向量）")
    args = ap.parse_args()

    test_signature()
    test_routing()
    test_cursor()
    if args.rebuild:
        test_rebuild_cycle()
    if args.live:
        test_live(args.base)

    print("\n" + "=" * 56)
    if FAILS:
        print(f"FAILED {len(FAILS)}:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
