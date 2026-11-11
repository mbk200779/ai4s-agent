'use client';

import { useState } from 'react';

/**
 * 检索计划（routing trace）展示。
 *
 * 为什么要把这东西摆到界面上：分流之后，"为什么召回的是这几段"不再由代码决定，
 * 而由分类结果决定。用户看不到分类，就看不到答案为什么长这样，也没法反馈
 * "你把我当点查了，其实我在问方法差异"。所以类型、策略、规则命中、每一跳、
 * 召回统计全部摊开，默认收起、点开看细节，不打扰只想看答案的人。
 */

export interface RoutingTraceData {
  query: string;
  search_text?: string;
  type: string;
  type_label: string;
  confidence: number;
  classifier: {
    method: string;
    latency_ms: number;
    budget_ms?: number;
    degraded?: boolean;
    degrade_reason?: string | null;
    fallback_reason?: string | null;
    rule_hits?: { rule: string; type?: string; weight?: number; note?: string; evidence?: string }[];
    prototype?: {
      used?: boolean;
      skipped?: string;
      error?: string;
      top?: { type: string; sim: number }[];
      margin?: number;
    };
  };
  slots?: Record<string, unknown>;
  strategy: {
    name: string;
    label: string;
    params?: Record<string, unknown>;
    latency_ms: number;
    hops?: Record<string, unknown>[];
    notes?: string[];
    direct?: boolean;
    reject_threshold?: number | null;
  };
  // 规则召回：问句里抽到了哪些数值/公式探针、扫出多少命中、注入了几条
  numeric?: {
    used: boolean;
    probes?: { label: string; rule: string; weight: number }[];
    scanned?: number;
    matched?: number;
    table_cells?: number;
    injected?: number;
    latency_ms?: number;
    // 规则召回限定在"融合候选里出现过的论文"内：scope 是允许的论文，out_of_scope 是被跳过的片段数
    paper_scope?: string[];
    out_of_scope?: number;
  };
  // 精排：候选 → 输出的裁剪、缓存命中、以及"谁被提上来了"
  rerank?: {
    enabled: boolean;
    source?: string;
    applied: boolean;
    degraded: boolean;
    degrade_reason?: string | null;
    model: string;
    candidates: number;
    output: number;
    returned: number;
    cache: string;
    index_snapshot_id?: string | null;
    latency_ms: number;
    moved?: { chunk_id: string; arxiv_id: string; page: number; from: number; to: number }[];
    scores?: {
      rank: number;
      arxiv_id: string;
      page: number;
      kind: string;
      fusion_rank?: number | null;
      rerank_score: number;
    }[];
  };
  recall?: {
    returned: number;
    candidates?: number;
    evidence?: string;
    papers: number;
    sections: number;
    kinds: Record<string, number>;
    top_score: number;
    top_raw_score?: number | null;
    paper_list?: { arxiv_id: string; title: string }[];
  };
  top_k?: number;
  rrf_k?: number;
  total_ms?: number;
  fallback?: { used: boolean; reason?: string | null; label?: string };
}

// 类型配色：点查取"精确"的冷青，对比取"跨文"的紫，综述取"广度"的蓝，
// 定位取"字面"的橙，闲聊与兜底都用灰——它们本质上都是"不特别处理"。
const TYPE_STYLE: Record<string, { bg: string; fg: string }> = {
  point: { bg: 'var(--signal-soft)', fg: 'var(--signal-hover)' },
  compare: { bg: '#efe9fb', fg: '#6d43c8' },
  survey: { bg: '#e4eefb', fg: '#2563a8' },
  nav: { bg: '#fbeee0', fg: '#a8620f' },
  chitchat: { bg: 'var(--surface-3)', fg: 'var(--ink-2)' },
  generic: { bg: 'var(--surface-3)', fg: 'var(--ink-2)' },
};

const METHOD_LABEL: Record<string, string> = {
  rules: '规则命中',
  'rules+prototype': '规则 + 原型',
  fallback: '兜底',
  forced: '强制指定',
};

function HopFacts({ hop }: { hop: Record<string, unknown> }) {
  // hops 的字段随策略而变（dense/sparse、scanned/matched、per_paper、pairs…），
  // 这里只挑"人看得懂的量"渲染，原始 JSON 不放上来。
  const facts: string[] = [];
  const num = (k: string) => (typeof hop[k] === 'number' ? (hop[k] as number) : undefined);

  const dense = num('dense');
  const sparse = num('sparse');
  if (dense !== undefined || sparse !== undefined) {
    facts.push(`dense ${dense ?? 0} · sparse ${sparse ?? 0}`);
  }
  const scanned = num('scanned');
  if (scanned !== undefined) facts.push(`扫描 ${scanned} 段`);
  const matched = num('matched');
  if (matched !== undefined) facts.push(`字面命中 ${matched}`);
  const anchors = hop.anchors;
  if (Array.isArray(anchors) && anchors.length) {
    facts.push(`锚点 ${anchors.slice(0, 3).map(String).join(' / ')}`);
  }
  const picked = hop.picked;
  if (Array.isArray(picked) && picked.length) facts.push(`选定 ${picked.length} 篇`);
  const perPaper = hop.per_paper;
  if (perPaper && typeof perPaper === 'object') {
    const entries = Object.entries(perPaper as Record<string, number>);
    facts.push(entries.map(([k, v]) => `${k}:${v}段`).join(' · '));
  }
  const each = num('each');
  if (each !== undefined) facts.push(`每篇 ${each} 段`);
  const pairs = num('pairs');
  if (pairs !== undefined) facts.push(`配对 ${pairs}`);
  const boosted = num('boosted');
  if (boosted !== undefined) facts.push(`加成 ${boosted} 段`);
  const added = num('added');
  if (added !== undefined) facts.push(`补入 ${added} 条`);
  const inn = num('in');
  const out = num('out');
  if (inn !== undefined && out !== undefined) facts.push(`${inn} → ${out} 条`);

  return (
    <>
      {facts.length > 0 && (
        <span className="mono text-[12.5px] text-[var(--ink-3)]">{facts.join(' · ')}</span>
      )}
      {Array.isArray(hop.pair_detail) && hop.pair_detail.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {hop.pair_detail.slice(0, 4).map((p, i) => {
            const members = ((p as Record<string, unknown>).members || []) as {
              arxiv_id: string;
              section?: string;
            }[];
            if (members.length < 2) return null;
            return (
              <span
                key={i}
                className="mono rounded bg-[var(--surface)] px-1.5 py-0.5 text-[11.5px] text-[var(--ink-2)]"
              >
                {members.map((m) => `${m.arxiv_id}·${(m.section || '').slice(0, 12)}`).join(' ↔ ')}
              </span>
            );
          })}
        </div>
      )}
    </>
  );
}

export default function RoutingTrace({ trace }: { trace?: RoutingTraceData | null }) {
  const [open, setOpen] = useState(false);
  if (!trace) return null;

  const tone = TYPE_STYLE[trace.type] || TYPE_STYLE.generic;
  const cls = trace.classifier || ({} as RoutingTraceData['classifier']);
  const strat = trace.strategy || ({} as RoutingTraceData['strategy']);
  const recall = trace.recall;

  const slots = Object.entries(trace.slots || {}).filter(([, v]) => {
    if (Array.isArray(v)) return v.length > 0;
    return Boolean(v);
  });

  const methodKey = (cls.method || '').startsWith('forced') ? 'forced' : cls.method;
  const methodText = METHOD_LABEL[methodKey] || cls.method;

  return (
    <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)]">
      {/* 常显行：类型 + 策略 + 关键数字，不点开也能看懂走了哪条路 */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 text-left"
        aria-expanded={open}
      >
        <span className="mono shrink-0 text-[11.5px] tracking-[0.18em] text-[var(--ink-3)]">
          检索计划
        </span>
        <span
          className="mono shrink-0 rounded px-1.5 py-0.5 text-[11.5px] font-medium"
          style={{ background: tone.bg, color: tone.fg }}
          title={`置信度 ${trace.confidence.toFixed(2)}`}
        >
          {trace.type.toUpperCase()} · {trace.type_label}
        </span>
        <span className="mono shrink-0 text-[12.5px] text-[var(--ink-2)]">{strat.name}</span>
        {trace.rerank?.enabled && (
          <span
            className="mono shrink-0 rounded px-1.5 py-0.5 text-[11.5px]"
            style={
              trace.rerank.degraded
                ? { background: 'var(--surface-3)', color: 'var(--warn)' }
                : { background: 'var(--surface-3)', color: 'var(--ink-2)' }
            }
            title={trace.rerank.degraded ? trace.rerank.degrade_reason || '精排降级' : 'cross-encoder 精排'}
          >
            {trace.rerank.applied
              ? `精排 ${trace.rerank.candidates}→${trace.rerank.output}`
              : '精排降级'}
          </span>
        )}
        {trace.fallback?.used && (
          <span className="mono shrink-0 rounded bg-[var(--surface-3)] px-1.5 py-0.5 text-[11.5px] text-[var(--ink-3)]">
            沿分流前行为
          </span>
        )}
        {cls.degraded && (
          <span
            className="mono shrink-0 rounded px-1.5 py-0.5 text-[11.5px] text-[var(--warn)]"
            title={cls.degrade_reason || '分类退化'}
          >
            降级
          </span>
        )}
        <span className="mono ml-auto shrink-0 text-[11.5px] text-[var(--ink-3)]">
          {methodText} · {cls.latency_ms?.toFixed?.(1) ?? 0}ms
          {recall ? ` · 召回 ${recall.returned} 段 / ${recall.papers} 篇` : ''}
        </span>
        <svg
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          className={`h-3.5 w-3.5 shrink-0 text-[var(--ink-3)] transition-transform ${open ? 'rotate-180' : ''}`}
          aria-hidden="true"
        >
          <path d="M5 7.5l5 5 5-5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div className="space-y-3 border-t border-[var(--line)] px-3 py-3">
          {/* 策略说明：一句话告诉用户这条路在干什么 */}
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="mono shrink-0 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
              策略
            </span>
            <span className="text-[13px] text-[var(--ink-2)]">{strat.label}</span>
            <span className="mono text-[11.5px] text-[var(--ink-3)]">
              {strat.latency_ms}ms · 全程 {trace.total_ms}ms
            </span>
          </div>

          {/* 分类依据：规则命中了哪几条，每条一句话说明为什么 */}
          {(cls.rule_hits?.length ?? 0) > 0 && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="mono shrink-0 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                规则
              </span>
              {cls.rule_hits!.map((h) => (
                <span
                  key={h.rule}
                  title={[h.note, h.evidence ? `命中「${h.evidence}」` : ''].filter(Boolean).join(' · ')}
                  className="mono rounded border border-[var(--line)] bg-[var(--surface)] px-1.5 py-0.5 text-[11.5px] text-[var(--ink-2)]"
                >
                  {h.rule}
                </span>
              ))}
            </div>
          )}

          {/* 小模型证据：规则不够强时由它补，把前三名相似度摆出来 */}
          {cls.prototype?.used && (cls.prototype.top?.length ?? 0) > 0 && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="mono shrink-0 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                原型
              </span>
              <span className="mono text-[11.5px] text-[var(--ink-2)]">
                {cls.prototype.top!.map((t) => `${t.type} ${t.sim}`).join(' · ')}
                {cls.prototype.margin !== undefined ? ` · 间距 ${cls.prototype.margin}` : ''}
              </span>
            </div>
          )}

          {/* 槽位：抽出来的实体/参数名/引号片段，是策略的输入 */}
          {slots.length > 0 && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="mono shrink-0 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                槽位
              </span>
              {slots.map(([k, v]) => (
                <span key={k} className="mono text-[11.5px] text-[var(--ink-2)]">
                  {k}=
                  <span className="text-[var(--ink)]">
                    {Array.isArray(v) ? v.join('/') : String(v)}
                  </span>
                </span>
              ))}
            </div>
          )}

          {/* 每一跳：多跳策略的关键——从哪跳到哪、每跳产出多少 */}
          {(strat.hops?.length ?? 0) > 0 && (
            <div className="space-y-1.5">
              <span className="mono text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                每一跳
              </span>
              <ol className="space-y-1.5">
                {strat.hops!.map((hop, i) => (
                  <li key={i} className="flex flex-wrap items-baseline gap-x-2">
                    <span className="mono shrink-0 rounded bg-[var(--surface-3)] px-1.5 py-0.5 text-[11.5px] text-[var(--ink-2)]">
                      {String(hop.hop ?? i + 1)}
                    </span>
                    <span className="text-[13px] text-[var(--ink)]">{String(hop.action ?? '')}</span>
                    <HopFacts hop={hop} />
                  </li>
                ))}
              </ol>
            </div>
          )}

          {/* 召回统计：段/篇/节分布 + 拒答判断用的绝对分 */}
          {recall && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="mono shrink-0 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                召回
              </span>
              <span className="mono text-[11.5px] text-[var(--ink-2)]">
                {recall.returned} 段 / {recall.papers} 篇 / {recall.sections} 节
                {Object.keys(recall.kinds || {}).length > 0 &&
                  ` · ${Object.entries(recall.kinds)
                    .map(([k, v]) => `${k} ${v}`)
                    .join(', ')}`}
                {` · top ${recall.top_score}`}
                {recall.top_raw_score != null && ` (raw ${recall.top_raw_score})`}
                {strat.reject_threshold != null && ` · 拒答阈值 ${strat.reject_threshold}`}
              </span>
              {(recall.paper_list?.length ?? 0) > 0 && (
                <span className="mono text-[11.5px] text-[var(--ink-3)]">
                  {recall.paper_list!.map((p) => p.arxiv_id).join(' / ')}
                </span>
              )}
            </div>
          )}

          {/* 规则召回：数值/公式探针。没有这一块，用户无法理解"为什么一条表格碎片会排前面" */}
          {trace.numeric?.used && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="mono shrink-0 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                数值规则
              </span>
              {(trace.numeric.probes || []).map((p) => (
                <span
                  key={p.rule + p.label}
                  title={`${p.rule} · 权重 ${p.weight}`}
                  className="mono rounded border border-[var(--line)] bg-[var(--surface)] px-1.5 py-0.5 text-[11.5px] text-[var(--ink-2)]"
                >
                  {p.label}
                </span>
              ))}
              <span className="mono text-[11.5px] text-[var(--ink-3)]">
                扫描 {trace.numeric.scanned ?? 0} 段 · 命中 {trace.numeric.matched ?? 0} · 表格单元格{' '}
                {trace.numeric.table_cells ?? 0} · 注入 {trace.numeric.injected ?? 0}
                {(trace.numeric.paper_scope?.length ?? 0) > 0 && (
                  <>
                    {' '}· 限定 {(trace.numeric.paper_scope ?? []).length} 篇论文（跳过{' '}
                    {trace.numeric.out_of_scope ?? 0} 段）
                  </>
                )}
              </span>
            </div>
          )}

          {/* 精排：候选怎么被裁到前 N，缓存有没有命中，哪些片段被提上来/压下去 */}
          {trace.rerank?.enabled && (
            <div className="space-y-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="mono shrink-0 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                  精排
                </span>
                <span className="mono text-[11.5px] text-[var(--ink-2)]">
                  {trace.rerank.candidates} 候选 → 前 {trace.rerank.output}
                  {trace.rerank.returned > trace.rerank.output
                    ? `（补位 ${trace.rerank.returned - trace.rerank.output}）`
                    : ''}
                  {` · ${trace.rerank.latency_ms}ms`}
                  {` · 缓存 ${trace.rerank.cache}`}
                </span>
                <span className="mono text-[11.5px] text-[var(--ink-3)]">
                  {trace.rerank.model}
                </span>
              </div>
              {(trace.rerank.moved?.length ?? 0) > 0 && (
                <div className="flex flex-wrap gap-1">
                  {trace.rerank.moved!.slice(0, 6).map((m) => (
                    <span
                      key={m.chunk_id}
                      className="mono rounded bg-[var(--surface)] px-1.5 py-0.5 text-[11.5px] text-[var(--ink-2)]"
                    >
                      {m.arxiv_id.slice(0, 14)} p{m.page} {m.from}→{m.to}
                    </span>
                  ))}
                </div>
              )}
              {trace.rerank.degrade_reason && (
                <p className="text-[12.5px] leading-relaxed text-[var(--warn)]">
                  {trace.rerank.degraded ? '降级原因：' : ''}
                  {trace.rerank.degrade_reason}
                </p>
              )}
            </div>
          )}

          {/* 降级/兜底原因：不藏起来，否则"为什么这条像没分流"没人答得上 */}
          {(cls.degrade_reason || cls.fallback_reason || (strat.notes?.length ?? 0) > 0) && (
            <div className="space-y-0.5 border-t border-[var(--line)] pt-2">
              {cls.degrade_reason && (
                <p className="text-[12.5px] leading-relaxed text-[var(--warn)]">
                  降级原因：{cls.degrade_reason}
                </p>
              )}
              {cls.fallback_reason && (
                <p className="text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                  兜底原因：{cls.fallback_reason}
                </p>
              )}
              {strat.notes?.map((n, i) => (
                <p key={i} className="text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                  {n}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
