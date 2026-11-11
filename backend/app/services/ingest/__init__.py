"""长文档异步解析子系统。

- limits.py    所有内存/分批/重试上限（唯一数值来源）
- progress.py  进度快照的读写与对外视图
- reuse.py     file_hash 命中时复制已有解析结果
- pipeline.py  流式下载 → 分批解析 → 分批入库 → 汇总引用
"""
