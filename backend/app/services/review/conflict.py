"""
冲突检测：找出「互斥结论」并标注它们的分歧类型。

两段式，规则召回 + LLM 复核，顺序不能反：

**规则召回（便宜、确定性、可复现）**
聚类已经把「同轴异极性」摆在一起了，所以召回就是：同一根轴下极性不同的簇两两配对。
这一步不需要任何阈值——同轴异极性**就是**冲突的定义，不需要再猜"哪里可能有冲突"。
（轴相似度只在"两条轴字面不同但可能同义"时用作二次召回，见 `_semantic_pairs`。）

**LLM 复核（贵、但能把"条件不同"和"结论互斥"分开）**
光有规则会误报：`ViT 在小数据下不如 ResNet` 与 `ViT 在大数据下超过 ResNet`
极性相反、同轴，但两者**都对**——它们不是矛盾，是条件不同。这个区别规则判不了，
必须让模型给出 `type` 与 `reason`。三种类型：
- `direct_contradiction`：同条件范围内互相否定，不可能同真；
- `conditional_split`：各自在自己的条件下成立，冲突来自条件差异（仍要标注，因为读者需要知道"为什么文献说法不一样"）；
- `scope_mismatch`：比较对象/指标/任务其实不同，**不构成**冲突（`confirmed=false`）。

降级规则：LLM 不可用时，召回结果保留为 `confirmed=null`（待复核），
不冒充"已确认"，也不丢弃——"这里可能有一处矛盾但我们没核实"本身就是有用信息。
"""
from __future__ import annotations

import math
import re
from typing import Any, Sequence

from app.core.config import settings
from app.services.llm_client import LLMClient
from app.services.rag import semantic
from app.services.review import prompts
from app.services.review.cluster import Cluster, entities as _entities

TYPE_DIRECT = "direct_contradiction"
TYPE_CONDITIONAL = "conditional_split"
TYPE_SCOPE = "scope_mismatch"

TYPE_LABELS = {
    TYPE_DIRECT: "互斥结论",
    TYPE_CONDITIONAL: "条件性分歧",
    TYPE_SCOPE: "范围不一致",
    "pending": "待复核",
}


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _opposing(a: Cluster, b: Cluster) -> bool:
    """
    这一对是否**方向相反**——冲突的必要条件。

    为什么不是简单的 `polarity !=`：`support` 与 `conditional` 不是互斥关系。
    「LoRA 不增加推理延迟」（support）与「LoRA 在短序列下延迟更低」（conditional）
    是**同一方向、不同范围**的两句话，把它们当冲突是纯误报（实测确实误报过，
    还被复核模型将错就错解释成"条件不同导致的分歧"）。
    真正的冲突必须有一方明确主张**反向**，也就是 `oppose`。
    所以配对条件是：两边方向不同 **且** 其中至少一边是 oppose。
    """
    if a.polarity == b.polarity:
        return False
    return "oppose" in (a.polarity, b.polarity)


def _type_hint(a: Cluster, b: Cluster) -> str:
    """规则给的先验类型：只要有一边带条件，就优先按"条件性分歧"理解。"""
    if a.polarity == "conditional" or b.polarity == "conditional":
        return TYPE_CONDITIONAL
    if a.conditions or b.conditions:
        return TYPE_CONDITIONAL
    return TYPE_DIRECT


def _semantic_pairs(
    clusters: list[Cluster],
) -> list[tuple[Cluster, Cluster, float, str]]:
    """
    二次召回：轴字面不同（归并阶段没并到一起）但语义上可能是同一维度的簇对。

    为什么阈值用 `review_conflict_axis_sim`（0.72）而不是聚类用的 `review_axis_merge_sim`（0.78）：
    这里宁可多召回几对交给 LLM 否掉，也不能漏掉一处真矛盾——
    漏召回是"综述把分歧写没了"，误召回只是多一次复核，代价不对称。

    为什么还要加"对象必须重叠"这道闸：
    纯靠余弦会把 `LoRA 相对 全量微调：训练速度` 和 `BERT 相对 其他模型：下游任务性能`
    也配成一对（都含"相对…：性能"这种框架词，余弦不低）。这种对子既没有信息量，
    还会把 LLM 复核名额挤掉。**同一个轴必然共享同一个对象名**，
    所以要求两条轴的拉丁实体集合有交集，是最省事又最准的闸门。
    """
    out: list[tuple[Cluster, Cluster, float, str]] = []
    if not semantic.enabled() or len(clusters) < 2:
        return out
    texts = [c.axis for c in clusters]
    vecs = semantic.embed_many(texts)
    if not vecs:
        return out
    th = float(settings.review_conflict_axis_sim)
    ents = [_entities(c.axis) for c in clusters]
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            a, b = clusters[i], clusters[j]
            if not _opposing(a, b):
                continue
            # 两边都有拉丁实体时，必须共享至少一个；没法判断（中文轴）时放行交给复核。
            if ents[i] and ents[j] and not (ents[i] & ents[j]):
                continue
            sim = _cos(vecs[i], vecs[j])
            if sim >= th:
                out.append((a, b, round(sim, 3), "semantic"))
    return out


def _statement_sims(
    clusters: list[Cluster], pairs: list[tuple[Cluster, Cluster, float, str]]
) -> dict[tuple[str, str], float]:
    """候选对的"结论相似度"。语义模型不可用时返回空表（不做这道闸）。"""
    if not pairs or not semantic.enabled():
        return {}
    vecs = semantic.embed_many([c.statement for c in clusters])
    if not vecs:
        return {}
    pos = {c.id: vecs[i] for i, c in enumerate(clusters)}
    out: dict[tuple[str, str], float] = {}
    for a, b, _, _ in pairs:
        va, vb = pos.get(a.id), pos.get(b.id)
        if va and vb:
            out[(a.id, b.id)] = round(_cos(va, vb), 3)
    return out


def detect_conflicts(
    clusters: list[Cluster],
    *,
    llm: LLMClient | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """返回 (冲突边列表, 诊断信息)。"""
    if len(clusters) < 2:
        return [], {"candidates": 0, "confirmed": 0, "llm_used": False}

    # ① 精确召回：同一根轴 + 极性不同
    exact: list[tuple[Cluster, Cluster, float, str]] = []
    by_axis: dict[str, list[Cluster]] = {}
    for c in clusters:
        by_axis.setdefault(c.axis_key, []).append(c)
    for group in by_axis.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if _opposing(a, b):
                    exact.append((a, b, 1.0, "exact"))

    # ② 语义召回（补齐轴措辞漂移）
    seen = {(min(a.id, b.id), max(a.id, b.id)) for a, b, _, _ in exact}
    candidates = list(exact)
    for a, b, sim, src in _semantic_pairs(clusters):
        key = (min(a.id, b.id), max(a.id, b.id))
        if key not in seen:
            seen.add(key)
            candidates.append((a, b, sim, src))

    if not candidates:
        return [], {"candidates": 0, "confirmed": 0, "llm_used": False}

    # 按"同轴强度"排序：精确同轴优先复核，语义召回排后面。
    # 复核前先过一道"结论相似度"闸：一对互斥结论必须**说的是同一件事**，只是方向相反。
    # 连结论都不像（实测 0.53 vs 0.70 两档分得很开），就不是矛盾而是两件事，
    # 交给复核模型只会被它硬凑出一个"条件性分歧"的理由。
    stmt_sim = _statement_sims(clusters, candidates)
    min_stmt = float(settings.review_conflict_statement_sim)
    kept: list[tuple[Cluster, Cluster, float, str]] = []
    for a, b, sim, src in candidates:
        s = stmt_sim.get((a.id, b.id))
        if s is not None and s < min_stmt:
            continue
        kept.append((a, b, sim, src))
    candidates = kept
    if not candidates:
        return [], {"candidates": 0, "confirmed": 0, "llm_used": False, "statement_gate_dropped": True}

    candidates.sort(key=lambda t: (-t[2], t[0].id, t[1].id))

    edges: list[dict[str, Any]] = []
    for a, b, sim, src in candidates:
        edges.append(
            {
                "id": "",
                "clusters": [a.id, b.id],
                "axis": a.axis,
                "type": _type_hint(a, b),
                "type_label": TYPE_LABELS[_type_hint(a, b)],
                "confirmed": None,
                "reason": "",
                "rule": {
                    "axis_match": src,
                    "axis_sim": sim,
                    "stances": [a.polarity, b.polarity],
                    "type_hint": _type_hint(a, b),
                },
                "papers": _merge_papers(a, b),
                "source": "rule",
            }
        )

    # ③ LLM 复核（只复核前 N 对，其余保留为待复核）
    llm = llm or LLMClient()
    limit = max(0, int(settings.review_conflict_llm_pairs))
    to_verify = edges[:limit]
    llm_used = False
    if to_verify:
        lookup = {c.id: c for c in clusters}
        payload_pairs = []
        for i, e in enumerate(to_verify, 1):
            a, b = lookup[e["clusters"][0]], lookup[e["clusters"][1]]
            payload_pairs.append(
                {
                    "pair_id": f"P{i}",
                    "axis": a.axis,
                    "a": {
                        "statement": a.statement,
                        "stance": a.stance_label,
                        "paper_count": a.paper_count,
                        "condition": "；".join(a.conditions),
                    },
                    "b": {
                        "statement": b.statement,
                        "stance": b.stance_label,
                        "paper_count": b.paper_count,
                        "condition": "；".join(b.conditions),
                    },
                }
            )
        result = llm.json_chat(
            prompts.conflict_verify_prompt(payload_pairs),
            system_key="review_conflict_verify",
            system=prompts.CONFLICT_VERIFY_SYSTEM,
        )
        if "error" not in result:
            llm_used = True
            verdicts = {
                str(v.get("pair_id")): v for v in (result.get("verdicts") or []) if isinstance(v, dict)
            }
            for i, e in enumerate(to_verify, 1):
                v = verdicts.get(f"P{i}")
                if not v:
                    continue
                vtype = str(v.get("type") or "").strip()
                if vtype not in (TYPE_DIRECT, TYPE_CONDITIONAL, TYPE_SCOPE):
                    vtype = e["rule"]["type_hint"]
                e["confirmed"] = bool(v.get("confirmed"))
                e["type"] = vtype
                e["type_label"] = TYPE_LABELS[vtype]
                e["reason"] = str(v.get("reason") or "").strip()
                e["source"] = "rule+llm"
        else:
            for e in to_verify:
                e["reason"] = "LLM 复核不可用，保留规则召回结果待人工确认"
                e["source"] = "rule"

    for e in edges:
        if e["confirmed"] is None and not e["reason"]:
            e["reason"] = "规则召回（同轴异极性），未进入 LLM 复核批次"
        if e["confirmed"] is None:
            e["type_label"] = TYPE_LABELS.get(e["type"], e["type"]) + "（待复核）"

    # 只留两类：① 同轴异极性的精确候选（哪怕没排进复核批次，它按定义就是分歧）；
    # ② 过了复核的语义候选。**没被复核过的语义候选直接丢掉**——
    # 它们只是"余弦看起来像"，留着会在界面上堆一堆「待复核」，
    # 把一个本来只有 1 处冲突的综述显示成 12 处，读者反而找不到真东西。
    edges = [
        e
        for e in edges
        if e["confirmed"] is not None or e["rule"]["axis_match"] == "exact"
    ]

    for i, e in enumerate(edges, 1):
        # 不能用纯数字：章节号已经是 1/2/3…，冲突块再用数字会出现两个「2」。
        # 也不要再用 C 前缀——正文里的编号体系只保留阿拉伯数字这一套。
        e["id"] = f"冲突{i}"

    confirmed = [e for e in edges if e["confirmed"]]
    diag = {
        "candidates": len(edges),
        "confirmed": len(confirmed),
        "rejected": len([e for e in edges if e["confirmed"] is False]),
        "pending": len([e for e in edges if e["confirmed"] is None]),
        "llm_used": llm_used,
        "by_type": {
            t: sum(1 for e in confirmed if e["type"] == t)
            for t in (TYPE_DIRECT, TYPE_CONDITIONAL, TYPE_SCOPE)
        },
    }
    return edges, diag


def _merge_papers(a: Cluster, b: Cluster) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in (a, b):
        for p in c.papers:
            out[p["arxiv_id"]] = p
    return [out[k] for k in sorted(out)]
