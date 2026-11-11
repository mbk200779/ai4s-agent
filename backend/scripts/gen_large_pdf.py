"""
生成一份 541 页的"教材级"测试 PDF，用于长文档解析实测。

为什么不用真实的 541 页教材：
- 体积太大不便随仓库携带，且版权不明；
- 实测关注的是「页数 → 内存/耗时」的曲线，文本密度比真实内容更重要。

这份 PDF 的刻意设计：
- 每页约 2600 字符（接近真实教材的正文密度），541 页 ≈ 140 万字符；
- 每 20 页一个章节标题，用来验证章节树与 section_path；
- 末尾 3 页是 References，共 60 条带编号的条目，用来验证文献解析与 [N] 引用定位；
- 内容确定性生成（不用随机数），因此 file_hash 稳定，可反复用于"二次上传"验证。

用法：
    python -m scripts.gen_large_pdf --out /app/data/bench/textbook_541.pdf
"""
import argparse
from pathlib import Path

import fitz

PAGE_W, PAGE_H = 595, 842  # A4 pt
MARGIN = 56
FONT_SIZE = 9.5
LINE_GAP = 1.35

SENTENCES = [
    "The proposed framework decomposes the retrieval pipeline into four stages, each of which can be evaluated independently.",
    "Empirical results on three benchmark collections show a consistent improvement over the strongest baseline.",
    "We observe that the variance across runs is small enough that the reported differences are meaningful.",
    "Ablation studies indicate that the sparse branch contributes most of the gain on short queries.",
    "All experiments were conducted on a single machine with eight cores and no accelerator.",
    "The preprocessing step removes headers, footers and page numbers before segmentation.",
    "Table 2 reports the latency breakdown for each stage of the pipeline.",
    "We follow the standard evaluation protocol and report mean average precision at ten.",
    "The notation used throughout this chapter is summarised in the following paragraphs.",
    "Readers familiar with the earlier chapters may skip the derivation and continue with Section 3.",
    "This section introduces the notation and defines the operators used in later proofs.",
    "The bound in Equation 4 follows from a straightforward application of the triangle inequality.",
    "We emphasise that the result does not depend on the choice of the normalisation constant.",
    "Chapter exercises are collected at the end of each part and are graded by difficulty.",
    "The discussion in this chapter is deliberately informal; rigorous statements appear in the appendix.",
    "A careful reader will notice that the two definitions differ only in their treatment of ties.",
    "Historical remarks are placed in smaller type and may be omitted on a first reading.",
    "The argument generalises to the multivariate case without modification.",
    "Numerical stability is discussed in the context of finite precision arithmetic.",
    "We conclude with a short summary and a list of open problems.",
    # 带编号引用的句子：用来验证正文 [N] → paper_refs 的定位是否落到正确条目
    "As reported in earlier work [3], the sparse branch dominates on short queries.",
    "The preprocessing pipeline follows the protocol of previous studies [12] and [17].",
    "A similar decomposition was proposed independently in [24, 25].",
    "Our setting matches the one described in [41] except for the tie-breaking rule.",
    "These observations are consistent with the analysis in [7].",
]

CAPTIONS = [
    "Figure 1.1: Overview of the ingestion pipeline used throughout this book.",
    "Table 1.1: Summary of notation and the sections in which it is introduced.",
    "Figure 1.2: Trade-off between batch size and peak memory for a fixed document length.",
]


def _paragraph(seed: int, count: int) -> str:
    """确定性拼接段落：同一个 seed 永远得到同一段文字。"""
    out = []
    for i in range(count):
        out.append(SENTENCES[(seed + i * 7) % len(SENTENCES)])
    return " ".join(out)


def _page_text(seed: int, paragraphs: int = 8, per_para: int = 3) -> str:
    """确定性生成整页文本，段间空行。约 2400~2800 字符。"""
    blocks = []
    for i in range(paragraphs):
        blocks.append(_paragraph(seed=seed + i * 31, count=per_para))
    return "\n\n".join(blocks)


def build(path: str | Path, pages: int = 541) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()

    # 15 条/页 × 4 页 = 60 条：A4 一页放得下 15 条 3 行条目，20 条会溢出
    ref_pages = 4
    body_pages = pages - ref_pages
    chapter = 0

    for p in range(body_pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = MARGIN

        if p % 20 == 0:
            chapter += 1
            heading = f"Chapter {chapter}. Structured Retrieval over Long Documents"
            page.insert_textbox(
                fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 34),
                heading,
                fontsize=15,
                fontname="hebo",
            )
            y += 38
            page.insert_textbox(
                fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 24),
                f"{chapter}.1 Motivation and scope",
                fontsize=11.5,
                fontname="hebo",
            )
            y += 30

        # 整页一次排版：文本放不下时逐步减段，保证每页都接近满页而不是只有一段。
        # （逐段 insert_textbox 时返回值是"框高"而不是"文字高"，会让 y 一步越界。）
        body_rect = fitz.Rect(MARGIN, y, PAGE_W - MARGIN, PAGE_H - MARGIN - 24)
        paragraphs = 8
        while paragraphs >= 2:
            used = page.insert_textbox(
                body_rect, _page_text(p * 13, paragraphs), fontsize=FONT_SIZE, fontname="helv"
            )
            if used >= 0:
                break
            paragraphs -= 1

        # 偶发图注，保证 figure 抽取有素材
        if p % 47 == 0:
            page.insert_textbox(
                fitz.Rect(MARGIN, PAGE_H - MARGIN - 30, PAGE_W - MARGIN, PAGE_H - MARGIN),
                CAPTIONS[(p // 47) % len(CAPTIONS)],
                fontsize=8.5,
                fontname="heit",
            )

        page.insert_textbox(
            fitz.Rect(PAGE_W - MARGIN - 40, PAGE_H - 32, PAGE_W - MARGIN, PAGE_H - 16),
            f"{p + 1}",
            fontsize=8,
            fontname="helv",
        )

        # 偶发图注，保证 figure 抽取有素材
        if p % 47 == 0:
            page.insert_textbox(
                fitz.Rect(MARGIN, PAGE_H - MARGIN - 30, PAGE_W - MARGIN, PAGE_H - MARGIN),
                CAPTIONS[(p // 47) % len(CAPTIONS)],
                fontsize=8.5,
                fontname="heit",
            )

        page.insert_textbox(
            fitz.Rect(PAGE_W - MARGIN - 40, PAGE_H - 32, PAGE_W - MARGIN, PAGE_H - 16),
            f"{p + 1}",
            fontsize=8,
            fontname="helv",
        )

    # ---- 参考文献区 ----
    # 整页一次排版：逐个 insert_textbox 时返回值是"框高"而不是"文字高"，
    # y 会一步跨过页底，导致每页只写进第一条（这个坑踩过，记在这里）。
    ref_no = 1
    for r in range(ref_pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = MARGIN
        if r == 0:
            page.insert_textbox(
                fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 34),
                "References",
                fontsize=14,
                fontname="hebo",
            )
            y += 40

        entries = []
        while ref_no <= 60 and len(entries) < 15:
            entries.append(
                f"[{ref_no}] Author{ref_no} Surname{ref_no}. "
                f"Structured retrieval for long technical documents, part {ref_no}. "
                f"Journal of Document Analysis, {2000 + ref_no % 20};{ref_no}({ref_no % 12}):"
                f"{100 + ref_no}-{120 + ref_no}."
            )
            ref_no += 1
        if not entries:
            break
        page.insert_textbox(
            fitz.Rect(MARGIN, y, PAGE_W - MARGIN, PAGE_H - MARGIN),
            "\n\n".join(entries),
            fontsize=8.5,
            fontname="helv",
        )

    doc.save(str(path), deflate=True)
    doc.close()
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/app/data/bench/textbook_541.pdf")
    ap.add_argument("--pages", type=int, default=541)
    args = ap.parse_args()
    out = build(args.out, args.pages)
    size = out.stat().st_size
    print(f"{out}  pages={args.pages}  size={size / 1048576:.2f}MB")
    with fitz.open(str(out)) as doc:
        chars = sum(len(p.get_text()) for p in doc)
    print(f"text_chars={chars}  avg_per_page={chars // max(1, args.pages)}")


if __name__ == "__main__":
    main()
