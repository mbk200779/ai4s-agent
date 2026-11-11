'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { computeLayout, rowCount } from './libraryLayout';
import type { LibraryEdge, LibraryGraphPayload, LibraryNode } from './libraryTypes';

const MIN_K = 0.4;
const MAX_K = 3;

const PALETTE: Record<string, { fill: string; stroke: string; ink: string; dash?: string }> = {
  cornerstone: { fill: 'var(--signal)', stroke: 'var(--signal-hover)', ink: '#ffffff' },
  bridge: { fill: '#f59e0b', stroke: '#b45309', ink: '#ffffff' },
  derivative: { fill: '#cbd5e1', stroke: '#94a3b8', ink: 'var(--ink)' },
  cited_but_peripheral: { fill: '#e2e8f0', stroke: '#94a3b8', ink: 'var(--ink)' },
  normal: { fill: '#5eead4', stroke: 'var(--signal)', ink: 'var(--ink)' },
  isolated: { fill: '#f1f5f9', stroke: '#cbd5e1', ink: 'var(--ink-3)', dash: '3 3' },
  gap: { fill: '#f5f3ff', stroke: '#7c3aed', ink: '#5b21b6', dash: '4 3' },
};

const CJK = /[\u2e80-\u9fff\uff00-\uffef\u3000-\u303f]/;

/** 按显示宽度截断（CJK 记 2，西文记 1），避免英文标题占满行、中文标题反而被切太早 */
function clip(text: string, max: number): string {
  let w = 0;
  let out = '';
  for (const ch of text) {
    const cw = CJK.test(ch) ? 2 : 1;
    if (w + cw > max) return `${out.trimEnd()}…`;
    out += ch;
    w += cw;
  }
  return out;
}

function wrapTwo(text: string, max: number): string[] {
  const one = clip(text, max);
  if (one.length >= text.length) return [one];
  // 按词断开，不在单词中间切。原来按半个字数硬切，切出过
  // `BERT: Pre-trainin…` / `g of Deep Bi…` 这种断法——看起来像乱码。
  // 标题里最稳的切点是空格；找不到合适空格（长 URL、化学式）才退回硬切。
  const cut = text.slice(0, max);
  const sp = cut.lastIndexOf(' ');
  const head = sp > max * 0.45 ? text.slice(0, sp) : cut;
  const tail = text.slice(head.length).trim();
  return tail ? [head.trimEnd(), clip(tail, max)] : [clip(head, max)];
}

const btn =
  'inline-flex h-7 items-center justify-center rounded-md border border-[var(--line)] ' +
  'bg-[var(--surface)] px-2 text-[11px] text-[var(--ink-2)] hover:bg-[var(--surface-3)]';

export default function LibraryGraph({
  payload,
  selected,
  onSelect,
}: {
  payload: LibraryGraphPayload;
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [cw, setCw] = useState(900);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [hovered, setHovered] = useState<string | null>(null);
  const [firstPaint, setFirstPaint] = useState<number | null>(null);
  const mountAt = useRef<number>(performance.now());

  const nodes = payload.nodes;
  const edges = payload.edges;

  // 画布高度按"层数"给，不按宽度比例拍：每层要放一个节点（直径 ~44）加两条标签带
  // （上下各两行、各约 30），固定 0.54×宽 在 4 层的图上会把标签挤到相邻层里去。
  const rows = useMemo(() => rowCount(nodes, edges), [nodes, edges]);
  const size = useMemo(
    () => ({ w: cw, h: Math.round(Math.min(Math.max(rows * 108 + 104, 360), 580)) }),
    [cw, rows],
  );

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = (w: number) => {
      if (w > 0) setCw(w);
    };
    update(el.clientWidth);
    const ro = new ResizeObserver((entries) => update(entries[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layout = useMemo(
    () => computeLayout(nodes, edges, size.w, size.h),
    [nodes, edges, size.w, size.h],
  );

  // 每个节点在自己那一层里、从左到右数第几个。标签据此交替上下。
  const labelRow = useMemo(() => {
    const byLevel = new Map<number, { id: string; x: number }[]>();
    nodes.forEach((n) => {
      const p = layout.positions.get(n.id);
      if (!p) return;
      const lv = layout.levels.get(n.id) ?? 0;
      if (!byLevel.has(lv)) byLevel.set(lv, []);
      byLevel.get(lv)!.push({ id: n.id, x: p.x });
    });
    const out = new Map<string, number>();
    byLevel.forEach((arr) => {
      arr.sort((a, b) => a.x - b.x).forEach((v, i) => out.set(v.id, i));
    });
    return out;
  }, [nodes, layout]);

  const fit = useCallback(
    (w: number, h: number) => {
      const { minX, minY, maxX, maxY } = layout.bounds;
      const bw = Math.max(maxX - minX, 1);
      const bh = Math.max(maxY - minY, 1);
      const k = Math.min(Math.max(Math.min((w - 96) / bw, (h - 108) / bh), 0.55), 1.3);
      setView({ k, x: w / 2 - ((minX + maxX) / 2) * k, y: h / 2 - ((minY + maxY) / 2) * k });
    },
    [layout.bounds],
  );

  useEffect(() => {
    fit(size.w, size.h);
  }, [fit, size.w, size.h]);

  // 首帧耗时：从挂载到布局完成后的第一次绘制。用来证伪"20 节点要 3 秒"这个担心。
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      setFirstPaint(Math.round(performance.now() - mountAt.current));
    });
    return () => cancelAnimationFrame(id);
  }, [layout]);

  const zoomAt = useCallback((px: number, py: number, factor: number) => {
    setView((v) => {
      const k = Math.min(Math.max(v.k * factor, MIN_K), MAX_K);
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
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY > 0 ? 0.9 : 1.11);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoomAt]);

  // 拖拽状态。`hitId` 记住按下时命中的节点——不能靠节点自己的 onClick 来选中：
  // 下面 onDown 会 setPointerCapture 到 <svg>，之后 pointerup/click 都会被重定向到
  // svg 上，节点的 click 永远不触发（表现就是"点节点没反应"）。所以选中必须在
  // onUp 里按按下时的命中结果来判，且只有"没拖动"才算点击。
  const drag = useRef<{
    sx: number;
    sy: number;
    vx: number;
    vy: number;
    moved: boolean;
    hitId: string | null;
  } | null>(null);

  const onDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    const hit = (e.target as Element | null)?.closest?.('[data-node-id]');
    drag.current = {
      sx: e.clientX,
      sy: e.clientY,
      vx: view.x,
      vy: view.y,
      moved: false,
      hitId: hit?.getAttribute('data-node-id') ?? null,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (Math.hypot(dx, dy) > 3) d.moved = true;
    setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
  };
  const onUp = () => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.moved) return;
    if (d.hitId) onSelect(selected === d.hitId ? null : d.hitId);
    else onSelect(null);
  };

  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const active = hovered ?? selected;

  const neighbors = useMemo(() => {
    if (!active) return null;
    const set = new Set<string>([active]);
    edges.forEach((e) => {
      if (e.source === active) set.add(e.target);
      if (e.target === active) set.add(e.source);
    });
    return set;
  }, [active, edges]);

  const edgePath = useCallback(
    (e: LibraryEdge) => {
      const a = layout.positions.get(e.source);
      const b = layout.positions.get(e.target);
      if (!a || !b) return null;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;
      const sx = a.x + ux * (a.r + 2);
      const sy = a.y + uy * (a.r + 2);
      const ex = b.x - ux * (b.r + 8);
      const ey = b.y - uy * (b.r + 8);
      if (Math.abs(dy) < 1) return `M ${sx} ${sy} L ${ex} ${ey}`;
      const mx = (sx + ex) / 2;
      const my = (sy + ey) / 2;
      return `M ${sx.toFixed(1)} ${sy.toFixed(1)} Q ${mx.toFixed(1)} ${my.toFixed(1)} ${ex.toFixed(1)} ${ey.toFixed(1)}`;
    },
    [layout.positions],
  );

  const selectedNode: LibraryNode | null = selected ? byId.get(selected) ?? null : null;

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-[var(--line)] px-4 py-2.5">
        <span className="mono text-[11px] tracking-[0.16em] text-[var(--ink-3)]">
          FIG.03 · LIBRARY CITATION GRAPH
        </span>
        <span className="mono text-[11px] text-[var(--ink-3)]">
          {nodes.length} 节点 · {edges.length} 边 · 布局 {layout.layoutMs.toFixed(1)}ms
          {firstPaint !== null && ` · 首帧 ${firstPaint}ms`}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <button type="button" className={btn} onClick={() => zoomAt(size.w / 2, size.h / 2, 0.85)}>
            −
          </button>
          <button type="button" className={btn} onClick={() => zoomAt(size.w / 2, size.h / 2, 1.18)}>
            +
          </button>
          <button type="button" className={btn} onClick={() => fit(size.w, size.h)}>
            适应视图
          </button>
        </div>
      </div>

      <div ref={wrapRef} className="relative w-full bg-[var(--surface-2)]">
        <svg
          ref={svgRef}
          width="100%"
          height={size.h}
          viewBox={`0 0 ${size.w} ${size.h}`}
          className="block cursor-grab touch-none select-none active:cursor-grabbing"
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerCancel={onUp}
          role="img"
          aria-label="库内引用关系图"
        >
          <defs>
            <pattern id="lg-dots" width="20" height="20" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="0.9" fill="var(--line)" />
            </pattern>
            <marker id="lg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6.5" markerHeight="6.5" orient="auto" markerUnits="userSpaceOnUse">
              <path d="M0,1 L7,4 L0,7 Z" fill="var(--signal)" />
            </marker>
            <marker id="lg-arrow-gap" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6.5" markerHeight="6.5" orient="auto" markerUnits="userSpaceOnUse">
              <path d="M0,1 L7,4 L0,7 Z" fill="#7c3aed" />
            </marker>
            <marker id="lg-arrow-muted" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto" markerUnits="userSpaceOnUse">
              <path d="M0,1 L7,4 L0,7 Z" fill="var(--line-strong)" />
            </marker>
          </defs>

          <rect width={size.w} height={size.h} fill="url(#lg-dots)" />

          <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
            {edges.map((e, i) => {
              const d = edgePath(e);
              if (!d) return null;
              const isGap = e.relation === 'cites_gap';
              const touches = !active || e.source === active || e.target === active;
              return (
                <path
                  key={`${e.source}-${e.target}-${i}`}
                  d={d}
                  fill="none"
                  stroke={isGap ? '#7c3aed' : 'var(--signal)'}
                  strokeWidth={isGap ? 0.9 : 1 + Math.log2(1 + e.weight) * 0.9}
                  strokeDasharray={isGap ? '4 3' : undefined}
                  opacity={touches ? (active ? 0.95 : 0.55) : 0.1}
                  markerEnd={isGap ? 'url(#lg-arrow-gap)' : 'url(#lg-arrow)'}
                />
              );
            })}

            {nodes.map((n) => {
              const p = layout.positions.get(n.id);
              if (!p) return null;
              const pal = PALETTE[n.label.key] ?? PALETTE.normal;
              const isActive = active === n.id;
              const isSel = selected === n.id;
              const dim = neighbors ? !neighbors.has(n.id) : false;
              // 同层节点交替把标签放到上方/下方：五个缺口排在一行时，
              // 标签宽度会超过间距，错开一条带是最省事也最稳的解法。
              // 顺序必须按**布局后的 x 从左到右**算，不能按 payload 里 nodes 的原顺序——
              // 后者与画出来的左右次序无关，会导致相邻两个都在同一侧、标签直接叠上。
              const rowIndex = labelRow.get(n.id) ?? 0;
              const above = rowIndex % 2 === 1;
              const lines = wrapTwo(n.title, n.kind === 'gap' ? 30 : 34);
              const fs = n.kind === 'gap' ? 10 : 11;
              const labelY = above
                ? p.y - p.r - 10 - (lines.length - 1) * (fs + 2.5)
                : p.y + p.r + 10;

              return (
                <g
                  key={n.id}
                  data-node-id={n.id}
                  opacity={dim ? 0.25 : 1}
                  className="cursor-pointer"
                  onPointerEnter={() => setHovered(n.id)}
                  onPointerLeave={() => setHovered((h) => (h === n.id ? null : h))}
                >
                  <title>{`${n.title}\n${n.label.label} · ${n.sentence}`}</title>
                  {(isActive || isSel) && (
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={p.r + 5}
                      fill="none"
                      stroke={pal.stroke}
                      strokeWidth={1.1}
                      strokeDasharray="2 3"
                    />
                  )}
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r={p.r}
                    fill={pal.fill}
                    stroke={pal.stroke}
                    strokeWidth={n.label.key === 'cornerstone' ? 2 : 1.4}
                    strokeDasharray={pal.dash}
                  />
                  <g className="pointer-events-none">
                    {lines.map((line, i) => (
                      <text
                        key={i}
                        x={p.x}
                        y={labelY + i * (fs + 2.5)}
                        textAnchor="middle"
                        fontSize={fs}
                        fontWeight={isActive || isSel ? 600 : 400}
                        fill={n.kind === 'gap' ? '#5b21b6' : 'var(--ink-2)'}
                        stroke="var(--surface-2)"
                        strokeWidth={2.6}
                        paintOrder="stroke"
                        dominantBaseline="hanging"
                      >
                        {line}
                      </text>
                    ))}
                  </g>
                </g>
              );
            })}
          </g>
        </svg>

        <div className="pointer-events-none absolute bottom-1.5 left-3 mono text-[10px] text-[var(--ink-3)]">
          箭头指向被引论文 · 上排是引用者，下排是前提 · 滚轮缩放 / 拖拽平移 · 点击节点看依据
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-[var(--line)] px-4 py-2 text-[11px] text-[var(--ink-3)]">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-[var(--signal)]" />基石论文
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-[#f59e0b]" />桥梁论文
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-[#cbd5e1]" />边缘衍生
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-full border border-dashed border-[#7c3aed] bg-[#f5f3ff]" />
          库外缺口（未入库但被引用）
        </span>
        <span>节点大小 = PageRank · 边粗 = 正文提及次数</span>
      </div>
    </div>
  );
}
