"""
去重与关系判定。

模块分工（顺序就是判定的推理顺序）：
    fingerprint  把正文压成可比较的指纹（MinHash 草图 + 段落 simhash + 摘要向量）
    similarity   算一组可解释的数（重合率、段落命中、标题/作者/语义）
    relation     由数得出关系类型（version_of / possible_duplicate / related）+ 置信度 + 依据
    diff         差异摘要（多了哪些章节、几段是新写的、哪些数字变了）
    store        指纹与判定记录的落库
    service      编排：预检 → 决策 → 记录 → 入库后落指纹
"""
from app.services.dedupe.diff import diff_from_texts, diff_versions, render_diff_markdown
from app.services.dedupe.fingerprint import (
    Fingerprint,
    containment,
    cosine,
    from_chunks,
    from_pdf,
    from_text,
    hamming,
    jaccard_from_sketch,
    minhash_sketch,
    paragraph_hashes,
    simhash64,
    split_paragraphs,
)
from app.services.dedupe.relation import (
    DISTINCT,
    POSSIBLE_DUPLICATE,
    RELATED,
    RELATION_LABEL,
    VERSION_OF,
    classify,
    suggest_actions,
    version_hint,
)
from app.services.dedupe.service import DedupeService, get_service
from app.services.dedupe.similarity import PeerFingerprint, Similarity, compare, prefilter

__all__ = [
    "DedupeService",
    "get_service",
    "Fingerprint",
    "PeerFingerprint",
    "Similarity",
    "compare",
    "prefilter",
    "classify",
    "suggest_actions",
    "version_hint",
    "RELATION_LABEL",
    "VERSION_OF",
    "POSSIBLE_DUPLICATE",
    "RELATED",
    "DISTINCT",
    "diff_versions",
    "diff_from_texts",
    "render_diff_markdown",
    "from_pdf",
    "from_chunks",
    "from_text",
    "simhash64",
    "minhash_sketch",
    "paragraph_hashes",
    "split_paragraphs",
    "jaccard_from_sketch",
    "containment",
    "hamming",
    "cosine",
]
