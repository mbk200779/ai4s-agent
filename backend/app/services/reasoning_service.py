"""
跨文献推理服务：多跳问答、跨文献对比、矛盾发现。
关键设计决策：把“多跳检索”拆成最多 3 步的子问题迭代检索，
每步结论必须 grounding 到原始片段；矛盾检测作为独立后处理 pass，
在综合答案前显式暴露冲突，避免被生成模型掩盖。
"""
import json
import re
import uuid
from datetime import datetime
from typing import List, Dict, Any, Set
from app.services.retrieval_service import RetrievalService, RetrievalResult
from app.services.llm_client import LLMClient
from app.services.storage import StorageService
from app.schemas.provenance import ProvenanceRecord, ProvenanceSource
from app.schemas.reasoning import ReasoningStep, Contradiction


class ReasoningService:
    """
    跨文献推理服务。
    """

    MIN_RRF_SCORE = 0.03

    def __init__(self):
        self.retrieval = RetrievalService()
        self.llm = LLMClient()
        self.storage = StorageService()

    def multi_hop_reason(
        self, query: str, max_hops: int = 3, top_k_per_hop: int = 8
    ) -> Dict[str, Any]:
        """
        多跳问答：最多 max_hops 步，每步针对子问题检索并得出结论，
        最后综合所有步骤结论生成答案。
        """
        # 第 0 步：初始宽召回，确保覆盖≥3 篇文献
        initial_results = self.retrieval.search(
            query, top_k=top_k_per_hop * 3, rrf_k=60
        )
        initial_sources = initial_results["fusion"]
        if not initial_sources or max(r.score for r in initial_sources) < self.MIN_RRF_SCORE:
            record = self._build_provenance(
                query, "", True, "未检索到足够支撑的跨文献证据", initial_sources, []
            )
            key = self._save_provenance(record)
            return {
                "answer": "",
                "rejected": True,
                "reject_reason": "未检索到足够支撑的跨文献证据，无法回答。",
                "reasoning_chain": [],
                "contradictions": [],
                "sources": [],
                "papers_involved": [],
                "provenance_key": key,
            }

        all_sources: List[RetrievalResult] = list(initial_sources)
        chain: List[ReasoningStep] = []
        current_query = query

        for hop in range(1, max_hops + 1):
            # 基于当前问题/中间结论检索
            hop_results = self.retrieval.search(
                current_query, top_k=top_k_per_hop, rrf_k=60
            )
            hop_sources = hop_results["fusion"]
            if not hop_sources:
                break
            all_sources.extend(hop_sources)

            # 构造带编号片段的上下文
            context = self._format_context(hop_sources)
            sub_prompt = (
                f"原始问题：{query}\n"
                f"当前子问题（第 {hop} 跳）：{current_query}\n\n"
                f"{context}\n\n"
                "请基于上述片段给出当前子问题的结论，并在结论后使用 [引#N] 标注来源。"
                "若片段不足以回答，请回答“无法回答”。"
            )
            conclusion = self.llm.chat(sub_prompt, system_key="qa_system")
            validated, _, ref_map = self._validate_citations(conclusion, hop_sources)

            step = ReasoningStep(
                step=hop,
                sub_question=current_query,
                conclusion=validated,
                citation_ids=list(ref_map.values()),
                sources=[s.model_dump() for s in hop_sources],
            )
            chain.append(step)

            # 生成下一跳子问题（若还需继续）
            if hop < max_hops:
                next_q = self._generate_next_hop_question(query, chain)
                if not next_q or next_q.lower() in ("无", "none", "不需要"):
                    break
                current_query = next_q

        # 去重并收集涉及文献
        unique_sources = self._dedup_sources(all_sources)
        papers_involved = sorted({s.paper_id for s in unique_sources})

        # 矛盾检测
        contradictions = self._detect_contradictions(unique_sources)

        # 综合最终答案
        final_context = self._format_context(unique_sources[:20])
        final_prompt = (
            f"原始问题：{query}\n"
            f"推理链（共 {len(chain)} 步）：\n" + "\n".join(
                f"第 {s.step} 步 [{s.sub_question}]：{s.conclusion}" for s in chain
            ) + f"\n\n{final_context}\n\n"
            "请综合以上推理链与原文片段，给出最终答案。"
            "若存在矛盾，请在答案中显式指出。"
            "每句话后使用 [引#N] 标注来源。"
        )
        final_answer_raw = self.llm.chat(final_prompt, system_key="qa_system")
        final_answer, _, final_ref_map = self._validate_citations(
            final_answer_raw, unique_sources[:20]
        )

        record = self._build_provenance(
            query, final_answer, False, None, unique_sources, [], reasoning_chain=chain
        )
        key = self._save_provenance(record)

        return {
            "answer": final_answer,
            "rejected": False,
            "reasoning_chain": [s.model_dump() for s in chain],
            "contradictions": [c.model_dump() for c in contradictions],
            "sources": [s.model_dump() for s in unique_sources],
            "papers_involved": papers_involved,
            "provenance_key": key,
        }

    def compare_papers(
        self, topic: str, arxiv_ids: List[str], top_k: int = 8
    ) -> Dict[str, Any]:
        """
        跨文献对比：针对主题分别检索指定论文，生成对比结论并检测矛盾。
        """
        all_sources: List[RetrievalResult] = []
        for arxiv_id in arxiv_ids:
            result = self.retrieval.search(
                f"{topic} arXiv:{arxiv_id}", top_k=top_k, rrf_k=60
            )
            all_sources.extend(result["fusion"])

        unique_sources = self._dedup_sources(all_sources)
        context = self._format_context(unique_sources)
        prompt = (
            f"请围绕主题「{topic}」对比以下 {len(arxiv_ids)} 篇文献的观点、方法与结论。"
            "若存在矛盾或不一致，请显式列出。\n\n"
            f"{context}\n\n请使用 [引#N] 标注来源。"
        )
        raw = self.llm.chat(prompt, system_key="review_system")
        comparison, _, ref_map = self._validate_citations(raw, unique_sources)
        contradictions = self._detect_contradictions(unique_sources)

        record = self._build_provenance(topic, comparison, False, None, unique_sources, [])
        key = self._save_provenance(record)

        return {
            "topic": topic,
            "comparison": comparison,
            "contradictions": [c.model_dump() for c in contradictions],
            "sources": [s.model_dump() for s in unique_sources],
            "provenance_key": key,
        }

    def detect_contradictions(
        self, query: str, top_k: int = 16
    ) -> Dict[str, Any]:
        """
        直接针对查询发现文献中的矛盾点。
        """
        result = self.retrieval.search(query, top_k=top_k, rrf_k=60)
        sources = self._dedup_sources(result["fusion"])
        contradictions = self._detect_contradictions(sources)
        return {
            "query": query,
            "contradictions": [c.model_dump() for c in contradictions],
            "sources": [s.model_dump() for s in sources],
        }

    def _detect_contradictions(self, sources: List[RetrievalResult]) -> List[Contradiction]:
        """调用 LLM 检测证据中的矛盾。"""
        if len(sources) < 2:
            return []
        context = self._format_context(sources)
        prompt = (
            "下面是从多篇文献中检索到的片段。请识别其中相互矛盾或不一致的论断。\n\n"
            f"{context}\n\n"
            "输出严格为 JSON：{\"contradictions\": [{\"claim_a\", \"claim_b\", \"chunk_id_a\", \"chunk_id_b\", \"explanation\"}]}。"
            "若未发现矛盾，返回空数组。"
        )
        parsed = self.llm.json_chat(prompt, system_key="references")
        items = parsed.get("contradictions", [])
        contradictions = []
        for item in items:
            try:
                contradictions.append(Contradiction(**item))
            except Exception:
                continue
        return contradictions

    def _generate_next_hop_question(self, original_query: str, chain: List[ReasoningStep]) -> str:
        """基于当前推理链生成下一跳子问题；若已充分则返回空。"""
        chain_text = "\n".join(f"第 {s.step} 步：{s.conclusion}" for s in chain)
        prompt = (
            f"原始问题：{original_query}\n"
            f"已完成的推理步骤：\n{chain_text}\n\n"
            "为了回答原始问题，是否还需要提出一个子问题继续检索？"
            "如果需要，请只输出子问题；如果已经足够，请只输出“无”。"
        )
        resp = self.llm.chat(prompt, system_key="qa_system")
        resp = resp.strip().strip("\"'")
        if resp.lower() in ("无", "none", "no", "不需要"):
            return ""
        return resp

    @staticmethod
    def _format_context(sources: List[RetrievalResult]) -> str:
        parts = []
        for i, s in enumerate(sources, 1):
            parts.append(
                f"[片段{i}] {s.title} (arXiv:{s.arxiv_id}, p.{s.page}, para.{s.paragraph}, "
                f"kind={s.kind}, score={s.score:.4f})\n{s.text}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _validate_citations(
        answer: str, sources: List[RetrievalResult]
    ) -> tuple[str, list, dict]:
        """校验 [引#N] 并映射到 chunk_id。"""
        max_valid = len(sources)
        invalid: List[str] = []
        ref_map: Dict[str, str] = {}
        pattern = re.compile(r"\[引#(\d+)\]")

        def replacer(m: re.Match) -> str:
            idx = int(m.group(1))
            if 1 <= idx <= max_valid:
                src = sources[idx - 1]
                ref_map[f"[引#{idx}]"] = src.chunk_id
                return f"[引#{idx}: {src.title} p.{src.page}]"
            invalid.append(str(idx))
            return "[无依据]"

        validated = pattern.sub(replacer, answer)
        return validated, invalid, ref_map

    @staticmethod
    def _dedup_sources(sources: List[RetrievalResult]) -> List[RetrievalResult]:
        seen: Set[str] = set()
        out: List[RetrievalResult] = []
        for s in sources:
            if s.chunk_id in seen:
                continue
            seen.add(s.chunk_id)
            out.append(s)
        return out

    def _build_provenance(
        self,
        query: str,
        answer: str,
        rejected: bool,
        reject_reason: str | None,
        sources: List[RetrievalResult],
        invalid_citations: List[str],
        reasoning_chain: List[ReasoningStep] | None = None,
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
            answer_ref_map={},
            reasoning_chain=[s.model_dump() for s in (reasoning_chain or [])],
        )

    def _save_provenance(self, record: ProvenanceRecord) -> str:
        key = f"reasoning/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}.json"
        data = record.model_dump_json(indent=2).encode("utf-8")
        self.storage.upload_provenance(key, data, content_type="application/json")
        return key
