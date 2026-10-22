"""
章节树构建：从 headings 列表生成≥三级章节树，LLM 辅助校正层级。
"""
from typing import List, Dict, Any
from app.services.llm_client import LLMClient


class ChapterParser:
    """
    关键设计决策：先用字体大小启发式生成 heading level，再用 LLM 做层级校正，
    保证 541 页长文档的章节树深度≥三级且结构合理。
    """

    def __init__(self):
        self.llm = LLMClient()

    def build_tree(
        self,
        headings: List[Dict[str, Any]],
        use_llm: bool = True,
        model: str | None = None,
    ) -> List[Dict[str, Any]]:
        """
        headings: [{"title": str, "page": int, "font_size": float}, ...]
        返回嵌套章节树。
        """
        if not headings:
            return []

        # 启发式：按字体大小排序，最大的为 level 1
        sizes = sorted({h["font_size"] for h in headings}, reverse=True)
        size_to_level = {s: i + 1 for i, s in enumerate(sizes[:5])}

        for h in headings:
            h["level"] = size_to_level.get(h["font_size"], len(size_to_level) + 1)

        if use_llm and headings:
            try:
                prompt = self._headings_to_prompt(headings)
                result = self.llm.json_chat(prompt, system_key="chapter_tree", model=model)
                tree = result.get("tree")
                if tree and isinstance(tree, list):
                    return tree
            except Exception:
                pass

        # 降级：启发式嵌套
        return self._heuristic_tree(headings)

    @staticmethod
    def _headings_to_prompt(headings: List[Dict[str, Any]]) -> str:
        lines = []
        for h in headings:
            lines.append(f"[level={h['level']}] {h['title']} (page {h['page']})")
        return "标题列表：\n" + "\n".join(lines)

    @staticmethod
    def _heuristic_tree(headings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 level 嵌套构建章节树。"""
        root: List[Dict[str, Any]] = []
        stack: List[Dict[str, Any]] = []

        for h in headings:
            node = {
                "id": f"ch_{h['page']}_{hash(h['title']) & 0xFFFFFFFF}",
                "title": h["title"],
                "level": h["level"],
                "page": h["page"],
                "children": [],
            }
            while stack and stack[-1]["level"] >= h["level"]:
                stack.pop()
            if stack:
                stack[-1]["children"].append(node)
            else:
                root.append(node)
            stack.append(node)
        return root
