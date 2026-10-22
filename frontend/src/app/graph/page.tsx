'use client';

import { useState, useRef, MouseEvent, WheelEvent } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

interface Node {
  id: string;
  label: string;
  title: string;
  arxiv_id: string;
  influential_citation_count: number;
  influence_score: number;
  source_url: string;
  group: string;
}

interface Edge {
  source: string;
  target: string;
  relation: string;
  weight: number;
  is_perfunctory: boolean;
}

interface GraphData {
  nodes: Node[];
  edges: Edge[];
}

export default function GraphPage() {
  const [arxivId, setArxivId] = useState('');
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(false);

  const buildGraph = async () => {
    if (!arxivId.trim()) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/graph/build`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seed_arxiv_id: arxivId, depth: 1, filter_perfunctory: true }),
      });
      const data: GraphData = await res.json();
      setGraph(data);
    } catch (e) {
      alert('构建失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">引用图谱（可交互、可点跳原文）</h2>
      <div className="flex gap-2">
        <input
          type="text"
          value={arxivId}
          onChange={(e) => setArxivId(e.target.value)}
          placeholder="输入中心论文 arXiv ID"
          className="flex-1 rounded border border-slate-700 bg-white p-3 text-slate-900"
          onKeyDown={(e) => e.key === 'Enter' && buildGraph()}
        />
        <button
          onClick={buildGraph}
          disabled={loading}
          className="rounded bg-primary px-6 py-2 font-medium text-white hover:bg-blue-700"
        >
          {loading ? '构建中...' : '构建'}
        </button>
      </div>

      {graph && <GraphView graph={graph} />}
    </div>
  );
}

function GraphView({ graph }: { graph: GraphData }) {
  const width = 900;
  const height = 560;
  const cx = width / 2;
  const cy = height / 2;
  const radius = 220;
  const n = graph.nodes.length || 1;

  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; node: Node } | null>(null);
  const dragStart = useRef({ x: 0, y: 0 });

  const positions = new Map<string, { x: number; y: number }>();
  graph.nodes.forEach((node, idx) => {
    if (node.group === 'seed') {
      positions.set(node.id, { x: cx, y: cy });
    } else {
      const angle = (idx / n) * Math.PI * 2;
      positions.set(node.id, {
        x: cx + radius * Math.cos(angle),
        y: cy + radius * Math.sin(angle),
      });
    }
  });

  const handleWheel = (e: WheelEvent<SVGSVGElement>) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    setScale((s) => Math.min(Math.max(s * delta, 0.3), 4));
  };

  const handleMouseDown = (e: MouseEvent<SVGSVGElement>) => {
    setDragging(true);
    dragStart.current = { x: e.clientX - offset.x, y: e.clientY - offset.y };
  };

  const handleMouseMove = (e: MouseEvent<SVGSVGElement>) => {
    if (dragging) {
      setOffset({ x: e.clientX - dragStart.current.x, y: e.clientY - dragStart.current.y });
    }
  };

  const handleMouseUp = () => setDragging(false);

  return (
    <div className="relative overflow-hidden rounded-xl border border-slate-800 bg-slate-900/50 p-4">
      <svg
        width={width}
        height={height}
        className={`mx-auto ${dragging ? 'cursor-grabbing' : 'cursor-grab'}`}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
      >
        <rect width={width} height={height} fill="transparent" />
        <g transform={`translate(${offset.x}, ${offset.y}) scale(${scale})`}>
          {graph.edges.map((e, i) => {
            const s = positions.get(e.source);
            const t = positions.get(e.target);
            if (!s || !t) return null;
            return (
              <line
                key={i}
                x1={s.x}
                y1={s.y}
                x2={t.x}
                y2={t.y}
                stroke={e.is_perfunctory ? '#475569' : '#60a5fa'}
                strokeWidth={e.is_perfunctory ? 1 : 1 + e.weight * 3}
                strokeDasharray={e.is_perfunctory ? '4 2' : undefined}
              />
            );
          })}
          {graph.nodes.map((node) => {
            const p = positions.get(node.id);
            if (!p) return null;
            const r = node.group === 'seed' ? 16 : 8 + node.influence_score * 2;
            return (
              <g key={node.id}>
                <a href={node.source_url} target="_blank" rel="noreferrer">
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r={r}
                    fill={node.group === 'seed' ? '#2563eb' : '#10b981'}
                    className="cursor-pointer hover:stroke-white hover:stroke-2"
                    onMouseEnter={(e) =>
                      setTooltip({ x: e.clientX + 12, y: e.clientY - 24, node })
                    }
                    onMouseLeave={() => setTooltip(null)}
                  />
                </a>
                <text x={p.x + r + 6} y={p.y + 4} fill="#e2e8f0" fontSize={12}>
                  {node.label}
                </text>
              </g>
            );
          })}
        </g>
      </svg>

      <div className="mt-2 flex items-center justify-between text-xs text-muted">
        <span>节点大小 = influence_score，边粗细 = 引用含金量；点击节点打开原文 PDF。</span>
        <span>滚轮缩放 · 拖拽平移</span>
      </div>

      {tooltip && (
        <div
          className="pointer-events-none fixed z-40 rounded border border-slate-700 bg-slate-800 p-2 text-xs text-slate-200 shadow-lg"
          style={{ left: tooltip.x, top: tooltip.y }}
        >
          <div className="font-semibold">{tooltip.node.title}</div>
          <div>arXiv:{tooltip.node.arxiv_id}</div>
          <div>影响力: {tooltip.node.influence_score.toFixed(3)}</div>
          {tooltip.node.influential_citation_count > 0 && (
            <div>高影响力引用: {tooltip.node.influential_citation_count}</div>
          )}
        </div>
      )}
    </div>
  );
}
