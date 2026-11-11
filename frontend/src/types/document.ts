/**
 * 课题级数据模型的前端契约，与 backend/app/db/schema.sql 一一对应。
 *
 * 这一层只描述「后端存了什么」，不参与界面状态：界面重构时照着这些类型取数，
 * 就不会再出现「论文是全局对象、版本是字符串后缀」这种耦合。
 *
 * 命名约定：
 * - 主键统一 `id`，与后端 uuid 前缀一致（proj_ / pap_ / ver_ / chk_ / ref_ / cit_ / ans_）
 * - 时间统一 ISO-8601 UTC 字符串，例如 "2026-11-09T10:24:31Z"
 * - 需要顺序或成对语义的集合用数组（bbox 是 [x0, y0, x1, y1]）
 */

/** 课题：数据模型的最小对象。论文必须归属某个课题。 */
export interface Project {
  id: string;
  name: string;
  research_question: string | null;
  status: 'active' | 'archived';
  created_by: string;
  created_at: string;
  updated_at: string;
}

/** 论文条目：一个课题下的一篇文献，不含版本信息。 */
export interface Paper {
  id: string;
  project_id: string;
  /** arXiv ID；本地 PDF 上传时为 upload_<hash> */
  arxiv_id: string | null;
  title: string;
  authors: string[];
  year: number | null;
  venue: string | null;
  abstract: string | null;
  doi: string | null;
  created_at: string;
  updated_at: string;
}

/** 论文版本：v1 / v7 各自独立，共享同一个 paper。 */
export interface PaperVersion {
  id: string;
  paper_id: string;
  version_label: string;
  /** PDF 字节 sha256 */
  file_hash: string;
  /** 解析后正文 sha256，用于「换排版但内容没变」的去重 */
  content_hash: string | null;
  ingested_at: string;
  pipeline_version: string;
  pdf_s3_key: string | null;
  page_count: number | null;
  /** pending=待解析 indexed=已入库 failed=解析失败 duplicate=命中已有论文被跳过 */
  status: 'pending' | 'indexed' | 'failed' | 'duplicate';
  /** 1 = 当前展示版本 */
  is_current: number;

  // ---- 异步解析的进度与断点（docs/2.7）----
  /** 当前这次解析的 Celery 任务 id，进度查询按它查 */
  task_id: string | null;
  /**
   * 过程阶段。status 是"结论"，stage 是"现在走到哪一步"：
   * queued → downloading → parsing → structuring → indexed
   * 另有 failed / duplicate / reused 三个终态。
   */
  stage: IngestStage;
  /** 已完成页数（进度条分子） */
  progress_page: number;
  /** 下一页从几开始，断点续跑用 */
  parse_cursor: number;
  /** 已写入的片段数，保证 Qdrant 点序号跨批次连续 */
  index_cursor: number;
  chunk_count: number;
  ref_count: number;
  citation_count: number;
  /** 已尝试次数，超过上限置 failed */
  attempt: number;
  error_message: string | null;
  error_trace: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** file_hash 命中时复用的源版本 id */
  reused_from: string | null;
}

export type ChunkType = 'text' | 'table' | 'figure' | 'formula' | 'reference';

/** 文本块：溯源的最小定位单元。 */
export interface Chunk {
  id: string;
  version_id: string;
  chunk_index: number;
  page: number | null;
  /** PDF 坐标系 [x0, y0, x1, y1] */
  bbox: number[] | null;
  /** 章节路径，如 "3 Method > 3.2 Scaled Attention" */
  section_path: string | null;
  chunk_type: ChunkType;
  table_id: string | null;
  latex: string | null;
  text: string;
  token_count: number | null;
  /** 与 Qdrant 点一一对应 */
  qdrant_point_id: string | null;
  created_at: string;
}

/** 参考文献条目（论文自己列出的那条）。 */
export interface PaperRef {
  id: string;
  version_id: string;
  ref_index: number;
  raw_text: string;
  parsed_title: string | null;
  parsed_authors: string[];
  parsed_year: number | null;
  /** 指向课题内已入库论文；未匹配为 null */
  matched_paper_id: string | null;
  match_score: number | null;
  match_method: 'arxiv_id' | 'title_exact' | 'title_fuzzy' | 'author_year' | 'none' | null;
}

/** 正文中某句话引用了某条参考文献的位置。 */
export interface Citation {
  id: string;
  ref_id: string;
  chunk_id: string | null;
  page: number | null;
  bbox: number[] | null;
  /** 所在句子原文 */
  sentence: string;
  created_at: string;
}

/** 回答里的一条论断；citations 存的是 Citation.id，不是 [3] 这种编号。 */
export interface Claim {
  text: string;
  citations: string[];
  confidence?: number;
}

export interface Answer {
  id: string;
  project_id: string;
  question: string;
  claims: Claim[];
  model_version: string | null;
  prompt_version: string | null;
  retrieval_snapshot_id: string | null;
  created_at: string;
}

/** 检索快照：一次检索命中的 chunk 序列，用于回答复现。 */
export interface RetrievalSnapshot {
  id: string;
  project_id: string | null;
  query: string;
  chunk_ids: string[];
  created_at: string;
}

/**
 * 反查结果：一条引用 → 哪篇论文、哪个版本、第几页、哪一句。
 * 对应后端 v_citation_provenance 视图。
 */
export interface CitationProvenance {
  citation_id: string;
  sentence: string;
  page: number | null;
  bbox: number[] | null;
  ref_index: number;
  ref_raw_text: string;
  matched_paper_id: string | null;
  version_id: string;
  version_label: string;
  ingested_at: string;
  paper_id: string;
  paper_title: string;
  paper_arxiv_id: string | null;
  project_id: string;
  project_name: string;
  chunk_id: string | null;
  section_path: string | null;
  chunk_text: string | null;
}

/** 课题概览统计 */
export interface ProjectStats {
  papers: number;
  versions: number;
  chunks: number;
  refs: number;
}

/** 论文 + 当前版本（列表页一次取完，避免 N+1） */
export interface PaperWithVersion extends Paper {
  version_id: string | null;
  version_label: string | null;
  version_status: PaperVersion['status'] | null;
  page_count: number | null;
  ingested_at: string | null;
}

// ==================== 异步解析（docs/2.7） ====================

/** 解析过程阶段；status 是结论，stage 是进度 */
export type IngestStage =
  | 'queued'
  | 'downloading'
  | 'parsing'
  | 'structuring'
  | 'indexed'
  | 'duplicate'
  | 'reused'
  | 'failed';

/**
 * 上传响应。task_id 是解析任务的句柄，progress_url 是轮询地址
 * （把 progress 换成 events 就是 SSE 流）。
 */
export interface PaperUploadResponse {
  paper_id: string;
  arxiv_id: string;
  version: string;
  task_id: string | null;
  project_id?: string | null;
  paper_uuid?: string | null;
  version_id?: string | null;
  stage?: IngestStage | null;
  /** true = 命中 file_hash，直接复用已有解析结果（未入队） */
  reused?: boolean | null;
  progress_url?: string | null;
  detail?: string | null;
}

/** 进度快照：/api/v1/ingest/{tasks,versions}/.../progress 的返回 */
export interface IngestProgress {
  task_id: string | null;
  version_id: string | null;
  paper_id: string | null;
  project_id: string | null;
  title: string | null;
  stage: IngestStage;
  stage_label: string;
  status: PaperVersion['status'];
  page: number;
  total_pages: number | null;
  /** 0..100，阶段加权后的百分比 */
  pct: number;
  chunks: number;
  refs: number;
  citations: number;
  attempt: number;
  attempt_limit: number;
  elapsed_ms: number | null;
  /** 解析阶段按已用时间线性外推的剩余毫秒数 */
  eta_ms: number | null;
  error: string | null;
  reused_from: string | null;
  /** 到达终态（indexed / duplicate / reused / failed） */
  done: boolean;
  updated_at: string;
}
