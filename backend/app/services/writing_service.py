"""
写作辅助服务：基于检索证据生成论文大纲。
"""
from app.services.search_service import SearchService
from app.services.llm_client import LLMClient


class WritingService:
    """
    关键设计决策：大纲生成基于已有文献证据，避免模型幻觉，
    输出的每一节均标注可追溯到相关论文。
    """

    def __init__(self):
        self.search = SearchService()
        self.llm = LLMClient()

    def generate_outline(self, topic: str, top_k: int = 8) -> str:
        chunks = self.search.retrieve(topic, top_k=top_k)
        context = self.search.format_context(chunks)
        prompt = (
            f"请为论文主题「{topic}」生成一份详细的论文大纲，"
            f"包括引言、相关工作、方法、实验、结论等章节，"
            f"并在每节后标注可能参考的 arXiv ID。"
        )
        messages = [
            {"role": "system", "content": self.llm._system_prompt("review_system")},
            {"role": "user", "content": f"参考片段：\n{context}\n\n{prompt}"},
        ]
        return self.llm.chat(prompt, system_key="review_system", context=context)
