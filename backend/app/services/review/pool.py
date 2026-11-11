"""
证据池构建：综述要的是「覆盖」，不是「top-k 相关」。

为什么不能直接拿一次 `/retrieve` 的 top-k 去写综述：
1. **一篇论文刷满池子**。主题检索按相关度排序，同一篇论文的不同段落天然最像主题，
   于是 top-10 里有 8 段来自同一篇，综述就成了"这一篇的摘要"。
2. **方向相反的结论永远排不到一起**。反例片段通常措辞更保守、和主题的相似度更低，
   单次 top-k 基本捞不到它，冲突检测也就无从谈起——这是"综述变摘要拼接"的机理。
3. **图/表说明文字会污染证据**。本项目的 figure 片段里有一类是**我们自己的模板回填文案**
   （"该图表展示了不同实验组在关键性能指标……"），它不是原文，拿它当证据等于
   拿我们的模板当文献。reference 片段同理：参考文献表里不会有可综述的结论。

所以证据池按三层拼：
  ① 主题检索（原问题）
  ② 视角检索（主题 × 方法/结果/局限…，专治"反例捞不到"）
  ③ 逐论文补检（每个 arxiv_id 各查一次，专治"一篇刷满"）
最后按 chunk_id 去重、按论文配额截断，并保留 `hit_source` 说明每段是从哪条路进来的。
"""
from __future__ import annotations

import re
from typing import Any

from app.core.config import settings
from app.db.connection import reader
from app.services.retrieval_service import RetrievalService

# 视角词：对每一个都单独发一次检索。这些词不是"更好的 query"，
# 而是**不同的检索意图**——局限/失效条件那一支就是专门去捞反例的。
ANGLES = (
    "实验结果与指标对比",
    "局限与失效条件",
    "与基线模型的差异",
    "训练成本与效率",
)

# 只收正文段落（payload 里的 kind 是 `paragraph`，库里同名概念是 `chunk_type='text'`）。
# 表格**故意不收**：
# 本项目的表格 chunk 是"逐列扫描"的产物，实测文本长这样——
#   `|  |  |  | …`（空单元格墙）、`ehT p | waL u | lliw t- | reven`（整段逆向）
# 它适合 05/06 的数值规则召回（那套逻辑专门解析表格结构），
# 却不适合当"逐字引文"的证据源：claim 抽取要求 `quote` 能回贴原文，
# 而这类残渣压根回贴不回去，硬收进来只会让每条 claim 都卡在引文核对上。
ALLOWED_KINDS = {"paragraph", "text"}
MIN_CHUNK_CHARS = 60
# 检索请求量 / 实际保留量。这两个数必须分开：拿到 20 条再筛出 5 条，
# 和"只要 5 条"完全不是一回事——后者一旦前 5 条里有一半是表格残渣，
# 这篇论文就从证据池里消失了，覆盖度直接崩掉。
RETRIEVE_MULTIPLIER = 4


def _catalog() -> list[dict[str, Any]]:
    """已入库且有 chunk 的论文清单（综述要逐篇补检，先得知道有哪几篇）。"""
    with reader() as c:
        rows = c.execute(
            """SELECT DISTINCT p.arxiv_id AS arxiv_id, p.title AS title
                 FROM papers p
                 JOIN paper_versions v ON v.paper_id = p.id AND v.is_current = 1
                 JOIN chunks ch ON ch.version_id = v.id
                WHERE ch.chunk_type = 'text'""",
        ).fetchall()
    return [{"arxiv_id": r["arxiv_id"], "title": r["title"]} for r in rows]


def _usable(hit) -> bool:
    if hit.kind not in ALLOWED_KINDS:
        return False
    text = (hit.text or "").strip()
    if len(text) < MIN_CHUNK_CHARS:
        return False
    # 模板回填文案的特征：以"该图表"开头。真正的表格文字不会是这种句式。
    if text.startswith("该图表"):
        return False
    return True


def _dedup_key(hit) -> str:
    """同一段文本可能以不同 point 出现（重入库未删旧点），按"论文+页+正文前 60 字"归一。"""
    head = re.sub(r"\s+", " ", (hit.text or "").strip())[:60]
    return f"{hit.arxiv_id}|{hit.page}|{head}"


def build_pool(
    topic: str,
    *,
    arxiv_ids: list[str] | None = None,
    per_paper: int | None = None,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    """
    返回 {"chunks": [...], "catalog": [...], "queries": [...], "notes": [...]}。

    chunk 里除了检索字段，还带 `hit_source`（theme/angle:xxx/paper:xxx），
    用于解释"这条证据是怎么被找出来的"——评审问"为什么会有反例"时能答得出来。
    """
    per_paper = per_paper or int(settings.review_pool_per_paper)
    max_chunks = max_chunks or int(settings.review_pool_max_chunks)
    retrieval = RetrievalService()
    notes: list[str] = []

    catalog = _catalog()
    if arxiv_ids:
        wanted = set(arxiv_ids)
        catalog = [p for p in catalog if p["arxiv_id"] in wanted]
        notes.append(f"按指定论文限定范围：{len(catalog)} 篇")

    queries: list[str] = [topic]
    n_angles = max(0, int(settings.review_subqueries))
    for angle in ANGLES[:n_angles]:
        queries.append(f"{topic} {angle}")

    pool: dict[str, dict[str, Any]] = {}

    def absorb(hits, source: str) -> None:
        for h in hits:
            if not _usable(h):
                continue
            key = _dedup_key(h)
            if key in pool:
                pool[key]["hit_source"] += f",{source}"
                continue
            item = h.model_dump()
            item["hit_source"] = source
            pool[key] = item

    # ① 主题 + ② 视角
    ask = max(8, per_paper * RETRIEVE_MULTIPLIER)
    for i, q in enumerate(queries):
        source = "theme" if i == 0 else f"angle:{ANGLES[i - 1]}"
        absorb(retrieval.search(q, top_k=ask)["fusion"], source)

    # ③ 逐论文补检：这条才是"覆盖"的来源，但**只补主题已经命中的论文**。
    #
    # 为什么不全库补检：那样会把"和主题无关的论文"也塞进证据池。
    # 实测过——主题「Transformer 架构的能力边界」全库补检后，孤子分子那篇论文
    # 也进了池子，抽出一条「NPR 效应会影响腔体传输」的结论，语气正式、引文真实，
    # 放进综述里毫无违和感，但它是彻底跑题的。补检的目的是**补齐已被判定相关的
    # 论文的覆盖度**，不是把整个库拉进来。相关性由主题/视角检索来判，
    # 补检只负责"这篇论文还有没有别的段落该被看到"。
    touched = {ch["arxiv_id"] for ch in pool.values()}
    explicit = set(arxiv_ids or [])
    supplement = [p for p in catalog if p["arxiv_id"] in (touched | explicit)]
    skipped = [p["arxiv_id"] for p in catalog if p["arxiv_id"] not in (touched | explicit)]
    if skipped:
        notes.append(f"主题未命中、未做补检：{', '.join(skipped)}")

    for p in supplement:
        try:
            hits = retrieval.search(
                topic, top_k=ask, filters={"arxiv_id": p["arxiv_id"]}
            )["fusion"]
        except Exception:  # 单篇补检失败不该毁掉整个综述
            notes.append(f"补检失败：{p['arxiv_id']}")
            continue
        absorb(hits, f"paper:{p['arxiv_id']}")

    chunks = list(pool.values())
    # 每篇的配额：先保证"每篇都有"，再按相关度补。否则高相关论文会把池子占满。
    by_paper: dict[str, list[dict[str, Any]]] = {}
    for ch in chunks:
        by_paper.setdefault(ch["arxiv_id"], []).append(ch)
    for items in by_paper.values():
        items.sort(key=lambda x: -float(x.get("score") or 0.0))

    balanced: list[dict[str, Any]] = []
    for depth in range(per_paper):
        for pid in sorted(by_paper):
            items = by_paper[pid]
            if depth < len(items):
                balanced.append(items[depth])
    selected = balanced[:max_chunks]

    covered = {c["arxiv_id"] for c in selected}
    missing = [p["arxiv_id"] for p in supplement if p["arxiv_id"] not in covered]
    if missing:
        notes.append(f"已命中但未进入证据池的论文：{', '.join(missing)}")

    return {"chunks": selected, "catalog": catalog, "queries": queries, "notes": notes}


def numbered_context(chunks: list[dict[str, Any]]) -> str:
    """把证据池编号成 `[1] 标题 (arXiv, p.N, 章节)` + 正文，供抽 claim 用。"""
    parts = []
    for i, ch in enumerate(chunks, 1):
        parts.append(
            f"[{i}] {ch['title']} (arXiv:{ch['arxiv_id']}, 第 {ch['page']} 页, "
            f"章节 {ch.get('section') or '-'}, 类型 {ch.get('kind')})\n{(ch.get('text') or '').strip()}"
        )
    return "\n\n".join(parts)
