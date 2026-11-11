"""
综述生成服务（兼容层）。

08 起真正的流水线在 `app/services/review/` 包里（证据池 → claim 抽取 → 观点聚类 →
冲突检测 → 正文渲染 → claim 表）。本文件只保留 `ReviewService` 这个入口名，
原因有两个：

1. `app/services/__init__.py` 与若干脚本按 `ReviewService` 导入，删掉会连带炸一片；
2. 端点层不需要知道流水线由几个阶段组成，它只问一件事：给我一份综述。

**这里不再保留旧的 `generate` 实现。** 旧实现是"检索 top-k → 让模型写成 Markdown"，
产出的是摘要拼接；留着它当 fallback 会让"综述"这个功能有两种语义，
出问题时也无法判断走的是哪条路。要降级只能在流水线内部降级（`degraded=true`）。
"""
from __future__ import annotations

from typing import Any

from app.services.review.service import ReviewPipeline


class ReviewService:
    """对外门面：与旧接口同形，语义已换成「观点聚类 + 冲突标注」。"""

    def __init__(self):
        self.pipeline = ReviewPipeline()

    def generate(
        self,
        topic: str,
        arxiv_ids: list[str] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        return self.pipeline.generate(topic, arxiv_ids=arxiv_ids, top_k=top_k)

    def get_export_url(self, key: str, expires: int = 3600) -> str:
        return self.pipeline.get_export_url(key, expires=expires)
