"""
图指标：PageRank、betweenness、入/出度。

口径都写在 `METHODS` 里，接口把它原样返回给前端——指标必须能被读者复核，
否则"基石论文"这类标签就只是一句无法验证的断言。

两个刻意的取舍：
- **PageRank 与入度分开算、分开展示。** 入度是"库内被引几次"，受收录范围影响；
  PageRank 还看"被谁引"——被一篇高权重论文引用，胜过被十篇边缘论文引用。
  两者经常一致，但一致不代表等价；不一致的时候恰恰是最有信息量的时候。
- **betweenness 在无向化之后算。** 引用图是 DAG，直接算有向最短路的话大量节点
  互相不可达，betweenness 会大面积归零。这里关心的是"结构上是否绕不过去"，
  无向化后才有意义；口径里写清楚。
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Iterable, Sequence

DAMPING = 0.85
MAX_ITER = 200
TOL = 1e-10


def _adjacency(nodes: Sequence[str], edges: Sequence[tuple[str, str]]) -> tuple[list[set[str]], list[set[str]]]:
    idx = {n: i for i, n in enumerate(nodes)}
    out = [set() for _ in nodes]
    inn = [set() for _ in nodes]
    for u, v in edges:
        if u not in idx or v not in idx or u == v:
            continue
        out[idx[u]].add(v)
        inn[idx[v]].add(u)
    return out, inn


def pagerank(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
    damping: float = DAMPING,
    max_iter: int = MAX_ITER,
    tol: float = TOL,
) -> tuple[dict[str, float], dict[str, Any]]:
    """
    标准 PageRank（幂迭代）。边方向是「引用者 → 被引者」，所以分数高的论文
    是被重要论文反复引用的那篇。

    悬挂节点（入边有、出边无）的分数会被均匀分回全体，而不是凭空消失：
    不这么处理的话，引用图里最常见的形态（被引很多、自己不引库内任何论文）
    会让总权重越迭代越少，最后分数全塌到 0。
    """
    n = len(nodes)
    if n == 0:
        return {}, {"iterations": 0, "converged": True, "dangling": 0}
    idx = {v: i for i, v in enumerate(nodes)}
    out, inn = _adjacency(nodes, edges)
    dangling = [i for i in range(n) if not out[i]]

    pr = [1.0 / n] * n
    iterations = 0
    converged = False
    for iterations in range(1, max_iter + 1):
        dangling_mass = sum(pr[i] for i in dangling)
        base = (1.0 - damping) / n + damping * dangling_mass / n
        nxt = [base] * n
        for i in range(n):
            if not out[i]:
                continue
            share = damping * pr[i] / len(out[i])
            for target in out[i]:
                nxt[idx[target]] += share
        delta = sum(abs(a - b) for a, b in zip(nxt, pr))
        pr = nxt
        if delta < tol:
            converged = True
            break

    return (
        {nodes[i]: round(pr[i], 6) for i in range(n)},
        {
            "iterations": iterations,
            "converged": converged,
            "damping": damping,
            "dangling": len(dangling),
            "nodes": n,
            "edges": sum(len(s) for s in out),
        },
    )


def betweenness(nodes: Sequence[str], edges: Sequence[tuple[str, str]]) -> dict[str, float]:
    """
    Brandes 算法，无向图。返回值是「经过该节点的最短路占比」的归一化分数（0~1）。
    """
    n = len(nodes)
    if n < 3:
        return {v: 0.0 for v in nodes}
    idx = {v: i for i, v in enumerate(nodes)}
    adj: list[set[int]] = [set() for _ in nodes]
    for u, v in edges:
        if u not in idx or v not in idx or u == v:
            continue
        adj[idx[u]].add(idx[v])
        adj[idx[v]].add(idx[u])

    cb = [0.0] * n
    for s in range(n):
        stack: list[int] = []
        preds: list[list[int]] = [[] for _ in range(n)]
        sigma = [0.0] * n
        sigma[s] = 1.0
        dist = [-1] * n
        dist[s] = 0
        queue = deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = [0.0] * n
        while stack:
            w = stack.pop()
            for v in preds[w]:
                if sigma[w]:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                cb[w] += delta[w]

    # Brandes 的 cb 把 (s,t) 与 (t,s) 各数一次，先除以 2 得到"无序对"口径；
    # 无向图里一个节点最多落在 (n-1)(n-2)/2 条最短路中间，所以归一化因子取 2/((n-1)(n-2))。
    scale = 2.0 / ((n - 1) * (n - 2)) if n > 2 else 0.0
    return {nodes[i]: round(cb[i] / 2 * scale, 6) for i in range(n)}


def degrees(nodes: Sequence[str], edges: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    out = {v: 0 for v in nodes}
    inn = {v: 0 for v in nodes}
    for u, v in edges:
        if u in out and v in out and u != v:
            out[u] += 1
            inn[v] += 1
    return {v: {"in": inn[v], "out": out[v], "total": inn[v] + out[v]} for v in nodes}


def rank_desc(scores: dict[str, float]) -> dict[str, int]:
    """1 起的排名（同分并排，按 id 稳定排序，保证同一份数据两次结果一致）。"""
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    out: dict[str, int] = {}
    prev: float | None = None
    prev_rank = 0
    for i, (node, score) in enumerate(ordered, start=1):
        if prev is not None and abs(score - prev) < 1e-9:
            out[node] = prev_rank
        else:
            out[node] = i
            prev_rank = i
        prev = score
    return out


def normalized(scores: dict[str, float]) -> dict[str, float]:
    """除最大值，用于视觉编码（半径/颜色）；不改变排序，只做尺度统一。"""
    if not scores:
        return {}
    top = max(scores.values()) or 1.0
    return {k: round(v / top, 4) for k, v in scores.items()}


def compute(
    nodes: Sequence[str], edges: Sequence[tuple[str, str]]
) -> dict[str, Any]:
    """一次算完三个指标，返回统一结构，供节点组装与前端指标卡共用。"""
    pr, pr_info = pagerank(nodes, edges)
    bc = betweenness(nodes, edges)
    deg = degrees(nodes, edges)
    return {
        "pagerank": pr,
        "betweenness": bc,
        "degree": deg,
        "pagerank_rank": rank_desc(pr),
        "betweenness_rank": rank_desc(bc),
        "pagerank_norm": normalized(pr),
        "betweenness_norm": normalized(bc),
        "info": pr_info,
    }


METHODS: dict[str, dict[str, Any]] = {
    "pagerank": {
        "name": "PageRank",
        "formula": "PR(i) = (1-d)/N + d · Σ_{j→i} PR(j)/outdeg(j) + d·D/N",
        "params": {"d": DAMPING, "收敛阈值": TOL, "最大迭代": MAX_ITER},
        "scope": "只在库内论文子图上计算；库外节点不参与",
        "reads": "被谁引用：被高权重论文引用一次，强于被边缘论文引用多次",
        "caveat": "悬挂节点（库内没有引用别人）的权重会均分回全体，所以分数是相对值，不能跨库比较",
    },
    "betweenness": {
        "name": "Betweenness",
        "formula": "BC(v) = Σ_{s≠v≠t} σ_st(v) / σ_st ÷ [(N-1)(N-2)]",
        "params": {"算法": "Brandes", "图": "无向化（引用图是 DAG，有向最短路大面积不可达）"},
        "scope": "只在库内论文子图上计算",
        "reads": "结构上绕不过去：删掉它，两簇论文之间就没有通路了",
        "caveat": "节点少时单点分数会被放大，需要配合入度一起看",
    },
    "indegree": {
        "name": "库内被引次数",
        "formula": "indeg(v) = |{u : u→v}|",
        "params": {"计数": "按引用者去重，不按提及次数"},
        "scope": "只看这个库；库外被引（Google Scholar 等）不计入",
        "reads": "只在这个库里有多热 —— 换一个库，数字就变",
        "caveat": "受收录范围影响极大：库里只收了 5 篇，被引 3 次就是第一。它不等于学术影响力，也不等于重要性",
    },
    "weight": {
        "name": "边权（引用强度）",
        "formula": "weight = 正文提及该文献的次数\n（[12] 出现几次 / (Vaswani et al., 2017) 出现几次）",
        "params": {"缺省": "正文标记缺失时记 1，并标记 weight_source=ref_only"},
        "scope": "按「引用者版本 → 被引论文」聚合，同一目标多条文献条目取最大匹配分",
        "reads": "真的在用，还是只在 Related Work 里点了一下名",
        "caveat": "提及次数依赖 PDF 里引用标记的可解析性；作者-年份格式的文献只有姓氏+年份能对上时才计次",
    },
}
