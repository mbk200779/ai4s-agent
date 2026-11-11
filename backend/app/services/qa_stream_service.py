"""
流式问答服务：把「检索 → 生成 → 校验」拆成一条 SSE 事件序列。

为什么要把执行放到后台线程、用队列往外吐事件：
检索+精排是**同步阻塞**的（ONNX、Qdrant、HTTP 全都有），而事件必须"发生即推送"。
如果在同一个生成器里写完再 yield，前端拿到的仍然是"卡 2 秒 → 一次性刷出全部 stage"，
那就不叫流式，只是换了个传输格式。所以 work() 在后台线程里跑，
每到一个真实边界就往队列里放一条事件，消费端边收边发。

事件序列（与前端 `src/types/stream.ts` 严格对应）：
    stage(understand|retrieve|rerank|generate) × start/done
    retrieval（命中片段摘要）
    token × N（正文增量，**不含任何角标**）
    claim × N（结构化主张 + evidence，校验完才发）
    done（汇总：正文、claims、citations、锚点索引、timings、trace_key）
可选：error（生成中断，带已产出的部分正文）

三条硬约束的落点：
1. token 流不得出现未校验角标 → `answer.CitationFilter` 在服务端把 `[引#N]` 摘掉，
   角标只随 claim 事件下发，前端按 span 回填；
2. 证据不足的 claim 必须保留 → `answer.build_answer` 对每条 claim 标 supported/unsupported
   并附 reason，unsupported 的照样发、照样进 done，前端只能标记不能删除；
3. 每个事件都带 trace_id → `_Emitter` 统一注入 trace_id/seq/ts/elapsed_ms，
   并在结束时把整条链路落到 MinIO（qa/stream/<日期>/<trace_id>.jsonl）供回放。
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Iterator

from app.core.config import settings
from app.schemas.provenance import ProvenanceRecord, ProvenanceSource
from app.services.llm_client import LLMClient
from app.services.qa_service import QAService
from app.services.rag import answer as answer_lib
from app.services.rag import confidence as confidence_lib
from app.services.rag import uncertainty as uncertainty_lib
from app.services.rag.retrieve import RAGRetriever
from app.services.rag.taxonomy import QueryType, label_of
from app.services.retrieval_service import RetrievalResult

# 阶段中文名只在前端用一次（进度条文案），但服务端也带一份：
# 前端若按 stage 枚举自己维护文案，新增阶段就得改两处。
STAGE_LABELS = {
    "understand": "理解问题",
    "retrieve": "检索",
    "rerank": "精排",
    "generate": "生成",
}

# 每个事件都必须带 trace_id：这是"答辩时回放整条链路"的唯一锚点
_HEARTBEAT = "__heartbeat__"
_END = "__end__"


class _Emitter:
    """事件工厂：统一注入 trace_id / seq / 时间戳，并保留一份完整日志供归档。"""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.started = time.perf_counter()
        self.seq = 0
        self.log: list[dict[str, Any]] = []

    def make(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        payload = {
            "trace_id": self.trace_id,
            "seq": self.seq,
            "ts": int(time.time() * 1000),
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 1),
            **data,
        }
        self.log.append({"seq": self.seq, "event": kind, "data": payload})
        return payload

    def heartbeat(self) -> dict[str, Any]:
        """心跳不进日志：它只用于保活，混进归档会让回放里出现一堆无意义的空事件。"""
        return {
            "trace_id": self.trace_id,
            "seq": self.seq,
            "ts": int(time.time() * 1000),
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 1),
        }


class QAStreamService:
    """SSE 问答的编排层。复用 QAService 的检索/锚点/prompt 装配，避免两套口径。"""

    def __init__(self) -> None:
        # QAService 里已经装好了 retriever / llm / storage，直接借；
        # 另建一套会重复加载 embedding 与 reranker（两个 2GB 级模型）。
        self.qa = QAService()
        self.router = self.qa.router
        self.rag: RAGRetriever = self.qa.rag
        self.llm: LLMClient = self.qa.llm
        self.storage = self.qa.storage

    # ---------------- 对外的流 ----------------
    def stream(
        self,
        question: str,
        top_k: int = 8,
        rrf_k: int = 60,
        force_type: str | None = None,
        rerank: bool | None = None,
        numeric: bool | None = None,
        project_id: str | None = None,
        index_version: str | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """
        产出 `(event_name, payload)`，由出口层转成 SSE 帧。

        消费端提前断开（浏览器关页面）时，生成器会被 close()，
        这里用 stop 事件通知后台线程尽快收尾——否则一次没人看的生成
        还会把整段答案跑完，白烧 token。
        """
        trace_id = f"tr_{uuid.uuid4().hex[:16]}"
        emitter = _Emitter(trace_id)
        q: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
        stop = threading.Event()

        def emit(kind: str, **data: Any) -> None:
            q.put((kind, emitter.make(kind, data)))

        def work() -> None:
            try:
                self._run(
                    emit, emitter, stop, question, top_k, rrf_k, force_type, rerank, numeric,
                    project_id, index_version,
                )
            except Exception as exc:  # noqa: BLE001 —— 任何异常都要变成事件，不能让连接挂着
                emit("error", message=f"{type(exc).__name__}: {exc}"[:400], stage="unknown")
            finally:
                q.put((_END, {}))

        threading.Thread(target=work, name=f"qa-stream-{trace_id}", daemon=True).start()

        try:
            while True:
                try:
                    kind, payload = q.get(timeout=settings.stream_heartbeat_s)
                except queue.Empty:
                    # 检索+精排可能十几秒没有事件，反代会把静默连接掐掉。
                    # 心跳走注释行（出口层负责转成 `: ping`），不占用事件序列。
                    yield _HEARTBEAT, emitter.heartbeat()
                    continue
                if kind == _END:
                    break
                yield kind, payload
        finally:
            stop.set()

    # ---------------- 后台线程里的真实执行 ----------------
    def _run(
        self,
        emit,
        emitter: _Emitter,
        stop: threading.Event,
        question: str,
        top_k: int,
        rrf_k: int,
        force_type: str | None,
        rerank: bool | None,
        numeric: bool | None,
        project_id: str | None = None,
        index_version: str | None = None,
    ) -> None:
        timings: dict[str, float] = {}
        t0 = time.perf_counter()

        # ---- 1. 理解 ----
        emit("stage", stage="understand", phase="start", label=STAGE_LABELS["understand"])
        plan = self.router.analyze(question)
        search_query, translated = question, None
        if re.search(r"[\u4e00-\u9fff]", question):
            try:
                translated = self.llm.chat(
                    f"Translate this research question to English:\n\n{question}",
                    system_key="translate_system",
                    model=settings.litellm_cheap_model,
                ).strip()
                if translated:
                    search_query = translated
            except Exception:
                translated = None  # 翻译失败退回原句，不影响后续
        timings["understand_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        emit(
            "stage", stage="understand", phase="done", label=STAGE_LABELS["understand"],
            ms=timings["understand_ms"],
            detail={
                "type": plan.type,
                "type_label": label_of(plan.type),
                "confidence": plan.confidence,
                "classifier": plan.method,
                "slots": plan.slots,
                "search_text": search_query,
                "translated": bool(translated),
            },
        )

        # ---- 闲聊：不检索、不出 claim ----
        if plan.type == QueryType.CHITCHAT.value:
            self._direct(emit, emitter, stop, question, timings, search_query)
            return

        # ---- 2. 检索 + 3. 精排（阶段事件由 route 内部真实触发）----
        def stage_cb(name: str, info: dict[str, Any]) -> None:
            detail = {k: v for k, v in info.items() if k not in ("phase", "ms")}
            emit(
                "stage", stage=name, phase=info.get("phase", "done"),
                label=STAGE_LABELS.get(name, name), ms=info.get("ms"), detail=detail,
            )

        t1 = time.perf_counter()
        route = self.rag.route(
            question, search_text=search_query, top_k=top_k, rrf_k=rrf_k, plan=plan,
            force_type=force_type, rerank=rerank, numeric=numeric, on_stage=stage_cb,
            project_id=project_id, index_version=index_version,
        )
        timings["route_ms"] = round((time.perf_counter() - t1) * 1000, 1)
        sources: list[RetrievalResult] = route.results
        routing = route.trace
        timings["rerank_ms"] = float((routing.get("rerank") or {}).get("latency_ms") or 0.0)

        # ---- 4. 不确定性状态（12）：空检索会改变"要不要生成"，所以放在最前面 ----
        #    判定必须在服务端：前端只有 top-k 片段，看不到"库内几篇、关键词命中几次"。
        #    冲突要等 claim 出来才判得了，这里先发一份 partial（空态 + 未识别），
        #    done 里再给完整版——两处同构，前者只是"更早"，不是"另一套"。
        reject_reason = self._reject_reason(route)
        retrieval_weak = self._retrieval_weak(route)
        top_raw = (routing.get("recall") or {}).get("top_raw_score")
        anchors_map = self.qa._load_anchors(sources)
        emit(
            "retrieval", count=len(sources), chunks=_chunk_brief(sources, anchors_map),
            top_raw_score=top_raw,
            reject_threshold=route.reject_threshold, type=plan.type,
            papers=(routing.get("recall") or {}).get("papers"),
            strategy=(routing.get("strategy") or {}).get("label"),
            retrieval_weak=retrieval_weak,
        )

        corpus = uncertainty_lib.corpus_of(self.rag.retrieval.vector_store)
        unc = uncertainty_lib.build(
            question=question, search_text=search_query, plan_type=plan.type,
            sources=sources, anchors_map=anchors_map, claims=[], refs=[],
            retrieval_weak=retrieval_weak, top_raw=top_raw,
            threshold=route.reject_threshold, corpus=corpus,
            strategy=(routing.get("strategy") or {}).get("label"),
            translated=bool(search_query != question),
        )
        emit("uncertainty", **unc, partial=True)

        if unc["empty"] or reject_reason:
            if unc["empty"]:
                reject_reason = uncertainty_lib.reason_text(unc["empty"])
            timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self._done(
                emit, emitter, question, "",
                {"claims": [], "refs": [], "annotated": "", "unsupported_claims": []},
                sources, anchors_map, routing, timings,
                rejected=True, reject_reason=reject_reason, plan_type=plan.type,
                search_query=search_query, retrieval_weak=retrieval_weak,
                reject_threshold=route.reject_threshold, uncertainty=unc,
            )
            return

        # ---- 5. 生成：token 流过过滤器，角标一律摘掉 ----
        context = QAService._format_context(sources)
        prompt = QAService._qa_prompt(question, context, plan.type)
        emit("stage", stage="generate", phase="start", label=STAGE_LABELS["generate"],
             detail={"chunks": len(sources), "context_chars": len(context)})
        filt = answer_lib.CitationFilter()
        buffered: list[str] = []
        t2 = time.perf_counter()
        first_token_ms: float | None = None

        for delta in self.llm.chat_stream(prompt, system_key="qa_system"):
            if stop.is_set():
                break
            buffered.append(delta)
            clean = filt.feed(delta)
            if clean:
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - t2) * 1000, 1)
                emit("token", text=clean)
        tail = filt.flush()
        if tail:
            emit("token", text=tail)

        timings["generate_ms"] = round((time.perf_counter() - t2) * 1000, 1)
        timings["ttft_ms"] = first_token_ms or 0.0
        raw_answer = "".join(buffered)
        emit(
            "stage", stage="generate", phase="done", label=STAGE_LABELS["generate"],
            ms=timings["generate_ms"],
            detail={
                "chars": len(filt.text),
                "raw_chars": len(raw_answer),
                "stripped_markers": len(filt.marks),
                "ttft_ms": timings["ttft_ms"],
            },
        )

        # ---- 6. 逐句校验 + 三档定档 → claim 事件（角标在这里才第一次出现）----
        built = answer_lib.build_answer(
            filt.text, filt.marks, sources, anchors_map, settings.claim_min_evidence,
            query_type=plan.type, top_raw_score=top_raw,
            reject_threshold=route.reject_threshold, retrieval_weak=retrieval_weak,
        )
        for claim in built["claims"]:
            emit("claim", **claim)

        # 冲突要等 claim 出来才判得了（"两句方向相反"是回答的属性，不是片段的属性）；
        # 未识别也要看"引用到了哪一段"才知道严重性。所以这里重算一次，
        # done 里的这份是权威版，前面那个 uncertainty 事件只是"更早的那半份"。
        unc = uncertainty_lib.build(
            question=question, search_text=search_query, plan_type=plan.type,
            sources=sources, anchors_map=anchors_map,
            claims=built["claims"], refs=built["refs"],
            retrieval_weak=retrieval_weak, top_raw=top_raw,
            threshold=route.reject_threshold, corpus=corpus,
            strategy=(routing.get("strategy") or {}).get("label"),
            translated=bool(search_query != question),
        )

        timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        # 6.5 生成后兜底：正文是一句"无法回答"、且没有一条 claim 过校验 →
        #     换成语义更明确的空检索卡（检索范围 + 关键词命中 + 改写建议）。
        #     正文传空串：前端见到 uncertainty.empty 就不渲染正文，
        #     否则屏幕上会同时出现"无法回答。"和"库里没有"，两句话说同一件事。
        if QAService._is_model_declined(built):
            empty = uncertainty_lib.declined_state(
                question=question, search_text=search_query, plan_type=plan.type,
                sources=sources, corpus=corpus,
                strategy=(routing.get("strategy") or {}).get("label"),
                translated=bool(search_query != question),
                top_raw=top_raw, threshold=route.reject_threshold,
                evidence=filt.text,
            )
            reason = uncertainty_lib.reason_text(empty)
            unc = dict(unc)
            unc["empty"] = empty
            unc["states"] = ["empty"] + [s for s in (unc.get("states") or []) if s != "empty"]
            unc["checked"] = {**(unc.get("checked") or {}), "empty": True}
            self._done(
                emit, emitter, question, "", built, sources, anchors_map, routing,
                timings, rejected=True, reject_reason=reason, plan_type=plan.type,
                search_query=search_query, retrieval_weak=retrieval_weak,
                reject_threshold=route.reject_threshold, uncertainty=unc,
            )
            return

        self._done(
            emit, emitter, question, filt.text, built, sources, anchors_map, routing,
            timings, rejected=False, reject_reason=None, plan_type=plan.type,
            search_query=search_query, retrieval_weak=retrieval_weak, uncertainty=unc,
        )

    # ---------------- 分支：闲聊直答 ----------------
    def _direct(self, emit, emitter, stop, question: str, timings: dict, search_query: str) -> None:
        emit("stage", stage="generate", phase="start", label=STAGE_LABELS["generate"])
        t = time.perf_counter()
        text = ""
        try:
            for delta in self.llm.chat_stream(
                "你是 AI4S 学术研究助手，可以检索已入库论文并给出带引用的答案。"
                f"用户现在只是打招呼或闲聊，请简短友好地回应，并提示你可以帮他查论文。\n\n用户：{question}",
                system_key="qa_system",
            ):
                if stop.is_set():
                    break
                text += delta
                emit("token", text=delta)
        except Exception:
            text = text or "你好，我是 AI4S 学术研究助手。可以问我已入库论文里的具体问题。"
        timings["generate_ms"] = round((time.perf_counter() - t) * 1000, 1)
        timings["total_ms"] = round(sum(timings.values()), 1)
        emit("stage", stage="generate", phase="done", label=STAGE_LABELS["generate"],
             ms=timings["generate_ms"], detail={"chars": len(text), "direct": True})
        self._done(
            emit, emitter, question, text,
            {"claims": [], "refs": [], "annotated": text, "unsupported_claims": []},
            [], {}, {}, timings, rejected=False, reject_reason=None,
            plan_type=QueryType.CHITCHAT.value, search_query=search_query,
            applicable=False, uncertainty=uncertainty_lib.none_payload(),
        )

    # ---------------- 拒答口径 ----------------
    @staticmethod
    def _reject_reason(route) -> str | None:
        """
        什么时候"完整拒答"：只有一条都没检索到。

        为什么把"分低"从拒答里拿掉（07）：用户看到"无法回答"时分不清是
        "库里没有"还是"检索没找到好片段"。前者是事实，后者是系统能力问题。
        现在分低的情况继续作答，把结论标成 needs_check 并注明"检索融合分偏低"——
        用户至少能看到系统找到了什么、为什么它觉得不够。
        `settings.reject_on_low_score` 保留旧口径，供 A/B 对比"拒答率"这个指标。

        12 起这里已经退居二线：真正的"库里没有"由 `uncertainty.empty` 判定并给出
        完整解释（检索范围、关键词命中表、改写建议）。这里只剩两句兜底文案，
        而且刻意都不写"无法回答"——那句话把三种情况压成一种，是这个任务要拆掉的东西。
        """
        if not route.results:
            return "库内没有检索到可用片段。"
        if settings.reject_on_low_score and QAService._is_weak(route):
            threshold = route.reject_threshold
            top_raw = (route.trace.get("recall") or {}).get("top_raw_score")
            return f"检索结果置信度过低（原始分 {top_raw:.4f} < {threshold}），这次不作答。"
        return None

    @staticmethod
    def _retrieval_weak(route) -> bool:
        """检索分是否低于该策略阈值。它不再决定"答不答"，只决定"标几档"。"""
        return QAService._is_weak(route)

    # ---------------- 汇总事件 ----------------
    def _done(
        self,
        emit,
        emitter: _Emitter,
        question: str,
        text: str,
        built: dict[str, Any],
        sources: list[RetrievalResult],
        anchors_map: dict[str, Any],
        routing: dict[str, Any],
        timings: dict[str, float],
        *,
        rejected: bool,
        reject_reason: str | None,
        plan_type: str,
        search_query: str,
        retrieval_weak: bool = False,
        applicable: bool = True,
        reject_threshold: float | None = None,
        uncertainty: dict[str, Any] | None = None,
    ) -> None:
        refs = built.get("refs") or []
        claims = built.get("claims") or []
        annotated = built.get("annotated") or text
        anchor_index = {cid: doc for cid, doc in (anchors_map or {}).items() if doc}
        invalid = [str(r["index"]) for r in refs if not r.get("chunk_id")]
        top_raw = (routing.get("recall") or {}).get("top_raw_score")
        # 拒答路径没有 claims，需要在这里补一份"为什么是 unsupported"的分档结果；
        # 正常路径直接用 build_answer 算好的那份（口径与 /qa/ask 完全一致）。
        overall = built.get("confidence") or confidence_lib.grade_answer(
            claims, sources=sources, query_type=plan_type, top_raw_score=top_raw,
            reject_threshold=reject_threshold, retrieval_weak=retrieval_weak,
            rejected=rejected, reject_reason=reject_reason, applicable=applicable,
        )

        payload: dict[str, Any] = {
            "answer": text,
            "answer_annotated": annotated,
            "claims": claims,
            "answer_refs": refs,
            "citations": _citations(sources, refs, anchor_index),
            "anchor_index": anchor_index,
            "invalid_citations": invalid,
            "rejected": rejected,
            "reject_reason": reject_reason,
            "type": plan_type,
            "search_text": search_query,
            # 完整分流 trace 与 /qa/ask 同构：界面上"这次为什么召回这几段"
            # 仍然可展开，不至于因为换成流式就丢掉可解释性。
            "routing": routing,
            "timings": timings,
            "claim_stats": built.get("claim_stats") or answer_lib.summarize(claims),
            "confidence": overall,
            # 12：三类不确定性状态的权威版（与最早那个 uncertainty 事件同构，多了冲突）
            "uncertainty": uncertainty or uncertainty_lib.none_payload(),
            "provenance_key": None,
            "trace_key": None,
        }

        # provenance：与 /qa/ask 同一套记录，多一个 trace_id 把两条链路对上
        try:
            ref_map = {f"[引#{r['index']}]": r["chunk_id"] for r in refs if r.get("chunk_id")}
            record = ProvenanceRecord(
                query=question,
                answer=annotated,
                rejected=rejected,
                reject_reason=reject_reason,
                sources=[
                    ProvenanceSource(
                        chunk_id=s.chunk_id, paper_id=s.paper_id, arxiv_id=s.arxiv_id,
                        version=s.version, title=s.title, kind=s.kind, page=s.page,
                        paragraph=s.paragraph, bbox=s.bbox, text=s.text, score=s.score,
                    )
                    for s in sources
                ],
                invalid_citations=invalid,
                answer_ref_map=ref_map,
                routing=routing,
                answer_refs=refs,
                trace_id=emitter.trace_id,
                confidence=overall,
                uncertainty=uncertainty or {},
            )
            payload["provenance_key"] = self.qa._save_provenance(record)
        except Exception:
            # 归档失败不能把已经生成好的答案变成错误页
            payload["provenance_key"] = None

        # 事件日志：把 done 也写进去，但 trace_key 自引用不了，所以先落盘再回填
        emitter.log.append(
            {"seq": emitter.seq + 1, "event": "done", "data": {**payload, "ts": int(time.time() * 1000)}}
        )
        key = self._dump_log(emitter)
        payload["trace_key"] = key
        emit("done", **payload)

    def _dump_log(self, emitter: _Emitter) -> str | None:
        """
        把整条事件流写成 JSONL 归档，返回对象存储 key。

        为什么单独存一份而不是只靠 provenance：
        provenance 是"结果"，事件流是"过程"——阶段耗时、精排降级、
        被摘掉几个角标，这些只存在于事件里。答辩现场要回放链路，缺的是过程。
        """
        if not settings.stream_log_enabled:
            return None
        key = f"qa/stream/{datetime.utcnow().strftime('%Y/%m/%d')}/{emitter.trace_id}.jsonl"
        body = "\n".join(json.dumps(rec, ensure_ascii=False) for rec in emitter.log)
        try:
            self.storage.upload_provenance(
                key, body.encode("utf-8"), content_type="application/x-ndjson"
            )
            return key
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 摘要工具
# ---------------------------------------------------------------------------
def _chunk_brief(
    sources: list[RetrievalResult], anchors_map: dict[str, Any]
) -> list[dict[str, Any]]:
    """检索事件里的片段摘要：够前端画"命中了什么"，不塞全文。"""
    out = []
    for i, s in enumerate(sources, 1):
        doc = anchors_map.get(s.chunk_id) or {}
        out.append(
            {
                "index": i,
                "chunk_id": s.chunk_id,
                "arxiv_id": s.arxiv_id,
                "title": s.title,
                "page": s.page,
                "kind": s.kind,
                "score": round(float(s.score or 0.0), 4),
                "rerank_score": (round(float(s.rerank_score), 4) if s.rerank_score is not None else None),
                "hit_source": s.hit_source,
                "has_anchors": bool(doc.get("items")),
                "preview": (s.text or "")[:160],
            }
        )
    return out


def _citations(
    sources: list[RetrievalResult], refs: list[dict[str, Any]], anchor_index: dict[str, Any]
) -> list[dict[str, Any]]:
    """片段级引用清单（chunk 去重），与 /qa/ask 的 citations 字段同构。"""
    first: dict[str, dict[str, Any]] = {}
    for r in refs:
        if r.get("chunk_id"):
            first.setdefault(r["chunk_id"], r)
    out = []
    for s in sources:
        hit = first.get(s.chunk_id)
        out.append(
            {
                "paper_id": s.paper_id,
                "arxiv_id": s.arxiv_id,
                "title": s.title,
                "chunk_id": s.chunk_id,
                "chunk_text": s.text,
                "page": s.page,
                "paragraph": s.paragraph,
                "bbox": s.bbox,
                "score": s.score,
                "rerank_score": s.rerank_score,
                "hit_source": s.hit_source,
                "ref_id": hit.get("ref_id") if hit else None,
                "precision": hit.get("precision") if hit else None,
                "has_anchors": s.chunk_id in anchor_index,
            }
        )
    return out
