"""
检索层：dense(e5-large) + sparse(BM25) 双路召回，RRF 融合排序。
关键设计决策：dense 与 sparse 两路分别检索，使用无参数的 RRF（k=60）
融合排序，避免学习权重，保证结果稳定、可复现、可解释。
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from qdrant_client.models import ScoredPoint
from app.repositories import ChunkRepository
from app.services.vector_store import VectorStoreService


class RetrievalResult(BaseModel):
    """单条检索结果，带完整溯源元数据。"""
    chunk_id: str
    # Qdrant 点 ID。保留它是为了排查"检索到了但库里查不到"这类问题：
    # chunk_id 是换过的，point_id 才是向量库里那一行。
    point_id: Optional[str] = None
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
    # 归一化之前的原始融合分（RRF 量级 ~1/60）。
    # score 会被策略归一化并叠加加成，用于排序与展示；
    # 但"这次检索到底靠不靠谱"必须看绝对量——归一化之后最大值永远是 1.0，
    # 拿它做拒答判断会永远判"靠谱"。所以原始分单独留一份。
    raw_score: Optional[float] = None
    # 精排分（cross-encoder，min-max 归一化到 0~1）。
    # 和 score 分开存：score 可能是"融合归一化 + 策略加成"的结果，
    # 而 rerank_score 只反映"这条片段对这个问题有多相关"。
    # A/B 对比和"精排到底改了什么"都要看它。
    rerank_score: Optional[float] = None
    # 这条结果是怎么来的：fusion（双路融合）/ rule:xxx（数值规则召回）/ nav_literal / rerank_pad。
    # 前端与评测用它可以区分"精排把它提上来的"和"它本来就是第一"。
    hit_source: Optional[str] = None
    rank_dense: int = -1
    rank_sparse: int = -1
    bbox: Optional[List[float]] = None
    latex: Optional[str] = None
    caption: Optional[str] = None
    description: Optional[str] = None
    # nav 策略命中字面时给出的一段上下文（含命中位置前后若干字符），
    # 前端可以直接高亮，不用自己再去文本里找。
    snippet: Optional[str] = None
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

    # 可用的召回通道。`hybrid` 是两者的并集，也是线上的唯一配置。
    CHANNELS = ("dense", "sparse", "hybrid")

    def __init__(self, channels: str = "hybrid"):
        self.vector_store = VectorStoreService()
        self._channels = "hybrid"
        self.channels = channels

    @property
    def channels(self) -> str:
        return self._channels

    @channels.setter
    def channels(self, value: str) -> None:
        """
        只影响"这一路要不要发查询"，不影响两侧的权重与融合公式。

        存在的理由只有一个：评测要能把"纯向量 / 纯 BM25 / 混合"作为**同一份代码的
        三种配置**来跑。如果基线是另写一套检索，那比出来的差异里就混进了实现差异，
        证明不了任何事。生产恒为 hybrid，改这个值等价于关掉一路召回。
        """
        v = (value or "hybrid").strip().lower()
        if v not in self.CHANNELS:
            raise ValueError(f"channels 只能是 {self.CHANNELS}，收到 {value!r}")
        self._channels = v

    def channel_on(self, name: str) -> bool:
        return self._channels == "hybrid" or self._channels == name

    @property
    def active_channels(self) -> list[str]:
        return [c for c in ("dense", "sparse") if self.channel_on(c)]

    def dense_search(
        self,
        query: str,
        top_k: int = 10,
        filters: Dict[str, Any] | None = None,
        query_vec: List[float] | None = None,
    ) -> List[RetrievalResult]:
        """
        dense 单路召回。

        query_vec 允许调用方把已经算好的查询向量传进来：
        对比策略要对每篇论文各发一次查询、分类器可能也已经编码过一次，
        每次都重新 embed 一遍是纯浪费（一次 ~20ms，四次就是 80ms）。

        通道关闭时返回空列表而不是报错：融合层对"缺一路"是免疫的
        （`_rrf_merge` 会跳过 rank<=0 的项），空列表正好表达"这一路没投票"。
        """
        if not self.channel_on("dense"):
            return []
        dense_vec = query_vec if query_vec is not None else self.vector_store.embedder.embed_dense([query])[0]
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
        if not self.channel_on("sparse"):
            return []
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
        # 点 ID 与 chunk 行 ID 是两套 ID：锚点/引用/provenance 都挂在 chunks.id 上，
        # 所以这里必须换一次，否则后续按 chunk_id 查锚点会全部落空。
        point_id = str(hit.id)
        chunk_id = ChunkRepository.resolve_point_ids([point_id]).get(point_id, point_id)
        data = {
            "chunk_id": chunk_id,
            "point_id": point_id,
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
