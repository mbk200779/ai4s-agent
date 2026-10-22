"""
检索服务：封装 RRF 检索与上下文拼接，供 QA/综述复用。
"""
from typing import List, Dict, Any
from app.services.retrieval_service import RetrievalService, RetrievalResult


class SearchService:
    """
    关键设计决策：检索与生成解耦，QA 和综述共用同一 RRF 检索层，
    保证不同模块看到的证据一致，降低幻觉风险。
    """

    def __init__(self):
        self.retrieval = RetrievalService()

    def retrieve(self, query: str, top_k: int = 8, rrf_k: int = 60) -> List[Dict[str, Any]]:
        """返回 RRF 融合后的片段列表（兼容旧 dict 格式）。"""
        result = self.retrieval.search(query, top_k=top_k, rrf_k=rrf_k)
        return [r.model_dump() for r in result["fusion"]]

    @staticmethod
    def format_context(chunks: List[Dict[str, Any]]) -> str:
        """将 chunk 拼接为带引用标记的上下文。"""
        parts = []
        for i, chunk in enumerate(chunks, 1):
            parts.append(
                f"[{i}] {chunk['title']} (arXiv:{chunk['arxiv_id']}, "
                f"页 {chunk['page']}, 段落 {chunk.get('paragraph', 0)}, 章节 {chunk['section']})\n{chunk['text']}"
            )
        return "\n\n".join(parts)
