/**
 * 结构化生成协议（SSE）的类型定义 —— 协议的唯一事实来源。
 *
 * 为什么把类型单独放一个文件，而不是散在各组件里：
 * 服务端（`app/services/qa_stream_service.py`）与前端消费的是同一份契约。
 * 契约一旦分叉，会出现"事件字段改了、前端还是老字段"这种只能在运行时
 * 用 `undefined` 体现的问题——而流式过程中它表现为"角标不出现"，不报错。
 *
 * 事件顺序（顺序有意义，前端按它推进度条）：
 *   stage(understand) → stage(retrieve) → stage(rerank) → stage(generate)
 *   → retrieval → token × N → claim × N → done
 * 每个事件都带 trace_id / seq / ts / elapsed_ms 四件套：
 * - trace_id：同一次对话的全部事件共用一个，答辩回放靠它；
 * - seq：从 1 连续递增，缺号就是丢帧（反代缓冲、连接中断都会造成缺号）；
 * - ts / elapsed_ms：墙钟时间与相对起点，用来核对"慢在哪一段"。
 *
 * 三条协议级约束（前端必须按它们实现，不能"顺手"放宽）：
 * 1. token 里没有角标。角标只存在于 claim.citations 里，渲染时按 claim.span 回填。
 * 2. claim 未到达之前，不得出现任何可点击的角标 —— 否则就是死链。
 * 3. evidence 不足的 claim 仍然会在事件里出现（status='unsupported' + reason），
 *    前端只能标注、不能丢弃，否则"这句话没有证据"这个事实就被吞了。
 */

/** 四个阶段。顺序即推进顺序，用于进度条。 */
export type StageName = 'understand' | 'retrieve' | 'rerank' | 'generate';

/** start = 阶段开始（此时还没有耗时），done = 阶段结束（带 ms 与 detail）。 */
export type StagePhase = 'start' | 'done';

/** 所有事件共有的信封字段 */
export interface EventBase {
  trace_id: string;
  seq: number;
  ts: number;
  elapsed_ms: number;
}

export interface StageEvent extends EventBase {
  stage: StageName;
  phase: StagePhase;
  label: string;
  /** 该阶段耗时；只有 done 有值，且是真实测量（不是按时长比例估的） */
  ms?: number | null;
  detail?: Record<string, unknown>;
}

export interface RetrievalChunk {
  index: number;
  chunk_id: string;
  arxiv_id: string;
  title: string;
  page: number;
  kind: string;
  score: number;
  rerank_score: number | null;
  hit_source: string | null;
  /** 该片段是否已建立句子级锚点。false 时角标只能定位到页/段，不能到句 */
  has_anchors: boolean;
  preview: string;
}

export interface RetrievalEvent extends EventBase {
  count: number;
  chunks: RetrievalChunk[];
  top_raw_score: number | null;
  reject_threshold: number | null;
  type: string;
  papers?: number;
  strategy?: string;
}

export interface TokenEvent extends EventBase {
  /** 正文增量。**保证不含任何 `[引#N]`**（服务端已摘除） */
  text: string;
}

export type ClaimStatus = 'supported' | 'unsupported';

/**
 * 三档置信（07）。
 *
 * 为什么不是布尔：科研用户读带引用的答案时本来就在分三种情况处理——
 * 直接可引用 / 有证据但结论是推出来的、要回去核一眼 / 原文里没有。
 * 把中间那档折叠进任何一边都会出问题：折进"能答"就是幻觉，折进"不能答"就把可用信息扔了。
 */
export type ConfidenceLevel = 'verified' | 'needs_check' | 'unsupported';

/** 「缺什么」的一条。code 稳定可统计，text 给人看。 */
export interface MissingItem {
  code: string;
  text: string;
  /** 整条回答里有多少句命中这条缺项（句子级不带） */
  claims?: number;
}

/** 定位精度：sentence/cell 能框到句，block/line 只能框到一段。不参与分档。 */
export interface Locate {
  precision: string | null;
  note: string | null;
}

export interface ClaimConfidence {
  level: ConfidenceLevel;
  label: string;
  score: number;
  missing: MissingItem[];
  locate?: Locate | null;
  /** 定档依据的原值（best_score / sem_score / effective_score / papers …） */
  signals?: Record<string, unknown>;
}

export interface ConfidenceConfig {
  version: string;
  prompt_version: string;
  fingerprint: string;
  thresholds: Record<string, number>;
  labels: Record<string, string>;
  descriptions: Record<string, string>;
}

/** 整条回答的档位。applicable=false 表示闲聊直答——三档对它不适用。 */
export interface Confidence {
  applicable: boolean;
  level: ConfidenceLevel | null;
  label: string;
  description: string;
  score: number;
  counts: { claims: number; verified: number; needs_check: number; unsupported: number };
  missing: MissingItem[];
  notes: string[];
  signals: Record<string, unknown>;
  config?: ConfidenceConfig;
}

/** 一条证据：把 [引#N] 落到具体句子（或单元格）上 */
export interface Evidence {
  /** 对应检索片段序号 N（从 1 开始） */
  index: number;
  status: ClaimStatus;
  /** unsupported 时说明为什么（编号越界 / 无锚点 / 匹配度过低） */
  reason: string | null;
  chunk_id: string | null;
  arxiv_id: string | null;
  title: string | null;
  page: number;
  kind: string | null;
  /** 命中的锚点 id，null 表示没落到句 */
  ref_id: string | null;
  /** sentence | cell | block | line | ocr */
  precision: string | null;
  /** 证据句原文（从锚点区间切出来的那一句） */
  text: string | null;
  /** 页面坐标（左上原点、y 向下），可直接画框 */
  rects: number[][] | null;
  bbox: number[] | null;
  /** 字面分：claim 与原句的词面/数值重合度 */
  match_score: number | null;
  /** 跨语言语义支持度（0~1）；null 表示未启用或失败 */
  sem_score?: number | null;
}

export interface ClaimEvent extends EventBase {
  claim_id: string;
  /** 在正文（token 拼接结果）里的字符区间，角标插在 span[1] 处 */
  span: [number, number];
  text: string;
  /** 证据校验过没过（05/06 口径） */
  status: ClaimStatus;
  /** 三档结论（07） */
  level: ConfidenceLevel;
  level_label: string;
  reason: string | null;
  missing: MissingItem[];
  confidence?: ClaimConfidence | null;
  citations: Evidence[];
}

/** 按角标出现顺序展开的一条引用（不去重：同一片段可能被引用多次） */
export interface AnswerRef {
  ordinal: number;
  index: number;
  claim_id: string;
  span: [number, number];
  chunk_id: string | null;
  arxiv_id: string | null;
  page: number;
  ref_id: string | null;
  text: string | null;
  bbox: number[] | null;
  rects: number[][] | null;
  precision: string | null;
  claim: string;
  match_score: number | null;
  status: ClaimStatus;
  /** 所属那句话的置信档 */
  level?: ConfidenceLevel;
  has_anchor: boolean;
}

/** 句子级锚点（与 chunks.anchors 同构，用于阅读器兜底定位） */
export interface AnchorItem {
  ref_id: string;
  kind?: string;
  precision?: string;
  start: number;
  end: number;
  rects: number[][];
  bbox?: number[];
}

export interface AnchorDoc {
  v?: number;
  page: number;
  page_size?: number[] | null;
  text_layer?: boolean;
  precision?: string;
  kind?: string;
  items: AnchorItem[];
}

export interface Citation {
  paper_id: string;
  arxiv_id: string;
  title: string;
  chunk_id: string;
  chunk_text: string;
  page: number;
  paragraph: number;
  bbox: number[] | null;
  score: number;
  rerank_score: number | null;
  hit_source: string | null;
  ref_id: string | null;
  precision: string | null;
  has_anchors: boolean;
}

/* ---------------------------------------------------------------------------
 * 12 · 不确定性状态（empty / conflict / unreadable）
 *
 * 三类状态由服务端判定并随响应下发。前端**不做任何推断**——它拿不到
 * "库内几篇、关键词命中几次、同符号取过几个值"，也就没有推断的输入。
 * 这里的类型只描述"服务端说了什么"，渲染时按它挑组件，不按字段猜。
 * ------------------------------------------------------------------------- */

export type UncertaintyState = 'empty' | 'conflict' | 'unreadable';

export interface EmptyTerm {
  term: string;
  /** 命中的**段数**（不是词频）：段数才说明"这个说法在库里存不存在" */
  chunks: number;
  hit: boolean;
}

export interface EmptyScope {
  papers: number;
  chunks: number;
  by_kind: Record<string, number>;
  titles: { arxiv_id: string; title: string }[];
}

export interface Suggestion {
  id: string;
  /** spelling / split / topic —— 三类改写思路，界面上用不同措辞 */
  kind: string;
  label: string;
  /** 点下去真的会提交的那一问。空字符串表示这条建议不可用（不该出现） */
  query: string;
  why: string;
}

export interface EmptyQuery {
  question: string;
  search_text: string;
  translated: boolean;
  type: string;
  strategy: string | null;
  terms: EmptyTerm[];
  zh_terms: string[];
  top_raw_score: number | null;
  threshold: number | null;
  coverage: {
    best_chunk_terms?: number;
    chunks_any?: number;
    need?: number;
    rare?: string[];
    rare_missing?: boolean;
  };
}

export interface EmptyNearest {
  chunk_id: string;
  arxiv_id: string;
  title: string;
  page: number;
  kind: string;
  score: number;
  preview: string;
}

export interface EmptyState {
  reason: string;
  title: string;
  detail: string;
  /** true = 这次真的没有生成答案（与"生成了但内容为空"是两件事） */
  blocking: boolean;
  scope: EmptyScope;
  query: EmptyQuery;
  /** 最近邻：身份是"系统看到了什么"，不是证据 */
  nearest: EmptyNearest[];
  suggestions: Suggestion[];
  action: Record<string, unknown>;
  version: string;
}

export interface ConflictPaper {
  arxiv_id: string;
  title: string;
  page: number;
}

export interface ConflictQuote {
  chunk_id: string;
  arxiv_id: string;
  title: string;
  page: number;
  text: string;
}

export interface ConflictSide {
  value: string;
  unit: string;
  conclusion: string;
  papers: ConflictPaper[];
  quotes: ConflictQuote[];
}

export interface Conflict {
  id: string;
  kind: string;
  /** value_diff / polarity / conditional_split / scope_mismatch */
  type: string;
  type_label: string;
  dimension: string;
  probe: string;
  sides: ConflictSide[];
  /** 结构里最要紧的一句：明写"系统不判断哪边对" */
  not_judged: string;
  why: string;
  ambiguous: {
    arxiv_id: string;
    title: string;
    page: number;
    unit: string;
    value: string;
    note: string;
  }[];
  action: Record<string, unknown>;
}

export interface UnreadableItem {
  id: string;
  /** table_empty / figure_caption_only / figure_synthetic / scan_page */
  kind: string;
  title: string;
  detail: string;
  severity: string;
  /** 被本次回答引用过 —— "你看到的结论可能不准"，与"顺带一提"分开 */
  cited: boolean;
  chunk_id: string;
  arxiv_id: string;
  paper_title: string;
  page: number;
  chunk_kind: string;
  evidence: {
    chars?: number;
    cells?: number | null;
    text_layer?: boolean;
    precision?: string | null;
    has_anchors?: boolean;
  };
  action: {
    type?: string;
    label?: string;
    arxiv_id?: string;
    page?: number;
  };
}

export interface Uncertainty {
  version: string;
  states: UncertaintyState[] | string[];
  empty: EmptyState | null;
  conflicts: Conflict[];
  unreadable: UnreadableItem[];
  /** 每类"检测了没有"。没检测与检测了没发现是两件事 */
  checked: {
    empty?: boolean;
    conflict?: { ran: boolean; symbols: string[]; claims: number };
    unreadable?: { ran: boolean; sources: number };
  };
  rules: { state: string; title: string; when: string; why: string; reads: string; next: string }[];
}

/**
 * 检索完成后立刻下发的那一份（partial）。
 *
 * 为什么要在 done 之前先发一次：空检索与"表格没复原"都是**证据层面**的事实，
 * 不必等正文生成完。先发出去，读者在等答案的三秒里就能知道"这段材料本身有问题"。
 * done 里那份是权威版（多了冲突，它要等 claim 出来才判得了）。
 */
export interface UncertaintyEvent extends EventBase, Uncertainty {
  partial?: boolean;
}

export interface DoneEvent extends EventBase {
  /** 正文：与所有 token 拼接结果逐字相同（前端可用它做一致性校验） */
  answer: string;
  /** 带完整出处的归档形态：`[引#1: 标题 p.7]`，用于复制/导出 */
  answer_annotated: string;
  claims: ClaimEvent[];
  answer_refs: AnswerRef[];
  citations: Citation[];
  anchor_index: Record<string, AnchorDoc>;
  invalid_citations: string[];
  rejected: boolean;
  reject_reason: string | null;
  type: string;
  search_text: string;
  /** 检索分流完整 trace（与 /qa/ask 同构），用于"为什么召回了这几段" */
  routing?: Record<string, unknown>;
  timings: Record<string, number>;
  claim_stats: {
    claims: number;
    verified: number;
    needs_check: number;
    unsupported: number;
    supported: number;
  };
  /** 整条回答的三档置信 + 当时的阈值版本/指纹 */
  confidence?: Confidence | null;
  /** 12：三类不确定性状态的权威版（与 uncertainty 事件同构，多了冲突） */
  uncertainty?: Uncertainty | null;
  provenance_key: string | null;
  /** 事件日志在对象存储里的 key：`qa/stream/<日期>/<trace_id>.jsonl` */
  trace_key: string | null;
}

export interface ErrorEvent {
  trace_id: string | null;
  seq: number;
  message: string;
}

export type StreamEventName =
  | 'stage' | 'retrieval' | 'uncertainty' | 'token' | 'claim' | 'done' | 'error';

/** 原始事件（调试面板与回放用；不做联合类型收窄，保持"原样记录"） */
export interface RawEvent {
  event: StreamEventName | string;
  data: Record<string, unknown>;
}

/** 一次流式对话的可渲染状态 */
export interface StreamState {
  status: 'idle' | 'streaming' | 'done' | 'error';
  traceId: string | null;
  stages: StageEvent[];
  retrieval: RetrievalEvent | null;
  /** 正文（token 累积）。**不含角标** */
  text: string;
  claims: ClaimEvent[];
  /** 检索后立刻到的那半份状态（unreadable / empty）；done 到达后被权威版覆盖 */
  uncertainty: Uncertainty | null;
  done: DoneEvent | null;
  error: string | null;
  /** 收到的心跳数：反代静默超时会被这里暴露出来 */
  heartbeats: number;
  events: RawEvent[];
}

/** 进度条要显示的四段，顺序固定 */
export const STAGE_ORDER: StageName[] = ['understand', 'retrieve', 'rerank', 'generate'];

export const STAGE_TEXT: Record<StageName, string> = {
  understand: '理解',
  retrieve: '检索',
  rerank: '精排',
  generate: '生成',
};
