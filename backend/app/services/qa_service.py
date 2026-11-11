"""
可溯源抗幻觉 QA 服务：强制引用、引用校验、无支撑拒答、provenance 日志。

相对上一版的关键变化：检索不再走"一套 top-k 打所有问题"，
而是先做查询理解（QueryRouter），再按类型派发到五条策略（RAGRetriever）。
分流过程写进 provenance 与接口响应的 `routing` 字段，前端可见。
"""
import re
import json
import uuid
from datetime import datetime
from typing import List, Dict, Any
from app.services.retrieval_service import RetrievalService, RetrievalResult
from app.services.llm_client import LLMClient
from app.services.storage import StorageService
from app.repositories import ChunkRepository
from app.services.rag import QueryType, RAGRetriever
from app.services.rag import answer as answer_lib
from app.services.rag import confidence as confidence_lib
from app.services.rag import uncertainty as uncertainty_lib
from app.services.rag.taxonomy import RRF_MIN_SCORE
from app.schemas.provenance import ProvenanceRecord, ProvenanceSource
from app.core.config import settings


class QAService:
    """
    关键设计决策：
    - 检索结果为空或最佳原始融合分低于阈值时直接拒答，把“拒答率”作为抗幻觉量化证据；
      阈值由策略自己给出（不同策略的两路权重不同），含义始终是"必须两路同时召回"；
    - 强制 LLM 每句话后挂 [引#N]，N 为检索片段序号；
    - 后处理逐条校验引用 ID，若不存在则替换为 [无依据]，并写入 provenance 日志；
    - 闲聊类不检索、不带引用、不拒答——用 RRF 分数去约束"你好"是没有意义的。
    """

    # 保留为对外可见的常量：分流前的唯一阈值。generic 策略与它逐字对齐。
    MIN_RRF_SCORE = RRF_MIN_SCORE

    CITE_RE = re.compile(r"\[引#(\d+)\]")

    def __init__(self):
        self.retrieval = RetrievalService()
        self.rag = RAGRetriever(self.retrieval)
        self.router = self.rag.router
        self.llm = LLMClient()
        self.storage = StorageService()

    def ask(
        self,
        question: str,
        top_k: int = 8,
        rrf_k: int = 60,
        force_type: str | None = None,
        rerank: bool | None = None,
        numeric: bool | None = None,
        project_id: str | None = None,
        index_version: str | None = None,
        search_text: str | None = None,
    ) -> Dict[str, Any]:
        """
        问答入口。
        返回：{answer, rejected, reject_reason, sources, invalid_citations,
               provenance_key, routing}

        project_id / index_version 决定这次读哪一版索引（13）：
        前者命中课题级切流规则，后者是显式指定（评测、排查、强制 A/B）。
        两者都不传就按全局灰度比例分桶。最终命中哪一版写在 routing.index 里。

        search_text 决定"拿什么去检索"（14）：不传就按原逻辑翻译。
        评测必须传——翻译本身是一次 LLM 调用，同一个中文问题两次跑出两个
        英文译法，检索结果就会漂，而那不是检索的方差，是翻译的方差。
        把译文固定下来存档，才能把"同输入同输出"这句话落实到可比的层面。
        """
        # 0. 查询理解。先分类再翻译：分类规则是中文的，必须看原句；
        #    翻译是为了让英文正文能被检索到，顺序反过来会先白花一次 LLM 调用。
        plan = self.router.analyze(question)

        # 0.1 闲聊：不检索、不翻译，直接作答
        if plan.type == QueryType.CHITCHAT.value:
            return self._answer_direct(question, plan, top_k, rrf_k)

        # 1. 跨语言检索优化：中文问题先翻译成英文再检索（论文正文多为英文），
        #    回答仍基于原问题与中文系统提示，最终用中文作答。
        search_query = search_text or question
        if search_text is None and re.search(r"[\u4e00-\u9fff]", question):
            try:
                translated = self.llm.chat(
                    f"Translate this research question to English:\n\n{question}",
                    system_key="translate_system",
                    model=settings.litellm_cheap_model,
                ).strip()
                if translated:
                    search_query = translated
            except Exception:
                search_query = question  # 翻译失败则回退原始问题

        # 2. 按类型分流检索 → 规则召回 → 精排。
        #    精排与检索共用 search_query：精排判断的是"这段正文能否回答**这次检索的意图**"，
        #    与召回用同一句才不会各说各话（默认 reranker 也是英文模型）。
        route = self.rag.route(
            question, search_text=search_query, top_k=top_k, rrf_k=rrf_k, plan=plan,
            rerank=rerank, numeric=numeric,
            project_id=project_id, index_version=index_version,
        )
        sources: List[RetrievalResult] = route.results
        routing = route.trace

        # 3. 不确定性状态（12）：先判"空检索"——它决定要不要生成。
        #    为什么判定放在这里、而不是交给前端：前端只拿到 top-k 片段，
        #    看不到"库内一共几篇、关键词命中几次"，判不出"是库里没有还是排序不好"；
        #    更要紧的是"跳过生成"这个决定前端没有权力做。
        corpus = uncertainty_lib.corpus_of(self.rag.retrieval.vector_store)
        weak = self._is_weak(route) if sources else False
        top_raw = routing["recall"].get("top_raw_score")
        unc = uncertainty_lib.build(
            question=question, search_text=search_query, plan_type=plan.type,
            sources=sources, retrieval_weak=weak, top_raw=top_raw,
            threshold=route.reject_threshold, corpus=corpus,
            strategy=(routing.get("strategy") or {}).get("label"),
            translated=search_query != question,
        )

        # 3.1 空检索：库里确实没有，就别让模型拿着 6 段近邻去编。
        #     旧版这里是一句"未检索到任何相关片段，无法回答"——
        #     那句话把"库里没有"和"系统没搜到"压成同一种，用户只能猜。
        if unc["empty"]:
            reason = uncertainty_lib.reason_text(unc["empty"])
            record = self._build_provenance(
                question, "", True, reason, sources, [], {}, routing,
                confidence=confidence_lib.grade_answer(
                    [], sources=[], query_type=plan.type, rejected=True,
                    reject_reason=reason,
                ),
                uncertainty=unc,
            )
            key = self._save_provenance(record)
            return {
                "answer": "",
                "rejected": True,
                "reject_reason": reason,
                "sources": [s.model_dump() for s in sources],
                "invalid_citations": [],
                "provenance_key": key,
                "routing": routing,
                "claims": [],
                "answer_refs": [],
                "claim_stats": confidence_lib.summarize([]),
                "confidence": confidence_lib.grade_answer(
                    [], sources=[], query_type=plan.type, rejected=True,
                    reject_reason=reason,
                ),
                "uncertainty": unc,
            }

        threshold = route.reject_threshold
        if weak and settings.reject_on_low_score:
            reason = f"检索结果置信度过低（原始分 {top_raw:.4f} < {threshold}），这次不作答。"
            record = self._build_provenance(
                question, "", True, reason, sources, [], {}, routing,
                confidence=confidence_lib.grade_answer(
                    [], sources=sources, query_type=plan.type, top_raw_score=top_raw,
                    reject_threshold=threshold, retrieval_weak=True,
                    rejected=True, reject_reason=reason,
                ),
                uncertainty=unc,
            )
            key = self._save_provenance(record)
            return {
                "answer": "",
                "rejected": True,
                "reject_reason": reason,
                "sources": [s.model_dump() for s in sources],
                "invalid_citations": [],
                "provenance_key": key,
                "routing": routing,
                "claims": [],
                "answer_refs": [],
                "claim_stats": confidence_lib.summarize([]),
                "confidence": confidence_lib.grade_answer(
                    [], sources=sources, query_type=plan.type, top_raw_score=top_raw,
                    reject_threshold=threshold, retrieval_weak=True,
                    rejected=True, reject_reason=reason,
                ),
                "uncertainty": unc,
            }

        # 4. 构造带强制引用的上下文
        context = self._format_context(sources)
        prompt = self._qa_prompt(question, context, plan.type)

        # 5. LLM 生成答案
        raw_answer = self.llm.chat(
            prompt,
            system_key="qa_system",
        )

        # 6. 引用校验 + 三档置信（同时把每个角标落到具体句子）
        anchors_map = self._load_anchors(sources)
        built = self._apply_protocol(
            raw_answer, sources, anchors_map,
            query_type=plan.type, top_raw_score=top_raw, reject_threshold=threshold,
            retrieval_weak=weak,
        )

        # 7. 不确定性状态（12）：这一轮带上 claims/refs 再算一次——
        #    冲突要看答出来的几句之间有没有方向相反，未识别要看引用的那段本身可不可读。
        #    两者都依赖"答完了"才知道，所以放在这里而不是第 3 步。
        unc = uncertainty_lib.build(
            question=question, search_text=search_query, plan_type=plan.type,
            sources=sources, anchors_map=anchors_map,
            claims=built["claims"], refs=built["refs"],
            retrieval_weak=weak, top_raw=top_raw, threshold=threshold,
            corpus=corpus, strategy=(routing.get("strategy") or {}).get("label"),
            translated=search_query != question,
        )

        # 7.5 生成后兜底：模型自己说答不了 → 转成结构化的"库里没有"。
        #     不进这一步，界面只会显示一句"无法回答。"——用户看不出是库里没材料、
        #     还是系统出错，也没有下一步动作（12 号任务要求每类状态都给下一步）。
        if self._is_model_declined(built):
            empty = uncertainty_lib.declined_state(
                question=question, search_text=search_query, plan_type=plan.type,
                sources=sources, corpus=corpus,
                strategy=(routing.get("strategy") or {}).get("label"),
                translated=search_query != question,
                top_raw=top_raw, threshold=threshold,
                evidence=(built.get("answer") or "").strip(),
            )
            reason = uncertainty_lib.reason_text(empty)
            conf = confidence_lib.grade_answer(
                [], sources=sources, query_type=plan.type, top_raw_score=top_raw,
                reject_threshold=threshold, retrieval_weak=weak,
                rejected=True, reject_reason=reason,
            )
            # 空的判定是这一轮才成立的：把它并回 states / checked，
            # 前端只认 uncertainty.empty，不用再加一套字段。
            unc = dict(unc)
            unc["empty"] = empty
            unc["states"] = ["empty"] + [s for s in (unc.get("states") or []) if s != "empty"]
            unc["checked"] = {**(unc.get("checked") or {}), "empty": True}

            record = self._build_provenance(
                question, "", True, reason, sources, [], built["ref_map"], routing,
                built["refs"], confidence=conf, uncertainty=unc,
            )
            key = self._save_provenance(record)
            return {
                "answer": "",
                "rejected": True,
                "reject_reason": reason,
                "sources": [s.model_dump() for s in sources],
                "invalid_citations": [],
                "provenance_key": key,
                "routing": routing,
                "claims": built["claims"],
                "answer_refs": [],
                "anchor_index": {cid: doc for cid, doc in anchors_map.items() if doc},
                "claim_stats": built["claim_stats"],
                "confidence": conf,
                "uncertainty": unc,
            }

        # 8. provenance 日志
        record = self._build_provenance(
            question, built["answer"], False, None, sources, built["invalid"],
            built["ref_map"], routing, built["refs"], confidence=built["confidence"],
            uncertainty=unc,
        )
        key = self._save_provenance(record)

        return {
            "answer": built["answer"],
            "rejected": False,
            "reject_reason": None,
            "sources": [s.model_dump() for s in sources],
            "invalid_citations": built["invalid"],
            "provenance_key": key,
            "routing": routing,
            "answer_refs": built["refs"],
            "anchor_index": {cid: doc for cid, doc in anchors_map.items() if doc},
            "claims": built["claims"],
            "claim_stats": built["claim_stats"],
            "confidence": built["confidence"],
            "uncertainty": unc,
        }

    # ---------------- 生成后的"模型自述答不了" ----------------
    # 只认开头这一小撮句式：模型说"无法回答/没有提供/根据给定片段"时，
    # 用词高度一致；而一个真答案几乎不会以这些词开头。
    _DECLINE_HEAD = re.compile(
        r"^(无法回答|无法确定|无法判断|无法给出|没有找到|没有提供|没有提及|未提供|未提及"
        r"|抱歉|很抱歉|对不起|根据(现有|给定|以上|提供|所给))"
    )

    @classmethod
    def _is_model_declined(cls, built: Dict[str, Any]) -> bool:
        """
        生成后兜底：一条 claim 都没过校验，且正文本身就是一句拒答 → 按"库里没有"呈现。

        为什么不在生成前拦：判不出来（词频与精排分数在评测集上都分不开，见
        `uncertainty.declined_state` 的注释）。生成后判最准：模型已经读过这 5 段，
        它说没有，就是没有。

        为什么要转换而不是原样返回"无法回答。"：那句话既没说明"库里有什么"，
        也没给下一步，用户看到的就是"这系统答不出来"。转成空检索卡后，
        同一屏里会出现检索范围、关键词命中情况和改写建议。
        """
        claims = built.get("claims") or []
        if any(c.get("status") == "supported" for c in claims):
            return False
        text = (built.get("answer") or "").strip()
        if not text:
            return True  # 空正文也算：既然没有任何一句有证据，留着空白没意义
        if not claims:
            return False
        head = text.lstrip("#>*-• \n\t")[:20]
        return bool(cls._DECLINE_HEAD.match(head))

    # ---------------- 检索强弱 ----------------
    @staticmethod
    def _is_weak(route) -> bool:
        """
        "检索分偏低"的唯一判据，两处（一次性/流式）都走它。

        看的是**重排前的原始融合分**：`score` 被归一化过（最大值恒为 1），
        拿它判高低会永远判"有把握"——这个坑在 03 踩过一次，留在这里当注释。
        """
        threshold = route.reject_threshold
        top_raw = (route.trace.get("recall") or {}).get("top_raw_score")
        return threshold is not None and top_raw is not None and top_raw < threshold

    # ---------------- 闲聊直答 ----------------
    def _answer_direct(
        self, question: str, plan, top_k: int, rrf_k: int
    ) -> Dict[str, Any]:
        """
        闲聊类：不检索、不带引用。
        但仍然走一次 route()，为的是让 trace 结构和别的类型完全一致——
        前端不需要为"没有检索"写第二套展示逻辑。
        """
        route = self.rag.route(question, plan=plan, top_k=top_k, rrf_k=rrf_k)
        try:
            answer = self.llm.chat(
                "你是 AI4S 学术研究助手，可以检索已入库论文并给出带引用的答案。"
                f"用户现在只是打招呼或闲聊，请简短友好地回应，并提示你可以帮他查论文。\n\n用户：{question}",
                system_key="qa_system",
            )
        except Exception:
            answer = "你好，我是 AI4S 学术研究助手。可以问我已入库论文里的具体问题，回答会附上原文页码。"
        record = self._build_provenance(
            question, answer, False, None, [], [], {}, route.trace,
            uncertainty=uncertainty_lib.none_payload(),
        )
        key = self._save_provenance(record)
        # 闲聊不涉及文献证据：三档置信对它不适用。这里显式给 applicable=false，
        # 而不是硬塞一个 verified —— 否则"已验证"这个词会被闲聊稀释掉。
        conf = confidence_lib.grade_answer([], sources=[], query_type=plan.type, applicable=False)
        return {
            "answer": answer,
            "rejected": False,
            "reject_reason": None,
            "sources": [],
            "invalid_citations": [],
            "provenance_key": key,
            "routing": route.trace,
            "claims": [],
            "answer_refs": [],
            "claim_stats": confidence_lib.summarize([]),
            "confidence": conf,
            # 三类不确定性状态对它同样不适用（没有检索、没有证据），
            # 但字段仍在，且 checked 里明写"没检测"——而不是给一个看起来正常的空状态。
            "uncertainty": uncertainty_lib.none_payload(),
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

    # 不同意图的回答方式不同，用一句话把要求点明，比让模型自己猜更稳：
    # 点查要直给数值，对比要逐点对齐，定位要指到页，综述要成条。
    _TYPE_HINTS = {
        QueryType.POINT.value: (
            "这是点查题：直接给出具体数值/配置，不要把数值模糊成\"约\"或\"较多\"。"
            "片段已按相关性排序，请优先采用排序最靠前、且明确写出「参数 = 取值」的那一条；"
            "同一个符号在不同论文里可能指不同物理量（例如 β2 在优化器里是二阶矩衰减率，"
            "在光纤里是色散系数），不要混用不同片段的取值。"
        ),
        QueryType.COMPARE.value: "这是跨文献对比题：片段已按论文成对给出，请逐点对照，明确指出谁更优、差异在哪。",
        QueryType.NAV.value: "这是原文定位题：请先说明这句话出现在哪篇论文的哪一页/哪一段，再引用原句。",
        QueryType.SURVEY.value: "这是综述题：请分条归纳，每条都要有出处，不要把同一篇论文的同一节重复展开。",
    }

    @classmethod
    def _qa_prompt(cls, question: str, context: str, query_type: str | None = None) -> str:
        hint = cls._TYPE_HINTS.get(query_type or "", "")
        return (
            "你是一位严谨的科研助手。请严格基于下面提供的编号片段回答问题。"
            "每写一句话，必须在句末用 [引#N] 标注来源，N 为片段编号。"
            "如果片段中找不到答案，请只回答“无法回答”。"
            "禁止使用片段以外的知识，禁止编造引用。\n"
            f"{hint}\n\n"
            f"{context}\n\n问题：{question}\n\n请回答："
        )

    @staticmethod
    def _load_anchors(sources: List[RetrievalResult]) -> Dict[str, Any]:
        """
        批量取回本次作答涉及的 chunk 锚点。取不到就是空 dict——
        锚点缺失只影响高亮精度，绝不能让它把一次正常的问答变成 500。
        """
        try:
            return ChunkRepository.anchors_map([s.chunk_id for s in sources])
        except Exception:
            return {}

    def _apply_protocol(
        self,
        raw_answer: str,
        sources: List[RetrievalResult],
        anchors_map: Dict[str, Any] | None = None,
        *,
        query_type: str | None = None,
        top_raw_score: float | None = None,
        reject_threshold: float | None = None,
        retrieval_weak: bool = False,
    ) -> Dict[str, Any]:
        """
        一次性答案走与流式完全相同的协议层：摘角标 → 逐句校验 → 三档定档。

        为什么不再写第二套正则替换：
        05/06 之前 `/qa/ask` 与 `/qa/stream` 各有一套"校验 + 选句"逻辑，
        结果是同一道题两个接口给出的锚点不一样（流式能落到 s27，一次性只能到页）。
        07 又要在这里加置信判定，再分叉一次就是三处口径。所以直接复用
        `answer.build_answer`：它有完整的 span 信息，能保证两条链路逐字一致。

        返回的 `answer` 是**带完整出处**的形态（`[引#1: 标题 p.7]`），
        与旧行为一致；`clean` 是纯正文，与流式的 done.answer 同构。
        """
        filt = answer_lib.CitationFilter()
        filt.feed(raw_answer or "")
        filt.flush()
        built = answer_lib.build_answer(
            filt.text, filt.marks, sources, anchors_map, settings.claim_min_evidence,
            query_type=query_type, top_raw_score=top_raw_score,
            reject_threshold=reject_threshold, retrieval_weak=retrieval_weak,
        )
        refs = built["refs"]
        # 越界编号在 `_evidence` 里被显式置为 chunk_id=None，是唯一可靠的判据
        invalid = [str(r["index"]) for r in refs if r["chunk_id"] is None]
        ref_map = {f"[引#{r['index']}]": r["chunk_id"] for r in refs if r["chunk_id"]}
        return {
            "answer": built["annotated"],
            "clean": filt.text,
            "claims": built["claims"],
            "refs": refs,
            "invalid": invalid,
            "ref_map": ref_map,
            "claim_stats": built["claim_stats"],
            "confidence": built["confidence"],
        }

    def _build_provenance(
        self,
        query: str,
        answer: str,
        rejected: bool,
        reject_reason: str | None,
        sources: List[RetrievalResult],
        invalid_citations: List[str],
        ref_map: Dict[str, str],
        routing: Dict[str, Any] | None = None,
        answer_refs: List[Dict[str, Any]] | None = None,
        confidence: Dict[str, Any] | None = None,
        uncertainty: Dict[str, Any] | None = None,
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
            routing=routing or {},
            answer_refs=answer_refs or [],
            confidence=confidence or {},
            uncertainty=uncertainty or {},
        )

    def _save_provenance(self, record: ProvenanceRecord) -> str:
        """把 provenance 记录写入 MinIO provenance bucket，返回 key。"""
        key = f"qa/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}.json"
        data = record.model_dump_json(indent=2).encode("utf-8")
        self.storage.upload_provenance(key, data, content_type="application/json")
        return key
