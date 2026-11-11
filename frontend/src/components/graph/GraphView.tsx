'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { computeLayout, type Point } from './forceLayout';
import type { GraphData, GraphEdgeData, GraphNodeData } from './types';

const MIN_K = 0.35;
const MAX_K = 3.2;

function clamp(v: number, lo: number, hi: number) {
  return Math.min(Math.max(v, lo), hi);
}

// ---------- 标签排版：按像素宽度计算文本尺寸 ----------
// SVG 不会自动换行，只能自己在渲染前把标题量好、截好。
// 这里按字符类别估算字宽（CJK 全宽 / 小写窄 / 大写与数字中宽），
// 比 len * 常数 更准，中英混排的标题也不会量歪。
const CJK_RE =
  /[\u1100-\u11FF\u2E80-\u9FFF\u3000-\u303F\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFFEF]/;

function glyphWidth(ch: string, fs: number) {
  if (CJK_RE.test(ch)) return fs * 1.0;
  if (ch === ' ') return fs * 0.28;
  if (/[ilIjtfr.,:;'`|!()\[\]]/.test(ch)) return fs * 0.33;
  if (/[mwMW]/.test(ch)) return fs * 0.87;
  if (/[A-Z0-9]/.test(ch)) return fs * 0.62;
  return fs * 0.55;
}

function textWidth(s: string, fs: number) {
  let w = 0;
  for (const ch of s) w += glyphWidth(ch, fs);
  return w;
}

/** 单行截断：超出宽度就断，尽量落在词边界上，末尾补省略号 */
function truncateToWidth(s: string, maxW: number, fs: number) {
  if (maxW <= 0) return '';
  if (textWidth(s, fs) <= maxW) return s;

  const ell = glyphWidth('…', fs);
  let out = '';
  let w = 0;
  for (const ch of s) {
    const cw = glyphWidth(ch, fs);
    if (w + cw + ell > maxW) break;
    out += ch;
    w += cw;
  }
  // 断在半个单词上很难看，能在词边界收尾就在词边界收尾
  const sp = out.lastIndexOf(' ');
  if (sp > out.length * 0.6) out = out.slice(0, sp);
  return `${out.trimEnd().replace(/[,;:.\-–—]+$/, '')}…`;
}

/** 折成最多两行，第二行仍超宽则截断 */
function wrapTwoLines(s: string, maxW: number, fs: number): string[] {
  if (textWidth(s, fs) <= maxW) return [s];

  let out = '';
  let w = 0;
  for (const ch of s) {
    const cw = glyphWidth(ch, fs);
    if (w + cw > maxW) break;
    out += ch;
    w += cw;
  }
  let cut = out.length;
  const sp = out.lastIndexOf(' ');
  if (sp > out.length * 0.6) cut = sp;

  const line1 = out.slice(0, cut).trimEnd().replace(/[,;:.\-–—]+$/, '');
  const rest = s.slice(cut).trim();
  if (!rest) return [line1];
  return [line1, truncateToWidth(rest, maxW, fs)];
}

/** 标题清洗：压缩空白；后端拿不到标题时会回填 arxiv id，这里退化成 id 展示 */
function nodeTitle(n: GraphNodeData) {
  const t = (n.title || '').replace(/\s+/g, ' ').trim();
  if (!t || t === n.arxiv_id) return `arXiv:${n.arxiv_id}`;
  return t;
}

const ctlBtn =
  'inline-flex h-8 items-center justify-center rounded-md border border-[var(--line)] ' +
  'bg-[var(--surface)] px-2.5 text-xs text-[var(--ink-2)] transition-colors ' +
  'hover:border-[var(--line-strong)] hover:bg-[var(--surface-3)]';

export default function GraphView({ graph }: { graph: GraphData }) {
  const seedId = useMemo(
    () => graph.nodes.find((n) => n.group === 'seed')?.id ?? graph.nodes[0]?.id ?? '',
    [graph],
  );

  const [showPerfunctory, setShowPerfunctory] = useState(false);
  const [query, setQuery] = useState('');
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [size, setSize] = useState({ w: 880, h: 540 });

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);

  // ---------- 可见数据（对应 Obsidian / Bloom 的筛选能力）----------
  const visible = useMemo(() => {
    const edges = showPerfunctory
      ? graph.edges
      : graph.edges.filter((e) => !e.is_perfunctory);
    const keep = new Set<string>([seedId]);
    edges.forEach((e) => {
      keep.add(e.source);
      keep.add(e.target);
    });
    return { nodes: graph.nodes.filter((n) => keep.has(n.id)), edges };
  }, [graph, showPerfunctory, seedId]);

  const perfunctoryCount = useMemo(
    () => graph.edges.filter((e) => e.is_perfunctory).length,
    [graph],
  );

  // ---------- 视觉编码：节点半径 = 影响力（开方压缩，避免巨节点吃掉画布）----------
  const maxScore = useMemo(
    () => visible.nodes.reduce((m, n) => Math.max(m, n.influence_score), 0),
    [visible.nodes],
  );

  const radiusOf = useCallback(
    (n: GraphNodeData) => {
      if (n.id === seedId) return 19;
      const t = maxScore > 0 ? Math.sqrt(clamp(n.influence_score / maxScore, 0, 1)) : 0;
      return 7 + t * 12;
    },
    [maxScore, seedId],
  );

  const simNodes = useMemo(
    () =>
      visible.nodes.map((n) => ({
        id: n.id,
        radius: radiusOf(n),
        fixed: n.id === seedId,
      })),
    [visible.nodes, radiusOf, seedId],
  );

  const simEdges = useMemo(
    () => visible.edges.map((e) => ({ source: e.source, target: e.target, weight: e.weight })),
    [visible.edges],
  );

  // 布局一次算完并冻结（Cytoscape 的「布局冻结」做法），交互只改变换矩阵
  const layout = useMemo(
    () => computeLayout(simNodes, simEdges, size.w, size.h),
    [simNodes, simEdges, size.w, size.h],
  );

  const [positions, setPositions] = useState<Map<string, Point>>(layout.positions);

  const fitTo = useCallback(
    (bounds: { minX: number; minY: number; maxX: number; maxY: number }, w: number, h: number) => {
      const bw = Math.max(bounds.maxX - bounds.minX, 1);
      const bh = Math.max(bounds.maxY - bounds.minY, 1);
      const k = clamp(Math.min((w - 116) / bw, (h - 116) / bh), 0.45, 1.85);
      const mx = (bounds.minX + bounds.maxX) / 2;
      const my = (bounds.minY + bounds.maxY) / 2;
      setView({ k, x: w / 2 - mx * k, y: h / 2 - my * k });
    },
    [],
  );

  useEffect(() => {
    setPositions(layout.positions);
    fitTo(layout.bounds, size.w, size.h);
  }, [layout, fitTo, size.w, size.h]);

  // ---------- 自适应宽度 ----------
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = (w: number) => {
      if (w > 0) setSize({ w, h: Math.round(clamp(w * 0.6, 420, 620)) });
    };
    update(el.clientWidth);
    const ro = new ResizeObserver((entries) => update(entries[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ---------- 缩放：以光标为锚点（Cytoscape 的 zoom about renderedPosition）----------
  const zoomAt = useCallback((px: number, py: number, factor: number) => {
    setView((v) => {
      const k = clamp(v.k * factor, MIN_K, MAX_K);
      if (Math.abs(k - v.k) < 1e-4) return v;
      return { k, x: px - ((px - v.x) / v.k) * k, y: py - ((py - v.y) / v.k) * k };
    });
  }, []);

  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY > 0 ? 0.88 : 1.14);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoomAt]);

  // ---------- 拖拽：背景平移 / 节点拖动 ----------
  const dragRef = useRef<{
    mode: 'pan' | 'node';
    id?: string;
    sx: number;
    sy: number;
    vx: number;
    vy: number;
    k: number;
    nx: number;
    ny: number;
    moved: boolean;
  } | null>(null);

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    const nodeEl = (e.target as Element).closest('[data-node-id]');
    if (nodeEl) {
      const id = nodeEl.getAttribute('data-node-id') as string;
      const p = positions.get(id);
      if (!p) return;
      dragRef.current = {
        mode: 'node',
        id,
        sx: e.clientX,
        sy: e.clientY,
        vx: view.x,
        vy: view.y,
        k: view.k,
        nx: p.x,
        ny: p.y,
        moved: false,
      };
    } else {
      dragRef.current = {
        mode: 'pan',
        sx: e.clientX,
        sy: e.clientY,
        vx: view.x,
        vy: view.y,
        k: view.k,
        nx: 0,
        ny: 0,
        moved: false,
      };
    }
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (Math.hypot(dx, dy) > 3) d.moved = true;

    if (d.mode === 'pan') {
      setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
    } else if (d.id) {
      const id = d.id;
      setPositions((prev) => new Map(prev).set(id, { x: d.nx + dx / d.k, y: d.ny + dy / d.k }));
    }
  };

  const onPointerUp = () => {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d || d.moved) return;
    if (d.mode === 'node' && d.id) {
      setSelected((prev) => (prev === d.id ? null : (d.id as string)));
    } else {
      setSelected(null);
    }
  };

  // ---------- 邻域高亮（Cytoscape 的 faded neighborhood）----------
  const activeId = hovered ?? selected;

  const adjacency = useMemo(() => {
    const map = new Map<string, Set<string>>();
    const add = (a: string, b: string) => {
      if (!map.has(a)) map.set(a, new Set());
      map.get(a)!.add(b);
    };
    visible.edges.forEach((e) => {
      add(e.source, e.target);
      add(e.target, e.source);
    });
    return map;
  }, [visible.edges]);

  const degree = useMemo(() => {
    const m = new Map<string, number>();
    visible.edges.forEach((e) => {
      m.set(e.source, (m.get(e.source) ?? 0) + 1);
      m.set(e.target, (m.get(e.target) ?? 0) + 1);
    });
    return m;
  }, [visible.edges]);

  const neighbors = useMemo(() => {
    if (!activeId) return null;
    const s = new Set<string>([activeId]);
    adjacency.get(activeId)?.forEach((id) => s.add(id));
    return s;
  }, [activeId, adjacency]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return null;
    const hit = new Set<string>();
    visible.nodes.forEach((n) => {
      if (`${n.arxiv_id} ${n.label} ${n.title}`.toLowerCase().includes(q)) hit.add(n.id);
    });
    return hit;
  }, [query, visible.nodes]);

  const nodeById = useMemo(() => {
    const m = new Map<string, GraphNodeData>();
    visible.nodes.forEach((n) => m.set(n.id, n));
    return m;
  }, [visible.nodes]);

  const radiusById = useMemo(() => {
    const m = new Map<string, number>();
    visible.nodes.forEach((n) => m.set(n.id, radiusOf(n)));
    return m;
  }, [visible.nodes, radiusOf]);

  /**
   * 标签防重叠：按半径从大到小贪心占位，每个标签尝试 5 个纵向位置与 3 个间距，
   * 与已放置标签或任何节点圆相交就换下一个位置；都放不下则该标签本轮不显示。
   * 这是 Obsidian / Neo4j Bloom 那类「文字淡出阈值」的具体实现。
   */
  const labels = useMemo(() => {
    const seedPos = positions.get(seedId);
    const out = new Map<
      string,
      {
        dx: number;
        dy: number;
        anchor: 'start' | 'end' | 'middle';
        boxW: number;
        fs: number;
        lineH: number;
        h: number;
      }
    >();
    const occupied: { x1: number; y1: number; x2: number; y2: number }[] = [];

    const circles: { p: Point; r: number }[] = [];
    visible.nodes.forEach((n) => {
      const p = positions.get(n.id);
      if (p) circles.push({ p, r: radiusById.get(n.id) ?? 8 });
    });

    const collides = (rect: { x1: number; y1: number; x2: number; y2: number }) => {
      if (
        occupied.some(
          (o) => !(rect.x2 < o.x1 || rect.x1 > o.x2 || rect.y2 < o.y1 || rect.y1 > o.y2),
        )
      ) {
        return true;
      }
      return circles.some((c) => {
        const nx = Math.max(rect.x1, Math.min(c.p.x, rect.x2));
        const ny = Math.max(rect.y1, Math.min(c.p.y, rect.y2));
        return Math.hypot(nx - c.p.x, ny - c.p.y) < c.r + 1;
      });
    };

    const ordered = [...visible.nodes].sort(
      (a, b) => (radiusById.get(b.id) ?? 0) - (radiusById.get(a.id) ?? 0),
    );

    ordered.forEach((n) => {
      const p = positions.get(n.id);
      if (!p) return;
      const r = radiusById.get(n.id) ?? 8;
      const isSeed = n.id === seedId;

      // 中心论文给两行的宽度，其余节点单行、宽度更紧，避免长标题互相打架
      const fs = isSeed ? 12.5 : 11;
      const boxW = isSeed ? 236 : 142;
      const title = nodeTitle(n);
      const lines = isSeed ? wrapTwoLines(title, boxW, fs) : [truncateToWidth(title, boxW, fs)];
      const lineH = fs + 3.5;
      const h = lines.length * lineH + (isSeed ? 12 : 0); // 种子多一行 arXiv 副标
      const w = Math.min(boxW, Math.max(...lines.map((l) => textWidth(l, fs)))) + 2;

      const rectOf = (dx: number, dy: number, anchor: 'start' | 'end' | 'middle') => {
        const x1 =
          anchor === 'middle' ? p.x + dx - w / 2 : anchor === 'start' ? p.x + dx : p.x + dx - w;
        return { x1, y1: p.y + dy - h / 2, x2: x1 + w, y2: p.y + dy + h / 2 };
      };
      const commit = (dx: number, dy: number, anchor: 'start' | 'end' | 'middle') => {
        occupied.push(rectOf(dx, dy, anchor));
        out.set(n.id, { dx, dy, anchor, boxW, fs, lineH, h });
      };

      if (isSeed) {
        // 中心论文的标题必须出现：先试八个方向，实在挤不下就压正下方强制放置，
        // 后续标签再绕开它。
        const cands: [number, number, 'start' | 'end' | 'middle'][] = [
          [0, r + 30, 'middle'],
          [0, -(r + 32), 'middle'],
          [r + 14, 0, 'start'],
          [-(r + 14), 0, 'end'],
          [r + 12, -(r + 18), 'start'],
          [-(r + 12), -(r + 18), 'end'],
          [r + 12, r + 18, 'start'],
          [-(r + 12), r + 18, 'end'],
        ];
        const hit = cands.find(([dx, dy, anchor]) => !collides(rectOf(dx, dy, anchor)));
        commit(...(hit ?? cands[0]));
        return;
      }

      const outward = !seedPos || p.x >= seedPos.x;
      const anchor = outward ? 'start' : 'end';
      const step = Math.max(11, h * 0.85);
      const dys = [0, step, -step, step * 2, -step * 2];
      const pads = [r + 7, r + 13, r + 4];

      for (const dy of dys) {
        let done = false;
        for (const pad of pads) {
          const dx = outward ? pad : -pad;
          if (collides(rectOf(dx, dy, anchor))) continue;
          commit(dx, dy, anchor);
          done = true;
          break;
        }
        if (done) break;
      }
    });

    return out;
  }, [visible.nodes, positions, radiusById, seedId]);

  const selectedNode = selected ? nodeById.get(selected) ?? null : null;

  const edgePath = useCallback(
    (e: GraphEdgeData, index: number) => {
      const child = positions.get(e.target);
      const seed = positions.get(e.source);
      if (!child || !seed) return null;

      const rChild = radiusById.get(e.target) ?? 8;
      const rSeed = radiusById.get(e.source) ?? 12;
      const dx = seed.x - child.x;
      const dy = seed.y - child.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;

      // 两端各留出节点半径，箭头不被节点遮住
      const sx = child.x + ux * (rChild + 2);
      const sy = child.y + uy * (rChild + 2);
      const ex = seed.x - ux * (rSeed + 9);
      const ey = seed.y - uy * (rSeed + 9);

      // 轻微弧度，避免多条边完全重叠
      const off = (index % 2 === 0 ? 1 : -1) * Math.min(26, len * 0.16);
      const mx = (sx + ex) / 2 - uy * off;
      const my = (sy + ey) / 2 + ux * off;

      return `M ${sx.toFixed(2)} ${sy.toFixed(2)} Q ${mx.toFixed(2)} ${my.toFixed(2)} ${ex.toFixed(
        2,
      )} ${ey.toFixed(2)}`;
    },
    [positions, radiusById],
  );

  const labelVisible = (n: GraphNodeData, r: number) => {
    if (n.id === seedId || activeId === n.id) return true;
    if (matches?.has(n.id)) return true;
    if (view.k >= 1.25) return true;
    if (visible.nodes.length <= 9) return true;
    return r >= 13;
  };

  const empty = visible.nodes.length === 0;

  return (
    <div className="card card-tick overflow-hidden">
      {/* 工具条 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-[var(--line)] px-4 py-3">
        <span className="mono text-[11px] tracking-[0.16em] text-[var(--ink-3)]">
          FIG.02 · CITATION GRAPH
        </span>
        <span className="mono text-[11px] text-[var(--ink-3)]">
          {visible.nodes.length} 篇 · {visible.edges.length} 边
        </span>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索 arXiv / 标题"
            className="field h-8 w-44 py-0 text-xs"
            aria-label="搜索节点"
          />
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-xs text-[var(--ink-2)]">
            <input
              type="checkbox"
              checked={showPerfunctory}
              onChange={(e) => setShowPerfunctory(e.target.checked)}
              disabled={perfunctoryCount === 0}
              title={
                perfunctoryCount === 0
                  ? '本次结果中没有被判定为应付式的引用'
                  : `显示 ${perfunctoryCount} 条应付式引用`
              }
              className="h-3.5 w-3.5 accent-[var(--signal)] disabled:opacity-40"
            />
            应付式引用
            <span className="mono text-[var(--ink-3)]">({perfunctoryCount})</span>
          </label>
          <div className="flex items-center gap-1">
            <button
              type="button"
              className={ctlBtn}
              onClick={() => zoomAt(size.w / 2, size.h / 2, 0.84)}
              aria-label="缩小"
            >
              −
            </button>
            <button
              type="button"
              className={ctlBtn}
              onClick={() => zoomAt(size.w / 2, size.h / 2, 1.19)}
              aria-label="放大"
            >
              +
            </button>
            <button
              type="button"
              className={ctlBtn}
              onClick={() => fitTo(layout.bounds, size.w, size.h)}
            >
              适应视图
            </button>
          </div>
        </div>
      </div>

      {/* 画布 */}
      <div ref={wrapRef} className="relative w-full bg-[var(--surface-2)]">
        <svg
          ref={svgRef}
          width="100%"
          height={size.h}
          viewBox={`0 0 ${size.w} ${size.h}`}
          className="block cursor-grab touch-none select-none active:cursor-grabbing"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          role="img"
          aria-label="引用关系图谱"
        >
          <defs>
            <pattern id="gv-dots" width="22" height="22" patternUnits="userSpaceOnUse">
              <circle cx="1.2" cy="1.2" r="1.1" fill="var(--line)" />
            </pattern>
            <marker
              id="gv-arrow"
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto"
              markerUnits="userSpaceOnUse"
            >
              <path d="M0,1 L7,4 L0,7 Z" fill="var(--signal)" />
            </marker>
            <marker
              id="gv-arrow-muted"
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto"
              markerUnits="userSpaceOnUse"
            >
              <path d="M0,1 L7,4 L0,7 Z" fill="var(--line-strong)" />
            </marker>
          </defs>

          <rect width={size.w} height={size.h} fill="url(#gv-dots)" />

          <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
            {/* 连线 */}
            {visible.edges.map((e, i) => {
              const d = edgePath(e, i);
              if (!d) return null;
              const touchesActive = !activeId || e.source === activeId || e.target === activeId;
              const matchBoth =
                !matches ||
                (matches.has(e.source) && matches.has(e.target)) ||
                matches.has(e.source) ||
                matches.has(e.target);
              const opacity = touchesActive && matchBoth ? (activeId ? 1 : 0.85) : 0.12;
              return (
                <path
                  key={`${e.source}-${e.target}-${i}`}
                  d={d}
                  fill="none"
                  stroke={e.is_perfunctory ? 'var(--line-strong)' : 'var(--signal)'}
                  strokeWidth={e.is_perfunctory ? 0.9 : 0.9 + e.weight * 2.6}
                  strokeDasharray={e.is_perfunctory ? '4 3' : undefined}
                  opacity={opacity}
                  markerEnd={
                    e.is_perfunctory ? 'url(#gv-arrow-muted)' : 'url(#gv-arrow)'
                  }
                />
              );
            })}

            {/* 节点 */}
            {visible.nodes.map((n) => {
              const p = positions.get(n.id);
              if (!p) return null;
              const r = radiusById.get(n.id) ?? 8;
              const isSeed = n.id === seedId;
              const t = maxScore > 0 ? clamp(n.influence_score / maxScore, 0, 1) : 0;

              const dim =
                (matches && !matches.has(n.id)) || (neighbors && !neighbors.has(n.id));
              const isActive = activeId === n.id;
              const isSelected = selected === n.id;

              const lab = labelVisible(n, r) ? labels.get(n.id) ?? null : null;
              // 悬停 / 选中 / 搜索命中的节点临时展开成两行完整标题，
              // 位置沿用布局阶段算好的锚点，避免整个标签层重排抖动。
              const labEmph = isSeed || isActive || isSelected || (matches?.has(n.id) ?? false);
              const labTitle = nodeTitle(n);
              const labLines = lab
                ? labEmph
                  ? wrapTwoLines(labTitle, lab.boxW, lab.fs)
                  : [truncateToWidth(labTitle, lab.boxW, lab.fs)]
                : null;
              const labSub = lab && (isSeed || isActive || isSelected) ? `arXiv:${n.arxiv_id}` : null;
              const labH = lab && labLines ? labLines.length * lab.lineH + (labSub ? 12 : 0) : 0;
              const labTop = lab ? lab.dy - labH / 2 : 0;
              const labW = lab && labLines ? Math.max(...labLines.map((l) => textWidth(l, lab.fs))) : 0;
              const labLeft = lab
                ? lab.anchor === 'start'
                  ? lab.dx
                  : lab.anchor === 'end'
                    ? lab.dx - labW
                    : lab.dx - labW / 2
                : 0;

              const fill = isSeed
                ? 'var(--signal)'
                : `rgba(13, 148, 136, ${0.08 + 0.46 * t})`;
              const stroke = isSeed ? 'var(--signal-hover)' : 'rgba(13, 148, 136, 0.55)';

              return (
                <g
                  key={n.id}
                  data-node-id={n.id}
                  transform={`translate(${p.x} ${p.y})`}
                  opacity={dim ? 0.18 : 1}
                  className="cursor-pointer transition-opacity"
                  onPointerEnter={() => setHovered(n.id)}
                  onPointerLeave={() => setHovered((h) => (h === n.id ? null : h))}
                  onDoubleClick={() =>
                    window.open(n.source_url, '_blank', 'noopener,noreferrer')
                  }
                >
                  <title>{`${labTitle} · arXiv:${n.arxiv_id}`}</title>
                  {isSeed && <circle r={r + 6} fill="var(--signal-soft)" opacity={0.9} />}
                  {(isActive || isSelected) && (
                    <circle
                      r={r + 6}
                      fill="none"
                      stroke="var(--signal)"
                      strokeWidth={1.1}
                      strokeDasharray="2 3"
                      opacity={0.85}
                    />
                  )}
                  <circle
                    r={r}
                    fill={fill}
                    stroke={stroke}
                    strokeWidth={isSeed || isSelected ? 2 : 1.4}
                  />
                  {lab && labLines && (
                    <g className="pointer-events-none">
                      {/* 中心标题 / 展开中的标题用药丸背板托住：
                          力导向图中心四周全是汇聚的边，任何位置都会压线，
                          靠描边压不住，只能给一块纸色底。 */}
                      {(isSeed || labEmph) && (
                        <rect
                          x={labLeft - 7}
                          y={labTop - 5}
                          width={labW + 14}
                          height={labH + 6}
                          rx={Math.min(11, (labH + 6) / 2)}
                          fill="var(--surface)"
                          fillOpacity={0.94}
                          stroke="var(--line)"
                          strokeWidth={0.8}
                        />
                      )}
                      {labLines.map((line, i) => (
                        <text
                          key={i}
                          x={lab.dx}
                          y={labTop + lab.lineH * (i + 0.5)}
                          textAnchor={lab.anchor}
                          fontSize={lab.fs}
                          fontWeight={labEmph ? 600 : 400}
                          fill={isSeed ? 'var(--ink)' : 'var(--ink-2)'}
                          stroke="var(--surface-2)"
                          strokeWidth={isSeed ? 4 : 3.2}
                          paintOrder="stroke"
                          dominantBaseline="central"
                        >
                          {line}
                        </text>
                      ))}
                      {labSub && (
                        <text
                          x={lab.dx}
                          y={labTop + labLines.length * lab.lineH + 6}
                          textAnchor={lab.anchor}
                          fontSize={9}
                          fill="var(--ink-3)"
                          stroke="var(--surface-2)"
                          strokeWidth={2.5}
                          paintOrder="stroke"
                          dominantBaseline="central"
                          className="mono"
                        >
                          {labSub}
                        </text>
                      )}
                    </g>
                  )}
                </g>
              );
            })}
          </g>
        </svg>

        {empty && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-[var(--ink-3)]">
            没有可显示的节点
          </div>
        )}

        <div className="pointer-events-none absolute bottom-2 left-3 mono text-[10px] text-[var(--ink-3)]">
          滚轮缩放 · 拖拽平移 · 拖节点调整 · 双击打开原文
        </div>
      </div>

      {/* 详情 / 图例 */}
      {selectedNode ? (
        <div className="flex flex-wrap items-start gap-4 border-t border-[var(--line)] px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="mono text-[10px] tracking-[0.14em] text-[var(--ink-3)]">
              SELECTED · arXiv:{selectedNode.arxiv_id}
            </div>
            <div className="mt-1 truncate text-sm font-medium text-[var(--ink)]">
              {selectedNode.title}
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--ink-2)]">
              <span>
                影响力 <b className="mono text-[var(--ink)]">{selectedNode.influence_score.toFixed(2)}</b>
              </span>
              <span>
                被引 <b className="mono text-[var(--ink)]">{selectedNode.citation_count ?? selectedNode.influential_citation_count}</b>
              </span>
              <span>
                图内连边 <b className="mono text-[var(--ink)]">{degree.get(selectedNode.id) ?? 0}</b>
              </span>
              {selectedNode.id === seedId && (
                <span className="text-[var(--signal)]">中心论文</span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <a
              href={selectedNode.source_url}
              target="_blank"
              rel="noreferrer"
              className="btn-primary h-8 px-3 py-0 text-xs"
            >
              打开原文 PDF
            </a>
            <button type="button" className={ctlBtn} onClick={() => setSelected(null)}>
              取消选中
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-[var(--line)] px-4 py-2.5 text-[11px] text-[var(--ink-3)]">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-[var(--signal)]" />
            中心论文
          </span>
          <span className="flex items-center gap-1.5">
            节点大小 / 填充深浅 = 影响力
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-px w-5 bg-[var(--signal)]" />
            边粗细 = 引用含金量
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-px w-5 border-t border-dashed border-[var(--line-strong)]" />
            虚线 = 应付式引用
          </span>
          <span>箭头指向被引论文</span>
          <span>标签 = 论文标题，过长省略；悬停看全文与 arXiv 号</span>
        </div>
      )}
    </div>
  );
}
