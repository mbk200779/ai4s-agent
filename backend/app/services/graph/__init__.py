"""
库内引用图谱。

模块分工：
- `refparse.py` 从 PDF 重抽参考文献条目（不依赖 LLM，按 block 边界切）
- `match.py`    条目 → 库内论文的匹配与失败统计（分级证据 + 分数）
- `markers.py`  正文 `[12]` / `(Vaswani et al., 2017)` → 条目编号，用于边权
- `metrics.py`  PageRank / betweenness / 度，附带可直接展示的计算口径
- `insight.py`  标签规则、一句式解读、一跳推荐
- `store.py`    库内论文、版本、条目、正文块的读写
- `backfill.py` 重抽参考文献并重建引用位置
- `service.py`  编排：一次 build 产出前端直接可渲染的完整快照

与 `app.services.graph_service.GraphService`（OpenAlex 外部引用网络）并存，
两者回答不同的问题：那个问"外面谁引了它"，这个问"我收的这批之间是什么关系"。
"""
from app.services.graph.backfill import rebuild_references
from app.services.graph.insight import LABELS, RULES
from app.services.graph.match import MATCH_MIN, build_candidates, match_ref, match_refs
from app.services.graph.metrics import METHODS
from app.services.graph.refparse import RefRecord, extract, quality
from app.services.graph.service import LibraryGraphService, get_service

__all__ = [
    "LABELS",
    "MATCH_MIN",
    "METHODS",
    "RULES",
    "LibraryGraphService",
    "RefRecord",
    "build_candidates",
    "extract",
    "get_service",
    "match_ref",
    "match_refs",
    "quality",
    "rebuild_references",
]
