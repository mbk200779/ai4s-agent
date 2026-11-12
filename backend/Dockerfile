# AI4S 学术研究 Agent 后端镜像
# 关键设计决策：单镜像同时承载 API 与 Celery Worker，仅通过 command 区分，
# 保证代码版本、模型缓存、依赖完全一致，避免 worker 与 API 行为漂移。
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖：编译工具、PDF/图片处理库
# 注意：Debian trixie 中 libgl1-mesa-glx 已更名为 libgl1，libglib2.0-0 更名为 libglib2.0-0t64
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0t64 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（走国内镜像源，加速下载）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 中文字体：python:slim 默认不含 CJK 字体，matplotlib 渲染中文标题会变成方框。
# 单独成层放在 pip 之后，修改字体不会触发依赖层重建。
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# OCR 引擎：扫描件（无文本层）靠 PyMuPDF 的 get_textpage_ocr 调 tesseract 出文字。
# 官方测试用例含 1706.03762v1_img.pdf 这类纯图像 PDF，没有这一步解析结果就是空文本。
# chi_sim 顺带装上：中文扫描件同理，缺语言包时 get_textpage_ocr 会直接抛异常。
# 位置放在 pip 之后：apt 层变动不该让整个 Python 依赖重装一遍。
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# 下载 embedding 模型到镜像层（可选；运行时也会自动下载到 /app/.cache）
# 关键设计决策：模型落盘 /app/.cache，挂载为命名卷，防止容器重建导致重复下载与随机 diff。
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed
ENV HF_HOME=/app/.cache/huggingface
ENV SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence_transformers

COPY app ./app
COPY scripts ./scripts
COPY eval ./eval

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
