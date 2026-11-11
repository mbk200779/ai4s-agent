/** 论文库（数据库管理）接口的返回结构。字段与后端 library_service.list_papers 一一对应。 */

export type PaperStatus = 'ready' | 'running' | 'failed' | 'duplicate' | 'unknown';

export interface LibraryPaper {
  id: string;
  title: string;
  authors: string[];
  arxiv_id: string | null;
  year: number | null;
  venue: string | null;
  doi: string | null;
  has_abstract: boolean;
  project_id: string;
  project_name: string;
  stored_at: string;
  updated_at: string;
  version_id: string | null;
  version_label: string | null;
  version_count: number;
  pages: number | null;
  chunks: number;
  refs: number;
  citations: number;
  ingested_at: string | null;
  pipeline_version: string | null;
  status: PaperStatus;
  status_label: string;
  stage: string | null;
  has_pdf: boolean;
  deletable: boolean;
  blocked_reason: string | null;
}

export interface LibrarySummary {
  papers: number;
  ready: number;
  running: number;
  failed: number;
  chunks: number;
  pages: number;
  versions: number;
  projects: number;
  without_pdf: number;
}

export interface LibraryProject {
  id: string;
  name: string;
  papers: number;
}

export interface LibraryResponse {
  items: LibraryPaper[];
  summary: LibrarySummary;
  projects: LibraryProject[];
}

export interface DeleteResponse {
  deleted: { id: string; title: string; arxiv_id: string | null; versions: number }[];
  deleted_count: number;
  requested: number;
  removed: { vectors: number; metas: number; pdfs: number; versions: number; chunks: number };
  warnings: string[];
  summary: LibrarySummary;
  elapsed_ms: number;
}
