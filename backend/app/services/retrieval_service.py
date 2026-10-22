"""
检索层：dense(bge-m3) + sparse(BM25) 双路召回，RRF 融合排序。
关键设计决策：dense 与 sparse 两路分别检索，使用无参数的 RRF（k=60）
融合排序，避免学习权重，保证结果稳定、可复现、可解释。
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from qdrant_client.models import ScoredPoint
from app.services.vector_store import VectorStoreService


class RetrievalResult(BaseModel):
    """单条检索结果，带完整溯源元数据。"""
    chunk_id: str
    paper_id: str
    arxiv_id: str
    version: str
    title: str
    kind: str
    text: str
    section: str
    page: int
    paragraph: int = 0
    score: float
    rank_dense: int = -1
    rank_sparse: int = -1
    bbox: Optional[List[float]] = None
    latex: Optional[str] = None
    caption: Optional[str] = None
    description: Optional[str] = None
    doi: Optional[str] = None
    year: Optional[int] = None
    authors: Optional[List[str]] = None


class RetrievalService:
    """
    关键设计决策：
    - dense 负责语义匹配，sparse 负责字面/关键词匹配；
    - RRF 只依赖排序位置，不需要训练或调参，k=60 是经典默认值；
    - 所有返回结果携带 page/paragraph/bbox/kind，支持前端高亮与溯源。
    """

    RRF_K = 60

    def __init__(self):
        self.vector_store = VectorStoreService()

    def dense_search(
        self, query: str, top_k: int = 10, filters: Dict[str, Any] | None = None
    ) -> List[RetrievalResult]:
        """dense 单路召回。"""
        dense_vec = self.vector_store.embedder.embed_dense([query])[0]
        hits = self.vector_store.client.query_points(
            collection_name=self.vector_store.chunk_collection,
            query=dense_vec,
            using=self.vector_store.DENSE_VECTOR_NAME,
            limit=top_k,
            with_payload=True,
            query_filter=self._build_filter(filters),
        ).points
        return [self._hit_to_result(hit, rank=idx + 1, rank_field="rank_dense") for idx, hit in enumerate(hits)]

    def sparse_search(
        self, query: str, top_k: int = 10, filters: Dict[str, Any] | None = None
    ) -> List[RetrievalResult]:
        """sparse（BM25）单路召回。"""
        sparse_vec = self.vector_store.embedder.embed_sparse([query])[0]
        hits = self.vector_store.client.query_points(
            collection_name=self.vector_store.chunk_collection,
            query=sparse_vec,
            using=self.vector_store.SPARSE_VECTOR_NAME,
            limit=top_k,
            with_payload=True,
            query_filter=self._build_filter(filters),
        ).points
        return [self._hit_to_result(hit, rank=idx + 1, rank_field="rank_sparse") for idx, hit in enumerate(hits)]

    def search(
        self,
        query: str,
        top_k: int = 10,
        rrf_k: int = RRF_K,
        filters: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        双路召回 + RRF 融合。
        返回：{"dense": [...], "sparse": [...], "fusion": [...]}
        """
        dense_hits = self.dense_search(query, top_k=top_k * 2, filters=filters)
        sparse_hits = self.sparse_search(query, top_k=top_k * 2, filters=filters)
        fusion = self._rrf_fusion(dense_hits, sparse_hits, top_k=top_k, rrf_k=rrf_k)
        return {
            "query": query,
            "rrf_k": rrf_k,
            "dense": dense_hits[:top_k],
            "sparse": sparse_hits[:top_k],
            "fusion": fusion,
        }

    @classmethod
    def _rrf_fusion(
        cls,
        dense_results: List[RetrievalResult],
        sparse_results: List[RetrievalResult],
        top_k: int,
        rrf_k: int,
    ) -> List[RetrievalResult]:
        """
        Reciprocal Rank Fusion：score = Σ 1/(k + rank)。
        结果按 score 降序排列，稳定可复现。
        """
        scores: Dict[str, float] = {}
        results: Dict[str, RetrievalResult] = {}

        for r in dense_results:
            key = cls._result_key(r)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + r.rank_dense)
            results[key] = r

        for r in sparse_results:
            key = cls._result_key(r)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + r.rank_sparse)
            results[key] = r

        sorted_keys = sorted(scores, key=scores.get, reverse=True)[:top_k]
        for key in sorted_keys:
            results[key].score = scores[key]
        return [results[key] for key in sorted_keys]

    @staticmethod
    def _result_key(r: RetrievalResult) -> str:
        """用 chunk_id 作为唯一键。"""
        return r.chunk_id

    def _hit_to_result(
        self, hit: ScoredPoint, rank: int, rank_field: str
    ) -> RetrievalResult:
        p = hit.payload or {}
        data = {
            "chunk_id": str(hit.id),
            "paper_id": p.get("paper_id", ""),
            "arxiv_id": p.get("arxiv_id", ""),
            "version": p.get("version", "v1"),
            "title": p.get("title", ""),
            "kind": p.get("kind", "paragraph"),
            "text": p.get("text", ""),
            "section": p.get("section", ""),
            "page": p.get("page", 0),
            "paragraph": p.get("paragraph", 0),
            "score": float(hit.score),
            "bbox": p.get("bbox"),
            "latex": p.get("latex"),
            "caption": p.get("caption"),
            "description": p.get("description"),
            "doi": p.get("doi"),
            "year": p.get("year"),
            "authors": p.get("authors"),
        }
        data[rank_field] = rank
        return RetrievalResult(**data)

    @staticmethod
    def _build_filter(filters: Dict[str, Any] | None) -> Any:
        """简单 filter 构造：目前支持 kind 过滤。"""
        if not filters:
            return None
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        must = []
        for key, value in filters.items():
            must.append(FieldCondition(key=key, match=MatchValue(value=value)))
        return Filter(must=must) if must else None
