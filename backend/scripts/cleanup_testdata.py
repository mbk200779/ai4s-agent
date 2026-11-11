"""
清掉压力测试留下的数据，把库恢复成 baseline。

只删"按标题点名"的测试论文（textbook_541 / resume_200p / small_40 / bench_tmp 这一类），
不碰真实的 2 篇样张。四层都要清，少清一层就会留下孤儿：
  1. SQLite：citations / paper_refs / chunks / paper_versions / papers
  2. Qdrant：ai4s_papers 的片段点、ai4s_paper_meta 的元数据点
  3. MinIO：papers 桶里的原始 PDF
  4. 工作目录：/app/data/ingest 下没有版本行认领的目录

用法：
    python /app/scripts/cleanup_testdata.py            # 只报告
    python /app/scripts/cleanup_testdata.py --apply    # 真删
"""
import os
import sys

# 这个脚本会被 `python /app/scripts/xxx.py` 直接跑，此时 sys.path[0] 是 scripts 目录，
# 不是 /app，所以要手动把应用根目录补进去。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.core.config import settings
from app.db import reader, transaction
from app.services.storage import StorageService

# 测试用的样张标题（papers.title 就是上传时的文件名）
TEST_TITLES = ("textbook_541.pdf", "resume_200p.pdf", "small_40.pdf", "bench_tmp")
# 早期 bench / smoke 脚本直接写进向量库、没有落 papers 行的脏点
ORPHAN_ARXIV = ("bench_tmp", "upload_smoke_0003")


def main(apply: bool) -> int:
    with reader() as c:
        rows = c.execute(
            f"select id, arxiv_id, title from papers where title in ({','.join('?' * len(TEST_TITLES))})",
            TEST_TITLES,
        ).fetchall()
        papers = [(r["id"], r["arxiv_id"], r["title"]) for r in rows]
        vers = []
        for pid, _, _ in papers:
            vers += [
                (r["id"], r["pdf_s3_key"])
                for r in c.execute(
                    "select id, pdf_s3_key from paper_versions where paper_id = ?", (pid,)
                )
            ]
        counts_before = {
            t: c.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("papers", "paper_versions", "chunks", "paper_refs", "citations")
        }

    print("待删论文：")
    for pid, aid, title in papers:
        print(f"  {pid}  {aid}  {title}")
    print(f"待删版本：{[v for v, _ in vers]}")
    print(f"清理前：{counts_before}")

    client = QdrantClient(url=settings.qdrant_url)
    arxiv_ids = [aid for _, aid, _ in papers] + list(ORPHAN_ARXIV)

    def _count(collection: str, aid: str) -> int:
        return client.count(
            collection,
            count_filter=qm.Filter(
                must=[qm.FieldCondition(key="arxiv_id", match=qm.MatchValue(value=aid))]
            ),
            exact=True,
        ).count

    for collection in (settings.qdrant_collection, settings.qdrant_paper_meta_collection):
        for aid in arxiv_ids:
            n = _count(collection, aid)
            if n:
                print(f"  qdrant {collection}/{aid}: {n} 点")
        _n_noaid = client.count(
            collection,
            count_filter=qm.Filter(
                must=[qm.IsEmptyCondition(is_empty=qm.PayloadField(key="arxiv_id"))]
            ),
            exact=True,
        ).count
        if _n_noaid:
            print(f"  qdrant {collection}/<无 arxiv_id>: {_n_noaid} 点")

    if not apply:
        print("\n只报告模式，加 --apply 才会真删。")
        return 0

    # ---- 1. SQLite ----
    with transaction() as c:
        for vid, _ in vers:
            # citations 没有 version_id 列，它挂在 ref_id / chunk_id 上，
            # 所以必须先把这两张表的 id 捞出来再删，否则会留一堆悬空引用。
            c.execute(
                "delete from citations where ref_id in "
                "(select id from paper_refs where version_id = ?) "
                "or chunk_id in (select id from chunks where version_id = ?)",
                (vid, vid),
            )
            c.execute("delete from paper_refs where version_id = ?", (vid,))
            c.execute("delete from chunks where version_id = ?", (vid,))
            c.execute("delete from paper_versions where id = ?", (vid,))
        for pid, _, _ in papers:
            c.execute("delete from papers where id = ?", (pid,))

    # ---- 2. Qdrant ----
    for collection in (settings.qdrant_collection, settings.qdrant_paper_meta_collection):
        for aid in arxiv_ids:
            if _count(collection, aid):
                client.delete(
                    collection,
                    points_selector=qm.FilterSelector(
                        filter=qm.Filter(
                            must=[
                                qm.FieldCondition(
                                    key="arxiv_id", match=qm.MatchValue(value=aid)
                                )
                            ]
                        )
                    ),
                )
        # 老版本 bench 脚本写进去的点没有 arxiv_id，靠 payload 找不回来，按空值筛掉
        client.delete(
            collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[qm.IsEmptyCondition(is_empty=qm.PayloadField(key="arxiv_id"))]
                )
            ),
        )

    # ---- 3. MinIO ----
    storage = StorageService()
    for _, s3_key in vers:
        if not s3_key:
            continue
        storage.delete_pdf(s3_key)

    # ---- 4. 工作目录 ----
    from app.services.ingest.pipeline import sweep_work_dirs

    swept = sweep_work_dirs(keep_s=0)
    if swept:
        print(f"  清理工作目录：{swept}")

    with reader() as c:
        counts_after = {
            t: c.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("papers", "paper_versions", "chunks", "paper_refs", "citations")
        }
    print(f"清理后：{counts_after}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
