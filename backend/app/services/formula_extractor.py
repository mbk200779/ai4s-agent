"""
公式视觉抽取：检测公式区域，通过多模态模型生成 LaTeX。
"""
import re
from typing import List, Dict, Any, Optional
import fitz
from app.services.storage import StorageService
from app.services.llm_client import LLMClient


class FormulaExtractor:
    """
    关键设计决策：先用规则定位含数学符号的文本行/块，再将其所在区域渲染为图片，
    调用 vision LLM 转成 LaTeX；避免对全页做 OCR，降低成本与幻觉。
    """

    MATH_RE = re.compile(r"[\\\^_∑∫∂∞∈∀∃∇√α-ωΑ-Ω]|\$[^$]+\$")

    def __init__(self):
        self.storage = StorageService()
        self.llm = LLMClient()

    def extract_from_page(
        self, page: fitz.Page, page_idx: int, arxiv_id: str, version: str
    ) -> List[Dict[str, Any]]:
        """从单页提取公式对象。"""
        formulas: List[Dict[str, Any]] = []
        blocks = page.get_text("dict")["blocks"]
        page_rect = page.rect

        formula_index = 0
        for block in blocks:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                line_text = "".join(s["text"] for s in line["spans"]).strip()
                if not self.MATH_RE.search(line_text):
                    continue
                # 以行 bbox 为公式区域
                bbox = fitz.Rect(line["bbox"])
                # 留边距
                bbox = bbox + (-2, -2, 2, 2)
                bbox.intersect(page_rect)
                if bbox.is_empty:
                    continue

                clip = page.get_pixmap(clip=bbox, dpi=200)
                img_bytes = clip.tobytes("png")
                formula_index += 1

                latex = self.llm.vision(
                    img_bytes, self.llm._system_prompt("formula_latex")
                ).strip()

                key = f"{arxiv_id}/{version}/formulas/page_{page_idx:04d}_formula_{formula_index:03d}.png"
                self.storage.upload_figure(key, img_bytes, content_type="image/png")

                formulas.append({
                    "type": "formula",
                    "key": key,
                    "page": page_idx,
                    "bbox": list(bbox),
                    "raw_text": line_text,
                    "latex": latex,
                })
        return formulas
