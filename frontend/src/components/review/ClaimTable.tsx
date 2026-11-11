'use client';

import { useState } from 'react';
import { ClaimRow, CONFLICT_STYLE, STANCE_STYLE } from '@/types/review';

/**
 * claim 表：一行 = 一个观点簇。
 *
 * 为什么按"簇"而不按"claim"列表：原始 claim 有几十条，同一结论被两三篇论文各说一遍，
 * 按条列会让读者看到一堆重复行；按簇列才回答得了他真正问的问题——
 * 「这条结论由哪几篇支撑、立场是什么、有没有人反对」。
 * 每条 claim 的原文仍然在展开区里，点开可查。
 *
 * 关键设计：**「仅单篇支持」不是次要信息，它和结论本身同级**。
 * 放在独立的列里、用琥珀色标出来，读者一眼就能看出哪些结论其实只有一篇文献撑着。
 */
export default function ClaimTable({ claims }: { claims: ClaimRow[] }) {
  const [open, setOpen] = useState<string | null>(null);

  if (!claims.length) return null;

  return (
    <div className="card overflow-hidden">
      <div className="flex items-baseline justify-between border-b border-[var(--line)] px-5 py-3">
        <h3 className="display text-lg">观点表</h3>
        <span className="mono text-xs text-[var(--ink-3)]">
          {claims.length} 个观点簇 · 点击行展开证据原文
        </span>
      </div>

      {/* 表头。没有表头时「1 篇 / 1 条」这种数字读者只能靠猜，列名必须写在脸上。 */}
      <div className="flex items-center gap-3 border-b border-[var(--line)] bg-[var(--surface-2)] px-5 py-2 text-xs text-[var(--ink-3)]">
        <span className="w-9 shrink-0" />
        <span className="min-w-0 flex-1">结论（一行 = 一个观点簇）</span>
        <span className="w-[4.5rem] shrink-0 text-center">立场</span>
        <span className="w-20 shrink-0 text-center">论文 / 证据</span>
        <span className="w-24 shrink-0 text-center">证据强度</span>
        <span className="w-16 shrink-0 text-center">冲突</span>
      </div>

      <div className="divide-y divide-[var(--line)]">
        {claims.map((c) => {
          const stance = STANCE_STYLE[c.stance] ?? STANCE_STYLE.support;
          const single = c.strength === 'single_paper';
          const expanded = open === c.id;
          return (
            <div key={c.id}>
              <button
                type="button"
                onClick={() => setOpen(expanded ? null : c.id)}
                className="flex w-full items-start gap-3 px-5 py-3 text-left transition-colors hover:bg-[var(--surface-2)]"
              >
                <span className="mono mt-0.5 w-9 shrink-0 text-xs text-[var(--ink-3)]">{c.id}</span>

                <span className="min-w-0 flex-1">
                  <span className="block text-sm leading-relaxed text-[var(--ink)]">{c.statement}</span>
                  <span className="mono mt-1 block truncate text-xs text-[var(--ink-3)]">
                    轴：{c.axis}
                  </span>
                </span>

                <span
                  className="mt-0.5 w-[4.5rem] shrink-0 rounded border px-1.5 py-0.5 text-center text-xs"
                  style={{ color: stance.color, background: stance.bg, borderColor: stance.border }}
                  title={stance.hint}
                >
                  {stance.label}
                </span>

                <span className="mt-0.5 w-20 shrink-0 text-center text-xs text-[var(--ink-2)]">
                  {c.paper_count} 篇 / {c.claim_count} 条
                </span>

                <span className="mt-0.5 w-24 shrink-0 text-center">
                  <span
                    className="rounded border px-1.5 py-0.5 text-xs"
                    style={
                      single
                        ? { color: 'var(--warn)', background: 'rgba(217,119,6,0.10)', borderColor: 'var(--warn)' }
                        : { color: 'var(--signal)', background: 'var(--signal-soft)', borderColor: 'var(--signal)' }
                    }
                  >
                    {single ? '仅单篇支持' : '多篇支持'}
                  </span>
                </span>

                <span className="mt-0.5 w-16 shrink-0 text-center text-xs">
                  {c.conflicts.length === 0 ? (
                    <span className="text-[var(--ink-3)]">—</span>
                  ) : (
                    c.conflicts.map((k) => {
                      const st = CONFLICT_STYLE[k.type] ?? CONFLICT_STYLE.scope_mismatch;
                      return (
                        <span
                          key={k.id}
                          className="mono rounded px-1 py-0.5"
                          style={{
                            color: st.color,
                            background: k.confirmed === false ? 'transparent' : 'rgba(0,0,0,0.04)',
                            textDecoration: k.confirmed === false ? 'line-through' : 'none',
                          }}
                          title={`${k.type_label} ↔ ${k.with_cluster}：${k.reason}`}
                        >
                          {k.id}
                        </span>
                      );
                    })
                  )}
                </span>
              </button>

              {expanded && (
                <div className="space-y-3 bg-[var(--surface-2)] px-5 pb-4 pl-[4.25rem] pt-1">
                  {c.conditions.length > 0 && (
                    <div className="text-xs text-[var(--ink-2)]">
                      <span className="text-[var(--ink-3)]">成立条件：</span>
                      {c.conditions.join('；')}
                    </div>
                  )}

                  <div className="space-y-2">
                    {c.evidence.map((e, i) => (
                      <div
                        key={`${e.chunk_id}-${i}`}
                        className="rounded border border-[var(--line)] bg-[var(--surface)] p-3"
                      >
                        <div className="mb-1 flex items-center gap-2 text-xs text-[var(--ink-3)]">
                          {typeof e.ref === 'number' && (
                            <span className="mono rounded bg-[var(--signal-soft)] px-1 text-[var(--signal)]">
                              引#{e.ref}
                            </span>
                          )}
                          <span className="mono truncate">
                            {e.arxiv_id} · p.{e.page}
                            {e.section ? ` · ${e.section}` : ''}
                          </span>
                          {typeof e.quote_score === 'number' && e.quote_score < 1 && (
                            <span className="text-[var(--warn)]">
                              引文匹配 {e.quote_score.toFixed(2)}
                            </span>
                          )}
                        </div>
                        <div className="text-xs leading-relaxed text-[var(--ink-2)]">
                          “{e.quote}”
                        </div>
                        {e.statement && (
                          <div className="mt-1 text-xs text-[var(--ink-3)]">→ {e.statement}</div>
                        )}
                      </div>
                    ))}
                  </div>

                  {c.conflicts.length > 0 && (
                    <div className="space-y-1">
                      {c.conflicts.map((k) => (
                        <div key={k.id} className="text-xs leading-relaxed text-[var(--ink-2)]">
                          <span className="mono text-[var(--ink-3)]">{k.id}</span>{' '}
                          <span style={{ color: (CONFLICT_STYLE[k.type] ?? CONFLICT_STYLE.scope_mismatch).color }}>
                            {k.type_label}
                          </span>{' '}
                          ↔ {k.with_cluster}
                          {k.confirmed === false && <span className="text-[var(--ink-3)]">（已复核排除）</span>}
                          {k.reason && <span className="text-[var(--ink-3)]">：{k.reason}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
