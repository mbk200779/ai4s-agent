"""
PDF 细粒度结构化解析：章节树、段落、图表、公式、参考文献。

两条入口：
- iter_pages(...)：逐页生成器，长文档（541 页教材）走这条。每次只持有一页的
  block 数据与页面图，读完即释放，所以 RSS 与文档页数无关。
- parse_range(...)：一次性返回一个页范围的聚合结果，短论文与旧调用方走这条。

05 版新增：每页额外产出 `lines`（行文本 + 行矩形）与 `text_layer` 标记。
它是句子级锚点的原料——锚点必须落在行级矩形上，而 chunk 只有块级 bbox。
`lines` 与 page 同生命周期，读完即释放，不改变内存曲线。
"""
import hashlib
import fitz  # PyMuPDF
import pdfplumber
from typing import Any, Dict, Iterator, List, Optional
from io import BytesIO

from app.services.ingest.limits import MAX_TEXT_CHARS_PER_PAGE


class PDFParser:
    """
    关键设计决策：
    - PyMuPDF 负责页面图、内嵌图像、字体/布局信息；
    - pdfplumber 负责精确文本块与表格检测；
    - 长文档按页范围分块解析，单任务只处理几十页，避免 541 页书 OOM。

    内存约束的实现方式：**不接受 bytes 作为长文档入口**。
    传 path 时 fitz 用 mmap 打开，只有当前页会被解码进内存；
    传 bytes 时整份 PDF 已经在内存里了，那就只适合短论文。
    """

    def __init__(
        self,
        pdf_source: bytes | str,
        ocr: bool = False,
        ocr_lang: str = "eng",
        ocr_dpi: int = 200,
    ):
        self.pdf_source = pdf_source
        # 只有显式打开才尝试 OCR：tesseract 不在镜像里时 get_textpage_ocr 会抛异常，
        # 每页都试一次的代价是几十毫秒 × 页数，且日志会被刷满。
        self.ocr = ocr
        self.ocr_lang = ocr_lang
        self.ocr_dpi = ocr_dpi

    # ---------- 打开文档 ----------
    def _open(self) -> "fitz.Document":
        if isinstance(self.pdf_source, str):
            return fitz.open(self.pdf_source)
        return fitz.open(stream=self.pdf_source, filetype="pdf")

    # ---------- 逐页流式入口 ----------
    def iter_pages(
        self,
        start_page: int,
        end_page: int,
        state: Optional[Dict[str, Any]] = None,
        render: bool = False,
        dpi: int = 150,
    ) -> Iterator[Dict[str, Any]]:
        """
        逐页产出，调用方每页处理完即丢弃。

        参数 state 是可变的跨批次状态（当前章节名、是否已进入 References 区）。
        分批解析时把 state 传进来并持久化，第二批才能接上第一批的章节上下文，
        否则每一批的前几段都会被错标成 "Abstract"。

        每页产出：
        - page: 1-based 页码
        - chunks / headings: 该页的段落与标题
        - text: 该页正文（用于累积 body_hash）
        - references_raw: 该页落在 References 区的文本
        - lines: 该页全部行的 {text, bbox}，按阅读顺序；锚点对齐用
        - page_size: [宽, 高]（PDF point），用于校验矩形是否越界/是否整页
        - text_layer: 是否本来就有文本层（False 表示走了 OCR 或纯扫描件）
        - ocr: 本页文本是否来自 OCR
        - png: render=True 时的页面图 bytes（调用方上传后立即释放）
        """
        doc = self._open()
        try:
            last = min(end_page, len(doc) - 1)
            if state is None:
                state = {"section": "Abstract", "in_references": False}

            for page_idx in range(start_page, last + 1):
                page = doc.load_page(page_idx)

                png = None
                if render:
                    png = page.get_pixmap(dpi=dpi).tobytes("png")

                chunks: List[Dict[str, Any]] = []
                headings: List[Dict[str, Any]] = []
                ref_parts: List[str] = []
                ref_lines: List[Dict[str, Any]] = []
                page_lines: List[Dict[str, Any]] = []
                text_budget = MAX_TEXT_CHARS_PER_PAGE

                page_dict, text_layer, ocr_used = self._page_dict(page)

                for block in page_dict["blocks"]:
                    if "lines" not in block:
                        continue
                    block_text = ""
                    block_lines: List[Dict[str, Any]] = []
                    font_sizes: List[float] = []
                    for line in block["lines"]:
                        line_text = "".join(s["text"] for s in line["spans"]).strip()
                        if not line_text:
                            continue
                        font_sizes.extend(span["size"] for span in line["spans"])
                        block_text += " " + line_text
                        if line.get("bbox"):
                            block_lines.append(
                                {
                                    "text": line_text,
                                    "bbox": [round(float(v), 2) for v in line["bbox"]],
                                }
                            )
                    block_text = block_text.strip()
                    if not block_text:
                        continue

                    # 参考文献区域检测：一旦进入就整页后续 block 都算文献区
                    lowered = block_text.lower()
                    if lowered.startswith("references") or lowered.startswith("bibliography"):
                        state["in_references"] = True
                        continue
                    if state["in_references"]:
                        ref_parts.append(block_text)
                        # 文献条目一条一行，锚点按行建（见 anchors.build_line_document）
                        ref_lines.extend(block_lines)
                        continue

                    if text_budget <= 0:
                        # 扫描版 PDF 的 OCR 结果可能整页糊成一坨，
                        # 截断而不是把它整段塞进 embedding，避免单页撑爆向量批次
                        break
                    text_budget -= len(block_text)

                    avg_font = sum(font_sizes) / len(font_sizes) if font_sizes else 12.0
                    is_heading = avg_font >= 14 or (
                        avg_font >= 12
                        and len(block_text) < 120
                        and not block_text.endswith((".", "?", "。", "？"))
                    )
                    if is_heading:
                        state["section"] = block_text
                        headings.append(
                            {
                                "title": block_text,
                                "page": page_idx + 1,
                                "font_size": round(avg_font, 1),
                            }
                        )

                    line_from = len(page_lines)
                    page_lines.extend(block_lines)
                    line_to = len(page_lines) - 1

                    chunks.append(
                        {
                            "text": block_text,
                            "section": state["section"],
                            "page": page_idx + 1,
                            "paragraph": len(chunks) + 1,
                            "font_size": round(avg_font, 1),
                            "kind": "paragraph",
                            # bbox 取本块所有行的并集，而不是 block["bbox"]：
                            # block 框偶尔带着栏间距的留白，行并集更贴文字。
                            "bbox": _union([ln["bbox"] for ln in block_lines])
                            or [round(float(v), 2) for v in (block.get("bbox") or [])],
                            # 行区间指向 page_lines。锚点对齐时用它把搜索范围
                            # 限定在本段的行内，避免同页多段互相抢行。
                            "line_from": line_from,
                            "line_to": line_to,
                            "lines": block_lines,
                        }
                    )

                merged = self._merge_chunks(chunks)
                yield {
                    "page": page_idx + 1,
                    "chunks": merged,
                    "headings": headings,
                    "text": "\n".join(c["text"] for c in merged),
                    "references_raw": "\n".join(ref_parts),
                    "references_lines": ref_lines,
                    "lines": page_lines,
                    "page_size": [
                        round(float(page.rect.width), 2),
                        round(float(page.rect.height), 2),
                    ],
                    "text_layer": text_layer,
                    "ocr": ocr_used,
                    "png": png,
                }

                # 显式释放：不依赖 GC 时机，长文档下 GC 来不及会明显看到 RSS 台阶
                page = None
                png = None
        finally:
            doc.close()

    # ---------- 文本层 / OCR ----------
    def _page_dict(self, page: "fitz.Page") -> tuple[Dict[str, Any], bool, bool]:
        """
        取本页的 dict 结构。返回 (dict, 是否有文本层, 是否走了 OCR)。

        判断"有没有文本层"不能用 `page.get_text()`：扫描件被别的工具做过 OCR 之后，
        页面上那层隐藏文字也算文本层，此时坐标和肉眼看到的位置能对上，锚点是可信的。
        真正没有文本层时 `get_text()` 一定是空的。
        """
        text_layer = bool(page.get_text().strip())
        if text_layer or not self.ocr:
            return page.get_text("dict"), text_layer, False
        try:
            textpage = page.get_textpage_ocr(
                flags=fitz.TEXT_PRESERVE_WHITESPACE,
                language=self.ocr_lang,
                dpi=self.ocr_dpi,
                full=True,
            )
        except Exception:
            # tesseract 没装 / 语言包缺失：这页就是没法出文字，交给上层记 scanned
            return page.get_text("dict"), False, False
        try:
            return page.get_text("dict", textpage=textpage), False, True
        except Exception:
            return page.get_text("dict"), False, False

    # ---------- 兼容入口：一次返回一个页范围 ----------
    def parse_full(self) -> Dict[str, Any]:
        """全文档解析（适合短篇论文）。"""
        return self.parse_range(0, self._page_count() - 1)

    def parse_range(self, start_page: int, end_page: int) -> Dict[str, Any]:
        """
        解析指定页范围 [start_page, end_page]（含），一次性返回聚合结果。
        保留这个入口是为了兼容旧调用方；它会把整段全文与所有页面图放在
        一个 dict 里返回，所以**不要**在长文档的主路径上用它。
        """
        chunks: List[Dict[str, Any]] = []
        headings: List[Dict[str, Any]] = []
        pages_png: List[bytes] = []
        references_raw_parts: List[str] = []
        text_parts: List[str] = []
        state = {"section": "Abstract", "in_references": False}

        for page in self.iter_pages(start_page, end_page, state=state, render=True):
            chunks.extend(page["chunks"])
            headings.extend(page["headings"])
            pages_png.append(page["png"])
            if page["references_raw"]:
                references_raw_parts.append(page["references_raw"])
            text_parts.append(page["text"])

        merged_chunks = self._merge_chunks(chunks)
        full_text = "\n".join(text_parts)
        body_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

        return {
            "full_text": full_text,
            "body_hash": body_hash,
            "chunks": merged_chunks,
            "headings": headings,
            "pages_png": pages_png,
            "references_raw": "\n".join(references_raw_parts),
            "start_page": start_page,
            "end_page": end_page,
        }

    def extract_tables_with_pdfplumber(
        self, start_page: int, end_page: int, with_cells: bool = False
    ) -> List[Dict[str, Any]]:
        """
        使用 pdfplumber 提取表格区域与文本。

        with_cells=True 时额外返回 `bbox`（整表）与 `cell_boxes`（每个单元格的矩形），
        句子级锚点在表格里要落到**单元格**，没有单元格矩形就只能整表高亮。

        这里用 find_tables() 而不是 extract_tables()：后者只吐文本网格，
        不吐几何信息，而表格恰好是"文本与几何必须一一对应"的场景。
        """
        tables: List[Dict[str, Any]] = []
        source = (
            self.pdf_source
            if isinstance(self.pdf_source, str)
            else BytesIO(self.pdf_source)
        )
        with pdfplumber.open(source) as pdf:
            end_page = min(end_page, len(pdf.pages) - 1)
            for idx in range(start_page, end_page + 1):
                page = pdf.pages[idx]
                if not with_cells:
                    for t_idx, table in enumerate(page.extract_tables() or [], start=1):
                        tables.append({
                            "page": idx + 1,
                            "index": t_idx,
                            "cells": table,
                            "text": "\n".join([" | ".join(str(c) if c else "" for c in row) for row in table]),
                        })
                    continue

                for t_idx, table in enumerate(page.find_tables() or [], start=1):
                    grid = table.extract() or []
                    boxes = [[cell for cell in row.cells] for row in table.rows]
                    text, cell_items = _table_text_and_cells(grid, boxes)
                    tables.append({
                        "page": idx + 1,
                        "index": t_idx,
                        "cells": grid,
                        "text": text,
                        "bbox": [round(float(v), 2) for v in table.bbox],
                        "cell_items": cell_items,
                    })
        return tables

    def _page_count(self) -> int:
        doc = self._open()
        try:
            return len(doc)
        finally:
            doc.close()

    @staticmethod
    def _merge_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """合并相邻同章节片段，过滤过短噪声。"""
        if not chunks:
            return []
        merged = [dict(chunks[0])]
        for cur in chunks[1:]:
            prev = merged[-1]
            if cur["section"] == prev["section"] and cur["page"] == prev["page"]:
                prev["text"] += " " + cur["text"]
                # 行区间取并集：锚点对齐要覆盖合并后的全部行，
                # 否则后并入的那块文字永远找不到自己的矩形。
                prev["line_to"] = max(prev.get("line_to", -1), cur.get("line_to", -1))
                prev["lines"] = list(prev.get("lines") or []) + list(cur.get("lines") or [])
                prev["bbox"] = _union([prev.get("bbox"), cur.get("bbox")]) or prev.get("bbox")
            else:
                merged.append(dict(cur))
        return [m for m in merged if len(m["text"]) >= 40]


def _union(rects: List[Any]) -> Optional[List[float]]:
    """多个矩形的并集；全空时返回 None。"""
    valid = [r for r in rects if r and len(r) == 4]
    if not valid:
        return None
    return [
        round(min(r[0] for r in valid), 2),
        round(min(r[1] for r in valid), 2),
        round(max(r[2] for r in valid), 2),
        round(max(r[3] for r in valid), 2),
    ]


def _table_text_and_cells(
    grid: List[List[Any]], boxes: List[List[Any]]
) -> tuple[str, List[Dict[str, Any]]]:
    """
    把表格网格拼成文本，同时记下每个单元格在文本里的字符区间。

    文本格式必须与旧实现逐字一致（`" | "` 连列、`"\\n"` 连行），
    否则表格片段的 embedding 会变、历史检索结果对不上。

    单元格为空但矩形存在时也留一条（start == end），让上层自己决定要不要出锚点：
    空单元格没有文字可高亮，但它在网格里的位置对"这张表长什么样"是有信息的。
    """
    parts: List[str] = []
    cells: List[Dict[str, Any]] = []
    cursor = 0
    for r_i, row in enumerate(grid):
        row_boxes = boxes[r_i] if r_i < len(boxes) else []
        for c_i, value in enumerate(row):
            text = str(value) if value is not None else ""
            start = cursor
            parts.append(text)
            cursor += len(text)
            box = row_boxes[c_i] if c_i < len(row_boxes) else None
            cells.append(
                {
                    "row": r_i + 1,
                    "col": c_i + 1,
                    "start": start,
                    "end": cursor,
                    "bbox": [round(float(v), 2) for v in box] if box else None,
                    "text": text,
                }
            )
            if c_i < len(row) - 1:
                sep = " | "
                parts.append(sep)
                cursor += len(sep)
        if r_i < len(grid) - 1:
            parts.append("\n")
            cursor += 1
    # 与 _commit_batch 里的 text.strip() 保持一致：不一致会让锚点区间整体偏移一格，
    # 校验脚本报 offset_range，前端高亮错一个字符。
    return "".join(parts).strip(), cells
