"""
句子级锚点：把一段 chunk 文本切成句子，并为每句话找回它在 PDF 上的真实矩形。

为什么不能只用 chunk 的 bbox：
chunk 的 bbox 是**块级**的（一个段落框），点引用时把整段框住，读者还得自己找是哪一句；
更糟的是 `_merge_chunks` 会把同章节相邻块拼成一个 chunk，此时 bbox 只覆盖第一块，
高亮区域与引用文本根本不重合。所以锚点必须落在**行级矩形**上，再由句子去合并行。

三个刻意的设计选择：

1. **锚点存的是「字符区间 + 矩形」，不存句子文本**
   句子文本由 `text[start:end]` 现切。存文本会让同一段话在库里出现两遍
   （chunk.text 里一遍、anchors 里一遍），一旦某次解析做了截断或清洗，
   两份就永久对不上，而校验脚本恰恰要拿"切片 == 引用句"当判据。

2. **一句话跨栏时切成多个矩形，而不是一个大框**
   双栏排版的论文里，一句话从右栏末尾续到左栏行首时，两行的并集矩形会横跨整页宽度，
   框住大量无关文字——这正是"整页高亮"的变种。所以按行做水平重叠判定：
   重叠够多才合并成一条带，否则另起一个矩形。

3. **精度是显式字段，不是隐含假设**
   `sentence / cell / block / ocr` 四档。扫描件没有文本层，坐标只能来自 OCR，
   那就老老实实标 `ocr`，让前端画虚框并写明「OCR 定位」，而不是假装精确到句。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Sequence

ANCHOR_VERSION = 1

# 精度档位：读的人要能一眼看出"这个框有多准"
PRECISION_SENTENCE = "sentence"  # 文本层 + 行级矩形，精确到句
PRECISION_CELL = "cell"          # 表格单元格，矩形就是单元格边框
PRECISION_BLOCK = "block"        # 图表/公式，只有整体区域
PRECISION_OCR = "ocr"            # 无文本层，坐标来自 OCR，只有区域级
PRECISION_LINE = "line"          # 逐行锚定：参考文献这种"一条一行"的结构
PRECISIONS = (
    PRECISION_SENTENCE, PRECISION_CELL, PRECISION_BLOCK, PRECISION_LINE, PRECISION_OCR,
)

# 单 chunk 锚点数上限：正常段落 3~12 句；超过这个数说明切句出了问题（例如整页糊成一坨），
# 与其把几千条锚点塞进一行 JSON，不如截断并让校验脚本报出来。
MAX_ANCHORS_PER_CHUNK = 120

# 覆盖率低于这个值时算"这段文本没被锚点解释清楚"，校验脚本给出警告
MIN_COVERAGE = 0.70

# 矩形面积超过页面的这个比例时视为"整页高亮"，直接判失败
MAX_RECT_PAGE_RATIO = 0.92

_TERMINATORS = ".!?。！？"
_CLOSERS = "\"'”’)]}）】」』"
_OPENERS = "\"'“‘([{（【「『"

# 句点前面是这些词时不切句。"al" 出现频率最高（et al.），
# 不处理的话论文正文几乎每段都会被切成半句。
_ABBREV = {
    "al", "e.g", "i.e", "fig", "figs", "eq", "eqs", "sec", "secs", "tab", "tabs",
    "ref", "refs", "vs", "cf", "resp", "approx", "no", "nos", "vol", "vols",
    "pp", "p", "ch", "chap", "app", "dept", "univ", "dr", "prof", "mr", "mrs", "ms",
    "inc", "ltd", "co", "corp", "st", "etc", "viz", "ca", "ed", "eds", "proc",
}

_WORD_TAIL = re.compile(r"([A-Za-z][A-Za-z.\-']*)$")

# 有"内容"的锚点：至少含一个字母/数字/汉字。纯分隔符、纯空白的切片不算。
_HAS_CONTENT = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")

# 章节编号开头的行："3 Method"、"5.2 Hardware and Schedule"、"A. Proofs"
_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z]\.|[IVX]+\.)\s+\S")


# ---------------------------------------------------------------------------
# 句子切分
# ---------------------------------------------------------------------------
def split_sentences(text: str, max_len: int = 480) -> list[tuple[int, int]]:
    """
    返回 [(start, end)]，区间已去掉首尾空白，且互不重叠、按顺序。

    不用 `re.split(r"(?<=[.!?])\\s+")` 的原因：
    - 会把 `0.98`、`Fig. 3`、`et al. (2017)` 切成碎块；
    - 拿不到区间（只能拿到字符串），而锚点必须要 offset 才能映射到行矩形。

    切不动的超长句（> max_len）再按分号补一刀：PDF 里一整段没有句号的
    "列表式长句"很常见，整段框住等于没定位。
    """
    if not text:
        return []
    n = len(text)
    out: list[tuple[int, int]] = []
    start = 0
    i = 0
    while i < n:
        ch = text[i]
        if ch not in _TERMINATORS:
            i += 1
            continue

        # 小数点 / 版本号 / 编号：`3.2`、`v1.5`、`28.4`
        if ch == "." and 0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
            i += 1
            continue

        # 连续的终止符算一个（`...`、`?!`）
        j = i
        while j + 1 < n and text[j + 1] in _TERMINATORS:
            j += 1

        # 终止符后可能紧跟右引号/右括号
        k = j + 1
        while k < n and text[k] in _CLOSERS:
            k += 1

        if k >= n:
            out.append((start, n))
            start = n
            break

        # 后面必须跟空白才可能是句末（`Fig.3`、`3.2M` 都不是）
        if text[k] not in " \t\n\r":
            if ch in "。！？":
                # 中文句号后常直接接字，也要切
                out.append((start, k))
                start = k
                i = k
                continue
            i = j + 1
            continue

        m = k
        while m < n and text[m] in " \t\n\r":
            m += 1
        if m >= n:
            out.append((start, n))
            start = n
            break

        if ch == "." and not _can_follow_period(text, i, text[m]):
            i = j + 1
            continue

        out.append((start, k))
        start = k
        i = k

    if start < n:
        out.append((start, n))

    spans = _trim(text, out)
    return _soft_split(text, spans, max_len)


def _can_follow_period(text: str, i: int, next_ch: str) -> bool:
    """`i` 是句点位置，`next_ch` 是它后面第一个非空白字符。"""
    # "e.g. this" / "i.e. the"：句点后是小写，几乎不可能是新句子
    if "a" <= next_ch <= "z":
        return False
    # 句点是数字的一部分（"3." 后面接上标除外）：交给调用方，这里不切
    tail = text[max(0, i - 24):i]
    m = _WORD_TAIL.search(tail)
    if not m:
        return True
    word = m.group(1).rstrip(".").lower()
    if word in _ABBREV:
        return False
    # 单字母大写：人名缩写 "J. Devlin"、"V. Vaswani"
    if len(word) == 1 and word.isupper():
        return False
    return True


def _trim(text: str, spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e - s < 2:
            continue
        out.append((s, e))
    return out


def _soft_split(text: str, spans: list[tuple[int, int]], max_len: int) -> list[tuple[int, int]]:
    """超长句在分号处补切；仍然超长就硬切，保证单个锚点不会大到失去意义。"""
    out: list[tuple[int, int]] = []
    for s, e in spans:
        if e - s <= max_len:
            out.append((s, e))
            continue
        cut = s
        while cut < e:
            window_end = min(cut + max_len, e)
            if window_end >= e:
                out.append((cut, e))
                break
            part = text[cut:window_end]
            br = max(part.rfind("; "), part.rfind("；"), part.rfind(": "), part.rfind("，"))
            if br < max_len // 3:
                br = max(part.rfind(", "), part.rfind(" "))
            if br <= 0:
                br = len(part) - 1
            piece = _trim(text, [(cut, cut + br + 1)])
            if piece:
                out.append(piece[0])
            cut = cut + br + 1
            while cut < e and text[cut].isspace():
                cut += 1
    return out


# ---------------------------------------------------------------------------
# 文本 → 行矩形对齐
# ---------------------------------------------------------------------------
def _norm_view(text: str) -> tuple[str, list[int]]:
    """
    归一化视图：折叠空白、统一小写，并记录每个归一化字符对应的原文下标。

    为什么需要 offset 映射：PDF 抽取出来的行文本与 chunk 文本经常差一个
    连字符断行、多个空格或大小写；直接在原文里做子串查找会大量落空，
    而归一化之后又拿不回原文位置。所以建一个"归一化串 + 反查表"。
    """
    chars: list[str] = []
    idx: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            chars.append(" ")
            idx.append(i)
            prev_space = True
        else:
            # NFKC 让全角字符/上下标与半角等价（β₂ 与 β2 在 PDF 里可能是两种码位）
            chars.append(unicodedata.normalize("NFKC", ch).lower())
            idx.append(i)
            prev_space = False
    return "".join(chars), idx


def align_lines(
    text: str, lines: Sequence[dict[str, Any]], start: int = 0
) -> list[tuple[int, int, tuple[float, float, float, float], int]]:
    """
    把 chunk 文本按顺序对齐到页面行，返回 [(start, end, rect, line_index)]（区间在 `text` 里）。

    做法是"行找文本"而不是"文本找行"：行的文字是解析器原样给出的，
    把它归一化后去 chunk 文本里顺序查找，命中就记一段映射。
    比反向切词再做编辑距离稳得多，也不会因为一行被拆成两行而崩掉——
    拆开的两行都能各自命中。

    `start` 用于多 chunk 同页时跳过已经消费过的行，避免后一段把前一段的行再认一遍。
    """
    if not text or not lines:
        return []
    norm, idx = _norm_view(text)
    cursor = 0
    out: list[tuple[int, int, tuple[float, float, float, float]]] = []
    for line_idx, line in enumerate(lines[start:], start=start):
        raw = (line.get("text") or "").strip()
        rect = line.get("bbox") or line.get("rect")
        if not raw or not rect or len(rect) != 4:
            continue
        needle = unicodedata.normalize("NFKC", raw).lower()
        needle = re.sub(r"\s+", " ", needle).strip()
        if not needle:
            continue
        pos = norm.find(needle, cursor)
        if pos < 0:
            # 行首尾被裁过（页眉页码截断、连字符断行）时，退一步用前 24 个字符定位
            probe = needle[:24]
            if len(probe) < 8:
                continue
            pos = norm.find(probe, cursor)
            if pos < 0:
                continue
            end_norm = pos + len(probe)
        else:
            end_norm = pos + len(needle)
        if pos >= len(idx):
            continue
        s = idx[pos]
        e = idx[min(end_norm - 1, len(idx) - 1)] + 1
        if e <= s:
            continue
        out.append(
            (s, e, (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])), line_idx)
        )
        cursor = end_norm
    return out


def _h_overlap(a: Sequence[float], b: Sequence[float]) -> float:
    """两行的水平重叠比例（相对较窄的那一行）。"""
    lo = max(a[0], b[0])
    hi = min(a[2], b[2])
    if hi <= lo:
        return 0.0
    narrower = min(a[2] - a[0], b[2] - b[0])
    if narrower <= 0:
        return 0.0
    return (hi - lo) / narrower


def _is_heading_line(text: str) -> bool:
    """
    行文本像不像章节标题。

    判据刻意保守：只有"编号开头且没有句末标点"、或"全大写短行"才算。
    判宽的代价是把正文首行拆成独立锚点，读者点开引用只会看到半句话；
    判窄的代价是标题被并进下一句，高亮框多出一条标题——后者只是不好看，
    前者才是错的，所以宁可漏判。
    """
    t = (text or "").strip()
    if not t or len(t) > 90:
        return False
    if t[-1] in ".!?。！？:：":
        return False
    if _HEADING_RE.match(t):
        return True
    return t.isupper() and len(t.split()) <= 10


def split_at_heading(
    text: str,
    spans: Sequence[tuple],
    s: int,
    e: int,
) -> list[tuple[int, int]]:
    """
    把"章节标题 + 正文首句"这种粘连拆开。

    为什么会粘：切句只看句末标点，而标题没有标点，于是 "5.2 Hardware and Schedule
    We trained our models on one machine with 8 NVIDIA P100 GPUs." 会被当成一句。
    这直接违反"高亮区域必须与实际引用文本重合"——框里会多出一条标题。
    拆开之后标题自己成为一个锚点，正文句从标题之后开始。

    注意标题在 PDF 里常被拆成两个文本块（"5.2" 与 "Hardware and Schedule" 同基线），
    所以判定要用"连续多行拼起来的文本"，只看第一行会永远判不出来。
    """
    hit = [sp for sp in spans if sp[0] < e and sp[1] > s]
    if len(hit) < 2:
        return [(s, e)]

    acc: list[str] = []
    acc_end: int | None = None
    for sp in hit[:5]:
        piece = text[max(s, sp[0]): min(e, sp[1])].strip()
        if not piece:
            continue
        if piece[-1] in ".!?。！？":
            break  # 到正文首句了，acc 里就是标题
        acc.append(piece)
        acc_end = min(e, sp[1])
        if len(" ".join(acc)) > 90:
            return [(s, e)]
    else:
        # 整句都没有句末标点：要么本身就是一个标题，要么是被截断的残句，不动
        return [(s, e)]

    if acc_end is None or not _is_heading_line(" ".join(acc)):
        return [(s, e)]
    cut = acc_end
    while cut < e and text[cut].isspace():
        cut += 1
    if cut >= e - 2:
        return [(s, e)]
    return _trim(text, [(s, acc_end), (cut, e)]) or [(s, e)]


def merge_rects(rects: Sequence[Sequence[float]]) -> list[list[float]]:
    """
    相邻行合并成矩形带；水平错开太多（换栏、换块）就另起一个矩形。
    见模块头第 2 条：一句话跨栏时宁可是两个框，也不要一个横跨全页的大框。

    两种"相邻"都要处理，少一种就会多出碎片框：
    - 上下相邻：同一段的连续行（章节正文里最常见）；
    - 同一基线左右相邻：PDF 里标题常被拆成两个文本块（"5.2" 与 "Hardware and Schedule"
      同基线、不同 block），不合并的话会留下一个 12pt 宽的小方块挂在句框旁边。
    """
    out: list[list[float]] = []
    for r in rects:
        r = [float(r[0]), float(r[1]), float(r[2]), float(r[3])]
        if r[2] <= r[0] or r[3] <= r[1]:
            continue
        if not out:
            out.append(r)
            continue
        last = out[-1]
        line_h = max(1.0, last[3] - last[1])
        v_gap = r[1] - last[3]
        stacked = -2.0 <= v_gap <= line_h * 1.4 and _h_overlap(last, r) >= 0.45

        shorter = max(1.0, min(last[3] - last[1], r[3] - r[1]))
        v_overlap = (min(last[3], r[3]) - max(last[1], r[1])) / shorter
        h_gap = max(0.0, max(last[0], r[0]) - min(last[2], r[2]))
        same_baseline = v_overlap >= 0.5 and h_gap <= line_h * 2.0

        if stacked or same_baseline:
            last[0] = min(last[0], r[0])
            last[1] = min(last[1], r[1])
            last[2] = max(last[2], r[2])
            last[3] = max(last[3], r[3])
        else:
            out.append(r)
    return [[round(v, 2) for v in rect] for rect in out]


def union(rects: Sequence[Sequence[float]]) -> list[float]:
    xs0 = min(r[0] for r in rects)
    ys0 = min(r[1] for r in rects)
    xs1 = max(r[2] for r in rects)
    ys1 = max(r[3] for r in rects)
    return [round(xs0, 2), round(ys0, 2), round(xs1, 2), round(ys1, 2)]


def rect_area(rect: Sequence[float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def is_full_page(rects: Sequence[Sequence[float]], page_size: Sequence[float] | None) -> bool:
    """一个矩形就顶满整页——这是明确禁止的"整页高亮"。"""
    if not page_size or len(page_size) < 2:
        return False
    area = float(page_size[0]) * float(page_size[1])
    if area <= 0:
        return False
    return any(rect_area(r) / area >= MAX_RECT_PAGE_RATIO for r in rects)


# ---------------------------------------------------------------------------
# 锚点文档
# ---------------------------------------------------------------------------
def build_document(
    *,
    text: str,
    lines: Sequence[dict[str, Any]],
    page: int,
    page_size: Sequence[float] | None = None,
    text_layer: bool = True,
    precision: str = PRECISION_SENTENCE,
    kind: str = "sentence",
    start_line: int = 0,
    prefix: str = "s",
) -> dict[str, Any]:
    """
    由「chunk 文本 + 页面行」构造锚点文档。

    `precision` 传 `ocr` 时不做句子级合并：OCR 出来的行坐标本身就有漂移，
    强行按行合并只会把相邻两栏粘在一起，不如老老实实按块给一个区域。
    """
    spans = align_lines(text, lines, start=start_line)
    items: list[dict[str, Any]] = []

    if precision == PRECISION_OCR or not spans:
        if spans:
            rects = merge_rects([s[2] for s in spans])
            if rects:
                items.append(
                    {
                        "ref_id": f"{prefix}0",
                        "kind": "block",
                        "precision": PRECISION_OCR,
                        "start": spans[0][0],
                        "end": spans[-1][1],
                        "rects": rects,
                        "bbox": union(rects),
                    }
                )
        return _doc(page, page_size, text_layer, items, precision, kind)

    ranges: list[tuple[int, int]] = []
    for s0, e0 in split_sentences(text):
        ranges.extend(split_at_heading(text, spans, s0, e0))

    for n, (s, e) in enumerate(ranges, 1):
        hit = [sp[2] for sp in spans if sp[0] < e and sp[1] > s]
        if not hit:
            continue
        rects = merge_rects(hit)
        if not rects:
            continue
        items.append(
            {
                "ref_id": f"{prefix}{n}",
                "kind": kind,
                "precision": PRECISION_SENTENCE,
                "start": s,
                "end": e,
                "rects": rects,
                "bbox": union(rects),
            }
        )
        if len(items) >= MAX_ANCHORS_PER_CHUNK:
            break
    return _doc(page, page_size, text_layer, items, precision, kind)


def build_cell_document(
    *,
    text: str,
    cells: Sequence[dict[str, Any]],
    page: int,
    page_size: Sequence[float] | None = None,
    text_layer: bool = True,
) -> dict[str, Any]:
    """
    表格：每个非空单元格一个锚点。

    `cells` 由解析器给出（行号、列号、字符区间、单元格矩形），所以不需要再做文本对齐——
    表格文本本来就是解析器拼出来的，" | " 分隔符的位置只有它自己知道。
    """
    # 有些页面把整张图当表格抽出来（旋转/镜像的图注），宽度只有十几 pt 却高达半页。
    # 这类"表"的单元格坐标与肉眼文字完全对不上（deep 校验重合度 0%），
    # 与其建一堆错锚点，不如判定它根本不是表格。
    if page_size and len(page_size) >= 2 and cells:
        xs0 = min(c["bbox"][0] for c in cells if c.get("bbox") and len(c["bbox"]) == 4)
        xs1 = max(c["bbox"][2] for c in cells if c.get("bbox") and len(c["bbox"]) == 4)
        if (xs1 - xs0) < 0.15 * float(page_size[0]):
            return _doc(page, page_size, text_layer, [], PRECISION_CELL, "cell")

    items: list[dict[str, Any]] = []
    # 表格文本落库前会 strip()，末尾的空单元格因此可能越界一格；
    # 这里统一按实际文本长度收口，而不是让调用方去猜。
    text_len = len(text)
    for cell in cells:
        s = max(0, int(cell.get("start") or 0))
        e = min(text_len, int(cell.get("end") or 0))
        rect = cell.get("bbox")
        if e <= s or not rect or len(rect) != 4:
            continue
        # 只认"有内容"的单元格：分隔符和空白的切片做锚点毫无意义，
        # 还会在读者点开时高亮一个看不出内容的空格子。
        if not _HAS_CONTENT.search(text[s:e]):
            continue
        items.append(
            {
                "ref_id": f"t{cell.get('row', 0)}c{cell.get('col', 0)}",
                "kind": "cell",
                "precision": PRECISION_CELL,
                "start": s,
                "end": e,
                "rects": [[round(float(v), 2) for v in rect]],
                "bbox": [round(float(v), 2) for v in rect],
            }
        )
        if len(items) >= MAX_ANCHORS_PER_CHUNK:
            break
    return _doc(page, page_size, text_layer, items, PRECISION_CELL, "cell")


def build_line_document(
    *,
    text: str,
    lines: Sequence[dict[str, Any]],
    page: int,
    page_size: Sequence[float] | None = None,
    text_layer: bool = True,
    start: int = 0,
    kind: str = "line",
) -> dict[str, Any]:
    """
    一行一个锚点。用于参考文献这类"一条一行、没有句末标点"的文本。

    这些片段用切句法只会得到一个覆盖整页文献区的巨框，等于没定位；
    而每条文献本来就占一行，按行锚定既准确又便宜。
    """
    spans = align_lines(text, lines, start=start)
    items: list[dict[str, Any]] = []
    for n, (s, e, rect, _idx) in enumerate(spans, 1):
        if len(text[s:e].strip()) < 3:
            continue
        items.append(
            {
                "ref_id": f"l{n}",
                "kind": kind,
                "precision": PRECISION_LINE if text_layer else PRECISION_OCR,
                "start": s,
                "end": e,
                "rects": [[round(float(v), 2) for v in rect]],
                "bbox": [round(float(v), 2) for v in rect],
            }
        )
        if len(items) >= MAX_ANCHORS_PER_CHUNK:
            break
    return _doc(page, page_size, text_layer, items, PRECISION_LINE, kind)


def build_block_document(
    *,
    text: str,
    page: int,
    bbox: Sequence[float] | None,
    page_size: Sequence[float] | None = None,
    text_layer: bool = True,
    kind: str = "block",
) -> dict[str, Any]:
    """图表/公式：只有整体区域，标注为 block（无文本层时是 ocr）。"""
    items: list[dict[str, Any]] = []
    if bbox and len(bbox) == 4:
        rect = [round(float(v), 2) for v in bbox]
        items.append(
            {
                "ref_id": "b0",
                "kind": kind,
                "precision": PRECISION_BLOCK if text_layer else PRECISION_OCR,
                "start": 0,
                "end": len(text),
                "rects": [rect],
                "bbox": rect,
            }
        )
    return _doc(
        page, page_size, text_layer, items, PRECISION_BLOCK if text_layer else PRECISION_OCR, kind
    )


def _clamp(rects: Sequence[Sequence[float]], page_size: Sequence[float] | None) -> list[list[float]]:
    """
    把矩形裁到页面内。

    pdfplumber 抽表格时偶尔会把框画到页面外一截（跨页表格、页脚合并），
    不裁的话校验会直接判"矩形超出页面"，整张表的锚点就白建了。
    裁掉越界部分比丢掉锚点划算——读者的注意力本来也到不了页面外。
    """
    out: list[list[float]] = []
    if not page_size or len(page_size) < 2:
        return [[round(float(v), 2) for v in r] for r in rects]
    w, h = float(page_size[0]), float(page_size[1])
    for r in rects:
        x0, y0 = max(0.0, float(r[0])), max(0.0, float(r[1]))
        x1, y1 = min(w, float(r[2])), min(h, float(r[3]))
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            continue
        out.append([round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)])
    return out


def _doc(
    page: int,
    page_size: Sequence[float] | None,
    text_layer: bool,
    items: list[dict[str, Any]],
    precision: str,
    kind: str,
) -> dict[str, Any]:
    for item in items:
        rects = _clamp(item.get("rects") or [], page_size)
        if rects:
            item["rects"] = rects
            item["bbox"] = union(rects)
    items = [i for i in items if i.get("rects")]
    return {
        "v": ANCHOR_VERSION,
        "page": int(page or 0),
        "page_size": [round(float(v), 2) for v in (page_size or [])][:2] or None,
        "text_layer": bool(text_layer),
        "precision": precision,
        "kind": kind,
        "items": items,
    }


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def load(value: Any) -> dict[str, Any] | None:
    """把库里的 anchors 字段（JSON 字符串或 dict）读成 dict，坏数据一律当没有。"""
    import json

    if not value:
        return None
    if isinstance(value, dict):
        return value if value.get("items") is not None else None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("items") is not None else None
    return None


def items_of(doc: Any) -> list[dict[str, Any]]:
    data = load(doc)
    return list(data.get("items") or []) if data else []


def slice_text(text: str, anchor: dict[str, Any]) -> str:
    s = max(0, int(anchor.get("start") or 0))
    e = min(len(text), int(anchor.get("end") or 0))
    return text[s:e] if e > s else ""


def find_by_offset(doc: Any, start: int, end: int) -> dict[str, Any] | None:
    """给定字符区间，找覆盖它的锚点（取重叠最多的那个）。"""
    best = None
    best_hit = 0
    for item in items_of(doc):
        hit = min(end, int(item["end"])) - max(start, int(item["start"]))
        if hit > best_hit:
            best, best_hit = item, hit
    return best


# ---------------------------------------------------------------------------
# 校验（解析期自检 + scripts/validate_anchors.py 共用）
# ---------------------------------------------------------------------------
def validate_document(
    text: str,
    doc: Any,
    *,
    page_size: Sequence[float] | None = None,
    allow_empty: bool = False,
) -> list[dict[str, str]]:
    """
    返回问题列表 `[{level, code, msg}]`。level=error 表示锚点不可用，warning 表示可用但有疑点。

    这套规则同时被解析期（写完立刻自检，坏数据当场记 warning 而不是等前端报错）
    与离线脚本（批量扫全库）使用，避免两处判据漂移。
    """
    problems: list[dict[str, str]] = []

    def bad(code: str, msg: str) -> None:
        problems.append({"level": "error", "code": code, "msg": msg})

    def warn(code: str, msg: str) -> None:
        problems.append({"level": "warning", "code": code, "msg": msg})

    data = load(doc)
    if data is None:
        bad("no_doc", "anchors 不是合法结构（缺 items）")
        return problems

    if int(data.get("v") or 0) != ANCHOR_VERSION:
        bad("version", f"锚点版本 {data.get('v')} 与当前 {ANCHOR_VERSION} 不一致")
    if data.get("precision") not in PRECISIONS:
        bad("precision", f"未知精度 {data.get('precision')!r}")

    items = data.get("items") or []
    if not items:
        if not allow_empty:
            warn("empty", "锚点为空（该 chunk 将退化为页级定位）")
        return problems

    page_size = page_size or data.get("page_size")
    seen: set[str] = set()
    last_end = -1
    covered = 0
    for n, item in enumerate(items):
        ref = str(item.get("ref_id") or "")
        if not ref:
            bad("ref_missing", f"第 {n} 条缺 ref_id")
        elif ref in seen:
            bad("ref_dup", f"ref_id 重复：{ref}")
        seen.add(ref)

        if item.get("precision") not in PRECISIONS:
            bad("item_precision", f"{ref} 精度非法：{item.get('precision')!r}")

        try:
            s, e = int(item["start"]), int(item["end"])
        except (KeyError, TypeError, ValueError):
            bad("offset_type", f"{ref} 的 start/end 不是整数")
            continue
        if not (0 <= s < e <= len(text)):
            bad("offset_range", f"{ref} 区间 [{s},{e}) 越界（文本长 {len(text)}）")
            continue
        if s < last_end:
            bad("offset_order", f"{ref} 与前一条区间重叠或乱序（{s} < {last_end}）")
        last_end = e
        covered += e - s
        if not text[s:e].strip():
            bad("empty_text", f"{ref} 区间内没有可见字符")

        rects = item.get("rects") or ([] if not item.get("bbox") else [item["bbox"]])
        if not rects:
            bad("no_rect", f"{ref} 没有任何矩形")
            continue
        for r in rects:
            if not isinstance(r, (list, tuple)) or len(r) != 4:
                bad("rect_shape", f"{ref} 的矩形不是 4 个数")
                continue
            if not (r[2] > r[0] and r[3] > r[1]):
                bad("rect_degenerate", f"{ref} 的矩形退化：{r}")
            if page_size and len(page_size) >= 2:
                w, h = float(page_size[0]), float(page_size[1])
                if r[0] < -2 or r[1] < -2 or r[2] > w + 2 or r[3] > h + 2:
                    bad("rect_outside", f"{ref} 的矩形超出页面 {w:.0f}x{h:.0f}：{r}")
        if is_full_page(rects, page_size):
            bad("full_page", f"{ref} 的矩形几乎占满整页（禁止整页高亮）")

    if text.strip():
        ratio = covered / max(1, len(text))
        if ratio < MIN_COVERAGE:
            warn(
                "coverage",
                f"锚点只覆盖 {ratio:.1%} 的文本（阈值 {MIN_COVERAGE:.0%}），部分句子无法定位",
            )
    return problems


def summarize(doc: Any) -> dict[str, Any]:
    """给接口/脚本用的一行摘要。"""
    data = load(doc)
    if not data:
        return {"has_anchors": False, "count": 0, "precision": None}
    items = data.get("items") or []
    precisions = sorted({str(i.get("precision")) for i in items})
    return {
        "has_anchors": bool(items),
        "count": len(items),
        "precision": data.get("precision"),
        "precisions": precisions,
        "text_layer": data.get("text_layer"),
        "page_size": data.get("page_size"),
    }
