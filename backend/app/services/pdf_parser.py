"""
PDF 细粒度结构化解析：章节树、段落、图表、公式、参考文献。
"""
import hashlib
import fitz  # PyMuPDF
import pdfplumber
from typing import List, Dict, Any, Optional
from io import BytesIO


class PDFParser:
    """
    关键设计决策：
    - PyMuPDF 负责页面图、内嵌图像、字体/布局信息；
    - pdfplumber 负责精确文本块与表格检测；
    - 长文档按页范围分块解析，单任务只处理几十页，避免 541 页书 OOM。
    """

    def __init__(self, pdf_bytes: bytes):
        self.pdf_bytes = pdf_bytes

    def parse_full(self) -> Dict[str, Any]:
        """全文档解析（适合短篇论文）。"""
        return self.parse_range(0, self._page_count() - 1)

    def parse_range(self, start_page: int, end_page: int) -> Dict[str, Any]:
        """
        解析指定页范围 [start_page, end_page]（含）。
        返回：
        - full_text: 该范围全文
        - body_hash: 全文 hash
        - chunks: 段落片段（带 page/paragraph/section）
        - headings: 标题列表（用于章节树）
        - pages_png: 页面截图 bytes
        - references_raw: References 区域原始文本
        """
        doc = fitz.open(stream=self.pdf_bytes, filetype="pdf")
        end_page = min(end_page, len(doc) - 1)

        chunks: List[Dict[str, Any]] = []
        headings: List[Dict[str, Any]] = []
        pages_png: List[bytes] = []
        references_raw_parts: List[str] = []
        current_section = "Abstract"
        in_references = False

        for page_idx in range(start_page, end_page + 1):
            page = doc.load_page(page_idx)
            pix = page.get_pixmap(dpi=150)
            pages_png.append(pix.tobytes("png"))

            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if "lines" not in block:
                    continue
                block_text = ""
                block_font_sizes = []
                for line in block["lines"]:
                    line_text = "".join(s["text"] for s in line["spans"]).strip()
                    if not line_text:
                        continue
                    for span in line["spans"]:
                        block_font_sizes.append(span["size"])
                    block_text += " " + line_text
                block_text = block_text.strip()
                if not block_text:
                    continue

                # 参考文献区域检测
                lower = block_text.lower()
                if lower.startswith("references") or lower.startswith("bibliography"):
                    in_references = True
                    continue
                if in_references:
                    references_raw_parts.append(block_text)
                    continue

                # 标题检测：字体较大或短行
                avg_font = sum(block_font_sizes) / len(block_font_sizes) if block_font_sizes else 12
                is_heading = avg_font >= 14 or (avg_font >= 12 and len(block_text) < 120 and not block_text.endswith((".", "?")))
                if is_heading:
                    current_section = block_text
                    headings.append({
                        "title": block_text,
                        "page": page_idx + 1,
                        "font_size": avg_font,
                    })

                chunk = {
                    "text": block_text,
                    "section": current_section,
                    "page": page_idx + 1,
                    "paragraph": len(chunks) + 1,
                    "font_size": avg_font,
                    "kind": "paragraph",
                    "bbox": block.get("bbox"),
                }
                chunks.append(chunk)

        merged_chunks = self._merge_chunks(chunks)
        full_text = "\n".join(c["text"] for c in merged_chunks)
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
        self, start_page: int, end_page: int
    ) -> List[Dict[str, Any]]:
        """
        使用 pdfplumber 提取表格区域与文本。
        """
        tables: List[Dict[str, Any]] = []
        with pdfplumber.open(BytesIO(self.pdf_bytes)) as pdf:
            end_page = min(end_page, len(pdf.pages) - 1)
            for idx in range(start_page, end_page + 1):
                page = pdf.pages[idx]
                for t_idx, table in enumerate(page.extract_tables() or [], start=1):
                    tables.append({
                        "page": idx + 1,
                        "index": t_idx,
                        "cells": table,
                        "text": "\n".join([" | ".join(str(c) if c else "" for c in row) for row in table]),
                    })
        return tables

    def _page_count(self) -> int:
        doc = fitz.open(stream=self.pdf_bytes, filetype="pdf")
        return len(doc)

    @staticmethod
    def _merge_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """合并相邻同章节片段，过滤过短噪声。"""
        if not chunks:
            return []
        merged = [chunks[0].copy()]
        for cur in chunks[1:]:
            prev = merged[-1]
            if cur["section"] == prev["section"] and cur["page"] == prev["page"]:
                prev["text"] += " " + cur["text"]
            else:
                merged.append(cur.copy())
        return [m for m in merged if len(m["text"]) >= 40]
