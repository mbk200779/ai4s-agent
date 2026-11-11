"""
精排层：cross-encoder 对融合候选逐条打分，把"该排第一的"提到第一。

为什么需要这一层：
融合（dense + sparse + RRF）解决的是**召回**——保证正确的片段进了候选池；
它解决不了**排序**——RRF 只看两路的排名位次，任何一路没召回，另一路也救不回来，
而候选池里"语义相似但答非所问"的段落恰好两路都能命中。
cross-encoder 把 (query, passage) 拼在一起读一遍，能分清
"β2 = 0.98 是本文的设置" 和 "β2 在光纤里是色散系数"——这是双塔模型结构上做不到的。

三条硬约束的落法：
1. **超时或失败降级为融合原序，不报错**：推理丢进单线程池，`future.result(timeout)`；
   任何异常/超时都返回融合原序并把原因写进 trace。超时后进 30s 熔断，避免每条请求都白等一次。
2. **缓存 key = query_hash + index_snapshot_id**：语料变了（重新入库、换版本），
   快照 id 就变，旧缓存自然失效——不需要手动清。
3. **开关**：`settings.rerank_enabled`（环境变量 `RERANK_ENABLED`）+ 单次请求 override，
   A/B 时同一批问题跑两遍即可。

缓存里存的是"chunk_id → 精排分"，不是整条结果。因为 chunk_id 在快照内稳定，
而结果集本身可能因策略微调变化；按 id 对齐可以让部分命中也有用。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.services.rag.strategies import load_corpus
from app.services.rag.taxonomy import (
    RERANK_CACHE_SIZE,
    RERANK_CACHE_TTL_S,
    RERANK_COOLDOWN_S,
    RERANK_TOP_K,
    RERANK_TOP_N,
    RERANK_TIMEOUT_MS,
)
from app.services.retrieval_service import RetrievalResult

# 送进 cross-encoder 的文本长度上限。
# 400 是实测出来的平衡点：语料片段中位数 322 字符、p90 3696，截到 400 时
# 20 对约 360ms（600 字符 500ms，800 字符 480ms，1200 字符 5s）。
# 再往上加，延迟涨得比 Recall 快——超长片段的后半段通常是参考文献噪声。
_MAX_PASSAGE_CHARS = 400

# 中文字符检测。英文 cross-encoder 拿中文问句打分等于随机排序——
# 实测「训练用了多少块 GPU，什么型号」被它排成了两段无关的 figure 片段，
# 而正确的那句 "8 NVIDIA P100 GPUs" 直接掉出了前 5。
# 所以非英文问句必须显式跳过精排（降级为融合原序），不能"打了分再说"。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

_MODEL_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")
# 单飞闸门：同一时刻只允许一次精排推理在跑。
# 为什么必须有：超时只是"不再等它"，那次推理还在跑；如果下一次请求继续往队列里塞，
# 后面的会排在它后面一起超时，表现为"一次超时之后全都在超时"。
# 现在超时期间新来的请求直接降级（快，不排队），等那一次跑完再恢复。
_RERANK_BUSY = threading.Event()


def _minmax(values: list[float] | None) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return [(v - lo) / span for v in values]


def _log(msg: str) -> None:
    print(f"[rerank] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 索引快照：缓存失效的依据
# ---------------------------------------------------------------------------

_SNAPSHOT_CACHE: dict[str, Any] = {"id": None, "at": 0.0}
_SNAPSHOT_TTL_S = 60


def index_snapshot_id(vector_store) -> str:
    """
    当前向量库内容的指纹，用作精排缓存 key 的后半段。

    为什么不用"集合名 + 点数"：重新入库一篇论文时点数会变，但**换版本**时点数不变，
    这时旧缓存会继续命中，用户会看到上一版的内容排在前面。
    所以取每篇论文的 (arxiv_id, version, 片段数) 做 sha1——版本一变指纹就变。
    代价是一次 scroll，但语料画像本来就有 60s 缓存，等于不要钱。
    """
    now = time.time()
    if _SNAPSHOT_CACHE["id"] and now - _SNAPSHOT_CACHE["at"] < _SNAPSHOT_TTL_S:
        return _SNAPSHOT_CACHE["id"]

    corpus = load_corpus(vector_store)
    papers = corpus.get("papers") or {}
    sig = "|".join(
        f"{aid}:{meta.get('version')}:{meta.get('chunks')}" for aid, meta in sorted(papers.items())
    )
    raw = f"{getattr(vector_store, 'chunk_collection', '')}#{corpus.get('size', 0)}#{sig}"
    snap = "snap-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    _SNAPSHOT_CACHE.update({"id": snap, "at": now})
    return snap


def query_hash(query: str) -> str:
    """问句归一化后取指纹：空白差异不该产生两份缓存。"""
    norm = " ".join((query or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

_CACHE: "OrderedDict[str, tuple[float, dict[str, float]]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> dict[str, float] | None:
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None
        at, scores = item
        if time.time() - at > RERANK_CACHE_TTL_S:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return scores


def _cache_put(key: str, scores: dict[str, float]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), scores)
        _CACHE.move_to_end(key)
        while len(_CACHE) > RERANK_CACHE_SIZE:
            _CACHE.popitem(last=False)


def clear_cache() -> int:
    with _CACHE_LOCK:
        n = len(_CACHE)
        _CACHE.clear()
    return n


def cache_stats() -> dict[str, Any]:
    with _CACHE_LOCK:
        return {"size": len(_CACHE), "capacity": RERANK_CACHE_SIZE, "ttl_s": RERANK_CACHE_TTL_S}


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------

@dataclass
class RerankOutcome:
    """一次精排的结果 + 过程数据。整个过程数据都会进 trace。"""

    results: list[RetrievalResult]
    applied: bool = False
    degraded: bool = False
    degrade_reason: str | None = None
    enabled: bool = True
    source: str = "env"            # env | request | disabled
    model: str = ""
    alpha: float = 0.0             # 精排分权重；1-α 是融合先验的权重
    rule_bonus: float = 0.0        # 规则命中的证据加成
    candidates: int = 0
    output: int = 0                # 进入"精排结论"的条数（= RERANK_TOP_K）
    returned: int = 0              # 实际返回条数（可能多于 output，多出来的是补位）
    cache: str = "off"             # off | hit | miss
    cache_key: str | None = None
    snapshot: str | None = None
    latency_ms: float = 0.0
    moved: list[dict[str, Any]] = field(default_factory=list)
    scores: list[dict[str, Any]] = field(default_factory=list)

    def to_trace(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "source": self.source,
            "applied": self.applied,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "model": self.model,
            "mode": "cross-encoder",
            "candidates": self.candidates,
            "output": self.output,
            "returned": self.returned,
            "cache": self.cache,
            "cache_key": self.cache_key,
            "index_snapshot_id": self.snapshot,
            "latency_ms": self.latency_ms,
            "moved": self.moved,
            "scores": self.scores,
        }


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

class Reranker:
    """
    cross-encoder 精排器。

    模型是进程级单例（类属性），首次使用时懒加载；加载失败只降级不抛出——
    精排是"锦上添花"，不该成为整条问答链路的单点故障。
    """

    # 按模型名缓存已加载的实例：评测要在同一个进程里并排比较多个模型，
    # 如果只留一个全局单例，第二个模型会静默复用第一个，评测结果全错。
    _models: dict[str, Any] = {}
    _load_errors: dict[str, str] = {}
    _load_lock = threading.Lock()
    _cooldown_until = 0.0
    _fail_streak = 0

    def __init__(
        self,
        enabled: bool | None = None,
        model_name: str | None = None,
        timeout_ms: int | None = None,
    ):
        self.enabled = settings.rerank_enabled if enabled is None else enabled
        self.model_name = model_name or settings.rerank_model
        self.timeout_ms = timeout_ms or settings.rerank_timeout_ms
        # α = 精排分在最终排序里的权重；(1-α) 留给融合分这个先验。
        # 默认 0.2 来自 docs/2.9 的 α 扫描：纯精排会把数值类召回打穿（84.9%→54.5%）。
        self.alpha = settings.rerank_alpha
        self.rule_bonus = settings.rerank_rule_bonus
        # 模型名里带 multilingual / m3 的才认多语言；其余按英文模型处理
        low = self.model_name.lower()
        self.multilingual = "multilingual" in low or "-m3" in low

    # ---- 模型加载 ----

    def _ensure_model(self):
        cls = Reranker
        model = cls._models.get(self.model_name)
        if model is not None:
            return model
        err = cls._load_errors.get(self.model_name)
        if err:
            raise RuntimeError(err)
        with cls._load_lock:
            model = cls._models.get(self.model_name)
            if model is not None:
                return model
            started = time.perf_counter()
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                model = TextCrossEncoder(
                    model_name=self.model_name,
                    threads=settings.embed_threads,
                )
                # 热身一次：ONNX 首次推理要额外分配内存并走一遍图优化，
                # 实测比稳态慢 300~400ms。不做这一步，第一条真请求大概率超时降级，
                # 然后触发熔断——用户看到的是"精排第一次就坏了"。
                # 单对热身不够：首请求是 20 对，batch 维度的图优化要到那一刻才发生，
                # 所以这里用 8 条互不相同的段落热身，把"第一次 20 对"提前消化掉。
                list(
                    model.rerank(
                        "warm up the onnx session",
                        [f"warm up passage {i}: attention layers and batch size on TPU devices" for i in range(8)],
                    )
                )
                cls._models[self.model_name] = model
                _log(f"模型加载完成 {self.model_name}，{time.perf_counter() - started:.1f}s")
            except Exception as exc:  # noqa: BLE001 — 任何加载失败都只降级
                cls._load_errors[self.model_name] = f"{type(exc).__name__}: {exc}"
                _log(f"模型加载失败，精排将一直降级：{cls._load_errors[self.model_name]}")
                raise
        return model

    @classmethod
    def status(cls) -> dict[str, Any]:
        return {
            "enabled": settings.rerank_enabled,
            "model": settings.rerank_model,
            "loaded": list(cls._models.keys()),
            "load_error": cls._load_errors or None,
            "timeout_ms": settings.rerank_timeout_ms,
            "cooldown_remaining_s": round(max(0.0, cls._cooldown_until - time.time()), 1),
            "cache": cache_stats(),
        }

    # ---- 推理 ----

    def _score_pairs(self, query: str, passages: list[str]) -> list[float]:
        """
        真正的打分。丢进单线程池 + 超时，超时就当失败。

        为什么用线程池而不是直接调：onnxruntime 的推理是同步阻塞的，
        没有一个可以中断它的接口；只能把等待的一端掐掉。
        掐掉后那次推理仍在跑，所以必须配合熔断——否则每个请求都排在同一条队列上等，
        表现为"超时之后越来越慢"，比直接降级更糟。
        """
        if _RERANK_BUSY.is_set():
            # 上一次还在算（多半是刚超时的那次）：不排队，直接降级
            raise TimeoutError("上一次精排仍在计算，跳过本次以免排队")
        model = self._ensure_model()

        def _run() -> list[float]:
            _RERANK_BUSY.set()
            try:
                return [float(s) for s in model.rerank(query, passages)]
            finally:
                _RERANK_BUSY.clear()

        future = _EXECUTOR.submit(_run)
        # 超时按批大小自适应：固定 1500ms 是按"20 对短文"标定的，
        # 而 20 对 250 词的正文在 8 线程上就要 480ms，赶上同时在建索引/跑嵌入时能翻三倍。
        # 一次超时不是"慢"，是整批降级为融合原序——用户拿到的是没精排过的结果，
        # 而界面只显示"精排 xx ms"，看不出已经降级。给每条 90ms 的预算，
        # 20 对 → 1800ms，40 对 → 3600ms；同时不低于配置值，避免把原来快的场景拖慢。
        budget_ms = max(self.timeout_ms, len(passages) * 90)
        try:
            return future.result(timeout=budget_ms / 1000.0)
        except FutureTimeout:
            raise TimeoutError(f"精排超时（>{budget_ms}ms，{len(passages)} 对）") from None

    # ---- 对外入口 ----

    def apply(
        self,
        query: str,
        candidates: list[RetrievalResult],
        top_k: int,
        snapshot: str | None = None,
        mode: str = "flat",
        enabled: bool | None = None,
        source: str | None = None,
    ) -> RerankOutcome:
        """
        query      : 与检索用同一段文本（中文问句的英文译文）。
                     精排要判断的是"这段正文能不能回答**这次检索的意图**"，
                     检索用的是译文，精排就必须用同一句——两边错位会让排序和召回各说各话。
        candidates : 融合 + 规则召回后的候选（已按融合分排序）
        top_k      : 调用方要几条
        mode       : flat（整体重排）/ coverage（按论文轮转，保住多篇覆盖）
        """
        started = time.perf_counter()
        want_enabled = self.enabled if enabled is None else enabled
        # source 只用于 trace 溯源：这次"开/关"是请求体决定的、环境变量决定的，还是压根被关掉。
        # 调用方（retrieve）知道用户到底有没有显式传参，所以允许它覆盖——否则永远是 "request"，
        # A/B 时看不出"以为在测精排，其实环境里早就关了"。
        source = source or ("request" if enabled is not None else ("env" if self.enabled else "disabled"))

        def passthrough(reason: str | None = None, degraded: bool = False) -> RerankOutcome:
            return RerankOutcome(
                results=candidates[:top_k],
                applied=False,
                degraded=degraded,
                degrade_reason=reason,
                enabled=want_enabled,
                source=source,
                model=self.model_name,
                candidates=len(candidates),
                returned=min(len(candidates), top_k),
                cache="off",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )

        if not want_enabled:
            return passthrough(None)
        if not candidates:
            return passthrough(None)
        cjk = len(_CJK_RE.findall(query or ""))
        if cjk and not self.multilingual:
            ratio = cjk / max(1, len(query or ""))
            if ratio >= 0.05:
                return passthrough(
                    "问句非英文，英文精排模型不适用，已降级为融合原序"
                    "（QA 链路会先把中文问句翻成英文再检索）",
                    degraded=True,
                )
        if len(candidates) == 1:
            return passthrough("只有 1 条候选，无需精排")
        if time.time() < Reranker._cooldown_until:
            left = round(Reranker._cooldown_until - time.time(), 1)
            return passthrough(f"熔断中（上次失败，{left}s 后重试）", degraded=True)

        passages = [
            " ".join(x for x in ((r.text or "")[: _MAX_PASSAGE_CHARS], (r.caption or "")[:160]) if x)
            for r in candidates
        ]
        # key = query_hash + index_snapshot_id，前面再挂模型名做分区。
        # 为什么必须分区：同一个问题换模型后分数完全不可比，
        # 不分区的话 A/B 的第二组会直接读到第一组的缓存，测出来"两个模型一模一样"。
        key = f"{self.model_name}:{query_hash(query)}:{snapshot or 'no-snap'}"
        # 融合分要在改写 r.score 之前快照，否则下面拿到的就是"上一次精排的结果"
        fusion_scores = [r.score or 0.0 for r in candidates]

        cached = _cache_get(key)
        if cached is not None:
            scores = [cached.get(r.chunk_id, -1e9) for r in candidates]
            if all(s > -1e8 for s in scores):
                out = self._reorder(query, candidates, scores, top_k, mode, fusion_scores)
                out.enabled = True
                out.source = source
                out.applied = True
                out.model = self.model_name
                out.cache = "hit"
                out.cache_key = key
                out.snapshot = snapshot
                out.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                return out
            # 只命中一部分（候选集变了）：当作 miss 重算，别拿半份分数凑排序
            _log("缓存匹配不完整，按 miss 重算")

        try:
            raw = self._score_pairs(query, passages)
            if len(raw) != len(candidates):
                raise ValueError(f"打分数量不一致：{len(raw)} vs {len(candidates)}")
        except Exception as exc:  # noqa: BLE001 — 精排失败绝不影响主链路
            # 单次超时不算"坏了"：可能只是这条问题特别长。连续两次才熔断。
            Reranker._fail_streak += 1
            if Reranker._fail_streak >= 2:
                Reranker._cooldown_until = time.time() + RERANK_COOLDOWN_S
                _log(f"连续 {Reranker._fail_streak} 次失败，熔断 {RERANK_COOLDOWN_S}s")
            reason = f"{type(exc).__name__}: {exc}"
            _log(f"降级为融合原序：{reason}")
            out = passthrough(f"精排失败，已降级为融合原序（{reason}）", degraded=True)
            out.cache = "miss"
            out.cache_key = key
            out.snapshot = snapshot
            return out

        Reranker._fail_streak = 0
        _cache_put(key, {r.chunk_id: s for r, s in zip(candidates, raw)})
        out = self._reorder(query, candidates, raw, top_k, mode, fusion_scores)
        out.enabled = True
        out.source = source
        out.applied = True
        out.model = self.model_name
        out.cache = "miss"
        out.cache_key = key
        out.snapshot = snapshot
        out.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return out

    # ---- 排序 ----

    def _reorder(
        self,
        query: str,
        candidates: list[RetrievalResult],
        scores: list[float],
        top_k: int,
        mode: str,
        fusion_scores: list[float] | None = None,
    ) -> RerankOutcome:
        """
        用「融合先验 + 精排 + 字面证据」三路合成最终分，再排序。

        final = α · norm(rerank) + (1-α) · norm(fusion) + rule_bonus · [规则命中]

        为什么不是 α=1（精排完全接管）：
        实测纯精排把 Recall@5 从 78.8% 压到 54.5%——数值/公式题的正确答案
        往往是一句 "β2 = 0.98" 或一行表格，通用 cross-encoder 不觉得它"像答案"，
        而融合分里已经编码了"参数名≈数值""实验设置段""表格"这些领域信号。
        α 是"信精排多少"的旋钮，默认 0.2：语义先验照旧，精排负责在同档里改判。

        rule_bonus 单独再加一项，而不是塞进融合分里：
        规则命中是**字面证据**（问句里的 8×P100 真的出现在这一段），
        它比任何语义分都硬，不该被语义模型压下去。

        coverage 模式（对比/综述）不是"整体排序"，而是**按论文轮转**：
        先按每篇的最高分排论文，再在篇内按最终分取。
        原因：对比题的价值在于"每一篇都给出了对应片段"，
        整体排序会把某一篇的 5 段全排到前面，另一篇一段不剩——那是把对比题做成了单篇问答。
        """
        # min-max 归一化到 0~1：不同模型的原始分量纲不同（有 logits 有概率），
        # 而下游（拒答展示、前端分数条）需要可比的量。
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0
        rerank_norm = [(s - lo) / span for s in scores]
        fusion_norm = _minmax(fusion_scores) if fusion_scores is not None else [0.0] * len(scores)

        alpha = self.alpha
        finals: list[float] = []
        for i, r in enumerate(candidates):
            bonus = self.rule_bonus if (r.hit_source or "").startswith("rule") else 0.0
            finals.append(alpha * rerank_norm[i] + (1.0 - alpha) * fusion_norm[i] + bonus)

        pair = list(zip(candidates, scores, rerank_norm, finals))
        fusion_rank = {r.chunk_id: i + 1 for i, r in enumerate(candidates)}

        if mode == "coverage":
            ordered = self._coverage_order(pair)
        else:
            ordered = sorted(pair, key=lambda t: t[3], reverse=True)

        results: list[RetrievalResult] = []
        for r, raw, rn, final in ordered[: max(top_k, 1)]:
            r.rerank_score = round(rn, 4)
            r.score = round(final, 4)
            if not r.hit_source:
                r.hit_source = "rerank"
            results.append(r)

        moved = []
        for new_rank, (r, _raw, _rn, _final) in enumerate(ordered[:RERANK_TOP_K], 1):
            old = fusion_rank.get(r.chunk_id, 0)
            if old and abs(old - new_rank) >= 2:
                moved.append({
                    "chunk_id": r.chunk_id,
                    "arxiv_id": r.arxiv_id,
                    "page": r.page,
                    "from": old,
                    "to": new_rank,
                    "delta": old - new_rank,
                })

        output = min(RERANK_TOP_K, len(results))
        return RerankOutcome(
            results=results,
            applied=True,
            model=self.model_name,
            alpha=alpha,
            rule_bonus=self.rule_bonus,
            candidates=len(candidates),
            output=output,
            returned=len(results),
            moved=moved,
            scores=[
                {
                    "rank": i,
                    "chunk_id": r.chunk_id,
                    "arxiv_id": r.arxiv_id,
                    "page": r.page,
                    "kind": r.kind,
                    "fusion_rank": fusion_rank.get(r.chunk_id),
                    "rerank_raw": round(raw, 4),
                    "rerank_score": round(rn, 4),
                    "fusion_score": round(fusion_norm[i - 1], 4),
                    "final_score": round(final, 4),
                }
                for i, (r, raw, rn, final) in enumerate(ordered[: max(top_k, RERANK_TOP_K)], 1)
            ],
        )

    @staticmethod
    def _coverage_order(pair: list[tuple]):
        """pair 元素为 (result, rerank_raw, rerank_norm, final)。"""
        by_paper: dict[str, list[tuple]] = {}
        for item in pair:
            by_paper.setdefault(item[0].arxiv_id, []).append(item)
        for items in by_paper.values():
            items.sort(key=lambda t: t[3], reverse=True)
        # 论文之间按各自最好的一条排序，保证"最强的那篇"先出场
        paper_order = sorted(
            by_paper, key=lambda aid: by_paper[aid][0][3], reverse=True
        )
        ordered: list[tuple] = []
        depth = max((len(v) for v in by_paper.values()), default=0)
        for i in range(depth):
            for aid in paper_order:
                if i < len(by_paper[aid]):
                    ordered.append(by_paper[aid][i])
        return ordered


# ---------------------------------------------------------------------------
# 进程启动时的预热
# ---------------------------------------------------------------------------

def warmup_reranker() -> None:
    """
    后台把精排模型加载进内存。

    不预热会怎样：第一条真正需要精排的请求要等 3~6 秒的 ONNX 会话初始化，
    用户体感是"问第一句话特别慢"，而且很容易撞上 1200ms 的超时预算直接降级——
    等于把这一步的效果浪费在冷启动上。
    """
    if not settings.rerank_enabled:
        _log("精排已关闭（RERANK_ENABLED=false），跳过预热")
        return

    def _run() -> None:
        try:
            Reranker()._ensure_model()
        except Exception as exc:  # noqa: BLE001
            _log(f"预热失败，后续请求将降级：{exc}")

    threading.Thread(target=_run, daemon=True, name="rerank-warmup").start()


__all__ = [
    "Reranker",
    "RerankOutcome",
    "cache_stats",
    "clear_cache",
    "index_snapshot_id",
    "query_hash",
    "warmup_reranker",
]
