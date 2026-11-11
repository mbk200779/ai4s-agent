/**
 * 写作辅助接口（09）的类型定义。
 *
 * 结构就三层，界面只做一件事：把这三层的关系摆出来。
 *
 *   句子（type: claim/bridge, cite_nos）→ 引#N → chunk（原文/页/章节）→ [K] 文献
 *
 * `cite_nos` 是 chunk 级编号（正文里的 `[引#N]`）；
 * `ref_nos` 是文献级编号（References 里的 `[K]`）。
 * 两套编号都随句子一起返回，界面不需要自己算映射，导出时也不会断链。
 */

export type SentenceType = 'claim' | 'bridge';

/** 一句所引证据的展开信息。后端生成时就绑好了，点引用不需要再发请求。 */
export interface SentenceEvidence {
  n: number;
  chunk_id: string;
  paper_id: string;
  arxiv_id: string;
  title: string;
  page: number;
  section: string;
  kind: string;
  text: string;
  truncated?: boolean;
  score?: number;
  relevance?: number | null;
  ref_no?: number | null;
  /** 被哪几段的检索命中过（不代表正文引用） */
  hit_in?: string[];
}

export interface WritingSentence {
  id: string;
  section_id: string;
  order: number;
  type: SentenceType | string;
  text: string;
  cite_nos: number[];
  chunk_ids: string[];
  ref_nos: number[];
  evidence: SentenceEvidence[];
  /** 是否含典型连接词（"因此""在此基础上"），用于给 bridge 一个可解释的标注。 */
  bridge_cue?: boolean;
  /** `server_bridge` 表示这句过渡由系统插入，不含任何事实断言。 */
  source?: string;
}

export interface DroppedSentence {
  reason: string;
  text?: string;
  missing?: string[];
  declared?: string;
  why?: string;
}

export interface WritingSection {
  id: string;
  no: number;
  title: string;
  intent?: string;
  query?: string;
  sentences: WritingSentence[];
  placeholder: boolean;
  gap: string;
  evidence: number[];
  evidence_candidates?: number;
  evidence_low_relevance?: number;
  dropped: DroppedSentence[];
}

export interface WritingReference {
  ref_no: number;
  paper_id: string;
  title: string;
  authors: string[];
  year?: number | null;
  venue?: string;
  doi?: string;
  arxiv_id?: string;
  chunk_nos: number[];
  checked: boolean;
  issues: string[];
}

export interface WritingRefMapRow {
  n: number;
  chunk_id: string;
  /** 关系库 papers 真实 id，与句子 evidence 同口径 */
  paper_id: string;
  /** chunk payload 的 `{arxiv_id}::{version}` 复合键，仅备查 */
  chunk_paper_ref?: string;
  arxiv_id: string;
  title: string;
  page: number;
  section: string;
  ref_no?: number | null;
  /** 真的引用了这段原文的句子 id；空数组 = 检索到但没引用 */
  used_in: string[];
  /** 被哪几段的检索命中过 */
  hit_in?: string[];
}

export interface WritingStats {
  sections: number;
  sentences: number;
  claim_sentences: number;
  bridge_sentences: number;
  cited_sentences: number;
  uncited_claim: number;
  placeholders: number;
  sentences_dropped: number;
  dropped_by_reason: Record<string, number>;
  refs: number;
  refs_rejected: number;
  fabricated_refs: number;
  evidence_chunks: number;
  papers: number;
  degraded: boolean;
  prompt_version?: string;
  config_version?: string;
  fingerprint?: string;
}

export interface WritingConfig {
  version: string;
  prompt_version: string;
  fingerprint: string;
  thresholds: Record<string, number | boolean>;
}

export interface WritingResponse {
  topic: string;
  markdown: string;
  sections: WritingSection[];
  references: WritingReference[];
  ref_map: WritingRefMapRow[];
  stats: WritingStats;
  config?: WritingConfig;
  export_key?: string | null;
  export_json_key?: string | null;
  rejected: boolean;
  reject_reason?: string | null;
  degraded: boolean;
  trace?: Record<string, unknown> | null;
}

/** 删句原因的读法。后端只给 code，中文解释放在这一层，改文案不用动后端。 */
export const DROP_REASON_LABEL: Record<string, string> = {
  no_citation: '结论句没有引用',
  cite_out_of_section: '引用了本段之外的编号',
  number_not_grounded: '数字在证据里找不到',
  reference_section: '模型自己写了参考文献',
  bracket_ref_stripped: '清掉论文式方括号引用',
  bridge_fragment: '过渡句碎片',
  empty_statement: '空句子',
};

export const SENTENCE_STYLE: Record<
  string,
  { label: string; hint: string; underline: string; badge: string }
> = {
  claim: {
    label: '结论',
    hint: '陈述事实或结论，必须带引用',
    underline: '',
    badge: 'border-[var(--line)] text-[var(--ink-3)]',
  },
  bridge: {
    label: '无需引用',
    hint: '过渡/连接句，不含事实断言',
    underline: 'border-dashed border-b border-[var(--ink-3)]/50',
    badge: 'border-[var(--ink-3)]/40 text-[var(--ink-3)]',
  },
};
