"""
逐段写作：模型只在"给定证据 + 给定段落意图"里把话说顺，其余全部服务端算。

`write_section` 的产出是**句子结构体**，不是一段字符串：

    {"id": "s1-2", "type": "claim", "text": "……更稳定。", "cite_nos": [3], "chunk_ids": [...]}

这一层要处理四件模型不会自觉做的事：

1. **伪造的 bridge**：模型倾向于把难写的判断句标成 bridge 逃掉引用。改判逻辑在
   `sentences.sentence_type`，这里负责"改判后仍无引用 → 删"。
2. **编号越界**：模型可能引本段之外的编号（甚至引用别的段的号）。超出本段证据范围的
   编号一律作废；作废后没有有效引用 → 整句删。
3. **数字编造**：见 `sentences.numbers_grounded`。
4. **无证据段**：不写，交给占位。占位文案里只放"缺什么"，不放任何像正文的话——
   一旦写了像样的句子，读者就会当成结论。

降级策略与综述不同：综述不可用时会退到"模板渲染"，因为综述的骨架（簇）本来就在服务端。
写作没有骨架，硬凑只会凑出空话，所以降级为**原文摘录**：直接给证据原句 + 编号，
并标 `degraded=true`。虽然读起来是英文，但每一句都能查得到。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.services.llm_client import LLMClient
from app.services.writing import prompts
from app.services.writing.evidence import CiteIndex, EvidenceUnit
from app.services.writing.sentences import (
    BRACKET_REF_RE,
    clean_sentence,
    has_bridge_cue,
    numbers_grounded,
    parse_units,
    sentence_type,
)

# 模型偶尔会把"参考文献"当成一段写出来。这些标题一律不认。
FORBIDDEN_TITLES = ("参考文献", "references", "引用文献", "致谢", "acknowledg")


@dataclass
class Draft:
    """一次生成的完整中间表示：段落 → 句子 → 引号 → chunk。"""

    topic: str
    sections: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EvidenceUnit] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    placeholder_sections: int = 0
    degraded: bool = False
    gaps: list[dict[str, str]] = field(default_factory=list)

    @property
    def sentences(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sec in self.sections:
            out.extend(sec["sentences"])
        return out


# ---------------------------------------------------------------- 分段计划

DEFAULT_SECTIONS = (
    ("研究背景与问题", "这个方向为什么值得研究，现有工作把问题定义成了什么", "研究背景 问题定义"),
    ("方法脉络", "主流方法各自解决什么、代价是什么", "方法设计 核心机制 复杂度"),
    ("证据与效果", "各方法在实验里的表现差异与适用条件", "实验结果 指标对比 适用条件"),
    ("局限与展望", "现有方法的失效条件与未解决问题", "局限 失效条件 未来工作"),
)


def _catalog_text(papers: list[dict[str, Any]]) -> str:
    lines = []
    for p in papers:
        who = ", ".join((p.get("authors") or [])[:2])
        year = p.get("year") or ""
        lines.append(f"- {p.get('title') or '(无标题)'}（{who}{', ' + str(year) if year else ''}）")
    return "\n".join(lines) or "（库内暂无可解析文献）"


def plan_sections(
    topic: str,
    papers: list[dict[str, Any]],
    *,
    n_sections: int,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """
    规划段落结构。任何一步不靠谱都退到固定模板——结构可以平庸，但不能缺。

    这里额外做一次"标题过滤"：模型很爱把「参考文献」规划成一段，
    因为论文里确实有这一段。但参考文献必须由 `papers` 表生成，不能成为正文段落。
    """
    llm = llm or LLMClient()
    payload: dict[str, Any] = {}
    try:
        payload = llm.json_chat(
            prompts.section_plan_prompt(topic, _catalog_text(papers), n_sections),
            system_key="writing_plan",
            system=prompts.SECTION_PLAN_SYSTEM,
        )
    except Exception as exc:  # 规划失败不该毁掉整次生成
        payload = {"error": str(exc)[:200]}

    raw = payload.get("sections") if isinstance(payload, dict) else None
    sections: list[dict[str, Any]] = []
    rejected: list[str] = []
    for i, item in enumerate(raw or [], 1):
        if not isinstance(item, dict):
            continue
        title = clean_sentence(str(item.get("title") or "")).strip("0123456789.、 )（")
        if not title:
            continue
        if any(bad in title.lower() for bad in FORBIDDEN_TITLES):
            rejected.append(title)
            continue
        sections.append(
            {
                "id": f"S{len(sections) + 1}",
                "no": len(sections) + 1,
                "title": title,
                "intent": clean_sentence(str(item.get("intent") or "")),
                "query": clean_sentence(str(item.get("query") or "")) or f"{topic} {title}",
            }
        )
        if len(sections) >= max(1, n_sections):
            break

    source = "llm"
    if not sections:
        source = "template"
        slices = DEFAULT_SECTIONS[: max(1, n_sections)]
        for i, (title, intent, q) in enumerate(slices, 1):
            sections.append(
                {
                    "id": f"S{i}",
                    "no": i,
                    "title": title,
                    "intent": intent,
                    "query": f"{topic} {q}",
                }
            )

    return {"sections": sections, "source": source, "rejected_titles": rejected}


# ---------------------------------------------------------------- 逐段写作

def _evidence_text(units: list[EvidenceUnit]) -> str:
    blocks = []
    for u in units:
        blocks.append(
            f"[引#{u.n}] {u.title}（arXiv:{u.arxiv_id}，第 {u.page} 页，章节 {u.section or '-'}）\n"
            f"{(u.text or '').strip()}"
        )
    return "\n\n".join(blocks)


def _fallback_sentences(units: list[EvidenceUnit], limit: int = 2) -> list[dict[str, Any]]:
    """降级：直接摘原文首句。读起来是英文，但每句都能查得到——比编一句中文强。"""
    out: list[dict[str, Any]] = []
    for u in units[:limit]:
        text = re.sub(r"\s+", " ", (u.text or "").strip())
        cut = re.split(r"(?<=[.!?])\s+", text)[0][:240].strip()
        if not cut:
            continue
        out.append(
            {
                "type": "claim",
                "text": cut,
                "cite_nos": [u.n],
                "source": "fallback_quote",
            }
        )
    return out


def _normalize_declared(item: dict[str, Any]) -> tuple[str, list[int], str]:
    """
    兼容两种输出形态：
    - 推荐形态：`{"type":"claim","text":"……。[引#3]"}`
    - 退化形态：`{"type":"claim","text":"……。","cite":[3]}`

    引用编号放在数组字段里时模型经常写坏 JSON（把字段名写进正文），
    所以主路径是行内标记；这里只是给"它偏要分字段"的情况兜个底，不算鼓励。
    """
    text = str(item.get("text") or item.get("sentence") or "")
    cites: list[int] = []
    for key in ("cite", "cites", "refs"):
        val = item.get(key)
        if isinstance(val, list):
            for x in val:
                try:
                    cites.append(int(x))
                except (TypeError, ValueError):
                    continue
        elif isinstance(val, (int, str)) and str(val).isdigit():
            cites.append(int(val))
    if cites and not re.search(r"[\[［]引#", text):
        text = text.rstrip() + "".join(f"[引#{n}]" for n in cites)
    return text, cites, str(item.get("type") or "")


def write_section(
    topic: str,
    section: dict[str, Any],
    units: list[EvidenceUnit],
    index: CiteIndex,
    *,
    prev_context: str = "",
    prev_title: str = "",
    empty_gap: str = "",
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """
    写一段。返回 `{sentences, dropped, placeholder, gap, degraded}`。

    `units` 是**本段**的证据（已编号）。编号是全篇唯一的，所以"本段合法编号集合"
    就是这一小撮，越界即作废。
    """
    llm = llm or LLMClient()
    max_sentences = max(1, int(settings.writing_max_sentences))
    allowed = {u.n for u in units}
    dropped: list[dict[str, Any]] = []

    def placeholder(gap: str) -> dict[str, Any]:
        return {
            "sentences": [],
            "dropped": dropped,
            "placeholder": True,
            "gap": gap,
            "degraded": False,
        }

    if not units:
        return placeholder(empty_gap or "本段没有检索到可用的正文证据（库内没有覆盖这个问题的段落）。")

    payload: dict[str, Any] = {}
    degraded = False
    try:
        payload = llm.json_chat(
            prompts.section_write_prompt(
                topic,
                section["title"],
                section.get("intent") or "",
                _evidence_text(units),
                prev_context,
                require_bridge=bool(prev_context),
            ),
            system_key="writing_section",
            system=prompts.SECTION_WRITE_SYSTEM,
        )
    except Exception as exc:
        payload = {"error": str(exc)[:200]}

    raw_sentences = payload.get("sentences") if isinstance(payload, dict) else None
    if not raw_sentences:
        # 两种情况必须分开处理：
        # ① 模型看着证据说"写不出来"（insufficient + gap）——尊重它，走占位。
        #    此时如果硬拿原文摘录顶上，等于用"摘录"冒充当模型的判断，是过度声称。
        # ② JSON 解析失败/调用异常——没有判断可尊重，退到原文摘录，至少每句查得到。
        gap = str(payload.get("gap") or "").strip() if isinstance(payload, dict) else ""
        failed = not isinstance(payload, dict) or "error" in payload
        if gap and not failed:
            return placeholder(gap)
        degraded = True
        sentences = []
        for i, item in enumerate(_fallback_sentences(units), 1):
            built = _build_sentence(section, i, item["text"], item["cite_nos"], "claim", index, dropped)
            if built:
                built["source"] = "fallback_quote"
                sentences.append(built)
        if not sentences:
            return placeholder(gap or "模型未返回可用句子，且证据无法直接摘录。")
        return {
            "sentences": sentences[:max_sentences],
            "dropped": dropped,
            "placeholder": False,
            "gap": "",
            "degraded": True,
        }

    kept: list[dict[str, Any]] = []
    for item in raw_sentences:
        if len(kept) >= max_sentences:
            break
        if not isinstance(item, dict):
            continue
        raw_text, array_cites, declared = _normalize_declared(item)
        if not raw_text.strip():
            continue
        # 模型自带的方括号数字引用（`[12]`）——库内没有这套编号，记账后清掉
        bracket = BRACKET_REF_RE.findall(raw_text)
        if bracket:
            dropped.append({"reason": "bracket_ref_stripped", "marks": bracket, "text": raw_text[:80]})

        for unit in parse_units(raw_text):
            body = unit["text"]
            if re.match(r"^\s*(参考文献|references)\s*[:：]?", body, re.I):
                dropped.append({"reason": "reference_section", "text": body[:80]})
                continue
            if len(body) < 6:
                continue

            stype, why = sentence_type(unit["raw"], declared)
            cites = [n for n in unit["cites"] if n in allowed]
            if len(cites) != len(unit["cites"]):
                dropped.append(
                    {
                        "reason": "cite_out_of_section",
                        "text": body[:80],
                        "invalid": [n for n in unit["cites"] if n not in allowed],
                    }
                )
            # 标了 bridge 却带引用 → 它其实在指涉证据，按 claim 处理（引用本身没错）
            if stype == "bridge" and cites:
                stype, why = "claim", why or "bridge_with_citation"

            # bridge 必须自成一句。模型常把承接词单独输出成一项——
            # `{"type":"bridge","text":"在此基础上，"}`——后面紧跟 claim 项，
            # 渲染出来就是正文里孤零零一行"在此基础上，"。这种残片不是句子，判废；
            # 段首过渡会由服务端补（见函数末尾）。
            if stype == "bridge" and (len(body) < 8 or not re.search(r"[。！？!?]$", body)):
                dropped.append({"reason": "bridge_fragment", "text": body[:60]})
                continue

            if stype == "claim":
                if not cites:
                    dropped.append(
                        {"reason": "no_citation", "text": body[:120], "declared": declared, "why": why}
                    )
                    continue
                if settings.writing_strict_numbers:
                    ok, missing = numbers_grounded(body, index.numbered_text(cites))
                    if not ok:
                        dropped.append(
                            {"reason": "number_not_grounded", "text": body[:120], "missing": missing}
                        )
                        continue

            sentence = _build_sentence(section, len(kept) + 1, body, cites, stype, index, dropped)
            if sentence:
                sentence["bridge_cue"] = has_bridge_cue(body) if stype == "bridge" else False
                kept.append(sentence)

    if not kept:
        reasons = sorted({d["reason"] for d in dropped}) or ["empty_output"]
        return placeholder(
            "本段证据未通过校验，未生成正文（" + "、".join(reasons) + "）。"
        )

    # 段间过渡句：模型漏写时由服务端补一句**结构性**承接。
    #
    # 为什么敢由服务端写：这句话只陈述"上一段讲什么、这一段讲什么"，不涉及任何事实、
    # 数字、判断，所以它天然不需要引用，也不可能编造内容。反过来，如果让模型自由发挥
    # 过渡句，它很容易写成"综上可见，该方法具有广阔前景"这种带判断的废话——
    # 那正是"bridge 免引用"这个口子最危险的用法。
    if not any(x["type"] == "bridge" for x in kept) and section.get("no", 1) > 1:
        bridge_text = (
            f"在上文关于「{prev_title or '上一节'}」的讨论基础上，"
            f"本节进一步讨论「{section['title']}」。"
        )
        kept.insert(
            0,
            {
                "id": "",
                "section_id": section["id"],
                "order": 0,
                "type": "bridge",
                "text": bridge_text,
                "cite_nos": [],
                "chunk_ids": [],
                "bridge_cue": True,
                "source": "server_bridge",
            },
        )
    for i, item in enumerate(kept, 1):
        item["order"] = i
        item["id"] = f"{section['id'].lower()}-{i}"

    return {
        "sentences": kept,
        "dropped": dropped,
        "placeholder": False,
        "gap": "",
        "degraded": degraded,
    }


def _build_sentence(
    section: dict[str, Any],
    order: int,
    text: str,
    cites: list[int],
    stype: str,
    index: CiteIndex,
    dropped: list[dict[str, Any]],
) -> dict[str, Any] | None:
    body = clean_sentence(text)
    if not body:
        return None
    chunk_ids = []
    for n in cites:
        unit = index.get(n)
        if unit:
            chunk_ids.append(unit.chunk_id)
    return {
        "id": f"{section['id'].lower()}-{order}",
        "section_id": section["id"],
        "order": order,
        "type": stype,
        "text": body,
        "cite_nos": list(dict.fromkeys(cites)),
        "chunk_ids": list(dict.fromkeys(chunk_ids)),
    }
