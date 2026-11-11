/**
 * 综述接口（08）的类型定义。
 *
 * 三件套：`markdown` 给人读、`claims` 是 claim 表、`conflicts` 是冲突清单。
 * 三者共用一套证据编号 `refs[].n`，正文里的 `[引#N]` 就是它。
 */

export type Stance = 'support' | 'oppose' | 'conditional';
export type Strength = 'multi_paper' | 'single_paper';
export type ConflictType =
  | 'direct_contradiction'
  | 'conditional_split'
  | 'scope_mismatch';

export interface ClaimPaper {
  arxiv_id: string;
  title?: string;
}

export interface ClaimEvidence {
  claim_id?: string;
  statement?: string;
  quote: string;
  arxiv_id?: string;
  title?: string;
  page?: number;
  section?: string;
  kind?: string;
  chunk_id?: string;
  ref?: number | null;
  quote_score?: number;
}

export interface ClaimConflictLink {
  id: string;
  with_cluster: string;
  type: ConflictType | string;
  type_label: string;
  confirmed?: boolean | null;
  reason?: string;
}

export interface ClaimRow {
  id: string;
  statement: string;
  axis: string;
  stance: Stance | string;
  stance_label: string;
  strength: Strength | string;
  paper_count: number;
  claim_count: number;
  papers: ClaimPaper[];
  conditions: string[];
  evidence: ClaimEvidence[];
  refs: number[];
  sentence_count: number;
  conflicts: ClaimConflictLink[];
  note: string;
}

export interface ReviewConflict {
  id: string;
  clusters: string[];
  axis: string;
  type: ConflictType | string;
  type_label: string;
  /** 三态：true 已确认 / false 复核否定 / null 规则召回未复核。 */
  confirmed?: boolean | null;
  reason: string;
  rule?: {
    axis_match?: string;
    axis_sim?: number;
    stances?: string[];
    type_hint?: string;
  };
  papers: ClaimPaper[];
  source: string;
}

export interface ReviewRef {
  n: number;
  chunk_id: string;
  arxiv_id: string;
  paper_id: string;
  title: string;
  page: number;
  section: string;
  kind: string;
  hit_source: string;
}

export interface ReviewStats {
  paper_count: number;
  raw_claims: number;
  cluster_count: number;
  support: number;
  oppose: number;
  conditional: number;
  single_paper: number;
  conflicts_confirmed: number;
  conflicts_pending: number;
  conflicts_rejected: number;
  dropped_sentences: number;
  violations: number;
  degraded: boolean;
}

export interface ReviewConfig {
  version: string;
  prompt_version: string;
  fingerprint: string;
  thresholds: Record<string, number>;
}

export interface ReviewResponse {
  topic: string;
  markdown: string;
  rejected: boolean;
  reject_reason?: string;
  invalid_citations: string[];
  referenced_arxiv_ids: string[];
  export_key?: string;
  claims: ClaimRow[];
  conflicts: ReviewConflict[];
  refs: ReviewRef[];
  stats: ReviewStats;
  config?: ReviewConfig;
  degraded: boolean;
  trace?: Record<string, unknown>;
}

/** 立场 → 视觉。与 07 的三档置信同一套语义：青绿=成立、琥珀=有条件、红=反向。 */
export const STANCE_STYLE: Record<
  Stance | string,
  { label: string; color: string; bg: string; border: string; hint: string }
> = {
  support: {
    label: '支持',
    color: 'var(--signal)',
    bg: 'var(--signal-soft)',
    border: 'var(--signal)',
    hint: '该簇沿轴的正面主张成立',
  },
  oppose: {
    label: '反对',
    color: 'var(--danger)',
    bg: 'rgba(220,38,38,0.08)',
    border: 'var(--danger)',
    hint: '该簇主张该轴的反向结论',
  },
  conditional: {
    label: '有条件成立',
    color: 'var(--warn)',
    bg: 'rgba(217,119,6,0.10)',
    border: 'var(--warn)',
    hint: '只在声明的条件下成立，条件外可能相反',
  },
};

export const CONFLICT_STYLE: Record<
  string,
  { label: string; color: string; hint: string }
> = {
  direct_contradiction: {
    label: '互斥结论',
    color: 'var(--danger)',
    hint: '同一条件范围内互相否定，不可能同时为真',
  },
  conditional_split: {
    label: '条件性分歧',
    color: 'var(--warn)',
    hint: '各自在自己的条件下成立，分歧来自条件差异',
  },
  scope_mismatch: {
    label: '范围不一致',
    color: 'var(--ink-3)',
    hint: '比较对象/指标不同，不构成矛盾',
  },
};
