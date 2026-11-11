"""
观点聚类：把 claim 归并成「观点簇」。

本模块只有一条不变量，其余都是它的实现细节：

    **极性是簇键的一部分。相反结论永远进不了同一个簇。**

为什么把它写成结构约束而不是 prompt 约束：
如果聚类只看语义相似度，「LoRA 匹配全量微调」与「LoRA 不如全量微调」的余弦相似度能到 0.9+，
必然被合并；合并之后生成模型拿到的就是一个"中性簇"，
它只能写出「研究表明两者性能关系复杂」——这正是任务书禁止的那句话。
把 polarity 放进簇键之后，这两条 claim 从聚类那一刻起就分属两个簇，
生成阶段**没有"把它们写在一起"的位置**，禁令由结构执行。

簇键 = (规范化后的 axis, polarity)，两级构造：
1. `axis` 先做**规范合并**：字面相同直接并；字面不同但语义相似度 ≥ `review_axis_merge_sim`
   的也并（LLM 对同一个维度会写出"下游任务性能""下游任务表现"这种措辞漂移）。
   语义模型不可用时退回纯字面合并——会多分出几个簇，但绝不错误合并，
   因为"多一个簇"的代价是正文多一段，"错并一个簇"的代价是把矛盾写没了。
2. 按 (axis_canon, polarity) 分组，每组一个簇。

同一轴下的 support / oppose / conditional 天然形成三个簇，
这也正好是冲突检测的输入：**同轴异极性 = 冲突候选**，不需要额外阈值去"猜"哪里可能有冲突。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.config import settings
from app.services.rag import semantic
from app.services.review import prompts
from app.services.review.claims import POLARITY_LABELS, Claim, _norm

_AXIS_NOISE = re.compile(r"[\s　]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")


def axis_key(axis: str) -> str:
    """轴的规范化主键。去空格是刻意的：中文轴里加不加空格纯属模型随性。"""
    t = _norm(axis).replace(":", "：")
    return _AXIS_NOISE.sub("", t)


def entities(axis: str) -> set[str]:
    """轴里的对象名（拉丁词）。`ViT 相对 ResNet：ImageNet 准确率` → {vit, resnet, imagenet}。"""
    return {m.group(0).lower() for m in _LATIN.finditer(axis or "")}


@dataclass
class Cluster:
    id: str
    axis: str
    axis_key: str
    polarity: str
    claims: list[Claim] = field(default_factory=list)

    @property
    def stance_label(self) -> str:
        return POLARITY_LABELS.get(self.polarity, self.polarity)

    @property
    def statement(self) -> str:
        """簇的代表结论：取证据命中率最高、再取最长的一条（确定性排序）。"""
        best = sorted(
            self.claims,
            key=lambda c: (-round(c.quote_score, 2), -len(c.statement), c.id),
        )
        return best[0].statement if best else ""

    @property
    def papers(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for c in self.claims:
            if c.arxiv_id not in seen:
                seen[c.arxiv_id] = {"arxiv_id": c.arxiv_id, "title": c.title}
        return [seen[k] for k in sorted(seen)]

    @property
    def paper_count(self) -> int:
        return len({c.arxiv_id for c in self.claims})

    @property
    def strength(self) -> str:
        """证据强度：多篇支持 / 仅单篇支持。

        按**论文数**而不是 claim 数：一篇论文里说三遍同一个结论，证据强度仍然是"单篇"。
        """
        return "single_paper" if self.paper_count <= 1 else "multi_paper"

    @property
    def conditions(self) -> list[str]:
        out: list[str] = []
        for c in self.claims:
            if c.condition and c.condition not in out:
                out.append(c.condition)
        return out

    def to_dict(self, evidence_index: dict[str, int] | None = None) -> dict[str, Any]:
        idx = evidence_index or {}
        return {
            "id": self.id,
            "statement": self.statement,
            "axis": self.axis,
            "axis_key": self.axis_key,
            "stance": self.polarity,
            "stance_label": self.stance_label,
            "strength": self.strength,
            "paper_count": self.paper_count,
            "claim_count": len(self.claims),
            "papers": self.papers,
            "conditions": self.conditions,
            "evidence": [
                {
                    "claim_id": c.id,
                    "statement": c.statement,
                    "quote": c.quote,
                    "arxiv_id": c.arxiv_id,
                    "title": c.title,
                    "page": c.page,
                    "section": c.section,
                    "kind": c.kind,
                    "chunk_id": c.chunk_id,
                    "ref": idx.get(c.chunk_id),
                    "quote_score": c.quote_score,
                }
                for c in self.claims
            ],
        }


# ------------------------------------------------------------------ 轴规范合并

def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[max(ri, rj)] = min(ri, rj)


def _lexical_canon(axes: list[str]) -> tuple[list[str], dict[str, Any]]:
    """
    退回字面 + 语义相似度的归并（LLM 不可用时）。

    先按字面 key 归并（零成本、确定性），再对"字面不同"的轴两两算语义相似度：
    轴的数量远小于 claim（一般 5~15 个），两两比较完全负担得起。
    """
    uniq = list(dict.fromkeys(a for a in axes if a))
    uf = _UnionFind(len(uniq))
    pos = {k: i for i, k in enumerate(uniq)}

    merged: list[dict[str, Any]] = []
    model_used = False
    if len(uniq) > 1 and semantic.enabled():
        vecs = semantic.embed_many(uniq)
        if vecs:
            model_used = True
            th = float(settings.review_axis_merge_sim)
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    sim = _cos(vecs[i], vecs[j])
                    if sim >= th:
                        uf.union(i, j)
                        merged.append({"a": uniq[i], "b": uniq[j], "sim": round(sim, 3)})

    groups: dict[int, list[str]] = {}
    for k in uniq:
        groups.setdefault(uf.find(pos[k]), []).append(k)
    canon_of: dict[str, str] = {}
    for _, members in groups.items():
        rep = sorted(members, key=lambda s: (len(s), s))[0]
        for m in members:
            canon_of[m] = rep

    return [canon_of.get(a, a) for a in axes], {
        "method": "semantic" if model_used else "literal",
        "axes": len(uniq),
        "merged": merged,
    }


def _llm_canon(axes: list[str], llm) -> tuple[list[str], dict[str, Any]]:
    """
    LLM 归并（首选）。

    为什么不用"阈值调参"解决轴归并：轴是**语义单位**，不是字符串。
    `Transformer：下游任务性能` 与 `BERT 相对 其他系统：GLUE测试准确率` 余弦相似度不低，
    但前者是"Transformer 这族架构的水平"，后者是"BERT 在某个榜单上的位置"，合并就是错的；
    反过来 `ViT 相对 BiT ResNet：ImageNet 准确率` 与 `ViT 相对 ResNet：ImageNet 准确率`
    余弦只有 0.8 出头，却必须合并。阈值挡不住这两类错误，只有"看懂维度是否相同"才行。
    LLM 归并失败（不可用/解析失败/漏了某些写法）时逐条退回字面归并，绝不丢轴。
    """
    uniq = list(dict.fromkeys(a for a in axes if a))
    if len(uniq) <= 1:
        return list(axes), {"method": "trivial", "axes": len(uniq), "merged": []}

    payload = llm.json_chat(
        prompts.canonicalize_prompt(uniq),
        system_key="review_canonicalize",
        system=prompts.CANONICALIZE_SYSTEM,
    )
    if "error" in payload or not isinstance(payload.get("axes"), list):
        return _lexical_canon(axes)

    # 原始写法 → 标准轴。用归一化后的字面做键，容忍模型把标点/空格改了。
    by_raw: dict[str, str] = {}
    merged: list[dict[str, Any]] = []
    rejected_merges: list[dict[str, Any]] = []
    for group in payload["axes"]:
        if not isinstance(group, dict):
            continue
        canonical = str(group.get("canonical") or "").strip()
        members = [str(m).strip() for m in (group.get("members") or []) if str(m).strip()]
        if not canonical or not members:
            continue
        canonical_ents = entities(canonical)
        kept_members: list[str] = []
        for m in members:
            m_ents = entities(m)
            # 对象必须重叠，否则不许并。实测反例：模型曾把
            # `LoRA 相对 全量微调：下游任务性能` 并进 `Transformer：下游任务性能`——
            # 两者都在讲"下游任务性能"，但对象根本不是一回事。
            # 余弦相似度拦不住这种（框架词占了大半），"有没有共同的拉丁实体"能。
            if canonical_ents and m_ents and not (canonical_ents & m_ents):
                rejected_merges.append({"member": m, "canonical": canonical})
                continue
            kept_members.append(m)
        for m in kept_members:
            by_raw[_norm(m)] = canonical
        if len(kept_members) > 1:
            merged.append({"canonical": canonical, "members": kept_members})

    if not by_raw:
        return _lexical_canon(axes)

    unmapped = [a for a in uniq if _norm(a) not in by_raw]
    out = [by_raw.get(_norm(a), a) for a in axes]
    diag = {
        "method": "llm",
        "axes": len(uniq),
        "canonical_axes": len({*by_raw.values(), *unmapped}),
        "merged": merged,
        "rejected_merges": rejected_merges,
        "unmapped": unmapped,
    }
    if unmapped:
        # 漏掉的写法不丢，按字面各成一组
        diag["method"] = "llm+literal"
    return out, diag


def canonical_axes(claims: Sequence[Claim], llm=None) -> tuple[list[str], list[str], dict[str, Any]]:
    """返回 (每条 claim 的轴键, 每条 claim 的轴显示名, 诊断)。"""
    raw = [c.axis for c in claims]
    if llm is not None and raw:
        canon, diag = _llm_canon(raw, llm)
    else:
        canon, diag = _lexical_canon(raw)
    keys = [axis_key(c) for c in canon]
    return keys, canon, diag


def _mean(vecs: list[Sequence[float]]) -> list[float]:
    n = len(vecs)
    return [sum(v[i] for v in vecs) / n for i in range(len(vecs[0]))]


def _split_by_statement(
    members: list[Claim], key: str, polarity: str, log: list[dict[str, Any]]
) -> list[list[Claim]]:
    """
    在 (轴, 极性) 桶内按结论相似度再切一刀。只在语义模型可用时生效。

    用的是"贪心 + 质心"而不是层次聚类：claim 数量最多几十条，
    贪心的结果与输入顺序绑定、完全确定性，重跑两次簇编号不会漂——这比"更优的分割"重要。
    """
    if len(members) <= 1:
        return [members]
    vecs = semantic.embed_many([m.statement for m in members])
    if not vecs:
        return [members]

    th = float(settings.review_statement_split_sim)
    groups: list[list[int]] = [[0]]
    centroids: list[list[float]] = [list(vecs[0])]
    for i in range(1, len(members)):
        best_j, best_sim = -1, -1.0
        for j, cen in enumerate(centroids):
            sim = _cos(vecs[i], cen)
            if sim > best_sim:
                best_j, best_sim = j, sim
        if best_sim >= th:
            groups[best_j].append(i)
            centroids[best_j] = _mean([vecs[k] for k in groups[best_j]])
        else:
            groups.append([i])
            centroids.append(list(vecs[i]))

    if len(groups) == 1:
        return [[members[i] for i in groups[0]]]
    log.append(
        {
            "axis": key,
            "polarity": polarity,
            "n": len(members),
            "groups": [[members[i].id for i in g] for g in groups],
        }
    )
    return [[members[i] for i in g] for g in groups]


def cluster_claims(
    claims: Sequence[Claim], *, llm=None
) -> tuple[list[Cluster], dict[str, Any]]:
    """按 (轴, 极性) 分组。返回按证据强度排序的簇列表 + 诊断信息。"""
    if not claims:
        return [], {
            "axes": 0,
            "canonical_axes": 0,
            "clusters": 0,
            "method": "none",
            "merged": [],
        }

    keys, canon, diag = canonical_axes(claims, llm)

    buckets: dict[tuple[str, str], list[tuple[Claim, str]]] = {}
    for claim, key, axis_display in zip(claims, keys, canon):
        claim.axis_key = key
        buckets.setdefault((key, claim.polarity), []).append((claim, axis_display))

    clusters: list[Cluster] = []
    split_log: list[dict[str, Any]] = []
    for (key, polarity), members in buckets.items():
        # 轴 + 极性相同还不够，再按**结论本身的相似度**聚一次。
        #
        # 为什么需要这道闸：轴是抽取模型给的标签，它会把标签打错。
        # 实测过一条「ViT 可以把图像切成 16×16 的块再线性嵌入」被打了
        # `ViT 相对 BiT ResNet：ImageNet 准确率` 的轴——描述架构的结论和比准确率的结论
        # 落进同一个簇，接着就跟"小数据下 ResNet 更好"配成一对，被复核模型
        # 硬解释成"条件性分歧"。轴是模型的**判断**，结论文本是**事实**，
        # 两者一致才允许合并；只信轴就会把不相干的结论焊在一起。
        groups = _split_by_statement([c for c, _ in members], key, polarity, split_log)
        for group in groups:
            clusters.append(
                Cluster(id="", axis=members[0][1], axis_key=key, polarity=polarity, claims=group)
            )

    # 排序：证据强的在前；同强度按 claim 数、轴、极性。全确定性，保证两次跑结果一致。
    clusters.sort(
        key=lambda c: (-c.paper_count, -len(c.claims), c.axis_key, c.polarity)
    )
    for i, c in enumerate(clusters, 1):
        # 序号直接当 id，不加字母前缀。
        # 旧的 `O1/O2` 是「观点簇」的英文缩写，纯展示用、没有分类含义；
        # 但它和引用编号 `[引#N]` 并排出现时，两套字母体系读者要额外分辨，
        # 正文里也更愿意读 `1｜…、2｜…`。改数字后必须保证：
        #   - 冲突块的编号不能也用纯数字（会跟章节号撞），见 conflict.py 的「冲突N」；
        #   - 任何按 id 字符串排序的地方都要换成按位置排（"10" < "9"）。
        c.id = str(i)

    diag["clusters"] = len(clusters)
    diag["statement_splits"] = split_log
    diag["by_stance"] = {
        p: sum(1 for c in clusters if c.polarity == p) for p in ("support", "oppose", "conditional")
    }
    diag["single_paper_clusters"] = [c.id for c in clusters if c.strength == "single_paper"]
    return clusters, diag
