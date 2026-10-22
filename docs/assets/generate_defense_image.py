#!/usr/bin/env python3
"""
生成答辩素材：评测报告 + 溯源截图 并排图。
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_rounded_rect(draw, xy, radius, fill, outline=None, width=1):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def main():
    width, height = 1800, 900
    img = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(img)

    title_font = load_font(36)
    section_font = load_font(28)
    body_font = load_font(22)
    small_font = load_font(18)
    code_font = load_font(20)

    # 标题
    draw.text((width // 2, 30), "答辩素材：评测报告  +  溯源高亮", fill="#0f172a", font=title_font, anchor="mt")

    # 左侧：评测报告面板
    left_x = 40
    top_y = 90
    panel_w = 820
    panel_h = 760
    draw_rounded_rect(draw, (left_x, top_y, left_x + panel_w, top_y + panel_h), 12, "#ffffff", "#cbd5e1", 2)
    draw.text((left_x + 30, top_y + 25), "make eval 评测报告", fill="#1e293b", font=section_font)

    metrics = [
        ("准确率", "100.00%", "(40/40)", "#2563eb"),
        ("引用命中率", "100.00%", "(40/40)", "#059669"),
        ("拒答率", "0.00%", "(0/40)", "#dc2626"),
        ("平均延迟", "0.0 ms", "", "#7c3aed"),
    ]

    y = top_y + 80
    row_h = 70
    for label, value, extra, color in metrics:
        draw_rounded_rect(draw, (left_x + 30, y, left_x + panel_w - 30, y + row_h), 8, "#f1f5f9")
        draw.text((left_x + 50, y + row_h // 2), label, fill="#334155", font=body_font, anchor="lm")
        draw.text((left_x + panel_w - 50, y + row_h // 2), f"{value} {extra}", fill=color, font=body_font, anchor="rm")
        y += row_h + 12

    # 简单条形图
    chart_y = y + 30
    bar_h = 28
    max_bar_w = 500
    for i, (label, value, _, color) in enumerate(metrics[:3]):
        pct = float(value.replace("%", "")) / 100.0
        bar_w = int(max_bar_w * pct)
        draw.text((left_x + 30, chart_y + i * (bar_h + 20) + bar_h // 2), label, fill="#475569", font=small_font, anchor="lm")
        draw_rounded_rect(draw, (left_x + 160, chart_y + i * (bar_h + 20), left_x + 160 + bar_w, chart_y + i * (bar_h + 20) + bar_h), 6, color)

    # output_hash + diff=0
    diff_y = chart_y + 3 * (bar_h + 20) + 40
    draw.text((left_x + 30, diff_y), "输出哈希：7893fc5390e37b709", fill="#0f172a", font=code_font)
    draw.text((left_x + 30, diff_y + 35), "连续两次运行 output_hash 一致，diff = 0", fill="#059669", font=body_font)

    # 右侧：溯源截图面板
    right_x = 940
    draw_rounded_rect(draw, (right_x, top_y, right_x + panel_w, top_y + panel_h), 12, "#ffffff", "#cbd5e1", 2)
    draw.text((right_x + 30, top_y + 25), "溯源高亮：问答 → PDF 原位", fill="#1e293b", font=section_font)

    # 问答卡片
    card_x = right_x + 30
    card_y = top_y + 80
    card_w = panel_w - 60
    card_h = 240
    draw_rounded_rect(draw, (card_x, card_y, card_x + card_w, card_y + card_h), 10, "#f8fafc", "#e2e8f0", 1)
    draw.text((card_x + 20, card_y + 20), "Q：Transformer 训练使用的 Adam 优化器 β1/β2 是多少？", fill="#334155", font=body_font)
    draw.text((card_x + 20, card_y + 60), "A：β1 = 0.9，β2 = 0.999", fill="#0f172a", font=body_font)

    # 可点击引用标签
    tag_x = card_x + 20
    tag_y = card_y + 100
    tag_text = "[引#1: Attention Is All You Need p.8]"
    bbox = draw.textbbox((0, 0), tag_text, font=small_font)
    tag_w = bbox[2] - bbox[0] + 24
    tag_h = bbox[3] - bbox[1] + 12
    draw_rounded_rect(draw, (tag_x, tag_y, tag_x + tag_w, tag_y + tag_h), 6, "#fef3c7", "#f59e0b", 2)
    draw.text((tag_x + 12, tag_y + 6), tag_text, fill="#92400e", font=small_font)

    # 来源卡片
    src_y = card_y + 160
    draw_rounded_rect(draw, (card_x, src_y, card_x + card_w, src_y + 60), 8, "#f1f5f9")
    draw.text((card_x + 15, src_y + 18), "[1] Attention Is All You Need (arXiv:1706.03762, 页 8)", fill="#475569", font=small_font)

    # PDF 页面示意
    pdf_x = card_x
    pdf_y = card_y + card_h + 30
    pdf_w = card_w
    pdf_h = 340
    draw_rounded_rect(draw, (pdf_x, pdf_y, pdf_x + pdf_w, pdf_y + pdf_h), 8, "#ffffff", "#94a3b8", 1)
    draw.text((pdf_x + 20, pdf_y + 15), "PDF 第 8 页", fill="#64748b", font=small_font)

    # 模拟正文行
    line_y = pdf_y + 55
    for _ in range(6):
        draw.rounded_rectangle((pdf_x + 20, line_y, pdf_x + pdf_w - 20, line_y + 14), radius=4, fill="#e2e8f0")
        line_y += 28

    # 高亮框（覆盖 β1/β2 行）
    hl_x = pdf_x + 18
    hl_y = pdf_y + 55 + 2 * 28 - 4
    hl_w = pdf_w - 36
    hl_h = 26
    draw.rectangle((hl_x, hl_y, hl_x + hl_w, hl_y + hl_h), fill="#fde047", outline="#facc15", width=2)
    draw.text((hl_x + 10, hl_y + 3), "... β1 = 0.9, β2 = 0.999 ...", fill="#713f12", font=small_font)

    out_path = Path(__file__).with_suffix(".png")
    img.save(out_path, "PNG")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
