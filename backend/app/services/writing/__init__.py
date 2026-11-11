"""
写作辅助：让「写出去的每一句都可追溯」。

包内分工：
- `prompts`   分段计划 + 逐段写作两套 prompt（自带版本线 `writing-v1.x.x`）
- `evidence`  分段证据池 + **全局** 引#N 编号（同一 chunk 全篇一个号）
- `sentences` 切句、解析行内引用、判 claim/bridge、数值落地校验
- `references` References 只由库内 `papers` 表生成，并对正文做反向审计
- `compose`   计划 → 逐段写 → 句级绑定 → 无证据段占位
- `service`   `WritingPipeline`（对外唯一入口）

导入顺序：`prompts` 必须最先 import。
`evidence` / `sentences` / `references` 会在模块级 import `prompts` 的常量，
包初始化到一半时如果先 import 它们，会拿到半初始化的包。
"""
from app.services.writing import prompts  # noqa: F401  （顺序有意义，见上）
from app.services.writing.evidence import CiteIndex, collect_section_evidence
from app.services.writing.compose import Draft, plan_sections, write_section
from app.services.writing.references import build_references, verify_references
from app.services.writing.sentences import parse_units, sentence_type, numbers_grounded
from app.services.writing.service import WritingPipeline

__all__ = [
    "CiteIndex",
    "collect_section_evidence",
    "Draft",
    "plan_sections",
    "write_section",
    "build_references",
    "verify_references",
    "parse_units",
    "sentence_type",
    "numbers_grounded",
    "WritingPipeline",
]
