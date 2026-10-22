"""
Semantic Scholar 数据接入：获取引用关系与 influentialCitationCount。
"""
from typing import List, Dict, Any, Optional
import requests
from app.core.config import settings


class SemanticScholarClient:
    """
    关键设计决策：优先用 S2 拿 paperId、引用/被引、influentialCitationCount；
    无 API key 时也能访问，仅速率受限，失败时由调用方降级或跳过。
    """

    BASE_URL = "https://api.semanticscholar.org/graph/v1"

    def __init__(self):
        self.headers = {}
        if settings.s2_api_key:
            self.headers["x-api-key"] = settings.s2_api_key

    def _get(self, url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        resp = requests.get(url, params=params, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_paper_by_arxiv(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """通过 arXiv ID 查询论文详情。"""
        url = f"{self.BASE_URL}/paper/ARXIV:{arxiv_id}"
        params = {
            "fields": (
                "paperId,title,authors,year,abstract,"
                "influentialCitationCount,citationCount,referenceCount,externalIds"
            )
        }
        try:
            return self._get(url, params=params)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def get_citations(self, paper_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """获取引用该论文的 paper 列表。"""
        url = f"{self.BASE_URL}/paper/{paper_id}/citations"
        params = {
            "fields": "paperId,title,authors,year,influentialCitationCount,externalIds",
            "limit": limit,
        }
        try:
            data = self._get(url, params=params)
            return [item["citedPaper"] for item in data.get("data", [])]
        except Exception:
            return []

    def get_references(self, paper_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """获取该论文引用的 paper 列表。"""
        url = f"{self.BASE_URL}/paper/{paper_id}/references"
        params = {
            "fields": "paperId,title,authors,year,influentialCitationCount,externalIds",
            "limit": limit,
        }
        try:
            data = self._get(url, params=params)
            return [item["citedPaper"] for item in data.get("data", [])]
        except Exception:
            return []

    def fetch_citation_graph(self, arxiv_id: str, limit: int = 50) -> Dict[str, Any]:
        """
        获取完整引用子图：论文本身 + references + citations。
        返回结构化数据供入库。
        """
        paper = self.get_paper_by_arxiv(arxiv_id)
        if not paper:
            return {
                "paper_id": None,
                "influential_citation_count": 0,
                "citation_count": 0,
                "reference_count": 0,
                "references": [],
                "citations": [],
            }
        paper_id = paper.get("paperId")
        refs = self.get_references(paper_id, limit=limit) if paper_id else []
        cites = self.get_citations(paper_id, limit=limit) if paper_id else []
        return {
            "paper_id": paper_id,
            "influential_citation_count": paper.get("influentialCitationCount", 0),
            "citation_count": paper.get("citationCount", 0),
            "reference_count": paper.get("referenceCount", 0),
            "s2_title": paper.get("title"),
            "s2_abstract": paper.get("abstract"),
            "references": [
                {
                    "paper_id": r.get("paperId"),
                    "arxiv_id": (r.get("externalIds") or {}).get("ArXiv"),
                    "title": r.get("title"),
                    "year": r.get("year"),
                    "influential_citation_count": r.get("influentialCitationCount", 0),
                }
                for r in refs
            ],
            "citations": [
                {
                    "paper_id": c.get("paperId"),
                    "arxiv_id": (c.get("externalIds") or {}).get("ArXiv"),
                    "title": c.get("title"),
                    "year": c.get("year"),
                    "influential_citation_count": c.get("influentialCitationCount", 0),
                }
                for c in cites
            ],
        }
