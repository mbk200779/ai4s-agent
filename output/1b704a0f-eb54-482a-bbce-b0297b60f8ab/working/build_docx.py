# -*- coding: utf-8 -*-
"""用 python-docx 生成专业的《作品介绍文档》Word 文档。"""
import re
from docx import Document
from docx.shared import Pt, RGBColor, Cm, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

MD = r'D:\workbuddy\2026-10-21-16-54-36\output\1b704a0f-eb54-482a-bbce-b0297b60f8ab\stage1\final_draft.md'
IMG = r'D:\workbuddy\2026-10-21-16-54-36\docs\assets\generate_defense_image.png'
OUT = r'C:\Users\36574\Desktop\作品介绍文档.docx'

PRIMARY = RGBColor(0x1a, 0x5f, 0xb4)
ACCENT = RGBColor(0x35, 0x84, 0xe4)
DARK = RGBColor(0x1f, 0x29, 0x33)
TEXT = RGBColor(0x33, 0x33, 0x33)
MUTED = RGBColor(0x66, 0x66, 0x66)
WHITE = RGBColor(0xff, 0xff, 0xff)

doc = Document()

# 页面设置 A4
for sec in doc.sections:
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(2.54)
    sec.bottom_margin = Cm(2.54)
    sec.left_margin = Cm(2.8)
    sec.right_margin = Cm(2.8)

# 设置默认字体
style = doc.styles['Normal']
style.font.name = '微软雅黑'
style.font.size = Pt(11)
style.font.color.rgb = TEXT
style.element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

def set_font(run, name='微软雅黑', size=None, color=None, bold=None, italic=None):
    run.font.name = name
    run._element.rPr.rFonts.set(qn('w:eastAsia'), name)
    if size is not None:
        run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic

def shade_cell(cell, hex_color):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_color)
    tcPr.append(shd)

def add_inline_runs(paragraph, text, base_size=11, base_color=TEXT):
    """解析行内 markdown：**加粗**、`代码`、*斜体*。"""
    tokens = re.split(r'(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)', text)
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith('**') and tok.endswith('**'):
            r = paragraph.add_run(tok[2:-2])
            set_font(r, size=base_size, color=DARK, bold=True)
        elif tok.startswith('`') and tok.endswith('`'):
            r = paragraph.add_run(tok[1:-1])
            set_font(r, name='Consolas', size=base_size - 1, color=RGBColor(0xc0, 0x39, 0x2b))
        elif tok.startswith('*') and tok.endswith('*') and len(tok) > 2:
            r = paragraph.add_run(tok[1:-1])
            set_font(r, size=base_size, color=base_color, italic=True)
        else:
            r = paragraph.add_run(tok)
            set_font(r, size=base_size, color=base_color)

def add_heading(text, level=2):
    p = doc.add_paragraph()
    p.space_before = Pt(18)
    p.space_after = Pt(8)
    if level == 2:
        size, color = 16, PRIMARY
    else:
        size, color = 13, DARK
    r = p.add_run(text)
    set_font(r, size=size, color=color, bold=True)
    # 下边框线
    if level == 2:
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement('w:pBdr')
        bottom = OxmlElement('w:bottom')
        bottom.set(qn('w:val'), 'single')
        bottom.set(qn('w:sz'), '8')
        bottom.set(qn('w:space'), '2')
        bottom.set(qn('w:color'), '3584e4')
        pBdr.append(bottom)
        pPr.append(pBdr)
    return p

def add_para(text, base_size=11, color=TEXT, align=None, space_after=6):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = 1.4
    if align:
        p.alignment = align
    add_inline_runs(p, text, base_size=base_size, base_color=color)
    return p

def add_table(headers, rows):
    t = doc.add_table(rows=1, cols=len(headers))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.style = 'Table Grid'
    hdr = t.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ''
        p = hdr[i].paragraphs[0]
        r = p.add_run(h)
        set_font(r, size=10.5, color=WHITE, bold=True)
        shade_cell(hdr[i], '1a5fb4')
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = ''
            p = cells[i].paragraphs[0]
            add_inline_runs(p, val, base_size=10)
    # 表格后加空行
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t

# 读取 markdown
md_text = open(MD, encoding='utf-8').read()
lines = md_text.split('\n')

# 提取主标题
main_title = ''
for ln in lines:
    m = re.match(r'^#\s+(.+)$', ln)
    if m:
        main_title = m.group(1).strip()
        break

# ========== 封面 ==========
for _ in range(6):
    doc.add_paragraph()
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run('传智杯 · 参赛作品')
set_font(r, size=12, color=ACCENT)
r._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_before = Pt(20)
r = p.add_run(main_title)
set_font(r, size=26, color=DARK, bold=True)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_before = Pt(10)
r = p.add_run('AI4S 学术研究 Agent')
set_font(r, size=18, color=MUTED)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_before = Pt(28)
r = p.add_run('可验证 · 可复现 · 可溯源的智能文献助手')
set_font(r, size=12, color=MUTED)

# 分页
doc.add_page_break()

# ========== 目录 ==========
add_heading('目录', level=2)
toc_entries = []
for ln in lines:
    m = re.match(r'^##\s+(.+)$', ln)
    if m:
        toc_entries.append(m.group(1).strip())
for i, entry in enumerate(toc_entries, 1):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(4)
    r = p.add_run(f'{entry}')
    set_font(r, size=11, color=TEXT)

doc.add_page_break()

# ========== 正文解析 ==========
def parse_blocks():
    blocks = []
    i = 0
    n = len(lines)
    # 定位第一个二级标题
    while i < n and not re.match(r'^##\s+', lines[i]):
        i += 1
    while i < n:
        ln = lines[i]
        s = ln.strip()
        m2 = re.match(r'^##\s+(.+)$', ln)
        if m2:
            blocks.append(('h2', m2.group(1).strip()))
            i += 1
            continue
        m3 = re.match(r'^###\s+(.+)$', ln)
        if m3:
            blocks.append(('h3', m3.group(1).strip()))
            i += 1
            continue
        if s.startswith('|'):
            tbl = [s]
            j = i + 1
            while j < n and lines[j].strip().startswith('|'):
                tbl.append(lines[j].strip())
                j += 1
            blocks.append(('table', tbl))
            i = j
            continue
        if re.match(r'^-\s+', s) or re.match(r'^\d+\.\s+', s):
            ordered = bool(re.match(r'^\d+\.\s+', s))
            items = []
            j = i
            while j < n and (re.match(r'^-\s+', lines[j].strip()) or re.match(r'^\d+\.\s+', lines[j].strip())):
                t = lines[j].strip()
                t = re.sub(r'^-\s+', '', t)
                t = re.sub(r'^\d+\.\s+', '', t)
                items.append(t)
                j += 1
            blocks.append(('list', (ordered, items)))
            i = j
            continue
        m = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)', s)
        if m:
            blocks.append(('img', m.group(1)))
            i += 1
            continue
        if s.startswith('>'):
            blocks.append(('quote', s.lstrip('> ').strip()))
            i += 1
            continue
        if s == '---' or s == '':
            i += 1
            continue
        blocks.append(('p', s))
        i += 1
    return blocks

blocks = parse_blocks()

for kind, val in blocks:
    if kind == 'h2':
        add_heading(val, level=2)
    elif kind == 'h3':
        add_heading(val, level=3)
    elif kind == 'p':
        add_para(val)
    elif kind == 'quote':
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.5)
        r = p.add_run(val)
        set_font(r, size=10.5, color=MUTED, italic=True)
    elif kind == 'list':
        ordered, items = val
        for idx, it in enumerate(items, 1):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_after = Pt(3)
            if ordered:
                r = p.add_run(f'{idx}. ')
                set_font(r, size=11, color=PRIMARY, bold=True)
                add_inline_runs(p, it, base_size=11)
            else:
                r = p.add_run('• ')
                set_font(r, size=11, color=ACCENT, bold=True)
                add_inline_runs(p, it, base_size=11)
    elif kind == 'img':
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run()
        run.add_picture(IMG, width=Cm(15.5))
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = cap.add_run('图：评测报告 + 溯源高亮并排图')
        set_font(r, size=9, color=MUTED)
    elif kind == 'table':
        # 解析表格
        rows = []
        for ln in val:
            cells = [c.strip() for c in ln.strip().strip('|').split('|')]
            rows.append(cells)
        if len(rows) >= 2 and all(re.fullmatch(r':?-{2,}:?', c) for c in rows[1]):
            rows = rows[:1] + rows[2:]
        if rows:
            add_table(rows[0], rows[1:])

# 有序列表编号问题：重新处理 list 块，但上面已经生成，这里用后处理修正
# 实际上为了简单，重新遍历一次处理有序列表编号

doc.save(OUT)
print('DOCX saved:', OUT)
