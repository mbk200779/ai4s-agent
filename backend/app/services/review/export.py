"""
综述 Markdown 导出。

导出文件是**离开产品之后**唯一的载体。正文里的 `[引#N]` 在页面内可以点开，
存到本地就只剩一个编号；所以导出时必须把编号接回「哪篇论文、第几页、哪一节」，
否则读者拿到的是一篇无法追溯的稿子。

文件名有两处坑，都会让下载直接失败：

1. HTTP 头只能装 latin-1，`filename=综述.md` 会在编码响应头时抛
   `UnicodeEncodeError`，整个请求变成 500。必须按 RFC 6266/5987 同时给
   ASCII 兜底名和 `filename*=UTF-8''<percent-encoded>`。
2. Windows 文件名禁用 `\\ / : * ? " < > |`，主题里带个冒号（"RAG: 综述"）
   就写不进磁盘。这些字符在落到文件名之前先换成下划线。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from app.services.review.generate import CITE_RE

# Windows / POSIX 都不接受或会截断的字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_MULTI_DOT = re.compile(r"\.{2,}")
_ARXIV_HEAD = re.compile(r"^arxiv:\s*\d{4}\.\d{4,5}", re.I)
_MAX_STEM = 80
_MAX_STEM_BYTES = 120  # 按字节算：一个汉字 3 字节，按字符截会让文件名在 ext4 上超 255 字节


def _truncate_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")

KIND_LABELS = {
    # 入库时 kinds 来自 pipeline._chunk_type / frag["kind"]，两套写法都出现过
    "paragraph": "正文",
    "chunk": "正文",
    "text": "正文",
    "chapter": "章节",
    "table": "表格",
    "figure": "图表",
    "caption": "图注",
    "formula": "公式",
    "reference": "参考文献",
}


def safe_stem(topic: str) -> str:
    """主题 → 可落盘的文件名主干（不含扩展名）。"""
    stem = _ILLEGAL.sub("_", (topic or "").strip())
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"_{2,}", "_", stem)
    stem = _MULTI_DOT.sub(".", stem).strip("._-")
    stem = _truncate_bytes(stem, _MAX_STEM_BYTES)
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM]
    stem = stem.rstrip("._-")
    return stem or "review"


def content_disposition(topic: str) -> str:
    """
    同时给 `filename` 与 `filename*`：老客户端读前者，新客户端读后者。

    ASCII 兜底名带日期，避免多个综述存到同一目录时被系统自动改名成 `(1)` `(2)`。
    """
    stem = safe_stem(topic)
    ascii_name = f"review-{datetime.now().strftime('%Y%m%d')}.md"
    utf8_name = quote(f"{stem}.md", safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


def _cell(value: Any) -> str:
    """表格单元转义：竖线会截断列，换行会截断行。"""
    text = str(value or "").replace("|", "\\|")
    return re.sub(r"\s*\n+\s*", " ", text).strip()


def _location(ref: dict[str, Any]) -> str:
    parts: list[str] = []
    page = ref.get("page") or 0
    if page:
        parts.append(f"p.{page}")
    section = (ref.get("section") or "").strip()
    if section and not _ARXIV_HEAD.match(section):
        # 有的论文首页 section 抓到的是 arXiv 头（`arXiv:1706.03762v7 [cs.CL]`），
        # 原样塞进表格只是把页码又说了一遍
        parts.append(section if len(section) <= 40 else section[:40] + "…")
    return " · ".join(parts) or "—"


def _cited_numbers(markdown: str) -> list[int]:
    """正文里真正出现的编号，升序——附录是查表用的，不是阅读顺序。"""
    return sorted({int(m.group(1)) for m in CITE_RE.finditer(markdown)})


def build_export(
    topic: str,
    markdown: str,
    refs: Sequence[dict[str, Any]] | Iterable[dict[str, Any]] | None = None,
) -> str:
    """拼出最终交付的 Markdown：正文 + 引用对照附录。"""
    body = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        body = f"# {topic.strip()}\n\n> 本次综述没有生成正文。"

    by_n = {int(r.get("n")): r for r in (refs or []) if r.get("n") is not None}
    cited = _cited_numbers(body)

    if not cited:
        return body + "\n"

    lines = [body, "", "## 附：引用对照（系统生成）", ""]
    lines.append(
        "> 正文中 `[引#N]` 的落点，编号与生成时一致、按编号升序排列。"
        "系统附录，不属于文献结论。"
    )
    lines.append("")
    lines.append("| 编号 | 论文 | 位置 | 片段类型 |")
    lines.append("| --- | --- | --- | --- |")
    missing = 0
    for n in cited:
        ref = by_n.get(n)
        if not ref:
            missing += 1
            lines.append(f"| 引#{n} | （未记录） | — | — |")
            continue
        title = (ref.get("title") or "").strip()
        arxiv_id = (ref.get("arxiv_id") or "").strip()
        # 手工上传的论文 ID 是 upload_xxx，写进文献表只是噪音
        shown_id = "" if arxiv_id.startswith("upload_") else arxiv_id
        if title and shown_id:
            title = f"{title} (arXiv:{shown_id})"
        elif shown_id:
            # 没标题时别写成 "arXiv:2005.14165 (arXiv:2005.14165)"
            title = f"arXiv:{shown_id}"
        elif not title:
            title = "（未记录）"
        kind = KIND_LABELS.get((ref.get("kind") or "").lower(), ref.get("kind") or "—")
        lines.append(f"| 引#{n} | {_cell(title)} | {_cell(_location(ref))} | {_cell(kind)} |")

    if missing:
        # 少编号不是失败，但必须写出来——默默少一行等于让读者以为那句话没有出处
        lines.append("")
        lines.append(f"> 其中 {missing} 处编号在本次生成记录里没有对应原文，已标注「未记录」。")
    lines.append("")
    return "\n".join(lines)


def encode(markdown: str) -> bytes:
    """带 BOM 的 UTF-8：Windows 记事本靠它区分 UTF-8 与 ANSI，否则中文全是乱码。"""
    return b"\xef\xbb\xbf" + markdown.encode("utf-8")
