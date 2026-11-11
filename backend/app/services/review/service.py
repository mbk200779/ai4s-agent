"""
综述生成流水线：证据池 → claim 抽取 → 观点聚类 → 冲突检测 → 正文生成 → claim 表。

为什么不能"检索几段 → 让模型写综述"（旧 `review_service.generate` 就是这么干的）：
那种做法产出的是**摘要拼接**——按检索顺序逐段复述，遇到相反结论就写
「研究表明存在差异」把它抹平，读者拿到的既不是观点结构，也不知道哪条结论有几篇文献支持。
本流水线把中间表示显式算出来：
- 观点簇（谁和谁在说同一件事、各自什么立场）
- 冲突边（哪两个簇互斥、为什么互斥、是条件差异还是真矛盾）
- claim 表（每条结论由哪几篇论文的哪几句原文支撑）

可复现性：簇的排序、冲突的排序、证据编号全部是确定性的；
LLM 三个调用都走 `temperature=0` + 固定 prompt 版本；
响应体回显 `config.version / prompt_version / fingerprint`，
任何一次"结果不一样"都能归因到"prompt 变了"还是"阈值变了"。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from app.services.llm_client import LLMClient
from app.services.review import prompts
from app.services.review.claims import extract_claims
from app.services.review.cluster import Cluster, cluster_claims
from app.services.review.conflict import detect_conflicts
from app.services.review.generate import render_markdown, write_sections
from app.services.review.pool import build_pool
from app.services.storage import StorageService


class ReviewPipeline:
    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()
        self.storage = StorageService()

    # ---------------------------------------------------------------- 主流程

    def generate(
        self,
        topic: str,
        arxiv_ids: list[str] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        cfg = prompts.config_block()
        timing: dict[str, float] = {}
        t_all = time.time()
        pool = build_pool(topic, arxiv_ids=arxiv_ids)
        timing["pool"] = round(time.time() - t_all, 2)
        chunks = pool["chunks"]

        if not chunks:
            return {
                "topic": topic,
                "markdown": "",
                "rejected": True,
                "reject_reason": "未检索到可用于综述的正文/表格片段，无法生成。",
                "referenced_arxiv_ids": [],
                "claims": [],
                "conflicts": [],
                "stats": {"clusters": 0, "claims": 0, "conflicts": 0, "papers": 0},
                "config": cfg,
            }

        # 证据编号：chunk_id → 全局 1..N。全篇一套编号，冲突块与正文共用。
        ref_index = {ch["chunk_id"]: i for i, ch in enumerate(chunks, 1)}
        refs = {
            i: {
                "n": i,
                "chunk_id": ch["chunk_id"],
                "arxiv_id": ch["arxiv_id"],
                "paper_id": ch["paper_id"],
                "title": ch["title"],
                "page": ch["page"],
                "section": ch.get("section") or "",
                "kind": ch.get("kind") or "",
                "hit_source": ch.get("hit_source") or "",
            }
            for i, ch in enumerate(chunks, 1)
        }

        t = time.time()
        extracted = extract_claims(topic, chunks, llm=self.llm)
        timing["extract"] = round(time.time() - t, 2)
        claims = extracted.claims
        if not claims:
            return {
                "topic": topic,
                "markdown": "",
                "rejected": True,
                "reject_reason": "未能从片段中抽取到可核对的结论（引文均无法回贴原文），无法生成综述。",
                "referenced_arxiv_ids": sorted({c["arxiv_id"] for c in chunks}),
                "claims": [],
                "conflicts": [],
                "stats": {
                    "clusters": 0,
                    "claims": 0,
                    "conflicts": 0,
                    "papers": len({c["arxiv_id"] for c in chunks}),
                    "dropped": len(extracted.dropped),
                },
                "config": cfg,
                "trace": {"pool_notes": pool["notes"], "dropped": extracted.dropped},
            }

        t = time.time()
        clusters, cluster_diag = cluster_claims(claims, llm=self.llm)
        timing["cluster"] = round(time.time() - t, 2)
        t = time.time()
        edges, conflict_diag = detect_conflicts(clusters, llm=self.llm)
        timing["conflict"] = round(time.time() - t, 2)
        t = time.time()
        written = write_sections(topic, clusters, edges, ref_index, llm=self.llm)
        timing["write"] = round(time.time() - t, 2)

        papers = sorted({c.arxiv_id for c in claims})
        meta = {
            "paper_count": len(papers),
            "claim_count": len(claims),
            "cluster_count": len(clusters),
            "conflict_count": len([e for e in edges if e["confirmed"]]),
            "single_paper": len([c for c in clusters if c.strength == "single_paper"]),
            "prompt_version": prompts.REVIEW_PROMPT_VERSION,
            "config_version": cfg["version"],
            "fingerprint": cfg["fingerprint"],
        }
        markdown = render_markdown(
            topic, clusters, edges, written["sections"], refs, ref_index, meta=meta
        )

        claim_rows = self._claim_rows(clusters, edges, written["sections"], ref_index)
        stats = {
            **meta,
            "raw_claims": len(claims),
            "support": cluster_diag.get("by_stance", {}).get("support", 0),
            "oppose": cluster_diag.get("by_stance", {}).get("oppose", 0),
            "conditional": cluster_diag.get("by_stance", {}).get("conditional", 0),
            "conflicts_confirmed": conflict_diag.get("confirmed", 0),
            "conflicts_pending": conflict_diag.get("pending", 0),
            "conflicts_rejected": conflict_diag.get("rejected", 0),
            "dropped_sentences": len([d for d in written["dropped"] if d.get("text")]),
            "violations": len(written["violations"]),
            "degraded": written["degraded"],
        }

        export_key = self._export_to_minio(topic, markdown)
        return {
            "topic": topic,
            "markdown": markdown,
            "rejected": False,
            "invalid_citations": [],
            "referenced_arxiv_ids": papers,
            "export_key": export_key,
            "claims": claim_rows,
            "conflicts": edges,
            "refs": list(refs.values()),
            "stats": stats,
            "config": cfg,
            "degraded": written["degraded"],
            "trace": {
                "pool_notes": pool["notes"],
                "queries": pool["queries"],
                "pool_size": len(chunks),
                "cluster": cluster_diag,
                "conflict": conflict_diag,
                "dropped_claims": extracted.dropped,
                "dropped_sentences": written["dropped"],
                "violations": written["violations"],
                "timing": {**timing, "total": round(time.time() - t_all, 2)},
            },
        }

    # ------------------------------------------------------------ claim 表

    @staticmethod
    def _claim_rows(
        clusters: list[Cluster],
        edges: list[dict[str, Any]],
        sections: dict[str, list[dict[str, Any]]],
        ref_index: dict[str, int],
    ) -> list[dict[str, Any]]:
        """
        claim 表的一行 = 一个观点簇。

        为什么行不是"一条 claim"：原始 claim 有几十条，同一结论被两三篇论文各说一遍，
        按条列会让读者看到一堆重复行；按簇列才回答得了他真正问的问题——
        「这条结论由哪几篇支撑、立场是什么、有没有人反对」。
        每条 claim 的原文仍然挂在 `evidence` 里，点开可查。
        """
        by_cluster: dict[str, list[dict[str, Any]]] = {}
        for e in edges:
            for cid in e["clusters"]:
                by_cluster.setdefault(cid, []).append(
                    {
                        "id": e["id"],
                        "with_cluster": [x for x in e["clusters"] if x != cid][0],
                        "type": e["type"],
                        "type_label": e["type_label"],
                        "confirmed": e["confirmed"],
                        "reason": e["reason"],
                    }
                )

        rows: list[dict[str, Any]] = []
        for c in clusters:
            row = c.to_dict(ref_index)
            row["refs"] = sorted(
                {n for n in (ref_index.get(x.chunk_id) for x in c.claims) if n}
            )
            row["sentence_count"] = len(sections.get(c.id) or [])
            row["conflicts"] = by_cluster.get(c.id, [])
            row["note"] = "仅单篇支持" if c.strength == "single_paper" else ""
            rows.append(row)
        return rows

    # ------------------------------------------------------------ 导出

    def _export_to_minio(self, topic: str, markdown: str) -> str | None:
        key = f"reviews/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}.md"
        try:
            self.storage.put_object(
                self.storage.provenance_bucket,
                key,
                markdown.encode("utf-8"),
                content_type="text/markdown",
            )
            return key
        except Exception:
            # 归档失败不该让用户拿不到综述：markdown 已经在响应体里了。
            return None

    def get_export_url(self, key: str, expires: int = 3600) -> str:
        return self.storage.get_presigned_url(self.storage.provenance_bucket, key, expires=expires)
