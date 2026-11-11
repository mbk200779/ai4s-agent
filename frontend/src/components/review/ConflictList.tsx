'use client';

import { useState } from 'react';
import { ClaimRow, CONFLICT_STYLE, ReviewConflict, STANCE_STYLE } from '@/types/review';

/**
 * 冲突清单。
 *
 * 为什么要显示"被排除的疑似冲突"：读者看到 A、B 两条结论像是矛盾，
 * 而系统没标冲突，会怀疑是漏检。把"系统看过、判为不相干"的那些摊开，
 * 这个疑问就消失了——**排除也是一种结论，要能被审计**。
 */
export default function ConflictList({
  conflicts,
  claims,
}: {
  conflicts: ReviewConflict[];
  claims: ClaimRow[];
}) {
  const [showRejected, setShowRejected] = useState(false);
  const byId = new Map(claims.map((c) => [c.id, c]));
  const confirmed = conflicts.filter((c) => c.confirmed !== false);
  const rejected = conflicts.filter((c) => c.confirmed === false);

  if (!conflicts.length) {
    return (
      <div className="card p-5 text-sm text-[var(--ink-2)]">
        本次没有检测到互斥结论：所有观点簇的立场方向一致，或分歧只是措辞差异（已复核排除）。
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {confirmed.map((e) => {
        const st = CONFLICT_STYLE[e.type] ?? CONFLICT_STYLE.conditional_split;
        const [aId, bId] = e.clusters;
        const a = byId.get(aId);
        const b = byId.get(bId);
        return (
          <div
            key={e.id}
            className="card p-5"
            style={{ borderLeft: `3px solid ${st.color}` }}
          >
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <span className="mono text-xs text-[var(--ink-3)]">{e.id}</span>
              <span
                className="rounded border px-1.5 py-0.5 text-xs"
                style={{ color: st.color, borderColor: st.color, background: 'transparent' }}
              >
                {e.type_label}
              </span>
              <span className="text-xs text-[var(--ink-3)]">
                {e.confirmed === true ? '已复核确认' : '规则召回 · 待复核'}
              </span>
              {e.rule?.axis_match && (
                <span className="mono text-xs text-[var(--ink-3)]">
                  召回：{e.rule.axis_match === 'exact' ? '同轴异极性' : `语义相似 ${e.rule.axis_sim}`}
                </span>
              )}
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              {[a, b].map((c, i) => {
                if (!c) return null;
                const stance = STANCE_STYLE[c.stance] ?? STANCE_STYLE.support;
                return (
                  <div
                    key={`${e.id}-${i}`}
                    className="rounded border border-[var(--line)] bg-[var(--surface-2)] p-3"
                  >
                    <div className="mb-1 flex items-center gap-2 text-xs">
                      <span className="mono text-[var(--ink-3)]">{c.id}</span>
                      <span style={{ color: stance.color }}>{stance.label}</span>
                      <span className="text-[var(--ink-3)]">{c.paper_count} 篇</span>
                      {c.strength === 'single_paper' && (
                        <span style={{ color: 'var(--warn)' }}>仅单篇支持</span>
                      )}
                    </div>
                    <div className="text-sm leading-relaxed text-[var(--ink)]">{c.statement}</div>
                    {c.conditions.length > 0 && (
                      <div className="mt-1 text-xs text-[var(--ink-3)]">
                        条件：{c.conditions.join('；')}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="mt-3 text-sm leading-relaxed text-[var(--ink-2)]">
              <span className="text-[var(--ink-3)]">判据：</span>
              {e.reason || '（规则召回，未经模型复核）'}
            </div>
            <div className="mono mt-1 text-xs text-[var(--ink-3)]">轴：{e.axis}</div>
          </div>
        );
      })}

      {rejected.length > 0 && (
        <div className="card p-4">
          <button
            type="button"
            onClick={() => setShowRejected((v) => !v)}
            className="flex w-full items-center justify-between text-left text-sm text-[var(--ink-2)]"
          >
            <span>
              已复核排除的疑似冲突 <span className="mono text-[var(--ink-3)]">{rejected.length}</span> 处
            </span>
            <span className="text-xs text-[var(--ink-3)]">{showRejected ? '收起' : '展开'}</span>
          </button>
          {showRejected && (
            <div className="mt-3 space-y-2">
              {rejected.map((e) => (
                <div key={e.id} className="rounded bg-[var(--surface-2)] p-3 text-xs">
                  <div className="mb-1 flex items-center gap-2 text-[var(--ink-3)]">
                    <span className="mono">{e.id}</span>
                    <span>{e.type_label}</span>
                    <span className="mono">{e.clusters.join(' ↔ ')}</span>
                  </div>
                  <div className="leading-relaxed text-[var(--ink-2)]">{e.reason}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
