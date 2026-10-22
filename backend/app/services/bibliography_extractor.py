"""
参考文献结构化抽取：聚合 References 章节文本，用 LLM 拆分为独立条目。
"""
from typing import List, Dict, Any
from app.services.llm_client import LLMClient


class BibliographyExtractor:
    """
    关键设计决策：将 References 区域原始文本一次性交给 LLM 拆分，
    量比逐条调用小；对超长文献列表可截断后分批处理，降低 token 成本。
    """

    def __init__(self):
        self.llm = LLMClient()

    def parse(self, raw_text: str, model: str | None = None) -> List[Dict[str, Any]]:
        """
        将参考文献原始文本拆分为结构化条目。
        model 可指定为 qwen-max 等降本模型。
        """
        # 截断到约 12k 字符，避免单次请求过长；更长文献可分批次调用
        truncated = raw_text[:12000]
        prompt = f"原始文本：\n{truncated}\n\n请按 JSON 格式输出参考文献条目。"
        result = self.llm.json_chat(prompt, system_key="references", model=model)
        refs = result.get("references", [])
        if not isinstance(refs, list):
            return []
        # 保证每条有 id
        for idx, ref in enumerate(refs, 1):
            ref.setdefault("id", f"ref_{idx}")
        return refs

    @staticmethod
    def is_references_section(text: str) -> bool:
        """启发式判断文本块是否属于 References 章节。"""
        lower = text.lower().strip()
        return lower.startswith("references") or lower.startswith("bibliography") or \
               "references" in lower[:40]
