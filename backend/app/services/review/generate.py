"""
综述正文生成与渲染。

关键分工（这是"综述而非摘要拼接"能落地的原因）：

    模型只负责**在给定的簇里把话说顺**；
    结构、标题、立场、证据强度、冲突块，全部由服务端渲染。

这样做的直接后果是三条硬约束不再依赖模型自觉：
1. **无引用不得写入正文** —— 句子必须带 `refs`，且编号必须落在该簇的证据集合里；
   过不了校验的句子直接**删除**（不是替换成"[无依据]"留在正文里，那样仍是在正文里说话）。
2. **相反结论不得合并** —— 每个簇一个 section，模型没有"两个簇写进一段"的输出位置；
   额外再加一道文本检测：出现「研究表明／普遍认为／存在争议」这类抹平分歧的措辞
   且综述里确实存在已确认冲突时，该句判违规并删除。这是那句被禁的话在文本上的可检测形态。
3. **单篇支持必须标注** —— 「（仅单篇支持）」由渲染器根据 `paper_count` 自动追加，
   模型写不写都不影响结果。模型会漏，渲染器不会。

降级：模型不可用/返回无法解析时，退回**模板渲染**（簇结论 + 原文引文），
并在响应体标 `degraded=true` —— 宁可读起来生硬，也不能没有综述，更不能装作正常。
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from app.services.llm_client import LLMClient
from app.services.review import prompts
from app.services.review.cluster import Cluster
from app.services.review.conflict import TYPE_LABELS

CITE_RE = re.compile(r"[\[［]引#(\d+)[\]］]")
# 模型会把"引用字段"本身写进正文：`……参数数量。refs: [8]`、`……的RNN或卷积 refs{6,15}`。
# 括号种类还随模型心情变（[] {} （）），所以别只认一种写法——
# 这是结构化输出与自然语言混写的残留，不清掉正文里就会同时出现 `refs{6,15}` 和 `[引#6][引#15]`。
REFS_ECHO_RE = re.compile(r"refs?\s*[:：]?\s*[\[{（(][^\]}）)\n]{0,80}[\]}）)]\s*", re.I)

# 把分歧抹平的措辞。命中且综述存在已确认冲突 → 该句违规。
MERGE_MARKERS = (
    "研究表明",
    "普遍认为",
    "存在争议",
    "尚有争议",
    "众说纷纭",
    "尚无定论",
    "莫衷一是",
    "说法不一",
)

# 句子单元 = 「到句末标点为止的正文」+「紧跟其后的引用标记」。
#
# 为什么要连引用一起切：模型按约定写的是「……推理延迟更低。[引#2]」——
# 引用在**句号之后**。如果只在句号处切，`[引#2]` 会被甩到下一句开头，
# 于是本句"没有引用"被判删除、下一句凭空多出一个引用，整篇的引用归属全错位。
# 实测这个错位会让 12 个簇各自丢掉第一句，直接把综述打成模板腔。
_UNIT_RE = re.compile(r"[^。！？!?]*(?:[。！？!?]+|$)(?:[\[［]引#\d+[\]］])*")


def _split_sentences(text: str) -> list[str]:
    units = [m.group(0).strip() for m in _UNIT_RE.finditer(text or "")]
    return [u for u in units if u]


def _strip_cites(text: str) -> str:
    return CITE_RE.sub("", text or "").strip()


def _clean_sentence(text: str) -> str:
    t = REFS_ECHO_RE.sub("", text or "")
    t = _strip_cites(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(" 　")


def _clusters_text(clusters: Sequence[Cluster], ref_index: dict[str, int]) -> str:
    """交给写手模型的簇清单。证据编号与全局 ref 编号一致，避免"簇内 1 号"和"全局 1 号"错位。"""
    blocks = []
    for c in clusters:
        ev_lines = []
        for claim in c.claims:
            n = ref_index.get(claim.chunk_id)
            ev_lines.append(f"  [引#{n}] {claim.quote}")
        conditions = "；".join(c.conditions) if c.conditions else "（无）"
        blocks.append(
            f"### {c.id}｜{c.stance_label}｜证据 {c.paper_count} 篇 / {len(c.claims)} 条\n"
            f"结论：{c.statement}\n"
            f"轴：{c.axis}\n"
            f"成立条件：{conditions}\n"
            f"证据：\n" + "\n".join(ev_lines)
        )
    return "\n\n".join(blocks)


def _conflicts_text(edges: Sequence[dict[str, Any]], clusters: dict[str, Cluster]) -> str:
    confirmed = [e for e in edges if e["confirmed"]]
    if not confirmed:
        return "（无）"
    lines = []
    for e in confirmed:
        a, b = e["clusters"]
        lines.append(
            f"- {e['id']}（{TYPE_LABELS.get(e['type'], e['type'])}）：{a} 与 {b} 在轴「{e['axis']}」上结论相反"
        )
    return "\n".join(lines)


def write_sections(
    topic: str,
    clusters: Sequence[Cluster],
    edges: Sequence[dict[str, Any]],
    ref_index: dict[str, int],
    *,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """
    返回 {"sections": {cluster_id: [sentence...]}, "dropped": [...], "violations": [...], "degraded": bool}。

    为什么让模型输出**一整段文字 + 行内 [引#N]**，而不是 `{"text":..., "refs":[2,8]}`：
    后者要把"引用编号"作为数组字段回传，模型经常写成 `……。refs":[2]`（把字段名当正文写进字符串），
    整块 JSON 因此解析失败，只能整体降级成模板——实测发生过一次，整篇综述都变成了模板腔。
    行内 `[引#N]` 是本项目 QA 链路用了很久的约定，模型写它几乎不出错，
    解析失败的概率低一个量级；句子边界与引用归属由服务端切，反正我们本来就要重排。
    """
    llm = llm or LLMClient()
    by_id = {c.id: c for c in clusters}
    dropped: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    # 只有**卷进冲突的那些簇**才检查"抹平措辞"。
    # 早先是对全文一律检查，结果一条讲 LoRA 秩的句子因为写了「研究表明」被删掉——
    # 它压根不涉及任何分歧，那句话只是行文习惯。禁令要落在"可能被用来抹平分歧"的位置，
    # 否则就成了对文风的独裁，删掉的都是正常句子。
    conflict_clusters = {c for e in edges if e["confirmed"] for c in e["clusters"]}

    prompt = prompts.review_write_prompt(
        topic,
        _clusters_text(clusters, ref_index),
        _conflicts_text(edges, by_id),
    )

    def run(extra: str = "") -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
        payload = llm.json_chat(
            prompt + extra, system_key="review_write", system=prompts.REVIEW_WRITE_SYSTEM
        )
        return _parse_sections(payload, clusters, by_id, ref_index, conflict_clusters)

    sections, dropped, violations = run()
    degraded = False

    # 引用是硬约束，但模型会"忘"——实测有一次 15 个簇全部返回了不带 [引#N] 的句子，
    # 于是每句都被判无引用删掉，整篇退化成模板腔。这不是约束错了，是模型没遵守。
    # 所以按"填满率"判一次重试：填不满一半就带着明确提醒再问一遍，取更好的那份。
    need = max(1, int(len(clusters) * 0.6))
    if len(sections) < need:
        retry_sections, retry_dropped, retry_violations = run(
            "\n\n上一次输出里大多数句子都没有 [引#N] 标记，这些句子会被系统整句删除。"
            "请**每一句**都以 [引#N] 结尾（N 取自该簇证据清单），例如："
            "「LoRA 的推理延迟低于全量微调。[引#2]」"
        )
        if len(retry_sections) > len(sections):
            sections, dropped, violations = retry_sections, retry_dropped, retry_violations

    if not sections:
        degraded = True
        dropped.append({"reason": "write_no_usable_sentence"})

    # 模型漏写的簇：记下来，渲染阶段用模板补齐（否则这个有证据的观点会凭空消失）
    for c in clusters:
        if c.id not in sections:
            degraded = True
            dropped.append({"cluster_id": c.id, "reason": "section_missing_template_used"})

    return {"sections": sections, "dropped": dropped, "violations": violations, "degraded": degraded}


def _parse_sections(
    payload: dict[str, Any],
    clusters: Sequence[Cluster],
    by_id: dict[str, Cluster],
    ref_index: dict[str, int],
    conflict_clusters: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """把模型返回的 sections 拆成"可进正文的句子"——引用校验、违规判定都在这儿。"""
    sections: dict[str, list[dict[str, Any]]] = {}
    dropped: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    if "error" in payload or not isinstance(payload.get("sections"), list):
        dropped.append({"reason": "write_json_failed", "detail": str(payload)[:200]})
        return sections, dropped, violations

    for raw in payload["sections"]:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("cluster_id") or "").strip()
        cluster = by_id.get(cid)
        if cluster is None:
            dropped.append({"reason": "unknown_cluster", "cluster_id": cid})
            continue
        allowed = {ref_index[c.chunk_id] for c in cluster.claims if c.chunk_id in ref_index}
        kept: list[dict[str, Any]] = []
        for sent in _split_sentences(str(raw.get("text") or "")):
            refs: list[int] = []
            for m in CITE_RE.finditer(sent):
                n = int(m.group(1))
                if n in allowed and n not in refs:
                    refs.append(n)
            text = _clean_sentence(sent)
            if not text:
                continue
            if cid in conflict_clusters and any(mk in text for mk in MERGE_MARKERS):
                violations.append({"cluster_id": cid, "text": text, "reason": "merge_marker"})
                continue
            if not refs:
                # 无有效引用 → 不得写入正文。
                dropped.append({"cluster_id": cid, "reason": "no_valid_ref", "text": text[:80]})
                continue
            kept.append({"text": text, "refs": refs})
        if kept:
            sections[cid] = kept
        else:
            dropped.append({"cluster_id": cid, "reason": "section_empty_after_filter"})
    return sections, dropped, violations


def _fallback_sentences(cluster: Cluster, ref_index: dict[str, int]) -> list[dict[str, Any]]:
    """模板兜底：簇结论 + 原文引文（截断到 120 字）。不产出任何无引用的句子。"""
    out: list[dict[str, Any]] = []
    for claim in cluster.claims:
        n = ref_index.get(claim.chunk_id)
        if not n:
            continue
        quote = re.sub(r"\s+", " ", claim.quote).strip()
        if len(quote) > 120:
            quote = quote[:120].rstrip() + "…"
        out.append(
            {
                "text": f"{_strip_cites(claim.statement)}。原文：“{quote}”",
                "refs": [n],
            }
        )
    return out[:3]


def render_markdown(
    topic: str,
    clusters: Sequence[Cluster],
    edges: Sequence[dict[str, Any]],
    sections: dict[str, list[dict[str, Any]]],
    refs: dict[int, dict[str, Any]],
    ref_index: dict[str, int],
    *,
    meta: dict[str, Any],
) -> str:
    """
    服务端渲染。所有"立场/证据强度/冲突"的标注都在这里生成，模型无权决定。
    """
    lines: list[str] = [f"# {topic}", ""]
    lines.append(
        f"> 本综述基于 {meta['paper_count']} 篇文献的 {meta['claim_count']} 条结论自动生成，"
        f"归并为 {meta['cluster_count']} 个观点簇（prompt {meta['prompt_version']} · "
        f"配置 {meta['config_version']}/{meta['fingerprint']}）。"
    )
    lines.append(
        f"> 每段只陈述一个观点簇；已确认的分歧单独标注、不合并叙述。"
        f"共 {meta['conflict_count']} 处冲突、{meta['single_paper']} 个观点仅单篇支持。"
    )
    lines.append("")

    # 冲突块挂在"编号靠后"的那个簇之后，读者先看完双方再看到对峙。
    # 必须按**簇在列表里的位置**比较，不能用 id 字符串比大小：
    # 章节号现在是纯数字，"10" < "9"（'1' < '9'）依然成立，
    # 按字符串排会把冲突块插到 9 号后面、10 号前面，读者先看到"冲突"再看到被冲突的一方。
    order = {c.id: i for i, c in enumerate(clusters)}
    conflict_after: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        if e["confirmed"] is False:
            continue  # 被否掉的"冲突"不进正文，但仍留在响应体的冲突清单里可审计
        a, b = e["clusters"]
        anchor = a if order.get(a, -1) > order.get(b, -1) else b
        conflict_after.setdefault(anchor, []).append(e)

    for c in clusters:
        sentences = sections.get(c.id)
        if not sentences:
            sentences = _fallback_sentences(c, ref_index)
        if not sentences:
            continue  # 一条带引用的句子都凑不出来 → 这个观点不进正文（宁缺毋滥）

        strength_note = "，**仅单篇支持**" if c.strength == "single_paper" else ""
        stance_note = "（主张该轴的反向结论）" if c.polarity == "oppose" else ""
        lines.append(f"## {c.id}｜{c.statement}")
        lines.append("")
        lines.append(
            f"> 立场：**{c.stance_label}**{stance_note} ｜ 证据：{c.paper_count} 篇 / "
            f"{len(c.claims)} 条{strength_note} ｜ 轴：{c.axis}"
        )
        if c.conditions:
            lines.append(f"> 成立条件：{'；'.join(c.conditions)}")
        lines.append("")
        body = "".join(f"{s['text']}{''.join(f'[引#{n}]' for n in s['refs'])}" for s in sentences)
        lines.append(body)
        lines.append("")

        for e in conflict_after.get(c.id, []):
            lines.extend(_render_conflict(e, clusters, sections, ref_index))

    single = [c for c in clusters if c.strength == "single_paper"]
    if single:
        lines.append("## 附：证据强度汇总（系统生成，非文献结论）")
        lines.append("")
        for c in single:
            lines.append(f"- {c.id}｜{c.statement} —— 仅单篇支持（{c.papers[0]['title'][:60]}）")
        lines.append("")

    lines.append("## 参考文献")
    lines.append("")
    seen: dict[str, dict[str, Any]] = {}
    for c in clusters:
        for p in c.papers:
            seen[p["arxiv_id"]] = p
    for i, aid in enumerate(sorted(seen), 1):
        title = seen[aid]["title"]
        # 手工上传的论文以 upload_xxx 作 ID，写进参考文献只是噪音
        if aid.startswith("upload_"):
            lines.append(f"{i}. {title}")
        else:
            lines.append(f"{i}. {title} (arXiv:{aid})")
    lines.append("")
    return "\n".join(lines)


def _render_conflict(
    e: dict[str, Any],
    clusters: Sequence[Cluster],
    sections: dict[str, list[dict[str, Any]]],
    ref_index: dict[str, int],
) -> list[str]:
    """冲突块。内容全部来自已核实的冲突边与簇结构，不由写手模型生成。"""
    by_id = {c.id: c for c in clusters}
    a, b = by_id[e["clusters"][0]], by_id[e["clusters"][1]]
    label = TYPE_LABELS.get(e["type"], e["type"])
    badge = "已确认" if e["confirmed"] else "待复核"
    out = [
        f"### {e['id']}｜{a.id} ↔ {b.id}（{label}·{badge}）",
        "",
        f"> **{a.id}｜{a.stance_label}（{a.paper_count} 篇）**：{a.statement}",
        f"> **{b.id}｜{b.stance_label}（{b.paper_count} 篇）**：{b.statement}",
        f"> 判据：{e['reason'] or '（规则召回，未经模型复核）'}",
        f"> 轴：{e['axis']}",
        "",
    ]
    # 各给一句带引用的原文，读者可以立刻自己判断谁对
    for c in (a, b):
        first = (sections.get(c.id) or _fallback_sentences(c, ref_index))[:1]
        for s in first:
            out.append(f"{s['text']}{''.join(f'[引#{n}]' for n in s['refs'])}")
        out.append("")
    return out
