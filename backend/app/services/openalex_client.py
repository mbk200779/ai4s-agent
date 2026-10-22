"""
OpenAlex 数据接入：补全引用关系与替代指标。
"""
from typing import List, Dict, Any, Optional
import requests


class OpenAlexClient:
    """
    关键设计决策：OpenAlex 作为 Semantic Scholar 的互补来源，
    当 S2 限流或缺失时提供引用网络 backup，不阻塞主入库流程。
    """

    BASE_URL = "https://api.openalex.org"
    EMAIL = "ai4s-agent@example.com"  #  polite pool

    def _get(self, url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """带邮箱的 polite pool 请求。"""
        headers = {"User-Agent": f"mailto:{self.EMAIL}"}
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_work_by_arxiv(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """通过 arXiv ID 查询 OpenAlex work。"""
        url = f"{self.BASE_URL}/works/arXiv:{arxiv_id}"
        try:
            data = self._get(url)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        return self._parse_work(data)

    def get_work_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        """通过 DOI 查询。"""
        if not doi:
            return None
        url = f"{self.BASE_URL}/works/{doi}"
        try:
            data = self._get(url)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        return self._parse_work(data)

    @staticmethod
    def _parse_work(data: Dict[str, Any]) -> Dict[str, Any]:
        """提取引用关系与指标。"""
        ids = data.get("ids", {})
        return {
            "openalex_id": data.get("id"),
            "doi": data.get("doi"),
            "title": data.get("display_name"),
            "publication_year": data.get("publication_year"),
            "cited_by_count": data.get("cited_by_count", 0),
            "concepts": [c.get("display_name") for c in data.get("concepts", [])],
            "referenced_works": data.get("referenced_works", []),
            "related_works": data.get("related_works", []),
            "arxiv_id": ids.get("arxiv") if ids else None,
        }

    def fetch_related_arxiv_ids(self, openalex_id: str, relation: str = "cited_by") -> List[str]:
        """
        获取引用/被引用的 arXiv ID 列表。
        relation: 'cited_by' | 'references'
        """
        url = openalex_id
        data = self._get(url)
        if relation == "cited_by":
            # OpenAlex 分页拿到所有引用本工作的 work（这里只取首页）
            citations_url = data.get("cited_by_api_url")
            if not citations_url:
                return []
            page = self._get(citations_url, params={"per-page": 50})
        else:
            referenced = data.get("referenced_works", [])
            if not referenced:
                return []
            # 批量查询（简单实现：取前 25 个）
            page = {"results": []}
            for rid in referenced[:25]:
                try:
                    page["results"].append(self._get(rid))
                except Exception:
                    continue

        results = page.get("results", [])
        arxiv_ids = []
        for item in results:
            ids = item.get("ids", {})
            arxiv = ids.get("arxiv")
            if arxiv:
                arxiv_ids.append(arxiv)
        return arxiv_ids
