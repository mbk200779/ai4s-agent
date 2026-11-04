"""
OpenAlex 数据接入：补全引用关系与替代指标。
"""
from typing import List, Dict, Any, Optional
import re
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
        """通过 arXiv ID 查询 OpenAlex work。
        OpenAlex 的 arXiv 外部 ID 需用 DOI 形式（10.48550/arXiv.<id>）查询，
        直接 arXiv:<id> 会 404，故做归一化。"""
        clean = arxiv_id.strip().split("v")[0].replace("arXiv:", "")
        url = f"{self.BASE_URL}/works/https://doi.org/10.48550/arXiv.{clean}"
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
    def _extract_arxiv_id(data: Dict[str, Any]) -> Optional[str]:
        """从 OpenAlex work 的 locations 里正则提取 arXiv ID。

        OpenAlex 的 `ids` 字段里没有 arxiv，arxiv 信息藏在 `locations` 的
        `landing_page_url`（http://arxiv.org/abs/2103.14030）或
        `pdf_url`（https://arxiv.org/pdf/2103.14030）中，需正则匹配。
        """
        pattern = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)
        for loc in data.get("locations") or []:
            for key in ("landing_page_url", "pdf_url"):
                url = loc.get(key) or ""
                m = pattern.search(url)
                if m:
                    return m.group(1)
        return None

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
            "cited_by_api_url": data.get("cited_by_api_url"),
            "concepts": [c.get("display_name") for c in data.get("concepts", [])],
            "referenced_works": data.get("referenced_works", []),
            "related_works": data.get("related_works", []),
            "arxiv_id": OpenAlexClient._extract_arxiv_id(data),
        }

    def fetch_citation_graph(self, arxiv_id: str, limit: int = 30) -> Dict[str, Any]:
        """
        以 OpenAlex 构建引用子图（替代 S2，无 key、无严格限流）。
        返回 {seed, citations: [work...]}，citations 为「引用本论文」的 work，
        每项带 arxiv_id/title/cited_by_count/influential 等价指标。
        """
        work = self.get_work_by_arxiv(arxiv_id)
        if not work:
            return {"seed": None, "citations": []}
        citations = []
        openalex_id = work.get("openalex_id")
        if openalex_id:
            try:
                # 用 filter=cites:{id} 拿「引用本论文」的 work（cited_by_api_url 在详情里为 None）
                cpage = self._get(
                    f"{self.BASE_URL}/works",
                    params={"filter": f"cites:{openalex_id}", "per-page": limit},
                )
                for item in cpage.get("results", []):
                    citations.append({
                        "arxiv_id": self._extract_arxiv_id(item),
                        "title": item.get("display_name") or "",
                        "cited_by_count": item.get("cited_by_count", 0),
                        "publication_year": item.get("publication_year"),
                    })
            except Exception:
                pass
        return {"seed": work, "citations": citations}

    def fetch_related_arxiv_ids(self, openalex_id: str, relation: str = "cited_by") -> List[str]:
        """
        获取引用/被引用的 arXiv ID 列表。
        relation: 'cited_by' | 'references'
        """
        url = openalex_id
        data = self._get(url)
        if relation == "cited_by":
            # 单 work 详情的 cited_by_api_url 为 None，改用 filter=cites:{id} 查询
            page = self._get(
                f"{self.BASE_URL}/works",
                params={"filter": f"cites:{openalex_id}", "per-page": 50},
            )
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
            arxiv = self._extract_arxiv_id(item)
            if arxiv:
                arxiv_ids.append(arxiv)
        return arxiv_ids
