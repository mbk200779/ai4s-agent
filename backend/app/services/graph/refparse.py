"""
参考文献区重抽取：按 PDF 的 block 边界切条目，不依赖 LLM。

为什么另起一套而不是复用 `BibliographyExtractor`：
ingest 阶段那套把整段 References 文本交给模型拆分，遇到两种常见情况会退化——
① 额度/超时失败后走 `_fallback_refs`，按行首数字切，图注里的坐标数字（"500"、"525"）
   会被当成条目编号；
② 模型返回的条目少于兜底条数时整批用兜底结果，于是一整页文献被合并成"第 1 条"。
对检索无所谓（正文 chunk 还在，照样能命中），但对图谱是致命的：图谱需要的
是「一条文献 ↔ 一篇库内论文」的一一对应，条目切错等于所有边都错。

这里的切法：PyMuPDF 的 block 在这类排版里几乎就是一条文献（实测 BERT / ViT / LoRA /
Attention 四个 PDF 都是 1 block = 1 entry），比按行切稳得多。

三个必须踩过的坑（都是实测出来的）：
- ICLR 模板的章节标题用小型大写字距排版，抽出来是 `R EFERENCES`，直接字符串比对认不出；
  所以标题用「去空白后比对」。
- 参考文献区后面常跟附录，而附录里的图注数字最像条目编号；用「下一个同级标题」作为
  区的右边界，比逐个匹配 "Appendix" 可靠（LoRA 的附录标题是 `A L ARGE ...` 这种字距排版）。
- 编号式文献（Attention 的 `[37]`）的方括号数字就是正文标记里的编号，必须留下当
  ref_index，不能按顺序重编，否则正文里的 [37] 会指到第 37 条条目而不是它自己。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 章节标题（去空白后比对，兼容 `R EFERENCES` 这种字距排版）
_REF_KEYS = ("references", "bibliography", "workscited", "参考文献")
_STOP_KEYS = ("appendix", "appendices", "supplementarymaterial", "appendixa")
_STOP_HEAD_RE = re.compile(r"^[a-z]?(appendix|appendices)\b", re.I)

# 单条文献的长度上限：超过基本是「整页被合成一条」，宁可丢也不要
MAX_ENTRY_CHARS = 900
MIN_ENTRY_CHARS = 24

# 条目起始：`[12]` / `12.` 编号式，或姓氏开头（"Armen Aghajanyan"）、
# 缩写开头（"Z. Chen"）。姓后面必须跟分隔符，避免把 "Abstract" 当条目。
_NAME = r"[A-Z\u00c0-\u024f][A-Za-z\u00c0-\u024f'’\-]+"
_ENTRY_START_RE = re.compile(
    r"^(?:(?:van|von|de|der|den|le|la|di|da)\s+)?"
    r"(?:" + _NAME + r"|(?:[A-Z]\.\s*){1,3}" + _NAME + r")"
    # 姓后面必须跟分隔符：逗号 / & / and / 带点的缩写 / 另一个姓名 / 不带点的缩写（"Marcus, ..."）
    r"(?:\s*[,&]|\s+and\s|\s+[A-Z]\.|\s+" + _NAME + r"|\s+[A-Z](?:\s|,))"
)
# 编号前缀：`[12]`、`12.`、`12)`
_NUM_PREFIX_RE = re.compile(r"^\[?\s*(\d{1,3})\s*[\]\).]\s*")
_DIGITS_ONLY_RE = re.compile(r"^[\d\s.,\-–—%]+$")
_PAGE_NOISE_RE = re.compile(
    r"^(published as a conference paper|preprint\.?\s|under review|proceedings of|"
    r"arxiv:\d{4}\.\d{4,5}|all rights reserved|copyright|https?://)",
    re.I,
)

# 年份：排除 arXiv 编号（2012.13255）、页码范围（pp. 1234-1245）、卷号
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[a-z]?(?!\d)")
# arXiv 编号可能带前缀（arXiv:2012.13255）也可能只剩裸编号（.../abs/2012.13255）
_ARXIV_LIKE_RE = re.compile(r"(?:arxiv[:.]?\s*)?\b\d{4}\.\d{4,5}(?:v\d+)?\b", re.I)
_PAGES_RE = re.compile(r"\b(?:pp?\.|pages?)\s*\.?\s*\d", re.I)


@dataclass
class RefRecord:
    """一条参考文献。`raw` 是原文（匹配时也用得上，字段抽歪了还能靠它兜底）。"""

    index: int
    raw: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    title: str = ""
    venue: str = ""
    page: int | None = None
    numbered: bool = False  # True 表示条目自带编号（[37]），index 就是正文标记里的号

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "raw": self.raw,
            "authors": list(self.authors),
            "year": self.year,
            "title": self.title,
            "venue": self.venue,
            "page": self.page,
            "numbered": self.numbered,
        }


def _collapse(text: str) -> str:
    # 断词连字符（"learn-\ning"）与软连字符先接回去，再压空白
    text = text.replace("\u00ad", "").replace("-\n", "").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _squash(text: str) -> str:
    """去掉所有空白并转小写：`R EFERENCES` 与 `References` 在这里等价。"""
    return re.sub(r"\s+", "", text).lower()


def _blocks(page: Any) -> list[dict[str, Any]]:
    """
    取文字 block，附带最大字号。字号用于两件事：识别标题（→ 参考文献区的右边界）、
    以及避免把图注/表头当成条目。`get_text("blocks")` 不给字号，所以走 dict。
    """
    out: list[dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = block.get("lines") or []
        text = "\n".join("".join(s.get("text", "") for s in ln.get("spans", [])) for ln in lines)
        if not text.strip():
            continue
        size = max((float(s.get("size", 0)) for ln in lines for s in ln.get("spans", [])), default=0.0)
        out.append(
            {
                "x0": float(block["bbox"][0]),
                "y0": float(block["bbox"][1]),
                "x1": float(block["bbox"][2]),
                "y1": float(block["bbox"][3]),
                "text": text,
                "size": size,
            }
        )
    return out


def _body_size(blocks: list[dict[str, Any]]) -> float:
    """正文字号：按字符数加权取中位数，避免被标题/公式的行内大字号带偏。"""
    pairs: list[tuple[float, int]] = []
    for b in blocks:
        n = len(_collapse(b["text"]))
        if n >= 8:
            pairs.append((b["size"], n))
    if not pairs:
        return 10.0
    pairs.sort(key=lambda p: p[0])
    total = sum(n for _, n in pairs)
    acc = 0
    for size, n in pairs:
        acc += n
        if acc >= total / 2:
            return size
    return pairs[-1][0]


def _is_heading(block: dict[str, Any], body: float) -> bool:
    flat = _collapse(block["text"])
    if len(flat) > 120:
        return False
    if _squash(flat) in _REF_KEYS or _squash(flat)[:12] in _REF_KEYS:
        return True
    return block["size"] >= body * 1.08


def _column_of(block: dict[str, Any], page_width: float) -> int:
    """
    两栏页判断：按 block 中心点分左右。用中心而不是左边界——
    右栏的悬挂缩进会把 x0 拉到中线附近，按左边界分栏会把右栏并进左栏。
    """
    center = (block["x0"] + block["x1"]) / 2
    return 0 if center < page_width * 0.52 else 1


def _reading_order(blocks: list[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    """按栏再按纵坐标。单栏页所有 block 落在同一栏，等价于按 y 排序。"""
    return sorted(blocks, key=lambda b: (_column_of(b, page_width), round(b["y0"], 1)))


def _find_heading_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """找 References 标题块。去空白后前缀匹配，允许标题与首条文献粘在同一个 block。"""
    for block in blocks:
        flat = _collapse(block["text"])
        sq = _squash(flat)
        for key in _REF_KEYS:
            if not sq.startswith(key):
                continue
            rest = sq[len(key):]
            if len(rest) <= 4:  # 纯标题
                return block
            if re.match(r"^\d{1,3}", rest) or rest[:1].isalpha():
                # 标题后紧跟首条文献（"References [1] Jimmy..."）
                return block
    return None


def find_reference_page(doc: Any, max_pages: int | None = None) -> tuple[int, dict[str, Any]] | None:
    """
    找到 References 标题所在页与那个 block。取**最后一个**命中：
    正文里偶尔出现 "References" 作为短句，真正的章节标题在文档后部。
    """
    limit = len(doc) if max_pages is None else min(len(doc), max_pages)
    hit: tuple[int, dict[str, Any]] | None = None
    for i in range(limit):
        page = doc.load_page(i)
        block = _find_heading_block(_blocks(page))
        if block is not None:
            hit = (i, block)
    return hit


def _years_in(text: str) -> list[int]:
    """取可信年份：先挖掉 arXiv 编号与页码区间，再匹配 19xx/20xx。"""
    cleaned = _ARXIV_LIKE_RE.sub(" ", text)
    cleaned = _PAGES_RE.sub(" ", cleaned)
    return [int(y) for y in _YEAR_RE.findall(cleaned)]


def _parse_authors(head: str) -> list[str]:
    """从 "A, B, and C." 里取人名。只保留像人名的段，去掉机构、邮箱、年份。"""
    head = re.sub(r"\(.*?\)", " ", head)
    head = head.replace(" and ", ", ")
    names: list[str] = []
    for part in re.split(r"[,;]", head):
        part = re.sub(r"^\s*(?:and|&)\s*", "", part.strip()).strip(" .")
        if not part or len(part) > 60:
            continue
        if "@" in part or re.search(r"\d{4}", part):
            continue
        if not re.match(r"^[A-Z\u00c0-\u017f]", part):
            continue
        names.append(part)
    return names[:24]


def _split_authors(raw: str) -> tuple[list[str], str, int | None]:
    """
    返回 (authors, 剩余文本, year)。

    两种主流格式都要吃：
    - ACL/NAACL：`Authors. 2018. Title. In Proceedings...`（年份紧跟作者）
    - ICLR/NeurIPS：`Authors. Title. Venue, 2021.`（年份在末尾）
    判据是第一个句点分句：作者段总是以句点结尾且较短。
    """
    parts = re.split(r"(?<=\.)\s+", raw)
    if not parts:
        return [], raw, None

    first = parts[0]
    rest_start = 1
    year: int | None = None

    if len(first) <= 240:
        authors = _parse_authors(first)
    else:
        authors = []
        for i, part in enumerate(parts[:4]):
            if len(part) <= 240 and _years_in(part):
                authors = _parse_authors(part)
                rest_start = i + 1
                break

    if rest_start < len(parts):
        m2 = re.match(r"^((?:19|20)\d{2})[a-z]?\.?$", parts[rest_start].strip())
        if m2:
            year = int(m2.group(1))
            rest_start += 1
        else:
            # "Quoc V. Le. Massive exploration..." 这种人名结尾带缩写的形式，
            # 第一个句点会把姓单独切出来（"Le."）。它长度很短且以大写字母开头，
            # 属于作者段的尾巴，不能当成标题。
            name_tail = re.compile(r"^[A-Z\u00c0-\u024f][a-z\u00c0-\u024f'’\-]{1,18}\.$")
            while rest_start < len(parts) - 1 and name_tail.match(parts[rest_start].strip()):
                rest_start += 1

    rest = " ".join(parts[rest_start:]).strip()
    return authors, rest, year


def _split_title_venue(rest: str) -> tuple[str, str]:
    """从剩余文本里切标题与出处。标题到出处引导词或句点为止。"""
    if not rest:
        return "", ""
    cut = re.split(
        r"\.\s+(?=(?:In|Proceedings|Advances|arXiv|Journal|Transactions|Conference|"
        r"International|IEEE|ACM|Nature|Science|Neural|Findings|Workshop)\b)",
        rest,
        maxsplit=1,
    )
    head = cut[0].strip()
    tail = cut[1].strip() if len(cut) > 1 else ""

    if not tail:
        m = re.match(r"^(.{24,}?[.?!])(\s+\S.*)?$", head)
        if m and m.group(2):
            head, tail = m.group(1).strip(), m.group(2).strip()

    head = head.rstrip(".")
    head = re.sub(r"^(?:et al\.?|and)\s+", "", head, flags=re.I)
    return head[:300], tail[:200]


def parse_entry(raw: str, index: int, page: int | None = None) -> RefRecord:
    raw = _collapse(raw)
    numbered = False
    m = _NUM_PREFIX_RE.match(raw)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 999:
            index = num
            numbered = True
            raw = raw[m.end():].lstrip()

    authors, rest, year = _split_authors(raw)
    title, venue = _split_title_venue(rest)
    if year is None:
        years = _years_in(raw)
        if years:
            # 取最后一个：末尾的出版年比正文里提到的年份可靠
            year = years[-1]
    if not title:
        title = raw[:200]
    return RefRecord(
        index=index, raw=raw, authors=authors, year=year, title=title, venue=venue,
        page=page, numbered=numbered,
    )


def _looks_like_entry(text: str) -> bool:
    flat = _collapse(text)
    if len(flat) < MIN_ENTRY_CHARS or len(flat) > MAX_ENTRY_CHARS:
        return False
    if _DIGITS_ONLY_RE.match(flat) or _PAGE_NOISE_RE.match(flat):
        return False
    body = _NUM_PREFIX_RE.sub("", flat, count=1)
    if not _ENTRY_START_RE.match(body):
        return False
    return bool("." in flat or _years_in(flat))


def extract(pdf_path: str, max_pages: int = 60, *, max_entries: int = 400) -> list[RefRecord]:
    """
    从 PDF 抽取参考文献条目。找不到 References 章节就返回空列表——
    调用方据此给出「参考文献未解析」的空态，而不是编一堆假条目。
    """
    import fitz  # 延迟导入：只有真跑抽取时才需要 PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        hit = find_reference_page(doc, max_pages=max_pages)
        if hit is None:
            return []
        start_page, heading = hit

        records: list[RefRecord] = []
        buffer: str | None = None
        buffer_page: int | None = None
        seq = 0

        def flush() -> None:
            nonlocal buffer, buffer_page, seq
            if buffer and len(_collapse(buffer)) >= MIN_ENTRY_CHARS:
                seq += 1
                records.append(parse_entry(buffer, seq, buffer_page))
            buffer, buffer_page = None, None

        for page_no in range(start_page, min(len(doc), max_pages)):
            page = doc.load_page(page_no)
            width = float(page.rect.width)
            blocks = _reading_order(_blocks(page), width)
            body = _body_size(blocks)

            for block in blocks:
                text = block["text"]
                flat = _collapse(text)
                if not flat:
                    continue

                if page_no == start_page and block["y1"] <= heading["y0"] + 1:
                    continue  # 标题之前的正文
                if _find_heading_block([block]) is not None and _squash(flat).startswith(_REF_KEYS):
                    continue  # 标题本身
                # 参考文献区的右边界：下一个同级标题（通常是附录）就停
                if (
                    page_no != start_page or block["y0"] > heading["y0"] + 1
                ) and _is_heading(block, body) and len(flat) <= 120:
                    flush()
                    return records
                if any(_squash(flat).startswith(k) for k in _STOP_KEYS):
                    flush()
                    return records

                if _looks_like_entry(text):
                    flush()
                    buffer, buffer_page = text, page_no + 1
                    continue

                # 不是条首：可能是上一条的续行，也可能是图注/页眉噪声
                if buffer is None:
                    continue
                if _DIGITS_ONLY_RE.match(flat) or _PAGE_NOISE_RE.match(flat):
                    continue
                if len(flat) > 320:
                    # 一条文献不会长这样；多半已经跑到附录正文里了
                    flush()
                    return records
                if len(flat) < 12 and not flat.endswith((".", "”", ")")):
                    continue
                buffer = f"{buffer} {text}"

            if len(records) >= max_entries:
                break

        flush()
        return records
    finally:
        doc.close()


def extract_from_bytes(data: bytes, *, suffix: str = ".pdf", **kwargs: Any) -> list[RefRecord]:
    """网关收到的是上传流，落盘后才能给 fitz；这里包一层便于测试与回填。"""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        return extract(path, **kwargs)
    finally:
        Path(path).unlink(missing_ok=True)


def quality(records: Iterable[RefRecord]) -> dict[str, Any]:
    """
    抽取质量自评。匹配失败率要和它一起看：条目都没切对的时候，
    "匹配失败"其实是在报抽取的账，不该算到匹配头上。
    """
    rows = list(records)
    total = len(rows)
    if not total:
        return {"total": 0, "with_year": 0, "with_authors": 0, "with_title": 0, "score": 0.0}
    with_year = sum(1 for r in rows if r.year)
    with_authors = sum(1 for r in rows if r.authors)
    with_title = sum(1 for r in rows if len(r.title) >= 20 and not _DIGITS_ONLY_RE.match(r.title))
    score = (with_year + with_authors + with_title) / (total * 3)
    return {
        "total": total,
        "with_year": with_year,
        "with_authors": with_authors,
        "with_title": with_title,
        "score": round(score, 4),
    }
