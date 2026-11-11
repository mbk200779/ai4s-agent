"""
写作辅助流水线：规划段落 → 逐段检索证据 → 句级生成 → References 校验 → 导出。

产出的是"可追溯的稿子"，不是"一段文字"：

    每句 → {type, cite_nos, chunk_ids}
    每篇文献 → {ref_no, paper_id, chunk_nos}
    两者由 chunk 连起来 → 导出时映射关系完整保留

任务三条约定的落实位置：
- **禁止库外文献**：模型不产出 References（`references.build_references` 从 papers 表生成）；
  正文里的 arXiv/DOI 反向审计，不属于任何参考文献的当场删掉（`references.audit_text`）。
- **无证据段占位**：`compose.write_section` 在"检索不到证据 / 证据全没过校验 / 模型说写不出"
  三种情况下都不产出正文，只产出 `placeholder + gap`；渲染成醒目的占位块。
- **导出保留映射**：`render_markdown` 末尾附「引#N → [K]」映射表，
  同时把完整结构写进 `.json` 旁车（MinIO），两者用同一份中间表示生成。

可复现：段落顺序、引号分配、参考文献顺序都是确定性的；两次调用模型才有差异，
所以响应体回显 `prompt_version / version / fingerprint`。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.services.llm_client import LLMClient
from app.services.storage import StorageService
from app.services.writing import prompts
from app.services.writing.compose import Draft, plan_sections, write_section
from app.services.writing.evidence import CiteIndex, collect_section_evidence
from app.services.writing.references import (
    audit_text,
    build_references,
    format_reference,
    index_by_arxiv,
    library_papers,
    verify_references,
)


class WritingPipeline:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()
        self.storage = StorageService()

    # ------------------------------------------------------------------ 主流程

    def generate(
        self,
        topic: str,
        *,
        n_sections: int | None = None,
        arxiv_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        cfg = prompts.config_block()
        timing: dict[str, float] = {}
        t_all = time.time()

        n_sections = int(n_sections or settings.writing_sections)
        catalog = library_papers()
        if arxiv_ids:
            wanted = set(arxiv_ids)
            catalog = [p for p in catalog if p["arxiv_id"] in wanted]
        if not catalog:
            return self._rejected(
                topic,
                "文献库里没有可引用的正文（只有元数据不算），无法生成带溯源的初稿。",
                cfg,
            )

        t = time.time()
        plan = plan_sections(topic, catalog, n_sections=n_sections, llm=self.llm)
        timing["plan"] = round(time.time() - t, 2)

        index = CiteIndex()
        draft = Draft(topic=topic)
        section_rows: list[dict[str, Any]] = []
        prev_context = ""
        prev_title = ""
        t = time.time()
        for section in plan["sections"]:
            pick = collect_section_evidence(
                section["query"],
                max_chunks=int(settings.writing_evidence_per_section),
                per_paper=int(settings.writing_per_paper),
                min_sim=float(settings.writing_evidence_min_sim),
                gate_query=topic,
                topic=topic,
            )
            units = [index.assign(item, section_id=section["id"]) for item in pick.chunks]
            written = write_section(
                topic,
                section,
                units,
                index,
                prev_context=prev_context,
                prev_title=prev_title,
                empty_gap=pick.gap_hint(),
                llm=self.llm,
            )
            if written["degraded"]:
                draft.degraded = True
            if written["placeholder"]:
                draft.placeholder_sections += 1
                draft.gaps.append({"section_id": section["id"], "title": section["title"], "gap": written["gap"]})

            section_rows.append(
                {
                    **section,
                    "sentences": written["sentences"],
                    "dropped": written["dropped"],
                    "placeholder": written["placeholder"],
                    "gap": written["gap"],
                    "evidence": [u.n for u in units],
                    "evidence_candidates": pick.candidates,
                    "evidence_low_relevance": pick.low_relevance,
                }
            )
            draft.sections = section_rows
            draft.dropped.extend(
                [{**d, "section_id": section["id"]} for d in written["dropped"]]
            )
            if written["sentences"]:
                prev_context = written["sentences"][-1]["text"]
            prev_title = section["title"]
        timing["write"] = round(time.time() - t, 2)

        draft.evidence = index.items
        if not draft.sentences:
            return self._rejected(
                topic,
                "本主题下没有任何句子通过引用/数值校验，全部段落为空——按约定不生成正文，只报缺口。",
                cfg,
                extra={
                    "sections": section_rows,
                    "stats": {
                        "sections": len(section_rows),
                        "placeholders": len([s for s in section_rows if s.get("placeholder")]),
                        "evidence_chunks": len(index),
                        "sentences_dropped": len(draft.dropped),
                        "dropped_by_reason": _count_reasons(draft.dropped),
                    },
                },
            )

        # ---- References：只从库内记录生成，再逐条回库复核
        t = time.time()
        # 用 arxiv_id 建索引，不用 chunk payload 里的 paper_id：
        # 后者是入库时的 `{arxiv_id}::{version}` 复合键，papers 表里查不到（见 references.index_by_arxiv）。
        papers_by_arxiv = index_by_arxiv(catalog)
        first_seen = [n for s in section_rows for sent in s["sentences"] for n in sent["cite_nos"]]
        refs = build_references(index.items, papers_by_arxiv, first_seen=first_seen)
        verified = verify_references(refs)
        refs = verified["references"]
        timing["refs"] = round(time.time() - t, 2)

        # ---- 正文反向审计：句子里出现库外 arXiv/DOI 就删掉
        fabricated: list[dict[str, Any]] = []
        for section in section_rows:
            for sent in section["sentences"]:
                checked = audit_text(sent["text"], refs)
                if checked["fabricated"]:
                    sent["text"] = checked["text"]
                    fabricated.extend([{**h, "sentence_id": sent["id"]} for h in checked["fabricated"]])

        # ---- 句 → 引# → 文献 的三级映射
        ref_by_arxiv = {r["arxiv_id"]: r["ref_no"] for r in refs}
        paper_by_arxiv = {r["arxiv_id"]: r["paper_id"] for r in refs}
        for section in section_rows:
            for sent in section["sentences"]:
                sent["ref_nos"] = sorted(
                    {
                        ref_by_arxiv[u.arxiv_id]
                        for n in sent["cite_nos"]
                        if (u := index.get(n)) and u.arxiv_id in ref_by_arxiv
                    }
                )
                sent["evidence"] = [
                    {
                        # `paper_id` 对外给**关系库里的真实 id**；chunk payload 里那个
                        # `{arxiv_id}::{version}` 复合键留在 `chunk_paper_ref` 里备查。
                        # 直接把复合键当 paper_id 返回，前端拿它去查论文必然查不到。
                        **{**u.to_dict(), "paper_id": paper_by_arxiv.get(u.arxiv_id, ""), "chunk_paper_ref": u.paper_id},
                        "ref_no": ref_by_arxiv.get(u.arxiv_id),
                    }
                    for u in (index.get(n) for n in sent["cite_nos"])
                    if u is not None
                ]

        sentences = draft.sentences
        claim_sentences = [s for s in sentences if s["type"] == "claim"]
        bridge_sentences = [s for s in sentences if s["type"] == "bridge"]

        # 引# → 引用它的句子 id 列表，即"哪一句用了这段原文"。
        cited_by: dict[int, list[str]] = {}
        for section in section_rows:
            for sent in section["sentences"]:
                for n in sent["cite_nos"]:
                    cited_by.setdefault(n, []).append(sent["id"])
        ref_map = [
            {
                "n": u.n,
                "chunk_id": u.chunk_id,
                # 与 `evidence[]` 同一口径：对外 `paper_id` 一律是关系库真实 id，
                # chunk payload 的复合键放 `chunk_paper_ref`。同一份响应里两个字段
                # 混着两种 id，前端点击「来源」跳论文页会有一半跳不过去。
                "paper_id": paper_by_arxiv.get(u.arxiv_id, ""),
                "chunk_paper_ref": u.paper_id,
                "arxiv_id": u.arxiv_id,
                "title": u.title,
                "page": u.page,
                "section": u.section,
                "ref_no": ref_by_arxiv.get(u.arxiv_id),
                # `used_in` 只算**真的引用了它**的句子，不是"检索时命中过它的段落"。
                # 两者差别很大：一段证据可能被四个段落检索命中（hit_in），
                # 但正文一次都没引（used_in 为空，ref_no 也是 None）。
                # 混成一个字段，看 json 的人会以为它在四段里都用上了。
                "used_in": cited_by.get(u.n, []),
                "hit_in": list(u.sections),
            }
            for u in index.items
        ]

        dropped_reasons = _count_reasons(draft.dropped)

        stats = {
            "sections": len(section_rows),
            "sentences": len(sentences),
            "claim_sentences": len(claim_sentences),
            "bridge_sentences": len(bridge_sentences),
            "cited_sentences": len([s for s in sentences if s["cite_nos"]]),
            "uncited_claim": len([s for s in claim_sentences if not s["cite_nos"]]),
            "placeholders": draft.placeholder_sections,
            "sentences_dropped": len(draft.dropped),
            "dropped_by_reason": dropped_reasons,
            "refs": len(refs),
            "refs_rejected": verified["rejected_count"],
            "fabricated_refs": len(fabricated),
            "evidence_chunks": len(index),
            "papers": len(refs),
            "degraded": draft.degraded,
            "prompt_version": prompts.WRITING_PROMPT_VERSION,
            "config_version": cfg["version"],
            "fingerprint": cfg["fingerprint"],
        }

        markdown = render_markdown(topic, section_rows, refs, ref_map, stats, cfg)
        export = self._export(topic, markdown, {
            "topic": topic,
            "config": cfg,
            "sections": section_rows,
            "references": refs,
            "ref_map": ref_map,
            "stats": stats,
        })
        timing["total"] = round(time.time() - t_all, 2)

        return {
            "topic": topic,
            "markdown": markdown,
            "sections": section_rows,
            "references": refs,
            "ref_map": ref_map,
            "stats": stats,
            "config": cfg,
            "export_key": export.get("markdown_key"),
            "export_json_key": export.get("json_key"),
            "rejected": False,
            "degraded": draft.degraded,
            "trace": {
                "plan": plan,
                "gaps": draft.gaps,
                "dropped": draft.dropped[:60],
                "fabricated_refs": fabricated,
                "refs_rejected": verified["rejected"],
                "timing": timing,
            },
        }

    # ------------------------------------------------------------------ 导出

    def _export(self, topic: str, markdown: str, payload: dict[str, Any]) -> dict[str, str | None]:
        """
        两份产物：人读的 `.md`（含映射表）+ 机读的 `.json`（完整结构）。

        为什么必须双份：md 里的映射表是"人能核对"的形式，但表格会截断（chunk_id 很长）；
        真正做二次处理（比如把稿子导进 Word 后重新编号）需要 json 里的完整字段。
        任一份失败都不影响另一份，也不影响响应体。
        """
        import json

        stem = f"drafts/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}"
        out: dict[str, str | None] = {"markdown_key": None, "json_key": None}
        try:
            self.storage.put_object(
                self.storage.provenance_bucket,
                f"{stem}.md",
                markdown.encode("utf-8"),
                content_type="text/markdown",
            )
            out["markdown_key"] = f"{stem}.md"
        except Exception:
            pass
        try:
            self.storage.put_object(
                self.storage.provenance_bucket,
                f"{stem}.json",
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                content_type="application/json",
            )
            out["json_key"] = f"{stem}.json"
        except Exception:
            pass
        return out

    def get_export_url(self, key: str, expires: int = 3600) -> str:
        return self.storage.get_presigned_url(self.storage.provenance_bucket, key, expires=expires)

    # ------------------------------------------------------------------ 拒答

    def _rejected(
        self,
        topic: str,
        reason: str,
        cfg: dict[str, Any],
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """拒答也是结果：给空稿 + 明确原因，而不是给一段看起来正常的空话。"""
        extra = extra or {}
        overrides = extra.get("stats") or {}
        return {
            "topic": topic,
            "markdown": "",
            "sections": extra.get("sections", []),
            "references": [],
            "ref_map": [],
            "stats": {
                "sections": len(extra.get("sections", [])),
                "sentences": 0,
                "claim_sentences": 0,
                "bridge_sentences": 0,
                "cited_sentences": 0,
                "uncited_claim": 0,
                "placeholders": 0,
                "sentences_dropped": 0,
                "dropped_by_reason": {},
                "refs": 0,
                "refs_rejected": 0,
                "fabricated_refs": 0,
                "evidence_chunks": 0,
                "papers": 0,
                "degraded": False,
                "prompt_version": prompts.WRITING_PROMPT_VERSION,
                "config_version": cfg["version"],
                "fingerprint": cfg["fingerprint"],
                # 调用方带来的真实计数放最后：拒答也要如实说"筛掉了多少"，
                # 否则统计表上会显示"一条都没筛"，与占位原因自相矛盾。
                **overrides,
            },
            "config": cfg,
            "export_key": None,
            "export_json_key": None,
            "rejected": True,
            "reject_reason": reason,
            "degraded": False,
            "trace": {},
        }


def _count_reasons(dropped: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in dropped:
        key = str(d.get("reason") or "unknown")
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------- 渲染

PLACEHOLDER_TITLE = "本段暂无库内证据支撑"


def render_markdown(
    topic: str,
    sections: list[dict[str, Any]],
    refs: list[dict[str, Any]],
    ref_map: list[dict[str, Any]],
    stats: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    """
    Markdown 由服务端渲染：标题、序号、占位块、参考文献、映射表都不经过模型。

    正文里的 `[引#N]` 原样保留——它是 chunk 级引用，指向一句具体原文；
    References 是 paper 级 `[K]`。两套编号的对应关系由末尾映射表给出，
    这也是"导出必须保留映射"的落点：把 md 发给别人，映射关系跟着一起走。
    """
    lines: list[str] = [f"# {topic}", ""]
    lines.append(
        f"> 生成口径：`{stats.get('prompt_version')}` ｜ 配置 `{stats.get('config_version')}`"
        f"/{stats.get('fingerprint')} ｜ 正文 {stats.get('sentences')} 句"
        f"（结论句 {stats.get('claim_sentences')}、连接句 {stats.get('bridge_sentences')}）"
        f" ｜ 参考文献 {stats.get('refs')} 条（全部来自库内）"
        f" ｜ 无证据占位 {stats.get('placeholders')} 段"
    )
    lines.append("")

    for section in sections:
        lines.append(f"## {section['no']}. {section['title']}")
        lines.append("")
        if section.get("placeholder"):
            lines.append(f"> ⚠ **{PLACEHOLDER_TITLE}**")
            lines.append(f"> {section.get('gap') or '（未说明缺口）'}")
            lines.append("")
            continue
        for sent in section["sentences"]:
            marks = "".join(f"[引#{n}]" for n in sent["cite_nos"])
            lines.append(f"{sent['text']}{marks}")
        lines.append("")

    lines.append("## 参考文献")
    lines.append("")
    if refs:
        for ref in refs:
            lines.append(format_reference(ref))
    else:
        lines.append("（本次正文没有引用任何文献）")
    lines.append("")

    # 映射表只列**正文真的引用了的**证据。
    #
    # 把"检索到但没引用"的也列进来会让表变成证据池清单：读者看到一堆 `[-]`（无对应文献），
    # 第一反应是"引用错乱"，而不是"这些是候选证据"。没被引用的证据留在 json 的 `ref_map` 里，
    # 需要复核时查得到，但不该出现在给读者的稿子里。
    cited = {n for section in sections for sent in section["sentences"] for n in sent["cite_nos"]}
    used = [row for row in ref_map if row["n"] in cited]
    unused = len(ref_map) - len(used)

    lines.append("## 引文映射（导出保留）")
    lines.append("")
    lines.append("| 引# | 文献 | 题名 | 页 | chunk_id |")
    lines.append("|---|---|---|---|---|")
    for row in used:
        title = (row.get("title") or "").replace("|", "/")
        lines.append(
            f"| 引#{row['n']} | [{row.get('ref_no') or '-'}] | {title} | p.{row.get('page')} | `{row.get('chunk_id')}` |"
        )
    lines.append("")
    if unused:
        lines.append(f"> 另有 {unused} 段证据被检索到但未被正文引用（见导出 json 的 `ref_map`）。")
        lines.append("")
    return "\n".join(lines)
