"""
五条检索策略。

共同约定：
- 输入是已理解的 QueryPlan（类型 + 槽位），输出是统一的 `StrategyResult`；
- 所有策略最终都产出 `RetrievalResult` 列表，`score` 是**归一化后的融合分 + 策略加成**，
  取值范围大致 0~2，不再是 RRF 的 1/60 量级——这样不同策略的分数可比，
  前端也更适合展示（老代码里 QA 的拒答阈值 0.03 是配套 RRF 原值的，这里由调用方换算）。

融合为什么先归一化再加成：RRF 原始分只有 1/(60+rank) ≈ 0.016 量级，
直接往上加 0.3 的表格加成会让"是不是表格"彻底压过语义相关性。
所以先把融合分按候选项内的最大值归一化到 0~1，再加 0~0.6 的策略加成，
加成才有"临门一脚"的意义，而不是把排序完全重写。
"""
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.rag.taxonomy import (
    COMPARE_MAX_PAPERS,
    COMPARE_PAIRWISE,
    COMPARE_PER_PAPER_MIN,
    NAV_CONTEXT_CHARS,
    NAV_MIN_MATCH_CHARS,
    NAV_SCAN_LIMIT,
    POINT_CANDIDATES,
    POINT_CHART_BOOST,
    POINT_EXPERIMENT_BOOST,
    POINT_NUMBER_BOOST,
    POINT_REFERENCE_PENALTY,
    POINT_SPARSE_WEIGHT,
    POINT_TABLE_BOOST,
    POINT_VALUE_ASSIGN_BOOST,
    RRF_MIN_SCORE,
    SURVEY_BREADTH,
    SURVEY_OUTPUT_FACTOR,
    SURVEY_PER_SECTION_CAP,
    QueryType,
)
from app.repositories import ChunkRepository
from app.services.retrieval_service import RetrievalResult, RetrievalService
from app.services.vector_store import VectorStoreService


@dataclass
class StrategyResult:
    """一条策略的产出：结果 + 给 trace 用的过程数据。"""

    results: list[RetrievalResult]
    params: dict[str, Any] = field(default_factory=dict)
    hops: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    direct: bool = False          # True 表示不检索、直接作答（闲聊）
    latency_ms: float = 0.0
    # 拒答阈值。含义是"这条策略认为什么叫有把握"，各策略口径不同：
    #   分数口径 —— 最佳片段的原始融合分必须过线（generic 与 point 的常规情况）；
    #   证据口径 —— 拿到了比分数更硬的东西就直接放行（阈值 0）：
    #               点查命中赋值形态、定位命中字面、对比/综述铺开到多篇。
    # 为什么要分两种：RRF 分数衡量的是"两路检索是否一致"，不是"库里有没有这个内容"。
    # 受限召回（filter=arxiv_id）天然把上限压掉一半，拿同一把尺子量必然集体误拒。
    reject_threshold: float | None = None


def weighted_threshold(dense_weight: float, sparse_weight: float) -> float:
    """
    把"两路等权时的 0.03"换算到当前权重下。

    RRF 原始分 ≈ weight/(k+rank)。等权时两路都命中约 2/61≈0.033，只命中一路约 0.016。
    阈值 0.03 的实际语义就是"必须两路同时召回才不算低置信度"。
    点查把权重按 0.38/0.62 分配后，两路都命中的分只有 1.0/61≈0.016，
    如果还拿 0.03 当阈值，所有点查都会被误判成"查不到"。
    所以阈值跟着权重和缩放，保持同一个语义。
    """
    return round(RRF_MIN_SCORE * (dense_weight + sparse_weight) / 2.0, 5)


# ---------------------------------------------------------------------------
# 语料画像：对比（实体消解）、综述（覆盖度）、定位（全量扫描）都要用
# ---------------------------------------------------------------------------

# 缓存**按 collection 分键**。原来只有一份全局缓存，换成按版本切流之后
# 它会立刻变成 bug：切到新版本的第一分钟内，对比/综述/定位仍在用旧版本的语料，
# 表现为"检索结果来自新版、实体消解来自旧版"，两边对不上且不报错。
# 按 collection 分键顺带解决了重建期间新旧两版并存的场景——
# 两次读的是两份不同的语料，本来就该各缓存各的。
_CORPUS_CACHE: dict[str, dict[str, Any]] = {}
_CORPUS_TTL_S = 60


def invalidate_corpus(collection: str | None = None) -> None:
    """切流/重建完成后调一次：不等 TTL 过期，让下一次请求就读到新语料。"""
    if collection is None:
        _CORPUS_CACHE.clear()
    else:
        _CORPUS_CACHE.pop(collection, None)


def load_corpus(vector_store: VectorStoreService) -> dict[str, Any]:
    """
    一次性把语料元数据捞到内存并缓存 60 秒。

    为什么不逐次查库：对比要在"问题里的名字 → 具体论文"之间做消解，
    综述要数覆盖了哪些论文/章节，定位要在全文里找字面命中。
    这三件事都需要"当前语料有哪些片段"，而且都在同一层。
    按点数量做 key 之外的失效判断没必要——60 秒足够短，扫描量也小（千级片段）。
    """
    coll = vector_store.chunk_collection
    now = time.time()
    cached = _CORPUS_CACHE.get(coll)
    if cached and cached["chunks"] and now - cached["at"] < _CORPUS_TTL_S:
        return cached

    points, _ = vector_store.client.scroll(
        collection_name=coll,
        limit=NAV_SCAN_LIMIT,
        with_payload=True,
        with_vectors=False,
    )
    chunks: list[dict[str, Any]] = []
    papers: dict[str, dict[str, Any]] = {}
    # 向量点 ID → chunk 行 ID。语料里的 `id` 最终会被拿去建 RetrievalResult.chunk_id，
    # 而锚点/引用记录都挂在 chunk 行 ID 上；不换的话，凡是走语料（对比/综述/定位/
    # 数值规则召回）出来的片段都查不到锚点——症状是"这些片段的高亮永远只能到页"。
    point_ids = [str(p.id) for p in points]
    id_map = ChunkRepository.resolve_point_ids(point_ids)
    for p in points:
        pl = p.payload or {}
        aid = pl.get("arxiv_id") or ""
        pid = str(p.id)
        record = {"id": id_map.get(pid, pid), "point_id": pid, "payload": pl}
        chunks.append(record)
        if not aid:
            continue
        entry = papers.setdefault(
            aid,
            {
                "arxiv_id": aid,
                "title": pl.get("title", ""),
                "version": pl.get("version", "v1"),
                "profile": "",
                "chunks": 0,
            },
        )
        entry["chunks"] += 1
        # 画像 = 标题 + 若干正文片段的开头。
        # 用来把问题里的"BERT""Transformer"这类名字消解到具体论文：
        # 只取第一段不够——Attention 论文第 1 段是作者名与邮箱，
        # 正文里才出现"the Transformer"，取一段会让"Transformer"这个词彻底匹配不上。
        # 所以按页顺序累积多段开头，凑够 ~2400 字符，兼顾命中率与缓存体积。
        if pl.get("kind") == "paragraph" and len(entry["profile"]) < 2400:
            entry["profile"] += " " + (pl.get("text") or "")[:220]

    entry_out = {
        "at": now, "papers": papers, "chunks": chunks, "size": len(chunks),
        "collection": coll, "pipeline_version": vector_store.pipeline_version,
    }
    _CORPUS_CACHE[coll] = entry_out
    # 只留最近三版：重建期间会同时存在"在服务的旧版 + 正在灌的新版"，
    # 再多就是历史版本，没有必要各留一份全量语料在内存里。
    if len(_CORPUS_CACHE) > 3:
        oldest = min(_CORPUS_CACHE, key=lambda k: _CORPUS_CACHE[k]["at"])
        _CORPUS_CACHE.pop(oldest, None)
    return entry_out
    return _CORPUS_CACHE


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _rrf_merge(
    dense: list[RetrievalResult],
    sparse: list[RetrievalResult],
    top_k: int,
    rrf_k: int,
    dense_weight: float,
    sparse_weight: float,
) -> list[RetrievalResult]:
    """
    带权 RRF：score = w_d/(k+rank_d) + w_s/(k+rank_s)。
    比原实现多两件事：权重可调（点查更信 BM25），以及缺某一路时不会误算 rank=-1。
    """
    acc: dict[str, float] = {}
    keep: dict[str, RetrievalResult] = {}
    for res, weight, rank_field in (
        (dense, dense_weight, "rank_dense"),
        (sparse, sparse_weight, "rank_sparse"),
    ):
        for r in res:
            rank = getattr(r, rank_field)
            if rank is None or rank <= 0:
                continue
            acc[r.chunk_id] = acc.get(r.chunk_id, 0.0) + weight / (rrf_k + rank)
            keep.setdefault(r.chunk_id, r)
    ordered = sorted(acc, key=acc.get, reverse=True)[:top_k]
    out: list[RetrievalResult] = []
    for cid in ordered:
        r = keep[cid]
        r.raw_score = round(acc[cid], 6)
        r.score = r.raw_score
        out.append(r)
    return out


def _normalize_scores(results: list[RetrievalResult]) -> None:
    """把融合分按最大值缩放到 0~1，让后续加成有意义。"""
    if not results:
        return
    top = max(r.score for r in results) or 1.0
    for r in results:
        r.score = round(r.score / top, 4)


_EXPERIMENT_HINTS = re.compile(
    r"(hyper-?parameter|optimizer|Adam|AdamW|SGD|learning rate|warmup|batch size|"
    r"dropout|weight decay|gradient clip|gradient norm|GPU|TPU|\bP100\b|\bV100\b|\bA100\b|"
    r"epoch|training steps|label smoothing|正则化|优化器|学习率|超参|批量大小|训练步|"
    r"权重衰减|梯度裁剪|实验设置|实验配置)",
    re.IGNORECASE,
)
_EXPERIMENT_SECTION = re.compile(
    r"(experiment|setup|setting|implementation|training|hyper-?parameter|ablation|"
    r"实验|设置|配置|训练细节)",
    re.IGNORECASE,
)


def _is_experiment_chunk(r: RetrievalResult) -> bool:
    """
    判断一个片段是不是"实验设置/超参"性质。

    不能只看 section 名：本项目的 PDF 解析常常把章节名落成页眉
    （如 "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023"），靠 section 会全军覆没。
    所以用「章节名 或 正文里超参关键词的密度」两路判断。
    实测 Attention 论文第 7 页那段（Adam/β1/β2/warmup/P100/100,000 steps）
    关键词命中 6 个以上，稳过。
    """
    if _EXPERIMENT_SECTION.search(r.section or ""):
        return True
    hits = {m.group(0).lower() for m in _EXPERIMENT_HINTS.finditer(r.text or "")}
    return len(hits) >= 3


_TABLE_KINDS = {"table"}


# 超参名的多种写法 → 统一的赋值形态正则。
# 要匹配的是 "β2 = 0.98"、"warmup_steps = 4000"、"dropout: 0.1" 这种
# "参数名紧跟着一个具体数值"，这是点查最硬的证据。
_HYPER_ASSIGN_PATTERNS: dict[str, str] = {
    "beta1": r"(β\s*1|beta\s*1)",
    "beta2": r"(β\s*2|beta\s*2)",
    "beta": r"(β|beta)",
    "epsilon": r"(ϵ|ε|epsilon)",
    "learning rate": r"(learning\s*rate|learning_rate|\blr\b)",
    "warmup": r"(warm\s*-?\s*up\w*)",
    "dropout": r"(dropout)",
    "batch size": r"(batch\s*size|batch_size)",
    "weight decay": r"(weight\s*decay|weight_decay)",
    "gradient clip": r"(gradient\s*clip\w*|clip\s*norm)",
    "label smoothing": r"(label\s*smoothing)",
    "epoch": r"(epochs?)",
    "steps": r"(training\s*steps|warmup_steps|\bsteps\b)",
    # 注意这几个只收"词形"，不收单字母缩写（d / h / N / r）：
    # 单字母加运算符加数字的组合在论文表格里遍地都是（"d = 512"），
    # 一旦算作赋值证据，等于给一堆表格片段同时加满分，排名反而被搅乱。
    "hidden size": r"(hidden\s*size|hidden\s*dim)",
    "d_model": r"\bd_model\b",
    "d_ff": r"\bd_ff\b",
    "heads": r"(attention\s*heads?|\bnum_heads?\b)",
    "layers": r"(\blayers?\b|encoder\s+layers?)",
    "rank": r"(\brank\b)",
    "alpha": r"(α|alpha\b)",
}

# 可以去噪的参数名：这些符号在语料里几乎不会以别的含义出现，
# 一旦某篇论文只"提到"它却没有赋值形态，基本就是在说另一件事。
# 像 hidden size / heads / layers 这种用单个字母（d、h、N）表示的参数名不进这个集合——
# 单字母到处都是，按它去噪会把正文整段删掉。
_DISTINCTIVE_TERMS = {
    "beta1", "beta2", "epsilon", "warmup", "dropout", "batch size",
    "weight decay", "gradient clip", "label smoothing", "d_model", "d_ff", "alpha",
}


def _value_assignment_score(r: RetrievalResult, hyper_terms: list[str]) -> tuple[float, list[str]]:
    """
    检查片段里是否出现"问句点到的超参名 + 紧邻的具体数值"。

    为什么这是点查最可信的信号：附录里的超参大表也含 β2、learning rate 这些词，
    但只有正文的实验设置段会写成 "β2 = 0.98" 这种赋值形态。
    靠"这一段是表格"永远分不开这两者——而靠赋值形态可以。

    窗口取 24 字符而不是"必须是 = 号"：论文里"hidden size of 768""warmup 取 4000"
    这类自然语言写法很常见，卡死运算符会把它们全漏掉（实测 hidden size 那条
    会从第 1 位掉到第 4 位）。所以这里只做形态上的粗筛，
    同名异义的误伤交给下游的「同名异义去噪」处理——那一步用"论文"做单位，
    比用"句子形态"做单位可靠得多。
    """
    text = r.text or ""
    hits: list[str] = []
    for term in hyper_terms:
        pat = _HYPER_ASSIGN_PATTERNS.get(term)
        if not pat:
            continue
        # 参数名后面 24 个字符内出现数字，就认为是在给它赋值
        if re.search(pat + r"[^\d\n]{0,24}[\d]", text, re.IGNORECASE):
            hits.append(term)
    if not hits:
        return 0.0, []
    return POINT_VALUE_ASSIGN_BOOST, [f"assign:{h}" for h in hits[:3]]


def _point_boost(
    r: RetrievalResult,
    chart_refs: list[str],
    query_numbers: list[str],
    hyper_terms: list[str] | None = None,
    numeric_question: bool = True,
) -> tuple[float, list[str]]:
    """
    点查的加成规则。返回 (总加成, 命中的加成名) —— 命中了哪些必须写进 trace，
    否则"为什么这条排第一"就又变成黑盒了。

    注意这里**不给"片段里有数字"加成**：正文里几乎每段都有数字，
    这种加成对排序毫无区分度，只会把所有结果一起抬高。真正有区分度的是
    "这一段写明了参数名 = 值"「这一段是实验设置」"这一段就是被点名的那个图表"。
    表格只给很小的权重：附录超参表几乎必然是表格，把表格当强信号会把它顶到首位。

    `numeric_question=False`（问句里没有参数名/数值/图表编号）时，**不给实验段与表格加成**。
    这两条的前提是"答案以'参数 = 值'的形态落在实验设置里"；问句里没有被问的参数时
    这个前提不成立，加成退化成"偏好含数字的段落"——在只有 5 篇论文的库里，
    它会稳定地把**另一篇**论文的实验设置段顶到首位。
    实测例：问 BERT 的掩码比例，第一名是 Attention 的训练数据段（同样含
    word-piece / tokens，但答的是 32000 词表），因为它是实验段拿到 +0.3 加成，
    而正确答案（BERT 第 4 页"mask 15% of all WordPiece tokens"）被顶到第 6 名。
    """
    boost = 0.0
    fired: list[str] = []

    # 赋值形态只对正文可信。参考文献/附录片段里，参数名和数值是表格的列式排列，
    # 一样能命中"参数名后面跟着数字"，但那是表格结构，不是"作者声明把它设成多少"。
    # 给它加同样的分，附录超参大表就会永远压过第 5.3 节 Optimizer 这类正文段落。
    if (r.kind or "").lower() != "reference":
        assign_boost, assign_hits = _value_assignment_score(r, hyper_terms or [])
        if assign_boost:
            boost += assign_boost
            fired.extend(assign_hits)

    if numeric_question and _is_experiment_chunk(r):
        boost += POINT_EXPERIMENT_BOOST
        fired.append("experiment")

    if numeric_question and (r.kind or "").lower() in _TABLE_KINDS:
        boost += POINT_TABLE_BOOST
        fired.append("table")

    # 参考文献片段降权：注意本项目的解析会把附录里的超参表误标成 reference，
    # 它们内容上确实相关，但"第 5.3 节 Optimizer"这类正文段落才是点查该给的答案。
    if (r.kind or "").lower() == "reference":
        boost -= POINT_REFERENCE_PENALTY
        fired.append("reference_penalty")

    if chart_refs:
        haystack = f"{r.caption or ''} {r.text or ''}".lower()
        for ref in chart_refs:
            kind, _, num = ref.partition(" ")
            if not num:
                continue
            # "figure 2" 要能匹配 "Figure 2:" / "图 2" / "表 2"
            pat = rf"({kind}|fig\.?|图|表|公式|方程|式)\s*[\(（]?\s*{re.escape(num)}"
            if re.search(pat, haystack, re.IGNORECASE):
                boost += POINT_CHART_BOOST
                fired.append(f"chart:{ref}")
                break

    # 问句里带了具体数值（"β2 是 0.98 还是 0.999"），片段里出现同一数值就再加一点
    if query_numbers:
        hit = [n for n in query_numbers if n in (r.text or "")]
        if hit:
            boost += POINT_NUMBER_BOOST
            fired.append(f"number:{hit[0]}")

    return round(boost, 3), fired


def _payload_to_result(payload: dict[str, Any], chunk_id: str, score: float) -> RetrievalResult:
    """把 Qdrant payload 直接组装成 RetrievalResult（全量扫描时没有 ScoredPoint 可用）。"""
    return RetrievalResult(
        chunk_id=chunk_id,
        paper_id=payload.get("paper_id", ""),
        arxiv_id=payload.get("arxiv_id", ""),
        version=payload.get("version", "v1"),
        title=payload.get("title", ""),
        kind=payload.get("kind", "paragraph"),
        text=payload.get("text", ""),
        section=payload.get("section", ""),
        page=payload.get("page", 0),
        paragraph=payload.get("paragraph", 0),
        score=score,
        bbox=payload.get("bbox"),
        latex=payload.get("latex"),
        caption=payload.get("caption"),
        description=payload.get("description"),
        doi=payload.get("doi"),
        year=payload.get("year"),
        authors=payload.get("authors"),
    )


def _squash(text: str) -> str:
    """归一化：去掉标点与空白差异，让跨行断词的原文句子也能被字面匹配到。"""
    return re.sub(r"[\s\u3000]+", " ", (text or "")).strip().lower()


# ---------------------------------------------------------------------------
# 1. point：表格与实验设置段优先 + 稀疏召回加权
# ---------------------------------------------------------------------------

def run_point(
    retrieval: RetrievalService,
    query: str,
    plan_slots: dict[str, Any],
    top_k: int,
    rrf_k: int,
    query_vec: list[float] | None = None,
) -> StrategyResult:
    started = time.perf_counter()
    n = max(POINT_CANDIDATES, top_k * 4)
    dense = retrieval.dense_search(query, top_k=n, query_vec=query_vec)
    sparse = retrieval.sparse_search(query, top_k=n)
    merged = _rrf_merge(
        dense, sparse, top_k=n, rrf_k=rrf_k,
        dense_weight=1.0 - POINT_SPARSE_WEIGHT, sparse_weight=POINT_SPARSE_WEIGHT,
    )
    _normalize_scores(merged)

    chart_refs = plan_slots.get("chart_refs") or []
    numbers = plan_slots.get("numbers") or []
    hyper_terms = plan_slots.get("hyper_terms") or []
    # 问句里到底有没有"被问到的那个量"。没有的话，实验段/表格加成没有依据（见 _point_boost）。
    numeric_question = bool(hyper_terms or numbers or chart_refs)
    boosts: list[dict[str, Any]] = []
    fired_of: dict[str, list[str]] = {}
    for r in merged:
        b, fired = _point_boost(r, chart_refs, numbers, hyper_terms, numeric_question)
        if fired:
            fired_of[r.chunk_id] = fired
        if b:
            r.score = round(r.score + b, 4)
            boosts.append({"chunk_id": r.chunk_id, "page": r.page, "boost": b, "why": fired})

    merged.sort(key=lambda r: r.score, reverse=True)

    # 同名异义去噪：点查问的是"这个参数取多少"，可语料里同一个符号常指不同物理量
    # （β2 在优化器里是二阶矩衰减率，在光纤里是色散系数）。
    # 首条已经拿到赋值证据、且问题没有点名多篇时，只保留首条所在论文的相关片段：
    # 别的论文里那些"β2 …"对回答这个问句没有贡献，只会在上下文里制造
    # 第二个"β2 = −20ps/nm/km"把答案带偏（实测不处理时 LLM 会去引光纤那篇）。
    # 只对高区分度参数名生效，且不动首条所在论文的片段。
    dropped: list[dict[str, Any]] = []
    filter_terms = [t for t in hyper_terms if t in _DISTINCTIVE_TERMS]
    if filter_terms and merged:
        head_paper = merged[0].arxiv_id
        head_assign = any(w.startswith("assign:") for w in fired_of.get(merged[0].chunk_id, []))
        if head_assign:
            kept: list[RetrievalResult] = []
            for r in merged:
                if r.arxiv_id == head_paper:
                    kept.append(r)
                    continue
                mentions = any(
                    re.search(_HYPER_ASSIGN_PATTERNS[t], r.text or "", re.IGNORECASE)
                    for t in filter_terms
                )
                if mentions:
                    dropped.append({
                        "chunk_id": r.chunk_id, "arxiv_id": r.arxiv_id,
                        "page": r.page, "terms": filter_terms,
                        "why": "其他论文提到同一符号（同名异义）",
                    })
                else:
                    kept.append(r)
            merged = kept

    # 拒答阈值：默认按权重和缩放；但首条若命中"参数名 ≈ 数值"的赋值形态，直接放行。
    # 为什么这里要开例外：拒答要回答的是"库里到底有没有这个内容"，
    # 而 RRF 分数衡量的是"两路检索是否一致"，两者不是一回事。
    # 正文里明明写着 `β2 = 0.98`，却因为中文问句翻成英文后 dense 排到第 20 名而拒答，
    # 是把度量尺用错了地方。有直接证据时就不该再看分数。
    top_assign = [w for w in fired_of.get(merged[0].chunk_id, []) if w.startswith("assign:")] if merged else []
    notes = [f"稀疏权重 {POINT_SPARSE_WEIGHT}（点查更依赖字面匹配）"]
    if top_assign:
        notes.append(
            f"首条命中赋值形态 {'/'.join(top_assign)}，按「命中即采信」放行，不再卡分数阈值"
        )
    if dropped:
        notes.append(f"已剔除 {len(dropped)} 段同名异义片段（提到符号但无赋值形态）")

    return StrategyResult(
        results=merged[:top_k],
        params={
            "top_k": top_k,
            "sparse_weight": POINT_SPARSE_WEIGHT,
            "candidates": n,
            "boosts": [
                {"name": "value_assignment", "value": POINT_VALUE_ASSIGN_BOOST},
                {"name": "table", "value": POINT_TABLE_BOOST},
                {"name": "experiment_section", "value": POINT_EXPERIMENT_BOOST},
                {"name": "query_number", "value": POINT_NUMBER_BOOST},
                {"name": "reference_penalty", "value": -POINT_REFERENCE_PENALTY},
                {"name": "chart_ref", "value": POINT_CHART_BOOST},
            ],
            "chart_refs": chart_refs,
            "hyper_terms": hyper_terms,
            "numeric_question": numeric_question,
            "dedup_same_symbol": len(dropped),
        },
        hops=[
            {"hop": 1, "action": "dense+sparse 召回", "dense": len(dense), "sparse": len(sparse)},
            {"hop": 2, "action": "按表格/实验段/图表编号加成重排", "boosted": len(boosts),
             "top_boosts": boosts[:5]},
            {"hop": 3, "action": "同名异义去噪（只留赋值证据）", "dropped": dropped[:6]},
        ],
        notes=notes,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        reject_threshold=(
            0.0
            if top_assign
            else weighted_threshold(1.0 - POINT_SPARSE_WEIGHT, POINT_SPARSE_WEIGHT)
        ),
    )


# ---------------------------------------------------------------------------
# 2. compare：多跳（先定位各文，再成对召回）
# ---------------------------------------------------------------------------

def resolve_papers(
    question: str,
    slots: dict[str, Any],
    corpus: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    把问题里提到的名字消解到具体论文。

    做法：对每篇论文取「标题 + 正文开头」当画像，用问题里的拉丁词元做子串匹配，
    标题命中权重 2、正文命中权重 1，取分数最高的前几篇。
    之所以不做严格的实体链接：语料只有几篇到几十篇，模糊匹配够用，
    而且失败时的回退（全语料/按召回分布取前几篇）比实体链接更稳。
    """
    papers = corpus.get("papers") or {}
    tokens = [t for t in (slots.get("entities") or []) if len(t) >= 2]
    scored: list[dict[str, Any]] = []

    for aid, meta in papers.items():
        title = (meta.get("title") or "").lower()
        profile = (meta.get("profile") or "").lower()
        score = 0.0
        hits: list[str] = []
        for tok in tokens:
            low = tok.lower()
            if low in title:
                score += 2.0
                hits.append(tok)
            elif low in profile:
                score += 1.0
                hits.append(tok)
        if score > 0:
            scored.append({"arxiv_id": aid, "title": meta.get("title", ""),
                           "score": round(score, 2), "matched": hits})

    scored.sort(key=lambda x: x["score"], reverse=True)
    picked = [s["arxiv_id"] for s in scored[:COMPARE_MAX_PAPERS]]

    if not picked and slots.get("all_scope"):
        picked = list(papers.keys())[:COMPARE_MAX_PAPERS]
        scored = [{"arxiv_id": a, "title": papers[a].get("title", ""), "score": 0.0,
                   "matched": [], "reason": "指代全集"} for a in picked]
    return picked, scored


def run_compare(
    retrieval: RetrievalService,
    query: str,
    plan_slots: dict[str, Any],
    top_k: int,
    rrf_k: int,
    corpus: dict[str, Any],
    query_vec: list[float] | None = None,
) -> StrategyResult:
    started = time.perf_counter()
    hops: list[dict[str, Any]] = []

    # ---- hop 1：实体消解——问题在说哪几篇 ----
    paper_ids, resolved = resolve_papers(query, plan_slots, corpus)
    hops.append({
        "hop": 1,
        "action": "定位论文",
        "papers": resolved,
        "picked": paper_ids,
    })

    per_paper: dict[str, list[RetrievalResult]] = {}

    if paper_ids:
        # ---- hop 2：逐篇受限召回，保证每篇都有代表片段 ----
        each = max(COMPARE_PER_PAPER_MIN, top_k // max(1, len(paper_ids)) + 1)
        for aid in paper_ids:
            filt = {"arxiv_id": aid}
            dense = retrieval.dense_search(query, top_k=each * 3, filters=filt, query_vec=query_vec)
            sparse = retrieval.sparse_search(query, top_k=each * 3, filters=filt)
            merged = _rrf_merge(dense, sparse, top_k=each, rrf_k=rrf_k,
                                dense_weight=0.6, sparse_weight=0.4)
            _normalize_scores(merged)
            per_paper[aid] = merged
        hops.append({
            "hop": len(hops) + 1,
            "action": "逐篇受限召回",
            "per_paper": {a: len(v) for a, v in per_paper.items()},
            "each": each,
        })
    else:
        # 消解失败：先全局召回，再按"各篇出现次数"挑出主论文，二次受限召回。
        # 这一步是"多跳"的第二跳——第一跳失败了不能直接退化成单轮。
        wide = retrieval.dense_search(query, top_k=top_k * 4, query_vec=query_vec)
        wide_sparse = retrieval.sparse_search(query, top_k=top_k * 4)
        merged_all = _rrf_merge(wide, wide_sparse, top_k=top_k * 4, rrf_k=rrf_k,
                                dense_weight=0.6, sparse_weight=0.4)
        counts: dict[str, int] = {}
        for r in merged_all:
            counts[r.arxiv_id] = counts.get(r.arxiv_id, 0) + 1
        paper_ids = [a for a, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)][:COMPARE_MAX_PAPERS]
        hops.append({
            "hop": len(hops) + 1,
            "action": "实体消解失败，改由召回分布定位主论文",
            "picked": paper_ids,
            "counts": counts,
        })
        each = max(COMPARE_PER_PAPER_MIN, top_k // max(1, len(paper_ids)))
        for aid in paper_ids:
            sub = [r for r in merged_all if r.arxiv_id == aid][:each]
            per_paper[aid] = sub
        hops.append({"hop": len(hops) + 1, "action": "按主论文重排（复用首轮候选）",
                     "per_paper": {a: len(v) for a, v in per_paper.items()}})

    # ---- hop 3：成对交错输出 ----
    pairs: list[dict[str, Any]] = []
    ordered: list[RetrievalResult] = []
    if COMPARE_PAIRWISE:
        depth = max((len(v) for v in per_paper.values()), default=0)
        for i in range(depth):
            row: list[RetrievalResult] = []
            for aid in per_paper:
                if i < len(per_paper[aid]):
                    row.append(per_paper[aid][i])
            ordered.extend(row)
            if len(row) >= 2:
                pairs.append({
                    "pair": i + 1,
                    "members": [
                        {"arxiv_id": r.arxiv_id, "title": r.title, "page": r.page,
                         "section": r.section, "score": r.score}
                        for r in row
                    ],
                })
    else:
        for v in per_paper.values():
            ordered.extend(v)

    hops.append({
        "hop": len(hops) + 1,
        "action": "成对交错输出（A1,B1,A2,B2…）",
        "pairs": len(pairs),
        "pair_detail": pairs[:6],
    })

    return StrategyResult(
        results=ordered[: max(top_k, COMPARE_PER_PAPER_MIN * len(per_paper))],
        params={
            "top_k": top_k,
            "max_papers": COMPARE_MAX_PAPERS,
            "pairwise": COMPARE_PAIRWISE,
            "papers_resolved": paper_ids,
        },
        hops=hops,
        notes=[
            "多跳：先定位各文，再逐篇受限召回，最后成对交错",
            (
                f"覆盖 {len(per_paper)} 篇，按覆盖度采信放行"
                if len(per_paper) >= 2
                else "只定位到 1 篇，退化为按分数阈值把关"
            ),
        ],
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        # 对比题的置信度不是"分数高不高"，而是"说到的几篇是不是都找到了片段"。
        # 受限召回（filter=arxiv_id）本身就把候选压到单篇，RRF 原始分的上限只有
        # 跨全库召回的一半，拿同一把尺子量必然集体误拒。
        reject_threshold=0.0 if len(per_paper) >= 2 else weighted_threshold(0.6, 0.4),
    )


# ---------------------------------------------------------------------------
# 3. survey：广度召回 + 去重（聚类在步骤 08）
# ---------------------------------------------------------------------------

def run_survey(
    retrieval: RetrievalService,
    query: str,
    top_k: int,
    rrf_k: int,
    query_vec: list[float] | None = None,
) -> StrategyResult:
    started = time.perf_counter()
    n = top_k * SURVEY_BREADTH
    dense = retrieval.dense_search(query, top_k=n, query_vec=query_vec)
    sparse = retrieval.sparse_search(query, top_k=n)
    merged = _rrf_merge(dense, sparse, top_k=n, rrf_k=rrf_k, dense_weight=0.7, sparse_weight=0.3)
    _normalize_scores(merged)

    # 多样性去重：同一篇同一节最多留 SURVEY_PER_SECTION_CAP 条。
    # 综述问的是"有哪些"，如果 top-k 全来自同一节的相邻段落，答案会变成复读机。
    kept: list[RetrievalResult] = []
    seen: dict[tuple[str, str], int] = {}
    papers: set[str] = set()
    sections: set[tuple[str, str]] = set()
    for r in merged:
        key = (r.arxiv_id, (r.section or "")[:40])
        if seen.get(key, 0) >= SURVEY_PER_SECTION_CAP:
            continue
        seen[key] = seen.get(key, 0) + 1
        kept.append(r)
        papers.add(r.arxiv_id)
        sections.add(key)
        if len(kept) >= top_k * SURVEY_OUTPUT_FACTOR:
            break

    return StrategyResult(
        results=kept,
        params={
            "top_k": top_k,
            "breadth": SURVEY_BREADTH,
            "per_section_cap": SURVEY_PER_SECTION_CAP,
            "output": top_k * SURVEY_OUTPUT_FACTOR,
        },
        hops=[
            {"hop": 1, "action": f"广度召回 {n} 条/路", "dense": len(dense), "sparse": len(sparse)},
            {"hop": 2, "action": "按（论文, 章节）去重",
             "in": len(merged), "out": len(kept),
             "papers": len(papers), "sections": len(sections)},
        ],
        notes=[f"输出 {len(kept)} 条，覆盖 {len(papers)} 篇 / {len(sections)} 节；聚类交给步骤 08"],
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        # 综述题同理：能不能答，看的是"是否铺开到了多篇/多节"，
        # 而不是某一段的融合分够不够高。铺开了就该让 LLM 去归纳。
        reject_threshold=0.0 if len(papers) >= 2 else weighted_threshold(0.7, 0.3),
    )


# ---------------------------------------------------------------------------
# 4. nav：全文精确匹配 + 段落定位
# ---------------------------------------------------------------------------

def _nav_anchors(slots: dict[str, Any], question: str) -> list[str]:
    """
    精确匹配的锚点，按可信度排序：
      1. 引号里的片段（用户明确要定位这句话）——≥2 字符就收，中文短语本来就短
      2. 问题里的拉丁实体/术语——≥4 字符，太短的没有区分度
      3. 问题里的数字（如 "2.1"）——≥3 字符，单个数字满篇都是
    中文长句不做整句子串匹配——PDF 抽出来的正文会断行、缺空格，整句几乎必 miss，
    真正要定位的那句通常就在引号里。
    """
    anchors: list[str] = []
    for q in slots.get("quoted") or []:
        if len(q.strip()) >= 2:
            anchors.append(q.strip())
    for tok in slots.get("entities") or []:
        if len(tok) >= 4:
            anchors.append(tok)
    for num in slots.get("numbers") or []:
        if len(num) >= 3:
            anchors.append(num)
    return list(dict.fromkeys(anchors))


def run_nav(
    retrieval: RetrievalService,
    query: str,
    plan_slots: dict[str, Any],
    top_k: int,
    rrf_k: int,
    corpus: dict[str, Any],
    query_vec: list[float] | None = None,
) -> StrategyResult:
    started = time.perf_counter()
    anchors = _nav_anchors(plan_slots, query)
    hops: list[dict[str, Any]] = []
    results: list[RetrievalResult] = []
    anchor_of: dict[str, str] = {}   # chunk_id → 命中的锚点（后面用来切上下文）

    scanned = 0
    if corpus.get("chunks") and len(corpus["chunks"]) <= NAV_SCAN_LIMIT and anchors:
        # 真正的全文精确匹配：逐片段做归一化子串查找，命中越长的锚点分越高。
        squashed_anchors = [(a, _squash(a)) for a in anchors]
        for rec in corpus["chunks"]:
            pl = rec["payload"]
            text = _squash(pl.get("text") or "")
            if not text:
                continue
            scanned += 1
            best_len = 0
            hit_anchor = ""
            for a, sa in squashed_anchors:
                if sa and sa in text and len(a) > best_len:
                    best_len = len(a)
                    hit_anchor = a
            if not best_len:
                continue
            # 分数只用来排序：锚点长度占主导，参考文献片段降权（术语多在正文里）
            weight = 1.0 if (pl.get("kind") or "") != "reference" else 0.4
            score = round(best_len / max(4, len(query)) + weight * 0.3, 4)
            res = _payload_to_result(pl, rec["id"], score)
            anchor_of[res.chunk_id] = hit_anchor
            results.append(res)

        results.sort(key=lambda r: r.score, reverse=True)
        hops.append({
            "hop": 1,
            "action": "全文归一化子串扫描",
            "scanned": scanned,
            "anchors": anchors,
            "matched": len(results),
        })

    # 字面命中数要单独记下来：拒答判断看的是"有没有真的在原文里找到这句话"，
    # 而不是"问句里有没有可用的锚点"。有锚点但一个字都没匹配上，
    # 恰恰是"库里没有这句话"的表现，这种时候必须允许拒答。
    literal_hits = len(results)

    # 字面命中不足时补语义/关键词召回：nav 问句常常只有半句话，
    # 用户真正要找的那句可能引号没引全，靠字面会漏。
    if len(results) < top_k:
        before = len(results)
        dense = retrieval.dense_search(query, top_k=top_k * 2, query_vec=query_vec)
        sparse = retrieval.sparse_search(query, top_k=top_k * 2)
        filler = _rrf_merge(dense, sparse, top_k=top_k * 2, rrf_k=rrf_k,
                            dense_weight=0.4, sparse_weight=0.6)
        _normalize_scores(filler)
        have = {r.chunk_id for r in results}
        for r in filler:
            if r.chunk_id in have:
                continue
            # 补进来的结果分数压在字面命中之下，别抢真正精确匹配的位置
            r.score = round(r.score * 0.45, 4)
            results.append(r)
            if len(results) >= top_k:
                break
        hops.append({
            "hop": 2,
            "action": "字面命中不足，补稀疏召回",
            "literal": before,
            "added": len(results) - before,
        })

    # 命中片段定位：给前端一段可以直接高亮的上下文
    for r in results:
        anchor = anchor_of.get(r.chunk_id)
        if not anchor:
            continue
        text = r.text or ""
        idx = _squash(text).find(_squash(anchor))
        if idx < 0:
            continue
        start = max(0, idx - NAV_CONTEXT_CHARS // 2)
        r.snippet = f"…{text[start: start + len(anchor) + NAV_CONTEXT_CHARS]}…"

    return StrategyResult(
        results=results[:top_k],
        params={
            "top_k": top_k,
            "anchors": anchors,
            "scan_limit": NAV_SCAN_LIMIT,
            "min_match_chars": NAV_MIN_MATCH_CHARS,
        },
        hops=hops,
        notes=[
            "全文精确匹配优先，语义召回仅作补充且分数压制在字面命中之下",
            f"字面命中 {literal_hits} 段"
            + ("，按命中即采信放行" if literal_hits else "，全程靠补召回，仍按分数阈值把关"),
        ],
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        # 字面命中是比 RRF 更强的证据，不该再用 RRF 阈值去否它；
        # 只有"全靠补召回"时才回到 0.4/0.6 权重下的阈值。
        reject_threshold=0.0 if literal_hits else weighted_threshold(0.4, 0.6),
    )


# ---------------------------------------------------------------------------
# 5. chitchat / generic
# ---------------------------------------------------------------------------

def run_chitchat() -> StrategyResult:
    """不检索。答案由 QA 层用同一套 LLM 直接给，不带引用也不拒答。"""
    return StrategyResult(
        results=[],
        params={"retrieval": False},
        hops=[{"hop": 0, "action": "跳过检索"}],
        notes=["闲聊类，直接作答，不进入检索链路"],
        direct=True,
        latency_ms=0.0,
    )


def run_generic(
    retrieval: RetrievalService,
    query: str,
    top_k: int,
    rrf_k: int,
    query_vec: list[float] | None = None,
) -> StrategyResult:
    """
    分流前的行为，逐字保留：dense+sparse 各取 2k，RRF(60) 融合取 top_k。
    这是"分类器判不出来"时的兜底，也是评测里"分流前"的基线本身。
    """
    started = time.perf_counter()
    dense = retrieval.dense_search(query, top_k=top_k * 2, query_vec=query_vec)
    sparse = retrieval.sparse_search(query, top_k=top_k * 2)
    fusion = _rrf_merge(dense, sparse, top_k=top_k, rrf_k=rrf_k,
                        dense_weight=1.0, sparse_weight=1.0)
    return StrategyResult(
        results=fusion,
        params={"top_k": top_k, "rrf_k": rrf_k, "dense_weight": 1.0, "sparse_weight": 1.0},
        hops=[{"hop": 1, "action": "dense+sparse 等权 RRF", "dense": len(dense), "sparse": len(sparse)}],
        notes=["兜底路径，与分流前完全一致"],
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        reject_threshold=RRF_MIN_SCORE,
    )
