"""
引用图谱服务：以单篇论文为中心，基于 OpenAlex 构建引用网络，
计算“引用含金量”并剔除应付式引用。
"""
import math
from typing import List, Dict, Any
from collections import deque
from app.services.openalex_client import OpenAlexClient
from app.services.vector_store import VectorStoreService


class GraphService:
    """
    关键设计决策：
    - 图数据实时从 OpenAlex 拉取（无 key、不限流），不单独维护图数据库；
    - “引用含金量”使用对数化 cited_by_count 归一化，低于阈值（0.1）的边视为应付式引用并过滤；
    - 每个节点携带 source_url，可点跳原文（arXiv PDF 或 MinIO 原稿）。
    """

    # 引用含金量阈值：低于此权重的边被标记为“应付式引用”
    INFLUENCE_THRESHOLD = 0.1

    def __init__(self):
        self.openalex = OpenAlexClient()
        self.vector_store = VectorStoreService()

    def build_graph(
        self,
        seed_arxiv_id: str,
        depth: int = 1,
        filter_perfunctory: bool = True,
    ) -> Dict[str, Any]:
        """构建引用图谱。优先用 OpenAlex（无 key、不限流），S2 作为补充。"""
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        visited = set()
        queue = deque([(seed_arxiv_id, 0)])

        while queue:
            arxiv_id, d = queue.popleft()
            if arxiv_id in visited:
                continue
            # 种子（d=0）始终处理；子节点只在深度未超限时展开
            if d > 0 and d >= depth:
                continue
            visited.add(arxiv_id)

            # 一次 fetch_citation_graph 同时拿到种子元数据 + 引用列表，
            # 避免重复请求（fetch 内部已调用 get_work_by_arxiv）
            graph = self.openalex.fetch_citation_graph(arxiv_id, limit=30)
            seed_work = graph["seed"]
            citations = graph["citations"]

            # 种子节点
            seed_title = (seed_work or {}).get("title") or arxiv_id
            seed_inf = (seed_work or {}).get("cited_by_count", 0)
            nodes[arxiv_id] = {
                "id": arxiv_id,
                "label": arxiv_id,
                "arxiv_id": arxiv_id,
                "title": seed_title,
                "influential_citation_count": seed_inf,
                "citation_count": seed_inf,
                "influence_score": round(math.log1p(seed_inf), 4),
                "source_url": self._source_url(arxiv_id),
                "group": "seed" if d == 0 else f"depth-{d}",
            }

            if d < depth:
                # 归一化：用引用列表里最大的 cited_by_count 作为分母
                max_inf = max([c.get("cited_by_count", 0) for c in citations] or [1])
                for c in citations:
                    child_arxiv = c.get("arxiv_id")
                    if not child_arxiv:
                        continue
                    child_inf = c.get("cited_by_count", 0)
                    edge_weight = (
                        round(math.log1p(child_inf) / math.log1p(max(max_inf, 1)), 4)
                        if max_inf > 0 else 0.0
                    )
                    is_perfunctory = edge_weight < self.INFLUENCE_THRESHOLD
                    if filter_perfunctory and is_perfunctory:
                        continue
                    child_id = child_arxiv
                    edges.append({
                        "source": arxiv_id,
                        "target": child_id,
                        "relation": "cited_by",
                        "weight": edge_weight,
                        "is_perfunctory": is_perfunctory,
                    })
                    nodes.setdefault(child_id, {
                        "id": child_id,
                        "label": child_arxiv,
                        "arxiv_id": child_arxiv,
                        "title": c.get("title", "") or child_arxiv,
                        "influential_citation_count": child_inf,
                        "citation_count": child_inf,
                        "influence_score": round(math.log1p(child_inf), 4),
                        "source_url": self._source_url(child_arxiv),
                        "group": f"depth-{d+1}",
                    })
                    queue.append((child_arxiv, d + 1))

        return {"nodes": list(nodes.values()), "edges": edges}

    def get_high_impact_citations(self, arxiv_id: str, top_n: int = 10) -> List[Dict[str, Any]]:
        """返回对本文影响力最高的 top_n 引用论文（改用 OpenAlex，无 key、不限流）。"""
        citations = self.openalex.fetch_citation_graph(arxiv_id, limit=50)["citations"]
        scored = []
        for c in citations:
            child_arxiv = c.get("arxiv_id")
            if not child_arxiv:
                continue
            scored.append({
                "arxiv_id": child_arxiv,
                "title": c.get("title", ""),
                "influential_citation_count": c.get("cited_by_count", 0),
                "weight": c.get("cited_by_count", 0),
                "source_url": self._source_url(child_arxiv),
            })
        scored.sort(key=lambda x: x["influential_citation_count"], reverse=True)
        return scored[:top_n]

    def _source_url(self, arxiv_id: str) -> str:
        """优先返回 MinIO 原稿 presigned URL，否则返回 arXiv PDF。"""
        meta = self.vector_store.get_paper_meta(arxiv_id, "v1")
        if meta and meta.get("pdf_s3_key"):
            url = self.vector_store.storage.get_presigned_url(
                self.vector_store.storage.papers_bucket,
                meta["pdf_s3_key"],
                expires=3600,
            )
            if url:
                return url
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"
