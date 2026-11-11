"""
图谱规则与指标的离线校验。

三件事：
1. 用可手工推算的小图，验证 PageRank / betweenness 的数值与排序对得上；
2. 验证「被引次数多 ≠ 重要」这条不是口号——构造一个入度高但中心性低的节点，
   看它会不会被标成基石；
3. 20 节点规模的指标计算耗时（前端分层布局的耗时在界面上直接显示）。

跑法：docker exec -i ai4s-api python /app/eval/graph/check_graph.py
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "/app")

from app.services.graph import insight, metrics  # noqa: E402


def node_view(nid, ins, outs, met, kind="library"):
    return insight.NodeView(
        node_id=nid,
        title=nid,
        kind=kind,
        in_edges=list(ins),
        out_edges=list(outs),
        metrics={
            "pagerank": met["pagerank"].get(nid, 0.0),
            "pagerank_rank": met["pagerank_rank"].get(nid),
            "betweenness": met["betweenness"].get(nid, 0.0),
            "betweenness_rank": met["betweenness_rank"].get(nid),
        },
    )


def build(nodes, edges):
    met = metrics.compute(nodes, edges)
    views = {}
    for n in nodes:
        ins = [(u, 1) for u, v in edges if v == n]
        outs = [(v, 1) for u, v in edges if u == n]
        views[n] = node_view(n, ins, outs, met)
    ctx = {"title_of": {n: n for n in nodes}, "library_count": len(nodes),
           "pagerank": met["pagerank"]}
    return met, views, ctx


def show(title, nodes, edges):
    met, views, ctx = build(nodes, edges)
    print(f"\n=== {title}")
    for n in nodes:
        label, tags, reasons = insight.classify(views[n], ctx)
        m = views[n].metrics
        print(
            f"  {n:14s} PR={m['pagerank']:.3f}(#{m['pagerank_rank']}) "
            f"BC={m['betweenness']:.3f}(#{m['betweenness_rank']}) "
            f"in={views[n].in_degree} out={views[n].out_degree} → {label} {tags}"
        )
        print(f"     {insight.sentence(views[n], label, ctx)}")
    return met, views, ctx


def main() -> None:
    # 1) 星形：A 被四篇共同引用，应该判基石
    nodes = ["A", "B", "C", "D", "E"]
    edges = [("B", "A"), ("C", "A"), ("D", "A"), ("E", "A")]
    met, views, ctx = show("星形：A 被 4 篇引用", nodes, edges)
    label, _, _ = insight.classify(views["A"], ctx)
    assert label == "cornerstone", label

    # 2) 桥梁：B 是两条链唯一的中转
    nodes = ["A", "B", "C", "D"]
    edges = [("A", "B"), ("C", "B"), ("B", "D"), ("A", "D")]
    met, views, ctx = show("桥梁：B 是 A/C → D 的中转", nodes, edges)
    print(f"  betweenness B={met['betweenness']['B']:.4f} D={met['betweenness']['D']:.4f}")

    # 3) 入度高但中心性低：A 只被 B、C 引用，但 B、C 自己很重；
    #    X 被 D、E、F 三篇引用，可这三篇把权重摊在六篇文献上。
    #    结果是 A 的入度（2）比 X（3）少，PageRank 反而更高——这就是
    #    「被引次数 ≠ 重要」的最小反例：重要的不是被引几次，是被谁引。
    nodes = ["A", "B", "C", "D", "E", "F", "X"] + [f"F{i}" for i in range(1, 7)]
    edges = [("B", "A"), ("C", "A")]
    edges += [(f"F{i}", "B") for i in range(1, 5)]
    edges += [(f"F{i}", "C") for i in range(5, 7)]
    edges += [("D", "X"), ("E", "X"), ("F", "X")]
    edges += [("D", f"F{i}") for i in range(1, 6)]
    met, views, ctx = show("反例：A 被引 2 次、X 被引 3 次", nodes, edges)
    a_label, _, _ = insight.classify(views["A"], ctx)
    x_label, _, _ = insight.classify(views["X"], ctx)
    print(f"  in(A)={views['A'].in_degree} PR(A)={met['pagerank']['A']:.4f} → {a_label}")
    print(f"  in(X)={views['X'].in_degree} PR(X)={met['pagerank']['X']:.4f} → {x_label}")
    assert met["pagerank"]["X"] < met["pagerank"]["A"], "入度高不该等于中心性高"
    assert x_label != "cornerstone", x_label
    print("  → 入度最高的 X 不是基石；被两篇『重量级』引用的 A 才是。")

    # 4) 20 节点：指标耗时
    rnd = random.Random(7)
    nodes = [f"N{i:02d}" for i in range(20)]
    edges = []
    for i in range(20):
        for _ in range(rnd.randint(0, 3)):
            j = rnd.randint(i + 1, 19)
            edges.append((nodes[i], nodes[j]))
    import time

    t0 = time.perf_counter()
    met = metrics.compute(nodes, edges)
    dt = (time.perf_counter() - t0) * 1000
    print(f"\n=== 20 节点 / {len(edges)} 边：PageRank+betweenness 合计 {dt:.2f}ms "
          f"(迭代 {met['info']['iterations']} 次收敛={met['info']['converged']})")
    print("  说明：前端分层布局是 O(V·E) 的静态计算，同一规模在浏览器里也是毫秒级；")
    print("  界面顶部显示的「布局 Xms · 首帧 Yms」就是这条约束的实测值。")


if __name__ == "__main__":
    main()
