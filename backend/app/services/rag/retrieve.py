"""
检索分流入口：把 QueryPlan 派发到五条策略之一，再把候选交给规则召回与精排，最后写 trace。

这一层是"路由 + 后处理"本身。它只做五件事：
  1. 调用 QueryRouter 拿到类型与槽位（或接受外部已算好的 plan，便于评测与调试）；
  2. 按类型派发到对应策略，并把语料画像按需加载；
  3. 数值/公式类问句额外走一次规则召回（正则扫描，命中表格单元格优先）；
  4. 把候选（top N=20）交给 cross-encoder 精排，取前 5；精排失败则原样落地；
  5. 组装 trace —— 分类依据、策略参数、规则探针、每一跳、精排前后位移，全部可见。

trace 是这一版的"验收物"：没有它，"精排到底把什么提上来了"就只能靠猜。
"""
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.core.config import settings
from app.services import index_version as index_version_lib
from app.services.rag import numeric_rules
from app.services.rag.query import QueryPlan, QueryRouter
from app.services.rag.rerank import Reranker, index_snapshot_id
from app.services.rag.strategies import (
    StrategyResult,
    load_corpus,
    run_chitchat,
    run_compare,
    run_generic,
    run_nav,
    run_point,
    run_survey,
)
from app.services.rag.taxonomy import (
    NUMERIC_PAPER_SCOPE,
    RERANK_MODE_BY_TYPE,
    RERANK_TOP_N,
    QueryType,
    label_of,
    strategy_of,
)
from app.services.retrieval_service import RetrievalResult, RetrievalService

# 单路检索时，"低置信"的等价线：该路命中前 5 名以内才不算低置信。
# 双路阈值的语义是"两路都在前 5 名附近"（2/(60+5)≈0.031，与 RRF_MIN_SCORE=0.03 同量级），
# 单路没有第二票可投，就把同一个名次要求压到一路的分母上。
SINGLE_CHANNEL_RANK_EQUIV = 5


@dataclass
class RouteResult:
    """一次分流的完整结果。"""

    results: list[RetrievalResult]
    trace: dict[str, Any]
    direct: bool = False
    # 这一次实际读的索引版本（含 collection 与命中规则）。
    # 单独留一份属性，是因为问答层要用它去加载"同一个版本"的语料快照——
    # 从 trace 里反查会把"约定"变成"解析字符串"。
    index: dict[str, Any] | None = None

    @property
    def reject_threshold(self) -> float | None:
        return self.trace["strategy"].get("reject_threshold")


class RAGRetriever:
    """
    检索分流器。

    与旧 `RetrievalService.search()` 的关系：不是替代，是**包装**。
    generic 策略调用的就是旧路径本身，所以"分流前后"的对比是干净的
    ——基线不是重写出来的另一套代码，而是同一份代码。
    """

    def __init__(
        self,
        retrieval: RetrievalService | None = None,
        router: QueryRouter | None = None,
        reranker: Reranker | None = None,
    ):
        self.retrieval = retrieval or RetrievalService()
        # 不在这里取 vector_store.embedder：那会立刻把 2.6GB 的模型加载进来，
        # 而闲聊类请求根本不需要向量。QueryRouter 内部按需构造 EmbedderService，
        # 而 EmbedderService 的模型是类级单例，和 VectorStoreService 用的是同一份，
        # 所以"谁先加载"不影响内存，只影响不该加载的时候要不要加载。
        self.router = router or QueryRouter()
        self.reranker = reranker or Reranker()

    # ---------------- 对外入口 ----------------

    def route(
        self,
        question: str,
        search_text: str | None = None,
        top_k: int = 8,
        rrf_k: int = 60,
        plan: QueryPlan | None = None,
        force_type: str | None = None,
        rerank: bool | None = None,
        numeric: bool | None = None,
        on_stage: Callable[[str, dict[str, Any]], None] | None = None,
        project_id: str | None = None,
        index_version: str | None = None,
        index_route: dict[str, Any] | None = None,
    ) -> RouteResult:
        """
        question    : 用户原问题，用于分类、槽位抽取与**精排打分**
        search_text : 实际拿去检索的文本（中文问题会先翻成英文）。默认同 question
        force_type  : 评测/调试时强制指定类型，跳过分类
        rerank      : 单次覆盖精排开关（A/B 用）。None = 跟随 `RERANK_ENABLED`
        numeric     : 单次覆盖规则召回开关（A/B 用）
        on_stage    : 阶段回调，`(stage, info)`。info 里带 phase=start|done 与计数。

        为什么阶段信息要靠回调而不是让调用方自己切分：
        "检索"与"精排"的边界在 route 内部（策略召回 → 规则注入 → 重排），
        外面看到的只有一次函数调用的总耗时。流式协议要推进度条，
        就必须在这两个边界上真的被通知一次，而不是事后按总耗时去猜比例。
        默认 None —— 评测脚本与旧调用方完全不受影响。

        为什么分类/精排用原句、检索用译文：规则与精排模型都能读中文，
        而正文是英文，检索必须用英文。混成一个参数会让两边都做不好。
        """
        started = time.perf_counter()
        text = search_text or question

        # ---- 索引版本：先定"读哪一版"，再谈检索 ----
        # 放在最前面是因为后面每一步都依赖它：策略召回、规则召回、
        # 语料画像、精排的语料缓存，读的必须是同一个 collection。
        # 一旦有一处读了别的版本，症状是"检索结果对、实体消解错"这类不报错的怪事。
        index = index_route or index_version_lib.resolve(
            project_id=project_id,
            sample_key=question,
            explicit=index_version,
        )
        switched = self.retrieval.vector_store.use_collection(
            index.get("pipeline_version", ""), index.get("collection", "")
        )
        if not switched:
            # 路由指向一个不存在的 collection：不切，但把事实写进 trace。
            # 静默留在旧版会让"灰度生效了没有"永远查不清。
            index = {
                **index,
                "fallback": {
                    "from": index.get("pipeline_version"),
                    "collection": index.get("collection"),
                    "reason": "collection_missing",
                    "to": self.retrieval.vector_store.pipeline_version,
                },
            }

        def fire(stage: str, phase: str, **info: Any) -> None:
            """阶段事件是"锦上添花"：回调自己炸了不能把检索带崩。"""
            if on_stage is None:
                return
            try:
                on_stage(stage, {"phase": phase, **info})
            except Exception:
                pass

        if plan is None:
            plan = self.router.analyze(question)
        if force_type:
            plan = QueryPlan(
                question=plan.question,
                type=force_type,
                confidence=plan.confidence,
                method=f"forced:{force_type}",
                slots=plan.slots,
                rule_hits=plan.rule_hits,
                prototype=plan.prototype,
                latency_ms=plan.latency_ms,
                degraded=False,
                degrade_reason="由调用方指定类型",
                query_vec=plan.query_vec,
            )

        qtype = plan.type
        # 复用分类时算过的向量有个前提：检索文本和分类文本是同一句。
        # 中文问题会先翻成英文再检索，这时 plan.query_vec 编码的是中文原句，
        # 拿它去做英文检索会把跨语言翻译这一步完全抵消掉。
        vec = plan.query_vec if (plan.query_vec and text == plan.question) else None

        rerank_on = settings.rerank_enabled if rerank is None else rerank
        numeric_on = settings.numeric_recall_enabled if numeric is None else numeric
        # 要让精排有的可选，召回侧必须给足候选：N=20 是精排的输入规模。
        # 这也顺带让各策略自己的召回更宽——但输出条数仍由 top_k 决定。
        candidate_k = max(top_k, RERANK_TOP_N) if rerank_on else top_k

        strategy: StrategyResult
        fire("retrieve", "start", strategy=strategy_of(qtype)["name"], candidates=candidate_k)
        if qtype == QueryType.CHITCHAT:
            strategy = run_chitchat()
        elif qtype == QueryType.POINT:
            strategy = run_point(self.retrieval, text, plan.slots, candidate_k, rrf_k, query_vec=vec)
        elif qtype == QueryType.COMPARE:
            corpus = load_corpus(self.retrieval.vector_store)
            strategy = run_compare(self.retrieval, text, plan.slots, candidate_k, rrf_k, corpus, query_vec=vec)
        elif qtype == QueryType.SURVEY:
            strategy = run_survey(self.retrieval, text, candidate_k, rrf_k, query_vec=vec)
        elif qtype == QueryType.NAV:
            corpus = load_corpus(self.retrieval.vector_store)
            strategy = run_nav(self.retrieval, text, plan.slots, candidate_k, rrf_k, corpus, query_vec=vec)
        else:
            strategy = run_generic(self.retrieval, text, candidate_k, rrf_k, query_vec=vec)

        candidates = list(strategy.results)

        # ---- 单路配置下的阈值重标定 ----
        # RRF 的分数是"两路各投一票"的总和；只开一路时，上限天然掉一半以上。
        # 拿双路的尺子去量单路，得到的不是"召回变差"，而是"几乎全部低置信"——
        # 那是量具换了，不是检索能力掉了。基线对比里如果不管这件事，
        # 纯向量/纯 BM25 两组的误拒率会恒等于 100%，比出来的东西没有意义。
        # 单路时改用**名次等价线**：该路命中前 5 名才不算低置信。
        # 生产恒为 hybrid（active_channels 长度为 2），这一段不执行。
        active_channels = self.retrieval.active_channels
        if len(active_channels) == 1 and strategy.reject_threshold is not None:
            weight = float((strategy.params or {}).get(f"{active_channels[0]}_weight", 1.0) or 1.0)
            strategy.reject_threshold = round(weight / (rrf_k + SINGLE_CHANNEL_RANK_EQUIV), 6)
        numeric_res: numeric_rules.NumericRecall | None = None
        injected: list[RetrievalResult] = []

        # ---- 规则召回：只对"问句里真的带了数值探针"的问句生效 ----
        # nav 不走这一步：它本来就在全语料做字面扫描，再加一遍是重复劳动，
        # 而且会把它已经算好的锚点命中排序搅乱。
        if numeric_on and not strategy.direct and qtype != QueryType.NAV:
            probes = numeric_rules.scan_query(question)
            if probes:
                corpus = load_corpus(self.retrieval.vector_store)
                # 论文范围 = 融合候选里已经出现过的论文。符号是多义的（β2 在 Transformer
                # 是 0.98、在光纤孤子论文是 −20 ps/nm/km），规则层没有语义能力分辨，
                # 但融合候选已经把"用户问的是哪几篇"带进来了。跨论文找证据是融合的职责，
                # 规则召回只在已选定的论文内找那个孤立单元格。
                anchor = {r.arxiv_id for r in candidates} if NUMERIC_PAPER_SCOPE else None
                numeric_res = numeric_rules.recall(corpus, probes, paper_ids=anchor)
                have = {r.chunk_id for r in candidates}
                for h in numeric_res.hits:
                    if h.chunk_id in have:
                        # 已在融合候选里：只标注"它同时被规则命中"，不重复注入。
                        # 重复注入会让同一条片段在 trace 里出现两次，A/B 时也算两票。
                        for r in candidates:
                            if r.chunk_id == h.chunk_id:
                                r.hit_source = (r.hit_source or "fusion") + "+rule"
                                break
                    else:
                        injected.append(h)
                candidates = candidates + injected

        fire(
            "retrieve",
            "done",
            ms=strategy.latency_ms,
            hits=len(candidates),
            injected=len(injected),
            papers=len({r.arxiv_id for r in candidates}),
            probes=len(numeric_res.probes) if numeric_res else 0,
        )

        # 拒答看的"绝对相关度"必须在重排之前取：精排只负责换顺序，
        # 不该改变"这次检索到底找没找到东西"这个判断。
        # 反过来（在重排后的 top5 里取 max）会出一个隐蔽 bug：精排把 RRF 分低但语义准的
        # 段落提到第 1，原本 RRF 高的那条被挤出前 5，最高原始分凭空掉到阈值以下 → 误拒答。
        top_raw_pool = max(
            (r.raw_score for r in candidates if r.raw_score is not None), default=None
        )

        # ---- 精排 ----
        mode = RERANK_MODE_BY_TYPE.get(qtype, "flat")
        snapshot = index_snapshot_id(self.retrieval.vector_store) if rerank_on else None
        # 精排用检索文本（中文问句的英文译文）而不是原句：
        # 精排要判断的是"这段正文能不能回答这次检索的意图"，
        # 检索用译文、精排用原句会让两边各说各话；而且默认模型是英文的。
        fire("rerank", "start", mode=mode, candidates=len(candidates), enabled=rerank_on)
        outcome = self.reranker.apply(
            text, candidates, top_k, snapshot=snapshot, mode=mode, enabled=rerank_on,
            source="request" if rerank is not None else ("env" if settings.rerank_enabled else "disabled"),
        )
        fire(
            "rerank", "done",
            applied=outcome.applied,
            degraded=outcome.degraded,
            reason=outcome.degrade_reason,
            cache=outcome.cache,
            moved=len(outcome.moved),
            ms=outcome.latency_ms,
            top_k=top_k,
        )

        if outcome.applied:
            results = outcome.results
        else:
            # 精排没跑（关闭 / 降级）：回到"策略排序 + 规则注入"。
            # flat 类型两种分数都是 0~1，可以按分数合并；
            # coverage 类型（对比/综述）的分数是逐篇归一化的，全局排序会破坏成对结构，
            # 所以保持策略原序，把注入项放在其后。
            if injected and mode == "flat":
                merged = sorted(candidates, key=lambda r: r.score or 0.0, reverse=True)
            else:
                merged = candidates
            results = merged[:top_k]

        # 统一标注来源：融合来的片段原本没有标记，trace 与前端靠这个字段区分
        # "精排把它提上来的" 和 "它本来就来自哪一路"。
        for r in results:
            if not r.hit_source:
                r.hit_source = "fusion"

        # ---- 拒答口径：首条如果是规则召回的"孤立命中"，按证据放行 ----
        # 这种片段没有融合分（raw_score=None），拿阈值去卡它等于用尺子量长度以外的量。
        evidence = (
            results[0].hit_source.startswith("rule") if results and results[0].hit_source else False
        )
        threshold = 0.0 if evidence else strategy.reject_threshold

        total_ms = round((time.perf_counter() - started) * 1000, 1)
        trace = self._build_trace(
            question, text, plan, strategy, outcome, numeric_res, results,
            total_ms, top_k, rrf_k, threshold, evidence, candidate_k, top_raw_pool,
            index=index,
        )
        return RouteResult(results=results, trace=trace, direct=strategy.direct, index=index)

    # ---------------- trace ----------------

    def _build_trace(
        self,
        question: str,
        search_text: str,
        plan: QueryPlan,
        strategy: StrategyResult,
        outcome,
        numeric_res: numeric_rules.NumericRecall | None,
        results: list[RetrievalResult],
        total_ms: float,
        top_k: int,
        rrf_k: int,
        threshold: float | None,
        evidence: bool,
        candidate_k: int,
        top_raw_pool: float | None = None,
        index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = strategy_of(plan.type)

        papers = {r.arxiv_id for r in results}
        sections = {(r.arxiv_id, (r.section or "")[:40]) for r in results}
        kinds: dict[str, int] = {}
        for r in results:
            kinds[r.kind or "unknown"] = kinds.get(r.kind or "unknown", 0) + 1

        # 口径见 apply 前那段注释：用候选池（重排前）的最高融合原始分
        top_raw = (
            top_raw_pool
            if top_raw_pool is not None
            else max((r.raw_score for r in results if r.raw_score is not None), default=None)
        )

        recall = {
            "returned": len(results),
            "candidates": candidate_k,
            "papers": len(papers),
            "sections": len(sections),
            "kinds": kinds,
            "top_score": round(max((r.score for r in results), default=0.0), 4),
            # 拒答判断看这个（绝对量），不看归一化后的 top_score
            "top_raw_score": round(top_raw, 6) if top_raw is not None else None,
            "paper_list": [
                {"arxiv_id": a, "title": next((r.title for r in results if r.arxiv_id == a), "")}
                for a in sorted(papers)
            ],
            # 每条结果的来源：fusion / rule / fusion+rule / rerank
            "sources": [
                {"chunk_id": r.chunk_id, "source": r.hit_source or "fusion"}
                for r in results
            ],
            "evidence": "rule_hit" if evidence else "score",
        }

        base = plan.to_trace()
        return {
            "query": question,
            "search_text": search_text,
            # 这次读的是哪一代索引。放在顶层而不是塞进 recall：
            # 它解释的是"整条结果从哪来"，不是"召回了几条"。
            # 排障时第一个该看的就是它——分数全对但内容不对，多半是版本不对。
            "index": _index_trace(index),
            "type": base["type"],
            "type_label": base["type_label"],
            "confidence": base["confidence"],
            "classifier": base["classifier"],
            "slots": base["slots"],
            "strategy": {
                "name": meta["name"],
                "label": meta["label"],
                "params": strategy.params,
                "latency_ms": strategy.latency_ms,
                "hops": strategy.hops,
                "notes": strategy.notes,
                "direct": strategy.direct,
                "reject_threshold": threshold,
                # 这次实际开了哪几路召回。基线对比、复现实验都要靠它：
                # 只看 reject_threshold 无法区分"阈值低"和"只开了一路"。
                "channels": self.retrieval.active_channels,
            },
            "numeric": (
                numeric_res.to_trace()
                if numeric_res
                else {"used": False, "probes": [], "injected": 0}
            ),
            "rerank": outcome.to_trace(),
            "recall": recall,
            "top_k": top_k,
            "rrf_k": rrf_k,
            "total_ms": total_ms,
            # 兜底是不是被动触发，一眼要能看出来——这是"没有降低原有效果"的证据位
            "fallback": {
                "used": plan.type == QueryType.GENERIC.value,
                "reason": plan.fallback_reason or plan.degrade_reason,
                "label": label_of(plan.type),
            },
        }


def _index_trace(index: dict[str, Any] | None) -> dict[str, Any]:
    """
    把路由决策整理成 trace 里的 `index` 块。

    为什么要带 `rule`（default/project/percent/explicit）而不只带版本号：
    "为什么这次读的是旧版"是一个会被反复问到的问题。只有版本号时，答案是
    "去看数据库里那三行切流规则"；带上规则之后，界面上一行就能说清
    "课题 A 单独钉了旧版" / "灰度 30%，本课题在 71 号桶"。
    """
    if not index:
        return {"pipeline_version": None, "collection": None, "rule": "unknown"}
    sig = index.get("signature") or {}
    return {
        "pipeline_version": index.get("pipeline_version"),
        "collection": index.get("collection"),
        "rule": index.get("rule"),
        "detail": index.get("detail"),
        "percent": index.get("percent", 0),
        "bucket": index.get("bucket"),
        "stable_version": index.get("stable_version"),
        "origin": index.get("origin"),
        "status": index.get("status"),
        "signature": {
            "schema": sig.get("schema"),
            "splitter": sig.get("splitter"),
            "dense_model": sig.get("dense_model"),
            "sparse_model": sig.get("sparse_model"),
            "embed_max_chars": sig.get("embed_max_chars"),
        } if sig else {},
        "candidates": index.get("candidates") or [],
        "fallback": index.get("fallback"),
    }


# 便捷函数：不关心 trace 时直接用
def retrieve(
    question: str,
    top_k: int = 8,
    rrf_k: int = 60,
    search_text: str | None = None,
    rerank: bool | None = None,
    numeric: bool | None = None,
) -> list[RetrievalResult]:
    return RAGRetriever().route(
        question, search_text=search_text, top_k=top_k, rrf_k=rrf_k,
        rerank=rerank, numeric=numeric,
    ).results
