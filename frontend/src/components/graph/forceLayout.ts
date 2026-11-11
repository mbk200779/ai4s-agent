/**
 * 引用图谱的力导向布局。
 *
 * 参考主流做法：
 * - Connected Papers / d3-force：弹簧 + 斥力 + 向心力，让相关节点自然聚拢；
 * - Cytoscape.js：布局算完即冻结位置，后续交互不再触发重排，避免拖拽时整图跳动。
 *
 * 因此这里用「同步迭代 + 布局冻结」：一次性算出稳定坐标，交互只改变换矩阵。
 */

export interface SimNodeInput {
  id: string;
  radius: number;
  fixed?: boolean;
}

export interface SimEdgeInput {
  source: string;
  target: string;
  weight: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface LayoutResult {
  positions: Map<string, Point>;
  bounds: { minX: number; minY: number; maxX: number; maxY: number };
}

const ITERATIONS = 420;

/** 由 id 派生的确定性抖动，避免每次刷新布局抖动，也避免用随机数导致截图不稳定 */
function jitter(id: string, seed: number): number {
  let h = seed;
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) % 100000;
  return ((h % 1000) / 1000 - 0.5) * 2;
}

/**
 * 计算布局。坐标原点为画布左上角，调用方通过 fit 变换映射到屏幕。
 */
export function computeLayout(
  nodes: SimNodeInput[],
  edges: SimEdgeInput[],
  width: number,
  height: number,
): LayoutResult {
  const cx = width / 2;
  const cy = height / 2;

  const sim = new Map<
    string,
    { x: number; y: number; r: number; fixed: boolean; vx: number; vy: number }
  >();

  const children = nodes.filter((n) => !n.fixed);
  const ringRadius = Math.min(width, height) * 0.3;

  nodes.forEach((n) => {
    if (n.fixed) {
      sim.set(n.id, { x: cx, y: cy, r: n.radius, fixed: true, vx: 0, vy: 0 });
      return;
    }
    const idx = children.findIndex((c) => c.id === n.id);
    const angle = (idx / Math.max(children.length, 1)) * Math.PI * 2 - Math.PI / 2;
    const rad = ringRadius * (0.82 + Math.abs(jitter(n.id, 7)) * 0.36);
    sim.set(n.id, {
      x: cx + Math.cos(angle) * rad + jitter(n.id, 3) * 10,
      y: cy + Math.sin(angle) * rad + jitter(n.id, 11) * 10,
      r: n.radius,
      fixed: false,
      vx: 0,
      vy: 0,
    });
  });

  const ids = Array.from(sim.keys());

  for (let it = 0; it < ITERATIONS; it += 1) {
    const alpha = 1 - it / ITERATIONS;

    // 1) 弹簧：引用含金量越高，静止长度越短 —— 强引用自然靠向中心论文
    for (const e of edges) {
      const a = sim.get(e.source);
      const b = sim.get(e.target);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      // 引用含金量越高，静止长度越短 —— 强引用贴着中心，弱引用被推远
      const rest = 104 + (1 - e.weight) * 70 + (a.r + b.r) * 1.3;
      const step = ((dist - rest) / dist) * 0.055 * alpha;
      const ox = dx * step;
      const oy = dy * step;
      if (!a.fixed) {
        a.x += ox;
        a.y += oy;
      }
      if (!b.fixed) {
        b.x -= ox;
        b.y -= oy;
      }
    }

    // 2) 碰撞：给标签留出空间，消除节点重叠
    for (let i = 0; i < ids.length; i += 1) {
      const a = sim.get(ids[i])!;
      for (let j = i + 1; j < ids.length; j += 1) {
        const b = sim.get(ids[j])!;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.hypot(dx, dy) || 0.01;
        const minGap = a.r + b.r + 26;
        if (dist >= minGap) continue;
        const push = ((minGap - dist) / dist) * 0.5 * (0.65 + 0.35 * alpha);
        const ox = dx * push;
        const oy = dy * push;
        if (!a.fixed) {
          a.x -= ox;
          a.y -= oy;
        }
        if (!b.fixed) {
          b.x += ox;
          b.y += oy;
        }
      }
    }

    // 3) 向心：防止松散节点漂出画布
    sim.forEach((n) => {
      if (n.fixed) return;
      n.x += (cx - n.x) * 0.014 * alpha;
      n.y += (cy - n.y) * 0.014 * alpha;
    });
  }

  const positions = new Map<string, Point>();
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  sim.forEach((n, id) => {
    positions.set(id, { x: n.x, y: n.y });
    minX = Math.min(minX, n.x - n.r);
    minY = Math.min(minY, n.y - n.r);
    maxX = Math.max(maxX, n.x + n.r);
    maxY = Math.max(maxY, n.y + n.r);
  });

  if (!Number.isFinite(minX)) {
    minX = 0;
    minY = 0;
    maxX = width;
    maxY = height;
  }

  return { positions, bounds: { minX, minY, maxX, maxY } };
}
