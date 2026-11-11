/** 库内引用图谱的数据结构（与后端 `/api/v1/graph/library` 一一对应） */

export interface LibraryNodeLabel {
  key: string;
  label: string;
  tone: string;
  hint: string;
}

export interface LibraryNodeMetrics {
  pagerank: number | null;
  pagerank_rank: number | null;
  betweenness: number | null;
  betweenness_rank: number | null;
  in_degree: number;
  out_degree: number;
  mentions_in: number;
}

export interface LibraryRecommendation {
  paper_id: string;
  title: string;
  relation: 'cites' | 'cited_by';
  why: string;
  score: number;
}

export interface LibraryNode {
  id: string;
  kind: 'library' | 'gap';
  title: string;
  arxiv_id?: string | null;
  year?: number | null;
  authors?: string[];
  version_label?: string | null;
  label: LibraryNodeLabel;
  tags: string[];
  reasons: string[];
  sentence: string;
  metrics: LibraryNodeMetrics;
  recommendations: LibraryRecommendation[];
  refs: { refs: number; matched: number };
  cites: string[];
  cited_by: string[];
}

export interface LibraryEdgeRef {
  ref_index: number | null;
  raw: string;
  score: number;
  method: string;
}

export interface LibraryEdge {
  source: string;
  target: string;
  weight: number;
  relation: string;
  match_score: number;
  match_method: string;
  weight_source: string;
  sentence: string;
  page: number | null;
  refs: LibraryEdgeRef[];
}

export interface MatchFailure {
  ref_index: number | null;
  title: string;
  raw: string;
  best_score: number;
  best_title: string;
  paper?: string;
}

export interface LibraryMatchStats {
  papers?: number;
  versions?: number;
  refs?: number;
  matched?: number;
  rate?: number;
  by_method?: Record<string, number>;
  markers_resolved?: number;
  markers_unresolved?: number;
  marker_resolution?: number;
  failures?: MatchFailure[];
  quality?: { total: number; score: number; with_year?: number; with_authors?: number; with_title?: number };
  low_quality_papers?: string[];
  by_paper?: {
    paper_id: string;
    title: string;
    version_id: string | null;
    version_label: string | null;
    refs: number;
    matched: number;
    markers: number;
    quality: { total: number; score: number };
  }[];
}

export interface MetricMethod {
  name: string;
  formula: string;
  params: Record<string, string | number>;
  scope: string;
  reads: string;
  caveat: string;
}

export interface InsightRule {
  label: string;
  cond: string;
  why: string;
}

export interface LibraryGraphPayload {
  project_id: string;
  generated_at: string;
  elapsed_ms: number;
  nodes: LibraryNode[];
  edges: LibraryEdge[];
  metrics: Record<string, unknown>;
  methods: Record<string, MetricMethod>;
  rules: InsightRule[];
  match_stats: LibraryMatchStats;
  warnings: string[];
  scope: {
    include_gaps?: boolean;
    max_gaps?: number;
    min_gap_citers?: number;
    library_nodes?: number;
    gap_nodes?: number;
    library_edges?: number;
    total_edges?: number;
  };
  empty: { title: string; hint: string; action?: string } | null;
  empty_reason: string | null;
}
