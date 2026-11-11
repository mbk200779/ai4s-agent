"""
写作辅助的兼容门面。

旧实现（`generate_outline`）只有一句话：「检索 top-k → 让模型写一份大纲」。
问题和大纲本身关系不大，和**产出物**关系很大：出来的是一段 Markdown 文字，
每个小节后面挂个 arXiv 号，读者没有任何办法核对那个号是不是真的、那句话说没说过。
09 之后写作的主路径是 `services/writing/` 的 `WritingPipeline`（句级溯源）。

这里只保留两个入口，且都**直接转给新实现**，不留旧代码当 fallback：
- `generate_outline`：只出结构，不写正文。结构由 `plan_sections` 依据库内文献规划，
  所以它天然不会规划出库里没有的章节。
- `get_export_url`：MinIO 临时链接。

为什么不留旧实现"兜底"：旧实现产出的文本**没有任何溯源信息**，
一旦在某次异常里被启用，用户拿到的就是一份"看起来正常、实际不可查"的稿子——
这比报错更糟。要兜底也只能兜到"没生成"。
"""
from __future__ import annotations

from typing import Any

from app.services.writing import prompts
from app.services.writing.compose import plan_sections
from app.services.writing.references import library_papers
from app.services.writing.service import WritingPipeline


class WritingService:
    def __init__(self) -> None:
        self.pipeline = WritingPipeline()

    # ------------------------------------------------------------------ 大纲

    def generate_outline(self, topic: str) -> tuple[str, list[str]]:
        """
        只出大纲：段落标题 + 本段意图 + 本段检索用的 query。

        返回 (markdown, 依据的文献标题)。段落规划用的是同一套 `plan_sections`，
        所以"大纲里的每一段将来都能检索到证据"这件事在出大纲时就成立过一遍。
        """
        catalog = library_papers()
        if not catalog:
            return (
                "文献库里还没有可引用的正文，无法规划大纲。\n\n"
                "（先上传 PDF 或导入 arXiv 论文，再回来生成。）",
                [],
            )
        plan = plan_sections(topic, catalog, n_sections=4)
        lines = [f"## {topic}", ""]
        if plan["source"] == "template":
            lines.append("> 规划模型不可用，已退到通用结构（背景 / 方法 / 证据 / 局限）。")
            lines.append("")
        for section in plan["sections"]:
            lines.append(f"### {section['no']}. {section['title']}")
            lines.append("")
            if section.get("intent"):
                lines.append(f"- **写什么**：{section['intent']}")
            lines.append(f"- **检索线索**：{section['query']}")
            lines.append("- **引用要求**：本段每句结论都要带 `[引#N]`，无证据则留空占位")
            lines.append("")
        lines.append("## 参考文献")
        lines.append("")
        lines.append("> 由系统从库内 `papers` 表生成，不接受模型自由发挥。")
        for p in catalog:
            ident = f"arXiv:{p['arxiv_id']}" if p.get("arxiv_id") else ""
            lines.append(f"- {p['title']} {ident}".strip())
        return "\n".join(lines), [p["title"] for p in catalog]

    # ------------------------------------------------------------------ 其它

    def generate_draft(self, topic: str, **kwargs: Any) -> dict[str, Any]:
        return self.pipeline.generate(topic, **kwargs)

    def get_export_url(self, key: str, expires: int = 3600) -> str:
        return self.pipeline.get_export_url(key, expires=expires)

    @staticmethod
    def config_block() -> dict[str, Any]:
        return prompts.config_block()
