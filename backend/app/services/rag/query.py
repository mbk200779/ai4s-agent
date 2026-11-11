"""
查询理解：把一个问题变成一份可解释的《检索计划》。

两段式（规则 → 小模型），全程受 300ms 墙钟预算约束：

  1. 规则表：0 延迟、可解释、可复现。命中数足够就到此为止（大多数问题属于这种）。
  2. 小模型：本地 embedding 原型分类器。把问题编码成向量，和每个类型的原型句
     比余弦相似度，取最高者。它补的是"规则没写到的说法"
     （比如"这两种架构各自的取舍是什么"没命中任何比较关键词）。
  3. 兜底：规则与小模型都判不准 → generic，走分流前那套行为。

小模型为什么不用 LLM：实测一次 JSON 分类要 400~900ms，直接违反 300ms 约束。
本地原型分类器的成本是"1 条 query 向量"（~20ms），原型句的向量算一次就缓存，
所以它既能真的在预算内跑完，也能在模型不可用（fastembed 加载失败）时干净降级成纯规则。
"""
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.rag import rules as R
from app.services.rag.taxonomy import (
    CLASSIFY_BUDGET_MS,
    MIN_CONF,
    PROTO_ONLY_MIN_MARGIN,
    PROTO_ONLY_MIN_SIM,
    STRONG_CONF,
    QueryType,
    label_of,
)

# 原型句向量的进程级缓存。原型是常量，算一次就够；缓存住之后每条查询只花 1 条向量。
_PROTO_VECS: dict[str, list[list[float]]] = {}
_PROTO_STATE: dict[str, Any] = {"ready": False, "build_ms": 0.0, "error": None}

# 原型相似度的归一化区间。实测同一模型下，正确类型的相似度约 0.78~0.95，
# 错误类型 0.55~0.72，所以拿 0.62~0.90 做 min-max 就够拉开差距。
_PROTO_LO = 0.62
_PROTO_HI = 0.90


def _norm(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class QueryPlan:
    """一次查询理解的结论。策略层只看这个对象，不再看原始字符串。"""

    question: str
    type: str
    confidence: float
    method: str                      # rules | rules+prototype | rules+llm | fallback
    slots: dict[str, Any] = field(default_factory=dict)
    rule_hits: list[dict[str, Any]] = field(default_factory=list)
    prototype: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    degraded: bool = False
    degrade_reason: str | None = None      # 功能性降级：编码失败 / 超预算 / 预热未完成
    fallback_reason: str | None = None      # 判不准退回 generic，不算"降级"

    query_vec: list[float] | None = None   # 复用给 dense 检索，避免二次编码

    @property
    def type_label(self) -> str:
        return label_of(self.type)

    def to_trace(self) -> dict[str, Any]:
        """只暴露该暴露的：向量本身不进 trace（又长又没用）。"""
        return {
            "type": self.type,
            "type_label": self.type_label,
            "confidence": self.confidence,
            "classifier": {
                "method": self.method,
                "latency_ms": self.latency_ms,
                "budget_ms": CLASSIFY_BUDGET_MS,
                "degraded": self.degraded,
                "degrade_reason": self.degrade_reason,
                "fallback_reason": self.fallback_reason,
                "rule_hits": self.rule_hits,
                "prototype": self.prototype,
            },
            "slots": self.slots,
        }


class QueryRouter:
    """规则 + 本地原型分类器。无状态（除模块级原型向量缓存），可并发。"""

    def __init__(self, embedder=None):
        # 传入 embedder 是为了和检索共用同一个已加载的模型实例；
        # 不传则用默认的惰性单例（测试或只做分类时用）。
        self._embedder = embedder

    # ---------------- 对外入口 ----------------

    def analyze(
        self,
        question: str,
        budget_ms: int = CLASSIFY_BUDGET_MS,
        query_vec: list[float] | None = None,
    ) -> QueryPlan:
        started = time.perf_counter()
        slots = self._extract_slots(question)

        # ---- 第一段：规则 ----
        rule_result = R.classify_by_rules(question)
        rule_hits = [h.to_dict() for h in rule_result.hits]
        top_type, top_score = rule_result.top
        conf = R.confidence_of(top_score, rule_result.second_score)
        method = "rules"
        proto_info: dict[str, Any] = {"used": False}

        if top_score <= 0:
            # 一条规则都没命中：直接交给小模型，别提前认输
            combined: dict[str, float] = {}
            best_type = QueryType.GENERIC.value
            best_conf = 0.0
        else:
            combined = {t: s / top_score for t, s in rule_result.scores.items()}
            best_type = top_type.value if isinstance(top_type, QueryType) else top_type
            best_conf = conf

        # ---- 第二段：小模型（只在规则不够强、且预算还够时）----
        elapsed_ms = (time.perf_counter() - started) * 1000
        need_proto = best_conf < STRONG_CONF
        vec = query_vec
        proto_ready = _PROTO_STATE["ready"]
        # 原型向量还没建好时**连 query 向量都不要算**：
        # 这会儿后台线程正在跑同一份 ONNX 模型，两个推理请求会互相排队，
        # 实测能把一条本该 20ms 的分类拖到 700ms+。宁可这次只用规则。
        if need_proto and proto_ready and (budget_ms - elapsed_ms) >= 40:
            if vec is None:
                try:
                    vec = self._embed_query(question)
                except Exception as exc:  # 模型不可用 → 纯规则，这就是降级路径
                    proto_info = {"used": False, "error": f"{type(exc).__name__}: {exc}"}
                    vec = None
            if vec is not None:
                proto_sims, proto_meta = self._prototype_scores(vec)
                if proto_sims:
                    if top_score <= 0:
                        # 规则一条都没命中，判断全靠小模型。这种情况要抬门槛：
                        # 相似度不够高或与第二名贴得太近，就宁可不要结论——
                        # 一次"看着有点像"就把检索行为换掉，风险远大于收益。
                        ordered = sorted(proto_sims.values(), reverse=True)
                        top_sim = ordered[0]
                        gap = top_sim - ordered[1] if len(ordered) > 1 else top_sim
                        picked = max(proto_sims, key=lambda k: proto_sims[k])
                        # 结构校验：对比题至少要能指出两个对象。
                        # 问句里只有 BERT 一个实体，却因为"看起来像对比"被派去多跳，
                        # 会平白多花 3 次检索，而且召回不到任何第二篇。
                        n_entities = len([e for e in slots.get("entities", []) if len(e) >= 2])
                        structural_ok = not (
                            picked == QueryType.COMPARE.value
                            and n_entities < 2
                            and not slots.get("all_scope")
                        )
                        if (
                            top_sim < PROTO_ONLY_MIN_SIM
                            or gap < PROTO_ONLY_MIN_MARGIN
                            or not structural_ok
                        ):
                            proto_info = {
                                "used": False,
                                "skipped": (
                                    "规则无命中且原型证据不足"
                                    f"（top_sim={top_sim:.3f}, gap={gap:.3f}）"
                                    if structural_ok
                                    else "规则无命中，原型判为对比但问句里不足两个对象"
                                ),
                                **proto_meta,
                            }
                            proto_sims = {}
                    if proto_sims:
                        combined = _blend(combined, proto_sims)
                        cand = max(combined, key=lambda k: combined[k])
                        best_type = cand
                        best_conf = _conf_of(combined, cand)
                        method = "rules+prototype"
                        proto_info = {"used": True, **proto_meta}
        elif need_proto:
            # 走到这里说明原型还在后台构建（或者预算已经被规则吃掉）：
            # 启动后台构建，这次先用规则的结果，trace 里如实标注。
            self._warm_prototypes_async()
            proto_info = {
                "used": False,
                "skipped": "prototype_warming" if not proto_ready else "budget_exhausted",
            }

        # ---- 兜底：判不准就不改行为 ----
        degrade_reason = proto_info.get("error") or proto_info.get("skipped")
        fallback_reason = None
        if best_conf < MIN_CONF:
            best_type = QueryType.GENERIC.value
            method = "fallback"
            # 退 generic 不算"降级"：这是设计内的正常结论（"这类问题就该走旧路径"），
            # 和"模型挂了/超预算"是两件事，混在一个字段里会让降级率虚高。
            fallback_reason = f"置信度 {best_conf:.2f} 低于 {MIN_CONF}，退回分流前行为"

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if latency_ms > budget_ms:
            # 超预算只报告，不重试：重试只会更慢。真超了说明是冷启动构建原型向量。
            degrade_reason = degrade_reason or f"分类耗时 {latency_ms}ms 超出预算 {budget_ms}ms"

        return QueryPlan(
            question=question,
            type=best_type,
            confidence=best_conf,
            method=method,
            slots=slots,
            rule_hits=rule_hits,
            prototype=proto_info,
            latency_ms=latency_ms,
            degraded=latency_ms > budget_ms,
            degrade_reason=degrade_reason if (latency_ms > budget_ms or degrade_reason) else None,
            fallback_reason=fallback_reason,
            # 分类过程中算过的向量交给检索复用：一次问答只编码一次 query。
            query_vec=vec,
        )

    # ---------------- 槽位 ----------------

    @staticmethod
    def _extract_slots(question: str) -> dict[str, Any]:
        """
        抽出来的槽位是策略层的输入：
          chart_refs  → point 用来给含该编号的图表段落加权
          quoted      → nav 用来做精确匹配的锚点
          entities    → compare 用来把问题里的名字消解到具体论文
          all_scope   → "这几篇/所有论文" 这类指代，无法消解时按全集处理
          numbers     → point 在重排时判断片段是否给出相近数值
        """
        return {
            "chart_refs": R.extract_chart_refs(question),
            "quoted": R.extract_quoted(question),
            "entities": R.extract_entity_tokens(question),
            "all_scope": R.wants_all_papers(question),
            "numbers": R.extract_numbers(question),
            "hyper_terms": R.extract_hyper_terms(question),
        }

    # ---------------- 小模型 ----------------

    def _embed_query(self, question: str) -> list[float]:
        embedder = self._embedder
        if embedder is None:
            from app.services.embedder import EmbedderService

            embedder = EmbedderService()
            self._embedder = embedder
        return _norm(list(embedder.embed_dense([question])[0]))

    def _prototype_scores(self, query_vec: list[float]) -> tuple[dict[str, float], dict[str, Any]]:
        """
        返回 (每类最高相似度, 元信息)。

        原型向量**不在这里同步构建**：48 条原型一次编码要 2 秒上下（实测 2257ms），
        直接做会让第一条模糊问句破掉 300ms 约束。改成"这次先不用，后台线程去建"，
        下一条请求起就可用。代价是启动后最初一两条查询只能靠规则，
        trace 里会记 degraded + prototype_warming，是可见的。
        """
        if not _PROTO_STATE["ready"]:
            self._warm_prototypes_async()
            return {}, {"used": False, "skipped": "prototype_warming"}

        sims: dict[str, float] = {}
        for qtype, vecs in _PROTO_VECS.items():
            sims[qtype] = max(_cos(query_vec, v) for v in vecs)

        ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)
        meta = {
            "top": [{"type": t, "sim": round(s, 3)} for t, s in ranked[:3]],
            "margin": round(ranked[0][1] - ranked[1][1], 3) if len(ranked) > 1 else 0.0,
            "build_ms": round(_PROTO_STATE["build_ms"], 1),
        }
        return sims, meta

    def _warm_prototypes_async(self) -> None:
        """后台构建原型向量。只启动一次；失败后不再重试（避免每条请求都白试一遍）。"""
        if _PROTO_STATE.get("building") or _PROTO_STATE.get("error"):
            return
        _PROTO_STATE["building"] = True

        def _run() -> None:
            started = time.perf_counter()
            try:
                texts: list[str] = []
                owners: list[str] = []
                for qtype, samples in R.PROTOTYPES.items():
                    for s in samples:
                        texts.append(s)
                        owners.append(qtype.value if hasattr(qtype, "value") else str(qtype))
                embedder = self._embedder
                if embedder is None:
                    from app.services.embedder import EmbedderService

                    embedder = EmbedderService()
                    self._embedder = embedder
                vecs = embedder.embed_dense(texts)
                for owner, vec in zip(owners, vecs):
                    _PROTO_VECS.setdefault(owner, []).append(_norm(list(vec)))
                _PROTO_STATE["ready"] = True
            except Exception as exc:  # 模型不可用：永久退化为纯规则
                _PROTO_STATE["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                _PROTO_STATE["build_ms"] = (time.perf_counter() - started) * 1000
                _PROTO_STATE["building"] = False

        threading.Thread(target=_run, name="rag-proto-warmup", daemon=True).start()


def _blend(rule_scores: dict[str, float], proto_sims: dict[str, float]) -> dict[str, float]:
    """
    规则 6 : 小模型 4 的加权。
    规则是硬判据（"β2"就是点查），小模型是软证据（语义接近某类问法），
    所以规则永远占大头；小模型只在规则没话说（该类型 0 分）时把候选抬起来。
    """
    out: dict[str, float] = {}
    for qtype in set(rule_scores) | set(proto_sims):
        r = rule_scores.get(qtype, 0.0)
        p = proto_sims.get(qtype, 0.0)
        pn = max(0.0, min(1.0, (p - _PROTO_LO) / (_PROTO_HI - _PROTO_LO)))
        out[qtype] = round(0.6 * r + 0.4 * pn, 4)
    return out


def _conf_of(scores: dict[str, float], best: str) -> float:
    top = scores.get(best, 0.0)
    second = max((v for k, v in scores.items() if k != best), default=0.0)
    return R.confidence_of(top, second)
