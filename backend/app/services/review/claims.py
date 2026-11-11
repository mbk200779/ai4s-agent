"""
claim 抽取：把检索片段变成「可判真假的结论」。

这里最容易做错的一件事是"归一化"：为了好看，把
「LoRA 匹配全量微调」和「LoRA 不如全量微调」都改写成「LoRA 与全量微调的性能关系」，
然后它们就会被聚成一簇，综述里写成「研究表明两者关系复杂」——这正是任务书
明令禁止的「把相反结论合并成一句『研究表明』」。所以抽取阶段的第一原则是：

    **保留极性，宁可多抽一条方向相反的 claim，也不许把两条焊接成一条中性的。**

为此每条 claim 必须带 `axis` + `polarity` 两个字段：
- `axis` 是"哪条可判定维度"（如 `LoRA 相对 全量微调：下游任务性能`），
- `polarity` 是"沿这条维度的方向"（support / oppose / conditional）。

两者合起来才是一条结论的完整身份；聚类时 polarity 是**簇键的一部分**，
所以"相反结论同簇"在结构上就不可能发生，不需要再靠 prompt 去劝模型。

`quote` 一律回到原文核对（`_locate_quote`）：模型改写过的引文不算证据。
核对不过的 claim 直接丢弃并记录原因——它要么是幻觉，要么是模型把两句话拼成了一句。
"""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.config import settings
from app.services.llm_client import LLMClient
from app.services.review import prompts
from app.services.review.pool import numbered_context

POLARITIES = {"support", "oppose", "conditional"}
POLARITY_LABELS = {"support": "支持", "oppose": "反对", "conditional": "有条件成立"}


def _norm(text: str) -> str:
    """只做"比较用"的归一化，不动原文字符（引文要能回贴到 PDF）。"""
    t = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\ufb01", "fi").replace("\ufb02", "fl")
    t = t.replace("\u2212", "-")
    return re.sub(r"\s+", " ", t).strip().lower()


_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(_norm(text))


def _locate_quote(quote: str, chunk_text: str) -> tuple[str | None, float]:
    """
    在片段里找回引文。返回 (原文中的真实子串 or None, 命中率)。

    为什么要"找回"而不是"包含判断"：PDF 抽出来的正文里连字符断行、
    弯引号、ﬁ 连字都很常见，模型的引文再经一次拷贝，逐字相等几乎必然失败。
    先做归一化子串匹配（快、准），失败再按 token 覆盖率在句级窗口里找最佳匹配（慢、宽），
    两步都不过才算"引文对不上原文"。
    """
    if not quote or not chunk_text:
        return None, 0.0
    nq, nc = _norm(quote), _norm(chunk_text)
    if nq and nq in nc:
        # 归一化后能对上，但要把原文（未归一化）里的对应片段切出来，
        # 前端要用它做高亮，必须落在原始字符区间上。
        head = nq[: min(60, len(nq))]
        pos = nc.find(head)
        if pos >= 0:
            approx = nc[pos : pos + len(nq)]
            raw_idx = chunk_text.lower().find(approx[:30])
            if raw_idx >= 0:
                return chunk_text[raw_idx : raw_idx + len(approx)], 1.0
        return quote, 1.0

    q_tokens = _tokens(quote)
    if len(q_tokens) < 4:
        return None, 0.0
    q_set = set(q_tokens)
    sentences = re.split(r"(?<=[.!?。！？])\s+", chunk_text)
    best, best_score = None, 0.0
    for sent in sentences:
        s_set = set(_tokens(sent))
        if not s_set:
            continue
        overlap = len(q_set & s_set) / len(q_set)
        if overlap > best_score:
            best, best_score = sent.strip(), overlap
    if best and best_score >= 0.7:
        return best, round(best_score, 3)
    return None, round(best_score, 3)


@dataclass
class Claim:
    id: str
    statement: str
    subject: str
    axis: str
    polarity: str
    condition: str
    quote: str
    chunk_index: int
    chunk_id: str
    paper_id: str
    arxiv_id: str
    title: str
    page: int
    section: str
    kind: str
    quote_score: float = 1.0
    axis_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def stance_label(self) -> str:
        return POLARITY_LABELS.get(self.polarity, self.polarity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "subject": self.subject,
            "axis": self.axis,
            "axis_key": self.axis_key,
            "polarity": self.polarity,
            "stance_label": self.stance_label,
            "condition": self.condition,
            "quote": self.quote,
            "quote_score": self.quote_score,
            "chunk_id": self.chunk_id,
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "page": self.page,
            "section": self.section,
            "kind": self.kind,
        }


@dataclass
class ExtractResult:
    claims: list[Claim]
    dropped: list[dict[str, Any]]


def _stable_id(*parts: str) -> str:
    return "c" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:6]


def _clean_statement(text: str) -> str:
    """模型爱在 statement 里带 [引#3]，必须摘掉——引用由 refs 字段承载，混在文字里会双重计数。"""
    return re.sub(r"[\[［]引?#?\d+[\]］]", "", text or "").strip(" 。；;")


def _search_pool(
    quote: str, chunks: Sequence[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None, float]:
    """在整个证据池里找这句引文，返回 (片段, 原文子串, 命中率)。

    为什么需要它：模型给的 `chunk_index` 经常指错——同一批塞 6 段时，
    它会把 3 号片段的句子挂到 4 号上。实测一次运行 10 条 claim 里 7 条
    因为"引文对不上所声明的那段"被丢，而那 7 句在池里的另一段上覆盖率 0.9~1.0。
    只按声明的片段核对，等于把模型的编号笔误当成幻觉证据删掉。
    池内回搜的前提没变：引文必须在这批原文里真实存在，所以不会放过编造的句子。
    """
    best: tuple[dict[str, Any] | None, str | None, float] = (None, None, 0.0)
    for ch in chunks:
        found, score = _locate_quote(quote, str(ch.get("text") or ""))
        if found is not None and score > best[2]:
            best = (ch, found, score)
            if score >= 1.0:
                break
    return best


def extract_claims(
    topic: str,
    chunks: list[dict[str, Any]],
    *,
    llm: LLMClient | None = None,
) -> ExtractResult:
    """
    分批抽 claim，逐条回原文核对引文。

    分批而不是一次性喂全部片段：一次 24 段正文会把上下文挤到一万多 token，
    模型的"漏抽后半段"几乎必然发生；分批还能把失败范围限制在一批之内。

    批次**并行**：各批之间没有依赖，串行跑 3 批实测 54s，并行后 20s 上下。
    结果按批序号回填，所以并行不影响输出顺序——这点很重要，
    否则同一份输入两次运行会得到不同的 claim 顺序，簇编号（1/2/3…）跟着漂。
    """
    llm = llm or LLMClient()
    batch_size = max(1, int(settings.review_claim_batch))
    batches = [chunks[i : i + batch_size] for i in range(0, len(chunks), batch_size)]
    # 片段在整池里的序号：引文被回搜到别的片段时，chunk_index 要跟着改，
    # 否则响应体里的编号和 markdown 里的 [引#N] 会对不上。
    pool_pos = {id(ch): i for i, ch in enumerate(chunks, 1)}

    def run_batch(batch: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = prompts.claim_extract_prompt(topic, numbered_context(batch), len(batch))
        return llm.json_chat(
            prompt,
            system_key="review_claim_extract",
            system=prompts.CLAIM_EXTRACT_SYSTEM,
        )

    results: list[dict[str, Any]] = [{"error": "not_run"} for _ in batches]
    if batches:
        workers = min(len(batches), 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_batch, b): i for i, b in enumerate(batches)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:  # 单批失败不拖垮整次抽取
                    results[i] = {"error": "batch_exception", "detail": str(exc)[:200]}

    dropped: list[dict[str, Any]] = []
    claims: list[Claim] = []
    seen_stmt: set[str] = set()

    for b_idx, (batch, payload) in enumerate(zip(batches, results)):
        start = b_idx * batch_size
        if "error" in payload:
            dropped.append(
                {"reason": "extract_json_failed", "batch": start, "raw": str(payload)[:200]}
            )
            continue

        for raw in payload.get("claims") or []:
            if not isinstance(raw, dict):
                continue
            statement = _clean_statement(str(raw.get("statement") or ""))
            if not statement:
                dropped.append({"reason": "empty_statement"})
                continue
            dedup = _norm(statement)
            if dedup in seen_stmt:
                dropped.append({"reason": "duplicate_statement", "statement": statement})
                continue

            try:
                idx = int(raw.get("chunk_index"))
            except (TypeError, ValueError):
                dropped.append({"reason": "bad_chunk_index", "statement": statement})
                continue
            if not 1 <= idx <= len(batch):
                dropped.append({"reason": "chunk_index_out_of_range", "statement": statement, "index": idx})
                continue

            chunk = batch[idx - 1]
            raw_quote = str(raw.get("quote") or "")
            quote, score = _locate_quote(raw_quote, str(chunk.get("text") or ""))
            relocated = False
            if quote is None:
                # 先在池内回搜：模型指错了片段号，但句子确实在库里。
                alt, alt_quote, alt_score = _search_pool(raw_quote, chunks)
                if alt is not None:
                    chunk, quote, score = alt, alt_quote, alt_score
                    relocated = True
            if quote is None:
                # 池里也找不到 = 引文被改写/拼接，这种 claim 不能进综述。
                dropped.append(
                    {
                        "reason": "quote_not_found",
                        "statement": statement,
                        "quote": raw_quote[:120],
                        "overlap": score,
                        "chunk": chunk.get("chunk_id"),
                    }
                )
                continue

            polarity = str(raw.get("polarity") or "support").strip().lower()
            if polarity not in POLARITIES:
                polarity = "conditional" if str(raw.get("condition") or "").strip() else "support"
            condition = str(raw.get("condition") or "").strip()
            # 有前提却标 support 的，一律降为 conditional：
            # "在 X 条件下 A 优于 B" 和 "A 优于 B" 是两条不同的结论，混在一起会让冲突判定失准。
            if condition and polarity == "support":
                polarity = "conditional"

            axis = str(raw.get("axis") or "").strip() or f"{raw.get('subject') or ''}：{statement}"
            claim = Claim(
                id=_stable_id(chunk.get("chunk_id", ""), statement),
                statement=statement,
                subject=str(raw.get("subject") or "").strip(),
                axis=axis,
                polarity=polarity,
                condition=condition,
                quote=quote,
                chunk_index=pool_pos.get(id(chunk), start + idx),
                chunk_id=str(chunk.get("chunk_id") or ""),
                paper_id=str(chunk.get("paper_id") or ""),
                arxiv_id=str(chunk.get("arxiv_id") or ""),
                title=str(chunk.get("title") or ""),
                page=int(chunk.get("page") or 0),
                section=str(chunk.get("section") or ""),
                kind=str(chunk.get("kind") or ""),
                quote_score=score,
                extra={"relocated": True, "declared_chunk": batch[idx - 1].get("chunk_id")}
                if relocated
                else {},
            )
            seen_stmt.add(dedup)
            claims.append(claim)

    return ExtractResult(claims=claims, dropped=dropped)


def stance_counts(claims: Sequence[Claim]) -> dict[str, int]:
    out = {"support": 0, "oppose": 0, "conditional": 0}
    for c in claims:
        out[c.polarity] = out.get(c.polarity, 0) + 1
    return out
