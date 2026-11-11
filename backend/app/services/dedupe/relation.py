"""
关系判定：先回答"是什么关系"，再决定提示什么、给哪些操作。

为什么不直接用阈值：
- 高相似 ≠ 重复。同一篇的 v1 和 v7 正文重合 84%，该提示"新版本"；
  同一批作者的另一篇论文也可能重合 40%（复用方法章节），那是"不同工作"；
  两篇都在讲注意力机制、摘要余弦 0.9 的论文，正文重合只有 2%，那是"相关"。
  这三件事在单一相似度轴上分不开，必须有第二层。
- 反过来，低相似也可能是重复：把方法章节整段搬走、摘要重写的"洗稿"，
  整篇重合率会掉到 0.4 以下，但段落级命中仍然很集中。

所以判两层：
    正文层（body_overlap / para_ratio）  →  是不是同一份稿子
    工作层（title / authors / abstract） →  是不是同一项研究
两个轴的四象限对应三种关系，剩下的落到"无关"（不打扰）。

置信度与依据必须一起给：`confidence` 是给界面的排序权重，`reasons` 是给人看的
"为什么这么判"。只给一个 0.93 而不给依据，用户没法反驳，也就没法纠正。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.dedupe.similarity import Similarity

VERSION_OF = "version_of"
POSSIBLE_DUPLICATE = "possible_duplicate"
RELATED = "related"
DISTINCT = "distinct"

RELATION_LABEL = {
    VERSION_OF: "同一工作的不同版本",
    POSSIBLE_DUPLICATE: "不同工作，正文高度重合",
    RELATED: "同主题相关",
    DISTINCT: "无显著关系",
}

# 三个轴上的档位。数字来自 10 组真实论文对的实测（见 docs/2.14）：
#   同一工作的 v1/v7      body 0.84~0.89   para 0.79   abs 0.95+   title 1.00
#   同一工作的重新排版     body 0.97+        para 0.93+  abs 0.99+
#   不同论文（同领域）     body 0.01~0.03   para 0~0.005 abs 0.55~0.88
BODY_SAME = 0.55          # 正文重合到这个程度：基本可以认定是同一份稿子
BODY_PARTIAL = 0.25       # 大幅改版 / 部分章节复用
WORK_SAME = 0.55          # 工作同一性分数
WORK_STRONG = 0.70
ABS_RELATED = 0.82        # 摘要余弦：同主题
LEAD_RELATED = 0.85


@dataclass
class Verdict:
    relation: str = DISTINCT
    kind: str = ""                      # retypeset | revision | major_revision | same_body_other_meta | partial_overlap | same_topic
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    work_score: float = 0.0
    body_overlap: float = 0.0
    para_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "relation_label": RELATION_LABEL.get(self.relation, self.relation),
            "kind": self.kind,
            "confidence": round(self.confidence, 4),
            "reasons": self.reasons,
            "caveats": self.caveats,
            "work_score": round(self.work_score, 4),
            "body_overlap": round(self.body_overlap, 4),
            "para_ratio": round(self.para_ratio, 4),
        }


def _renormalized(pairs: list[tuple[float | None, float]]) -> float:
    """按可用项重新归一化：元数据缺失时不让它把整体分数拉成 0。"""
    total = sum(w for v, w in pairs if v is not None)
    if total <= 0:
        return 0.0
    return sum(v * w for v, w in pairs if v is not None) / total


def work_score(sim: Similarity) -> float:
    """
    工作同一性：标题 0.40 / 作者 0.25 / 摘要 0.25 / 首段 0.10。

    摘要权重不低，是因为"同一工作改版"最稳的信号就是摘要几乎没变；
    但它是语义分，会被同领域写作套路抬高，所以不能单独当判据——
    必须和正文层一起看（见 classify 的规则）。
    """
    return _renormalized(
        [
            (sim.title_sim, 0.40),
            (sim.author_sim, 0.25),
            (sim.abstract_cos, 0.25),
            (sim.lead_cos, 0.10),
        ]
    )


def classify(sim: Similarity) -> Verdict:
    ws = work_score(sim)
    body = min(sim.body_overlap_new, sim.body_overlap_peer)
    body_max = max(sim.body_overlap_new, sim.body_overlap_peer)
    para = max(sim.para_ratio_new, sim.para_ratio_peer)
    abs_cos = sim.abstract_cos if sim.abstract_cos is not None else 0.0
    lead_cos = sim.lead_cos if sim.lead_cos is not None else 0.0

    v = Verdict(work_score=ws, body_overlap=body, para_ratio=para)
    v.reasons.append(
        f"正文 3-gram 集合重合：本次 {sim.body_overlap_new:.0%} / 库内 {sim.body_overlap_peer:.0%}"
        f"（Jaccard {sim.jaccard:.2f}）"
    )
    v.reasons.append(
        f"相似段落 {sim.para_hit_new}/{sim.para_total_new} 段（本次视角，容差 12/64），"
        f"反向 {sim.para_hit_peer}/{sim.para_total_peer} 段"
    )
    if sim.title_sim is not None:
        v.reasons.append(f"标题相似度 {sim.title_sim:.2f}")
    if sim.author_sim is not None:
        v.reasons.append(f"作者重合度 {sim.author_sim:.2f}")
    if sim.abstract_cos is not None:
        v.reasons.append(f"摘要语义相似度 {sim.abstract_cos:.2f}")

    if sim.title_sim is None:
        v.caveats.append("本次上传没抽出标题（可能是扫描件或版式异常），标题这一票不参与判定")
    if sim.author_sim is None:
        v.caveats.append("作者信息缺失，作者这一票不参与判定")
    if sim.abstract_cos is None:
        v.caveats.append("语义模型不可用，摘要相似度缺省，判定只用文本证据")

    # ---------- 第一象限：正文高度重合 ----------
    if body >= BODY_SAME:
        if ws >= WORK_SAME or (sim.title_sim or 0) >= 0.72 or (sim.author_sim or 0) >= 0.60 or abs_cos >= 0.93:
            if body >= 0.90 and para >= 0.85:
                v.kind = "retypeset"
                v.confidence = 0.90 + 0.08 * min(1.0, para)
                v.reasons.append("正文几乎逐段一致，差异在版式/编号层面——判为同一版本的重新排版")
            else:
                v.kind = "revision"
                v.confidence = 0.55 + 0.28 * body + 0.10 * abs_cos + 0.07 * max(sim.title_sim or 0, sim.author_sim or 0)
                changed = sim.para_total_new - sim.para_hit_new
                v.reasons.append(
                    f"同一份正文的改版：{changed} 段改动或新增（占比 {changed / max(1, sim.para_total_new):.0%}）"
                )
            v.relation = VERSION_OF
            v.confidence = min(0.99, v.confidence)
            return v
        # 正文几乎一样、但标题作者都不一样：换题重复发表 / 预印本与正式版改名
        v.relation = POSSIBLE_DUPLICATE
        v.kind = "same_body_other_meta"
        v.confidence = 0.50 + 0.30 * body + 0.20 * (1.0 - ws)
        v.reasons.append("正文重合很高，但标题/作者对不上——更像换个题目的重复发表，而不是同一工作的版本")
        return v

    # ---------- 第二象限：正文中等重合 + 工作同一 ----------
    if body >= BODY_PARTIAL and ws >= WORK_STRONG and abs_cos >= 0.80:
        v.relation = VERSION_OF
        v.kind = "major_revision"
        v.confidence = 0.45 + 0.30 * body + 0.15 * abs_cos
        v.reasons.append("同一工作的大幅改版：正文只保留了一部分，但标题/作者/摘要一致")
        return v

    # ---------- 第三象限：正文中等重合但工作不同 ----------
    if body >= BODY_PARTIAL or (body_max >= BODY_SAME and ws < WORK_SAME):
        v.relation = POSSIBLE_DUPLICATE
        v.kind = "partial_overlap"
        v.confidence = 0.45 + 0.25 * body + 0.20 * para
        v.reasons.append("有相当篇幅重合但方向不同（可能是复用了方法/相关工作章节），提示并存核对")
        return v

    # ---------- 第四象限：正文几乎不重合，只能谈主题 ----------
    if abs_cos >= ABS_RELATED or lead_cos >= LEAD_RELATED or para >= 0.12:
        v.relation = RELATED
        v.kind = "same_topic"
        v.confidence = min(0.90, 0.35 + 0.35 * max(abs_cos, lead_cos) + 0.20 * para)
        v.reasons.append("正文几乎不重合，只是主题相近——按「仅关联」处理，不影响入库")
        return v

    v.relation = DISTINCT
    v.kind = ""
    v.confidence = 0.0
    return v


def version_hint(sim: Similarity, new_label: str, peer_label: str) -> str:
    """
    版本方向只按**用户声明的版本号**给，不猜。

    从内容推测"谁更新"是可以做的（页数、章节数、数字修订），但那只能给概率，
    而这个决定会影响"哪个版本成为当前版本"——猜错比不给更糟。
    """
    if not new_label or not peer_label:
        return ""
    if sim.direction == "newer":
        return f"库内已是 {peer_label}，本次是 {new_label}：内容较新"
    if sim.direction == "older":
        return f"库内已是 {peer_label}，本次是 {new_label}：内容更早"
    if sim.direction == "same":
        return f"与库内同为 {peer_label}：同一版本的另一份文件"
    return f"库内 {peer_label}，本次 {new_label}：版本号无法比较先后"


def suggest_actions(relation: str) -> list[dict[str, str]]:
    """
    每种关系给三个操作，附一句后果说明。

    操作的**语义按关系变化**，所以文案不能复用：
    - version_of + keep_new  = 挂到同一工作，本次成为当前版本
    - possible_duplicate + keep_new = 合并成同一工作（有风险，所以标灰风险文案）
    - related 只给"正常入库"与"仅关联"，因为没什么可合并的
    """
    if relation == VERSION_OF:
        return [
            {"action": "keep_new", "label": "设为当前版本", "hint": "挂到同一工作下，本次内容成为默认展示版本，旧版本保留可切换"},
            {"action": "keep_both", "label": "两个版本并存", "hint": "都入库；当前版本仍停留在库内已有的那个"},
            {"action": "link_only", "label": "跳过本次", "hint": "只记录这条判断，不解析不入库（省一次解析）"},
        ]
    if relation == POSSIBLE_DUPLICATE:
        return [
            {"action": "keep_both", "label": "作为两篇并存", "hint": "两篇都入库，各自可检索；这是最安全的默认"},
            {"action": "keep_new", "label": "合并为同一工作", "hint": "把本次作为库内那篇的新版本；只在确认是同一份研究时选"},
            {"action": "link_only", "label": "仅标注关联", "hint": "正常入库，只记一条「疑似重复」的关联，不合并"},
        ]
    return [
        {"action": "keep_both", "label": "正常入库", "hint": "作为独立论文入库，不做任何合并"},
        {"action": "link_only", "label": "入库并标注关联", "hint": "独立入库，同时记一条「主题相关」的关联"},
    ]
