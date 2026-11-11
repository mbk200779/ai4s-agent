"""
Embedding 服务：使用 fastembed 同时产出 dense(e5-large) 与 sparse(BM25) 向量，
通过 Qdrant named vectors 分别存储，检索时用 RRF 融合。
"""
from typing import List, Dict, Any
from fastembed import TextEmbedding, SparseTextEmbedding
from qdrant_client.models import SparseVector

from app.core.config import settings


class EmbedderService:
    """
    关键设计决策：模型实例在进程内单例化，避免每次请求重复加载；
    模型缓存目录通过环境变量固定到 /app/.cache，保证复现性。

    线程数显式指定（默认 8，可由 EMBED_THREADS 覆盖）：不指定时 onnxruntime
    按核数开线程，单条短文本会因为线程调度开销反而更慢——实测 16 核机器上
    同一句「这两种架构各自的取舍是什么」默认 197~350ms、8 线程 40ms，
    20 条长文本批量 8.8s vs 4.4s。这条对分类延迟（300ms 预算）和长文档解析
    （embedding 占总时长 9 成）都是直接收益，所以不做成"让它自己选"。
    """

    _dense_model: TextEmbedding | None = None
    _sparse_model: SparseTextEmbedding | None = None

    # 模型名必须是**常量**而不是构造参数里的一次性字符串：
    # 索引版本签名要拿它算 hash（见 services/index_version.py），
    # 签名与真正加载的模型必须是同一个来源。分成两处写，换模型时
    # 只改了一处，签名就不变——于是新向量被写进旧 collection。
    DENSE_MODEL_NAME = "intfloat/multilingual-e5-large"
    SPARSE_MODEL_NAME = "Qdrant/bm25"

    def __init__(self):
        if EmbedderService._dense_model is None:
            # fastembed 0.9.x 不支持 bge-m3，改用同为 1024 维的多语言模型 e5-large
            EmbedderService._dense_model = TextEmbedding(
                model_name=self.DENSE_MODEL_NAME,
                threads=max(1, int(settings.embed_threads)),
            )
        if EmbedderService._sparse_model is None:
            EmbedderService._sparse_model = SparseTextEmbedding(model_name=self.SPARSE_MODEL_NAME)
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
