"""
实体 dataclass：与 db/schema.sql 一一对应。

为什么用 dataclass 而不是 Pydantic：
- 这层只做存储映射，不需要校验/序列化，dataclass 更轻；
- 接口层仍用 Pydantic（app/schemas），保持「外部契约」和「内部存储」分离，
  以后换 Postgres 或加字段不会把 API 响应一起改掉。
"""
import json
from dataclasses import dataclass, field, fields
from typing import Any, Optional

from app.db import dump_json, load_json


@dataclass
class Project:
    id: str
    name: str
    research_question: Optional[str] = None
    status: str = "active"
    created_by: str = "system"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Project":
        return cls(**{k: row[k] for k in row.keys()})


@dataclass
class Paper:
    id: str
    project_id: str
    arxiv_id: Optional[str] = None
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Paper":
        data = {k: row[k] for k in row.keys()}
        data["authors"] = load_json(data.get("authors"), [])
        return cls(**data)


@dataclass
class PaperVersion:
    id: str
    paper_id: str
    version_label: str
    file_hash: str
    content_hash: Optional[str] = None
    ingested_at: str = ""
    pipeline_version: str = "v1.0.0"
    pdf_s3_key: Optional[str] = None
    page_count: Optional[int] = None
    status: str = "pending"
    is_current: int = 1
    # ---- 异步解析进度与断点 ----
    task_id: Optional[str] = None
    stage: str = "queued"
    progress_page: int = 0
    parse_cursor: int = 0
    index_cursor: int = 0
    text_bytes: int = 0
    chunk_count: int = 0
    ref_count: int = 0
    citation_count: int = 0
    attempt: int = 0
    error_message: Optional[str] = None
    error_trace: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    reused_from: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "PaperVersion":
        # 老库里可能没有新增的进度列（init_db 会补，但读取可能发生在补列之前），
        # 所以这里按白名单取，缺列回落默认值，而不是直接 KeyError。
        keys = set(row.keys())
        kwargs = {f.name: row[f.name] for f in fields(cls) if f.name in keys}
        return cls(**kwargs)


@dataclass
class Chunk:
    id: str
    version_id: str
    chunk_index: int
    text: str
    page: Optional[int] = None
    bbox: Optional[list[float]] = None
    section_path: Optional[str] = None
    chunk_type: str = "text"
    table_id: Optional[str] = None
    latex: Optional[str] = None
    token_count: Optional[int] = None
    qdrant_point_id: Optional[str] = None
    # 句子级锚点文档（见 services/anchors.py）。历史数据为 None，
    # 这不是"坏了"，是"这一版解析器还没有锚点概念"，前端要区别对待。
    anchors: Optional[dict] = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Chunk":
        data = {k: row[k] for k in row.keys()}
        data["bbox"] = load_json(data.get("bbox"), None)
        raw = data.get("anchors")
        if isinstance(raw, str) and raw:
            try:
                data["anchors"] = json.loads(raw)
            except ValueError:
                data["anchors"] = None
        elif not isinstance(raw, dict):
            data["anchors"] = None
        return cls(**data)


@dataclass
class PaperRef:
    id: str
    version_id: str
    ref_index: int
    raw_text: str
    parsed_title: Optional[str] = None
    parsed_authors: list[str] = field(default_factory=list)
    parsed_year: Optional[int] = None
    matched_paper_id: Optional[str] = None
    match_score: Optional[float] = None
    match_method: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "PaperRef":
        data = {k: row[k] for k in row.keys()}
        data["parsed_authors"] = load_json(data.get("parsed_authors"), [])
        return cls(**data)


@dataclass
class Citation:
    id: str
    ref_id: str
    sentence: str
    chunk_id: Optional[str] = None
    page: Optional[int] = None
    bbox: Optional[list[float]] = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Citation":
        data = {k: row[k] for k in row.keys()}
        data["bbox"] = load_json(data.get("bbox"), None)
        return cls(**data)


@dataclass
class Answer:
    id: str
    project_id: str
    question: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None
    retrieval_snapshot_id: Optional[str] = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Answer":
        data = {k: row[k] for k in row.keys()}
        data["claims"] = load_json(data.get("claims"), [])
        return cls(**data)


__all__ = [
    "Project",
    "Paper",
    "PaperVersion",
    "Chunk",
    "PaperRef",
    "Citation",
    "Answer",
    "dump_json",
]
