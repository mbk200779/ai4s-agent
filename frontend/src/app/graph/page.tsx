'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import SuggestionChips from '@/components/SuggestionChips';
import GraphInsight from '@/components/graph/GraphInsight';
import GraphMethodCards from '@/components/graph/GraphMethodCards';
import LibraryGraph from '@/components/graph/LibraryGraph';
import GraphView from '@/components/graph/GraphView';
import type { GraphData } from '@/components/graph/types';
import type { LibraryGraphPayload } from '@/components/graph/libraryTypes';
import { useToast } from '@/components/Toast';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

type Tab = 'library' | 'external';

export default function GraphPage() {
  const [tab, setTab] = useState<Tab>('library');

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex items-center gap-4">
          <span className="flex h-11 w-11 items-center justify-center rounded-lg border border-[var(--line)] bg-[var(--surface)] text-[var(--signal)]">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6.75l3.75 3.75M12 5.25a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0zm-7.5 10.5a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zm15 0a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zm-4.5-10.5a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zM7.5 8.25l3 2.25m0 3.75l3-2.25" />
            </svg>
          </span>
          <div>
            <h1 className="display text-3xl">引用图谱</h1>
            <p className="mt-0.5 text-[12.5px] text-[var(--ink-2)]">
              库内关系看「谁是谁的前提」，图上的每个标签都能追到计算口径。
            </p>
          </div>
        </div>

        <div className="flex rounded-md border border-[var(--line)] bg-[var(--surface)] p-0.5">
          {(
            [
              ['library', '库内关系'],
              ['external', '外部引用网络'],
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`rounded px-3 py-1.5 text-[12.5px] transition ${
                tab === key
                  ? 'bg-[var(--signal)] text-white'
                  : 'text-[var(--ink-2)] hover:bg-[var(--surface-3)]'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'library' ? <LibraryTab /> : <ExternalTab />}
    </div>
  );
}

/* ------------------------------------------------------------------ 库内关系 */

function LibraryTab() {
  // 允许用 ?project=proj_xxx 指定课题：多课题时看板要能直接分享某个库的图，
  // 不传则落到默认课题（与上传页一致）。
  const [projectId, setProjectId] = useState<string | null>(null);
  useEffect(() => {
    const pid = new URLSearchParams(window.location.search).get('project');
    setProjectId(pid);
  }, []);

  const [payload, setPayload] = useState<LibraryGraphPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [rebuilding, setRebuilding] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const { toast } = useToast();

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
      const res = await fetch(`${API_BASE}/api/v1/graph/library${qs}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: LibraryGraphPayload = await res.json();
      setPayload(data);
    } catch (e) {
      setError('图谱加载失败，检查后端服务是否在运行');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  const rebuild = async () => {
    setRebuilding(true);
    try {
      const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
      const res = await fetch(`${API_BASE}/api/v1/graph/refs/rebuild${qs}`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const ok = (data.results || []).filter((r: { refs?: number }) => r.refs).length;
      toast(ok ? `已重抽 ${ok} 篇的参考文献` : '没有需要重抽的版本', ok ? 'success' : 'error');
      await load();
    } catch (e) {
      toast('重抽失败', 'error');
    } finally {
      setRebuilding(false);
    }
  };

  if (loading && !payload) {
    return (
      <div className="card flex h-64 items-center justify-center text-sm text-[var(--ink-3)]">
        正在计算图谱与指标…
      </div>
    );
  }

  if (error) {
    return (
      <div className="card p-6">
        <p className="text-sm text-[var(--danger)]">{error}</p>
        <button type="button" onClick={load} className="btn-primary mt-3 h-8 px-3 text-xs">
          重试
        </button>
      </div>
    );
  }

  if (!payload) return null;

  // 空态：宁可明确说"没有数据、原因是这个"，也不要画一张空图
  if (payload.empty) {
    return (
      <div className="card card-tick p-8">
        <div className="mono text-[11px] tracking-[0.16em] text-[var(--ink-3)]">EMPTY STATE</div>
        <h2 className="display mt-2 text-xl">{payload.empty.title}</h2>
        <p className="mt-2 max-w-2xl text-[13px] leading-relaxed text-[var(--ink-2)]">
          {payload.empty.hint}
        </p>
        <div className="mt-4 flex flex-wrap gap-2">
          {payload.empty.action === '重抽参考文献' && (
            <button
              type="button"
              onClick={rebuild}
              disabled={rebuilding}
              className="btn-primary h-8 px-3 text-xs"
            >
              {rebuilding ? '重抽中…' : '重抽参考文献'}
            </button>
          )}
          {payload.empty.action === '去上传' && (
            <a href="/library#import" className="btn-primary h-8 px-3 text-xs leading-8">
              去上传
            </a>
          )}
          <button
            type="button"
            onClick={load}
            className="h-8 rounded-md border border-[var(--line)] px-3 text-xs text-[var(--ink-2)]"
          >
            刷新
          </button>
        </div>
        <div className="mt-5 grid gap-2 text-[11.5px] text-[var(--ink-3)] sm:grid-cols-3">
          <div className="rounded border border-[var(--line)] bg-[var(--surface-2)] p-2">
            条目 {payload.match_stats.refs ?? 0} 条 · 匹配 {payload.match_stats.matched ?? 0} 条
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--surface-2)] p-2">
            论文 {payload.match_stats.papers ?? 0} 篇 · 有参考文献{" "}
            {(payload.match_stats.by_paper || []).filter((p) => p.refs > 0).length} 篇
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--surface-2)] p-2">
            解析耗时 {payload.elapsed_ms}ms
          </div>
        </div>
      </div>
    );
  }

  const scope = payload.scope;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-[var(--ink-3)]">
        <span className="mono">SCOPE</span>
        <span>库内 {scope.library_nodes ?? 0} 篇</span>
        <span>·</span>
        <span>库外缺口 {scope.gap_nodes ?? 0} 个</span>
        <span>·</span>
        <span>库内边 {scope.library_edges ?? 0} 条 / 总边 {scope.total_edges ?? 0} 条</span>
        <span>·</span>
        <span>服务端 {payload.elapsed_ms}ms</span>
        <button
          type="button"
          onClick={load}
          className="ml-auto h-6 rounded border border-[var(--line)] px-2 text-[11px] hover:bg-[var(--surface-3)]"
        >
          刷新
        </button>
      </div>

      <div className="grid items-start gap-3 lg:grid-cols-[minmax(0,1.6fr)_minmax(320px,1fr)]">
        {/* 左栏：图 → 口径与规则；右栏：匹配概览 → 节点解读。
            口径卡放在图正下方而不是右栏末尾，是为了让"图上的标签怎么来的"能在
            同一屏里查，不用先滚过一屏解读。 */}
        <div className="space-y-3">
          <LibraryGraph payload={payload} selected={selected} onSelect={setSelected} />
          <GraphMethodCards payload={payload} />
        </div>
        <GraphInsight
          payload={payload}
          selected={selected}
          onSelect={setSelected}
          onRebuild={rebuild}
          rebuilding={rebuilding}
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 外部网络 */

function ExternalTab() {
  const [arxivId, setArxivId] = useState('');
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const { toast } = useToast();

  const buildGraph = async () => {
    if (!arxivId.trim() || loading) return;
    setLoading(true);
    try {
      // 取全量（含应付式引用），由前端开关决定是否显示
      const res = await fetch(`${API_BASE}/api/v1/graph/build`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          seed_arxiv_id: arxivId.trim(),
          depth: 1,
          filter_perfunctory: false,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: GraphData = await res.json();
      if (!data?.nodes?.length) {
        toast('没有找到引用关系，换一篇试试', 'error');
        return;
      }
      setGraph(data);
    } catch (e) {
      toast('构建失败，请检查后端服务', 'error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      <p className="text-[12.5px] text-[var(--ink-2)]">
        以一篇论文为种子，实时向 OpenAlex 要它的引用网络；数据在库外，不参与上图指标。
      </p>
      <div className="flex gap-2">
        <input
          ref={inputRef}
          type="text"
          value={arxivId}
          onChange={(e) => setArxivId(e.target.value)}
          placeholder="输入中心论文 arXiv ID"
          className="field flex-1"
          onKeyDown={(e) => e.key === 'Enter' && buildGraph()}
        />
        <button onClick={buildGraph} disabled={loading} className="btn-primary">
          {loading ? '构建中...' : '构建'}
        </button>
      </div>

      <SuggestionChips
        title="试试这些论文"
        items={[
          { label: '1706.03762 · Attention Is All You Need', value: '1706.03762' },
          { label: '1810.04805 · BERT 预训练模型', value: '1810.04805' },
          { label: '2010.11929 · ViT 视觉 Transformer', value: '2010.11929' },
          { label: '2106.09685 · LoRA 低秩适配', value: '2106.09685' },
        ]}
        onPick={(id) => {
          setArxivId(id);
          inputRef.current?.focus();
        }}
      />

      {graph && <GraphView graph={graph} />}
    </div>
  );
}
