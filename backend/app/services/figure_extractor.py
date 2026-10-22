"""
图表视觉抽取：从 PDF 页面中提取图片/表格区域，上传 MinIO，并用多模态模型生成说明。
"""
import hashlib
from typing import List, Dict, Any, Optional, Tuple
import fitz
from app.services.storage import StorageService
from app.services.llm_client import LLMClient


class FigureExtractor:
    """
    关键设计决策：先用 PyMuPDF 枚举页面内嵌图像的 bbox 并裁剪，
    再通过就近文本块匹配标题（Fig./Table），避免全页 OCR 带来的噪声与成本。
    """

    def __init__(self):
        self.storage = StorageService()
        self.llm = LLMClient()

    def extract_from_page(
        self, page: fitz.Page, page_idx: int, arxiv_id: str, version: str
    ) -> List[Dict[str, Any]]:
        """
        从单页提取图表对象。
        返回列表：{key, type, page, bbox, caption, s3_url?}
        """
        figures: List[Dict[str, Any]] = []
        image_list = page.get_images(full=True)
        text_blocks = [b for b in page.get_text("blocks") if b[6] == 0]  # type 0 = text

        for img_index, img in enumerate(image_list, start=1):
            xref = img[0]
            try:
                pix = fitz.Pixmap(page.parent, xref)
                if pix.n > 4:  # CMYK -> RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_bytes = pix.tobytes("png")
            except Exception:
                continue

            # 通过 image bbox 匹配最近文本块作为 caption
            img_bbox = page.get_image_bbox(img)
            caption = self._find_nearest_caption(text_blocks, img_bbox, page.rect)

            key = f"{arxiv_id}/{version}/figures/page_{page_idx:04d}_fig_{img_index:03d}.png"
            self.storage.upload_figure(key, img_bytes, content_type="image/png")

            # 生成中文说明（可选；若已有 caption 可跳过 LLM）
            description = caption or self.llm.vision(
                img_bytes, self.llm._system_prompt("figure_caption")
            )

            figures.append({
                "type": "figure",
                "key": key,
                "page": page_idx,
                "bbox": list(img_bbox),
                "caption": caption,
                "description": description,
            })
        return figures

    @staticmethod
    def _find_nearest_caption(
        text_blocks: List[Tuple],
        img_bbox: fitz.Rect,
        page_rect: fitz.Rect,
    ) -> Optional[str]:
        """
        寻找距离图片最近的、包含 Fig./Table 的文本块作为 caption。
        """
        candidates = []
        for b in text_blocks:
            text = b[4].strip()
            if not text:
                continue
            lower = text.lower()
            if "fig." in lower or "figure" in lower or "table" in lower:
                block_rect = fitz.Rect(b[:4])
                dist = abs(block_rect.y1 - img_bbox.y0)
                candidates.append((dist, text, block_rect.y0 > img_bbox.y0))
        if not candidates:
            return None
        # 优先图片下方的 caption
        below = [c for c in candidates if c[2]]
        if below:
            return min(below, key=lambda x: x[0])[1]
        return min(candidates, key=lambda x: x[0])[1]
