"""
参考文献 → 库内论文的匹配。

匹配是图谱里唯一会出错的地方，错了整张图的边都是错的，所以这里不做"阈值一刀切"，
而是先看命中了哪一级证据，再给分：

| 级别 | 证据 | 分值 | 为什么可信 |
|---|---|---|---|
| 1 | 条目原文里出现库内论文的 arXiv ID | 0.99 | 强标识符，不会撞车 |
| 2 | 库内标题（归一化后 ≥18 字）作为子串出现在条目原文里 | 0.95 | 标题是逐字抄的；再加作者姓氏复核 |
| 3 | 解析出的标题与库内标题归一化后完全相等 | 0.93 | 抄写时可能少了副标题，但主体一致 |
| 4 | 标题词集 Dice ≥ 0.78 | 0.55+0.40·dice | 允许断词、缩写、大小写差异 |
| 5 | Dice ≥ 0.55 且第一作者姓氏命中 | 0.55+0.35·dice | 标题残缺时作者兜底 |
| 6 | Dice ≥ 0.50 且作者、年份都对得上 | 0.72 | 最弱的一档，仅在有双证据时放行 |

低于 `MATCH_MIN=0.70` 的一律返回 unmatched，并把"最接近的候选 + 分数"一起带出去——
这样界面能说"最接近的是 X，但只有 0.41"，比一句"未匹配"有用得多。
不猜是刻意的：错误关联会污染 PageRank，而漏匹配只是少一条边。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

MATCH_MIN = 0.70
# 归一化标题作为子串时的最小长度：太短的标题（"BERT"、"LoRA"）到处都能撞上
MIN_CONTAIN_LEN = 18

_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st"}
_ARXIV_IN_TEXT = re.compile(r"(?:arxiv[:./]?(?:abs/|pdf/)?)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_NAME_SPLIT = re.compile(r"[\s,]+")


def normalize(text: str | None) -> str:
    """标题归一化：连字还原 → 只留字母数字汉字 → 压空白。"""
    s = (text or "")
    for k, v in _LIGATURES.items():
        s = s.replace(k, v)
    s = s.lower()
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str | None) -> list[str]:
    return [t for t in normalize(text).split(" ") if t]


def dice(a: Iterable[str], b: Iterable[str]) -> float:
    """Sørensen–Dice：对短标题比 Jaccard 宽容，不会因为少一个词就掉到 0.5。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return 2 * len(sa & sb) / (len(sa) + len(sb))


def _surname(name: str) -> str:
    """
    取姓氏。中文名取第一段，西文名取最后一段；"Vaswani, Ashish" 这种逗号格式取逗号前。
    参考文献里的作者常写成 "Vaswani, A." 或 "A. Vaswani"，两种都要能对上。
    """
    name = (name or "").strip().strip(".,")
    if not name:
        return ""
    if "," in name:
        head = name.split(",")[0].strip()
        return normalize(head).replace(" ", "")
    parts = [p for p in _NAME_SPLIT.split(name) if len(p) > 1 and not p.endswith(".")]
    if not parts:
        return ""
    return normalize(parts[-1]).replace(" ", "")


@dataclass
class Candidate:
    """库内候选：一篇论文（不是版本），带匹配需要的三个字段。"""

    paper_id: str
    title: str
    arxiv_id: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    title_norm: str = ""
    surnames: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.title_norm = normalize(self.title)
        self.surnames = {_surname(a) for a in self.authors if _surname(a)}


@dataclass
class MatchOutcome:
    paper_id: str | None
    score: float
    method: str
    evidence: dict[str, Any] = field(default_factory=dict)
    runner_up: dict[str, Any] | None = None

    @property
    def matched(self) -> bool:
        return self.paper_id is not None


def build_candidates(papers: Sequence[dict[str, Any]]) -> list[Candidate]:
    """papers 行：id / title / arxiv_id / authors(JSON 字符串或 list) / year。"""
    out: list[Candidate] = []
    for p in papers:
        authors = p.get("authors")
        if isinstance(authors, str):
            try:
                authors = json.loads(authors)
            except Exception:  # noqa: BLE001 - 老数据可能是逗号分隔的裸串
                authors = [a.strip() for a in authors.split(",") if a.strip()]
        out.append(
            Candidate(
                paper_id=p["id"],
                title=p.get("title") or "",
                arxiv_id=(p.get("arxiv_id") or "").strip(),
                authors=list(authors or []),
                year=p.get("year"),
            )
        )
    return out


def _arxiv_ids(text: str) -> set[str]:
    return {m.group(1) for m in _ARXIV_IN_TEXT.finditer(text or "")}


def _score(
    ref: dict[str, Any], cand: Candidate
) -> tuple[float, str, dict[str, Any]]:
    raw = ref.get("raw") or ref.get("raw_text") or ""
    parsed_title = ref.get("title") or ref.get("parsed_title") or ""
    raw_norm = normalize(raw)
    title_norm = normalize(parsed_title or raw)
    ref_surnames = {_surname(a) for a in (ref.get("authors") or ref.get("parsed_authors") or [])}
    ref_year = ref.get("year") or ref.get("parsed_year")

    # 证据 1：arXiv ID
    if cand.arxiv_id and cand.arxiv_id in _arxiv_ids(raw):
        return 0.99, "arxiv_id", {"arxiv_id": cand.arxiv_id}

    # 证据 2：库内标题整段出现在条目原文里
    author_hit = bool(cand.surnames & ref_surnames)
    contained = len(cand.title_norm) >= MIN_CONTAIN_LEN and cand.title_norm in raw_norm
    if contained:
        # 有作者数据却没命中 → 标题撞车的可能性上升（比如两篇论文标题互相包含）
        if author_hit or not cand.surnames:
            return 0.95, "title_in_raw", {"contained": True, "author_hit": author_hit}
        return 0.74, "title_in_raw_weak", {"contained": True, "author_hit": False}

    # 证据 3：标题归一化后相等
    if cand.title_norm and title_norm == cand.title_norm:
        return 0.93, "title_exact", {"title_equal": True, "author_hit": author_hit}

    d = dice(tokens(parsed_title or raw), tokens(cand.title))
    year_gap = None
    if ref_year and cand.year:
        year_gap = abs(int(ref_year) - int(cand.year))
    ev = {"dice": round(d, 3), "author_hit": author_hit, "year_gap": year_gap}

    # 证据 4/5/6：标题相似度分层
    if d >= 0.78:
        return round(0.55 + 0.40 * d, 4), "title_fuzzy", ev
    if d >= 0.55 and author_hit:
        return round(0.55 + 0.35 * d, 4), "title_author", ev
    if d >= 0.50 and author_hit and year_gap is not None and year_gap <= 1:
        return 0.72, "author_year", ev
    return round(d * 0.60, 4), "below_threshold", ev


def match_ref(
    ref: dict[str, Any],
    candidates: Sequence[Candidate],
    *,
    self_paper_id: str | None = None,
) -> MatchOutcome:
    """单条文献匹配。排除自引（同一篇论文不可能引用自己，除非是版本关系被误配）。"""
    scored: list[tuple[float, str, Candidate, dict[str, Any]]] = []
    for cand in candidates:
        if self_paper_id and cand.paper_id == self_paper_id:
            continue
        score, method, ev = _score(ref, cand)
        scored.append((score, method, cand, ev))

    if not scored:
        return MatchOutcome(None, 0.0, "no_candidate", {"candidates": 0})

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_method, best_cand, best_ev = scored[0]
    runner = None
    if len(scored) > 1 and scored[1][0] >= 0.50:
        runner = {
            "paper_id": scored[1][2].paper_id,
            "title": scored[1][2].title,
            "score": scored[1][0],
        }

    if best_score < MATCH_MIN:
        return MatchOutcome(
            None,
            best_score,
            "below_threshold",
            {**best_ev, "best_paper_id": best_cand.paper_id,
             "best_title": best_cand.title, "best_score": best_score},
            runner,
        )

    return MatchOutcome(
        best_cand.paper_id,
        best_score,
        best_method,
        {**best_ev, "candidate_title": best_cand.title,
         "margin": round(best_score - (runner["score"] if runner else 0.0), 3)},
        runner,
    )


def match_refs(
    refs: Sequence[dict[str, Any]],
    candidates: Sequence[Candidate],
    *,
    self_paper_id: str | None = None,
) -> tuple[list[tuple[dict[str, Any], MatchOutcome]], dict[str, Any]]:
    """
    批量匹配并汇总统计。失败按原因分桶——"匹配失败率"如果不分原因，
    就没法判断该去修抽取、修匹配，还是该往库里补论文。
    """
    results: list[tuple[dict[str, Any], MatchOutcome]] = []
    by_method: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    near_miss = 0

    for ref in refs:
        outcome = match_ref(ref, candidates, self_paper_id=self_paper_id)
        results.append((ref, outcome))
        key = outcome.method if outcome.matched else "unmatched"
        by_method[key] = by_method.get(key, 0) + 1
        if not outcome.matched:
            if outcome.score >= 0.45:
                near_miss += 1
            failures.append(
                {
                    "ref_index": ref.get("ref_index") or ref.get("index"),
                    "title": (ref.get("title") or ref.get("parsed_title") or "")[:120],
                    "raw": (ref.get("raw") or ref.get("raw_text") or "")[:160],
                    "best_score": round(outcome.score, 3),
                    "best_title": (outcome.evidence or {}).get("best_title", ""),
                }
            )

    total = len(refs)
    matched = sum(1 for _, o in results if o.matched)
    stats = {
        "total": total,
        "matched": matched,
        "unmatched": total - matched,
        "rate": round(matched / total, 4) if total else 0.0,
        "by_method": by_method,
        "near_miss": near_miss,
        "candidates": len(candidates),
        "failures": sorted(failures, key=lambda f: -f["best_score"])[:8],
    }
    return results, stats
