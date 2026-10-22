"""
引用图谱服务：以单篇论文为中心，基于 S2/OpenAlex 构建引用网络，
计算“引用含金量”并剔除应付式引用。
"""
import math
from typing import List, Dict, Any, Optional
from collections import deque
from app.services.s2_client import SemanticScholarClient
from app.services.openalex_client import OpenAlexClient
from app.services.vector_store import VectorStoreService


class GraphService:
    """
    关键设计决策：
    - 图数据实时从 S2 拉取，OpenAlex 作为元数据补全，不单独维护图数据库；
    - “引用含金量”使用对数化 influentialCitationCount 归一化，低于阈值（0.1）的边视为应付式引用并过滤；
    - 每个节点携带 source_url，可点跳原文（arXiv PDF 或 MinIO 原稿）。
    """

    # 引用含金量阈值：低于此权重的边被标记为“应付式引用”
    INFLUENCE_THRESHOLD = 0.1

    def __init__(self):
        self.s2 = SemanticScholarClient()
        self.openalex = OpenAlexClient()
        self.vector_store = VectorStoreService()

    def build_graph(
        self,
        seed_arxiv_id: str,
        depth: int = 1,
        filter_perfunctory: bool = True,
    ) -> Dict[str, Any]:
        """BFS 构建引用图谱，计算引用含金量。"""
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        visited = set()
        queue = deque([(seed_arxiv_id, 0)])

        while queue:
            arxiv_id, d = queue.popleft()
            if arxiv_id in visited or d > depth:
                continue
            visited.add(arxiv_id)

            paper = self.s2.get_paper_by_arxiv(arxiv_id)
            if not paper:
                continue

            paper_id = paper.get("paperId") or arxiv_id
            influence = self._influence_score(paper)
            source_url = self._source_url(arxiv_id)

            nodes[paper_id] = {
                "id": paper_id,
                "label": arxiv_id,
                "arxiv_id": arxiv_id,
                "title": paper.get("title", ""),
                "influential_citation_count": paper.get("influentialCitationCount", 0),
                "citation_count": paper.get("citationCount", 0),
                "influence_score": influence,
                "source_url": source_url,
                "group": "seed" if d == 0 else f"depth-{d}",
            }

            if d < depth:
                citations = self.s2.get_citations(paper_id, limit=30)
                # 计算最大 influential count 用于归一化
                max_inf = max(
                    [c.get("influentialCitationCount", 0) for c in citations] or [1]
                )
                for c in citations:
                    ext = c.get("externalIds") or {}
                    child_arxiv = ext.get("ArXiv")
                    if not child_arxiv:
                        continue
                    child_id = c.get("paperId") or child_arxiv
                    edge_weight = self._edge_weight(c, max_inf)
                    is_perfunctory = edge_weight < self.INFLUENCE_THRESHOLD

                    if filter_perfunctory and is_perfunctory:
                        continue

                    edges.append({
                        "source": paper_id,
                        "target": child_id,
                        "relation": "cited_by",
                        "weight": edge_weight,
                        "is_perfunctory": is_perfunctory,
                    })
                    nodes.setdefault(child_id, {
                        "id": child_id,
                        "label": child_arxiv,
                        "arxiv_id": child_arxiv,
                        "title": c.get("title", ""),
                        "influential_citation_count": c.get("influentialCitationCount", 0),
                        "citation_count": 0,
                        "influence_score": self._influence_score(c),
                        "source_url": self._source_url(child_arxiv),
                        "group": f"depth-{d+1}",
                    })
                    queue.append((child_arxiv, d + 1))

        return {"nodes": list(nodes.values()), "edges": edges}

    def get_high_impact_citations(self, arxiv_id: str, top_n: int = 10) -> List[Dict[str, Any]]:
        """返回对本文影响力最高的 top_n 引用论文。"""
        paper = self.s2.get_paper_by_arxiv(arxiv_id)
        if not paper:
            return []
        citations = self.s2.get_citations(paper.get("paperId"), limit=100)
        max_inf = max([c.get("influentialCitationCount", 0) for c in citations] or [1])
        scored = []
        for c in citations:
            ext = c.get("externalIds") or {}
            child_arxiv = ext.get("ArXiv")
            if not child_arxiv:
                continue
            weight = self._edge_weight(c, max_inf)
            scored.append({
                "arxiv_id": child_arxiv,
                "title": c.get("title", ""),
                "influential_citation_count": c.get("influentialCitationCount", 0),
                "weight": weight,
                "source_url": self._source_url(child_arxiv),
            })
        scored.sort(key=lambda x: x["weight"], reverse=True)
        return scored[:top_n]

    @staticmethod
    def _influence_score(paper: Dict[str, Any]) -> float:
        """节点影响力：对数化 influentialCitationCount。"""
        inf = paper.get("influentialCitationCount", 0)
        return round(math.log1p(inf), 4)

    @staticmethod
    def _edge_weight(citation: Dict[str, Any], max_inf: int) -> float:
        """边引用含金量：用被引论文的 influentialCitationCount 归一化。"""
        if max_inf <= 0:
            return 0.0
        inf = citation.get("influentialCitationCount", 0)
        return round(math.log1p(inf) / math.log1p(max(max_inf, 1)), 4)

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
