'use client';

import { WritingRefMapRow, WritingReference, WritingStats } from '@/types/writing';

const ARXIV_RE = /^\d{4}\.\d{4,5}$/;

export default function ReferenceList({
  references,
  refMap,
  stats,
  onPickCite,
}: {
  references: WritingReference[];
  refMap: WritingRefMapRow[];
  stats: WritingStats;
  onPickCite: (n: number) => void;
}) {
  const citedNos = new Set(refMap.map((r) => r.n));

  return (
    <div className="card card-tick p-6">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="display text-lg">参考文献</h3>
        <span className="mono text-xs text-[var(--ink-3)]">
          {references.length} 条 · 全部由库内 <span className="text-[var(--ink-2)]">papers</span> 表生成
          {stats.refs_rejected > 0 && ` · 拒收 ${stats.refs_rejected} 条`}
        </span>
      </div>

      {references.length === 0 ? (
        <p className="text-sm text-[var(--ink-3)]">本次正文没有引用任何文献。</p>
      ) : (
        <ol className="space-y-3">
          {references.map((ref) => {
            const ok = ref.checked;
            return (
              <li key={ref.ref_no} className="flex gap-3">
                <span className="mono mt-0.5 w-8 shrink-0 text-sm text-[var(--signal)]">
                  [{ref.ref_no}]
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm leading-relaxed text-[var(--ink)]">
                    {(ref.authors || []).length > 0 && (
                      <span className="text-[var(--ink-2)]">
                        {ref.authors.slice(0, 3).join(', ')}
                        {ref.authors.length > 3 ? ' et al.' : ''} ·{' '}
                      </span>
                    )}
                    {ref.title}
                  </p>
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--ink-3)]">
                    {ref.venue && <span>{ref.venue}</span>}
                    {ref.year && <span>{ref.year}</span>}
                    {ARXIV_RE.test(ref.arxiv_id || '') ? (
                      <a
                        href={`https://arxiv.org/abs/${ref.arxiv_id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="mono text-[var(--signal)] hover:underline"
                      >
                        arXiv:{ref.arxiv_id}
                      </a>
                    ) : (
                      ref.arxiv_id && <span className="mono">{ref.arxiv_id}</span>
                    )}
                    <span
                      className={`rounded border px-1.5 py-0.5 ${
                        ok
                          ? 'border-[var(--ok)]/40 bg-[var(--ok)]/10 text-[var(--ok)]'
                          : 'border-[var(--warn)]/50 bg-amber-50 text-[var(--warn)]'
                      }`}
                      title={
                        ok
                          ? '已在 papers 表重新查到，标题一致，且有正文 chunk'
                          : `复核未通过：${ref.issues.join(', ')}`
                      }
                    >
                      {ok ? '库内已核验' : `未核验 · ${ref.issues.join(',')}`}
                    </span>
                    {ref.chunk_nos.filter((n) => citedNos.has(n)).length > 0 && (
                      <span className="flex items-center gap-1">
                        正文引用
                        {ref.chunk_nos
                          .filter((n) => citedNos.has(n))
                          .map((n) => (
                            <button
                              key={n}
                              type="button"
                              onClick={() => onPickCite(n)}
                              className="mono text-[var(--signal)] hover:underline"
                            >
                              引#{n}
                            </button>
                          ))}
                      </span>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      <details className="mt-5 border-t border-[var(--line)] pt-3">
        <summary className="cursor-pointer text-xs text-[var(--ink-3)]">
          引文映射表（引#N → [K] → chunk_id，导出时原样保留）
        </summary>
        <div className="mt-2 overflow-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="text-[var(--ink-3)]">
              <tr>
                <th className="py-1 pr-3 font-normal">引#</th>
                <th className="py-1 pr-3 font-normal">文献</th>
                <th className="py-1 pr-3 font-normal">页</th>
                <th className="py-1 pr-3 font-normal">被引句</th>
                <th className="py-1 font-normal">chunk_id</th>
              </tr>
            </thead>
            <tbody className="mono">
              {refMap.map((row) => (
                <tr
                  key={row.n}
                  className={`border-t border-[var(--line)] ${
                    row.used_in.length ? 'text-[var(--ink-2)]' : 'text-[var(--ink-3)] opacity-70'
                  }`}
                >
                  <td className="py-1 pr-3 text-[var(--signal)]">引#{row.n}</td>
                  <td className="py-1 pr-3">{row.ref_no ? `[${row.ref_no}]` : '—'}</td>
                  <td className="py-1 pr-3">p.{row.page}</td>
                  {/* 检索到 ≠ 用上了：空表示这段原文在证据池里但正文没引它。 */}
                  <td className="py-1 pr-3">
                    {row.used_in.length ? row.used_in.join(' ') : '未引用'}
                  </td>
                  <td className="max-w-[220px] truncate py-1" title={row.chunk_id}>
                    {row.chunk_id}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
