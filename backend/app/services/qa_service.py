"""
可溯源抗幻觉 QA 服务：强制引用、引用校验、无支撑拒答、provenance 日志。
"""
import re
import json
import uuid
from datetime import datetime
from typing import List, Dict, Any, Tuple
from app.services.retrieval_service import RetrievalService, RetrievalResult
from app.services.llm_client import LLMClient
from app.services.storage import StorageService
from app.schemas.provenance import ProvenanceRecord, ProvenanceSource
from app.core.config import settings


class QAService:
    """
    关键设计决策：
    - 检索结果为空或最佳 RRF 分数低于阈值时直接拒答，把“拒答率”作为抗幻觉量化证据；
    - 强制 LLM 每句话后挂 [引#N]，N 为检索片段序号；
    - 后处理逐条校验引用 ID，若不存在则替换为 [无依据]，并写入 provenance 日志。
    """

    # RRF 融合分数阈值：低于此值认为检索结果不足以支撑答案
    MIN_RRF_SCORE = 0.03

    CITE_RE = re.compile(r"\[引#(\d+)\]")

    def __init__(self):
        self.retrieval = RetrievalService()
        self.llm = LLMClient()
        self.storage = StorageService()

    def ask(
        self,
        question: str,
        top_k: int = 8,
        rrf_k: int = 60,
    ) -> Dict[str, Any]:
        """
        问答入口。
        返回：{answer, rejected, reject_reason, sources, invalid_citations, provenance_key}
        """
        # 1. 检索
        result = self.retrieval.search(question, top_k=top_k, rrf_k=rrf_k)
        sources: List[RetrievalResult] = result["fusion"]

        # 2. 拒答判断：无结果或最高分过低
        if not sources:
            record = self._build_provenance(question, "", True, "未检索到任何相关片段", sources, [], {})
            key = self._save_provenance(record)
            return {
                "answer": "",
                "rejected": True,
                "reject_reason": "未检索到任何相关片段，无法回答。",
                "sources": [],
                "invalid_citations": [],
                "provenance_key": key,
            }

        best_score = max(r.score for r in sources)
        if best_score < self.MIN_RRF_SCORE:
            record = self._build_provenance(
                question, "", True, f"最佳 RRF 分数 {best_score:.4f} 低于阈值 {self.MIN_RRF_SCORE}", sources, [], {}
            )
            key = self._save_provenance(record)
            return {
                "answer": "",
                "rejected": True,
                "reject_reason": f"检索结果置信度过低（最高 {best_score:.4f}），无法回答。",
                "sources": [s.model_dump() for s in sources],
                "invalid_citations": [],
                "provenance_key": key,
            }

        # 3. 构造带强制引用的上下文
        context = self._format_context(sources)
        prompt = self._qa_prompt(question, context)

        # 4. LLM 生成答案
        raw_answer = self.llm.chat(
            prompt,
            system_key="qa_system",
        )

        # 5. 引用校验与降级
        validated_answer, invalid_ids, ref_map = self._validate_citations(raw_answer, sources)

        # 6. provenance 日志
        record = self._build_provenance(
            question, validated_answer, False, None, sources, invalid_ids, ref_map
        )
        key = self._save_provenance(record)

        return {
            "answer": validated_answer,
            "rejected": False,
            "reject_reason": None,
            "sources": [s.model_dump() for s in sources],
            "invalid_citations": invalid_ids,
            "provenance_key": key,
        }

    @staticmethod
    def _format_context(sources: List[RetrievalResult]) -> str:
        """把检索片段编号后拼成上下文。"""
        parts = []
        for i, s in enumerate(sources, 1):
            parts.append(
                f"[片段{i}] {s.title} (arXiv:{s.arxiv_id}, 页 {s.page}, 段落 {s.paragraph}, "
                f"类型 {s.kind}, 分数 {s.score:.4f})\n{s.text}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _qa_prompt(question: str, context: str) -> str:
        return (
            "你是一位严谨的科研助手。请严格基于下面提供的编号片段回答问题。"
            "每写一句话，必须在句末用 [引#N] 标注来源，N 为片段编号。"
            "如果片段中找不到答案，请只回答“无法回答”。"
            "禁止使用片段以外的知识，禁止编造引用。\n\n"
            f"{context}\n\n问题：{question}\n\n请回答："
        )

    def _validate_citations(
        self, answer: str, sources: List[RetrievalResult]
    ) -> Tuple[str, List[str], Dict[str, str]]:
        """
        校验答案中的 [引#N] 是否都存在于检索结果。
        不存在的替换为 [无依据]，并记录无效 ID。
        """
        max_valid = len(sources)
        invalid_ids: List[str] = []
        ref_map: Dict[str, str] = {}

        def replacer(match: re.Match) -> str:
            idx_str = match.group(1)
            idx = int(idx_str)
            if 1 <= idx <= max_valid:
                src = sources[idx - 1]
                ref_map[f"[引#{idx}]"] = src.chunk_id
                # 增强为更详细的引用，便于人读
                return f"[引#{idx}: {src.title} p.{src.page}]"
            invalid_ids.append(idx_str)
            return "[无依据]"

        validated = self.CITE_RE.sub(replacer, answer)
        return validated, invalid_ids, ref_map

    def _build_provenance(
        self,
        query: str,
        answer: str,
        rejected: bool,
        reject_reason: str | None,
        sources: List[RetrievalResult],
        invalid_citations: List[str],
        ref_map: Dict[str, str],
    ) -> ProvenanceRecord:
        provenance_sources = [
            ProvenanceSource(
                chunk_id=s.chunk_id,
                paper_id=s.paper_id,
                arxiv_id=s.arxiv_id,
                version=s.version,
                title=s.title,
                kind=s.kind,
                page=s.page,
                paragraph=s.paragraph,
                bbox=s.bbox,
                text=s.text,
                score=s.score,
            )
            for s in sources
        ]
        return ProvenanceRecord(
            query=query,
            answer=answer,
            rejected=rejected,
            reject_reason=reject_reason,
            sources=provenance_sources,
            invalid_citations=invalid_citations,
            answer_ref_map=ref_map,
        )

    def _save_provenance(self, record: ProvenanceRecord) -> str:
        """把 provenance 记录写入 MinIO provenance bucket，返回 key。"""
        key = f"qa/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}.json"
        data = record.model_dump_json(indent=2).encode("utf-8")
        self.storage.upload_provenance(key, data, content_type="application/json")
        return key
