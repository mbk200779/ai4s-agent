"""
库内引用图谱：编排抽取 → 匹配 → 指标 → 解读，产出一次请求就能渲染完的数据。

与 `services/graph_service.py` 的区别：
那个服务是「以一篇论文为中心、实时问 OpenAlex 要引用网络」，回答的是
"外面谁引了它"；这个服务只读**库里已有的**参考文献与正文标记，回答的是
"我收的这批论文之间是什么关系"。两者数据源不同、问题不同，所以不合并。

每次 build 都现算，不落缓存：库小的时候（十几篇）整条链路是几十毫秒，
缓存只会带来"图上的数字和刚上传的论文对不上"这类问题。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Sequence

from app.db import utc_now
from app.services.graph import insight, markers, metrics, store
from app.services.graph import match as match_mod


def _gap_id(key: str) -> str:
    return "gap_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _short(text: str, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quality(refs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """
    条目字段的完整度。要和匹配失败率一起看：条目本身切歪了，
    失败率高是抽取的账，不是匹配的账。
    """
    total = len(refs)
    if not total:
        return {"total": 0, "score": 0.0}
    with_year = sum(1 for r in refs if r.get("parsed_year"))
    with_authors = sum(1 for r in refs if r.get("authors"))
    with_title = sum(
        1 for r in refs if len(r.get("parsed_title") or "") >= 20
        and not (r.get("parsed_title") or "").strip().isdigit()
    )
    return {
        "total": total,
        "with_year": with_year,
        "with_authors": with_authors,
        "with_title": with_title,
        "score": round((with_year + with_authors + with_title) / (total * 3), 4),
    }


EMPTY_STATES: dict[str, dict[str, Any]] = {
    "no_papers": {
        "title": "这个课题还没有论文",
        "hint": "先上传几篇 PDF。图谱的边来自参考文献与正文引用标记，没有论文就没有边。",
        "action": "去上传",
    },
    "no_references": {
        "title": "参考文献还没解析出来",
        "hint": (
            "论文已入库，但没有抽到参考文献条目，所以没有边可画。"
            "常见原因：PDF 里 References 是图片、章节标题被排版成图、或者解析时模型失败退化了。"
        ),
        "action": "重抽参考文献",
    },
    "no_edges": {
        "title": "库内论文之间没有引用关系",
        "hint": (
            "参考文献抽到了，但没有任何一条指向库内其它论文。"
            "要么这个库收的论文彼此不引用，要么它们的引用落在库外——"
            "这时把库外被引最多的那几篇补进来，图才会连起来。"
        ),
        "action": "看补库建议",
    },
}


class LibraryGraphService:
    """一次 build = 一次完整的「库内引用图谱」快照。"""

    def build(
        self,
        project_id: str,
        *,
        include_gaps: bool = True,
        max_gaps: int = 8,
        min_gap_citers: int = 2,
        persist_matches: bool = False,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        papers = store.library_papers(project_id)
        if not papers:
            return self._payload(project_id, t0, empty="no_papers")

        candidates = match_mod.build_candidates(papers)
        title_of = {p["id"]: (p.get("title") or p["id"]) for p in papers}

        edge_map: dict[tuple[str, str], dict[str, Any]] = {}
        gap_pool: dict[str, dict[str, Any]] = {}
        fail_pool: list[dict[str, Any]] = []

        total_refs = 0
        total_markers = 0
        unresolved_markers = 0
        by_method: dict[str, int] = {}
        matched_total = 0
        versions: list[dict[str, Any]] = []
        paper_ref_stats: dict[str, dict[str, Any]] = {}
        low_quality: list[str] = []
        quality_acc = {"total": 0, "with_year": 0, "with_authors": 0, "with_title": 0}

        for paper in papers:
            pid = paper["id"]
            version_id = paper.get("version_id")
            row = {"paper_id": pid, "title": title_of[pid], "version_id": version_id,
                   "version_label": paper.get("version_label"), "refs": 0,
                   "matched": 0, "markers": 0, "quality": {"total": 0, "score": 0.0}}
            versions.append(row)
            if not version_id:
                continue

            refs = store.refs_of(version_id)
            row["refs"] = len(refs)
            total_refs += len(refs)
            if not refs:
                continue

            q = _quality(refs)
            row["quality"] = q
            for key in quality_acc:
                quality_acc[key] += int(q.get(key) or 0)
            if q["score"] < 0.6:
                low_quality.append(title_of[pid])

            chunks = store.body_chunks(version_id)
            hits, mstats = markers.outline(chunks, refs)
            row["markers"] = mstats["resolved"]
            total_markers += mstats["resolved"]
            unresolved_markers += mstats["unresolved"]

            results, stats = match_mod.match_refs(refs, candidates, self_paper_id=pid)
            row["matched"] = stats["matched"]
            matched_total += stats["matched"]
            for key, val in stats["by_method"].items():
                by_method[key] = by_method.get(key, 0) + val
            paper_ref_stats[pid] = {"refs": len(refs), "matched": stats["matched"]}
            for f in stats["failures"]:
                fail_pool.append({**f, "paper": title_of[pid]})

            persisted: list[tuple[int, str, float, str]] = []
            for ref, outcome in results:
                ref_index = int(ref["ref_index"])
                hit = hits.get(ref_index) or {}
                weight = int(hit.get("count") or 0)
                weight_source = "markers" if weight > 0 else "ref_only"
                weight = max(1, weight)

                if outcome.matched and outcome.paper_id:
                    key = (pid, outcome.paper_id)
                    edge = edge_map.get(key)
                    if edge is None:
                        edge = {
                            "source": pid,
                            "target": outcome.paper_id,
                            "weight": 0,
                            "relation": "cites",
                            "refs": [],
                            "match_score": 0.0,
                            "match_method": outcome.method,
                            "weight_source": weight_source,
                            "sentence": hit.get("sentence") or "",
                            "page": hit.get("page"),
                            "mentions": 0,
                        }
                        edge_map[key] = edge
                    edge["weight"] += weight
                    edge["mentions"] += weight
                    if outcome.score > edge["match_score"]:
                        edge["match_score"] = outcome.score
                        edge["match_method"] = outcome.method
                    if len(edge["refs"]) < 3:
                        edge["refs"].append(
                            {
                                "ref_index": ref_index,
                                "raw": _short(ref.get("raw_text"), 160),
                                "score": round(outcome.score, 3),
                                "method": outcome.method,
                            }
                        )
                    if not edge["sentence"] and hit.get("sentence"):
                        edge["sentence"] = hit["sentence"]
                        edge["page"] = hit.get("page")
                    if persist_matches:
                        persisted.append(
                            (ref_index, outcome.paper_id, outcome.score, outcome.method)
                        )
                else:
                    if not include_gaps:
                        continue
                    gkey = (match_mod.normalize(ref.get("parsed_title") or "") or
                            match_mod.normalize(ref.get("raw_text") or ""))[:90]
                    if len(gkey) < 12:
                        continue
                    gkey = f"{gkey}#{ref.get('parsed_year') or ''}"
                    slot = gap_pool.setdefault(
                        gkey,
                        {
                            "key": gkey,
                            "title": _short(ref.get("parsed_title") or ref.get("raw_text") or "", 120),
                            "raw": _short(ref.get("raw_text") or "", 180),
                            "year": ref.get("parsed_year"),
                            "authors": (ref.get("authors") or [])[:3],
                            "citers": {},
                            "mentions": 0,
                            "best_score": 0.0,
                        },
                    )
                    slot["citers"][pid] = slot["citers"].get(pid, 0) + weight
                    slot["mentions"] += weight
                    slot["best_score"] = max(slot["best_score"], outcome.score)
                    if not slot.get("year") and ref.get("parsed_year"):
                        slot["year"] = ref["parsed_year"]

            if persisted:
                store.save_matches(version_id, persisted)

        # ---------- 指标只在库内子图上算 ----------
        library_ids = [p["id"] for p in papers]
        library_set = set(library_ids)
        lib_edges = [
            (e["source"], e["target"]) for e in edge_map.values() if e["target"] in library_set
        ]
        met = metrics.compute(library_ids, lib_edges)
        pr_all = met["pagerank"]
        ctx: dict[str, Any] = {
            "title_of": title_of,
            "library_count": len(library_ids),
            "pagerank": pr_all,
        }

        # ---------- 库内节点 ----------
        nodes: list[dict[str, Any]] = []
        for paper in papers:
            pid = paper["id"]
            outs = [
                (e["target"], e["weight"])
                for e in edge_map.values()
                if e["source"] == pid and e["target"] in library_set
            ]
            ins = [
                (e["source"], e["weight"])
                for e in edge_map.values()
                if e["target"] == pid and e["source"] in library_set
            ]
            view = insight.NodeView(
                node_id=pid,
                title=title_of[pid],
                kind="library",
                in_edges=ins,
                out_edges=outs,
                metrics={
                    "pagerank": pr_all.get(pid, 0.0),
                    "pagerank_rank": met["pagerank_rank"].get(pid),
                    "betweenness": met["betweenness"].get(pid, 0.0),
                    "betweenness_rank": met["betweenness_rank"].get(pid),
                },
            )
            label, tags, reasons = insight.classify(view, ctx)
            nodes.append(
                {
                    "id": pid,
                    "kind": "library",
                    "title": title_of[pid],
                    "arxiv_id": paper.get("arxiv_id"),
                    "year": paper.get("year"),
                    "version_label": paper.get("version_label"),
                    "label": {"key": label, **insight.LABELS[label]},
                    "tags": tags,
                    "reasons": reasons,
                    "sentence": insight.sentence(view, label, ctx),
                    "metrics": {
                        "pagerank": view.metrics["pagerank"],
                        "pagerank_rank": view.metrics["pagerank_rank"],
                        "betweenness": view.metrics["betweenness"],
                        "betweenness_rank": view.metrics["betweenness_rank"],
                        "in_degree": view.in_degree,
                        "out_degree": view.out_degree,
                        "mentions_in": view.mentions_in,
                    },
                    "recommendations": insight.recommend(view, ctx),
                    "refs": paper_ref_stats.get(pid, {"refs": 0, "matched": 0}),
                    "cites": [title_of.get(t, t) for t, _ in outs],
                    "cited_by": [title_of.get(s, s) for s, _ in ins],
                }
            )

        # ---------- 库外缺口 ----------
        gap_nodes, gap_edges = self._gaps_to_nodes(
            gap_pool, title_of, max_gaps=max_gaps, min_citers=min_gap_citers
        )
        nodes.extend(gap_nodes)

        edges = [self._edge_payload(e) for e in edge_map.values() if e["target"] in library_set]
        edges.extend(gap_edges)

        stats = {
            "papers": len(papers),
            "versions": sum(1 for v in versions if v.get("version_id")),
            "refs": total_refs,
            "matched": matched_total,
            "rate": round(matched_total / total_refs, 4) if total_refs else 0.0,
            "by_method": by_method,
            "markers_resolved": total_markers,
            "markers_unresolved": unresolved_markers,
            "marker_resolution": (
                round(total_markers / (total_markers + unresolved_markers), 4)
                if (total_markers + unresolved_markers)
                else 0.0
            ),
            "failures": sorted(fail_pool, key=lambda f: -f["best_score"])[:10],
            "quality": {
                **quality_acc,
                "score": round(
                    (quality_acc["with_year"] + quality_acc["with_authors"]
                     + quality_acc["with_title"]) / (quality_acc["total"] * 3),
                    4,
                ) if quality_acc["total"] else 0.0,
            },
            "low_quality_papers": low_quality,
            "by_paper": versions,
        }

        warnings: list[str] = []
        if low_quality:
            warnings.append(
                "参考文献字段完整度偏低：" + "、".join(low_quality[:3])
                + "。这类失败要算在抽取上，不是匹配的问题。"
            )
        if total_refs and stats["rate"] < 0.15:
            warnings.append(
                f"匹配率只有 {stats['rate']:.0%}：库内只有 {len(papers)} 篇，"
                "绝大多数引用指向库外是正常的——缺口节点的被引次数说明了该补哪几篇。"
            )
        empty = None
        if total_refs == 0:
            empty = "no_references"
        elif not edges:
            empty = "no_edges"

        return self._payload(
            project_id,
            t0,
            nodes=nodes,
            edges=edges,
            metrics_payload=met,
            stats=stats,
            warnings=warnings,
            empty=empty,
            scope={
                "include_gaps": include_gaps,
                "max_gaps": max_gaps,
                "min_gap_citers": min_gap_citers,
                "library_nodes": len(library_ids),
                "gap_nodes": len(gap_nodes),
                "library_edges": len(lib_edges),
                "total_edges": len(edges),
            },
        )

    # ---------------- 内部 ----------------

    def _gaps_to_nodes(
        self,
        pool: dict[str, dict[str, Any]],
        title_of: dict[str, str],
        *,
        max_gaps: int,
        min_citers: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        库外缺口排序：先按"被库内几篇引用"，再按提及次数。
        强缺口（≥2 篇）不足 3 个时，用 1 篇的被引条目补位——
        小库（5 篇）里强缺口往往就一两个，全不显示会让图看着像空的。
        """
        strong = [g for g in pool.values() if len(g["citers"]) >= max(1, min_citers)]
        weak = [g for g in pool.values() if g not in strong]
        strong.sort(key=lambda g: (-len(g["citers"]), -g["mentions"], g["key"]))
        weak.sort(key=lambda g: (-g["mentions"], g["key"]))

        chosen = strong[:max_gaps]
        if len(chosen) < 3:
            chosen += weak[: max(0, min(3, max_gaps) - len(chosen))]

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for gap in chosen:
            gid = _gap_id(gap["key"])
            citers = sorted(gap["citers"].items(), key=lambda kv: (-kv[1], kv[0]))
            view = insight.NodeView(
                node_id=gid,
                title=gap["title"],
                kind="gap",
                in_edges=[(p, w) for p, w in citers],
                out_edges=[],
                metrics={},
            )
            label, tags, reasons = insight.classify(view, {})
            nodes.append(
                {
                    "id": gid,
                    "kind": "gap",
                    "title": gap["title"],
                    "year": gap["year"],
                    "authors": gap.get("authors") or [],
                    "label": {"key": label, **insight.LABELS[label]},
                    "tags": tags,
                    "reasons": reasons,
                    "sentence": insight.sentence(view, "gap", {"title_of": title_of}),
                    "metrics": {
                        "pagerank": None,
                        "pagerank_rank": None,
                        "betweenness": None,
                        "betweenness_rank": None,
                        "in_degree": len(citers),
                        "out_degree": 0,
                        "mentions_in": sum(w for _, w in citers),
                    },
                    "recommendations": [
                        {
                            "paper_id": p,
                            "title": title_of.get(p, p),
                            "relation": "cited_by",
                            "why": f"这篇引用了它（提及 {w} 次）",
                            "score": 0.0,
                        }
                        for p, w in citers[:3]
                    ],
                    "refs": {"refs": 1, "matched": 0},
                    "cites": [],
                    "cited_by": [title_of.get(p, p) for p, _ in citers],
                }
            )
            for p, w in citers:
                edges.append(
                    {
                        "source": p,
                        "target": gid,
                        "weight": w,
                        "relation": "cites_gap",
                        "match_score": round(gap["best_score"], 3),
                        "match_method": "unmatched",
                        "weight_source": "markers" if gap["mentions"] else "ref_only",
                        "sentence": "",
                        "page": None,
                        "refs": [{"ref_index": None, "raw": gap["raw"], "score": 0.0,
                                  "method": "unmatched"}],
                    }
                )
        return nodes, [self._edge_payload(e) for e in edges]

    @staticmethod
    def _edge_payload(edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": edge["source"],
            "target": edge["target"],
            "weight": edge["weight"],
            "relation": edge["relation"],
            "match_score": round(edge["match_score"], 3),
            "match_method": edge["match_method"],
            "weight_source": edge["weight_source"],
            "sentence": edge.get("sentence") or "",
            "page": edge.get("page"),
            "refs": edge.get("refs") or [],
        }

    def _payload(
        self,
        project_id: str,
        t0: float,
        *,
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        metrics_payload: dict[str, Any] | None = None,
        stats: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        empty: str | None = None,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nodes = nodes or []
        edges = edges or []
        return {
            "project_id": project_id,
            "generated_at": utc_now(),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "nodes": nodes,
            "edges": edges,
            "metrics": metrics_payload or {},
            "methods": metrics.METHODS,
            "rules": insight.RULES,
            "match_stats": stats or {},
            "warnings": warnings or [],
            "scope": scope or {},
            "empty": EMPTY_STATES.get(empty) if empty else None,
            "empty_reason": empty,
        }


_service: LibraryGraphService | None = None


def get_service() -> LibraryGraphService:
    global _service
    if _service is None:
        _service = LibraryGraphService()
    return _service
