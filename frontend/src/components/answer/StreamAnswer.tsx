'use client';

import { useMemo, useState } from 'react';
import RoutingTrace, { RoutingTraceData } from '@/components/RoutingTrace';
import type {
  ClaimEvent, Evidence, RetrievalChunk, StageEvent, StageName, StreamState,
} from '@/types/stream';
import { STAGE_ORDER, STAGE_TEXT } from '@/types/stream';
import {
  ConfidenceConfigLine, ConfidenceDetail, ConfidencePill, LEVEL_STYLE,
  levelOf, missingTitle, shortMissing,
} from './confidence';
import {
  ConflictPanel, EmptySearchCard, UnreadableNotice, UncertaintyRules,
} from './uncertainty';
import { useAnswerStream } from './useAnswerStream';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

/** 打开原文时需要的最小信息。evidence / answer_ref 都能直接转成它。 */
export interface OpenRef {
  arxiv_id: string;
  title: string | null;
  page: number;
  bbox: number[] | null;
  rects: number[][] | null;
  precision: string | null;
  text: string | null;
  ref_id: string | null;
  match_score: number | null;
}

interface Props {
  question: string;
  onOpenRef: (ref: OpenRef) => void;
  /** 空检索卡片上的"换个问法"要能直接提交新问题；不给就只是只读展示 */
  onAsk?: (q: string) => void;
}

/**
 * 句子 + 角标回填。
 *
 * 关键约束只有一条：**角标只来自 claim**。
 * `text` 是 token 拼出来的纯正文，里面没有任何 `[引#N]`（服务端已摘除）；
 * 渲染时按 `claim.span` 把句子切开，在句末插入角标。
 * 于是"claim 未到达 → 没有角标 → 不可能点到死链"是结构上成立的，不靠时序运气。
 *
 * 07 起每句多了一个档位：正文装饰与角标配色都跟着档位走（见 confidence.tsx），
 * 三档在界面上是三种"看得见"的形态，而不是三种颜色的同一个东西：
 * 已验证=无装饰+实心角标、需核对=琥珀虚线+琥珀角标、证据不足=红点线+禁用角标。
 */
function ClaimText({
  text,
  claims,
  streaming,
  onOpenRef,
}: {
  text: string;
  claims: ClaimEvent[];
  streaming: boolean;
  onOpenRef: (ref: OpenRef) => void;
}) {
  const ordered = useMemo(
    () => [...claims].sort((a, b) => a.span[0] - b.span[0]),
    [claims],
  );

  if (!ordered.length) {
    return (
      <>
        {text}
        {streaming && <span className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-[var(--signal)] align-middle" />}
      </>
    );
  }

  const nodes: React.ReactNode[] = [];
  let cursor = 0;

  ordered.forEach((claim, i) => {
    const [s, e] = claim.span;
    // span 越界（例如正文被截断过）时跳过这条，绝不把角标插到字中间
    if (s < cursor || e > text.length) return;
    if (s > cursor) nodes.push(<span key={`t${i}`}>{text.slice(cursor, s)}</span>);

    const level = levelOf(claim);
    const style = LEVEL_STYLE[level];
    const supported = claim.citations.filter((c) => c.status === 'supported').length;

    nodes.push(
      <span
        key={`c${i}`}
        className={style.textClass || undefined}
        title={
          level === 'verified'
            ? `已验证：${supported} 条证据与原文句对应`
            : missingTitle(claim.missing) || claim.reason || style.hint
        }
      >
        {text.slice(s, e)}
      </span>,
    );

    if (claim.citations.length) {
      nodes.push(
        <sup key={`b${i}`} className="ml-0.5 whitespace-nowrap">
          {claim.citations.map((ev, j) => {
            if (ev.status === 'unsupported') {
              // 不可点击：点进去也只会落空，宁可让读者看到"这条没站住"
              return (
                <span
                  key={j}
                  title={`#${ev.index} 未通过校验：${ev.reason || '证据不足'}`}
                  className="mono mx-[1px] inline-flex cursor-not-allowed items-center rounded border border-dashed border-[#d6c7c4] bg-[#f7f4f3] px-[3px] py-[1px] align-super text-[11.5px] leading-none text-[#a8a29e]"
                >
                  {ev.index}
                </span>
              );
            }
            const glyph = level === 'verified' ? '✓' : '?';
            return (
              <button
                key={j}
                type="button"
                onClick={() =>
                  onOpenRef({
                    arxiv_id: ev.arxiv_id || '',
                    title: ev.title,
                    page: ev.page,
                    bbox: ev.bbox,
                    rects: ev.rects,
                    precision: ev.precision,
                    text: ev.text,
                    ref_id: ev.ref_id,
                    match_score: ev.match_score,
                  })
                }
                title={`#${ev.index} ${ev.title || ''} p.${ev.page}${
                  ev.ref_id ? ` · ${ev.ref_id}` : ' · 块级定位'
                }${ev.match_score != null ? ` · 字面 ${ev.match_score}` : ''}${
                  ev.sem_score != null ? ` · 语义 ${ev.sem_score}` : ''
                } · ${style.label}`}
                className={`mx-[1px] inline-flex items-center gap-[2px] rounded border px-[3px] py-[1px] align-super text-[11.5px] font-medium leading-none transition hover:brightness-95 ${
                  level === 'needs_check' ? 'border-dashed' : ''
                }`}
                style={{ color: style.color, background: style.bg, borderColor: style.border }}
              >
                <span className="text-[10px] leading-none opacity-80">{glyph}</span>
                <span className="mono">{ev.index}</span>
              </button>
            );
          })}
        </sup>,
      );
    }

    // 需核对 / 证据不足都要在句末留一个文字标记：
    // 颜色本身不构成"说明文案"，色盲用户与打印稿都读不出来。
    if (level === 'needs_check') {
      nodes.push(
        <sup key={`w${i}`} className="ml-0.5 align-super">
          <span
            className="rounded border border-dashed border-[#e0b872] bg-[#fdf6e8] px-1 py-[1px] text-[11.5px] leading-none text-[#a16207]"
            title={missingTitle(claim.missing) || style.hint}
          >
            需核对{shortMissing(claim.missing) ? ` · ${shortMissing(claim.missing)!.slice(0, 12)}` : ''}
          </span>
        </sup>,
      );
    }
    if (level === 'unsupported') {
      nodes.push(
        <sup key={`u${i}`} className="ml-0.5 align-super">
          <span
            className="rounded border border-[#f0a9a2] bg-[#fef3f2] px-1 py-[1px] text-[11.5px] leading-none text-[#b42318]"
            title={claim.reason || style.hint}
          >
            证据不足
          </span>
        </sup>,
      );
    }
    cursor = e;
  });

  if (cursor < text.length) nodes.push(<span key="tail">{text.slice(cursor)}</span>);
  return <>{nodes}</>;
}

function StageRail({ stages, status }: { stages: StageEvent[]; status: StreamState['status'] }) {
  const state = (name: StageName) => {
    const done = stages.find((s) => s.stage === name && s.phase === 'done');
    const started = stages.find((s) => s.stage === name && s.phase === 'start');
    if (done) return { phase: 'done' as const, ms: done.ms, detail: done.detail };
    if (started && status === 'streaming') return { phase: 'running' as const, ms: null, detail: started.detail };
    if (started) return { phase: 'stalled' as const, ms: null, detail: started.detail };
    return { phase: 'idle' as const, ms: null, detail: undefined };
  };

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      {STAGE_ORDER.map((name, i) => {
        const st = state(name);
        const tone =
          st.phase === 'done'
            ? 'text-[var(--ink-2)] border-[var(--signal)] bg-[var(--signal-soft)]'
            : st.phase === 'running'
              ? 'text-[var(--signal-hover)] border-[var(--signal)] bg-white animate-pulse'
              : 'text-[var(--ink-3)] border-[var(--line)] bg-[var(--surface-2)]';
        return (
          <div key={name} className="flex items-center gap-2">
            <span
              className={`mono rounded border px-2 py-[2px] text-[11.5px] tracking-wide ${tone}`}
            >
              {STAGE_TEXT[name]}
              {st.phase === 'done' && st.ms != null
                ? ` · ${Number(st.ms) < 1 ? '<1' : Math.round(Number(st.ms))}ms`
                : ''}
            </span>
            {i < STAGE_ORDER.length - 1 && <span className="h-px w-3 bg-[var(--line-strong)]" />}
          </div>
        );
      })}
    </div>
  );
}

function RetrievalList({
  chunks,
  onOpenRef,
}: {
  chunks: RetrievalChunk[];
  onOpenRef: (ref: OpenRef) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? chunks : chunks.slice(0, 4);
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="mono text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">RETRIEVAL</span>
        <span className="h-px flex-1 bg-[var(--line)]" />
        <span className="mono text-[11.5px] text-[var(--ink-3)]">{chunks.length} 段</span>
      </div>
      <div className="grid gap-1.5 sm:grid-cols-2">
        {shown.map((c) => (
          <button
            key={c.chunk_id}
            type="button"
            onClick={() =>
              onOpenRef({
                arxiv_id: c.arxiv_id,
                title: c.title,
                page: c.page,
                bbox: null,
                rects: null,
                precision: null,
                text: null,
                ref_id: null,
                match_score: c.score,
              })
            }
            className="group rounded border border-[var(--line)] bg-[var(--surface-2)] px-2.5 py-1.5 text-left transition hover:border-[var(--signal)]"
          >
            <div className="mono mb-0.5 flex items-center gap-1.5 text-[11.5px] text-[var(--ink-3)]">
              <span className="text-[var(--signal)]">#{c.index}</span>
              <span className="truncate">{c.arxiv_id}</span>
              <span>p.{c.page}</span>
              <span className="opacity-70">{c.kind}</span>
              {c.has_anchors && (
                <span title="该片段有句子级锚点" className="text-[var(--signal)]">
                  ·
                </span>
              )}
            </div>
            <div className="line-clamp-2 text-[12.5px] leading-snug text-[var(--ink-2)]">
              {c.preview}
            </div>
          </button>
        ))}
      </div>
      {chunks.length > 4 && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mono text-[11.5px] text-[var(--signal)] hover:underline"
        >
          {expanded ? '收起' : `展开其余 ${chunks.length - 4} 段`}
        </button>
      )}
    </div>
  );
}

function EventLog({ state }: { state: StreamState }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded border border-[var(--line)] bg-[var(--surface-2)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <span className="mono text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
          EVENT STREAM
        </span>
        <span className="mono text-[11.5px] text-[var(--ink-3)]">
          {state.events.length} 条 · trace {state.traceId || '-'}
          {state.heartbeats ? ` · ping ${state.heartbeats}` : ''}
        </span>
        <span className="h-px flex-1 bg-[var(--line)]" />
        <span className="text-[11.5px] text-[var(--ink-3)]">{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div className="max-h-64 overflow-auto border-t border-[var(--line)] px-3 py-2">
          <table className="w-full border-collapse text-left">
            <tbody>
              {state.events.map((ev, i) => (
                <tr key={i} className="align-top">
                  <td className="mono w-10 pr-2 text-[11.5px] text-[var(--ink-3)]">
                    {String((ev.data as { seq?: number }).seq ?? i + 1)}
                  </td>
                  <td className="mono w-20 pr-2 text-[11.5px] text-[var(--signal)]">{ev.event}</td>
                  <td className="mono break-all text-[11.5px] leading-relaxed text-[var(--ink-2)]">
                    {JSON.stringify(ev.data).slice(0, 220)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/**
 * 一条流式回答的完整渲染：阶段 → 命中 → 正文（回填角标）→ 汇总。
 *
 * 每个区块对应一类事件，缺哪类就整块不渲染——而不是渲染一个空壳。
 * 这样"事件没到"和"事件到了但内容为空"在界面上是可区分的。
 */
export default function StreamAnswer({ question, onOpenRef, onAsk }: Props) {
  const { state, restart } = useAnswerStream(question);
  const { retrieval, text, claims, done, status, error } = state;
  // 状态来自服务端：检索后先到一份 partial，done 到达后是权威版（多了冲突）。
  // 前端在这里只做"取最新那一份"，不做任何推断——判定全在服务端。
  const unc = done?.uncertainty ?? state.uncertainty ?? null;
  const streaming = status === 'streaming';
  const [detailOpen, setDetailOpen] = useState(false);

  const confidence = done?.confidence ?? null;
  const stats = done?.claim_stats;
  const counts = confidence?.counts;

  return (
    <div className="space-y-4">
      {/* 窄屏下让 TRACE 换到下一行：字号放大后阶段徽章更宽，
          和 TRACE 挤一行会被压成四行竖排 */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
        <StageRail stages={state.stages} status={status} />
        <span className="mono shrink-0 text-[11.5px] text-[var(--ink-3)]">
          {state.traceId ? `TRACE ${state.traceId.slice(3, 11)}` : ''}
        </span>
      </div>

      {retrieval && <RetrievalList chunks={retrieval.chunks} onOpenRef={onOpenRef} />}

      {/* 分流 trace 仍然照旧可展开：换传输方式不该让"为什么召回这几段"变得不可解释 */}
      {done?.routing && (
        <RoutingTrace trace={done.routing as unknown as RoutingTraceData} />
      )}

      {unc?.empty ? (
        /* 空检索：用结构化卡片替掉"无法作答"那句话——
           范围、关键词命中、下一步按钮都在里面，读者不必猜"是我问错了还是库里没有" */
        <EmptySearchCard empty={unc.empty} onAsk={onAsk} onOpenRef={onOpenRef} />
      ) : done?.rejected ? (
        /* 老服务端（12 之前）没有 uncertainty：保留一个中性兜底，
           文案仍然不写"抱歉/无法回答"，并且明确说清这一块已经退化 */
        <div className="rounded-lg border border-[var(--line-strong)] bg-[var(--surface-2)] p-4">
          <div className="mb-1 text-[16px] font-medium text-[var(--ink)]">这次没有作答</div>
          <div className="text-[13.5px] leading-relaxed text-[var(--ink-2)]">{done.reject_reason}</div>
          <div className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--ink-3)]">
            这条响应里没有结构化的检索范围与关键词命中表（服务端未返回 uncertainty），
            所以无法说明是"库里没有"还是"没检索到"。
          </div>
        </div>
      ) : (
        <div>
          {/* 档位标签放在正文上方：读第一句之前就该知道这段回答能不能直接用 */}
          {confidence && (
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <ConfidencePill
                confidence={confidence}
                expanded={detailOpen}
                onToggle={() => setDetailOpen((v) => !v)}
              />
              <ConfidenceConfigLine confidence={confidence} />
            </div>
          )}
          <p className="text-[17px] leading-[1.85] text-[var(--ink)]">
            <ClaimText text={text} claims={claims} streaming={streaming} onOpenRef={onOpenRef} />
          </p>
          {streaming && (
            <div className="mt-1 flex items-center gap-2 text-[11.5px] text-[var(--ink-3)]">
              <span className="status-dot" />
              <span>
                {claims.length
                  ? `已校验 ${claims.length} 句，正文仍在生成`
                  : '正文生成中 · 角标与置信标记将在校验完成后回填'}
              </span>
            </div>
          )}
          {done && detailOpen && <div className="mt-2"><ConfidenceDetail claims={claims} /></div>}
        </div>
      )}

      {/* 冲突与未识别都挂在"回答之外"：它们是关于证据本身的判断，
          与"这句话可不可信"（三档置信）是两件事，混排会让人以为是同一套标记 */}
      {!unc?.empty && unc?.conflicts?.length ? (
        <ConflictPanel conflicts={unc.conflicts} onOpenRef={onOpenRef} />
      ) : null}
      {!unc?.empty && unc?.unreadable?.length ? (
        <UnreadableNotice items={unc.unreadable} onOpenRef={onOpenRef} />
      ) : null}

      {error && (
        <div className="rounded border border-[var(--danger)] bg-red-50 px-3 py-2 text-[13px] text-[var(--danger)]">
          {error}
          <button type="button" onClick={restart} className="ml-3 underline">
            重试
          </button>
        </div>
      )}

      {done && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-[var(--line)] pt-2.5 text-[12.5px] text-[var(--ink-3)]">
            <span>
              主张 <span className="mono text-[var(--ink-2)]">{stats?.claims ?? 0}</span> 条 ·
              已验证 <span className="mono text-[#0f766e]">{counts?.verified ?? 0}</span> ·
              需核对 <span className="mono text-[#a16207]">{counts?.needs_check ?? 0}</span> ·
              证据不足 <span className="mono text-[#b42318]">{counts?.unsupported ?? 0}</span>
            </span>
            <span className="mono">
              首字 {Math.round(done.timings.ttft_ms || 0)}ms · 总{' '}
              {Math.round(done.timings.total_ms || 0)}ms
            </span>
            {done.invalid_citations.length > 0 && (
              <span className="text-[var(--danger)]">
                无效引用：{done.invalid_citations.join(', ')}
              </span>
            )}
            {confidence?.signals?.block_located ? (
              <span className="text-[var(--ink-3)]">
                {String(confidence.signals.block_located)} 处只能块级定位（该片段未建句级锚点）
              </span>
            ) : null}
          </div>

          {done.trace_key && (
            <div className="mono flex flex-wrap items-center gap-2 text-[11.5px] text-[var(--ink-3)]">
              <span>事件日志</span>
              <a
                href={`${API_BASE}/api/v1/qa/stream/${done.trace_id}`}
                target="_blank"
                rel="noreferrer"
                className="text-[var(--signal)] hover:underline"
              >
                {done.trace_key}
              </a>
              <button
                type="button"
                onClick={() => navigator.clipboard?.writeText(done.answer_annotated)}
                className="rounded border border-[var(--line-strong)] px-1.5 py-[1px] hover:border-[var(--signal)]"
              >
                复制带出处全文
              </button>
            </div>
          )}
        </div>
      )}

      {unc && unc.rules?.length ? <UncertaintyRules uncertainty={unc} /> : null}

      {state.events.length > 0 && <EventLog state={state} />}
    </div>
  );
}

export type { Evidence };
