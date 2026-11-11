"""
arXiv 数据接入：支持单篇查询（arxiv 库）与 OAI-PMH 批量追新（sickle）。
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import requests
import arxiv
from sickle import Sickle
from app.core.config import settings


class ArxivClient:
    """
    关键设计决策：单篇用 arxiv 官方库保证字段完整；追新用 OAI-PMH（sickle）
    支持按时间窗口增量拉取，避免频繁轮询 arxiv.org API。
    """

    OAI_URL = "http://export.arxiv.org/oai2"

    # arxiv 库把查询端点写死成 http（Client.query_url_format），而 export.arxiv.org
    # 现在对 http 一律回 301 跳 https；feedparser 不跟跳转，库内部重试三次后抛
    # HTTPError: Page request resulted in HTTP 301，导入接口直接 500。
    # 这里把实例上的端点改成 https，绕开那层跳转。
    QUERY_URL_FORMAT = "https://export.arxiv.org/api/query?{}"

    def __init__(self):
        self.client = arxiv.Client()
        self.client.query_url_format = self.QUERY_URL_FORMAT
        self.sickle = Sickle(self.OAI_URL)

    @staticmethod
    def _normalize_id(raw_id: str) -> str:
        """移除版本号与多余空格，返回纯 arxiv_id。"""
        return raw_id.strip().split("v")[0]

    @staticmethod
    def _split_version(short_id: str) -> tuple[str, str]:
        """
        把 1810.04805v2 拆成 ("1810.04805", "v2")。

        新版 arxiv 库去掉了 Result.version，版本号只留在 get_short_id() 里，
        所以两个取元数据的方法都得走这里拆，别再各写一份。
        """
        head, sep, tail = short_id.rpartition("v")
        if sep and tail.isdigit():
            return head, f"v{tail}"
        return short_id, "v1"

    def fetch_by_id(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """通过 arXiv ID 获取论文元数据。"""
        arxiv_id = self._normalize_id(arxiv_id)
        search = arxiv.Search(id_list=[arxiv_id])
        results = list(self.client.results(search))
        if not results:
            return None
        r = results[0]
        _, version = self._split_version(r.get_short_id())
        return {
            "arxiv_id": arxiv_id,
            "version": version,
            "title": r.title,
            "authors": [str(a) for a in r.authors],
            "abstract": r.summary,
            "pdf_url": r.pdf_url,
            "published": r.published.isoformat() if r.published else None,
            "primary_category": r.primary_category,
            "categories": r.categories,
            "doi": r.doi,
        }

    def download_pdf(self, pdf_url: str) -> bytes:
        """下载 PDF 字节。"""
        resp = requests.get(pdf_url, timeout=120)
        resp.raise_for_status()
        return resp.content

    def fetch_updates_oai(
        self,
        category: str = "cs.AI",
        from_date: datetime | None = None,
        until_date: datetime | None = None,
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        使用 OAI-PMH 按分类增量追新。
        验收标准：
        1. 支持 from/until 时间窗口；
        2. 返回 arxiv_id、version、标题、摘要、PDF 链接；
        3. 可限制返回数量。
        """
        if until_date is None:
            until_date = datetime.utcnow()
        if from_date is None:
            from_date = until_date - timedelta(days=1)

        # OAI-PMH set 命名规则：{category}
        records = self.sickle.ListRecords(
            metadataPrefix="arXiv",
            set=category,
            from_date=from_date.strftime("%Y-%m-%d"),
            until_date=until_date.strftime("%Y-%m-%d"),
        )

        papers: List[Dict[str, Any]] = []
        for record in records:
            meta = record.metadata
            # 取第一个标识符作为 arxiv_id
            ids = meta.get("identifier", [])
            if not ids:
                continue
            raw_id = ids[0]
            arxiv_id = self._normalize_id(raw_id)
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            papers.append({
                "arxiv_id": arxiv_id,
                "version": "v1",  # OAI 通常不带版本，默认 v1；入库时会自动以 arxiv_id+version 去重
                "title": " ".join(meta.get("title", [])),
                "authors": meta.get("creator", []),
                "abstract": " ".join(meta.get("abstract", [])),
                "pdf_url": pdf_url,
                "published": record.header.datestamp,
                "primary_category": category,
                "categories": meta.get("categories", []),
                "doi": None,
            })
            if len(papers) >= max_results:
                break
        return papers

    def search_updates(
        self, category: str = "cs.AI", max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """兼容性方法：使用 arxiv 库搜索最新论文。"""
        search = arxiv.Search(
            query=f"cat:{category}",
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
            max_results=max_results,
        )
        return [
            {
                "arxiv_id": self._normalize_id(r.get_short_id()),
                "version": self._split_version(r.get_short_id())[1],
                "title": r.title,
                "authors": [str(a) for a in r.authors],
                "abstract": r.summary,
                "pdf_url": r.pdf_url,
                "published": r.published.isoformat() if r.published else None,
                "primary_category": r.primary_category,
                "categories": r.categories,
                "doi": r.doi,
            }
            for r in self.client.results(search)
        ]
