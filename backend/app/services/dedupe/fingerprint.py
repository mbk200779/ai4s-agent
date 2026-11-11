"""
正文指纹：把一篇论文的正文压成可比较的几个数。

为什么不能只用 file_hash：
- `file_hash` 是 PDF 字节的 sha256。同一份稿子重新导出（换 LaTeX 模板、合并字体子集）
  字节就变了，但它还是同一篇。
- 反过来，同一篇的 v1 和 v7 是"多数段落相同、少数改过"的两份文件，
  它们**应该**被判成同一工作的不同版本，而不是"两篇无关论文"。

所以要的是"内容指纹"，不是"文件指纹"。这里用四样东西，各有各的不可替代性：

1. **MinHash 草图（`minhash`, k=128）**：正文 3-gram 集合的 128 个最小哈希值。
   由它可以估出集合的重合率——**这是主判据**。
   实测：Attention v1 vs v7 正文重合 83.9%/89.2%；Attention vs BERT / LoRA / ViT 是 1.0%~2.6%。
   为什么不用整篇 simhash 做主判据：整篇 simhash 对**长度**敏感，v1→v7 只差 12 位
   （相似度 0.81），看着像"不太像"，其实是同一篇；它也不便回答"重合了多少"。
2. **段落级 simhash（`para_hashes`）**：界面上"相似段落数"就是数它算出来的。
   容差 12/64（实测：同版本段落 79% 命中，不同论文的同主题段落 0~0.5% 命中）。
3. **摘要 / 首段向量**：语义层面"是不是在讲同一件事"。用来把
   "同一工作的改版"（摘要几乎一样）和"同主题的另一篇"（都在讲注意力，摘要不同）分开。
4. **首页元数据（标题/作者）**：从 PDF 首页按字号抽，**不从文件名取**。
   本地上传的论文 `papers.title` 默认就是文件名，拿 "final_v9.pdf" 比标题等于没比。

文件名不参与任何一步计算；正文证据可以否决标题（见 relation.py 的 possible_duplicate）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 段落 simhash 的匹配容差。64 位差 12 位 ≈ 段落 81% 同构：够容忍
# "v1→v7 改了公式编号、补了一个从句、换了引用格式"，又不足以把
# 两篇不同论文的同主题段落配成一对（实测不同论文 0~0.5%）。
PARA_HAMMING_MAX = 12

# MinHash 草图长度。128 个值估 Jaccard 的标准误约 1/sqrt(128)≈0.09，
# 对"0.02 还是 0.85"这种量级的判断绰绰有余；存下来 128×17 字符 ≈ 2KB/版本。
MINHASH_K = 128

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")

# 页眉页脚、版权行、arXiv 水印：每页都出现，会把所有论文的指纹拉近
_NOISE_RE = re.compile(
    r"(arxiv:\s*\d{4}\.\d{4,5}v?\d*|"
    r"published as a conference paper|"
    r"proceedings of the|"
    r"preprint\.?\s*under review|"
    r"all rights reserved|"
    r"^\s*\d{1,3}\s*$)",
    re.IGNORECASE,
)

_ABSTRACT_END_RE = re.compile(
    r"\b(1\s+introduction|introduction\b|keywords?\b|ccs concepts)\b", re.IGNORECASE
)


def normalize(text: str) -> str:
    """去掉水印与多余空白，保留可用于比较的正文。"""
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if not _NOISE_RE.search(ln)]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def tokens(text: str) -> list[str]:
    """
    中英混排的分词：拉丁按词、汉字按字。

    不用 jieba：指纹要的是"可复现"。分词器升级会悄悄改变所有历史指纹，
    存量指纹就和新算的对不上了。按字切中文虽然粗，但它是**稳定**的。
    """
    return _TOKEN_RE.findall(text.lower())


def shingles(toks: list[str], n: int = 3) -> Iterable[str]:
    if len(toks) < n:
        if toks:
            yield " ".join(toks)
        return
    for i in range(len(toks) - n + 1):
        yield " ".join(toks[i : i + n])


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


def simhash64(text: str, *, n: int = 3) -> str:
    """64 位 simhash，返回 16 位 hex。空文本返回全 0。"""
    toks = tokens(normalize(text))
    if not toks:
        return "0" * 16
    bits = [0.0] * 64
    for sh in shingles(toks, n):
        h = _h64(sh)
        for i in range(64):
            bits[i] += 1.0 if (h >> i) & 1 else -1.0
    value = 0
    for i in range(64):
        if bits[i] > 0:
            value |= 1 << i
    return f"{value:016x}"


def minhash_sketch(text: str, *, k: int = MINHASH_K, n: int = 3) -> tuple[list[str], int]:
    """
    KMV 草图：所有 shingle 的 64 位哈希里取最小的 k 个，返回 (升序 hex 列表, shingle 总数)。

    一次哈希 + 维护最小值集合，比"k 个独立哈希函数各跑一遍"快 k 倍
    （5500 个 shingle × 128 个哈希函数 = 70 万次 md5，会把上传拖到秒级）。
    """
    toks = tokens(normalize(text))
    hashes: set[int] = {_h64(sh) for sh in shingles(toks, n)}
    if not hashes:
        return [], 0
    smallest = sorted(hashes)[:k]
    return [f"{v:016x}" for v in smallest], len(hashes)


def hamming(a: str, b: str) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def sim_from_hamming(dist: int) -> float:
    return max(0.0, 1.0 - dist / 64.0)


def jaccard_from_sketch(a: list[str], b: list[str], *, na: int, nb: int) -> float | None:
    """
    由 KMV 草图估 Jaccard：两份草图共有的值个数 / min(len(a), len(b))。

    分母取两者较短的那个：文本短于 k 个 shingle 时草图会短一截，
    直接除 len(a) 会把相似度算高。
    """
    if not a or not b or na <= 0 or nb <= 0:
        return None
    k = min(len(a), len(b))
    if k == 0:
        return None
    return min(1.0, len(set(a) & set(b)) / k)


def containment(jaccard: float | None, na: int, nb: int, *, direction: str = "a") -> float | None:
    """
    由 Jaccard 与两侧基数推重合率：J = I/(na+nb-I) ⇒ I = J(na+nb)/(1+J)。
    `direction="a"` 求 |A∩B|/na，即"A 有多少能在 B 里找到"。
    """
    if jaccard is None or na <= 0 or nb <= 0:
        return None
    inter = jaccard * (na + nb) / (1.0 + jaccard)
    denom = na if direction == "a" else nb
    return max(0.0, min(1.0, inter / denom))


def split_paragraphs(text: str, *, min_chars: int = 80, max_chars: int = 1600) -> list[str]:
    """
    切自然段。

    只按空行切会把双栏 PDF 的整页切成一段（PyMuPDF 抽出的文本常没有空行），
    所以超长块再按句末标点补一刀，保证每段 simhash 描述的是"一小段连贯论述"。
    """
    text = normalize(text)
    if not text:
        return []
    raw = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(raw) <= 1:
        raw = [p for p in re.split(r"(?<=[.!?。！？])\s+", text) if p.strip()]

    out: list[str] = []
    buf = ""
    for piece in raw:
        if len(piece) > max_chars:
            for sent in re.split(r"(?<=[.!?。！？])\s+", piece):
                if len(buf) + len(sent) < min_chars:
                    buf = f"{buf} {sent}".strip()
                else:
                    if buf:
                        out.append(buf)
                    buf = sent
        else:
            if len(buf) + len(piece) < min_chars:
                buf = f"{buf} {piece}".strip()
            else:
                if buf:
                    out.append(buf)
                buf = piece
    if buf:
        out.append(buf)
    return [p for p in out if len(p) >= min_chars] or ([text] if text else [])


def paragraph_hashes(paragraphs: list[str]) -> list[str]:
    return [simhash64(p) for p in paragraphs]


def best_match_ratio(
    hashes_a: list[str], hashes_b: list[str], *, max_dist: int = PARA_HAMMING_MAX
) -> tuple[int, float]:
    """
    a 里有多少段能在 b 里找到近同段，返回 (命中段数, 命中比例)。

    贪心一对一：每段 a 只配对最近的一段 b，已用过的 b 不再参与——
    否则 30 段正文对上一段摘要式介绍，会算出"30 段全命中"。
    """
    if not hashes_a or not hashes_b:
        return 0, 0.0
    used: set[int] = set()
    hit = 0
    for ha in hashes_a:
        best_i, best_d = -1, 65
        for i, hb in enumerate(hashes_b):
            if i in used:
                continue
            d = hamming(ha, hb)
            if d < best_d:
                best_i, best_d = i, d
        if best_i >= 0 and best_d <= max_dist:
            used.add(best_i)
            hit += 1
    return hit, hit / len(hashes_a)


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return None
    return max(-1.0, min(1.0, dot / (na * nb)))


def extract_abstract(text: str) -> str:
    """从正文开头找摘要：取 "Abstract" 之后、Introduction 之前的一段。"""
    t = normalize(text)
    if not t:
        return ""
    m = re.search(r"\babstract\b[\s:.\-]*", t, re.IGNORECASE)
    start = m.end() if m else 0
    tail = t[start : start + 4000]
    end = _ABSTRACT_END_RE.search(tail)
    body = tail[: end.start()] if end else tail[:1600]
    return body.strip()


def extract_lead(text: str) -> str:
    """首段：摘要之后的第一段正文（没有摘要时取正文开头）。"""
    paras = split_paragraphs(text)
    if not paras:
        return ""
    t = normalize(text)
    abs_start = t.lower().find("abstract")
    for p in paras[:6]:
        if abs_start >= 0 and p[:40] and p[:40] in t[abs_start : abs_start + 200]:
            continue
        return p
    return paras[0]


_SECTION_NUM_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]+)\.?\s+")


def section_titles(section_paths: Iterable[str | None]) -> list[str]:
    """
    从 `3 Method > 3.2 Scaled Attention` 里取顶层标题。

    顺手做三件去噪的事，否则章节增删的结论会被这些假章节带偏：
    ① 去掉前面的编号（`1 Introduction` 与 `Introduction` 是同一节）；
    ② 丢掉 arXiv 水印、版权行这类"长得像标题"的行；
    ③ 丢掉纯数字（页码）。
    """
    seen: list[str] = []
    for path in section_paths:
        if not path:
            continue
        top = str(path).split(">")[0].strip()
        top = _SECTION_NUM_RE.sub("", top).strip()
        if not top or not re.search(r"[A-Za-z\u4e00-\u9fff]", top):
            continue
        if _NOISE_RE.search(top):
            continue
        # 标题不会以小写字母开头、也不会以句号结尾——这两条能把被换行切碎的
        # 正文残句（比如 "days."）挡在"章节"之外。
        if top[0].islower() or top.endswith("."):
            continue
        if top not in seen:
            seen.append(top)
    return seen


@dataclass
class Fingerprint:
    simhash: str
    minhash: list[str] = field(default_factory=list)
    shingle_count: int = 0
    para_hashes: list[str] = field(default_factory=list)
    abstract_emb: list[float] | None = None
    lead_emb: list[float] | None = None
    abstract_head: str = ""
    lead_head: str = ""
    body_chars: int = 0
    section_count: int = 0
    sections: list[str] = field(default_factory=list)
    source: str = "pdf"
    title: str = ""
    authors: list[str] = field(default_factory=list)
    # 归一化后的正文。不落库（太大），只在本次请求内存里给差异摘要用。
    text: str = ""

    @property
    def para_count(self) -> int:
        return len(self.para_hashes)

    def to_row(self) -> dict[str, Any]:
        return {
            "simhash": self.simhash,
            "minhash": json.dumps(self.minhash),
            "shingle_count": self.shingle_count,
            "para_hashes": json.dumps(self.para_hashes),
            "para_count": len(self.para_hashes),
            "abstract_emb": json.dumps(self.abstract_emb) if self.abstract_emb else None,
            "lead_emb": json.dumps(self.lead_emb) if self.lead_emb else None,
            "abstract_head": self.abstract_head[:300],
            "lead_head": self.lead_head[:300],
            "body_chars": self.body_chars,
            "section_count": self.section_count,
            "source": self.source,
        }

    def brief(self) -> dict[str, Any]:
        """给接口/界面的指纹摘要：不返回向量与草图本体（没人看，还大）。"""
        return {
            "simhash": self.simhash,
            "para_count": len(self.para_hashes),
            "shingle_count": self.shingle_count,
            "body_chars": self.body_chars,
            "section_count": self.section_count,
            "title": self.title,
            "authors": self.authors[:8],
            "abstract_head": self.abstract_head[:160],
            "lead_head": self.lead_head[:160],
            "source": self.source,
            "has_embedding": bool(self.abstract_emb),
        }


def _embed(texts: list[str]) -> list[list[float] | None]:
    try:
        from app.services.embedder import EmbedderService

        emb = EmbedderService()
        # fastembed 返回 numpy.float32：直接 json.dumps 会抛 TypeError，
        # 必须在入口转成 python float（指纹要落库、要跨进程比较）
        return [[float(x) for x in v] for v in emb.embed_dense(texts)]
    except Exception:
        # 向量算不出来不许挡路：关系判定在没有语义分时按纯文本证据降级，
        # 相似度与段落匹配仍然可用（这正是 simhash / MinHash 存在的意义）。
        return [None for _ in texts]


def from_text(
    text: str,
    *,
    source: str = "pdf",
    sections: list[str] | None = None,
    title: str = "",
    authors: list[str] | None = None,
    embed: bool = True,
) -> Fingerprint:
    """从纯正文构造指纹（PDF 抽取与 chunk 拼接都走这里，保证算法一致）。"""
    norm = normalize(text)
    paras = split_paragraphs(norm)
    sketch, shingle_count = minhash_sketch(norm)
    abstract = extract_abstract(norm)
    lead = extract_lead(norm)
    abstract_emb = lead_emb = None
    if embed:
        vecs = _embed([abstract[:1200] or norm[:400], lead[:800] or norm[:400]])
        abstract_emb = vecs[0]
        lead_emb = vecs[1] if len(vecs) > 1 else None
    return Fingerprint(
        simhash=simhash64(norm),
        minhash=sketch,
        shingle_count=shingle_count,
        para_hashes=paragraph_hashes(paras),
        abstract_emb=abstract_emb,
        lead_emb=lead_emb,
        abstract_head=abstract,
        lead_head=lead,
        body_chars=len(norm),
        section_count=len(sections or []),
        sections=list(sections or []),
        source=source,
        title=title,
        authors=list(authors or []),
        text=norm,
    )


# ---------- 首页元数据：标题 / 作者 ----------
# 为什么不从文件名取标题：本地上传的论文 `papers.title` 默认就是文件名，
# 拿 "attention_v7.pdf" 和 "final_v9.pdf" 比标题相似度是 0，会把同一篇判成不同篇。
# 反过来，把文件名改成主标题、正文却是别的论文，也不能骗过正文证据。
# 机构名黑名单：作者块里常混着单位与邮箱，这些词一出现就不是人名
_AFFIL_WORDS = {
    "university", "institute", "department", "college", "school", "laboratory",
    "lab", "center", "centre", "research", "google", "brain", "inc", "ltd",
    "corporation", "company", "hospital", "academy", "faculty", "group", "team",
    "facebook", "meta", "deepmind", "openai", "microsoft", "amazon", "nvidia",
    "alibaba", "tencent", "baidu", "ai", "science", "technologies",
}

# 连续 2~3 个首字母大写的词。允许 "N." 这样的中间名缩写。
_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z.\-']+(?:\s+[A-Z][a-zA-Z.\-']+){1,2})\b")
_EMAIL_RE = re.compile(r"\S+@\S+")


def _parse_authors(lines: list[str]) -> list[str]:
    """
    从作者块里抠人名。

    真实版式（NeurIPS 模板）是三行一组，且组与组之间没有分隔符：
        Ashish Vaswani∗ / Google Brain / avaswani@google.com / Noam Shazeer∗ / ...
    所以不能按行取，也不能只按逗号切——直接找"连续的 2~3 个首字母大写的词"，
    再用机构黑名单剔除 "Google Brain" 这类假人名。宁可漏一个作者，
    也不要把单位算成共同作者（那会让"作者重合度"彻底失去意义）。
    """
    text = _EMAIL_RE.sub(" ", " ".join(lines))
    out: list[str] = []
    for m in _NAME_RE.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        words = name.split()
        if not 2 <= len(words) <= 3:
            continue
        if any(w.lower().strip(".,") in _AFFIL_WORDS for w in words):
            continue
        if name.lower() in [x.lower() for x in out]:
            continue
        out.append(name)
    return out[:20]


_ABSTRACT_LINE_RE = re.compile(r"^\s*(abstract|a\s*b\s*s\s*t\s*r\s*a\s*c\s*t)\b", re.I)
_WATERMARK_RE = re.compile(r"arXiv:\d{4}\.\d{4,5}v?\d*", re.I)


def extract_meta_from_pdf(path: str) -> dict[str, Any]:
    """
    抽取首页的标题与作者。

    三个坑，都踩过：
    1. arXiv 水印是**竖排旋转**的大字号文本，字号比标题还大（20pt vs 17pt），
       按字号取必然取到它 → 先用 `line["dir"]` 过滤掉非水平行；
    2. PyMuPDF 的 block 顺序**不是阅读顺序**（双栏模板里作者块可能排在标题前面），
       所以不能用"标题之后的行"来取作者，要按纵坐标切区间；
    3. 作者块里名字/单位/邮箱交替出现、同字号 → 交给 `_parse_authors` 认出人名。
    """
    import fitz

    doc = fitz.open(path)
    try:
        if doc.page_count == 0:
            return {"title": "", "authors": [], "abstract": ""}
        page = doc.load_page(0)
        page_h = float(page.rect.height) or 1.0
        data = page.get_text("dict")
        rows: list[tuple[float, str, float, float]] = []  # (size, text, y0, y1)
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                dx, dy = (line.get("dir") or (1, 0))[:2]
                if abs(dy) > 0.01:  # 旋转文本（水印/侧栏）
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text or _WATERMARK_RE.search(text):
                    continue
                size = max(float(s.get("size", 0)) for s in spans)
                bbox = line.get("bbox") or (0, 0, 0, 0)
                rows.append((size, text, float(bbox[1]), float(bbox[3])))
        if not rows:
            return {"title": "", "authors": [], "abstract": ""}

        max_size = max(r[0] for r in rows)
        title_rows = [r for r in rows if r[0] >= max_size - 0.6 and not _ABSTRACT_LINE_RE.match(r[1])]
        # 标题常折行：取字号最大的一簇里纵向相邻的几行
        title_rows.sort(key=lambda r: r[2])
        title_parts: list[str] = []
        prev_y1 = None
        for size, text, y0, y1 in title_rows:
            if prev_y1 is not None and y0 - prev_y1 > size * 1.2:
                break
            title_parts.append(text)
            prev_y1 = y1
        title = re.sub(r"\s+", " ", " ".join(title_parts)).strip()
        title_bottom = max((r[3] for r in title_rows if r[1] in title_parts), default=0.0)

        # 作者区：标题下沿 → Abstract 之前
        abs_y = None
        for size, text, y0, y1 in sorted(rows, key=lambda r: r[2]):
            if y0 > title_bottom and _ABSTRACT_LINE_RE.match(text):
                abs_y = y0
                break
        author_rows = [
            r[1]
            for r in sorted(rows, key=lambda r: r[2])
            if r[2] >= title_bottom - 1 and (abs_y is None or r[2] < abs_y)
        ][:60]

        authors = _parse_authors(author_rows)

        head = "\n\n".join(
            doc.load_page(i).get_text("text") for i in range(min(doc.page_count, 2))
        )
        return {"title": title, "authors": authors, "abstract": extract_abstract(head)}
    finally:
        doc.close()


def from_pdf(path: str, *, max_pages: int = 60, embed: bool = True) -> Fingerprint:
    """
    直接从 PDF 文本层抽正文算指纹（上传时用，此时还没有 chunk）。

    `max_pages` 是为了不把 541 页教材整本抽出来：判断"是不是同一份稿子"主要靠
    前 60 页，再往后几乎不增加信息，却要等好几秒。截断时在 `source` 上留痕
    （`pdf:60p`），事后能看出这份指纹是按抽样算的。
    """
    import fitz

    doc = fitz.open(path)
    try:
        total = doc.page_count
        limit = min(total, max_pages)
        texts = [doc.load_page(i).get_text("text") for i in range(limit)]
        sections: list[str] = []
        for t in texts:
            for line in t.splitlines():
                s = line.strip()
                if 3 <= len(s) <= 80 and re.match(
                    r"^(\d+(\.\d+)*\s+\S|abstract$|introduction$|references$)", s, re.I
                ):
                    if s.lower() not in [x.lower() for x in sections]:
                        sections.append(s)
        meta = extract_meta_from_pdf(path)
        return from_text(
            "\n\n".join(texts),
            source=f"pdf:{limit}p" if total > limit else "pdf",
            sections=sections,
            title=meta["title"],
            authors=meta["authors"],
            embed=embed,
        )
    finally:
        doc.close()


def from_chunks(
    chunks: list[Any], *, title: str = "", authors: list[str] | None = None, embed: bool = True
) -> Fingerprint:
    """从已解析的 chunk 拼正文算指纹（入库后、老数据回填时用）。"""
    body = "\n\n".join(c.text for c in chunks if getattr(c, "text", None))
    paths = [getattr(c, "section_path", None) for c in chunks]
    return from_text(
        body,
        source="chunks",
        sections=section_titles(paths),
        title=title,
        authors=list(authors or []),
        embed=embed,
    )
