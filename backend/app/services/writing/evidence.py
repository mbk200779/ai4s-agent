"""
分段证据池 + 全篇统一的引用编号。

两个和"综述证据池"不同的地方，都是写作场景逼出来的：

1. **编号必须全篇统一**。如果把每段的证据各自从 1 编号，那么第 2 段的 `引#1`
   和第 4 段的 `引#1` 是两段不同的原文，导出后没人能对上号。
   所以 `CiteIndex` 是**全篇唯一**的编号器：同一 chunk 无论出现在几段里，都只有一个号；
   新号按"先出现的先编号"分配，顺序由段落顺序决定，因此可复现。

2. **每段各自检索、各自截断**。写作是分段推进的，"相关工作"和"局限"要的证据不同，
   拿一次全局 top-k 喂给所有段，结果就是每段都在说同一批片段——那又变成摘要拼接了。

证据筛选沿用综述那两条硬规则（只收正文、排除模板回填文案），理由见 `review/pool.py`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.rag import semantic
from app.services.retrieval_service import RetrievalService

# 只收正文段落。表格 chunk 在本项目里是"逐列扫描"的残渣，不能当逐字引文的来源。
ALLOWED_KINDS = {"paragraph", "text"}
MIN_CHUNK_CHARS = 60
# 检索量 ≠ 保留量：先多拿再筛，否则前几条全是表格残渣时整段就没证据了。
# 取 6 而不是 4 是量出来的：每段要 6 条证据时，top-24 里能过 `usable()` 的只有 2~4 段，
# 因为本语料的表格/参考文献 chunk 占比很高；拿到 top-40 才有 6~9 段可选。
RETRIEVE_MULTIPLIER = 6


def _norm_head(text: str, n: int = 60) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())[:n].lower()


# 页眉/版权/版式行。这类片段长度够、kind 也是 paragraph，但**没有任何内容**：
#   "Published as a conference paper at ICLR 2021"、"Proceedings of the 2020 Conference…"
# 它们会占掉一个证据名额，还会在相关性打分里蒙混过关（实测 0.30~0.33），
# 因为它和任何学术主题都"有点像"。内容层面的过滤器管不了它，只能按版式特征认。
_BOILERPLATE_RE = re.compile(
    r"^\s*(?:published\s+as|proceedings\s+of|arxiv\s+preprint|preprint\b|under\s+review|"
    r"copyright|©|all\s+rights\s+reserved|figure\s+\d|table\s+\d|"
    r"published\s+by|workshop\s+on)",
    re.I,
)


def usable(item: dict[str, Any]) -> bool:
    if (item.get("kind") or "") not in ALLOWED_KINDS:
        return False
    text = (item.get("text") or "").strip()
    if len(text) < MIN_CHUNK_CHARS:
        return False
    # 模板回填文案：以"该图表"开头的是我们自己写的说明，不是原文。
    if text.startswith("该图表"):
        return False
    if _BOILERPLATE_RE.match(text):
        return False
    return True


@dataclass
class EvidencePick:
    """一段的证据筛选结果。诊断字段是为了让"占位"能说出原因，而不是一句"没证据"。"""

    chunks: list[dict[str, Any]] = field(default_factory=list)
    candidates: int = 0          # 过了 kind/长度过滤的候选数
    low_relevance: int = 0       # 被相关性下限刷掉的
    semantic_ok: bool = True     # 语义模型是否可用（不可用时不做相关性过滤）

    def gap_hint(self) -> str:
        if not self.candidates:
            return "本段检索不到可用的正文证据（命中的都是表格/参考文献等非正文片段）。"
        if self.low_relevance and not self.semantic_ok:
            return "本段证据未通过相关性检查。"
        if self.low_relevance:
            return (
                f"本段检索到 {self.candidates} 段候选，但 {self.low_relevance} 段"
                "都不在讨论本节的问题（相关性低于阈值），没有可用证据。"
            )
        return "本段没有可用证据。"


@dataclass
class EvidenceUnit:
    """一个可被引用的证据单元 = 一个 chunk。`n` 就是正文里的 `[引#N]`。"""

    n: int
    chunk_id: str
    paper_id: str
    arxiv_id: str
    title: str
    page: int
    section: str
    kind: str
    text: str
    score: float = 0.0
    hit_source: str = ""
    relevance: float | None = None  # 与本节问题的跨语言余弦（语义模型可用时）
    sections: list[str] = field(default_factory=list)  # 被哪几段的**检索**命中（不代表正文引用了）

    def to_dict(self, *, quote_chars: int = 600) -> dict[str, Any]:
        text = self.text or ""
        return {
            "n": self.n,
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "page": self.page,
            "section": self.section,
            "kind": self.kind,
            "text": text[:quote_chars],
            "truncated": len(text) > quote_chars,
            "score": round(float(self.score or 0.0), 4),
            "hit_source": self.hit_source,
            "relevance": self.relevance,
            "hit_in": list(self.sections),
        }


class CiteIndex:
    """
    全篇唯一的 `chunk_id → 引#N` 编号器。

    去重键用 `arxiv_id|page|正文前 60 字` 而不是 chunk_id：
    向量库里存在重入库留下的孤儿点，同一段正文会有两个 point／两个 chunk_id。
    只按 chunk_id 去重会让同一段原文拿到两个引号，读者以为引了两处不同证据。
    """

    def __init__(self) -> None:
        self._by_chunk: dict[str, EvidenceUnit] = {}
        self._by_key: dict[str, int] = {}
        self._items: list[EvidenceUnit] = []

    def assign(self, item: dict[str, Any], *, section_id: str = "") -> EvidenceUnit:
        chunk_id = str(item.get("chunk_id") or "")
        key = f"{item.get('arxiv_id')}|{item.get('page')}|{_norm_head(str(item.get('text') or ''))}"
        if key in self._by_key:
            unit = self._items[self._by_key[key] - 1]
        elif chunk_id in self._by_chunk:
            unit = self._by_chunk[chunk_id]
        else:
            unit = EvidenceUnit(
                n=len(self._items) + 1,
                chunk_id=chunk_id,
                paper_id=str(item.get("paper_id") or ""),
                arxiv_id=str(item.get("arxiv_id") or ""),
                title=str(item.get("title") or ""),
                page=int(item.get("page") or 0),
                section=str(item.get("section") or ""),
                kind=str(item.get("kind") or ""),
                text=str(item.get("text") or ""),
                score=float(item.get("score") or 0.0),
                hit_source=str(item.get("hit_source") or ""),
                relevance=(
                    float(item["relevance"]) if item.get("relevance") is not None else None
                ),
            )
            self._items.append(unit)
            self._by_key[key] = unit.n
            self._by_chunk[chunk_id] = unit
        if section_id and section_id not in unit.sections:
            unit.sections.append(section_id)
        return unit

    def get(self, n: int) -> EvidenceUnit | None:
        if 1 <= n <= len(self._items):
            return self._items[n - 1]
        return None

    def numbered_text(self, numbers: list[int]) -> str:
        """把若干个号拼成证据文本（数值落地校验要用）。"""
        parts = []
        for n in numbers:
            unit = self.get(n)
            if unit:
                parts.append(unit.text)
        return "\n".join(parts)

    @property
    def items(self) -> list[EvidenceUnit]:
        return list(self._items)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._items)


def collect_section_evidence(
    query: str,
    *,
    max_chunks: int,
    per_paper: int,
    min_sim: float | None = None,
    gate_query: str | None = None,
    topic: str = "",
    retrieval: RetrievalService | None = None,
) -> EvidencePick:
    """
    对一段发一次检索，按论文配额筛出证据。

    为什么还要按论文配额：一段 6 条证据如果 5 条来自同一篇，这段就变成那篇的摘要，
    而读者以为它是"多篇文献支持"。写作比综述更容易犯这个错——段落更短，一篇就能填满。

    相关性下限 `min_sim`（跨语言余弦）：

    检索是"总是返回 top-k"的，库里没有这个话题时它照样会端上一堆别的论文的段落。
    实测拿「量子纠错码的表面码容错阈值」去检索，回来的全是 LoRA 论文的正文，
    模型于是写出「低秩适应在量子计算中的应用」——每句都有引用，整段是胡话。
    引用校验拦不住这种错误（引用是真的），只能靠"这段证据到底在不在讲这件事"来拦。

    **闸门用主题、检索用段落 query**（`gate_query`）。这两个不能合并：
    段落 query 很具体（"Transformer 及类似架构在实际应用中遇到的问题或限制"），
    而证据原文是英文的具体句子，跨语言余弦会被措辞差异拖低——实测这一节 4 段证据
    全部低于阈值，整段变成占位，可库里明明有 ViT 的局限讨论。
    换成语义更宽的**主题**做闸门，"这段是不是在讲这件事"这个粗判断反而稳；
    至于"这段属不属于本节"，那本来就是检索排序的活，不该让闸门兼职。

    语义模型不可用时**放行**（不拦），并在返回里带 `relevance=None`：
    宁可这一段多写几句，也不要因为模型没加载成功就让全篇变成占位。

    返回按 score 降序的 dict 列表（未编号，编号由调用方在段落顺序里统一分配）。
    """
    retrieval = retrieval or RetrievalService()
    # 检索词 = 主题 + 段落 query。
    # 只用段落 query 是实测出来的坑：段落 query 是规划器现编的中文短语
    # （"注意力机制在自然语言处理中的初步应用"），在双语语料里锚点太少，
    # top-24 里只有 2 段是正文，其余全是表格残渣；拼上主题后同样的 top-40 能到 9 段。
    search_query = f"{topic} {query}".strip() if topic else query
    hits = retrieval.search(search_query, top_k=max(8, max_chunks * RETRIEVE_MULTIPLIER))["fusion"]
    picked: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for h in hits:
        item = h.model_dump()
        if not usable(item):
            continue
        key = f"{item.get('arxiv_id')}|{item.get('page')}|{_norm_head(str(item.get('text') or ''))}"
        if key in seen:
            continue
        seen.add(key)
        picked.setdefault(str(item.get("arxiv_id") or ""), []).append(item)

    for items in picked.values():
        items.sort(key=lambda x: -float(x.get("score") or 0.0))

    candidates = sum(len(v) for v in picked.values())
    low_relevance = 0
    semantic_ok = True
    if min_sim is not None:
        flat = [it for items in picked.values() for it in items]
        probe = gate_query or query
        sims = semantic.raw_similarities([(probe, str(it.get("text") or "")[:600]) for it in flat])
        if sims and all(v is not None for v in sims):
            keep: dict[str, list[dict[str, Any]]] = {}
            for it, sim in zip(flat, sims):
                it["relevance"] = round(float(sim), 3)
                if float(sim) >= min_sim:
                    keep.setdefault(str(it.get("arxiv_id") or ""), []).append(it)
                else:
                    low_relevance += 1
            picked = keep
        else:
            semantic_ok = False

    # 轮转取：每篇先各取 1 条，再各取第 2 条……保证"多篇"这件事在结构上成立。
    balanced: list[dict[str, Any]] = []
    for depth in range(max(1, per_paper)):
        for pid in sorted(picked):
            items = picked[pid]
            if depth < len(items):
                balanced.append(items[depth])
    return EvidencePick(
        chunks=balanced[:max_chunks],
        candidates=candidates,
        low_relevance=low_relevance,
        semantic_ok=semantic_ok,
    )
