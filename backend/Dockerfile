# AI4S 学术研究 Agent 后端镜像
# 关键设计决策：单镜像同时承载 API 与 Celery Worker，仅通过 command 区分，
# 保证代码版本、模型缓存、依赖完全一致，避免 worker 与 API 行为漂移。
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖：编译工具、PDF/图片处理库
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
