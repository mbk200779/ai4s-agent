"""
Crossref 数据接入：补全 DOI、期刊、出版日期等元数据。
"""
from typing import Dict, Any, Optional
import requests


class CrossrefClient:
    """
    关键设计决策：Crossref 仅做元数据补全，失败时不阻塞主入库流程；
    使用 polite User-Agent 与邮箱，降低被限流概率。
    """

    BASE_URL = "https://api.crossref.org"
    EMAIL = "ai4s-agent@example.com"

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": f"AI4S-Agent/1.0 (mailto:{self.EMAIL})",
            "Accept": "application/json",
        }

    def get_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        """通过 DOI 查询 Crossref 记录。"""
        if not doi:
            return None
        url = f"{self.BASE_URL}/works/{doi}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return self._parse_message(resp.json().get("message", {}))
        except Exception:
            return None

    def search_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """通过标题模糊查询，取第一条。"""
        if not title:
            return None
        try:
            params = {"query.title": title, "rows": 1}
            resp = requests.get(f"{self.BASE_URL}/works", params=params, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            items = resp.json().get("message", {}).get("items", [])
            if not items:
                return None
            return self._parse_message(items[0])
        except Exception:
            return None

    @staticmethod
    def _parse_message(msg: Dict[str, Any]) -> Dict[str, Any]:
        """提取关键元数据。"""
        authors = []
        for a in msg.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            authors.append(f"{given} {family}".strip())
        return {
            "doi": msg.get("DOI"),
            "title": msg.get("title", [None])[0],
            "container_title": msg.get("container-title", [None])[0],
            "published_print": msg.get("published-print"),
            "published_online": msg.get("published-online"),
            "authors": authors,
            "references_count": msg.get("references-count"),
            "is_referenced_by_count": msg.get("is-referenced-by-count"),
        }
