'use client';

/**
 * 上传前的关系判定弹窗。
 *
 * 这里要回答的不是"像不像"，而是"是什么关系"，所以界面按三种关系分三套文案：
 *   version_of        同一工作的不同版本 —— 给"设为当前版本 / 并存 / 跳过"
 *   possible_duplicate 不同工作的高重合   —— 给"并存 / 合并（带风险提示）/ 仅标注"
 *   related           同主题相关          —— 给"正常入库 / 入库并标注"
 *
 * 每个结论都必须能看到依据：正文重合率、相似段落数、标题与作者重合度、以及
 * 差异摘要（新增/删去多少段、哪些数字被改过）。只给一个"相似度 94%"的进度条
 * 是不够的——用户没法据此判断该不该合并。
 */

import { useEffect, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

export type DedupeAction = 'keep_new' | 'keep_both' | 'link_only';

export interface DedupeActionOption {
  action: DedupeAction;
  label: string;
  hint: string;
}

export interface DedupeDiff {
  from: { label: string; pages: number | null; paragraphs: number; chars: number; sections: string[]; chunks: number };
  to: { label: string; pages: number | null; paragraphs: number; chars: number; sections: string[]; chunks: number };
  paragraphs: { matched: number; added: number; removed: number; matched_ratio: number };
  sections: { added: string[]; removed: string[]; kept: number; comparable?: boolean; note?: string };
  numbers: { from: string; to: string; context: string }[];
  delta_chars: number;
  highlights: string[];
  examples: { added: string[]; removed: string[] };
}

export interface DedupeCandidate {
  version_id: string;
  peer_simhash?: string;
  paper_id: string;
  paper_title: string;
  version_label: string;
  year: number | null;
  authors: string[];
  pages: number | null;
  other_project: boolean;
  relation: string;
  relation_label: string;
  kind: string;
  confidence: number;
  reasons: string[];
  caveats: string[];
  similarity: Record<string, number | string>;
  hint: string;
  actions: DedupeActionOption[];
  memorized: { action: string; relation: string; decided_at?: string; suppress?: boolean } | null;
  fingerprint_source: string;
  diff: DedupeDiff | null;
}

export interface ExactDuplicate {
  version_id: string;
  version_label: string;
  paper_id: string;
  paper_title: string;
  arxiv_id?: string;
  status?: string;
}

export interface PrecheckResult {
  ask: boolean;
  suppressed: boolean;
  /** 字节级完全相同的文件：这条路上不做关系判定，直接别问 */
  exact_duplicate?: ExactDuplicate | null;
  primary: DedupeCandidate | null;
  others: DedupeCandidate[];
  fingerprint: {
    simhash: string;
    para_count: number;
    shingle_count: number;
    body_chars: number;
    title: string | null;
    authors: string[];
    candidates_considered: number;
    peers_scanned: number;
  };
  elapsed: number;
  version_label: string;
}

const KIND_LABEL: Record<string, string> = {
  retypeset: '重排版',
  revision: '小幅修订',
  major_revision: '大幅改版',
  same_body_other_meta: '正文相同、元数据不同',
  partial_overlap: '部分重合',
  same_topic: '同主题',
};

// 三种关系各自的配色：同一工作用主信号青，疑似重复必须用警示色（它会诱导
// 用户做"合并"这种不可逆的选择），同主题相关用中性色、不抢注意力。
function palette(relation: string) {
  if (relation === 'version_of')
    return { fg: 'var(--signal)', bg: 'var(--signal-soft)', border: 'var(--signal)' };
  if (relation === 'possible_duplicate')
    return { fg: 'var(--warn)', bg: '#fdf3e3', border: 'var(--warn)' };
  return { fg: 'var(--ink-2)', bg: 'var(--surface-2)', border: 'var(--line-strong)' };
}

const pct = (v: unknown, digits = 0) =>
  typeof v === 'number' ? `${(v * 100).toFixed(digits)}%` : '—';

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="border border-[var(--line)] bg-[var(--surface-2)] px-2.5 py-2">
      <div className="mono text-[10px] uppercase tracking-wider text-[var(--ink-3)]">{label}</div>
      <div className="mono mt-0.5 text-sm text-[var(--ink)]">{value}</div>
      {sub && <div className="mono mt-0.5 text-[10px] text-[var(--ink-3)]">{sub}</div>}
    </div>
  );
}

export default function DedupePrompt({
  result,
  busy,
  onCancel,
  onConfirm,
}: {
  result: PrecheckResult;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: (action: DedupeAction, suppress: boolean) => void;
}) {
  const peer = result.primary!;
  const [action, setAction] = useState<DedupeAction>(peer.actions[0]?.action ?? 'keep_both');
  const [suppress, setSuppress] = useState(true);
  const [showExamples, setShowExamples] = useState(false);
  const tone = palette(peer.relation);
  const sim = peer.similarity || {};
  const diff = peer.diff;

  // Esc 关闭：弹窗是"可以后悔"的一步，不能只有鼠标一条路
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onCancel]);

  const selected = peer.actions.find((a) => a.action === action);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 backdrop-blur-[2px] sm:p-8"
      onClick={() => !busy && onCancel()}
    >
      <div
        className="card w-full max-w-3xl p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        {/* ---- 头部：关系是结论，放在最上面 ---- */}
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="eyebrow">DEDUPE.PRECHECK</div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span
                className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold"
                style={{ color: tone.fg, background: tone.bg, borderColor: tone.border }}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: tone.fg }} />
                {peer.relation_label}
              </span>
              <span className="mono rounded border border-[var(--line)] px-1.5 py-0.5 text-[11px] text-[var(--ink-2)]">
                {KIND_LABEL[peer.kind] || peer.kind}
              </span>
              <span className="mono text-[11px] text-[var(--ink-3)]">
                置信度 {(peer.confidence * 100).toFixed(0)}% · 比对耗时 {result.elapsed.toFixed(2)}s
              </span>
            </div>
          </div>
          <button
            onClick={() => !busy && onCancel()}
            className="mono shrink-0 text-xs text-[var(--ink-3)] hover:text-[var(--ink)]"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        {/* ---- 和谁撞了 ---- */}
        <div className="mt-4 border border-[var(--line)] bg-[var(--surface-2)] p-3">
          <div className="mono text-[10px] uppercase tracking-wider text-[var(--ink-3)]">
            库内最接近的版本
          </div>
          <div className="mt-1 font-serif text-[15px] leading-snug text-[var(--ink)]">
            {peer.paper_title}
          </div>
          <div className="mono mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--ink-2)]">
            <span className="rounded border border-[var(--line-strong)] px-1.5 py-0.5">
              {peer.version_label}
            </span>
            {peer.pages ? <span>{peer.pages} 页</span> : null}
            <span className="truncate">
              {peer.authors.length ? peer.authors.slice(0, 4).join('、') + (peer.authors.length > 4 ? ' 等' : '') : '作者未知'}
            </span>
            {peer.other_project && (
              <span className="rounded bg-[#fdf3e3] px-1.5 py-0.5 text-[var(--warn)]">其他课题</span>
            )}
            <span className="text-[var(--ink-3)]">指纹来源 {peer.fingerprint_source}</span>
          </div>
          {peer.hint && (
            <div className="mt-2 text-xs" style={{ color: tone.fg }}>
              {peer.hint}
            </div>
          )}
        </div>

        {/* ---- 依据：为什么是这个结论 ---- */}
        <div className="mt-4 grid gap-3 md:grid-cols-[1.15fr_1fr]">
          <div>
            <div className="mono mb-1.5 text-[10px] uppercase tracking-wider text-[var(--ink-3)]">
              判定依据
            </div>
            <ul className="space-y-1">
              {peer.reasons.map((r, i) => (
                <li key={i} className="flex gap-2 text-xs leading-relaxed text-[var(--ink-2)]">
                  <span className="mono mt-[3px] shrink-0 text-[10px] text-[var(--ink-3)]">
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
            {peer.caveats.length > 0 && (
              <div className="mt-2 border-l-2 border-[var(--warn)] bg-[#fdf3e3] px-2.5 py-1.5">
                {peer.caveats.map((c, i) => (
                  <div key={i} className="text-xs leading-relaxed text-[var(--warn)]">
                    {c}
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>
            <div className="mono mb-1.5 text-[10px] uppercase tracking-wider text-[var(--ink-3)]">
              相似度明细
            </div>
            <div className="grid grid-cols-2 gap-1.5">
              <Metric label="正文重合·本次" value={pct(sim.body_overlap_new, 1)} sub={`库内 ${pct(sim.body_overlap_peer, 1)}`} />
              <Metric
                label="相似段落"
                value={`${sim.para_hit_new ?? '—'}/${sim.para_total_new ?? '—'}`}
                sub={`Jaccard ${typeof sim.jaccard === 'number' ? sim.jaccard.toFixed(2) : '—'}`}
              />
              <Metric label="标题重合" value={typeof sim.title_sim === 'number' ? sim.title_sim.toFixed(2) : '—'} />
              <Metric label="作者重合" value={typeof sim.author_sim === 'number' ? sim.author_sim.toFixed(2) : '—'} />
              <Metric label="摘要语义" value={typeof sim.abstract_cos === 'number' ? sim.abstract_cos.toFixed(3) : '—'} />
              <Metric
                label="篇幅"
                value={`${sim.chars_new ?? '—'} / ${sim.chars_peer ?? '—'}`}
                sub={`simhash 距离 ${sim.simhash_distance ?? '—'}/64`}
              />
            </div>
          </div>
        </div>

        {/* ---- 差异摘要 ---- */}
        {diff && (
          <div className="mt-4 border border-[var(--line)]">
            <div className="flex items-center justify-between border-b border-[var(--line)] bg-[var(--surface-2)] px-3 py-2">
              <span className="mono text-[10px] uppercase tracking-wider text-[var(--ink-3)]">
                版本差异摘要
              </span>
              <span className="mono text-[11px] text-[var(--ink-2)]">
                {diff.from.label} → {diff.to.label} · {diff.from.paragraphs} 段 → {diff.to.paragraphs} 段
                {diff.delta_chars >= 0 ? ' · 正文 +' : ' · 正文 '}
                {diff.delta_chars} 字符
              </span>
            </div>
            <div className="grid grid-cols-3 divide-x divide-[var(--line)] border-b border-[var(--line)]">
              {[
                { k: '匹配', v: diff.paragraphs.matched, s: `${(diff.paragraphs.matched_ratio * 100).toFixed(0)}% 段落一对一命中` },
                { k: '新增', v: diff.paragraphs.added, s: '新版有、旧版没有' },
                { k: '删去', v: diff.paragraphs.removed, s: '旧版有、新版没有' },
              ].map((c) => (
                <div key={c.k} className="px-3 py-2">
                  <div className="mono text-[10px] uppercase tracking-wider text-[var(--ink-3)]">{c.k}</div>
                  <div className="mono text-base text-[var(--ink)]">{c.v}</div>
                  <div className="mono text-[10px] text-[var(--ink-3)]">{c.s}</div>
                </div>
              ))}
            </div>
            <div className="px-3 py-2">
              {diff.highlights.length > 0 && (
                <ul className="space-y-0.5">
                  {diff.highlights.map((h, i) => (
                    <li key={i} className="flex gap-1.5 text-xs text-[var(--ink-2)]">
                      <span style={{ color: tone.fg }}>·</span>
                      {h}
                    </li>
                  ))}
                </ul>
              )}
              {diff.numbers.length > 0 && (
                <div className="mt-2 space-y-1">
                  {diff.numbers.slice(0, 3).map((n, i) => (
                    <div key={i} className="mono text-[11px] text-[var(--ink-2)]">
                      <span className="text-[var(--danger)] line-through">{n.from}</span>
                      {' → '}
                      <span className="text-[var(--ok)]">{n.to}</span>
                      <span className="ml-2 text-[var(--ink-3)]">{n.context}</span>
                    </div>
                  ))}
                </div>
              )}
              {diff.sections.note && (
                <div className="mono mt-2 text-[11px] text-[var(--ink-3)]">{diff.sections.note}</div>
              )}
              {(diff.examples.added.length > 0 || diff.examples.removed.length > 0) && (
                <>
                  <button
                    onClick={() => setShowExamples((v) => !v)}
                    className="mono mt-2 text-[11px] text-[var(--signal)] hover:underline"
                  >
                    {showExamples ? '收起样例段落' : '展开样例段落'}
                  </button>
                  {showExamples && (
                    <div className="mt-2 grid gap-2 md:grid-cols-2">
                      {[
                        { title: `新增（${diff.to.label}）`, items: diff.examples.added, color: 'var(--ok)' },
                        { title: `删去（${diff.from.label}）`, items: diff.examples.removed, color: 'var(--danger)' },
                      ].map((col) => (
                        <div key={col.title} className="border border-[var(--line)]">
                          <div className="mono border-b border-[var(--line)] bg-[var(--surface-2)] px-2 py-1 text-[10px]" style={{ color: col.color }}>
                            {col.title}
                          </div>
                          <div className="space-y-1 p-2">
                            {col.items.slice(0, 3).map((t, i) => (
                              <div key={i} className="text-[11px] leading-snug text-[var(--ink-2)]">
                                {t}
                              </div>
                            ))}
                            {col.items.length === 0 && (
                              <div className="text-[11px] text-[var(--ink-3)]">无</div>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        )}

        {/* ---- 操作：语义按关系变化 ---- */}
        <div className="mt-4">
          <div className="mono mb-1.5 text-[10px] uppercase tracking-wider text-[var(--ink-3)]">
            怎么处理
          </div>
          <div className="space-y-1.5">
            {peer.actions.map((a) => {
              const on = a.action === action;
              return (
                <button
                  key={a.action}
                  onClick={() => setAction(a.action)}
                  disabled={busy}
                  className="flex w-full items-start gap-3 border px-3 py-2 text-left transition"
                  style={{
                    borderColor: on ? tone.border : 'var(--line)',
                    background: on ? tone.bg : 'var(--surface)',
                  }}
                >
                  <span
                    className="mt-[3px] flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border"
                    style={{ borderColor: on ? tone.fg : 'var(--line-strong)' }}
                  >
                    {on && <span className="h-2 w-2 rounded-full" style={{ background: tone.fg }} />}
                  </span>
                  <span>
                    <span className="text-sm font-medium text-[var(--ink)]">{a.label}</span>
                    <span className="ml-2 text-xs text-[var(--ink-3)]">{a.hint}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        {peer.memorized && (
          <div className="mono mt-2 text-[11px] text-[var(--ink-3)]">
            上次对同一对内容的处理：{peer.memorized.action}（本次已按它预选）
          </div>
        )}

        {result.others.length > 0 && (
          <div className="mono mt-2 text-[11px] text-[var(--ink-3)]">
            另有 {result.others.length} 个较接近的版本：
            {result.others.map((o) => ` ${o.paper_title.slice(0, 24)}(${o.version_label}, ${(o.confidence * 100).toFixed(0)}%)`).join('；')}
          </div>
        )}

        {/* ---- 页脚 ---- */}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] pt-3">
          <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--ink-2)]">
            <input
              type="checkbox"
              checked={suppress}
              onChange={(e) => setSuppress(e.target.checked)}
              className="h-3.5 w-3.5 accent-[var(--signal)]"
            />
            记住这次选择，之后同一对内容不再弹窗
          </label>
          <div className="flex items-center gap-2">
            <button onClick={onCancel} disabled={busy} className="btn-ghost">
              取消
            </button>
            <button
              onClick={() => onConfirm(action, suppress)}
              disabled={busy}
              className="btn-primary"
              title={selected?.hint}
            >
              {busy ? '处理中…' : selected?.label ?? '确认'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 调用预检；失败时返回 null（预检挂了不该挡住上传）。 */
export async function runPrecheck(
  file: File,
  opts: { projectId?: string; version?: string; withDiff?: boolean } = {},
): Promise<PrecheckResult | null> {
  const form = new FormData();
  form.append('file', file);
  if (opts.projectId) form.append('project_id', opts.projectId);
  if (opts.version) form.append('version', opts.version);
  form.append('with_diff', String(opts.withDiff ?? true));
  try {
    const res = await fetch(`${API_BASE}/api/v1/dedupe/precheck`, { method: 'POST', body: form });
    if (!res.ok) return null;
    return (await res.json()) as PrecheckResult;
  } catch {
    return null;
  }
}

/** 记录用户的选择（pair_key 跟内容走，同一对内容重传时用它做去重提示）。 */
export async function recordDecision(
  result: PrecheckResult,
  action: DedupeAction,
  suppress: boolean,
  ids: { newVersionId?: string } = {},
) {
  const peer = result.primary;
  if (!peer) return null;
  try {
    const res = await fetch(`${API_BASE}/api/v1/dedupe/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        relation: peer.relation,
        action,
        confidence: peer.confidence,
        peer_version_id: peer.version_id,
        new_version_id: ids.newVersionId || '',
        new_simhash: result.fingerprint.simhash,
        peer_simhash: peer.peer_simhash || '',
        new_label: result.version_label || '',
        peer_label: peer.version_label,
        suppress,
        evidence: {
          reasons: peer.reasons,
          similarity: peer.similarity,
          kind: peer.kind,
          diff_highlights: peer.diff?.highlights ?? [],
        },
      }),
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}
