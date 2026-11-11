"""
一句式解读与一跳推荐。

为什么是规则而不是让模型写：
- 解读里必须带数字（PageRank 排名、betweenness、被引次数、哪几篇引用它），
  模型很容易把数字写错或编一个，而这类错误在可信度上最致命；
- 同一份数据要能复现出同一句话，截图和结论才对得上；
- 规则本身可以展示给用户看（`RULES`），"为什么说它是基石"这个问题的答案
  不该是"模型觉得"。

规则输出的是**标签 + 一句带数字的话 + 依据条目**，三者一一对应；
标签只是入口，依据才是内容。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LABELS: dict[str, dict[str, str]] = {
    "cornerstone": {"label": "基石论文", "tone": "signal",
                    "hint": "库内多篇论文的共同前置，中心性最高"},
    "bridge": {"label": "桥梁论文", "tone": "amber",
               "hint": "连接两簇论文的中转，删掉就断链"},
    "derivative": {"label": "边缘衍生", "tone": "muted",
                   "hint": "大量引用库内论文，但很少被库内引用"},
    "cited_but_peripheral": {"label": "高频被引但非关键", "tone": "muted",
                             "hint": "被引次数多，但引用它的是低权重论文"},
    "gap": {"label": "库外缺口", "tone": "violet",
            "hint": "库内多篇引用它，但它不在库里"},
    "isolated": {"label": "孤立节点", "tone": "muted",
                 "hint": "与库内其它论文没有引用关系"},
    "normal": {"label": "一般节点", "tone": "muted", "hint": "有引用关系，但不在结构关键位置"},
}

# 判定规则表：直接返回给前端展示，避免"标签是黑盒"
RULES: list[dict[str, Any]] = [
    {
        "label": "基石论文",
        "cond": "PageRank 排名进前 25%（至少第 1）＋ 库内被引 ≥ 2 ＋ PageRank ≥ 1.5/N（N = 库内篇数，即高于均匀分布 50% 以上）",
        "why": "被多篇论文共同当前置，且这些引用者本身也有分量；第三条是防「并列第 1」——小图里一堆悬挂节点分数完全相同，只看排名会全部命中",
    },
    {
        "label": "桥梁论文",
        "cond": "betweenness 排第 1、≥ 0.15，且入度 ≥ 1、出度 ≥ 1（纯源头或纯汇聚不算中转）",
        "why": "结构上绕不过去：删掉它，两簇之间就没有通路。多标签并存时按 基石 > 桥梁 > 衍生 取主标签，侧面标签仍保留",
    },
    {
        "label": "边缘衍生",
        "cond": "引用库内 ≥ 2 篇，被库内引用 ≤ 1 篇",
        "why": "沿用了既有工作而不是被沿用",
    },
    {
        "label": "高频被引但非关键",
        "cond": "库内被引 ≥ 2，PageRank 排名落在后 40%，且未判为基石",
        "why": "次数多但引用它的论文本身边缘——这正是「被引多 ≠ 重要」",
    },
    {
        "label": "库外缺口",
        "cond": "不在库内，但被库内 ≥ 2 篇引用",
        "why": "补进来能一次连上多条边，性价比最高的补库目标",
    },
    {
        "label": "孤立节点",
        "cond": "出入度都为 0",
        "why": "参考文献没解析到、方向确实独立，或库太小",
    },
]

MAX_NAME = 3


def _names(titles: list[str], limit: int = MAX_NAME) -> str:
    clean = [t.strip() for t in titles if t and t.strip()]
    if not clean:
        return ""
    short = [t if len(t) <= 26 else t[:25].rstrip() + "…" for t in clean[:limit]]
    more = len(clean) - len(short)
    return "、".join(short) + (f" 等 {len(clean)} 篇" if more > 0 else "")


@dataclass
class NodeView:
    """解读需要的节点上下文：结构 + 指标 + 标题。"""

    node_id: str
    title: str
    kind: str  # library | gap
    in_edges: list[tuple[str, int]] = field(default_factory=list)   # (引用者, 提及次数)
    out_edges: list[tuple[str, int]] = field(default_factory=list)  # (被引者, 提及次数)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def in_degree(self) -> int:
        return len({u for u, _ in self.in_edges})

    @property
    def out_degree(self) -> int:
        return len({v for v, _ in self.out_edges})

    @property
    def mentions_in(self) -> int:
        return sum(w for _, w in self.in_edges)


def classify(node: NodeView, ctx: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """
    返回 (主标签, 命中的全部标签, 判定依据)。多标签时按 基石 > 桥梁 > 衍生 排优先级，
    但 `tags` 全留着——一篇论文既可以是基石也是桥梁，只报一个会丢信息。
    """
    total = max(ctx.get("library_count", 1), 1)
    pr_rank = node.metrics.get("pagerank_rank") or total
    bc_rank = node.metrics.get("betweenness_rank") or total
    bc = node.metrics.get("betweenness") or 0.0
    pr = node.metrics.get("pagerank") or 0.0

    tags: list[str] = []
    reasons: list[str] = []

    if node.kind == "gap":
        citers = len({u for u, _ in node.in_edges})
        tags.append("gap")
        reasons.append(f"库内 {citers} 篇引用了它，但它不在库里")
        return "gap", tags, reasons

    if node.in_degree == 0 and node.out_degree == 0:
        tags.append("isolated")
        reasons.append("与库内任何论文都没有引用边")
        return "isolated", tags, reasons

    # 「排名前 25%」在小图上会被并列第一骗过去：一个 10 节点图里有 6 个悬挂节点时，
    # 它们分数完全相同、并列第 1，全部满足排名条件。再加一条绝对量约束——
    # 至少是均匀分布（1/N）的 1.5 倍，才算"分数集中在它身上"。
    cornerstone_cut = max(1, int(round(total * 0.25)))
    dominant = pr >= 1.5 / total
    if pr_rank <= cornerstone_cut and node.in_degree >= 2 and dominant:
        tags.append("cornerstone")
        reasons.append(
            f"PageRank {pr:.3f}（第 {pr_rank}/{total}，均匀分布为 {1 / total:.3f}），"
            f"库内被引 {node.in_degree} 次"
        )

    # 桥梁必须是"中间"：只有出边（源头）或只有入边（汇聚）都不算中转
    if bc_rank == 1 and bc >= 0.15 and node.in_degree >= 1 and node.out_degree >= 1:
        tags.append("bridge")
        reasons.append(f"betweenness {bc:.3f} 排第 1，是两簇之间唯一的中转")

    if node.out_degree >= 2 and node.in_degree <= 1:
        tags.append("derivative")
        reasons.append(f"引用库内 {node.out_degree} 篇、被库内引用 {node.in_degree} 篇")

    if node.in_degree >= 2 and pr_rank > total * 0.6 and "cornerstone" not in tags:
        tags.append("cited_but_peripheral")
        reasons.append(
            f"被引 {node.in_degree} 次但 PageRank 只排第 {pr_rank}/{total}：引用它的论文本身靠后"
        )

    if not tags:
        tags.append("normal")
        reasons.append(
            f"被引 {node.in_degree} 次、引用 {node.out_degree} 篇，PageRank 第 {pr_rank}/{total}"
        )

    primary = next((t for t in ("cornerstone", "bridge", "derivative", "cited_but_peripheral")
                    if t in tags), tags[0])
    return primary, tags, reasons


def sentence(node: NodeView, label: str, ctx: dict[str, Any]) -> str:
    """一句带数字的解读。每类一句，句式固定，方便对照。"""
    title_of = ctx.get("title_of", {})
    total = max(ctx.get("library_count", 1), 1)
    pr = node.metrics.get("pagerank") or 0.0
    pr_rank = node.metrics.get("pagerank_rank") or total
    bc = node.metrics.get("betweenness") or 0.0

    citers = [title_of.get(u, u) for u, _ in node.in_edges]
    cited = [title_of.get(v, v) for v, _ in node.out_edges]

    if label == "cornerstone":
        who = _names(citers) or "库内论文"
        return (
            f"{who}都以它为前置，PageRank {pr:.3f} 排第 {pr_rank}/{total}"
            f"、库内被引 {node.in_degree} 次 —— 读这个方向先读它。"
        )
    if label == "bridge":
        left = _names(citers, 2)
        right = _names(cited, 2)
        if left and right:
            return (
                f"它是 {left} 与 {right} 之间的中转，betweenness {bc:.3f} 排第 1"
                f"（PageRank 第 {pr_rank}/{total}）—— 跳过它会漏掉两条引用链的交汇。"
            )
        return (
            f"它把{right or '多篇论文'}串在同一条上游上，betweenness {bc:.3f} 排第 1"
            f"（PageRank 第 {pr_rank}/{total}）—— 这个位置的连接作用比它的被引次数更值得注意。"
        )
    if label == "derivative":
        who = _names(cited) or "库内多篇"
        return (
            f"它引用了{who}、却没有被库内任何论文引用 —— 属于沿用既有工作，"
            f"读它之前先把它引的那几篇读掉。"
        )
    if label == "cited_but_peripheral":
        who = _names(citers) or "库内论文"
        return (
            f"库内被引 {node.in_degree} 次看着不少，但引用它的是{who}，"
            f"PageRank 只排第 {pr_rank}/{total} —— 次数多不等于关键。"
        )
    if label == "gap":
        who = _names(citers)
        return (
            f"库内 {who}都引用了它，但库里没有这篇 —— 补进来能一次连上 "
            f"{node.in_degree} 条边，是当前性价比最高的补库目标。"
        )
    if label == "isolated":
        return (
            "库内既没有引用它、也没有被它引用 —— 可能是刚上传还没解析出参考文献，"
            "也可能是这个方向确实独立。"
        )
    if node.in_degree == 0:
        return (
            f"它引用了库内 {node.out_degree} 篇、没有被库内引用，PageRank 第 {pr_rank}/{total}"
            f"、betweenness {bc:.3f} —— 库内的上游输入，位于引用链的起点。"
        )
    if node.out_degree == 0:
        return (
            f"它被库内 {node.in_degree} 篇引用、自己不引用库内论文，PageRank 第 {pr_rank}/{total}"
            f"（均匀分布为 {1 / total:.3f}）—— 是汇聚点，但集中度还不足以称基石。"
        )
    return (
        f"被库内引用 {node.in_degree} 次、引用库内 {node.out_degree} 篇，"
        f"PageRank 第 {pr_rank}/{total}、betweenness {bc:.3f} —— 处在引用链中间，但不在瓶颈位置。"
    )


def recommend(node: NodeView, ctx: dict[str, Any], *, limit: int = 2) -> list[dict[str, Any]]:
    """
    一跳推荐：先给"前置"（它引的，按 PageRank 排），再给"后续"（引它的）。
    顺序固定：读者按引用方向读，从被引的往引用者读，才不会读到一半缺前提。
    """
    title_of = ctx.get("title_of", {})
    pr = ctx.get("pagerank", {})
    out: list[dict[str, Any]] = []

    for target, weight in sorted(node.out_edges, key=lambda e: (-pr.get(e[0], 0.0), e[0]))[:limit]:
        out.append(
            {
                "paper_id": target,
                "title": title_of.get(target, target),
                "relation": "cites",
                "why": f"它引用了这篇（正文提及 {weight} 次），是本节点的前置",
                "score": round(pr.get(target, 0.0), 4),
            }
        )

    for source, weight in sorted(node.in_edges, key=lambda e: (-pr.get(e[0], 0.0), e[0]))[:limit]:
        out.append(
            {
                "paper_id": source,
                "title": title_of.get(source, source),
                "relation": "cited_by",
                "why": f"这篇引用了它（正文提及 {weight} 次），是后续工作",
                "score": round(pr.get(source, 0.0), 4),
            }
        )
    return out
