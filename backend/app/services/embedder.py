"""
Embedding 服务：使用 fastembed 同时产出 dense(bge-m3) 与 sparse(BM25) 向量，
通过 Qdrant named vectors 分别存储，检索时用 RRF 融合。
"""
from typing import List, Dict, Any
from fastembed import TextEmbedding, SparseTextEmbedding
from qdrant_client.models import SparseVector


class EmbedderService:
    """
    关键设计决策：模型实例在进程内单例化，避免每次请求重复加载；
    模型缓存目录通过环境变量固定到 /app/.cache，保证复现性。
    """

    _dense_model: TextEmbedding | None = None
    _sparse_model: SparseTextEmbedding | None = None

    def __init__(self):
        if EmbedderService._dense_model is None:
            EmbedderService._dense_model = TextEmbedding(model_name="BAAI/bge-m3")
        if EmbedderService._sparse_model is None:
            EmbedderService._sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
        self.dense_model = EmbedderService._dense_model
        self.sparse_model = EmbedderService._sparse_model

    def embed_dense(self, texts: List[str]) -> List[List[float]]:
        """生成 BGE-M3 dense 向量（1024 维）。"""
        return list(self.dense_model.embed(texts))

    def embed_sparse(self, texts: List[str]) -> List[SparseVector]:
        """生成 BM25 sparse 向量，转换为 Qdrant SparseVector。"""
        sparse_iter = self.sparse_model.embed(texts)
        result: List[SparseVector] = []
        for emb in sparse_iter:
            # fastembed sparse 对象提供 indices 与 values 属性
            result.append(
                SparseVector(
                    indices=[int(i) for i in emb.indices.tolist()],
                    values=[float(v) for v in emb.values.tolist()],
                )
            )
        return result

    def embed(self, texts: List[str]) -> Dict[str, Any]:
        """同时生成 dense 与 sparse 向量。"""
        return {
            "dense": self.embed_dense(texts),
            "sparse": self.embed_sparse(texts),
        }
