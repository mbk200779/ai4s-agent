"""
绘图辅助服务：基于论文数据/结论生成可复现的 matplotlib / mermaid 图表。
关键设计决策：用【代码生成】而非文生图模型，配合输入哈希缓存，
保证同输入同输出、可溯源、可导出 PNG/SVG。
"""

import ast
import base64
import hashlib
import io
import json
import math
import traceback
import zlib
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import requests

from app.services.llm_client import LLMClient
from app.services.search_service import SearchService
from app.services.storage import StorageService
from app.schemas.drawing import DrawingRequest, DrawingResponse, SourceInfo


matplotlib.use("Agg")


class _AllowedCodeVisitor(ast.NodeVisitor):
    """AST 白名单：仅允许 matplotlib / numpy 相关导入与安全的内置函数。"""

    ALLOWED_TOP = {"matplotlib", "numpy"}
    DISALLOWED_NAMES = {"__import__", "open", "exec", "eval", "compile", "input", "exit", "quit"}

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top not in self.ALLOWED_TOP:
                raise ValueError(f"禁止导入模块：{alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            top = node.module.split(".")[0]
            if top not in self.ALLOWED_TOP:
                raise ValueError(f"禁止导入模块：{node.module}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id in self.DISALLOWED_NAMES:
            raise ValueError(f"禁止使用的名称：{node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "__builtins__":
            raise ValueError("禁止访问 __builtins__")
        self.generic_visit(node)


class DrawingService:
    """
    关键设计决策：
    1. 生成 matplotlib Python 或 mermaid 文本代码，并在受限环境（AST 白名单 + 安全 builtins）中执行 matplotlib；
    2. 用请求内容的 SHA256 作为缓存键，已生成的图片落 MinIO figures bucket，保证同请求同输出；
    3. 图表必须带数据来源标注（arXiv ID / 页码）， provenance 日志记录代码与来源。
    """

    ALLOWED_CHART_TYPES = {
        "matplotlib_line",
        "matplotlib_bar",
        "matplotlib_scatter",
        "matplotlib_histogram",
        "mermaid_flowchart",
        "mermaid_sequence",
        "mermaid_mindmap",
    }

    # 给 matplotlib 代码执行时开放的安全 builtins
    SAFE_BUILTINS: Dict[str, Any] = {
        "abs": abs,
        "all": all,
        "any": any,
        "bin": bin,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "hex": hex,
        "int": int,
        "iter": iter,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "next": next,
        "oct": oct,
        "ord": ord,
        "pow": pow,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "math": math,
    }

    def __init__(self):
        self.search = SearchService()
        self.llm = LLMClient()
        self.storage = StorageService()

    def generate(self, req: DrawingRequest) -> DrawingResponse:
        """入口：生成代码、渲染图片、写入 MinIO、返回下载链接。"""
        if req.chart_type not in self.ALLOWED_CHART_TYPES:
            return self._reject(f"不支持的图表类型：{req.chart_type}")

        # 1. 检索支撑片段（可选）
        chunks: List[Dict[str, Any]] = []
        if req.query:
            chunks = self.search.retrieve(req.query, top_k=req.top_k)

        sources = self._build_sources(chunks, req.sources)
        source_annotation = self._format_source_annotation(sources)
        cache_key = self._cache_key(req, sources)

        try:
            if req.chart_type.startswith("mermaid"):
                code_language = "mermaid"
                code = self._generate_mermaid(req, source_annotation)
                png_key, svg_key = self._render_mermaid(code, cache_key, req.formats)
            else:
                code_language = "python"
                code = self._generate_matplotlib(req, source_annotation)
                png_key, svg_key = self._render_matplotlib(code, cache_key, req.formats)
        except Exception as e:
            traceback.print_exc()
            return self._reject(f"渲染失败：{e}")

        # 2. 把代码本身也存一份，保证可复现
        code_ext = "mmd" if code_language == "mermaid" else "py"
        code_key = f"drawings/{cache_key}/code.{code_ext}"
        self.storage.upload_figure(code_key, code.encode("utf-8"), content_type="text/plain")

        # 3. provenance 日志
        provenance = {
            "chart_type": req.chart_type,
            "title": req.title,
            "description": req.description,
            "data": req.data,
            "query": req.query,
            "sources": [s.model_dump() for s in sources],
            "code": code,
            "code_language": code_language,
            "png_key": png_key,
            "svg_key": svg_key,
            "cache_key": cache_key,
        }
        provenance_key = f"drawings/{cache_key}/provenance.json"
        self.storage.upload_provenance(
            provenance_key,
            json.dumps(provenance, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

        return DrawingResponse(
            chart_type=req.chart_type,
            title=req.title,
            code=code,
            code_language=code_language,
            image_url_png=self._url(png_key),
            image_url_svg=self._url(svg_key),
            sources=sources,
            cache_key=cache_key,
            provenance_key=provenance_key,
        )

    def _build_sources(
        self, chunks: List[Dict[str, Any]], explicit_ids: List[str]
    ) -> List[SourceInfo]:
        """合并检索片段与显式来源，去重。"""
        sources: List[SourceInfo] = []
        seen: set = set()
        for c in chunks:
            sid = c.get("arxiv_id") or ""
            page = c.get("page")
            para = c.get("paragraph")
            key = (sid, page, para)
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                SourceInfo(
                    arxiv_id=sid or None,
                    title=c.get("title") or None,
                    page=page,
                    paragraph=para,
                    text=(c.get("text") or "")[:300],
                )
            )
        for sid in explicit_ids:
            if sid and sid not in {s.arxiv_id for s in sources}:
                sources.append(SourceInfo(arxiv_id=sid))
        return sources

    @staticmethod
    def _format_source_annotation(sources: List[SourceInfo]) -> str:
        """把来源列表转成一句图表标注。"""
        if not sources:
            return "Source: AI4S Agent generated"
        parts = []
        for s in sources:
            label = f"arXiv:{s.arxiv_id}" if s.arxiv_id else "未知来源"
            if s.page is not None:
                label += f" p.{s.page}"
            parts.append(label)
        return "Source: " + "; ".join(parts)

    @staticmethod
    def _cache_key(req: DrawingRequest, sources: List[SourceInfo]) -> str:
        """对请求与来源做确定性哈希，作为缓存键。"""
        payload = {
            "chart_type": req.chart_type,
            "title": req.title,
            "description": req.description,
            "data": req.data,
            "query": req.query,
            "sources": sorted(
                {
                    s.arxiv_id
                    for s in sources
                    if s.arxiv_id
                }
            ),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def _generate_matplotlib(self, req: DrawingRequest, source_annotation: str) -> str:
        """调用 LLM 生成 matplotlib Python 代码。"""
        subtype = req.chart_type.replace("matplotlib_", "")
        data_json = json.dumps(req.data, ensure_ascii=False, sort_keys=True) if req.data is not None else "null"
        prompt = (
            f"请生成一段完整的 matplotlib Python 代码，用于绘制一个可复现的科研 {subtype} 图。\n\n"
            "要求：\n"
            "- 只使用 matplotlib 和 numpy；不要调用 plt.show()；不要包含文件 I/O、网络请求或系统调用。\n"
            "- 若需要随机数据，必须显式设置 np.random.seed(42)。\n"
            "- 优先使用下面提供的结构化 data；若 data 为空，则根据描述构造合理的示例数据。\n"
            "- 图表标题、轴标签、图例清晰；在图中明显位置添加数据来源标注。\n"
            "- 代码生成后可通过 plt.gcf() 获取当前 Figure。\n"
            "- 只输出纯 Python 代码，不要 Markdown 代码块，不要解释。\n\n"
            f"标题：{req.title}\n"
            f"描述：{req.description}\n"
            f"数据来源标注：{source_annotation}\n"
            f"结构化 data：{data_json}\n"
        )
        code = self.llm.chat(prompt, system_key="drawing_system")
        return self._clean_code(code)

    def _generate_mermaid(self, req: DrawingRequest, source_annotation: str) -> str:
        """调用 LLM 生成 mermaid 文本。"""
        subtype = req.chart_type.replace("mermaid_", "")
        data_json = json.dumps(req.data, ensure_ascii=False, sort_keys=True) if req.data is not None else "null"
        prompt = (
            f"请生成一段完整的 Mermaid {subtype} 代码。\n\n"
            "要求：\n"
            "- 只输出 Mermaid 语法，不要 Markdown 代码块，不要解释。\n"
            "- 图表标题和节点标签清晰；在图中以注释形式添加数据来源标注。\n"
            "- 不使用随机布局之外的不可控元素。\n\n"
            f"标题：{req.title}\n"
            f"描述：{req.description}\n"
            f"数据来源标注：{source_annotation}\n"
            f"结构化 data：{data_json}\n"
        )
        return self._clean_code(self.llm.chat(prompt, system_key="drawing_system"))

    @staticmethod
    def _clean_code(code: str) -> str:
        """去掉 LLM 可能包裹的 markdown 代码块。"""
        code = code.strip()
        if code.startswith("```"):
            lines = code.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines).strip()
        return code

    def _render_matplotlib(
        self, code: str, cache_key: str, formats: List[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """在受限环境中执行 matplotlib 代码并导出 PNG/SVG。"""
        tree = ast.parse(code)
        _AllowedCodeVisitor().visit(tree)

        safe_globals: Dict[str, Any] = {
            "__builtins__": self.SAFE_BUILTINS,
            "matplotlib": matplotlib,
            "plt": plt,
            "numpy": np,
            "np": np,
        }
        exec(compile(tree, "<drawing>", "exec"), safe_globals)

        fig = plt.gcf()
        fig.tight_layout()

        png_key: Optional[str] = None
        svg_key: Optional[str] = None

        if "png" in formats:
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
            buf.seek(0)
            png_key = f"drawings/{cache_key}/chart.png"
            self.storage.upload_figure(png_key, buf.getvalue(), content_type="image/png")

        if "svg" in formats:
            matplotlib.rcParams["svg.hashsalt"] = "ai4s"
            buf = io.BytesIO()
            fig.savefig(buf, format="svg", bbox_inches="tight")
            buf.seek(0)
            svg_key = f"drawings/{cache_key}/chart.svg"
            self.storage.upload_figure(svg_key, buf.getvalue(), content_type="image/svg+xml")

        plt.close(fig)
        return png_key, svg_key

    def _render_mermaid(
        self, code: str, cache_key: str, formats: List[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """通过 mermaid.ink 渲染 mermaid 代码为 PNG/SVG。"""
        png_key: Optional[str] = None
        svg_key: Optional[str] = None

        if "png" in formats:
            png_key = f"drawings/{cache_key}/chart.png"
            data = self._fetch_mermaid_image(code, "img")
            self.storage.upload_figure(png_key, data, content_type="image/png")

        if "svg" in formats:
            svg_key = f"drawings/{cache_key}/chart.svg"
            data = self._fetch_mermaid_image(code, "svg")
            self.storage.upload_figure(svg_key, data, content_type="image/svg+xml")

        return png_key, svg_key

    @staticmethod
    def _fetch_mermaid_image(code: str, ext: str) -> bytes:
        """请求 mermaid.ink；先尝试原始 base64，失败则使用 pako 压缩。"""
        encoded = base64.urlsafe_b64encode(code.encode("utf-8")).decode("utf-8").rstrip("=")
        urls = [
            f"https://mermaid.ink/{ext}/{encoded}",
        ]
        compressed = base64.urlsafe_b64encode(zlib.compress(code.encode("utf-8"), level=9)).decode("utf-8").rstrip("=")
        urls.append(f"https://mermaid.ink/{ext}/pako:{compressed}")

        last_err: Optional[Exception] = None
        for url in urls:
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                last_err = e
        raise RuntimeError(f"mermaid.ink 渲染失败：{last_err}")

    def _url(self, key: Optional[str]) -> Optional[str]:
        if not key:
            return None
        return self.storage.get_presigned_url(self.storage.figures_bucket, key)

    def _reject(self, reason: str) -> DrawingResponse:
        return DrawingResponse(
            chart_type="",
            title="",
            code="",
            code_language="",
            sources=[],
            cache_key="",
            provenance_key="",
            rejected=True,
            reject_reason=reason,
        )
