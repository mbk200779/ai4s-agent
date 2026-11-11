/**
 * 库内引用图的分层静态布局。
 *
 * 为什么不用力导向：引用图是有向无环的，力导向会让"谁引谁"的方向信息被
 * 揉成一团；分层布局把引用者放上面、被引者放下面，读起来就是"往下是前提"。
 * 另一个好处是确定性——同一份数据每次渲染坐标一样，截图和结论才对得上，
 * 而且 20 个节点是纯 O(V·E) 的松弛，毫秒级，不需要"降级"这一步。
 */
import type { LibraryEdge, LibraryNode } from './libraryTypes';

export interface PlacedNode {
  id: string;
  x: number;
  y: number;
  r: number;
  level: number;
}

export interface LayoutResult {
  positions: Map<string, PlacedNode>;
  levels: Map<string, number>;
  bounds: { minX: number; minY: number; maxX: number; maxY: number };
  layoutMs: number;
}

const PAD_X = 112;
const PAD_TOP = 56;
const PAD_BOTTOM = 64;

function radiusOf(node: LibraryNode, maxPr: number): number {
  if (node.kind === 'gap') {
    return 10 + Math.min(node.metrics.in_degree, 4) * 1.8;
  }
  const pr = node.metrics.pagerank ?? 0;
  const t = maxPr > 0 ? Math.sqrt(pr / maxPr) : 0;
  return 13 + t * 14;
}

/**
 * 层号 = 到"最上游引用者"的最长路径长度。用最长路径而不是最短路径，
 * 是为了让"A 被 B 和 C 引用，C 又被 B 引用"时 A 落在 B 的下面而不是并排。
 * 引用图理论上无环，仍然限制迭代次数，防止脏数据把布局拖死。
 */
function assignLevels(nodes: LibraryNode[], edges: LibraryEdge[]): Map<string, number> {
  const level = new Map<string, number>();
  nodes.forEach((n) => level.set(n.id, 0));
  const usable = edges.filter((e) => level.has(e.source) && level.has(e.target));
  for (let i = 0; i < nodes.length; i += 1) {
    let changed = false;
    for (const e of usable) {
      const next = (level.get(e.source) ?? 0) + 1;
      if (next > (level.get(e.target) ?? 0) && next < nodes.length) {
        level.set(e.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const min = Math.min(...Array.from(level.values()));
  if (min !== 0) level.forEach((v, k) => level.set(k, v - min));
  return level;
}

/** 层数。画布高度需要它，而高度又反过来是布局的输入，所以单独暴露一个不依赖尺寸的入口。 */
export function rowCount(nodes: LibraryNode[], edges: LibraryEdge[]): number {
  if (!nodes.length) return 1;
  return new Set(assignLevels(nodes, edges).values()).size || 1;
}

export function computeLayout(
  nodes: LibraryNode[],
  edges: LibraryEdge[],
  width: number,
  height: number,
): LayoutResult {
  const t0 = performance.now();
  const level = assignLevels(nodes, edges);
  const maxPr = nodes.reduce((m, n) => Math.max(m, n.metrics.pagerank ?? 0), 0);

  const rows = new Map<number, LibraryNode[]>();
  nodes.forEach((n) => {
    const lv = level.get(n.id) ?? 0;
    if (!rows.has(lv)) rows.set(lv, []);
    rows.get(lv)!.push(n);
  });

  // 层内排序用重心的两趟扫描（Sugiyama 的标准做法）：
  // 先自上而下按父节点平均位置排，再自下而上按子节点平均位置排。
  // 只做第一趟的话，最上面那层没有父节点、只能按标题字母序排，
  // 于是 ViT 可能被排到 LoRA 左边，跨整幅图拉一条对角线。
  const parents = new Map<string, string[]>();
  const children = new Map<string, string[]>();
  edges.forEach((e) => {
    if (!parents.has(e.target)) parents.set(e.target, []);
    parents.get(e.target)!.push(e.source);
    if (!children.has(e.source)) children.set(e.source, []);
    children.get(e.source)!.push(e.target);
  });

  const orderIndex = new Map<string, number>();
  const sortedLevels = Array.from(rows.keys()).sort((a, b) => a - b);
  sortedLevels.forEach((lv) => {
    rows.get(lv)!.sort((a, b) => a.title.localeCompare(b.title));
    rows.get(lv)!.forEach((n, i) => orderIndex.set(n.id, i));
  });

  const sortBy = (lv: number, link: Map<string, string[]>) => {
    const row = rows.get(lv)!;
    const bary = (n: LibraryNode) => {
      const ns = link.get(n.id) ?? [];
      const idx = ns.map((p) => orderIndex.get(p)).filter((v): v is number => v !== undefined);
      return idx.length ? idx.reduce((x, y) => x + y, 0) / idx.length : 999;
    };
    row.sort((a, b) => {
      const d = bary(a) - bary(b);
      if (Math.abs(d) > 1e-6) return d;
      return a.title.localeCompare(b.title);
    });
    row.forEach((n, i) => orderIndex.set(n.id, i));
  };

  sortedLevels.forEach((lv, i) => {
    if (i > 0) sortBy(lv, parents);
  });
  [...sortedLevels].reverse().forEach((lv, i) => {
    if (i > 0) sortBy(lv, children);
  });

  const levelCount = Math.max(sortedLevels.length, 1);
  const rowGap = levelCount > 1 ? (height - PAD_TOP - PAD_BOTTOM) / (levelCount - 1) : 0;

  const positions = new Map<string, PlacedNode>();
  sortedLevels.forEach((lv, li) => {
    const row = rows.get(lv)!;
    const usable = width - PAD_X * 2;
    const step = row.length > 1 ? usable / (row.length - 1) : 0;
    const startX = row.length > 1 ? PAD_X : width / 2;
    const y = PAD_TOP + (levelCount > 1 ? rowGap * li : (height - PAD_TOP - PAD_BOTTOM) / 2);
    row.forEach((n, i) => {
      positions.set(n.id, {
        id: n.id,
        x: startX + step * i,
        y,
        r: radiusOf(n, maxPr),
        level: lv,
      });
    });
  });

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  positions.forEach((p) => {
    minX = Math.min(minX, p.x - p.r);
    minY = Math.min(minY, p.y - p.r);
    maxX = Math.max(maxX, p.x + p.r);
    maxY = Math.max(maxY, p.y + p.r);
  });
  if (!Number.isFinite(minX)) {
    minX = 0;
    minY = 0;
    maxX = width;
    maxY = height;
  }

  return {
    positions,
    levels: level,
    bounds: { minX, minY, maxX, maxY },
    layoutMs: performance.now() - t0,
  };
}
