"""
把 docs/作品介绍文档.md 构建成可提交的 PDF（官方第 6 项「作品介绍文档」）。

与技术文档共用同一套排版规则（CSS 从 build_tech_pdf 复用，不复制一份出来漂移），
只替换封面文案与输出路径。图片比技术文档多得多，所以把图宽收到 92%，
一是让图注不至于被挤到下一页，二是控制总页数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import markdown  # noqa: E402
import build_tech_pdf as base  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = DOCS / "作品介绍文档.md"
BUILD = DOCS / ".build"
HTML_OUT = BUILD / "intro-doc.html"

COVER = """
<section class="cover">
  <div class="eyebrow">AI FOR SCIENCE · 参赛作品介绍</div>
  <h1>可验证、可复现、可溯源的<br>AI4S 学术研究 Agent</h1>
  <div class="rule"></div>
  <p class="lead">一份把大模型放进证据边界里的工程实现：<br>
  每句结论可回跳 PDF 原位，每次评测可用哈希证明一致。</p>
  <div class="meta">
    <div><b>文档名称</b>作品介绍文档（对应官方第 6 项要求）</div>
    <div><b>版本</b>v1.0.0</div>
    <div><b>作者</b>马博凯</div>
    <div><b>日期</b>2026 年 11 月</div>
    <div><b>代码仓库</b>github.com/mbk200779/ai4s-agent</div>
    <div><b>在线演示</b>demo.mabokai.site</div>
  </div>
</section>
"""

EXTRA_CSS = """
/* 图片与图注必须作为一个不可分割块。用行内的 <img><br><em> 写法时，
   Chrome 允许在段落中间分页，会出现「图在本页、图注跑到下一页顶部」的断裂；
   改成 <figure> + break-inside:avoid 后整块一起换页。 */
figure { margin: 10pt 0 12pt; break-inside: avoid; page-break-inside: avoid; }
figure img { max-width: 80%; max-height: 200mm; margin: 0 auto 6pt; display: block; border: 0.4pt solid #e2e8f0; border-radius: 3pt; }
figcaption { font-size: 8.6pt; color: #6b7f96; text-align: center; line-height: 1.62; }
img { max-width: 92%; }
"""


def build_html(md_text: str) -> str:
    head, body = base.split_cover(md_text)
    title = "AI4S 学术研究 Agent 作品介绍文档"
    if head:
        import re

        t = re.search(r"^# (.+)$", head, flags=re.M)
        if t:
            title = t.group(1).strip()

    body_html = markdown.markdown(
        body,
        extensions=["tables", "fenced_code", "toc", "attr_list", "md_in_html", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}},
    )
    toc_md = markdown.Markdown(extensions=["toc"], extension_configs={"toc": {"toc_depth": "2-3"}})
    toc_md.convert(body)
    toc_html = toc_md.toc.replace('<div class="toc">', "").rstrip()

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>{base.CSS}{EXTRA_CSS}</style>
</head><body>
{COVER}
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

    html = build_html(SRC.read_text(encoding="utf-8"))
    # HTML 在 docs/.build/ 下，图片相对路径需上跳一级
    html = html.replace('src="./assets/', 'src="../assets/')
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"HTML 已生成：{HTML_OUT}  ({len(html)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
