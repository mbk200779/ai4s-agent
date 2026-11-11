'use client';

import { useMemo } from 'react';
import type { LibraryGraphPayload, LibraryNode } from './libraryTypes';

const METHOD_LABEL: Record<string, string> = {
  arxiv_id: 'arXiv ID',
  title_in_raw: '标题原文命中',
  title_exact: '标题完全一致',
  title_fuzzy: '标题相似',
  title_author: '标题+作者',
  author_year: '作者+年份',
  unmatched: '未匹配',
};

function Chip({ children, tone = 'muted' }: { children: React.ReactNode; tone?: string }) {
  const cls =
    tone === 'signal'
      ? 'border-[var(--signal)] bg-[var(--signal-soft)] text-[var(--signal-hover)]'
      : tone === 'amber'
        ? 'border-[#f59e0b] bg-[#fffbeb] text-[#b45309]'
        : tone === 'violet'
          ? 'border-[#7c3aed] bg-[#f5f3ff] text-[#5b21b6]'
          : 'border-[var(--line)] bg-[var(--surface-2)] text-[var(--ink-3)]';
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] ${cls}`}>
      {children}
    </span>
  );
}

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-md border border-[var(--line)] bg-[var(--surface-2)] px-2.5 py-2">
      <div className="text-[10px] text-[var(--ink-3)]">{label}</div>
      <div className="mono text-[15px] leading-tight text-[var(--ink)]">{value}</div>
      {sub && <div className="text-[10px] text-[var(--ink-3)]">{sub}</div>}
    </div>
  );
}

export default function GraphInsight({
  payload,
  selected,
  onSelect,
  onRebuild,
  rebuilding,
}: {
  payload: LibraryGraphPayload;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onRebuild: () => void;
  rebuilding: boolean;
}) {
  const stats = payload.match_stats;
  const nodes = payload.nodes;
  const selectedNode = useMemo(
    () => nodes.find((n) => n.id === selected) ?? null,
    [nodes, selected],
  );
  const inEdges = useMemo(
    () =>
      selected
        ? payload.edges.filter((e) => e.target === selected).sort((a, b) => b.weight - a.weight)
        : [],
    [payload.edges, selected],
  );
  const titleOf = useMemo(() => new Map(nodes.map((n) => [n.id, n.title])), [nodes]);

  const rate = stats.refs ? (stats.matched ?? 0) / stats.refs : 0;
  const gapCount = payload.scope.gap_nodes ?? 0;

  return (
    <div className="flex flex-col gap-3">
      {/* 匹配概览：失败率是这一屏最先要能解释的数字 */}
      <section className="card p-3.5">
        <div className="flex items-center justify-between">
          <h2 className="text-[13px] font-semibold text-[var(--ink)]">匹配概览</h2>
          <Chip tone={rate >= 0.6 ? 'signal' : 'muted'}>匹配率 {(rate * 100).toFixed(1)}%</Chip>
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2">
          <Metric label="条目" value={String(stats.refs ?? 0)} sub="References" />
          <Metric label="匹配到库内" value={String(stats.matched ?? 0)} sub="一一对应" />
          <Metric label="库外缺口" value={String(gapCount)} sub="被 ≥2 篇引用" />
        </div>
        <div className="mt-2 space-y-1 text-[11px] leading-relaxed text-[var(--ink-2)]">
          <p>
            {stats.refs ?? 0} 条参考文献里 {stats.matched ?? 0} 条指向库内论文，其余{" "}
            {(stats.refs ?? 0) - (stats.matched ?? 0)} 条指向库外——库越大这个比例越高，
            所以它衡量的是<strong className="font-medium">收录覆盖度</strong>，不是匹配质量。
          </p>
          {typeof stats.quality?.score === 'number' && (
            <p>
              条目字段完整度 <span className="mono">{(stats.quality.score * 100).toFixed(0)}%</span>
              （年份 {stats.quality.with_year ?? 0} / 作者 {stats.quality.with_authors ?? 0} / 标题{" "}
              {stats.quality.with_title ?? 0}）。完整度低时，失败要算在抽取上。
            </p>
          )}
          {payload.warnings.map((w) => (
            <p key={w} className="text-[var(--warn)]">
              {w}
            </p>
          ))}
        </div>
        <button
          type="button"
          onClick={onRebuild}
          disabled={rebuilding}
          className="mt-2 inline-flex h-7 items-center rounded-md border border-[var(--line)] bg-[var(--surface)] px-2 text-[11px] text-[var(--ink-2)] hover:bg-[var(--surface-3)] disabled:opacity-50"
        >
          {rebuilding ? '重抽中…' : '重抽参考文献'}
        </button>
      </section>

      {/* 选中节点：指标 + 依据 + 一跳推荐 */}
      <section className="card p-3.5">
        <div className="flex items-center justify-between">
          <h2 className="text-[13px] font-semibold text-[var(--ink)]">
            {selectedNode ? '节点解读' : '逐条解读'}
          </h2>
          {selectedNode && (
            <button
              type="button"
              className="text-[11px] text-[var(--ink-3)] hover:text-[var(--ink)]"
              onClick={() => onSelect(null)}
            >
              收起
            </button>
          )}
        </div>

        {selectedNode ? (
          <NodeDetail
            node={selectedNode}
            inEdges={inEdges}
            titleOf={titleOf}
            onSelect={onSelect}
          />
        ) : (
          <ul className="mt-2 space-y-2">
            {nodes.map((n) => (
              <li key={n.id}>
                <button
                  type="button"
                  onClick={() => onSelect(n.id)}
                  className="w-full rounded-md border border-transparent px-2 py-1.5 text-left hover:border-[var(--line)] hover:bg-[var(--surface-2)]"
                >
                  <span className="flex items-center gap-1.5">
                    <Chip tone={n.label.tone}>{n.label.label}</Chip>
                    <span className="truncate text-[12px] font-medium text-[var(--ink)]">
                      {n.title}
                    </span>
                  </span>
                  <span className="mt-0.5 block text-[11px] leading-relaxed text-[var(--ink-2)]">
                    {n.sentence}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

    </div>
  );
}

function NodeDetail({
  node,
  inEdges,
  titleOf,
  onSelect,
}: {
  node: LibraryNode;
  inEdges: LibraryGraphPayload['edges'];
  titleOf: Map<string, string>;
  onSelect: (id: string | null) => void;
}) {
  const m = node.metrics;
  const isGap = node.kind === 'gap';
  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Chip tone={node.label.tone}>{node.label.label}</Chip>
        {node.tags
          .filter((t) => t !== node.label.key)
          .map((t) => (
            <Chip key={t}>{t}</Chip>
          ))}
        {node.year && <span className="mono text-[10px] text-[var(--ink-3)]">{node.year}</span>}
      </div>
      <p className="mt-1.5 text-[12.5px] font-medium leading-snug text-[var(--ink)]">{node.title}</p>
      <p className="mt-1 text-[12px] leading-relaxed text-[var(--ink-2)]">{node.sentence}</p>

      <div className="mt-2 grid grid-cols-3 gap-2">
        {isGap ? (
          <>
            <Metric label="库内被引" value={`${m.in_degree} 篇`} sub={`提及 ${m.mentions_in} 次`} />
            <Metric label="PageRank" value="—" sub="库外不参与" />
            <Metric label="Betweenness" value="—" sub="库外不参与" />
          </>
        ) : (
          <>
            <Metric
              label="PageRank"
              value={(m.pagerank ?? 0).toFixed(3)}
              sub={m.pagerank_rank ? `第 ${m.pagerank_rank} 名` : undefined}
            />
            <Metric
              label="Betweenness"
              value={(m.betweenness ?? 0).toFixed(3)}
              sub={m.betweenness_rank ? `第 ${m.betweenness_rank} 名` : undefined}
            />
            <Metric label="入 / 出度" value={`${m.in_degree} / ${m.out_degree}`} sub={`提及 ${m.mentions_in} 次`} />
          </>
        )}
      </div>

      {node.reasons.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-[11px] text-[var(--ink-3)]">
          {node.reasons.map((r) => (
            <li key={r}>· {r}</li>
          ))}
        </ul>
      )}

      {inEdges.length > 0 && (
        <div className="mt-2.5">
          <div className="text-[11px] font-medium text-[var(--ink-2)]">
            {isGap ? '谁引用了它' : '被引用的原文依据'}
          </div>
          <ul className="mt-1 space-y-1.5">
            {inEdges.slice(0, 4).map((e, i) => (
              <li key={`${e.source}-${i}`} className="rounded border border-[var(--line)] bg-[var(--surface-2)] px-2 py-1.5">
                <div className="flex items-center justify-between gap-2">
                  <button
                    type="button"
                    className="truncate text-left text-[11px] font-medium text-[var(--ink)] hover:underline"
                    onClick={() => onSelect(e.source)}
                  >
                    {titleOf.get(e.source) ?? e.source}
                  </button>
                  <span className="mono shrink-0 text-[10px] text-[var(--ink-3)]">×{e.weight}</span>
                </div>
                {e.sentence && (
                  <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-[var(--ink-2)]">
                    “{e.sentence}”
                  </div>
                )}
                <div className="mono mt-0.5 text-[10px] text-[var(--ink-3)]">
                  {e.page ? `第 ${e.page} 页 · ` : ''}
                  {e.match_score > 0
                    ? `${METHOD_LABEL[e.match_method] ?? e.match_method} ${e.match_score.toFixed(2)}`
                    : '未匹配到库内（按条目原文归并）'}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {node.recommendations.length > 0 && (
        <div className="mt-2.5">
          <div className="text-[11px] font-medium text-[var(--ink-2)]">下一步读什么</div>
          <ul className="mt-1 space-y-1">
            {node.recommendations.map((r) => (
              <li key={`${r.paper_id}-${r.relation}`} className="text-[11px] leading-snug">
                <button
                  type="button"
                  className="text-left text-[var(--ink)] hover:underline"
                  onClick={() => onSelect(r.paper_id)}
                >
                  {r.relation === 'cites' ? '前置 ↑ ' : '后续 ↓ '}
                  {r.title}
                </button>
                <span className="block text-[10.5px] text-[var(--ink-3)]">{r.why}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
