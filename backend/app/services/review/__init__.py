"""
综述生成：从「摘要拼接」改为「观点聚类 + 冲突标注」。

对外只暴露 `ReviewPipeline`（`app/services/review/service.py`）；
本包内各模块的边界见各自文件头：
- `pool.py`     证据池（覆盖优先，逐论文补检）
- `claims.py`   claim 抽取（保极性、引文回贴原文）
- `cluster.py`  观点聚类（极性是簇键的一部分，轴先做归并）
- `conflict.py` 冲突检测（规则召回 + LLM 复核）
- `generate.py` 正文生成与渲染（硬约束在渲染器里，不在 prompt 里）

**导入顺序有意义**：`prompts` 没有任何包内依赖，必须先导入。
其余模块都写 `from app.services.review import prompts`，
若 `__init__` 先导入了 `claims`，那次导入会撞上"包初始化到一半"的状态。
"""
from app.services.review import prompts  # noqa: F401  （必须最先，见上文）
from app.services.review.claims import Claim, extract_claims
from app.services.review.cluster import Cluster, cluster_claims, canonical_axes
from app.services.review.conflict import detect_conflicts
from app.services.review.generate import render_markdown, write_sections
from app.services.review.pool import build_pool
from app.services.review.prompts import REVIEW_PROMPT_VERSION, config_block
from app.services.review.service import ReviewPipeline

__all__ = [
    "Claim",
    "Cluster",
    "REVIEW_PROMPT_VERSION",
    "ReviewPipeline",
    "build_pool",
    "canonical_axes",
    "cluster_claims",
    "config_block",
    "detect_conflicts",
    "extract_claims",
    "prompts",
    "render_markdown",
    "write_sections",
]
