"""
论文相关接口：上传 PDF、arXiv 导入、列出论文、检索 chunk。

上传路径的设计要点（见 docs/2.7-异步解析与长文档.md）：
1. 上传不解析。落库为 queued 立刻返回 task_id，解析交给 worker；
2. 读上传流时按 1MiB 分块边写临时文件边算 hash，不在内存里攒整份 PDF；
3. 算完 hash 先查"这个文件是不是已经解析过了"，命中就直接复制结果返回，
   根本不进队列——这是"同一文件二次上传秒级返回"的唯一实现方式。
"""
import hashlib
import os
import tempfile
from typing import List

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from app.schemas.paper import (
    PaperMeta,
    PaperUploadResponse,
    PaperQueryRequest,
    PaperQueryResponse,
    RetrievedChunk,
)
from app.services.storage import StorageService
from app.services.vector_store import VectorStoreService
from app.services.retrieval_service import RetrievalService, RetrievalResult
from app.services.arxiv_client import ArxivClient
from app.repositories.base import tx
from app.services.ingest import limits
from app.services.ingest.reuse import clone_version, copy_reference_matches
from app.repositories import (
    ChunkRepository,
    PaperRepository,
    PaperVersionRepository,
    ProjectRepository,
)
from app.tasks.ingest_tasks import enqueue

router = APIRouter(prefix="/papers", tags=["论文"])


def _normalize_arxiv_id(raw: str) -> str:
    """移除多余空格与版本号，保留纯 arxiv id。"""
    return raw.strip().split("v")[0]


def _resolve_project(project_id: str | None):
    """
    课题是数据模型的最小单位，所以任何入口都必须落到一个真实课题上：
    传了不存在的 id 直接 404（而不是悄悄落默认课题，避免写错课题还静默成功）；
    没传则落默认课题，保证旧前端不改也能继续上传。
    """
    if project_id:
        project = ProjectRepository.get(project_id)
        if not project:
            raise HTTPException(status_code=404, detail=f"课题不存在: {project_id}")
        return project
    return ProjectRepository.get_or_create_default()


async def _spool_upload(file: UploadFile) -> tuple[str, str, int]:
    """
    把上传流落到临时文件，返回 (路径, sha256, 字节数)。

    为什么不用 await file.read()：
    一次 read 会把整份 PDF 放进内存，541 页教材是 30~80MB，
    并发几个上传就直接把 api 容器顶到内存上限。
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    hasher = hashlib.sha256()
    size = 0
    head = b""
    try:
        while True:
            chunk = await file.read(limits.IO_CHUNK_BYTES)
            if not chunk:
                break
            if not head:
                head = chunk[:8]
            size += len(chunk)
            if size > limits.MAX_PDF_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"PDF 超过上限 {limits.MAX_PDF_BYTES // 1048576}MB",
                )
            hasher.update(chunk)
            tmp.write(chunk)
        tmp.close()
        if not head.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
        return tmp.name, hasher.hexdigest(), size
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _reuse_if_possible(
    body_hash: str,
    arxiv_id: str,
    version: str,
    project_id: str,
    title: str,
) -> PaperUploadResponse | None:
    """
    file_hash 命中且向量已在库时，直接复制解析结果并返回。
    返回 None 表示没有可复用的源版本，调用方应走正常入队流程。
    """
    src = PaperVersionRepository.find_reusable(body_hash, arxiv_id, version)
    if not src:
        return None

    # 向量是共享的（点 ID 由 arxiv_id+version+kind+idx 决定），但 meta 必须真的在库里。
    # 否则复用完会出现"有 chunk 行、搜不到"的空壳。
    if not VectorStoreService().get_paper_meta(arxiv_id, version):
        return None

    paper = PaperRepository.upsert_paper(project_id=project_id, arxiv_id=arxiv_id, title=title)
    dst, _created = PaperVersionRepository.create(
        paper_id=paper.id,
        version_label=version,
        file_hash=body_hash,
        pdf_s3_key=src.get("pdf_s3_key"),
        page_count=src.get("page_count"),
        status="pending",
        stage="reused",
    )

    if dst.id == src["id"]:
        # 同一课题重复上传同一文件：命中的就是这一行自己，不需要复制，
        # 也不能去复制（clone 会先删自己的 chunk 再照抄，属于自伤操作）
        stats = {
            "chunks": src.get("chunk_count") or 0,
            "refs": src.get("ref_count") or 0,
            "citations": src.get("citation_count") or 0,
        }
    else:
        stats = clone_version(src["id"], dst.id)
        copy_reference_matches(src["id"], dst.id)

    return PaperUploadResponse(
        paper_id=f"{arxiv_id}::{version}",
        arxiv_id=arxiv_id,
        version=version,
        task_id=f"reuse::{dst.id}",
        project_id=project_id,
        paper_uuid=paper.id,
        version_id=dst.id,
        stage="reused",
        reused=True,
        progress_url=f"/api/v1/ingest/versions/{dst.id}/progress",
        detail=f"命中已解析文件，复用 {stats['chunks']} 个片段（来源版本 {src['id']}）",
    )


@router.post("/upload", response_model=PaperUploadResponse)
async def upload_pdf(
    file: UploadFile = File(...),
    arxiv_id: str | None = None,
    version: str = "v1",
    project_id: str | None = None,
    dedupe_action: str | None = Form(None),
    dedupe_relation: str | None = Form(None),
    peer_version_id: str | None = Form(None),
):
    """
    上传 PDF，落库为 queued 后立即返回，解析在 worker 里分批进行。
    验收标准：
    1. 接收 multipart/form-data 的 PDF，全程流式落盘，不整份读入内存；
    2. 响应 < 1s 返回 task_id 与 progress_url，HTTP 不等待解析；
    3. file_hash 已解析过的文件（含跨课题）秒级返回并复用结果；
    4. 响应保留原有 paper_id/arxiv_id/version/task_id 字段，新增字段全部可选。

    `dedupe_action` 是用户在去重弹窗里选的动作（见 services/dedupe/）：
    - keep_new   → 挂到 `peer_version_id` 所属的那个**工作**下，本次成为当前版本
    - keep_both  → 独立入库（不合并），两篇各自可检索
    - link_only  → 关系是 version_of 时不解析不入库（省一次几十秒的解析），
                   其他关系下正常入库但只记一条关联
    不传则以 keep_both 处理并在判定记录里标 `auto`——宁可并存，不可静默合并。
    """
    tmp_path, body_hash, _size = await _spool_upload(file)
    try:
        arxiv_id = _normalize_arxiv_id(arxiv_id) if arxiv_id else f"upload_{body_hash[:16]}"
        project = _resolve_project(project_id)

        # 先判重再上传对象存储：命中时连 MinIO 都不用写，这才叫秒级
        reused = _reuse_if_possible(
            body_hash=body_hash,
            arxiv_id=arxiv_id,
            version=version,
            project_id=project.id,
            title=file.filename or arxiv_id,
        )
        if reused:
            return reused

        # ---- 关系判定给的上游决定（用户选的 / 系统记住的） ----
        peer = PaperVersionRepository.get(peer_version_id) if peer_version_id else None
        action = dedupe_action if dedupe_action in ("keep_new", "keep_both", "link_only") else "keep_both"
        relation = dedupe_relation or ""

        # version_of + link_only = 「这次不处理」：用户已经有一版，不想再花时间解析。
        # 判定记录不在这里写——写记录需要两份指纹算 pair_key，而指纹在上一步的
        # /dedupe/precheck 里已经算过一次。这里重算要多花 1~2 秒，且同一件事
        # 有两个写入点，迟早会不一致。记录的职责归 /dedupe/decide。
        if action == "link_only" and relation == "version_of":
            return PaperUploadResponse(
                paper_id=f"{arxiv_id}::{version}",
                arxiv_id=arxiv_id,
                version=version,
                task_id="",
                project_id=project.id,
                paper_uuid=peer.paper_id if peer else "",
                version_id="",
                stage="skipped",
                reused=False,
                progress_url="",
                detail="按你的选择跳过入库：本次只记录了与库内版本的判定关系",
            )

        s3_key = f"{arxiv_id}/{version}/paper.pdf"
        storage = StorageService()
        storage.upload_pdf_file(s3_key, tmp_path)

        # 从首页抽标题与作者：本地上传的论文原来拿**文件名**当标题，
        # 于是 "A.pdf" 和 "final_v9.pdf" 在库里是两篇毫无关系的论文，
        # 去重判定连"标题重合度"这一票都投不出来。抽一次只要几十毫秒。
        doc_title, doc_authors = file.filename or arxiv_id, []
        try:
            from app.services.dedupe.fingerprint import extract_meta_from_pdf

            _meta = extract_meta_from_pdf(tmp_path)
            if _meta.get("title") and len(_meta["title"]) >= 8:
                doc_title = _meta["title"]
            doc_authors = [a for a in (_meta.get("authors") or []) if a][:20]
        except Exception:
            pass

        attach_paper_id = ""
        if action == "keep_new" and peer and relation == "version_of":
            # 挂到同一工作下：不是"新建一篇论文"，而是给已有论文加一个版本
            attach_paper_id = peer.paper_id

        if attach_paper_id:
            paper = PaperRepository.get(attach_paper_id)
            # 同名版本已存在时换一个标签，避免 UNIQUE(paper_id, version_label) 冲突
            label = version
            if PaperVersionRepository.get_by_label(attach_paper_id, label):
                label = f"{version}-b"
            version_row, _created = PaperVersionRepository.create(
                paper_id=attach_paper_id,
                version_label=label,
                file_hash=body_hash,
                pdf_s3_key=s3_key,
                status="pending",
                stage="queued",
            )
            version = label
        else:
            paper = PaperRepository.upsert_paper(
                project_id=project.id,
                arxiv_id=arxiv_id,
                title=doc_title,
                authors=doc_authors,
            )
            version_row, _created = PaperVersionRepository.create(
                paper_id=paper.id,
                version_label=version,
                file_hash=body_hash,
                pdf_s3_key=s3_key,
                status="pending",
                stage="queued",
            )

        # keep_both：两个版本并存时，当前版本仍留在库内已有的那个
        if action == "keep_both" and peer and peer.paper_id == paper.id and peer.id != version_row.id:
            with tx(None) as c:
                c.execute("UPDATE paper_versions SET is_current = 0 WHERE id = ?", (version_row.id,))
                c.execute("UPDATE paper_versions SET is_current = 1 WHERE id = ?", (peer.id,))


        task_id = enqueue(version_row.id)
        return PaperUploadResponse(
            paper_id=f"{arxiv_id}::{version}",
            arxiv_id=arxiv_id,
            version=version,
            task_id=task_id,
            project_id=project.id,
            paper_uuid=paper.id,
            version_id=version_row.id,
            stage="queued",
            reused=False,
            progress_url=f"/api/v1/ingest/tasks/{task_id}/progress",
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.post("/arxiv/import", response_model=PaperUploadResponse)
def import_arxiv(arxiv_id: str, project_id: str | None = None):
    """
    通过 arXiv ID 拉取并索引论文。
    验收标准：
    1. 校验 arxiv_id 格式；
    2. 从 arXiv 下载 PDF 并上传 MinIO；
    3. 派发解析任务并返回 task_id。

    同步 `def` 而不是 `async def`：函数体里全是 requests 的阻塞调用（取元数据 +
    下载整份 PDF，实测 1~5 s，慢的能到十几秒）。写成 async 会把这些等待挂在事件
    循环上，期间所有接口一起卡住；同步函数 FastAPI 会丢进线程池，互不影响。
    """
    arxiv_id = _normalize_arxiv_id(arxiv_id)
    client = ArxivClient()
    meta = client.fetch_by_id(arxiv_id)
    if not meta:
        raise HTTPException(status_code=404, detail="未找到该 arXiv 论文")

    pdf_bytes = client.download_pdf(meta["pdf_url"])
    s3_key = f"{meta['arxiv_id']}/{meta['version']}/paper.pdf"
    storage = StorageService()
    storage.upload_pdf(s3_key, pdf_bytes)

    project = _resolve_project(project_id)
    body_hash = hashlib.sha256(pdf_bytes).hexdigest()

    # arXiv 反复导入同一版本时同样走复用，不重复解析
    reused = _reuse_if_possible(
        body_hash=body_hash,
        arxiv_id=meta["arxiv_id"],
        version=meta["version"],
        project_id=project.id,
        title=meta["title"],
    )
    if reused:
        return reused

    paper = PaperRepository.upsert_paper(
        project_id=project.id,
        arxiv_id=meta["arxiv_id"],
        title=meta["title"],
        authors=meta.get("authors", []),
        abstract=meta.get("abstract", ""),
        doi=meta.get("doi"),
        year=meta.get("year"),
        venue=meta.get("venue"),
    )
    version_row, _created = PaperVersionRepository.create(
        paper_id=paper.id,
        version_label=meta["version"],
        file_hash=body_hash,
        pdf_s3_key=s3_key,
        status="pending",
        stage="queued",
    )

    # 同一篇之前解析失败过：`create` 会原样返回那一行，此时直接入队等于再跑一次
    # 注定失败的尝试（attempt 已到上限，pipeline 一进去就判死）。
    # 用户点「重试导入」要的是"再来一次"，所以先清终态——`reset_for_retry` 保留
    # parse_cursor，是"从断点续跑"而不是"从头再解析一遍"。
    if not _created and version_row.status == "failed":
        PaperVersionRepository.reset_for_retry(version_row.id)

    task_id = enqueue(version_row.id)
    return PaperUploadResponse(
        paper_id=f"{meta['arxiv_id']}::{meta['version']}",
        arxiv_id=meta["arxiv_id"],
        version=meta["version"],
        task_id=task_id,
        project_id=project.id,
        paper_uuid=paper.id,
        version_id=version_row.id,
        stage="queued",
        reused=False,
        progress_url=f"/api/v1/ingest/tasks/{task_id}/progress",
    )


@router.get("/", response_model=List[dict])
async def list_papers():
    """列出已索引论文元数据（去重）。"""
    vs = VectorStoreService()
    return vs.list_papers(limit=200)


@router.post("/search", response_model=PaperQueryResponse)
async def search_papers(req: PaperQueryRequest):
    """
    跨文献检索：dense + sparse + RRF。
    验收标准：
    1. 接受 query/top_k/rrf_k；
    2. 返回带 paper_id、arXiv ID、页码、段落、bbox 的原文片段；
    3. 结果按 RRF 融合分数降序排列。
    """
    retrieval = RetrievalService()
    result = retrieval.search(req.query, top_k=req.top_k, rrf_k=req.rrf_k)
    chunks = [
        RetrievedChunk(
            paper_id=r.paper_id,
            arxiv_id=r.arxiv_id,
            version=r.version,
            title=r.title,
            kind=r.kind,
            text=r.text,
            section=r.section,
            page=r.page,
            paragraph=r.paragraph,
            score=r.score,
            bbox=r.bbox,
            latex=r.latex,
            caption=r.caption,
        )
        for r in result["fusion"]
    ]
    return PaperQueryResponse(query=req.query, chunks=chunks)


@router.post("/retrieve")
async def retrieve_papers(req: PaperQueryRequest):
    """
    检索层原始输出：同时返回 dense、sparse、RRF fusion 三路结果，便于调试与评测。
    """
    retrieval = RetrievalService()
    return retrieval.search(req.query, top_k=req.top_k, rrf_k=req.rrf_k)


@router.get("/{arxiv_id}/pdf")
async def get_pdf_stream(arxiv_id: str):
    """
    后端代理论文 PDF 原文件流。
    浏览器/前端 pdf.js 直接访问该端点，无需 presigned URL（容器内
    minio:9000 地址浏览器不可达，签名换域后又会失效，故走代理最稳）。
    """
    arxiv_id = _normalize_arxiv_id(arxiv_id)
    vs = VectorStoreService()
    meta = vs.get_paper_meta_by_arxiv_id(arxiv_id)
    storage = StorageService()
    s3_key = None
    if meta and meta.get("pdf_s3_key"):
        s3_key = meta["pdf_s3_key"]
    else:
        # 本地上传论文兜底：按已知 key 规则尝试
        s3_key = f"{arxiv_id}/v1/paper.pdf"
    try:
        data = storage.get_pdf_bytes(s3_key)
    except Exception:
        raise HTTPException(status_code=404, detail="未找到论文 PDF，请重新上传")
    from fastapi import Response
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{arxiv_id}.pdf"'},
    )


@router.get("/{arxiv_id}/pdf-url")
async def get_pdf_url(arxiv_id: str):
    """
    获取论文 PDF 的临时访问链接；若本地 MinIO 不存在则回退 arXiv 官方 PDF。
    前端溯源高亮需要该 URL 配合 pdf.js 渲染。
    """
    arxiv_id = _normalize_arxiv_id(arxiv_id)
    vs = VectorStoreService()
    meta = vs.get_paper_meta_by_arxiv_id(arxiv_id)
    storage = StorageService()
    if meta and meta.get("pdf_s3_key"):
        url = storage.get_presigned_url(storage.papers_bucket, meta["pdf_s3_key"], expires=3600)
        if url:
            return {"arxiv_id": arxiv_id, "pdf_url": url, "source": "minio"}
    return {
        "arxiv_id": arxiv_id,
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        "source": "arxiv",
    }


@router.get("/{arxiv_id}/anchor-profile")
async def get_anchor_profile(arxiv_id: str):
    """
    文档级锚点概览。前端打开阅读器之前先问一次：
    `has_anchors=false` 就直接显示「该文档未建立精细锚点」，不必等点击失败。
    """
    return ChunkRepository.anchor_profile(_normalize_arxiv_id(arxiv_id))


@router.get("/{arxiv_id}/anchors")
async def get_chunk_anchors(arxiv_id: str, chunk_id: str):
    """
    单个 chunk 的句子级锚点。阅读器在"引用没带锚点但用户仍想定位"时用它兜底，
    也用于校验：拿 ref_id 回来核对 start/end 与库里的文本是否还对得上。
    """
    chunk = ChunkRepository.get(chunk_id)
    if not chunk:
        raise HTTPException(status_code=404, detail="片段不存在")
    items = (chunk.anchors or {}).get("items") or []
    return {
        "chunk_id": chunk.id,
        "arxiv_id": _normalize_arxiv_id(arxiv_id),
        "page": chunk.page,
        "kind": chunk.chunk_type,
        "page_size": (chunk.anchors or {}).get("page_size"),
        "text_layer": (chunk.anchors or {}).get("text_layer", True),
        "precision": (chunk.anchors or {}).get("precision"),
        "has_anchors": bool(items),
        # 带上文本本身：前端要用 start/end 切片校验高亮范围，
        # 不一致时（例如换了版本）宁可退回页级定位，也不要框错地方
        "text": chunk.text,
        "items": items,
    }
