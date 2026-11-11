"""
异步任务：PDF 结构化解析、去重、索引，以及外部数据接入。
关键设计决策：长文档（如 541 页）按页范围拆分为子任务并行解析，
再通过 chord 回调合并，避免单任务超时/OOM；图表/公式走视觉模型，
章节树/参考文献走 LLM 结构化。
"""
from typing import Dict, Any, List
from celery import group, chord
import fitz
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.services.storage import StorageService
from app.services.pdf_parser import PDFParser
from app.services.vector_store import VectorStoreService
from app.services.figure_extractor import FigureExtractor
from app.services.formula_extractor import FormulaExtractor
from app.services.chapter_parser import ChapterParser
from app.services.bibliography_extractor import BibliographyExtractor
from app.services.citation_extractor import extract_from_chunks
from app.services.arxiv_client import ArxivClient
from app.services.s2_client import SemanticScholarClient
from app.services.openalex_client import OpenAlexClient
from app.services.crossref_client import CrossrefClient
from app.repositories import (
    ChunkRepository,
    CitationRepository,
    PaperRepository,
    PaperVersionRepository,
    ProjectRepository,
    ReferenceRepository,
)

# 单个子任务处理页数；541 页书约拆成 18 个子任务
CHUNK_SIZE = 30


def _qdrant_point_id(arxiv_id: str, version: str, kind: str, idx: int) -> str:
    """
    与 VectorStoreService.index_fragments 里的点 ID 算法保持一致。
    chunk 表存下这个 ID，检索命中后可以直接 point → chunk → page/bbox 反查，
    不用再按文本模糊匹配。
    """
    import hashlib

    return hashlib.md5(f"frag::{arxiv_id}::{version}::{kind}::{idx}".encode("utf-8")).hexdigest()


def _ensure_version(
    arxiv_id: str,
    version: str,
    title: str,
    s3_key: str,
    file_hash: str,
    project_id: str | None,
    paper_id: str | None,
    version_id: str | None,
) -> str | None:
    """
    兜底建结构：定时追新等入口不会先调上传接口，这里现场补 project→paper→version，
    保证任何一条解析路径结束后，chunk 都能反查到课题。
    """
    if version_id:
        return version_id
    try:
        project = ProjectRepository.get(project_id) if project_id else None
        if not project:
            project = ProjectRepository.get_or_create_default()
        paper = PaperRepository.get(paper_id) if paper_id else None
        if not paper:
            paper = PaperRepository.upsert_paper(
                project_id=project.id, arxiv_id=arxiv_id, title=title or arxiv_id
            )
        pv, _ = PaperVersionRepository.create(
            paper_id=paper.id,
            version_label=version,
            file_hash=file_hash,
            pdf_s3_key=s3_key,
            status="pending",
        )
        return pv.id
    except Exception:
        # 结构写入失败不能反过来阻断向量索引，否则一次 DB 抖动会让论文彻底入不了库
        import traceback
        traceback.print_exc()
        return None


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def parse_and_index_pdf(
    self,
    arxiv_id: str,
    version: str,
    title: str,
    s3_key: str,
    authors: List[str] | None = None,
    abstract: str | None = None,
    doi: str | None = None,
    project_id: str | None = None,
    paper_id: str | None = None,
    version_id: str | None = None,
) -> Dict[str, Any]:
    """
    异步解析 PDF 的入口：按页拆分并行解析，合并后去重、建索引。
    课题化改造后额外接收 project_id / paper_id / version_id，
    这三个值由上传接口预先建好；定时任务等入口没传，则在下面兜底补齐。
    """
    try:
        vector_store = VectorStoreService()
        if vector_store.get_paper_meta(arxiv_id, version):
            return {
                "arxiv_id": arxiv_id,
                "version": version,
                "status": "duplicate_key",
                "message": "该 arxiv_id+version 已存在",
            }

        storage = StorageService()
        pdf_bytes = storage.get_pdf_bytes(s3_key)
        page_count = len(fitz.open(stream=pdf_bytes, filetype="pdf"))

        import hashlib

        resolved_version_id = _ensure_version(
            arxiv_id=arxiv_id,
            version=version,
            title=title,
            s3_key=s3_key,
            file_hash=hashlib.sha256(pdf_bytes).hexdigest(),
            project_id=project_id,
            paper_id=paper_id,
            version_id=version_id,
        )

        ranges = [
            (i, min(i + CHUNK_SIZE - 1, page_count - 1))
            for i in range(0, page_count, CHUNK_SIZE)
        ]

        callback = merge_parsed_chunks.s(
            arxiv_id=arxiv_id,
            version=version,
            title=title,
            s3_key=s3_key,
            authors=authors or [],
            abstract=abstract or "",
            doi=doi,
            project_id=project_id,
            version_id=resolved_version_id,
        )
        job = chord(
            group(parse_pdf_page_range.s(arxiv_id, version, s3_key, s, e) for s, e in ranges),
            callback,
        )
        result = job.apply_async()
        return {
            "arxiv_id": arxiv_id,
            "version": version,
            "status": "dispatched",
            "pages": page_count,
            "chunks": len(ranges),
            "version_id": resolved_version_id,
            "merge_task_id": result.id,
        }
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=600,
    soft_time_limit=480,
)
def parse_pdf_page_range(
    self,
    arxiv_id: str,
    version: str,
    s3_key: str,
    start_page: int,
    end_page: int,
) -> Dict[str, Any]:
    """
    解析指定页范围：段落、标题、图表、公式、表格、页面图。
    所有图片直接上传 MinIO，只返回轻量元数据，避免结果过大。
    """
    try:
        storage = StorageService()
        pdf_bytes = storage.get_pdf_bytes(s3_key)

        parser = PDFParser(pdf_bytes)
        parsed = parser.parse_range(start_page, end_page)

        figure_ext = FigureExtractor()
        formula_ext = FormulaExtractor()

        fragments: List[Dict[str, Any]] = []
        figure_keys: List[str] = []
        formula_items: List[Dict[str, Any]] = []
        all_headings: List[Dict[str, Any]] = list(parsed["headings"])

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for local_idx, page_idx in enumerate(range(start_page, end_page + 1)):
            page = doc.load_page(page_idx)

            # 页面图上传
            page_png_key = f"{arxiv_id}/{version}/pages/page_{page_idx + 1:04d}.png"
            storage.upload_figure(page_png_key, parsed["pages_png"][local_idx], content_type="image/png")

            # 图表抽取
            for fig in figure_ext.extract_from_page(page, page_idx + 1, arxiv_id, version):
                figure_keys.append(fig["key"])
                fragments.append({
                    "kind": "figure",
                    "text": f"{fig.get('caption') or fig.get('description') or 'Figure'}",
                    "page": fig["page"],
                    "section": "Figures",
                    "caption": fig.get("caption"),
                    "description": fig.get("description"),
                    "key": fig["key"],
                })

            # 公式抽取
            for formula in formula_ext.extract_from_page(page, page_idx + 1, arxiv_id, version):
                formula_items.append(formula)
                fragments.append({
                    "kind": "formula",
                    "text": formula["latex"],
                    "page": formula["page"],
                    "section": "Formulas",
                    "latex": formula["latex"],
                    "raw_text": formula["raw_text"],
                    "key": formula["key"],
                })

        # 表格作为文本片段
        tables = parser.extract_tables_with_pdfplumber(start_page, end_page)
        for t in tables:
            fragments.append({
                "kind": "table",
                "text": t["text"],
                "page": t["page"],
                "section": "Tables",
                "index": t["index"],
            })

        # 段落本身
        for chunk in parsed["chunks"]:
            chunk["kind"] = "paragraph"
            fragments.append(chunk)

        return {
            "start_page": start_page,
            "end_page": end_page,
            "fragments": fragments,
            "headings": all_headings,
            "references_raw": parsed["references_raw"],
            "body_hash": parsed["body_hash"],
            "full_text": parsed["full_text"],
            "figure_keys": figure_keys,
            "formula_items": formula_items,
        }
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def merge_parsed_chunks(
    self,
    chunk_results: List[Dict[str, Any]],
    arxiv_id: str,
    version: str,
    title: str,
    s3_key: str,
    authors: List[str],
    abstract: str,
    doi: str | None,
    project_id: str | None = None,
    version_id: str | None = None,
) -> Dict[str, Any]:
    """
    chord 回调：合并各页范围结果，去重，建章节树，解析参考文献，
    补齐外部元数据，最终写入 Qdrant。
    """
    try:
        vector_store = VectorStoreService()

        # 1. 再次检查唯一键
        if vector_store.get_paper_meta(arxiv_id, version):
            return {"arxiv_id": arxiv_id, "version": version, "status": "duplicate_key"}

        # 2. 聚合
        all_fragments: List[Dict[str, Any]] = []
        all_headings: List[Dict[str, Any]] = []
        references_raw_parts: List[str] = []
        full_text_parts: List[str] = []
        figure_keys: List[str] = []
        formula_items: List[Dict[str, Any]] = []

        for res in chunk_results:
            all_fragments.extend(res.get("fragments", []))
            all_headings.extend(res.get("headings", []))
            if res.get("references_raw"):
                references_raw_parts.append(res["references_raw"])
            full_text_parts.append(res.get("full_text", ""))
            figure_keys.extend(res.get("figure_keys", []))
            formula_items.extend(res.get("formula_items", []))

        full_text = "\n".join(full_text_parts)
        import hashlib
        body_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

        # 3. 去重（body_hash + embedding）
        dup_hash = vector_store.find_by_body_hash(body_hash)
        if dup_hash:
            # 版本行已在上传时建好，这里必须给它一个终态，
            # 否则会一直挂在 pending 上，看起来像“解析卡住了”
            if version_id:
                PaperVersionRepository.mark_indexed(version_id, status="duplicate")
            return {
                "arxiv_id": arxiv_id,
                "version": version,
                "status": "duplicate_body_hash",
                "merged_into": dup_hash.get("arxiv_id"),
            }
        dup_sim = vector_store.find_similar_by_embedding(full_text[:4096], threshold=0.96)
        if dup_sim:
            if version_id:
                PaperVersionRepository.mark_indexed(version_id, status="duplicate")
            return {
                "arxiv_id": arxiv_id,
                "version": version,
                "status": "duplicate_embedding",
                "merged_into": dup_sim.get("arxiv_id"),
            }

        # 4. 章节树
        chapter_tree = ChapterParser().build_tree(all_headings, model=settings.litellm_cheap_model)

        # 5. 参考文献结构化
        references_raw = "\n".join(references_raw_parts)
        structured_refs = BibliographyExtractor().parse(
            references_raw, model=settings.litellm_cheap_model
        )
        for ref in structured_refs:
            all_fragments.append({
                "kind": "reference",
                "text": f"{ref.get('title', '')} {ref.get('authors', '')} {ref.get('year', '')}",
                "page": 0,
                "section": "References",
                **ref,
            })

        # 章节树本身也作为可检索片段
        def flatten_tree(nodes: List[Dict[str, Any]], parent: str = ""):
            for node in nodes:
                all_fragments.append({
                    "kind": "chapter",
                    "text": node["title"],
                    "page": node.get("page", 0),
                    "section": parent or "Chapters",
                    "level": node.get("level", 1),
                    "children": [c.get("title", "") for c in node.get("children", [])],
                })
                flatten_tree(node.get("children", []), parent=node["title"])

        flatten_tree(chapter_tree)

        # 6. 写入向量
        indexed = vector_store.index_fragments(
            arxiv_id=arxiv_id,
            version=version,
            title=title,
            body_hash=body_hash,
            fragments=all_fragments,
        )

        # 7. 外部元数据
        # 本地上传的论文（arxiv_id 以 upload_ 开头）没有真实 arXiv ID，
        # Semantic Scholar 必然查不到（429/404），直接跳过避免阻塞索引。
        s2_graph = None
        if not arxiv_id.startswith("upload_"):
            try:
                s2_graph = SemanticScholarClient().fetch_citation_graph(arxiv_id)
            except Exception:
                s2_graph = None  # S2 限流/失败不阻塞索引，可后续补抓
        openalex = None
        crossref = None
        try:
            openalex = OpenAlexClient().get_work_by_arxiv(arxiv_id)
        except Exception:
            pass
        try:
            cr = CrossrefClient()
            crossref = cr.get_by_doi(doi) if doi else cr.search_by_title(title)
        except Exception:
            pass

        vector_store.upsert_paper_meta(
            arxiv_id=arxiv_id,
            version=version,
            title=title,
            authors=authors,
            abstract=abstract,
            body_hash=body_hash,
            pdf_s3_key=s3_key,
            figure_keys=figure_keys,
            s2_graph=s2_graph,
            openalex=openalex,
            crossref=crossref,
            chapter_tree=chapter_tree,
            structured_references=structured_refs,
        )

        # 8. 结构化落库：chunk / 参考文献 / 正文引用位置
        # 这一段失败不回滚向量索引——向量已经可检索，结构可以靠迁移脚本补，
        # 反过来（结构写成功、向量失败）才是不可接受的。
        stats = {"chunks": 0, "refs": 0, "citations": 0, "matched_refs": 0}
        if version_id:
            try:
                # 8.1 chunk：索引与 Qdrant 点保持同一个下标，point_id 可直接互换
                chunk_payloads = [
                    {
                        **frag,
                        "qdrant_point_id": _qdrant_point_id(
                            arxiv_id, version, frag.get("kind", "p"), idx
                        ),
                    }
                    for idx, frag in enumerate(all_fragments)
                ]
                stats["chunks"] = ChunkRepository.bulk_create(version_id, chunk_payloads)

                # 8.2 参考文献条目
                stats["refs"] = ReferenceRepository.bulk_create(version_id, structured_refs)
                ref_map = {
                    r.ref_index: r.id for r in ReferenceRepository.list_by_version(version_id)
                }

                # 8.3 正文引用位置：把 [12] 这类标记落到具体句子与页码
                stored_chunks = ChunkRepository.list_by_version(version_id)
                cite_items = []
                for item in extract_from_chunks(
                    [
                        {
                            "id": c.id,
                            "text": c.text,
                            "page": c.page,
                            "bbox": c.bbox,
                            "chunk_type": c.chunk_type,
                        }
                        for c in stored_chunks
                    ]
                ):
                    ref_id = ref_map.get(item["ref_index"]) if item["ref_index"] else None
                    if not ref_id:
                        continue  # 编号超出文献表范围，多半是版面噪声
                    cite_items.append(
                        {
                            "ref_id": ref_id,
                            "chunk_id": item["chunk_id"],
                            "page": item["page"],
                            "bbox": item["bbox"],
                            "sentence": item["sentence"],
                        }
                    )
                stats["citations"] = CitationRepository.bulk_create(cite_items)

                # 8.4 参考文献回连课题内已入库论文
                if project_id:
                    stats["matched_refs"] = ReferenceRepository.match_to_project(
                        version_id, project_id
                    )

                PaperVersionRepository.mark_indexed(version_id, content_hash=body_hash)
            except Exception:
                import traceback

                traceback.print_exc()

        return {
            "arxiv_id": arxiv_id,
            "version": version,
            "status": "indexed",
            "fragments": indexed,
            "chapters": len(chapter_tree),
            "references": len(structured_refs),
            "formulas": len(formula_items),
            "figures": len(figure_keys),
            "version_id": version_id,
            "structured": stats,
        }
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_arxiv_category_updates(
    self,
    category: str = "cs.AI",
    days_back: int = 1,
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    定时追新：使用 OAI-PMH 增量拉取指定分类最新论文，异步索引。
    """
    try:
        from datetime import datetime, timedelta
        arxiv_client = ArxivClient()
        until_date = datetime.utcnow()
        from_date = until_date - timedelta(days=days_back)
        papers = arxiv_client.fetch_updates_oai(
            category=category,
            from_date=from_date,
            until_date=until_date,
            max_results=max_results,
        )
        enqueued = 0
        for paper in papers:
            pdf_bytes = arxiv_client.download_pdf(paper["pdf_url"])
            storage = StorageService()
            s3_key = f"{paper['arxiv_id']}/{paper['version']}/paper.pdf"
            storage.upload_pdf(s3_key, pdf_bytes)
            parse_and_index_pdf.delay(
                arxiv_id=paper["arxiv_id"],
                version=paper["version"],
                title=paper["title"],
                s3_key=s3_key,
                authors=paper.get("authors", []),
                abstract=paper.get("abstract", ""),
                doi=paper.get("doi"),
            )
            enqueued += 1
        return {"category": category, "enqueued": enqueued, "from": from_date.isoformat()}
    except Exception as exc:
        raise self.retry(exc=exc)
