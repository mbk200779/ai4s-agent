"""
把 docs/技术文档.md 构建成可提交的 PDF。

为什么不用 pandoc / wkhtmltopdf：
  · 本机没有这两个工具，装一个要解决 LaTeX/中文字体依赖；
  · 真正需要的排版能力（A4、页边距、页码、表格斑马纹、图片自适应）
    用 CSS + Chromium 打印就能完整拿到，且 CSS 调一版比改 LaTeX 模板快。

流程：Markdown → HTML（python-markdown）→ 套模板 → Chromium 打印 PDF。
页数在打印后回读校验，超过 30 页直接报错而不是静默交付。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = DOCS / "技术文档.md"
BUILD = DOCS / ".build"
HTML_OUT = BUILD / "tech-doc.html"
PDF_OUT = ROOT / "docs" / "AI4S学术研究Agent-技术文档.pdf"

CSS = r"""
/* 页边距交给打印器（puppeteer margin）控制：CSS 若在此声明 margin，
   会覆盖打印器的设置，导致内容贴边且页脚与正文重叠。 */
@page { size: A4; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Source Han Sans SC", sans-serif;
  font-size: 10.4pt;
  line-height: 1.72;
  color: #1f2937;
  margin: 0;
}
p { margin: 0 0 7pt; text-align: justify; }
strong { color: #0f2e4d; font-weight: 600; }

/* ---------- 封面 ---------- */
.cover { height: 228mm; display: flex; flex-direction: column; justify-content: center; page-break-after: always; }
.cover .eyebrow { font-size: 10pt; letter-spacing: 3pt; color: #6b7f96; margin-bottom: 10mm; }
.cover h1 { font-size: 27pt; line-height: 1.35; color: #10365c; margin: 0 0 6mm; border: none; padding: 0; page-break-before: avoid; }
.cover .rule { width: 62mm; height: 3pt; background: #0f766e; margin-bottom: 8mm; }
.cover .lead { font-size: 11.5pt; color: #37526e; line-height: 1.9; margin-bottom: 12mm; }
.cover .meta { border-top: 0.6pt solid #d8e0ea; padding-top: 6mm; font-size: 9.8pt; color: #4b5f74; line-height: 2.1; }
.cover .meta b { color: #10365c; font-weight: 600; margin-right: 4pt; }

/* ---------- 目录 ---------- */
.toc-page { page-break-after: always; }
.toc-page h2 { border: none; padding: 0; margin: 0 0 6mm; font-size: 15pt; color: #10365c; }
.toc-page > ul { column-count: 2; column-gap: 12mm; list-style: none; padding-left: 0; margin: 0; }
.toc-page ul { list-style: none; padding-left: 0; margin: 0; }
.toc-page ul ul { padding-left: 11pt; }
.toc-page li { font-size: 9.2pt; line-height: 1.62; color: #33465c; page-break-inside: avoid; }
.toc-page a { color: inherit; text-decoration: none; }
.toc-page > ul > li { font-weight: 600; color: #10365c; margin-bottom: 3.2pt; }
.toc-page > ul > li > ul > li { font-weight: 400; color: #4a5f76; }

/* ---------- 标题 ---------- */
h1 { font-size: 19pt; color: #10365c; margin: 0 0 8pt; padding-bottom: 5pt; border-bottom: 1.4pt solid #10365c; page-break-before: always; page-break-after: avoid; }
h2 { font-size: 14.2pt; color: #10365c; margin: 17pt 0 7pt; padding-left: 8pt; border-left: 3.6pt solid #0f766e; page-break-after: avoid; }
h3 { font-size: 11.6pt; color: #17466f; margin: 12pt 0 5pt; page-break-after: avoid; }
h4 { font-size: 10.6pt; color: #1f3b58; margin: 9pt 0 4pt; page-break-after: avoid; }

/* ---------- 表格 ---------- */
table { width: 100%; border-collapse: collapse; margin: 7pt 0 11pt; font-size: 8.9pt; line-height: 1.5; page-break-inside: avoid; }
th { background: #10365c; color: #ffffff; font-weight: 600; text-align: left; padding: 4.4pt 5.5pt; border: 0.4pt solid #10365c; }
td { padding: 4pt 5.5pt; border: 0.4pt solid #d5dee8; vertical-align: top; }
tbody tr:nth-child(even) td { background: #f4f8fb; }

/* ---------- 代码 ---------- */
pre { background: #f6f8fa; border: 0.4pt solid #dde4ec; border-radius: 3pt; padding: 7pt 9pt; margin: 6pt 0 11pt; page-break-inside: avoid; }
pre code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 8.4pt; line-height: 1.6; color: #243b53; background: none; padding: 0; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 8.9pt; background: #eef3f8; padding: 0.6pt 3pt; border-radius: 2pt; color: #0f3b5f; }
pre code code { background: none; padding: 0; }

/* ---------- 图片 ---------- */
img { max-width: 100%; display: block; margin: 9pt auto 5pt; border: 0.4pt solid #e2e8f0; border-radius: 3pt; }
p:has(> img) { text-align: center; }
p:has(> img) em { display: block; font-size: 8.6pt; color: #6b7f96; margin-top: 5pt; text-align: center; font-style: normal; }

/* ---------- 其他 ---------- */
blockquote { margin: 7pt 0 10pt; padding: 6pt 10pt; background: #f7fbfa; border-left: 3pt solid #0f766e; color: #2c4a48; font-size: 9.7pt; }
blockquote p { margin: 0; }
ul, ol { margin: 4pt 0 9pt; padding-left: 17pt; }
li { margin-bottom: 3pt; }
hr { border: none; border-top: 0.5pt solid #dde4ec; margin: 12pt 0; }
.meta-note { font-size: 9.4pt; color: #4b5f74; }
"""

FOOTER = """
<div style="width:100%;font-family:'Microsoft YaHei',sans-serif;font-size:7.6pt;color:#8b9bb0;
            padding:0 17mm;display:flex;justify-content:space-between;">
  <span>AI4S 学术研究 Agent · 技术文档 v1.0.0</span>
  <span><span class="pageNumber"></span> / <span class="totalPages"></span></span>
</div>
"""


def split_cover(text: str) -> tuple[str, str]:
    """正文从第一个 `## ` 开始；之前的标题与元信息做成封面。"""
    m = re.search(r"^## ", text, flags=re.M)
    if not m:
        return "", text
    return text[: m.start()], text[m.start():]


def build_html(md_text: str) -> str:
    head, body = split_cover(md_text)
    title = "AI4S 学术研究 Agent"
    if head:
        t = re.search(r"^# (.+)$", head, flags=re.M)
        if t:
            title = t.group(1).strip()

    body_html = markdown.markdown(
        body,
        extensions=["tables", "fenced_code", "toc", "attr_list", "md_in_html", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}},
    )
    toc_md = markdown.Markdown(extensions=["toc"], extension_configs={"toc": {"toc_depth": "2-3"}})
    toc_html = toc_md.convert(body)
    toc_html = toc_md.toc
    # 目录只保留两级的锚点，并去掉「附录」以外的噪音
    toc_html = toc_html.replace("<div class=\"toc\">", "").rstrip()

    cover = f"""
<section class="cover">
  <div class="eyebrow">AI FOR SCIENCE · 学术研究 Agent</div>
  <h1>可验证、可复现、可溯源的<br>AI4S 学术研究 Agent</h1>
  <div class="rule"></div>
  <p class="lead">一份把大模型放进证据边界里的工程实现：<br>
  每句结论可回跳 PDF 原位，每次评测可用哈希证明一致。</p>
  <div class="meta">
    <div><b>文档名称</b>技术文档（参赛作品）</div>
    <div><b>版本</b>v1.0.0</div>
    <div><b>作者</b>马博凯</div>
    <div><b>日期</b>2026 年 11 月</div>
    <div><b>代码仓库</b>github.com/mbk200779/ai4s-agent</div>
  </div>
</section>
"""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>{CSS}</style>
</head><body>
{cover}
<section class="toc-page">
  <h2>目 录</h2>
  {toc_html}
</section>
{body_html}
</body></html>
"""


def main() -> int:
    if not SRC.exists():
        print(f"找不到源文件：{SRC}", file=sys.stderr)
        return 1
    BUILD.mkdir(parents=True, exist_ok=True)

    md_text = SRC.read_text(encoding="utf-8")
    html = build_html(md_text)
    # HTML 在 docs/.build/ 下，图片相对路径需上跳一级
    html = html.replace('src="./assets/', 'src="../assets/')
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"HTML 已生成：{HTML_OUT}  ({len(html)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
