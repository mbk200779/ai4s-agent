'use client';

/**
 * 库外推荐：库内判空之后的「去库外找」。
 *
 * 为什么长在空检索卡片里，而不是单开一个页面：
 * 用户读到"库里没有能支撑这个问题的内容"的这一刻，唯一想知道的是"那我现在能做什么"。
 * 把这条出路放到另一个页面、让他自己导航过去，等于把一句解释又还给他。
 *
 * 三个状态都必须有明确出口（与 12 号三类不确定性的规则一致）：
 * - 检索失败 → 「重新检索」：网络抖一下不该让整块消失；
 * - 检索为空 → 说清试过哪些检索式，给「重新检索」；
 * - 导入失败 → 原样显示后端 detail + 「重试」，不吞错误。
 *
 * 导入复用 `/papers/arxiv/import`（与论文库的 arXiv 导入同一条链路）：
 * 它已经带 file_hash 复用、版本去重与任务派发，另写一条导入路径必然出现
 * "两个入口判定不一致"。这里只负责把它的四种结果翻成人话：
 * 入队（带进度）/ 命中复用 / 已在库 / 失败。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

/** 相关性口径由服务端算好，这里只负责展示——判定不写第二遍。 */
export interface RecommendRelevance {
  score: number;
  coverage: number;
  level: string;
  terms: string[];
  title_hits: string[];
  abstract_hits: string[];
  hits: string[];
  /** 摘要里命中最多的那一句，直接当"为什么推荐它"的证据 */
  evidence: string;
  text: string;
}

export interface RecommendCandidate {
  arxiv_id: string;
  version: string;
  title: string;
  abstract: string;
  authors: string[];
  author_count: number;
  published: string | null;
  year: number | null;
  primary_category: string | null;
  abs_url: string;
  pdf_url: string;
  in_library: boolean;
  relevance: RecommendRelevance;
}

export interface RecommendResponse {
  status: 'ok' | 'no_result' | 'no_query' | 'error';
  message: string;
  question: string;
  search_text: string;
  terms: string[];
  queries: string[];
  source: { name: string; url: string };
  elapsed_ms: number;
  cached: boolean;
  skipped_in_library: number;
  candidates: RecommendCandidate[];
}

type ImportPhase = 'importing' | 'queued' | 'done' | 'reused' | 'failed';

interface ImportState {
  phase: ImportPhase;
  /** 解析进度 0~100，仅 queued 时有意义 */
  pct?: number;
  /** 阶段人话：排队中 / 解析中 / 建索引中 */
  label?: string;
  /** 失败原因，或复用/完成时的补充说明 */
  msg?: string;
  /** 完成时入库了多少片段 */
  chunks?: number;
}

// 会话内缓存：同一次会话里上下滚动、CSS 重排都会重新渲染，
// 但不该因此再打一次 arXiv（服务端也有 10 分钟缓存，两层各有各的用处：
// 这一层省的是网络往返，服务端那层省的是外部请求）。
const memo = new Map<string, RecommendResponse>();

const LEVEL_TONE: Record<string, { fg: string; bg: string; bd: string }> = {
  强: { fg: '#0f766e', bg: '#e6f4f1', bd: '#9ed4cb' },
  中: { fg: '#a16207', bg: '#fdf6e8', bd: '#e0b872' },
  弱: { fg: '#475569', bg: '#f5f7f9', bd: '#cbd5e1' },
};

function tone(level: string) {
  return LEVEL_TONE[level] || LEVEL_TONE['弱'];
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function authorLine(c: RecommendCandidate) {
  if (!c.authors.length) return '作者未记录';
  const head = c.authors.slice(0, 3).join('、');
  return c.author_count > 3 ? `${head} 等 ${c.author_count} 人` : head;
}

export default function ExternalRecommend({
  question,
  searchText,
  onAsk,
}: {
  question: string;
  /** 问答链路已经翻好的英文检索文本；有就直接用，不再让服务端翻一次 */
  searchText?: string;
  /** 导入完成后「用它重问」要能直接提交同一个问题 */
  onAsk?: (q: string) => void;
}) {
  const [data, setData] = useState<RecommendResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [imports, setImports] = useState<Record<string, ImportState>>({});
  const [openAbs, setOpenAbs] = useState<Record<string, boolean>>({});
  const aliveRef = useRef(true);

  const key = `${question}::${searchText || ''}`;

  const run = useCallback(
    async (force: boolean) => {
      const hit = !force ? memo.get(key) : undefined;
      if (hit) {
        setData(hit);
        setLoading(false);
        setError('');
        return;
      }
      setLoading(true);
      setError('');
      if (force) setData(null);
      try {
        const res = await fetch(`${API_BASE}/api/v1/recommend/papers`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            question,
            search_text: searchText || null,
            limit: 5,
            force,
          }),
        });
        const body = (await res.json().catch(() => ({}))) as RecommendResponse & {
          detail?: string;
        };
        if (!aliveRef.current) return;
        if (!res.ok) {
          setError(body?.detail || `检索失败（HTTP ${res.status}）`);
          return;
        }
        memo.set(key, body);
        setData(body);
      } catch {
        if (aliveRef.current) setError('网络异常，没能连上检索服务');
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    },
    [key, question, searchText],
  );

  useEffect(() => {
    aliveRef.current = true;
    void run(false);
    return () => {
      aliveRef.current = false;
    };
  }, [run]);

  const patch = useCallback((id: string, next: ImportState) => {
    setImports((m) => ({ ...m, [id]: next }));
  }, []);

  /**
   * 解析进度：这里不复用 `IngestProgressPanel`。
   * 那是个整块面板（阶段刻度、片段/文献/引用三行），塞进一张列表卡片会把
   * 推荐列表变成三个进度条的长页面；这条路径上用户只需要一行"现在到哪了"。
   */
  const watch = useCallback(
    async (id: string, taskId?: string | null) => {
      if (!taskId) return;
      // 10 分钟上限：541 页的教材实测 8 分 46 秒，留一倍余量后必须是有限循环，
      // 否则用户切走页面后这个定时器会一直挂着。
      for (let i = 0; i < 500; i += 1) {
        await sleep(1200);
        if (!aliveRef.current) return;
        try {
          const res = await fetch(`${API_BASE}/api/v1/ingest/tasks/${taskId}/progress`, {
            cache: 'no-store',
          });
          if (!res.ok) continue;
          const p = await res.json();
          if (p.done) {
            if (p.status === 'failed' || p.stage === 'failed') {
              patch(id, { phase: 'failed', msg: p.error || '解析失败（未返回原因）' });
            } else {
              patch(id, { phase: 'done', chunks: p.chunks as number });
            }
            return;
          }
          patch(id, {
            phase: 'queued',
            pct: typeof p.pct === 'number' ? Math.round(p.pct) : 0,
            label: p.stage_label || p.stage || '解析中',
          });
        } catch {
          /* 网络抖动不打断：下一轮接着问 */
        }
      }
      patch(id, { phase: 'failed', msg: '10 分钟内没等到解析结果，可到论文库查看该篇状态' });
    },
    [patch],
  );

  const importPaper = useCallback(
    async (c: RecommendCandidate) => {
      patch(c.arxiv_id, { phase: 'importing' });
      try {
        const res = await fetch(
          `${API_BASE}/api/v1/papers/arxiv/import?arxiv_id=${encodeURIComponent(c.arxiv_id)}`,
          { method: 'POST' },
        );
        const body = await res.json().catch(() => ({}));
        if (!aliveRef.current) return;
        if (!res.ok) {
          patch(c.arxiv_id, {
            phase: 'failed',
            msg: typeof body?.detail === 'string' ? body.detail : `HTTP ${res.status}`,
          });
          return;
        }
        if (body.reused) {
          // 后端命中 file_hash：这条论文之前已经解析过，直接复用结果。
          // 这不是错误，用户要的是"它在库里"，现在已经在了。
          patch(c.arxiv_id, { phase: 'reused', msg: body.detail || '命中已解析文件，已复用结果' });
          return;
        }
        patch(c.arxiv_id, { phase: 'queued', pct: 0, label: '排队中' });
        void watch(c.arxiv_id, body.task_id);
      } catch {
        if (aliveRef.current) patch(c.arxiv_id, { phase: 'failed', msg: '网络异常，导入请求没发出去' });
      }
    },
    [patch, watch],
  );

  const candidates = data?.candidates || [];
  const showHead = (
    <div className="mb-2 flex flex-wrap items-center gap-2">
      <span
        className="mono rounded border px-1.5 py-[1px] text-[11.5px] tracking-[0.12em]"
        style={{ color: 'var(--signal)', background: 'var(--signal-soft)', borderColor: '#9ed4cb' }}
      >
        库外推荐 · arXiv
      </span>
      <span className="h-px flex-1 bg-[var(--line)]" />
      {data && !loading && (
        <span className="mono text-[11.5px] text-[var(--ink-3)]">
          {candidates.length} 篇 · {(data.elapsed_ms / 1000).toFixed(1)}s
          {data.cached ? ' · 缓存' : ''}
        </span>
      )}
      <button
        type="button"
        onClick={() => void run(true)}
        disabled={loading}
        className="mono rounded border border-[var(--line-strong)] px-1.5 py-[1px] text-[11.5px] text-[var(--ink-2)] transition hover:border-[var(--signal)] hover:text-[var(--signal)] disabled:opacity-50"
      >
        {loading ? '检索中…' : '重新检索'}
      </button>
    </div>
  );

  return (
    <div className="mb-3">
      {showHead}

      <p className="mb-2 text-[12.5px] leading-snug text-[var(--ink-3)]">
        库内没有，就去库外找：按问句的关键词在 arXiv 检索，选中的论文一键入库，
        解析完成后可以直接用原问题重问。
      </p>

      {loading && (
        <div className="space-y-1.5">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="animate-pulse rounded border border-dashed border-[var(--line)] bg-[var(--surface-2)] px-2.5 py-2"
            >
              <div className="mb-1.5 h-2.5 w-2/3 rounded bg-[var(--line)]" />
              <div className="h-2 w-full rounded bg-[var(--line)]" />
              <div className="mt-1 h-2 w-4/5 rounded bg-[var(--line)]" />
            </div>
          ))}
          <p className="mono text-[11.5px] text-[var(--ink-3)]">
            正在 arXiv 检索「{(searchText || question).slice(0, 60)}」…
          </p>
        </div>
      )}

      {!loading && error && (
        <div className="rounded border border-[var(--danger)] bg-[#fef3f2] px-2.5 py-2">
          <p className="text-[13px] leading-snug text-[var(--danger)]">{error}</p>
          <button
            type="button"
            onClick={() => void run(true)}
            className="mono mt-1 text-[11.5px] text-[var(--danger)] underline underline-offset-2"
          >
            重新检索
          </button>
        </div>
      )}

      {!loading && !error && data && data.status !== 'ok' && (
        <div className="rounded border border-dashed border-[var(--line-strong)] bg-[var(--surface-2)] px-2.5 py-2">
          <p className="text-[13px] leading-relaxed text-[var(--ink-2)]">{data.message}</p>
          {data.queries.length > 0 && (
            <p className="mono mt-1 break-all text-[11px] leading-relaxed text-[var(--ink-3)]">
              试过：{data.queries.join(' → ')}
            </p>
          )}
        </div>
      )}

      {!loading && !error && candidates.length > 0 && (
        <div className="space-y-2">
          {candidates.map((c) => {
            const st = imports[c.arxiv_id];
            const busy = st?.phase === 'importing' || st?.phase === 'queued';
            const done = st?.phase === 'done' || st?.phase === 'reused';
            const t = tone(c.relevance.level);
            return (
              <div
                key={c.arxiv_id}
                className="rounded border border-[var(--line)] bg-[var(--surface)] p-2.5"
              >
                <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="mono text-[11px] text-[var(--ink-3)]">
                    arXiv:{c.arxiv_id}
                    {c.year ? ` · ${c.year}` : ''}
                    {c.primary_category ? ` · ${c.primary_category}` : ''}
                  </span>
                  <span className="h-px flex-1 bg-[var(--line)]" />
                  <span
                    className="mono shrink-0 rounded border px-1.5 py-[1px] text-[11px]"
                    style={{ color: t.fg, background: t.bg, borderColor: t.bd }}
                    title={`词面覆盖度 ${Math.round(c.relevance.coverage * 100)}%（${c.relevance.text}）`}
                  >
                    相关性 {c.relevance.level} · {c.relevance.score.toFixed(2)}
                  </span>
                </div>

                <a
                  href={c.abs_url}
                  target="_blank"
                  rel="noreferrer"
                  className="block text-[14.5px] font-semibold leading-snug text-[var(--ink)] hover:text-[var(--signal)]"
                >
                  {c.title}
                </a>
                <p className="mono mt-0.5 text-[11px] text-[var(--ink-3)]">{authorLine(c)}</p>

                <p
                  className={`mt-1.5 text-[13px] leading-relaxed text-[var(--ink-2)] ${
                    openAbs[c.arxiv_id] ? '' : 'line-clamp-2'
                  }`}
                >
                  {c.abstract}
                </p>
                {c.abstract.length > 220 && (
                  <button
                    type="button"
                    onClick={() =>
                      setOpenAbs((m) => ({ ...m, [c.arxiv_id]: !m[c.arxiv_id] }))
                    }
                    className="mono mt-0.5 text-[11px] text-[var(--ink-3)] hover:text-[var(--signal)]"
                  >
                    {openAbs[c.arxiv_id] ? '收起摘要' : '展开摘要'}
                  </button>
                )}

                {/* 相关性说明：只陈述词面证据，不做语义判断——所以这里写"命中"而不是"相关" */}
                <p className="mt-1.5 border-l-2 border-[var(--line-strong)] pl-2 text-[12px] leading-relaxed text-[var(--ink-2)]">
                  <span className="mono mr-1 text-[11px] tracking-[0.1em] text-[var(--ink-3)]">
                    为什么推荐
                  </span>
                  {c.relevance.text}
                </p>
                {c.relevance.evidence && (
                  <p className="mono mt-1 line-clamp-2 rounded bg-[var(--surface-2)] px-2 py-1 text-[11.5px] leading-relaxed text-[var(--ink-2)]">
                    {c.relevance.evidence}
                  </p>
                )}

                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void importPaper(c)}
                    disabled={busy || done}
                    className="btn-primary"
                    style={{ padding: '5px 12px', fontSize: 12.5 }}
                  >
                    {busy
                      ? `${st?.label || '导入中'}${st?.phase === 'queued' && st?.pct != null ? ` ${st.pct}%` : '…'}`
                      : done
                        ? '✓ 已入库'
                        : st?.phase === 'failed'
                          ? '重试导入'
                          : '一键导入数据库'}
                  </button>
                  <a
                    href={c.abs_url}
                    target="_blank"
                    rel="noreferrer"
                    className="mono rounded border border-[var(--line-strong)] px-1.5 py-[2px] text-[11.5px] text-[var(--ink-2)] transition hover:border-[var(--signal)] hover:text-[var(--signal)]"
                  >
                    看原文 ↗
                  </a>

                  {st?.phase === 'failed' && (
                    <span className="text-[12px] leading-snug text-[var(--danger)]">
                      导入失败：{st.msg}
                    </span>
                  )}
                  {st?.phase === 'reused' && (
                    <span className="text-[12px] leading-snug text-[var(--ink-2)]">
                      跳过重复解析（{st.msg}）
                    </span>
                  )}
                  {st?.phase === 'done' && (
                    <span className="text-[12px] leading-snug text-[var(--signal)]">
                      {st.chunks ?? 0} 个片段已入库
                      {onAsk ? ' · 可以用原问题重问了' : ''}
                    </span>
                  )}
                  {done && onAsk && (
                    <button
                      type="button"
                      onClick={() => onAsk(question)}
                      className="mono rounded border border-[var(--signal)] px-1.5 py-[2px] text-[11.5px] text-[var(--signal)] transition hover:bg-[var(--signal-soft)]"
                    >
                      用它重问
                    </button>
                  )}
                </div>

                {st?.phase === 'queued' && (
                  <div className="mt-1.5 h-1 w-full overflow-hidden bg-[var(--line)]">
                    <div
                      className="h-full transition-[width] duration-500"
                      style={{ width: `${st.pct ?? 0}%`, background: 'var(--signal)' }}
                    />
                  </div>
                )}
              </div>
            );
          })}

          <p className="mono text-[11px] leading-relaxed text-[var(--ink-3)]">
            相关性只按标题 / 摘要的词面命中计算，不含语义判断——点开摘要自行确认。
            导入会在后台解析（一篇通常几十秒到几分钟），完成后回论文库可见。
          </p>
        </div>
      )}
    </div>
  );
}
