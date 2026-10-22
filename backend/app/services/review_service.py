"""
综述生成服务：自动检索相关论文片段，调用 LLM 生成可验证综述。
"""
import re
import uuid
from datetime import datetime
from typing import List, Dict, Any
from app.services.search_service import SearchService
from app.services.llm_client import LLMClient
from app.services.storage import StorageService


class ReviewService:
    """
    关键设计决策：综述完全基于检索到的原文片段生成，每句话后强制 [引#N]；
    后处理校验引用有效性，无效引用替换为 [无依据]；最终 Markdown 附带参考文献列表，
    可直接导出复现。
    """

    CITE_RE = re.compile(r"\[引#(\d+)\]")

    def __init__(self):
        self.search = SearchService()
        self.llm = LLMClient()
        self.storage = StorageService()

    def generate(self, topic: str, arxiv_ids: List[str] | None = None, top_k: int = 10) -> Dict[str, Any]:
        """生成带引用的 Markdown 综述。"""
        query = topic if not arxiv_ids else f"{topic} " + " ".join(arxiv_ids)
        chunks = self.search.retrieve(query, top_k=top_k)

        if arxiv_ids:
            chunks = [c for c in chunks if c["arxiv_id"] in arxiv_ids]

        if not chunks:
            return {
                "topic": topic,
                "markdown": "",
                "rejected": True,
                "reject_reason": "未检索到足够相关片段，无法生成综述。",
                "referenced_arxiv_ids": [],
            }

        context = self.search.format_context(chunks)
        prompt = self._review_prompt(topic, context)
        raw = self.llm.chat(prompt, system_key="review_system")

        # 引用校验
        validated, invalid_ids, ref_map = self._validate_citations(raw, chunks)

        # 生成 Markdown 与参考文献
        markdown = self._to_markdown(topic, validated, chunks, ref_map)

        # 可选：导出到 MinIO
        export_key = self._export_to_minio(topic, markdown)

        referenced = sorted({c["arxiv_id"] for c in chunks})
        return {
            "topic": topic,
            "markdown": markdown,
            "rejected": False,
            "invalid_citations": invalid_ids,
            "referenced_arxiv_ids": referenced,
            "export_key": export_key,
        }

    @staticmethod
    def _review_prompt(topic: str, context: str) -> str:
        return (
            f"请基于以下论文片段，围绕主题「{topic}」生成一份学术综述草稿。\n"
            "要求：\n"
            "1. 结构包含：引言、相关工作、方法、主要发现、待解决问题；\n"
            "2. 每句话后必须用 [引#N] 标注来源，N 为片段编号；\n"
            "3. 禁止引用片段以外的知识；\n"
            "4. 输出为 Markdown 格式。\n\n"
            f"{context}\n\n请生成综述："
        )

    @staticmethod
    def _validate_citations(text: str, chunks: List[Dict[str, Any]]) -> tuple[str, List[str], Dict[str, str]]:
        max_valid = len(chunks)
        invalid: List[str] = []
        ref_map: Dict[str, str] = {}

        def replacer(m: re.Match) -> str:
            idx = int(m.group(1))
            if 1 <= idx <= max_valid:
                c = chunks[idx - 1]
                ref_map[f"[引#{idx}]"] = c.get("chunk_id", "")
                return f"[引#{idx}: {c['title']} arXiv:{c['arxiv_id']} p.{c['page']}]"
            invalid.append(str(idx))
            return "[无依据]"

        validated = ReviewService.CITE_RE.sub(replacer, text)
        return validated, invalid, ref_map

    @staticmethod
    def _to_markdown(
        topic: str,
        body: str,
        chunks: List[Dict[str, Any]],
        ref_map: Dict[str, str],
    ) -> str:
        lines = [
            f"# {topic}",
            "",
            "> 本综述由 AI4S Agent 基于检索到的文献片段自动生成，每句均带引用。",
            "",
            body,
            "",
            "## 参考文献",
            "",
        ]
        seen = set()
        for i, c in enumerate(chunks, 1):
            key = (c["arxiv_id"], c["paper_id"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{len(seen)}. {c['title']} (arXiv:{c['arxiv_id']}) [paper_id: {c['paper_id']}]")
        return "\n".join(lines)

    def _export_to_minio(self, topic: str, markdown: str) -> str:
        """把综述 Markdown 写入 MinIO，返回下载 key。"""
        key = f"reviews/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}.md"
        self.storage.put_object(
            self.storage.provenance_bucket,  # 复用 provenance bucket 存储导出文件
            key,
            markdown.encode("utf-8"),
            content_type="text/markdown",
        )
        return key

    def get_export_url(self, key: str, expires: int = 3600) -> str:
        """获取综述 Markdown 临时下载链接。"""
        return self.storage.get_presigned_url(self.storage.provenance_bucket, key, expires=expires)
