"""
References 生成与校验：只允许库内真实文献，模型连写参考文献的机会都没有。

任务约束是"禁止生成库外文献条目，即使格式看起来正确"。靠 prompt 里写一句
「不要编造参考文献」是不可能守住的——参考文献恰好是大模型最擅长编得像真的东西：
作者、年份、会议、页码、DOI 全都能编得整整齐齐。所以这里换了思路：

**模型根本不产出参考文献。** 正文里只有 `[引#N]`（chunk 级），
References 由服务端从 `papers` 表按被引 chunk 反查生成。编号顺序 = 首次被引顺序。

仍然保留三道校验，因为"服务端生成"也会出错（chunk 的元数据可能过期、论文可能被删）：

1. `library_check`：每条参考文献对应的 paper 必须在 `papers` 表里**重新查到**，
   不是信内存里的副本——chunk 的 payload 是入库时写进向量库的，删库后它还活着。
2. `title_check`：库内 title 与生成时用的 title 归一化后必须一致，不一致以**库内为准**并记录。
3. `text_audit`：正文里出现的 arXiv 号 / DOI 如果不属于任何一条参考文献，
   视为"模型自己写进去的库外文献"，**从正文删掉**并计入 `fabricated_refs`。

另外还有一种伪装：模型按论文习惯写 `[12]` 这种方括号数字引用。库内没有这套编号，
它指向的是一条不存在的文献——`sentences.BRACKET_REF_RE` 在切句时就清掉了，
这里负责记账。
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from app.db.connection import reader
from app.services.writing.evidence import EvidenceUnit

ARXIV_IN_TEXT_RE = re.compile(r"(?:arXiv\s*[:：]?\s*|arxiv\.org/abs/)(\d{4}\.\d{4,5})", re.I)
DOI_IN_TEXT_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")


def _norm_title(text: str) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _authors(raw: Any) -> list[str]:
    """papers.authors 是 JSON 字符串；历史数据里也有逗号分隔的裸文本。"""
    import json

    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [x.strip() for x in re.split(r"[;；,，]", text) if x.strip()]


def library_papers() -> list[dict[str, Any]]:
    """
    库内"有正文可引"的论文清单，给分段规划看。

    只列有 text chunk 的：规划器如果看到一篇只有元数据的论文，会规划出
    "就这篇论文谈谈方法"这种段落，然后那一段必然检索不到证据。
    """
    with reader() as c:
        rows = c.execute(
            """SELECT DISTINCT p.id, p.arxiv_id, p.title, p.authors, p.year, p.venue, p.doi
                 FROM papers p
                 JOIN paper_versions v ON v.paper_id = p.id AND v.is_current = 1
                 JOIN chunks ch ON ch.version_id = v.id
                WHERE ch.chunk_type = 'text'"""
        ).fetchall()
    return [
        {
            "paper_id": r["id"],
            "arxiv_id": r["arxiv_id"] or "",
            "title": r["title"] or "",
            "authors": _authors(r["authors"]),
            "year": r["year"],
            "venue": r["venue"] or "",
            "doi": r["doi"] or "",
        }
        for r in rows
    ]


def load_papers(paper_ids: list[str]) -> dict[str, dict[str, Any]]:
    """按 paper_id 取库内元数据（一次查完，供生成用；逐条复查在 `verify_references`）。"""
    ids = [p for p in dict.fromkeys(paper_ids) if p]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with reader() as c:
        rows = c.execute(
            f"""SELECT id, arxiv_id, title, authors, year, venue, doi
                  FROM papers WHERE id IN ({marks})""",
            ids,
        ).fetchall()
    return {
        r["id"]: {
            "paper_id": r["id"],
            "arxiv_id": r["arxiv_id"] or "",
            "title": r["title"] or "",
            "authors": _authors(r["authors"]),
            "year": r["year"],
            "venue": r["venue"] or "",
            "doi": r["doi"] or "",
        }
        for r in rows
    }


def has_chunks(paper_id: str) -> bool:
    """这篇论文在库里是否真的有可引正文（防止引到一条只有元数据、没有正文的记录）。"""
    with reader() as c:
        row = c.execute(
            """SELECT COUNT(*) AS n
                 FROM chunks ch
                 JOIN paper_versions v ON v.id = ch.version_id
                WHERE v.paper_id = ? AND ch.chunk_type = 'text'""",
            (paper_id,),
        ).fetchone()
    return bool(row and row["n"])


def index_by_arxiv(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    按 `arxiv_id` 建立索引——**这是本项目 chunk 与 papers 之间唯一可靠的连接键**。

    踩过的坑：向量库 payload 里的 `paper_id` 不是 `papers.id`，而是入库时拼的
    `{arxiv_id}::{version}` 复合键（例如 `2106.09685::v2`）。拿它去 `papers` 表查
    一条也查不到——第一版 `build_references` 就是这么写的，结果 4 段正文 8 句结论、
    2 篇真实文献，References 列表**空的**，`refs_rejected=2`。
    校验器当场把这件事抓了出来（它回库重查，查不到就拒），而不是生成一份
    paper_id 是 `2106.09685::v2` 的假条目。

    所以参考文献一律按 `arxiv_id` 反查，库里 `papers.arxiv_id` 才是持久身份。
    """
    out: dict[str, dict[str, Any]] = {}
    for p in papers:
        arxiv = str(p.get("arxiv_id") or "").strip()
        if arxiv:
            out[arxiv] = p
    return out


def build_references(
    units: list[EvidenceUnit],
    papers_by_arxiv: dict[str, dict[str, Any]],
    *,
    first_seen: list[int],
) -> list[dict[str, Any]]:
    """
    按**首次被引顺序**生成参考文献列表。

    为什么不用字典序/年份序：正文里的 `[引#N]` 是读者唯一能顺着查的线索，
    参考文献按首次出现排序，读者的查找路径最短；也符合学术写作惯例。

    `first_seen` 是引号的首次出现顺序（由 service 在拼正文时记录）。
    """
    order: list[str] = []
    for n in first_seen:
        unit = next((u for u in units if u.n == n), None)
        if unit and unit.arxiv_id and unit.arxiv_id not in order:
            order.append(unit.arxiv_id)

    refs: list[dict[str, Any]] = []
    for i, arxiv in enumerate(order, 1):
        meta = papers_by_arxiv.get(arxiv)
        chunk_nos = sorted(u.n for u in units if u.arxiv_id == arxiv)
        payload_ids = sorted({u.paper_id for u in units if u.arxiv_id == arxiv})
        if not meta:
            # 库内查不到这篇（向量库有、关系库没有）——登记但标未校验，交给 verify 拒掉
            refs.append(
                {
                    "ref_no": i,
                    "paper_id": "",
                    "arxiv_id": arxiv,
                    "title": "",
                    "authors": [],
                    "year": None,
                    "venue": "",
                    "doi": "",
                    "chunk_nos": chunk_nos,
                    "payload_paper_ids": payload_ids,
                    "checked": False,
                    "issues": ["not_in_papers_table"],
                }
            )
            continue
        refs.append(
            {
                "ref_no": i,
                "paper_id": meta["paper_id"],
                "arxiv_id": arxiv,
                "title": meta["title"],
                "authors": meta["authors"],
                "year": meta["year"],
                "venue": meta["venue"],
                "doi": meta["doi"],
                "chunk_nos": chunk_nos,
                "payload_paper_ids": payload_ids,
                "checked": True,
                "issues": [],
            }
        )
    return refs


def format_reference(ref: dict[str, Any]) -> str:
    """一行人读的条目。作者超过 3 个用 et al.，与常见学术格式一致。"""
    authors = ref.get("authors") or []
    if authors:
        who = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
        # `et al.` 自带句点，再补一个就成了 "et al.."；单数作者名后也没有句点。
        head = f"{who} " if who.endswith(".") else f"{who}. "
    else:
        head = ""
    title = (ref.get("title") or "").strip().rstrip(".")
    venue = (ref.get("venue") or "").strip()
    year = ref.get("year")
    tail_bits = [b for b in (venue, str(year) if year else "") if b]
    tail = ", ".join(tail_bits)
    ident = f"arXiv:{ref['arxiv_id']}" if ref.get("arxiv_id") else (f"doi:{ref['doi']}" if ref.get("doi") else "")
    parts = [f"[{ref['ref_no']}]", head + title + "."]
    if tail:
        parts.append(tail + ".")
    if ident:
        parts.append(ident + ".")
    return " ".join(p for p in parts if p).strip()


def verify_references(refs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    对每条参考文献做库内复核（**重新查库**，不信生成时的快照）。

    四类问题：
    - `not_in_library`：`papers` 表按 id 查不到 → 整条拒掉（"库外文献"唯一可能的落点）
    - `arxiv_mismatch`：查到的论文 arxiv_id 与条目不符 → 拒掉（id 与 arxiv 指向两篇论文）
    - `title_mismatch`：库里标题与生成用的不一致 → 用库内标题覆盖（不是删，文献是真的）
    - `no_chunks`：论文在库但没有正文 chunk → 保留条目但标 `unverifiable`
    """
    rejected: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for ref in refs:
        issues = [i for i in (ref.get("issues") or []) if i != "not_in_papers_table"]
        fresh = load_papers([ref.get("paper_id") or ""]).get(ref.get("paper_id") or "")
        if not fresh:
            rejected.append({**ref, "reason": "not_in_library"})
            continue
        if ref.get("arxiv_id") and fresh.get("arxiv_id") and ref["arxiv_id"] != fresh["arxiv_id"]:
            rejected.append({**ref, "reason": "arxiv_mismatch", "library_arxiv": fresh["arxiv_id"]})
            continue
        if ref.get("title") and _norm_title(ref["title"]) != _norm_title(fresh["title"]):
            ratio = difflib.SequenceMatcher(
                None, _norm_title(ref["title"]), _norm_title(fresh["title"])
            ).ratio()
            issues.append(f"title_mismatch:{ratio:.2f}")
            ref = {**ref, "title": fresh["title"]}  # 以库内为准
        for key in ("authors", "year", "venue", "doi", "arxiv_id"):
            if not ref.get(key) and fresh.get(key):
                ref[key] = fresh[key]
        if not has_chunks(fresh["paper_id"]):
            issues.append("no_chunks")
        ref["issues"] = issues
        ref["checked"] = not issues
        kept.append(ref)

    return {
        "references": kept,
        "rejected": rejected,
        "checked": len(kept),
        "rejected_count": len(rejected),
    }


def audit_text(text: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    正文反向审计：句子里出现的 arXiv 号 / DOI 必须在参考文献列表里。

    命中但不在列表 = 模型自己写进去的库外文献标识。返回清洗后的文本 + 证据清单。
    """
    allowed_arxiv = {str(r.get("arxiv_id") or "") for r in refs if r.get("arxiv_id")}
    allowed_doi = {str(r.get("doi") or "").lower() for r in refs if r.get("doi")}

    hits: list[dict[str, str]] = []

    def _sub_arxiv(m: re.Match[str]) -> str:
        ident = m.group(1)
        if ident in allowed_arxiv:
            return m.group(0)
        hits.append({"kind": "arxiv", "value": ident})
        return ""

    def _sub_doi(m: re.Match[str]) -> str:
        ident = m.group(0)
        if ident.lower() in allowed_doi:
            return m.group(0)
        hits.append({"kind": "doi", "value": ident})
        return ""

    cleaned = ARXIV_IN_TEXT_RE.sub(_sub_arxiv, text or "")
    cleaned = DOI_IN_TEXT_RE.sub(_sub_doi, cleaned)
    # 标识本体删掉后还会剩个标签：`（arXiv:，doi:）` → 把裸标签也去掉
    cleaned = re.sub(r"\b(?:doi|arxiv)\s*[:：]\s*(?=[）)，,。.；;、]|$)", "", cleaned, flags=re.I)
    # 删掉标识之后会留下空括号：`已有工作（，doi:）给出……`。
    # 这种残句比留着假文献更糟——读者会以为这里原本有内容。
    cleaned = re.sub(r"[（(]\s*[，,、;；:：]?\s*[）)]", "", cleaned)
    cleaned = re.sub(r"[（(]\s*[，,、;；:：]\s*", "（", cleaned)
    cleaned = re.sub(r"\s*[，,、;；:：]\s*[）)]", "）", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return {"text": cleaned, "fabricated": hits}
