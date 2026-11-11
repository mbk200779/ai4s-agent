"""
库外补货：库内检索判空之后，去 arXiv 找几篇能回答这个问题的论文，供一键入库。

**为什么只接 arXiv，不接 S2 / OpenAlex**

1. 推荐出来的每一条都必须能一键入库，而入库链路只认 arXiv ID。S2 / OpenAlex
   里搜到但没有 arXiv 版本的条目，用户点「导入」只能拿到一个失败——那比不推荐更糟。
2. S2 无 key 时实测连续 429（本项目就没有 key），把它做成主路径等于把功能交给运气；
   OpenAlex 对 arXiv 的覆盖不全，同样有"推荐了导不进来"的问题。
3. arXiv 无 key、无配额；实测关键词检索 1~2 s，且直接给出 PDF 地址。

**三条约束（决定了这个文件怎么写）**

- **相关性说明必须可核对**：命中词、命中位置（标题 / 摘要）、摘要里命中最多的那一句，
  全部原样返回，界面照抄。这条链路不调任何模型——空检索（尤其 LLM 配额耗尽时）
  恰恰是这个功能最需要可用的时刻，它不能依赖模型。
  代价是它只能说明"词面对上了"，不能说明"语义相关"；这一点在界面上如实写出来，
  不让它看起来像一句语义判断。
- **搜不到就说搜不到**：检索式逐级放宽（全部词 → 前 3 词 → 前 2 词），全空就返回空列表，
  并把真正试过的检索式带回去，不凑一个结果。
- **不重复推荐库里已有的**：已在库的条目从结果里剔除、单独计数。用户点了「导入」
  才发现「已存在」，是这条路径上最没必要的一次往返。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Sequence

import feedparser
import requests

from app.db.connection import reader
from app.services.library_service import _status_label
from app.services.rag.uncertainty import _latin_terms, _term_hit

ARXIV_API = "https://export.arxiv.org/api/query"
SOURCE = {"name": "arXiv", "url": "https://arxiv.org"}

# 单次请求超时。前端一挂载就会调这个接口，上游慢不能变成页面无限转圈：
# 超时按"这次没搜到"处理，用户手上还剩「重新检索」这一个明确动作。
REQUEST_TIMEOUT_S = 8.0

# 逐级放宽最多三次。每次约 1~2 s，加起来的等待上限还在"读一段说明"的量级内。
MAX_ATTEMPTS = 3

# 多取一些再自己重排：要剔掉库里已有的，还要按词面覆盖度排，5 条结果不够挑。
FETCH_MULTIPLIER = 3
MIN_FETCH = 12

# 命中缓存 10 分钟：用户改一个字再问、或者刷新页面重问，都不该再打一次 arXiv。
CACHE_TTL_S = 600.0
_CACHE_MAX = 128

# 排队用：接口是同步函数（跑在线程池里），缓存要显式加锁。
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, list[dict[str, Any]], list[str]]] = {}

# 专供「构造外部检索式」的过滤词。
#
# 与 `rag/uncertainty.py` 的 `_LATIN_STOP` 分工不同，别合并：
# 那个表决定"关键词在库内有没有命中"，改一个字都会改变空检索的判定；
# 这张表只影响发去 arXiv 的检索式，改错了最多是召回变差，不会让已有的拒答逻辑漂。
#
# 这些词在问句里几乎不携带主题信息，留着只会把 AND 召回压到 0，
# 或者把结果带到同形不同义的论文上（实测 "core" 会把 Transformer vs RNN
# 的问题带到《Robust Core-Periphery Constrained Transformer》）。
_QUERY_STOP = {
    "core", "main", "key", "basic", "general", "overall", "mainly",
    "improve", "improved", "improvement", "improvements",
    "compare", "compared", "comparing", "comparison", "differences", "difference",
    "advantage", "advantages", "disadvantage", "better", "best", "worse",
    "method", "methods", "methodology", "approach", "approaches",
    "technique", "techniques", "model", "models", "large", "small",
    "language", "languages", "work", "works", "paper", "papers", "study", "studies",
    "research", "result", "results", "problem", "problems", "issue", "issues",
    "novel", "new", "recent", "state", "art", "calculate", "calculation", "compute",
    "computing", "exactly", "often", "usually",
}

LEVELS = ((0.80, "强"), (0.50, "中"), (0.0, "弱"))


# ---------------------------------------------------------------------------
# 文本与检索式
# ---------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    """arXiv 的标题/摘要在 Atom 里带换行和双空格，统一压成一行。"""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _query_terms(text: str) -> list[str]:
    """
    问句 → 检索词。

    先按 uncertainty 的同一套规则取英文实词（同一份词表、同一个最短长度），
    再滤掉 `_QUERY_STOP` 里的空壳词。全被滤掉时退回原表：
    宁可检索式偏宽，也不要拿一个空检索式去问 arXiv。
    """
    raw = _latin_terms(text, 12)
    kept = [t for t in raw if t not in _QUERY_STOP]
    return kept if len(kept) >= 2 else raw


def _ladder(terms: Sequence[str]) -> list[str]:
    """
    逐级放宽的检索式。arXiv 的 AND 很硬：五个词全 AND 经常直接 0 条，
    而"这次没有"和"检索式太严"在界面上长得一模一样，所以必须自己退一步。

    顺序按问句原序取前缀，而不是按词长排序：实测按词长排会先丢掉 "rag" 这种
    短但决定主题的词（"hallucinations AND alleviate AND language" 反而 0 条）。
    """
    out: list[str] = []
    for k in (min(len(terms), 5), 3, 2):
        if k < 2:
            continue
        q = " AND ".join(f"all:{t}" for t in list(terms)[:k])
        if q not in out:
            out.append(q)
    return out[:MAX_ATTEMPTS]


def _fetch(query: str, n: int) -> list[dict[str, Any]]:
    """
    直接打 arXiv Atom 接口，不用 arxiv 那个库。

    原因只有一个：那个库的 `Client` 不暴露请求超时，上游卡住时线程会一直挂着；
    而这个接口是页面一挂载就自动调的，必须有明确的超时边界。
    （顺带绕开它默认 http 端点 301 到 https 的老问题，那个坑见 arxiv_client.py。）
    """
    resp = requests.get(
        ARXIV_API,
        params={
            "search_query": query,
            "start": 0,
            "max_results": n,
            "sortBy": "relevance",
            "sortOrder": "descending",
        },
        timeout=REQUEST_TIMEOUT_S,
        headers={"User-Agent": "ai4s-agent/1.0 (library recovery)"},
    )
    resp.raise_for_status()
    return _parse_feed(resp.content)


def _parse_feed(raw: bytes) -> list[dict[str, Any]]:
    feed = feedparser.parse(raw)
    out: list[dict[str, Any]] = []
    for e in feed.entries:
        link = (e.get("id") or e.get("link") or "").strip()
        m = re.search(r"abs/([^/]+?)/?$", link)
        if not m:
            continue
        short = m.group(1)
        head, sep, tail = short.rpartition("v")
        arxiv_id, version = (head, f"v{tail}") if sep and tail.isdigit() else (short, "v1")
        published = (e.get("published") or "")[:10]
        authors = [a.get("name") for a in (e.get("authors") or [])]
        out.append({
            "arxiv_id": arxiv_id,
            "version": version,
            "title": _norm(e.get("title")),
            "abstract": _norm(e.get("summary")),
            "authors": [a for a in authors if a][:8],
            "author_count": len(authors),
            "published": published or None,
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "primary_category": (e.get("arxiv_primary_category") or {}).get("term"),
            "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        })
    return out


# ---------------------------------------------------------------------------
# 相关性：只陈述词面证据
# ---------------------------------------------------------------------------


def _best_sentence(abstract: str, terms: Sequence[str]) -> tuple[str, int]:
    """
    摘要里命中最多的那一句，返回 (原句, 命中几个词)。

    为什么值得单独取一句、还要参与打分：只有 `["rag", "hallucinations"]` 这种词表，
    读者仍要自己去摘要里找对应关系；把那一句挑出来，"为什么推荐它"就变成可以直接读的
    东西。**同句共现**也比"分散在摘要各处"强得多——一句里同时出现 rag 与 hallucination，
    基本就是这个句子在讲这件事；两个词各出现在一句里，可能只是这篇论文的常规背景段落。
    """
    if not abstract:
        return "", 0
    best, best_n = "", 0
    for sent in re.split(r"(?<=[.!?])\s+", abstract):
        n = sum(1 for t in terms if _term_hit(t, sent.lower()))
        if n > best_n:
            best, best_n = sent, n
    if best_n == 0:
        return "", 0
    if len(best) > 260:
        best = best[:258].rstrip() + "…"
    return best, best_n


def _affinity(terms: Sequence[str], title: str, abstract: str) -> dict[str, Any]:
    """
    词面相关性。三项信号，权重不同：

    - 标题命中 ×2 —— 标题写了这件事，这篇大概率就是讲这件事；
    - 摘要命中 ×1 —— 只说明它被提到过；
    - 摘要同句共现 ×2 —— 一句话里同时出现几个问句关键词，是最接近"这一段在讲这个问题"的
      证据（与 12 号空检索判定用「同段覆盖度」而不是「词是否命中」是同一个理由）。

    满分 5N，归一到 0~1 只用于排序；界面上的"强/中/弱"按**关键词覆盖率**给，
    因为它可以直接对上旁边那行"问句 3 个关键词命中 2 个"，可核对。
    """
    title_hits = [t for t in terms if _term_hit(t, title.lower())]
    abstract_hits = [t for t in terms if _term_hit(t, abstract.lower())]
    hits = sorted(set(title_hits) | set(abstract_hits))
    evidence, spot_hits = _best_sentence(abstract, terms)

    n = len(terms) or 1
    raw = (2 * len(title_hits) + len(abstract_hits) + 2 * spot_hits) / (5 * n)
    coverage = len(hits) / n
    level = next(label for cut, label in LEVELS if coverage >= cut)

    bits: list[str] = []
    if title_hits:
        bits.append(f"标题命中 {'、'.join(title_hits)}")
    extra = [t for t in abstract_hits if t not in title_hits]
    if extra:
        bits.append(f"摘要命中 {'、'.join(extra)}")
    bits.append(f"问句 {len(terms)} 个关键词命中 {len(hits)} 个")

    return {
        "score": round(min(raw, 1.0), 2),
        "coverage": round(coverage, 2),
        "level": level,
        "terms": list(terms),
        "title_hits": title_hits,
        "abstract_hits": abstract_hits,
        "hits": hits,
        "evidence": evidence,
        "text": "；".join(bits) + "。",
    }


# ---------------------------------------------------------------------------
# 库内已有：用于去重
# ---------------------------------------------------------------------------


def _title_key(title: str | None) -> str:
    """标题归一：小写、只留字母数字与汉字、空白折叠成单空格。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", (title or "").lower()).strip()


def _library_snapshot() -> tuple[dict[str, tuple[str, str]], set[str]]:
    """
    返回 (arxiv_id → 状态, 归一化标题集合)，取当前版本的状态。

    直接读 SQLite 而不是 Qdrant 的 meta 集合：解析失败、正在解析的论文在 Qdrant
    里还没有点，但它们确实已经在库中，再推荐一遍只会让用户白点一次导入。
    状态口径复用 library_service，避免"清单说解析中、推荐说没入库"这种两套说法。

    **为什么要按标题再判一次**：本地 PDF 上传进来的论文，库里存的 arxiv_id 是
    `upload_<hash>`，不是真实的 arXiv ID。只按 ID 判重的话，「Attention Is All You Need」
    这种"自己上传过、又被 arXiv 推荐出来"的情况会被当成新论文，导进来就是第二行同名记录。
    归一化标题相等即视为已在库——误判的代价是少一条推荐，比多一条重复记录轻。
    """
    with reader() as c:
        rows = c.execute(
            """SELECT p.arxiv_id AS arxiv_id, p.title AS title,
                      v.status AS status, v.stage AS stage
                 FROM papers p
                 LEFT JOIN paper_versions v
                        ON v.paper_id = p.id AND v.is_current = 1"""
        ).fetchall()
    by_id: dict[str, tuple[str, str]] = {}
    titles: set[str] = set()
    for row in rows:
        d = dict(row)
        aid = str(d.get("arxiv_id") or "")
        if aid:
            by_id[aid] = _status_label(d.get("status"), d.get("stage"))
        key = _title_key(d.get("title"))
        if key:
            titles.add(key)
    return by_id, titles


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def _cache_get(key: str) -> tuple[list[dict[str, Any]], list[str]] | None:
    with _LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        ts, metas, tried = hit
        if time.time() - ts > CACHE_TTL_S:
            _CACHE.pop(key, None)
            return None
        # 返回副本：调用方会往里塞 relevance / in_library 这类"这一次"的字段，
        # 直接给引用会让下一次命中缓存时带着上一次的痕迹。
        return [dict(m) for m in metas], list(tried)


def _cache_put(key: str, metas: list[dict[str, Any]], tried: list[str]) -> None:
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # 简单粗暴地丢最早写入的那一条：这里存的是外部检索结果，
            # 丢了最多多打一次 arXiv，不值得为它引入 LRU 结构。
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = (time.time(), [dict(m) for m in metas], list(tried))


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def _payload(status: str, message: str, **kw: Any) -> dict[str, Any]:
    base = {
        "status": status,
        "message": message,
        "question": "",
        "search_text": "",
        "terms": [],
        "queries": [],
        "source": SOURCE,
        "elapsed_ms": 0,
        "cached": False,
        "skipped_in_library": 0,
        "candidates": [],
    }
    base.update(kw)
    return base


def recommend(
    question: str,
    search_text: str | None = None,
    limit: int = 5,
    force: bool = False,
) -> dict[str, Any]:
    """
    按问题在 arXiv 找库里没有的论文。

    `search_text` 用问答链路已经翻好的英文检索文本（空检索卡片上"实际检索文本"
    那一行就是它）。传了就不必再翻一次——少一次模型调用，也少一份"两次翻译不一样"
    的不确定性。没传且问句是中文时不做翻译：arXiv 只认英文，此时如实说明，
    而不是拿中文去检索再回一句"没找到"。
    """
    started = time.time()
    text = _norm(search_text or question)
    terms = _query_terms(text)

    if not terms:
        return _payload(
            "no_query",
            "这句问话里没有可用的英文实词，arXiv 只支持英文检索。"
            "用英文关键词再问一次，或直接在论文库里导入相关论文。",
            question=question,
            search_text=text,
        )

    key = f"{text.lower()}|{limit}"
    cached = None if force else _cache_get(key)
    already_cached = cached is not None

    metas: list[dict[str, Any]] = []
    tried: list[str] = []
    if cached is not None:
        metas, tried = cached
    else:
        n = max(limit * FETCH_MULTIPLIER, MIN_FETCH)
        lib, lib_titles = _library_snapshot()
        for query in _ladder(terms):
            tried.append(query)
            try:
                got = _fetch(query, n)
            except Exception as exc:  # 网络 / 上游 5xx / 超时
                if not metas:
                    return _payload(
                        "error",
                        f"arXiv 检索失败（{type(exc).__name__}），稍后再试；"
                        "这不影响库内已有的论文。",
                        question=question,
                        search_text=text,
                        terms=terms,
                        queries=tried,
                        elapsed_ms=int((time.time() - started) * 1000),
                    )
                break
            if got:
                metas = got
                # 剔掉库里已有的之后再判断"够不够"：
                # 10 条里 9 条已在库，剩 1 条不是一次合格的推荐。
                fresh = sum(
                    1 for m in got
                    if m["arxiv_id"] not in lib and _title_key(m["title"]) not in lib_titles
                )
                if fresh >= limit:
                    break
            if len(tried) >= MAX_ATTEMPTS:
                break
        _cache_put(key, metas, tried)

    lib, lib_titles = _library_snapshot()
    skipped = 0
    weak = 0
    scored: list[dict[str, Any]] = []
    for idx, m in enumerate(metas):
        if m["arxiv_id"] in lib or _title_key(m["title"]) in lib_titles:
            skipped += 1
            continue
        rel = _affinity(terms, m["title"], m["abstract"])
        # 至少要有两个关键词落在标题或摘要里，才解释得出"为什么推荐它"。
        # 只要一个就够的话，检索式放宽到两词时"quantum AND ..."会把一堆量子论文
        # 全部带进来——它们命中的是那个泛词，不是这个问题。
        if len(rel["hits"]) < 2:
            weak += 1
            continue
        scored.append({
            **m,
            "relevance": rel,
            "in_library": False,
            # arXiv 自己的相关度顺序只作**同分时的次序**：它对"core / improvement"
            # 这类空壳词并不敏感，而我们按 0.7/0.3 混合会排出一列分数与"强/中/弱"
            # 互相矛盾的顺序——界面上那行分数就在卡片上，顺序必须对得上它。
            "_rank": 1.0 - idx / max(len(metas), 1),
        })

    scored.sort(key=lambda c: (c["relevance"]["score"], c["_rank"]), reverse=True)
    candidates = scored[:limit]
    for c in candidates:
        c.pop("_rank", None)

    elapsed = int((time.time() - started) * 1000)
    common = {
        "question": question,
        "search_text": text,
        "terms": terms,
        "queries": tried,
        "elapsed_ms": elapsed,
        "cached": already_cached,
        "skipped_in_library": skipped,
    }

    if not candidates:
        if skipped:
            msg = (
                f"arXiv 检索到的 {skipped} 篇都已在库里。既然它们在库内没能答上这个问题，"
                "说明还缺更贴题的论文——换个更具体的说法再搜一次。"
            )
        elif weak:
            msg = (
                f"arXiv 回来 {weak} 篇，但没有一篇在标题或摘要里同时出现两个以上的问句关键词。"
                "这类条目是被全文匹配带出来的，导进来也答不上这个问题，所以没有推荐。"
            )
        else:
            msg = "arXiv 里也没有匹配这几个关键词的论文。可以把问题拆得更具体一些，或换用领域内的标准术语。"
        return _payload("no_result", msg, **common)

    tail = f"，另有 {skipped} 篇已在库中未重复推荐" if skipped else ""
    return _payload(
        "ok",
        f"在 arXiv 找到 {len(candidates)} 篇库里没有的论文{tail}。导入后可直接用原问题重问。",
        candidates=candidates,
        **common,
    )
