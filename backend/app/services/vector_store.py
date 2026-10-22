"""
向量库存储服务：Qdrant named vectors（dense + sparse）+ RRF 融合检索 + 论文元数据/引用关系存储。
"""
import hashlib
from typing import List, Dict, Any, Optional
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    SparseVectorParams,
    SparseVector,
    PointStruct,
    ScoredPoint,
    Filter,
    FieldCondition,
    MatchValue,
)
from app.core.config import settings
from app.services.embedder import EmbedderService
from app.services.storage import StorageService


class VectorStoreService:
    """
    关键设计决策：
    - chunk 与论文元数据分两个 collection 存储：chunk 负责检索与溯源，paper_meta 负责
      去重、唯一键与引用关系（references/citations/influentialCitationCount）。
    - paper_meta 使用全文 dense embedding 作为向量，支持基于 bge-m3 相似度的正文去重。
    """

    DENSE_VECTOR_NAME = "dense"
    SPARSE_VECTOR_NAME = "sparse"
    DENSE_DIM = 1024

    def __init__(self):
        self.client = QdrantClient(url=settings.qdrant_url)
        self.chunk_collection = settings.qdrant_collection
        self.meta_collection = settings.qdrant_paper_meta_collection
        self.embedder = EmbedderService()
        self.storage = StorageService()
        self._ensure_collections()

    def _ensure_collections(self):
        """创建 chunk 与 paper_meta collection。"""
        # chunk collection：dense + sparse named vectors
        if not self.client.collection_exists(self.chunk_collection):
            self.client.create_collection(
                collection_name=self.chunk_collection,
                vectors_config={
                    self.DENSE_VECTOR_NAME: VectorParams(
                        size=self.DENSE_DIM, distance=Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    self.SPARSE_VECTOR_NAME: SparseVectorParams()
                },
            )

        # paper_meta collection：仅 dense，用于去重与元数据查询
        if not self.client.collection_exists(self.meta_collection):
            self.client.create_collection(
                collection_name=self.meta_collection,
                vectors_config={
                    self.DENSE_VECTOR_NAME: VectorParams(
                        size=self.DENSE_DIM, distance=Distance.COSINE
                    )
                },
            )

    @staticmethod
    def _chunk_point_id(arxiv_id: str, version: str, chunk_idx: int) -> str:
        raw = f"chunk::{arxiv_id}::{version}::{chunk_idx}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _meta_point_id(arxiv_id: str, version: str) -> str:
        raw = f"meta::{arxiv_id}::{version}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def index_fragments(
        self,
        arxiv_id: str,
        version: str,
        title: str,
        body_hash: str,
        fragments: List[Dict[str, Any]],
    ) -> int:
        """
        索引结构化片段（paragraph/figure/formula/reference/chapter）。
        每种 kind 携带 page/section 等元数据，支持统一 RRF 检索。
        """
        self.delete_chunks(arxiv_id, version)
        if not fragments:
            return 0

        texts = [f["text"] for f in fragments]
        dense_vectors = self.embedder.embed_dense(texts)
        sparse_vectors = self.embedder.embed_sparse(texts)

        points: List[PointStruct] = []
        for idx, frag in enumerate(fragments):
            payload = {
                "kind": frag.get("kind", "paragraph"),
                "paper_id": f"{arxiv_id}::{version}",
                "arxiv_id": arxiv_id,
                "version": version,
                "title": title,
                "body_hash": body_hash,
                "section": frag.get("section", ""),
                "page": frag.get("page", 0),
                "paragraph": frag.get("paragraph", 0),
                "text": frag["text"],
            }
            # 保留公式/图表/参考文献/段落专有字段
            for key in ("latex", "caption", "description", "raw", "authors", "year", "venue", "doi", "children", "bbox"):
                if key in frag:
                    payload[key] = frag[key]

            pid_raw = f"frag::{arxiv_id}::{version}::{frag.get('kind','p')}::{idx}"
            points.append(
                PointStruct(
                    id=hashlib.md5(pid_raw.encode("utf-8")).hexdigest(),
                    vector={
                        self.DENSE_VECTOR_NAME: dense_vectors[idx],
                        self.SPARSE_VECTOR_NAME: sparse_vectors[idx],
                    },
                    payload=payload,
                )
            )

        self.client.upsert(collection_name=self.chunk_collection, points=points)
        return len(points)

    # 兼容旧接口：paragraph chunks
    def index_chunks(
        self,
        arxiv_id: str,
        version: str,
        title: str,
        body_hash: str,
        chunks: List[Dict[str, Any]],
    ) -> int:
        """
        将论文 chunk 编码后写入 Qdrant；同名论文会先删除旧版本。
        返回写入的 chunk 数量。
        """
        self.delete_chunks(arxiv_id, version)
        if not chunks:
            return 0

        texts = [c["text"] for c in chunks]
        dense_vectors = self.embedder.embed_dense(texts)
        sparse_vectors = self.embedder.embed_sparse(texts)

        points: List[PointStruct] = []
        for idx, chunk in enumerate(chunks):
            payload = {
                "kind": "chunk",
                "paper_id": f"{arxiv_id}::{version}",
                "arxiv_id": arxiv_id,
                "version": version,
                "title": title,
                "body_hash": body_hash,
                "section": chunk.get("section", ""),
                "page": chunk.get("page", 0),
                "text": chunk["text"],
            }
            points.append(
                PointStruct(
                    id=self._chunk_point_id(arxiv_id, version, idx),
                    vector={
                        self.DENSE_VECTOR_NAME: dense_vectors[idx],
                        self.SPARSE_VECTOR_NAME: sparse_vectors[idx],
                    },
                    payload=payload,
                )
            )

        self.client.upsert(collection_name=self.chunk_collection, points=points)
        return len(points)

    def delete_chunks(self, arxiv_id: str, version: str):
        """删除指定论文的所有 chunk。"""
        self.client.delete(
            collection_name=self.chunk_collection,
            points_selector=Filter(
                must=[
                    FieldCondition(key="arxiv_id", match=MatchValue(value=arxiv_id)),
                    FieldCondition(key="version", match=MatchValue(value=version)),
                ]
            ),
        )

    def upsert_paper_meta(
        self,
        arxiv_id: str,
        version: str,
        title: str,
        authors: List[str],
        abstract: str,
        body_hash: str,
        pdf_s3_key: str,
        figure_keys: List[str],
        s2_graph: Dict[str, Any],
        openalex: Dict[str, Any] | None,
        crossref: Dict[str, Any] | None,
        chapter_tree: List[Dict[str, Any]] | None = None,
        structured_references: List[Dict[str, Any]] | None = None,
    ):
        """
        写入/更新论文元数据与引用关系到 paper_meta collection。
        向量使用全文（title + abstract + 正文前 4096 字符可在此处传入）的 dense embedding。
        """
        # 用于去重的全文语义向量：优先用 abstract，若为空则用 title
        semantic_text = abstract if abstract else title
        dense_vector = self.embedder.embed_dense([semantic_text])[0]

        payload = {
            "arxiv_id": arxiv_id,
            "version": version,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "body_hash": body_hash,
            "pdf_s3_key": pdf_s3_key,
            "figure_keys": figure_keys,
            "s2_paper_id": s2_graph.get("paper_id"),
            "influential_citation_count": s2_graph.get("influential_citation_count", 0),
            "citation_count": s2_graph.get("citation_count", 0),
            "reference_count": s2_graph.get("reference_count", 0),
            "references": s2_graph.get("references", []),
            "citations": s2_graph.get("citations", []),
            "openalex": openalex or {},
            "crossref": crossref or {},
            "chapter_tree": chapter_tree or [],
            "structured_references": structured_references or [],
        }

        self.client.upsert(
            collection_name=self.meta_collection,
            points=[
                PointStruct(
                    id=self._meta_point_id(arxiv_id, version),
                    vector={self.DENSE_VECTOR_NAME: dense_vector},
                    payload=payload,
                )
            ],
        )

    def get_paper_meta(self, arxiv_id: str, version: str) -> Optional[Dict[str, Any]]:
        """通过唯一键查询论文元数据。"""
        try:
            result = self.client.retrieve(
                collection_name=self.meta_collection,
                ids=[self._meta_point_id(arxiv_id, version)],
                with_payload=True,
                with_vectors=False,
            )
            if result:
                return result[0].payload
        except Exception:
            pass
        return None

    def get_paper_meta_by_arxiv_id(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """通过 arxiv_id 查找任意版本的论文元数据（返回第一个命中）。"""
        try:
            result, _ = self.client.scroll(
                collection_name=self.meta_collection,
                scroll_filter=Filter(
                    must=[FieldCondition(key="arxiv_id", match=MatchValue(value=arxiv_id))]
                ),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
            if result:
                return result[0].payload
        except Exception:
            pass
        return None

    def find_by_body_hash(self, body_hash: str) -> Optional[Dict[str, Any]]:
        """通过正文 hash 查找是否已有重复论文。"""
        result, _ = self.client.scroll(
            collection_name=self.meta_collection,
            scroll_filter=Filter(
                must=[FieldCondition(key="body_hash", match=MatchValue(value=body_hash))]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        return result[0].payload if result else None

    def find_similar_by_embedding(
        self, text: str, threshold: float = 0.96, top_k: int = 1
    ) -> Optional[Dict[str, Any]]:
        """
        基于正文 bge-m3 embedding 相似度查找重复论文。
        关键设计决策：在精确 hash 之外增加语义相似度去重，可捕获
        不同文件名、同内容、或 arxiv 小版本差异的重复。
        """
        dense_vector = self.embedder.embed_dense([text])[0]
        hits = self.client.query_points(
            collection_name=self.meta_collection,
            query=dense_vector,
            using=self.DENSE_VECTOR_NAME,
            limit=top_k,
            with_payload=True,
            score_threshold=threshold,
        ).points
        return hits[0].payload if hits else None

    def search_rrf(
        self, query: str, top_k: int = 10, rrf_k: int = 60
    ) -> List[Dict[str, Any]]:
        """
        在 chunk collection 上分别做 dense 与 sparse 检索，使用 RRF 融合排序。
        """
        dense_vec = self.embedder.embed_dense([query])[0]
        sparse_vec = self.embedder.embed_sparse([query])[0]

        dense_hits = self.client.query_points(
            collection_name=self.chunk_collection,
            query=dense_vec,
            using=self.DENSE_VECTOR_NAME,
            limit=top_k * 2,
            with_payload=True,
        ).points

        sparse_hits = self.client.query_points(
            collection_name=self.chunk_collection,
            query=sparse_vec,
            using=self.SPARSE_VECTOR_NAME,
            limit=top_k * 2,
            with_payload=True,
        ).points

        return self._rrf_fusion(dense_hits, sparse_hits, top_k, rrf_k)

    @staticmethod
    def _rrf_fusion(
        dense_hits: List[ScoredPoint],
        sparse_hits: List[ScoredPoint],
        top_k: int,
        rrf_k: int,
    ) -> List[Dict[str, Any]]:
        scores: Dict[str, float] = {}
        payloads: Dict[str, Dict[str, Any]] = {}

        for rank, hit in enumerate(dense_hits):
            pid = str(hit.id)
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (rrf_k + rank + 1)
            payloads.setdefault(pid, hit.payload)

        for rank, hit in enumerate(sparse_hits):
            pid = str(hit.id)
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (rrf_k + rank + 1)
            payloads.setdefault(pid, hit.payload)

        sorted_pids = sorted(scores, key=scores.get, reverse=True)[:top_k]
        return [
            {
                "paper_id": payloads[pid].get("paper_id", ""),
                "arxiv_id": payloads[pid].get("arxiv_id", ""),
                "title": payloads[pid].get("title", ""),
                "text": payloads[pid].get("text", ""),
                "section": payloads[pid].get("section", ""),
                "page": payloads[pid].get("page", 0),
                "score": scores[pid],
            }
            for pid in sorted_pids
        ]

    def list_papers(self, limit: int = 100) -> List[Dict[str, Any]]:
        """列出已索引论文的去重元数据列表。"""
        points, _ = self.client.scroll(
            collection_name=self.meta_collection,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [p.payload for p in points]
