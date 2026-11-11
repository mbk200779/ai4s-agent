"""
跨语言证据核验：算「中文结论 ↔ 英文原句」的语义相似度。

为什么单靠 `anchor_select` 的字面分不够（这也是 07 必须补这一层的原因）：
答案是用中文写的，原文是英文。字面打分只认 ASCII 词与数字，于是
「BERT 的预训练用了 MLM 和 NSP」这种**完全正确**的中文结论，
在原句上的字面分只有 0.25——按字面线只能判 needs_check。
结果是 `verified` 这一档除了数值题之外几乎不可达，三档就退化成了"两档 + 一个摆设"。

为什么用 paraphrase-multilingual-MiniLM-L12-v2，而不是链路里已有的 e5-large：
- e5-large 是 1024 维、24 层，实测单条短句 ~150ms，一次问答案要核 3~6 对，
  加进去就是 0.9s，会把总时长从 2s 拖到 3s；
- MiniLM-L12 是 384 维、12 层，实测 12 条 82ms、单条 5ms，一次问答只要 30~80ms；
- 它本身就是**跨语言释义相似度**训练出来的，正好对上"中文改写 ↔ 英文原句"这个任务；
- 不替换 e5：检索用的向量必须与索引一致，换模型等于重建全库。

校准（实测，见 docs/2.11 的表）：
    同义改写/直译对   0.51 ~ 0.91
    无关句对          0.008 ~ 0.18
所以把 [0.35, 0.85] 线性映射到 [0, 1] 作为"语义支持度"，
与字面分取较大者参与定档——两簇之间的空档从 0.18 到 0.51 很宽，
边界放在映射区间里不会因为一两句话的措辞波动而左右横跳。

失败即降级：模型加载不出来（离线、缺文件）时返回 None，
定档退回纯字面分——宁可保守判 needs_check，也不能因为一个可选模型
把整条问答打成 500。
"""
from __future__ import annotations

import hashlib
import math
import threading
from typing import Sequence

from app.core.config import settings

_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_lock = threading.Lock()
_model = None
_load_failed = False

# 同一句话会被反复核验（同一问题重问、同一段落被多次引用）。
# 缓存的是向量不是分数：句子换了个说法就没法复用，向量可以。
_cache: dict[str, list[float]] = {}
_CACHE_MAX = 2048


def enabled() -> bool:
    return bool(settings.confidence_semantic_enabled)


def _get_model():
    """懒加载单例。加载失败一次就不再重试，避免每条请求都卡在同一个异常上。"""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _lock:
        if _model is not None or _load_failed:
            return _model
        try:
            from fastembed import TextEmbedding

            _model = TextEmbedding(
                model_name=_MODEL_NAME,
                threads=max(1, int(settings.embed_threads)),
            )
        except Exception:
            _load_failed = True
            _model = None
    return _model


def warm() -> bool:
    """启动时预热。返回是否就绪——预热的唯一目的是让首个请求不必等模型加载。"""
    if not enabled():
        return False
    model = _get_model()
    if model is None:
        return False
    try:
        list(model.embed(["warmup"]))
        return True
    except Exception:
        return False


def _embed(texts: Sequence[str]) -> list[list[float]] | None:
    model = _get_model()
    if model is None:
        return None
    out: list[list[float]] = []
    missing: list[str] = []
    missing_at: list[int] = []
    for i, t in enumerate(texts):
        key = hashlib.sha1(t.encode("utf-8")).hexdigest()
        hit = _cache.get(key)
        if hit is None:
            missing.append(t)
            missing_at.append(i)
            out.append([])
        else:
            out.append(hit)

    if missing:
        try:
            computed = [list(map(float, v)) for v in model.embed(missing)]
        except Exception:
            return None
        for pos, vec in zip(missing_at, computed):
            out[pos] = vec
            if len(_cache) < _CACHE_MAX:
                _cache[hashlib.sha1(texts[pos].encode("utf-8")).hexdigest()] = vec
    return out


def _norm(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def embed_many(texts: Sequence[str]) -> list[list[float]] | None:
    """
    批量取向量（综述聚类用）。

    为什么单独开一个口子而不是让调用方直接 `raw_similarities`：
    聚类要的是 n×n 矩阵，用「两两配对」去算等于把同一句话编码 n 次；
    这里一次编码、调用方自己点积，40 条 claim 只要 40 次编码而不是 1600 次。
    返回的向量**未归一化**，调用方自行归一化——归一化逻辑只该有一份。
    """
    if not enabled():
        return None
    vecs = _embed([t or "" for t in texts])
    if vecs is None:
        return None
    if any(not v for v in vecs):
        return None
    return vecs


def raw_similarities(pairs: Sequence[tuple[str, str]]) -> list[float | None]:
    """逐对算余弦。任何一步失败都返回 None，调用方据此退回字面分。"""
    if not enabled() or not pairs:
        return [None] * len(pairs)

    left = [a or "" for a, _ in pairs]
    right = [b or "" for _, b in pairs]
    va = _embed(left)
    vb = _embed(right)
    if va is None or vb is None:
        return [None] * len(pairs)

    out: list[float | None] = []
    for x, y in zip(va, vb):
        if not x or not y:
            out.append(None)
            continue
        nx, ny = _norm(x), _norm(y)
        out.append(round(sum(p * q for p, q in zip(nx, ny)), 4))
    return out


def support_scores(pairs: Sequence[tuple[str, str]]) -> list[float | None]:
    """
    余弦 → 0~1 的"语义支持度"。

    映射区间取 [0.35, 0.85]：低于 0.35 的相关对在本语料里没有见过
    （无关对实测 0.008~0.18），高于 0.85 基本就是同一句话的翻译。
    映射而不是直接卡阈值，是为了让分档只依赖一条 `verified_score` 线，
    不用再维护第二套"语义阈值"，否则调阈值时得同时改两个数、还容易改漏。
    """
    floor = float(settings.confidence_semantic_floor)
    ceil = float(settings.confidence_semantic_ceiling)
    span = max(1e-6, ceil - floor)
    out: list[float | None] = []
    for cos in raw_similarities(pairs):
        if cos is None:
            out.append(None)
        else:
            out.append(round(min(1.0, max(0.0, (cos - floor) / span)), 4))
    return out
