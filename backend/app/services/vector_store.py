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
    - paper_meta 使用全文 dense embedding 作为向量，支持基于 e5-large 相似度的正文去重。
    - **chunk collection 是按索引版本分的**（见 services/index_version.py）：
      一个 pipeline 签名一个 collection，切版本 = 换指针，不搬数据。
      元数据 collection 不版本化——它不参与检索排序，换 embedding 模型只影响
      "判重像不像"，重算成本是每篇一次，没有必要跟着切。
    """

    DENSE_VECTOR_NAME = "dense"
    SPARSE_VECTOR_NAME = "sparse"
    DENSE_DIM = 1024

    def __init__(self, collection: str | None = None, pipeline_version: str | None = None):
        self.client = QdrantClient(url=settings.qdrant_url)
        self.meta_collection = settings.qdrant_paper_meta_collection
        self.storage = StorageService()
        self._embedder: EmbedderService | None = None
        # 显式传 collection 时不动路由：重建任务要写"目标版本"，
        # 而那时候路由还指着旧版本。两者必须能各说各的。
        self.chunk_collection = collection or ""
        self.pipeline_version = pipeline_version or ""
        # collection 是**这一次请求的上下文**，绑在实例上而不是散在 12 个调用点。
        # 为什么不把 collection 作为参数一路透传：5 条策略、精排、语料加载、
        # 数值规则都要用它，每加一个调用点就得记得传一次；漏传一次的表现是
        # "检索到了旧版本的片段"，而不会报错——这种错最难发现。
        # 每个请求各自构造本服务（没有进程级单例），所以实例属性不会串。
        self._ensure_collections()

    def use_collection(self, pipeline_version: str, collection: str) -> bool:
        """
        把这次请求切到指定版本的 collection。返回是否切换成功。

        目标 collection 不存在时不切，留在原地——路由配置指向一个没建好的索引，
        是运维动作的错误，不该让用户的问答 500。调用方把这个事实写进 trace：

            "fallback": {"from": "idx-2.0.0-…", "reason": "collection_missing"}

        不静默回退：界面/日志里必须能看出"这次本该走新版但没走成"。
        """
        if collection == self.chunk_collection:
            self.pipeline_version = pipeline_version or self.pipeline_version
            return True
        try:
            if not self.client.collection_exists(collection):
                return False
        except Exception:
            return False
        self.chunk_collection = collection
        self.pipeline_version = pipeline_version
        return True

    @property
    def embedder(self) -> EmbedderService:
        """
        惰性初始化：embedding 模型常驻约占 2.6GB、首次加载十几秒。
        API 进程里有些路径（比如"文件命中直接复用"的判重）只需要 Qdrant，
        完全用不到向量模型——如果构造 VectorStoreService 就顺手把模型加载进来，
        一次本该 100ms 的复用请求会变成 2 秒起（实测 2.24s 里有 2s 是模型加载）。
        """
        if self._embedder is None:
            self._embedder = EmbedderService()
        return self._embedder

    def _ensure_collections(self):
        """
        建 chunk（当前版本的那个）与 paper_meta collection。

        第一次启动时先让版本注册表落地：它决定"写哪一版"，
        而写哪一版决定 chunk collection 叫什么。顺序反了就会先按配置建一个
        `ai4s_papers`，再发现真正的目标是 `ai4s_papers__idx-…`，
        于是库里多一个永远没人写的空 collection。
        """
        from app.services import index_version

        if not self.pipeline_version:
            # 显式指定了 collection（重建/迁移）时不问路由，否则等于把
            # "写目标版本"又交回给当前切流状态，重建会写到自己身上。
            self.pipeline_version = index_version.ensure_registry(self.client)
        if not self.chunk_collection:
            self.chunk_collection = index_version.collection_for(self.pipeline_version)

        self._ensure_collection(self.chunk_collection, sparse=True)
        self._ensure_collection(self.meta_collection, sparse=False)

    def _ensure_collection(self, name: str, sparse: bool) -> None:
        """按需建 collection。dense 维度与稀疏配置都固定在这里，只此一处。"""
        if self.client.collection_exists(name):
            return
        kwargs = {
            "collection_name": name,
            "vectors_config": {
                self.DENSE_VECTOR_NAME: VectorParams(
                    size=self.DENSE_DIM, distance=Distance.COSINE
                )
            },
        }
        if sparse:
            kwargs["sparse_vectors_config"] = {self.SPARSE_VECTOR_NAME: SparseVectorParams()}
        self.client.create_collection(**kwargs)

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

        一次性索引用这个方法（先清空再写）；长文档分批入库请用
        upsert_fragment_batch，否则每批都会把前面的批次删掉。
        """
        self.delete_chunks(arxiv_id, version)
        return self.upsert_fragment_batch(
            arxiv_id=arxiv_id,
            version=version,
            title=title,
            body_hash=body_hash,
            fragments=fragments,
        )

    def upsert_fragment_batch(
        self,
        arxiv_id: str,
        version: str,
        title: str,
        body_hash: str,
        fragments: List[Dict[str, Any]],
        index_offset: int = 0,
    ) -> int:
        """
        增量写入一批片段，不清空已有数据。

        点 ID 由 (arxiv_id, version, kind, 全局序号) 派生，index_offset 保证
        第 2 批的序号接在第 1 批之后，因此：
        - 批次可以乱序重试，同一批写两次得到同一批点 ID（幂等 upsert）；
        - 断点续跑时只要 index_offset 与 chunks.index_cursor 对齐，
          向量库与关系库就不会错位。
        """
        if not fragments:
            return 0

        # 只把前 MAX_EMBED_CHARS 个字符送进模型：e5 的有效上下文是 512 token，
        # 多出来的部分不影响向量，但会线性拉长时间（见 limits.py 的实测数据）。
        # chunks.text 仍然是完整原文，溯源与展示都不受影响。
        from app.services.ingest.limits import MAX_EMBED_CHARS

        texts = [f["text"][:MAX_EMBED_CHARS] for f in fragments]
        dense_vectors = self.embedder.embed_dense(texts)
        sparse_vectors = self.embedder.embed_sparse(texts)

        points: List[PointStruct] = []
        for i, frag in enumerate(fragments):
            idx = index_offset + i
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
                # 版本化的第二道保险：版本主要由 collection 隔离，
                # 但把版本写进 payload 之后，单看一条点也能回答"它是哪套管线产的"。
                # 排查"这条片段为什么搜不到"时，这一步能省掉一次对 collection 名的推断。
                "pipeline_version": self.pipeline_version,
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
                        self.DENSE_VECTOR_NAME: dense_vectors[i],
                        self.SPARSE_VECTOR_NAME: sparse_vectors[i],
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
                "pipeline_version": self.pipeline_version,
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

    def delete_chunks(self, arxiv_id: str, version: str) -> int:
        """
        删除指定论文的所有 chunk，返回删除前的命中条数。

        先 count 再 delete，不是为了省一次请求：Qdrant 1.11 的稀疏倒排索引在
        「某个 token 的 posting list 被删空」时会触发 `inverted_index_ram.rs:63`
        的越界 panic，而 panic 发生在写入线程里——线程一死，后续所有 upsert/delete
        都回 `channel closed`，只能重启进程恢复。空删除本来就没有意义，跳过它
        就把这条最容易踩的路径绕开了。

        首次入库（绝大多数情况）走的就是 count=0 的短路分支。

        为什么 count 必须 `exact=True`：近似计数在带 payload filter 时并不按
        「命中条件」去数，而是拿集合的近似总量来回答——实测对一个库内根本不存在的
        arxiv_id 也返回 138（集合总量）。后果有两条：① 短路分支永远不触发，
        空删除照发，正好踩上上面说的 panic 路径；② 返回值虚高，论文库删除时
        会把「清了 2 个片段」报成「清了 70 个」。删除是低频操作，精确数一次的
        代价远小于报错数。
        """
        selector = Filter(
            must=[
                FieldCondition(key="arxiv_id", match=MatchValue(value=arxiv_id)),
                FieldCondition(key="version", match=MatchValue(value=version)),
            ]
        )
        existing = self.client.count(
            collection_name=self.chunk_collection,
            count_filter=selector,
            exact=True,
        ).count
        if existing == 0:
            return 0

        self.client.delete(
            collection_name=self.chunk_collection,
            points_selector=selector,
            wait=True,
        )
        return existing

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
            # s2_graph 可能为 None（本地论文跳过 / S2 限流），统一降级为空字典
            "s2_paper_id": (s2_graph or {}).get("paper_id"),
            "influential_citation_count": (s2_graph or {}).get("influential_citation_count", 0),
            "citation_count": (s2_graph or {}).get("citation_count", 0),
            "reference_count": (s2_graph or {}).get("reference_count", 0),
            "references": (s2_graph or {}).get("references", []),
            "citations": (s2_graph or {}).get("citations", []),
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

    def delete_paper_meta(self, arxiv_id: str, version: str) -> bool:
        """
        删除某个 (arxiv_id, version) 的论文级 meta 点。

        和 delete_chunks 一样先 retrieve 再删：meta 集合里没有这个点时，
        直接 delete 不会报错但也没有意义；而 Qdrant 的删除走的是同一套
        write-ahead channel，能少发一次就少一次触发 panic 的机会。

        返回 True 表示确实删掉了一个点。调用方（论文库删除）只在
        「库里再没有第二个版本引用同一 arxiv_id+version」时才会调它，
        否则会把别的课题还在用的元数据一起删掉。
        """
        pid = self._meta_point_id(arxiv_id, version)
        try:
            found = self.client.retrieve(
                collection_name=self.meta_collection,
                ids=[pid],
                with_payload=False,
                with_vectors=False,
            )
            if not found:
                return False
            self.client.delete(
                collection_name=self.meta_collection,
                points_selector=[pid],
                wait=True,
            )
            return True
        except Exception:
            return False

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
        基于正文 e5-large embedding 相似度查找重复论文。
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
