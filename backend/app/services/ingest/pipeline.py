"""
长文档解析主管线：流式下载 → 分批解析 → 分批入库 → 汇总引用。

三条硬约束与对应的实现手段：

1. **不得一次性把整本 PDF 读进内存**
   PDF 先落临时文件，PyMuPDF 用 mmap 打开；每批只解码当前页，
   页面图「渲染一页 → 上传一页 → 立刻释放」，不累计 pages_png 列表。

2. **解析必须可重入**
   每批的边界由 (parse_cursor, index_cursor) 两个游标定义，写在版本行上：
   parse_cursor = 已解析到第几页，index_cursor = 已写入多少个片段。
   批次顺序是「先写向量库（幂等 upsert）→ 再提交关系库并推进游标」，
   所以任何一步崩溃后重试，都只会重做同一批，不会产生脏数据。

3. **内存不得持续增长**
   每批开始前检查进程 RSS，超过 RSS_SOFT_LIMIT_MB 主动失败并留下上下文，
   而不是等内核 OOM Killer 把 worker 杀掉（那样连日志都没有）。
"""
import hashlib
import json
import logging
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import fitz

from app.core.config import settings
from app.db import new_id, utc_now
from app.repositories import (
    ChunkRepository,
    CitationRepository,
    PaperVersionRepository,
    ReferenceRepository,
)
from app.services import anchors

logger = logging.getLogger(__name__)
from app.services.bibliography_extractor import BibliographyExtractor
from app.services.chapter_parser import ChapterParser
from app.services.citation_extractor import extract_from_chunks
from app.services.figure_extractor import FigureExtractor
from app.services.formula_extractor import FormulaExtractor
from app.services.ingest import limits
from app.services.pdf_parser import PDFParser
from app.services.storage import StorageService
from app.services.vector_store import VectorStoreService


class IngestError(RuntimeError):
    """可预期的失败（页数超限、内容为空等），带阶段信息，用于写 error_message。"""

    def __init__(self, message: str, stage: str = "parsing"):
        super().__init__(message)
        self.stage = stage


class IngestMemoryError(IngestError):
    """RSS 超软上限。单独一类是因为它不该重试——重试只会再撞一次。"""


@dataclass
class IngestOutcome:
    version_id: str
    status: str
    stage: str = "indexed"
    pages: int = 0
    chunks: int = 0
    refs: int = 0
    citations: int = 0
    elapsed_ms: int = 0
    resumed_from_page: int = 0
    skipped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "status": self.status,
            "stage": self.stage,
            "pages": self.pages,
            "chunks": self.chunks,
            "refs": self.refs,
            "citations": self.citations,
            "elapsed_ms": self.elapsed_ms,
            "resumed_from_page": self.resumed_from_page,
            "skipped": self.skipped,
            **self.extra,
        }


# ---------- 进程与工作目录 ----------
def rss_mb() -> Optional[float]:
    """读 /proc/self/status 的 VmRSS，单位 MiB。取不到就返回 None（非 Linux）。"""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def _check_memory(stage: str) -> None:
    current = rss_mb()
    if current is not None and current > limits.RSS_SOFT_LIMIT_MB:
        raise IngestMemoryError(
            f"进程内存 {current:.0f}MB 超过软上限 {limits.RSS_SOFT_LIMIT_MB}MB，已主动中止（{stage}）",
            stage=stage,
        )


def work_dir(version_id: str) -> Path:
    """
    每个版本一个工作目录，放下载的 PDF 与跨批状态。
    放在持久卷（app_data）而不是 /tmp，这样容器重启后断点仍然成立。
    """
    root = Path(settings.ingest_work_dir) / version_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def sweep_work_dirs(keep_s: int = 3600) -> list[str]:
    """
    清理"没人认领"的工作目录。

    正常路径下 run_ingest 成功后会自己 rmtree，但有两种情况会漏：
      1. worker 被 SIGKILL（docker restart），进程没走到 finally；
      2. 版本行被删掉了（清理测试数据、删课题级联），目录就成了孤儿。

    这里只删两类：所属版本行已经不存在、或版本已是终态 indexed 的目录。
    且必须 mtime 超过 keep_s——避免把正在跨批解析的目录当成垃圾扫掉
    （它的 raw.pdf 还没刷新，但目录本身在批与批之间可能长时间不写）。

    返回被删掉的 version_id 列表，给巡检日志用。
    """
    root = Path(settings.ingest_work_dir)
    if not root.exists():
        return []
    cutoff = time.time() - keep_s
    removed: list[str] = []
    terminal = {"indexed"}
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        version = PaperVersionRepository.get(child.name)
        if version is not None and version.stage not in terminal:
            continue  # pending / failed 的目录留着，续跑要用
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    return removed


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _point_id(arxiv_id: str, version: str, kind: str, idx: int) -> str:
    """与 VectorStoreService 内部的点 ID 算法保持一致（见 vector_store 注释）。"""
    return hashlib.md5(f"frag::{arxiv_id}::{version}::{kind}::{idx}".encode("utf-8")).hexdigest()


def _log_phase(label: str, started: float, extra: str = "") -> None:
    """
    分阶段耗时打到 worker 日志。
    长文档解析最难排查的就是"卡住了"——没有分段计时，只能看到总时长，
    分不清是在下载、在解析、在生成向量还是在等外部 API。
    """
    print(f"[ingest] {label} {time.time() - started:.1f}s {extra}".rstrip(), flush=True)


# ---------- 单批解析 ----------
def _parse_batch(
    pdf_path: Path,
    start_page: int,
    end_page: int,
    state: dict[str, Any],
    storage: StorageService,
    arxiv_id: str,
    version: str,
    use_vision: bool,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """
    解析 [start_page, end_page]（0-based 闭区间），返回 (片段, 页面正文, 标题)。

    页面图在这里「渲染 → 上传 → 释放」，片段列表是本批唯一的累加器，
    大小由 MAX_FRAGMENTS_PER_BATCH 兜住。
    """
    parser = PDFParser(
        str(pdf_path),
        ocr=settings.ingest_ocr_enabled,
        ocr_lang=settings.ingest_ocr_lang,
        ocr_dpi=settings.ingest_ocr_dpi,
    )
    figure_ext = FigureExtractor()
    formula_ext = FormulaExtractor() if use_vision else None

    fragments: list[dict[str, Any]] = []
    page_texts: list[str] = []
    headings: list[dict[str, Any]] = []
    # 表格在页面循环之后才抽，那时已经拿不到 page 对象，
    # 所以把锚点需要的页级信息（尺寸、有无文本层）先记下来。
    page_meta: dict[int, dict[str, Any]] = {}
    stats = {"anchor_chunks": 0, "anchor_items": 0, "anchor_missing": 0, "ocr_pages": 0}

    for page in parser.iter_pages(
        start_page,
        end_page,
        state=state,
        render=limits.RENDER_PAGE_IMAGES,
        dpi=limits.RENDER_DPI,
    ):
        page_no = page["page"]
        page_meta[page_no] = {
            "page_size": page.get("page_size"),
            "text_layer": bool(page.get("text_layer", True)),
            "ocr": bool(page.get("ocr")),
        }
        if page.get("ocr"):
            stats["ocr_pages"] += 1

        # 页面图：上传后立刻丢掉 bytes，前端溯源高亮靠它
        if page["png"]:
            storage.upload_figure(
                f"{arxiv_id}/{version}/pages/page_{page_no:04d}.png",
                page["png"],
                content_type="image/png",
            )
            page["png"] = None

        headings.extend(page["headings"])
        page_texts.append(page["text"])

        # 参考文献区：作为 reference 片段落库，结构化时再从库里读回来。
        # 不放在内存里跨批累积，是断点续跑能成立的前提。
        if page["references_raw"]:
            ref_text = page["references_raw"][: limits.MAX_REFERENCE_CHARS]
            ref_lines = page.get("references_lines") or []
            fragments.append(
                {
                    "kind": "reference",
                    "text": ref_text,
                    "page": page_no,
                    "section": "References",
                    "bbox": _bbox_union([ln.get("bbox") for ln in ref_lines]),
                    # 文献条目一条一行：逐行锚定，不然整页文献区只能框成一个大块
                    "anchors": anchors.build_line_document(
                        text=ref_text,
                        lines=ref_lines,
                        page=page_no,
                        page_size=page.get("page_size"),
                        text_layer=bool(page.get("text_layer", True)),
                    )
                    if settings.anchor_enabled and ref_lines
                    else None,
                }
            )

        if page["chunks"]:
            # 图表与公式需要 page 对象，这里单独开一次页面拿它
            doc = fitz.open(str(pdf_path))
            try:
                fitz_page = doc.load_page(page_no - 1)
                if figure_ext is not None:
                    for fig in figure_ext.extract_from_page(
                        fitz_page, page_no, arxiv_id, version, allow_vision=use_vision
                    ):
                        caption_text = fig.get("caption") or fig.get("description") or "Figure"
                        fragments.append(
                            {
                                "kind": "figure",
                                "text": caption_text,
                                "page": fig["page"],
                                "section": "Figures",
                                "caption": fig.get("caption"),
                                "description": fig.get("description"),
                                "key": fig["key"],
                                # 图注也要能高亮：没有 bbox 时前端只能整页定位，
                                # 而 figure_extractor 本来就算出了图片框，白丢可惜。
                                "bbox": fig.get("bbox"),
                                "anchors": _block_anchors(
                                    caption_text,
                                    fig["page"],
                                    fig.get("bbox"),
                                    page_meta.get(fig["page"]),
                                ),
                            }
                        )
                if formula_ext is not None:
                    # 公式抽取走视觉模型，是最容易因为额度/网络失败的一步。
                    # 它只是**增强**：没有公式行，正文与表格照常入库，检索也照常能命中
                    # 公式所在的段落。所以这里降级而不是让整篇解析失败——
                    # 一次视觉调用失败就把几十页的解析结果全丢掉，代价完全不对等。
                    try:
                        formulas = list(
                            formula_ext.extract_from_page(fitz_page, page_no, arxiv_id, version)
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "第 %s 页公式抽取失败，本批起跳过公式抽取：%s", page_no, exc
                        )
                        formula_ext = None
                        formulas = []
                    for formula in formulas:
                        fragments.append(
                            {
                                "kind": "formula",
                                "text": formula["latex"],
                                "page": formula["page"],
                                "section": "Formulas",
                                "latex": formula["latex"],
                                "raw_text": formula.get("raw_text"),
                                "key": formula["key"],
                                "bbox": formula.get("bbox"),
                                "anchors": _block_anchors(
                                    formula["latex"],
                                    formula["page"],
                                    formula.get("bbox"),
                                    page_meta.get(formula["page"]),
                                ),
                            }
                        )
            finally:
                doc.close()
                fitz_page = None

            # 正文片段：句子级锚点在这里写。行区间（line_from/line_to）由解析器给出，
            # 用它把对齐范围限定在本段的行内——同一页往往有好几段，
            # 不限定范围的话后一段会把前一段的行再认一遍，高亮就飘到别的段上了。
            for ch in page["chunks"]:
                ch["anchors"] = _sentence_anchors(ch, page)
                if ch["anchors"] and ch["anchors"].get("items"):
                    stats["anchor_chunks"] += 1
                    stats["anchor_items"] += len(ch["anchors"]["items"])
                else:
                    stats["anchor_missing"] += 1
                fragments.append(ch)

        if len(fragments) >= limits.MAX_FRAGMENTS_PER_BATCH:
            # 密集表格页可能一页就顶掉一整批的预算。截断而不是继续堆，
            # 否则单批 embedding 的显存/内存峰值不可控。
            fragments = fragments[: limits.MAX_FRAGMENTS_PER_BATCH]
            break

    # 表格用 pdfplumber 单独抽（只覆盖本批页范围）
    try:
        for t in parser.extract_tables_with_pdfplumber(start_page, end_page, with_cells=True):
            if len(fragments) >= limits.MAX_FRAGMENTS_PER_BATCH:
                break
            meta = page_meta.get(t["page"]) or {}
            fragments.append(
                {
                    "kind": "table",
                    "text": t["text"],
                    "page": t["page"],
                    "section": "Tables",
                    "index": t["index"],
                    # 表格的定位单元是单元格，不是整张表：只给表格 bbox 的话，
                    # 引用"BLEU 28.4"会把 12 行 × 6 列全框住。
                    "bbox": t.get("bbox"),
                    "anchors": anchors.build_cell_document(
                        text=t["text"],
                        cells=t.get("cell_items") or [],
                        page=t["page"],
                        page_size=meta.get("page_size"),
                        text_layer=bool(meta.get("text_layer", True)),
                    ),
                }
            )
            if (fragments[-1]["anchors"] or {}).get("items"):
                stats["anchor_chunks"] += 1
                stats["anchor_items"] += len(fragments[-1]["anchors"]["items"])
    except Exception:
        # 表格抽取失败不该拖垮整篇：pdfplumber 对畸形 PDF 比较敏感
        traceback.print_exc()

    return fragments, page_texts, headings, stats


def _bbox_union(rects: list[Any]) -> Optional[list[float]]:
    valid = [r for r in rects if r and len(r) == 4]
    if not valid:
        return None
    return [
        round(min(r[0] for r in valid), 2),
        round(min(r[1] for r in valid), 2),
        round(max(r[2] for r in valid), 2),
        round(max(r[3] for r in valid), 2),
    ]


def _sentence_anchors(chunk: dict[str, Any], page: dict[str, Any]) -> dict[str, Any] | None:
    """正文片段的句子级锚点；开关关掉时返回 None（等价于历史文档）。"""
    if not settings.anchor_enabled:
        return None
    lines = page.get("lines") or []
    lo = int(chunk.get("line_from", 0) or 0)
    hi = int(chunk.get("line_to", -1) or -1)
    scoped = lines[lo: hi + 1] if hi >= lo else lines
    return anchors.build_document(
        text=chunk["text"],
        lines=scoped,
        page=chunk.get("page") or 0,
        page_size=page.get("page_size"),
        text_layer=bool(page.get("text_layer", True)),
        precision=anchors.PRECISION_OCR if page.get("ocr") else anchors.PRECISION_SENTENCE,
    )


def _block_anchors(
    text: str, page_no: int, bbox: Any, meta: dict[str, Any] | None
) -> dict[str, Any] | None:
    """图表/公式：只有整体区域。无文本层时精度退化为 ocr。"""
    if not settings.anchor_enabled or not bbox:
        return None
    meta = meta or {}
    return anchors.build_block_document(
        text=text,
        page=page_no,
        bbox=bbox,
        page_size=meta.get("page_size"),
        text_layer=bool(meta.get("text_layer", True)),
    )


# ---------- 单批入库 ----------
def _commit_batch(
    version_id: str,
    fragments: list[dict[str, Any]],
    index_cursor: int,
    next_cursor: int,
    total_pages: int,
    body_bytes: int,
    chunk_delta: int,
) -> None:
    """
    关系库提交：chunk 写入 + 游标推进，放在**同一个事务**里。
    只有两者一起成功，"第 120 页解析完了但只写进去 110 页的 chunk" 才不可能出现。
    """
    from app.db import transaction

    now = utc_now()
    rows = []
    for offset, frag in enumerate(fragments):
        idx = index_cursor + offset
        text = (frag.get("text") or "").strip()
        if not text:
            continue
        kind = frag.get("kind", "paragraph")
        rows.append(
            (
                new_id("chk"),
                version_id,
                idx,
                frag.get("page"),
                _bbox_json(frag.get("bbox")),
                frag.get("section_path") or frag.get("section") or "",
                _chunk_type(kind),
                frag.get("table_id") or (f"Table {frag['index']}" if kind == "table" and frag.get("index") else None),
                frag.get("latex"),
                text,
                _tokens(text),
                _point_id(frag["_arxiv_id"], frag["_version"], kind, idx),
                _anchors_json(frag.get("anchors")),
                now,
            )
        )

    with transaction() as c:
        # 只清本批区间：重试同一批不会留下重复行，也不会碰别的批次
        c.execute(
            "DELETE FROM chunks WHERE version_id = ? AND chunk_index >= ? AND chunk_index < ?",
            (version_id, index_cursor, index_cursor + max(1, len(fragments))),
        )
        if rows:
            c.executemany(
                """INSERT INTO chunks
                   (id, version_id, chunk_index, page, bbox, section_path, chunk_type,
                    table_id, latex, text, token_count, qdrant_point_id, anchors, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        c.execute(
            """UPDATE paper_versions
                  SET parse_cursor = ?, index_cursor = ?, progress_page = ?,
                      text_bytes = ?, chunk_count = chunk_count + ?, stage = 'parsing'
                WHERE id = ?""",
            (next_cursor, index_cursor + len(fragments), next_cursor, body_bytes, chunk_delta, version_id),
        )


def _chunk_type(kind: str) -> str:
    return {
        "paragraph": "text",
        "chunk": "text",
        "chapter": "text",
        "table": "table",
        "figure": "figure",
        "formula": "formula",
        "reference": "reference",
    }.get(kind, "text")


def _tokens(text: str) -> int:
    from app.repositories.chunk_repo import estimate_tokens

    return estimate_tokens(text)


def _anchors_json(doc: Any) -> Optional[str]:
    """锚点文档落库。空 items 直接写 NULL：有 doc 但没 item 与"没做过锚点"是两回事，
    NULL 让前端能区分"历史文档"与"这页是扫描件/空白页"。"""
    from app.db import dump_json

    if not doc:
        return None
    items = doc.get("items") if isinstance(doc, dict) else None
    if not items:
        return None
    return dump_json(doc)


def _bbox_json(value: Any) -> Optional[str]:
    from app.db import dump_json

    if not value:
        return None
    # 库里读出来的 bbox 可能已经是字符串（迁移脚本踩过这个坑），
    # 再 dumps 一层会变成 "\"[...]\"" 这种双重编码
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except ValueError:
            return None
    return dump_json(value)


# ---------- 汇总阶段 ----------
def _call_with_deadline(fn, timeout_s: float):
    """
    在后台线程里跑 fn，超过 timeout_s 就放弃并返回 None。

    为什么不能只依赖 litellm 的 timeout 参数：
    实测把 timeout=45 传给 dashscope 的 OpenAI 兼容端点后，参考文献那次调用
    仍然跑了 136s（provider 侧的连接/读取超时语义与 litellm 的假设不一致）。
    一次教材解析里这段白等占了四分之一的总时长，所以必须在自己的这一层设死线。

    代价是超时后线程仍在后台跑（daemon，不阻止进程退出）。这是有意的取舍：
    比起让用户盯着 136 秒的进度条，留一个很快会自然结束的线程更划算。
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样抛回给调用方
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _llm_available() -> bool:
    """
    有没有配置可用的模型 key。

    没有 key 时 litellm 不会立刻报错，而是走"连接失败 → 退避重试"，
    实测每次调用要白等 30 秒左右。541 页的汇总阶段有两次这样的调用
    （章节树、参考文献），加起来就是一分钟的死等。
    所以这里先判断一次：没配 key 就直接走确定性启发式，不假装能调模型。
    """
    return bool(
        settings.openai_api_key
        or settings.anthropic_api_key
        or settings.dashscope_api_key
    )


def _structure(
    version_id: str,
    arxiv_id: str,
    version: str,
    title: str,
    authors: list[str],
    abstract: str,
    doi: str | None,
    s3_key: str,
    project_id: str,
    headings: list[dict[str, Any]],
    total_pages: int,
) -> tuple[str, int, int, int]:
    """
    解析后的汇总：章节树、参考文献、正文引用位置、paper_meta。
    返回 (body_hash, 片段总数, 文献数, 引用数)。
    """
    vector_store = VectorStoreService()

    t_phase = time.time()
    # 正文 hash 从落盘的正文日志流式算，不把全文读进内存
    hasher = hashlib.sha256()
    body_path = work_dir(version_id) / "body.txt"
    if body_path.exists():
        with open(body_path, "rb") as fh:
            while True:
                chunk = fh.read(limits.IO_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
    body_hash = hasher.hexdigest()

    # 内容级去重：字节不同但正文一致的 PDF（重新排版、加了水印）在这里合并
    dup = vector_store.find_by_body_hash(body_hash)
    _log_phase("structure.dedupe", t_phase)
    t_phase = time.time()
    if dup and dup.get("arxiv_id") != arxiv_id:
        vector_store.delete_chunks(arxiv_id, version)
        PaperVersionRepository.mark_indexed(version_id, page_count=total_pages, status="duplicate")
        PaperVersionRepository.advance(
            version_id, stage="duplicate", progress_page=total_pages, finished_at=utc_now()
        )
        return body_hash, 0, 0, 0

    # 1. 章节树：LLM 失败就退回按字号分级的启发式树，不能因为模型抖动丢结构
    t_phase = time.time()
    chapter_tree: list[dict[str, Any]] = []
    if _llm_available():
        try:
            chapter_tree = _call_with_deadline(
                lambda: ChapterParser().build_tree(
                    headings[: limits.MAX_HEADINGS],
                    model=settings.litellm_cheap_model,
                    timeout=limits.LLM_STRUCTURE_TIMEOUT_S,
                ),
                limits.LLM_STRUCTURE_TIMEOUT_S,
            ) or []
        except Exception:
            traceback.print_exc()
            chapter_tree = []
    if not chapter_tree:
        # 没配模型 / 超时 / 解析失败：按字号分层，结果确定、可复现
        chapter_tree = _fallback_tree(headings)

    _log_phase("structure.chapter_tree", t_phase, f"nodes={len(chapter_tree)}")
    # 2. 参考文献：从库里读回 reference 片段，避免内存累积
    t_phase = time.time()
    ref_chunks = ChunkRepository.list_by_version(version_id, chunk_type="reference")
    refs_text = "\n".join(c.text for c in ref_chunks)[: limits.MAX_REFERENCE_CHARS]
    structured_refs: list[dict[str, Any]] = []
    if refs_text and _llm_available():
        try:
            structured_refs = _call_with_deadline(
                lambda: BibliographyExtractor().parse(
                    refs_text,
                    model=settings.litellm_cheap_model,
                    timeout=limits.LLM_STRUCTURE_TIMEOUT_S,
                ),
                limits.LLM_STRUCTURE_TIMEOUT_S,
            ) or []
        except Exception:
            traceback.print_exc()
            structured_refs = []

    # 质量闸：LLM 只认出很少几条时，说明它把整页当成了一条（或直接没解析），
    # 这时按行首编号的启发式结果反而更准。取多者，避免"文献表只剩 3 条"。
    if refs_text:
        heuristic = _fallback_refs(refs_text)
        if len(structured_refs) < max(3, len(heuristic) // 2):
            structured_refs = heuristic

    _log_phase("structure.references", t_phase, f"refs={len(structured_refs)}")
    ref_count = ReferenceRepository.bulk_create(version_id, structured_refs)
    ref_map = {r.ref_index: r.id for r in ReferenceRepository.list_by_version(version_id)}

    # 3. 正文引用位置：按 chunk 分窗扫描，单窗 CITATION_SCAN_CHUNKS 条
    t_phase = time.time()
    chunk_total = ChunkRepository.count_by_version(version_id)
    cite_count = 0
    offset = 0
    while offset < chunk_total:
        window = ChunkRepository.list_range(version_id, offset, limits.CITATION_SCAN_CHUNKS)
        if not window:
            break
        items = []
        for item in extract_from_chunks(
            [
                {"id": c.id, "text": c.text, "page": c.page, "bbox": c.bbox, "chunk_type": c.chunk_type}
                for c in window
            ]
        ):
            ref_id = ref_map.get(item["ref_index"]) if item["ref_index"] else None
            if not ref_id:
                continue
            items.append(
                {
                    "ref_id": ref_id,
                    "chunk_id": item["chunk_id"],
                    "page": item["page"],
                    "bbox": item["bbox"],
                    "sentence": item["sentence"],
                }
            )
        cite_count += CitationRepository.bulk_create(items)
        offset += limits.CITATION_SCAN_CHUNKS

    _log_phase("structure.citations", t_phase, f"citations={cite_count}")
    # 4. paper_meta：检索列表与溯源都要它，最后一步写，避免"半成品"被消费
    t_phase = time.time()
    from app.services.s2_client import SemanticScholarClient
    from app.services.openalex_client import OpenAlexClient
    from app.services.crossref_client import CrossrefClient

    s2_graph = None
    openalex = None
    crossref = None

    # 外部元数据补全只对"有真实 arXiv ID / DOI"的论文有意义。
    # 本地上传的论文 id 是 upload_<hash>，去查 S2/OpenAlex/Crossref 必然 404 或
    # 长时间等超时——每本书因此在 structuring 阶段白白卡住 1~2 分钟。
    if not arxiv_id.startswith("upload_") or doi:
        if not arxiv_id.startswith("upload_"):
            try:
                s2_graph = SemanticScholarClient().fetch_citation_graph(arxiv_id)
            except Exception:
                s2_graph = None
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
        figure_keys=[],
        s2_graph=s2_graph,
        openalex=openalex,
        crossref=crossref,
        chapter_tree=chapter_tree,
        structured_references=structured_refs,
    )

    _log_phase("structure.paper_meta", t_phase)
    # 5. 参考文献回连课题内已入库论文
    try:
        ReferenceRepository.match_to_project(version_id, project_id)
    except Exception:
        traceback.print_exc()

    return body_hash, chunk_total, ref_count, cite_count


def _fallback_tree(headings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """章节树兜底：按字号把标题压成两级，至少让前端大纲有东西可展示。"""
    if not headings:
        return []
    sizes = sorted({round(h.get("font_size", 12), 1) for h in headings}, reverse=True)
    top = sizes[:2]
    out: list[dict[str, Any]] = []
    for h in headings:
        size = round(h.get("font_size", 12), 1)
        node = {
            "title": h["title"],
            "page": h.get("page", 0),
            "level": 1 if size == top[0] or len(top) == 1 else 2,
            "children": [],
        }
        if node["level"] == 1 or not out:
            out.append(node)
        else:
            out[-1]["children"].append(node)
    return out


def _fallback_refs(refs_text: str) -> list[dict[str, Any]]:
    """
    文献兜底解析：按行首编号切条。
    LLM 不可用（没配 key、限流、超长上下文）时至少保住"第 N 条是什么"，
    正文里的 [N] 才能落到对应条目上，不然引用位置全丢。
    """
    import re

    lines = [ln.strip() for ln in refs_text.splitlines() if ln.strip()]
    items: list[dict[str, Any]] = []
    current: Optional[str] = None
    for line in lines:
        m = re.match(r"^\[?\s*(\d{1,3})[\]\).、]\s*", line)
        if m:
            if current:
                items.append({"raw": current})
            current = line[m.end():] or line
        elif current:
            current += " " + line
        else:
            current = line
    if current:
        items.append({"raw": current})

    out = []
    for i, item in enumerate(items, start=1):
        raw = item["raw"]
        year = None
        ym = re.search(r"(19|20)\d{2}", raw)
        if ym:
            year = int(ym.group(0))
        out.append(
            {
                "index": i,
                "raw": raw,
                "title": raw[:180],
                "authors": [],
                "year": year,
                "venue": None,
                "doi": None,
                "arxiv_id": None,
            }
        )
    return out


# ---------- 主入口 ----------
def run_ingest(version_id: str, task_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """
    跑完一个版本的解析。可重入：已完成的版本直接返回；中断过的版本从游标继续。
    """
    started = time.time()
    version = PaperVersionRepository.get(version_id)
    if not version:
        raise IngestError(f"版本不存在: {version_id}", stage="queued")

    if not force and version.status == "indexed" and version.stage in ("indexed", "reused"):
        prog = PaperVersionRepository.get(version_id)
        return IngestOutcome(
            version_id=version_id,
            status="indexed",
            stage=prog.stage if prog else "indexed",
            pages=version.page_count or 0,
            chunks=version.chunk_count,
            refs=version.ref_count,
            citations=version.citation_count,
            skipped=True,
        ).to_dict()

    chain = PaperVersionRepository.resolve_chain(version_id)
    if not chain:
        raise IngestError("版本 → 论文 → 课题 链路不完整，拒绝解析", stage="queued")
    arxiv_id = chain["paper_arxiv_id"]
    version_label = chain["version_label"]
    title = chain["paper_title"]
    project_id = chain["project_id"]

    from app.repositories import PaperRepository

    paper = PaperRepository.get(chain["paper_id"])
    authors = paper.authors if paper else []
    abstract = paper.abstract if paper else ""
    doi = paper.doi if paper else None
    s3_key = version.pdf_s3_key or f"{arxiv_id}/{version_label}/paper.pdf"

    attempt = (version.attempt or 0) + 1
    PaperVersionRepository.advance(
        version_id,
        task_id=task_id or version.task_id,
        attempt=attempt,
        started_at=version.started_at or utc_now(),
        stage="downloading",
    )

    wd = work_dir(version_id)
    pdf_path = wd / "raw.pdf"
    storage = StorageService()

    try:
        # ---------- 1. 流式下载 ----------
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            size = storage.download_to_file(s3_key, str(pdf_path))
            if size <= 0:
                raise IngestError("PDF 下载为空，检查对象存储里的 s3_key", stage="downloading")
        file_size = pdf_path.stat().st_size
        if file_size > limits.MAX_PDF_BYTES:
            raise IngestError(
                f"PDF 体积 {file_size / 1048576:.0f}MB 超过上限 {limits.MAX_PDF_BYTES // 1048576}MB",
                stage="downloading",
            )

        doc = fitz.open(str(pdf_path))
        try:
            total_pages = len(doc)
        finally:
            doc.close()
        if total_pages == 0:
            raise IngestError("PDF 页数为 0", stage="downloading")
        if total_pages > limits.MAX_PAGES:
            raise IngestError(
                f"页数 {total_pages} 超过上限 {limits.MAX_PAGES}", stage="downloading"
            )

        PaperVersionRepository.advance(
            version_id, page_count=total_pages, stage="parsing"
        )

        # ---------- 2. 断点状态 ----------
        cursor = version.parse_cursor or 0
        index_cursor = version.index_cursor or 0
        body_path = wd / "body.txt"

        if cursor == 0:
            # 全新开始：清干净上一次的残留（改了解析器重跑也走这条）
            VectorStoreService().delete_chunks(arxiv_id, version_label)
            PaperVersionRepository.advance(
                version_id, progress_page=0, index_cursor=0, text_bytes=0, chunk_count=0
            )
            body_path.write_bytes(b"")
            index_cursor = 0
            _save_json(wd / "state.json", {"section": "Abstract", "in_references": False})
            (wd / "headings.json").unlink(missing_ok=True)
        else:
            # 续跑：把正文日志截到上次提交的位置，多出来的字节来自「写了但没提交」的那一批
            committed = version.text_bytes or 0
            if body_path.exists() and body_path.stat().st_size > committed:
                with open(body_path, "r+b") as fh:
                    fh.truncate(committed)

        state = _load_json(wd / "state.json", {"section": "Abstract", "in_references": False})
        headings: list[dict[str, Any]] = _load_json(wd / "headings.json", [])
        resumed_from = cursor

        # ---------- 3. 分批解析 + 分批入库 ----------
        # 锚点覆盖率是本步的核心指标：解析完就知道有多少片段能精确定位，
        # 而不是等前端点引用才发现高亮不出来。
        anchor_stats: dict[str, int] = {}
        use_vision = total_pages <= limits.LONG_DOC_PAGES
        # 向量库客户端在循环外建一次：模型是进程级单例，但客户端与 collection
        # 探测没必要每批做一遍
        vector_store = VectorStoreService()
        while cursor < total_pages:
            _check_memory("parsing")
            batch_end = min(cursor + limits.PAGES_PER_BATCH - 1, total_pages - 1)

            t_batch = time.time()
            fragments, page_texts, batch_headings, batch_stats = _parse_batch(
                pdf_path=pdf_path,
                start_page=cursor,
                end_page=batch_end,
                state=state,
                storage=storage,
                arxiv_id=arxiv_id,
                version=version_label,
                use_vision=use_vision,
            )
            t_parsed = time.time()
            # 供 _commit_batch 计算点 ID，避免把 arxiv/version 再透传两层
            for frag in fragments:
                frag["_arxiv_id"] = arxiv_id
                frag["_version"] = version_label

            # 先写向量库：upsert 是幂等的，失败就整批重试；
            # 关系库随后提交并推进游标，两者顺序不可颠倒（否则会漏批）。
            if fragments:
                vector_store.upsert_fragment_batch(
                    arxiv_id=arxiv_id,
                    version=version_label,
                    title=title,
                    body_hash="",
                    fragments=fragments,
                    index_offset=index_cursor,
                )

            t_indexed = time.time()
            blob = ("\n".join(t for t in page_texts if t) + "\n").encode("utf-8")
            with open(body_path, "ab") as fh:
                fh.write(blob)
            body_bytes = body_path.stat().st_size

            headings.extend(batch_headings)
            if len(headings) > limits.MAX_HEADINGS:
                headings = headings[: limits.MAX_HEADINGS]
            _save_json(wd / "headings.json", headings)
            _save_json(wd / "state.json", state)

            index_cursor_before = index_cursor
            index_cursor += len(fragments)
            _commit_batch(
                version_id=version_id,
                fragments=fragments,
                index_cursor=index_cursor_before,
                next_cursor=batch_end + 1,
                total_pages=total_pages,
                body_bytes=body_bytes,
                chunk_delta=len(fragments),
            )

            for key, value in batch_stats.items():
                anchor_stats[key] = anchor_stats.get(key, 0) + value

            _log_phase(
                f"batch pages {cursor}-{batch_end}",
                t_batch,
                f"parse={t_parsed - t_batch:.1f}s index={t_indexed - t_parsed:.1f}s "
                f"commit={time.time() - t_indexed:.1f}s frags={len(fragments)} rss={rss_mb() or 0:.0f}MB",
            )
            cursor = batch_end + 1
            # 显式释放本批所有大对象，防止 GC 拖到下一批才回收
            fragments = []
            page_texts = []

        # ---------- 4. 汇总 ----------
        PaperVersionRepository.advance(version_id, stage="structuring")
        _check_memory("structuring")
        body_hash, chunk_total, ref_count, cite_count = _structure(
            version_id=version_id,
            arxiv_id=arxiv_id,
            version=version_label,
            title=title,
            authors=authors or [],
            abstract=abstract or "",
            doi=doi,
            s3_key=s3_key,
            project_id=project_id,
            headings=headings,
            total_pages=total_pages,
        )

        current = PaperVersionRepository.get(version_id)
        if current and current.stage == "duplicate":
            return IngestOutcome(
                version_id=version_id,
                status="duplicate",
                stage="duplicate",
                pages=total_pages,
                elapsed_ms=int((time.time() - started) * 1000),
                resumed_from_page=resumed_from,
            ).to_dict()

        PaperVersionRepository.mark_indexed(
            version_id, page_count=total_pages, content_hash=body_hash
        )
        PaperVersionRepository.advance(
            version_id,
            stage="indexed",
            progress_page=total_pages,
            parse_cursor=total_pages,
            chunk_count=chunk_total,
            ref_count=ref_count,
            citation_count=cite_count,
            finished_at=utc_now(),
        )

        # 正文指纹：下一次上传要靠它判断"这条是不是同一篇的另一个版本"。
        # 放在解析完成之后，是因为它读的是 chunk（已结构化、已去掉页眉页脚），
        # 与上传时从 PDF 文本层算的指纹同源同算法。失败不回滚入库：
        # 指纹只是"下次上传才有用"的副产品，没它也能正常用，补跑 backfill 即可。
        try:
            from app.services.dedupe.service import get_service as _dedupe_service

            _dedupe_service().index_version(version_id)
        except Exception:
            logger.warning("版本 %s 指纹计算失败（不影响入库）", version_id, exc_info=True)

        # 解析成功后清掉工作目录：raw.pdf 有几十 MB，留着会撑爆卷
        shutil.rmtree(wd, ignore_errors=True)

        return IngestOutcome(
            version_id=version_id,
            status="indexed",
            stage="indexed",
            pages=total_pages,
            chunks=chunk_total,
            refs=ref_count,
            citations=cite_count,
            elapsed_ms=int((time.time() - started) * 1000),
            resumed_from_page=resumed_from,
            # 锚点覆盖率写进结果，验收时不用再单独跑一遍脚本：
            # anchor_chunks / (anchor_chunks + anchor_missing) 就是"能精确到句的比例"。
            extra={"anchors": anchor_stats},
        ).to_dict()

    except IngestMemoryError as exc:
        # 内存超限不重试：重试只会再撞一次，直接给终态并保留上下文
        PaperVersionRepository.mark_failed(version_id, str(exc), traceback.format_exc())
        raise
    except Exception as exc:
        trace = traceback.format_exc()
        message = f"{type(exc).__name__}: {exc}"
        if attempt >= limits.MAX_ATTEMPTS:
            PaperVersionRepository.mark_failed(version_id, message, trace)
        else:
            # 未超限：留在 pending 让 Celery 重试，游标保住了断点
            PaperVersionRepository.advance(
                version_id, error_message=message[:2000], error_trace=trace[:20000]
            )
        raise
