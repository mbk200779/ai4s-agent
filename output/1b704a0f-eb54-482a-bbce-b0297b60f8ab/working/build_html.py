# -*- coding: utf-8 -*-
"""把作品介绍文档 markdown 转成美化 HTML（含封面 + 目录 + 正文 + 内嵌图片）。"""
import re, html, base64

MD = r'D:\workbuddy\2026-10-21-16-54-36\output\1b704a0f-eb54-482a-bbce-b0297b60f8ab\stage1\final_draft.md'
B64 = r'D:\workbuddy\2026-10-21-16-54-36\output\1b704a0f-eb54-482a-bbce-b0297b60f8ab\stage2\image_b64.txt'
OUT = r'D:\workbuddy\2026-10-21-16-54-36\output\1b704a0f-eb54-482a-bbce-b0297b60f8ab\stage2\formatted-作品介绍文档.html'

md = open(MD, encoding='utf-8').read()
img_b64 = open(B64, encoding='utf-8').read().strip()

# 去掉文档开头的一级标题（会单独做封面）
lines = md.split('\n')

def esc(t):
    return html.escape(t)

def inline(s):
    # 行内代码
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    # 加粗
    s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
    # 斜体
    s = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', s)
    # 图片
    s = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'', s)
    return s

def parse_table(block_lines):
    rows = []
    for ln in block_lines:
        ln = ln.strip()
        if not ln.startswith('|'):
            continue
        cells = [c.strip() for c in ln.strip('|').split('|')]
        rows.append(cells)
    # 去掉分隔行（第二行，全是 --- ）
    if len(rows) >= 2:
        sep = rows[1]
        if all(re.fullmatch(r':?-{2,}:?', c) for c in sep):
            rows = rows[:1] + rows[2:]
    if not rows:
        return ''
    out = ['<table>']
    header = rows[0]
    out.append('<thead><tr>' + ''.join(f'<th>{inline(esc(c))}</th>' for c in header) + '</tr></thead>')
    out.append('<tbody>')
    for r in rows[1:]:
        out.append('<tr>' + ''.join(f'<td>{inline(esc(c))}</td>' for c in r) + '</tr>')
    out.append('</tbody></table>')
    return '\n'.join(out)

# 解析文档结构
# 收集所有二级标题作为目录
sections = []
body_blocks = []
i = 0
# 跳过文件开头的大标题和引用行，找到第一个 ## 二级标题
cur_sec = None

# 先扫描标题列表用于目录
heading2_list = []
for ln in lines:
    m = re.match(r'^##\s+(.+)$', ln)
    if m:
        heading2_list.append(m.group(1).strip())

# 正文渲染：按二级标题分段
cur_h2 = None
cur_buf = []
h2_body = {}  # h2 -> html
order = []

def flush():
    global cur_h2, cur_buf
    if cur_h2 is not None:
        h2_body[cur_h2] = '\n'.join(cur_buf)
        order.append(cur_h2)
    cur_buf = []

in_blockquote = False
blockquote_buf = []

for ln in lines:
    stripped = ln.strip()
    # 一级标题（文档主标题）
    if re.match(r'^#\s+', ln):
        continue
    # 引用块（文档开头说明）
    if stripped.startswith('>'):
        in_blockquote = True
        blockquote_buf.append(stripped.lstrip('> ').strip())
        continue
    elif in_blockquote and stripped == '':
        continue
    # 二级标题
    m2 = re.match(r'^##\s+(.+)$', ln)
    if m2:
        flush()
        cur_h2 = m2.group(1).strip()
        continue
    # 三级标题
    m3 = re.match(r'^###\s+(.+)$', ln)
    if m3:
        cur_buf.append(f'<h3>{inline(esc(m3.group(1).strip()))}</h3>')
        continue
    # 表格
    if stripped.startswith('|') and cur_h2:
        cur_buf.append(parse_table([ln]))
        continue
    # 无序列表
    m = re.match(r'^-\s+(.+)$', ln)
    if m:
        cur_buf.append(f'<li>{inline(esc(m.group(1)))}</li>')
        continue
    # 有序列表
    m = re.match(r'^\d+\.\s+(.+)$', ln)
    if m:
        cur_buf.append(f'<li>{inline(esc(m.group(1)))}</li>')
        continue
    # 图片
    m = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)', stripped)
    if m:
        cur_buf.append(f'<div class="figure"><img src="data:image/png;base64,{img_b64}" alt="{esc(m.group(1))}"/></div>')
        continue
    # 分隔线
    if stripped == '---':
        continue
    # 空行
    if stripped == '':
        if cur_buf and cur_buf[-1].startswith('<li>'):
            # 收尾列表
            cur_buf.append('</ul>')
        cur_buf.append('')
        continue
    # 普通段落
    if stripped:
        cur_buf.append(f'<p>{inline(esc(stripped))}</p>')

flush()

# 处理表格：连续的 <table> 行需要合并（上面是逐行处理的，实际表格是多行）
# 简化：这里重新做一次表格合并太复杂，改为后处理——把相邻的 <table> 合并
def merge_tables(blocks):
    # 由于上面逐行解析表格会每行生成一个 table，这里重新解析更稳妥
    pass

# 重新用更可靠的方式解析表格：逐段处理
# 上面的实现有问题，直接用完整的分块解析

# 重新解析（更可靠）
def build_body():
    out_blocks = []
    i = 0
    n = len(lines)
    # 找到正文开始（第一个 ## 之后）
    # 跳过标题、引用、到正文
    idx = 0
    # 定位到第一个二级标题
    while idx < n and not re.match(r'^##\s+', lines[idx]):
        idx += 1
    i = idx
    cur_sec = None
    buf = []
    def flush_sec():
        nonlocal buf
        if cur_sec is not None:
            out_blocks.append((cur_sec, buf))
        buf = []
    while i < n:
        ln = lines[i]
        s = ln.strip()
        m2 = re.match(r'^##\s+(.+)$', ln)
        if m2:
            flush_sec()
            cur_sec = m2.group(1).strip()
            i += 1
            continue
        if cur_sec is None:
            i += 1
            continue
        m3 = re.match(r'^###\s+(.+)$', ln)
        if m3:
            buf.append(('h3', m3.group(1).strip()))
            i += 1
            continue
        # 表格块
        if s.startswith('|'):
            tbl = [s]
            j = i + 1
            while j < n and lines[j].strip().startswith('|'):
                tbl.append(lines[j].strip())
                j += 1
            buf.append(('table', tbl))
            i = j
            continue
        # 列表块
        if re.match(r'^-\s+', s) or re.match(r'^\d+\.\s+', s):
            items = []
            ordered = bool(re.match(r'^\d+\.\s+', s))
            j = i
            while j < n and (re.match(r'^-\s+', lines[j].strip()) or re.match(r'^\d+\.\s+', lines[j].strip())):
                t = lines[j].strip()
                t = re.sub(r'^-\s+', '', t)
                t = re.sub(r'^\d+\.\s+', '', t)
                items.append(t)
                j += 1
            buf.append(('list', (ordered, items)))
            i = j
            continue
        # 图片
        m = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)', s)
        if m:
            buf.append(('img', m.group(1)))
            i += 1
            continue
        # 引用块
        if s.startswith('>'):
            buf.append(('quote', s.lstrip('> ').strip()))
            i += 1
            continue
        # 分隔线
        if s == '---':
            i += 1
            continue
        # 空行
        if s == '':
            i += 1
            continue
        # 普通段落
        buf.append(('p', s))
        i += 1
    flush_sec()
    return out_blocks

blocks = build_body()

# 生成 HTML 正文
def render_block(b):
    kind = b[0]
    if kind == 'h3':
        return f'<h3>{inline(esc(b[1]))}</h3>'
    if kind == 'p':
        return f'<p>{inline(esc(b[1]))}</p>'
    if kind == 'quote':
        return f'<blockquote>{inline(esc(b[1]))}</blockquote>'
    if kind == 'img':
        return f'<div class="figure"><img src="data:image/png;base64,{img_b64}" alt="{esc(b[1])}"/><p class="figcaption">评测报告 + 溯源高亮并排图</p></div>'
    if kind == 'list':
        ordered, items = b[1]
        tag = 'ol' if ordered else 'ul'
        lis = ''.join(f'<li>{inline(esc(it))}</li>' for it in items)
        return f'<{tag}>{lis}</{tag}>'
    if kind == 'table':
        rows = []
        for ln in b[1]:
            cells = [c.strip() for c in ln.strip().strip('|').split('|')]
            rows.append(cells)
        if len(rows) >= 2 and all(re.fullmatch(r':?-{2,}:?', c) for c in rows[1]):
            rows = rows[:1] + rows[2:]
        if not rows:
            return ''
        out = ['<table>']
        out.append('<thead><tr>' + ''.join(f'<th>{inline(esc(c))}</th>' for c in rows[0]) + '</tr></thead>')
        out.append('<tbody>')
        for r in rows[1:]:
            out.append('<tr>' + ''.join(f'<td>{inline(esc(c))}</td>' for c in r) + '</tr>')
        out.append('</tbody></table>')
        return '\n'.join(out)
    return ''

body_html_parts = []
for sec, bks in blocks:
    sec_html = [f'<h2 id="sec-{len(body_html_parts)}">{inline(esc(sec))}</h2>']
    for bk in bks:
        sec_html.append(render_block(bk))
    body_html_parts.append('\n'.join(sec_html))

body_html = '\n\n'.join(body_html_parts)

# 目录
toc_items = []
for idx, sec in enumerate([s for s, _ in blocks]):
    toc_items.append(f'<li><a href="#sec-{idx}">{esc(sec)}</a></li>')
toc_html = '<ul class="toc">' + ''.join(toc_items) + '</ul>'

# 主标题
main_title = 'AI4S 学术研究 Agent'
subtitle = '作品介绍文档'
# 提取第一行标题
first_heading = ''
for ln in lines:
    m = re.match(r'^#\s+(.+)$', ln)
    if m:
        first_heading = m.group(1).strip()
        break

html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<style>
:root {{
  --primary: #1a5fb4;
  --accent: #3584e4;
  --dark: #1f2933;
  --text: #333333;
  --muted: #666666;
  --bg: #ffffff;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", sans-serif;
  color: var(--text);
  line-height: 1.75;
  font-size: 14px;
  margin: 0;
}}
/* 封面 */
.cover {{
  text-align: center;
  padding: 120px 60px 90px;
  border-bottom: 4px solid var(--primary);
}}
.cover .tag {{
  display: inline-block;
  font-size: 13px;
  color: var(--accent);
  letter-spacing: 4px;
  margin-bottom: 30px;
}}
.cover h1 {{
  font-size: 34px;
  color: var(--dark);
  margin: 0 0 14px;
}}
.cover .subtitle {{
  font-size: 20px;
  color: var(--muted);
  margin: 0 0 40px;
}}
.cover .meta {{
  font-size: 13px;
  color: var(--muted);
}}
/* 目录 */
.toc-wrap {{
  padding: 40px 60px;
  border-bottom: 1px solid #e5e5e5;
}}
.toc-wrap h2 {{ color: var(--primary); font-size: 20px; }}
.toc {{
  list-style: none;
  padding: 0;
}}
.toc li {{
  padding: 7px 0;
  border-bottom: 1px dashed #ddd;
}}
.toc a {{
  color: var(--text);
  text-decoration: none;
  font-size: 14px;
}}
.toc a::before {{
  content: counter(li) ". ";
}}
/* 正文 */
.content {{
  padding: 30px 60px 60px;
}}
h2 {{
  color: var(--primary);
  font-size: 22px;
  margin: 34px 0 16px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--accent);
}}
h3 {{
  color: var(--dark);
  font-size: 16px;
  margin: 22px 0 10px;
}}
p {{ margin: 10px 0; }}
strong {{ color: var(--dark); }}
code {{
  background: #f0f4f8;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  color: #c0392b;
}}
ul, ol {{ padding-left: 24px; }}
li {{ margin: 5px 0; }}
blockquote {{
  margin: 14px 0;
  padding: 12px 18px;
  background: #f7f9fc;
  border-left: 4px solid var(--accent);
  color: var(--muted);
}}
table {{
  border-collapse: collapse;
  width: 100%;
  margin: 14px 0;
  font-size: 13px;
}}
th, td {{
  border: 1px solid #d5dbe2;
  padding: 8px 12px;
  text-align: left;
  vertical-align: top;
}}
th {{
  background: var(--primary);
  color: #ffffff;
  font-weight: 600;
}}
tr:nth-child(even) td {{
  background: #f6f9fc;
}}
.figure {{
  margin: 20px 0;
  text-align: center;
}}
.figure img {{
  max-width: 100%;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
}}
.figcaption {{
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
}}
</style>
</head>
<body>

<div class="cover">
  <div class="tag">传智杯 · 参赛作品</div>
  <h1>{esc(first_heading or main_title)}</h1>
  <div class="subtitle">AI4S 学术研究 Agent</div>
  <div class="meta">可验证 · 可复现 · 可溯源的智能文献助手</div>
</div>

<div class="toc-wrap">
  <h2>目录</h2>
  {toc_html}
</div>

<div class="content">
{body_html}
</div>

</body>
</html>
'''

open(OUT, 'w', encoding='utf-8').write(html_doc)
print('HTML written, size=', len(html_doc.encode('utf-8')))
print('sections:', [s for s, _ in blocks])
