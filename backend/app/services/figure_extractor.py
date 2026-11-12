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
        self,
        page: fitz.Page,
        page_idx: int,
        arxiv_id: str,
        version: str,
        allow_vision: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        从单页提取图表对象。
        返回列表：{key, type, page, bbox, caption, s3_url?}

        allow_vision=False 时跳过多模态描述，只用就近 caption。
        长文档（教材）一张图一次 vision 会跑成几小时，且图注本来就自带说明，
        所以超过 LONG_DOC_PAGES 的文档默认走纯 caption 路径。
        """
        figures: List[Dict[str, Any]] = []
        image_list = page.get_images(full=True)
        text_blocks = [b for b in page.get_text("blocks") if b[6] == 0]  # type 0 = text
        page_area = abs(page.rect)
        page_has_text = bool(page.get_text().strip())

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

            # 整页扫描图不是「配图」，它就是这一页本身：
            # 1) 扫描件没有文本层，整页图会被当成分图送去视觉模型，模型往往回一段
            #    「当然，可以帮您撰写图表说明……」的客套话，最后被当成正文片段入索引；
            # 2) 正文 PDF 里的整页大图（跨栏图、整页表）同理，送视觉模型也是浪费额度。
            # 面积占比 ≥ 0.9 或本页无文本层，一律跳过，正文交给 OCR 路径。
            if page_area > 0 and abs(img_bbox) / page_area >= 0.9:
                continue
            if not page_has_text:
                continue

            caption = self._find_nearest_caption(text_blocks, img_bbox, page.rect)

            key = f"{arxiv_id}/{version}/figures/page_{page_idx:04d}_fig_{img_index:03d}.png"
            self.storage.upload_figure(key, img_bytes, content_type="image/png")

            # 生成中文说明（可选；若已有 caption 可跳过 LLM）
            if caption:
                description = caption
            elif allow_vision:
                description = self.llm.vision(
                    img_bytes, self.llm._system_prompt("figure_caption")
                )
            else:
                description = None

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
