'use client';

/**
 * 三类不确定性状态的视觉层（12）。
 *
 * 为什么单独成文件、而不是塞进 StreamAnswer：
 * 三类状态共用"标题行 + 说明 + 下一步动作"这套骨架，但各自的正文结构完全不同
 * （关键词命中表 / 左右并列 / 一条条残缺片段）。分头写必然出现
 * "空态有按钮、冲突没按钮"这种不一致；集中在这里，改一处就全改。
 *
 * 一条贯穿三类的规则：**每个卡片都必须有一个能点的下一步**。
 * 只解释不给出路的提示，本质上还是"抱歉，我无法回答"换了个排版。
 */

import { useState } from 'react';
import type {
  Conflict, EmptyState, UnreadableItem, Uncertainty,
} from '@/types/stream';
import ExternalRecommend from './ExternalRecommend';

/** 打开原文页。结构与 StreamAnswer 的 OpenRef 一致（这里不 import，避免循环依赖）。 */
export type OpenPageRef = (ref: {
  arxiv_id: string;
  title: string | null;
  page: number;
  bbox: number[] | null;
  rects: number[][] | null;
  precision: string | null;
  text: string | null;
  ref_id: string | null;
  match_score: number | null;
}) => void;

const AMBER = { fg: '#a16207', bg: '#fdf6e8', bd: '#e0b872' };
const VIOLET = { fg: '#6d28d9', bg: '#f4f1fd', bd: '#c3b4ee' };
const SLATE = { fg: '#475569', bg: '#f5f7f9', bd: '#cbd5e1' };
const RED = { fg: '#b42318', bg: '#fef3f2', bd: '#f0a9a2' };

function SectionHead({ label, right }: { label: string; right?: React.ReactNode }) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <span className="mono text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">{label}</span>
      <span className="h-px flex-1 bg-[var(--line)]" />
      {right}
    </div>
  );
}

/* ---------------------------------------------------------------------------
 * 1. 空检索
 * ------------------------------------------------------------------------- */

/**
 * 空检索卡片。
 *
 * 信息顺序是刻意排的：**先给范围，再给关键词，最后才给建议**。
 * 反过来会出现"你换个说法试试"这种感觉像在怪用户的提示——
 * 用户需要先看到"你们其实搜了 5 篇 279 段"，才会相信这句"没有"。
 */
export function EmptySearchCard({
  empty, onAsk, onOpenRef,
}: {
  empty: EmptyState;
  onAsk?: (q: string) => void;
  onOpenRef: OpenPageRef;
}) {
  const [showNearest, setShowNearest] = useState(false);
  const q = empty.query;
  const cov = q.coverage || {};
  const missed = q.terms.filter((t) => !t.hit);

  // 「换题」类建议的理由是同一句（这些论文在库内，所以问它们一定有答案）。
  // 三条建议下面重复印三遍同一行，看着就像模板生成的——所以共用的理由提到组上面说一次，
  // 每条只在自己有额外理由时才写。
  const sharedWhy =
    empty.suggestions.length > 1 &&
    empty.suggestions.every((s) => s.why === empty.suggestions[0].why)
      ? empty.suggestions[0].why
      : '';

  return (
    <section
      className="rounded-lg border bg-[var(--surface)] p-4"
      style={{ borderColor: SLATE.bd }}
    >
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span
          className="mono rounded border px-1.5 py-[1px] text-[11.5px] tracking-[0.12em]"
          style={{ color: SLATE.fg, background: SLATE.bg, borderColor: SLATE.bd }}
        >
          EMPTY · 没有可用的片段
        </span>
        <span className="text-[17px] font-semibold text-[var(--ink)]">{empty.title}</span>
      </div>
      <p className="mb-3 text-[14px] leading-relaxed text-[var(--ink-2)]">{empty.detail}</p>

      {/* 已检索范围：先证明"我们找过了" */}
      <div className="mb-3 grid grid-cols-2 gap-px overflow-hidden rounded border border-[var(--line)] bg-[var(--line)] sm:grid-cols-4">
        {[
          { k: '论文', v: empty.scope.papers },
          { k: '片段', v: empty.scope.chunks },
          { k: '检索类型', v: q.type || '-' },
          { k: '同段最多命中', v: `${cov.best_chunk_terms ?? 0} / ${q.terms.length}` },
        ].map((it) => (
          <div key={it.k} className="bg-[var(--surface-2)] px-2.5 py-1.5">
            <div className="mono text-[11px] tracking-[0.1em] text-[var(--ink-3)]">{it.k}</div>
            <div className="text-[14.5px] font-medium text-[var(--ink)]">{it.v}</div>
          </div>
        ))}
      </div>

      {/* 关键词命中表 */}
      {q.terms.length > 0 && (
        <div className="mb-3">
          <SectionHead
            label="检索关键词"
            right={
              <span className="mono text-[11.5px] text-[var(--ink-3)]">
                {missed.length} / {q.terms.length} 未命中
              </span>
            }
          />
          <div className="flex flex-wrap gap-1.5">
            {q.terms.map((t) => (
              <span
                key={t.term}
                title={t.hit ? `库内有 ${t.chunks} 段出现这个说法` : '库内没有任何一段出现这个说法'}
                className="mono inline-flex items-center gap-1 rounded border px-1.5 py-[2px] text-[12.5px]"
                style={
                  t.hit
                    ? { color: SLATE.fg, background: '#fff', borderColor: SLATE.bd }
                    : { color: RED.fg, background: RED.bg, borderColor: RED.bd }
                }
              >
                {t.term}
                <span className="text-[11.5px] opacity-70">{t.chunks} 段</span>
              </span>
            ))}
          </div>
          {q.translated && (
            <p className="mono mt-1.5 break-all text-[11.5px] leading-relaxed text-[var(--ink-3)]">
              实际检索文本：{q.search_text}
            </p>
          )}
        </div>
      )}

      {/* 下一步：改写建议 */}
      {empty.suggestions.length > 0 && (
        <div className="mb-2">
          <SectionHead label="换个问法" />
          {sharedWhy && (
            <p className="mb-1.5 text-[12.5px] leading-snug text-[var(--ink-3)]">{sharedWhy}</p>
          )}
          <div className="space-y-1.5">
            {empty.suggestions.map((s) => (
              <button
                key={s.id}
                type="button"
                disabled={!onAsk}
                onClick={() => onAsk?.(s.query)}
                className="group flex w-full items-start gap-2 rounded border border-[var(--line)] bg-[var(--surface-2)] px-2.5 py-2 text-left transition hover:border-[var(--signal)] disabled:cursor-default disabled:opacity-60"
              >
                <span className="mono mt-[1px] shrink-0 rounded border border-[var(--line-strong)] px-1 text-[11px] text-[var(--ink-3)]">
                  {s.kind === 'spelling' ? '改词' : s.kind === 'split' ? '拆句' : '换题'}
                </span>
                <span className="min-w-0">
                  {/* 主行是**点下去真的会提交的那一句**。只写"改问某篇"这种摘要，
                      用户点之前不知道要问什么，等于让他盲签一个字。 */}
                  <span className="block text-[14.5px] leading-snug text-[var(--ink)] group-hover:text-[var(--signal)]">
                    {s.query}
                  </span>
                  {s.why !== sharedWhy && (
                    <span className="mt-0.5 block text-[12.5px] leading-snug text-[var(--ink-3)]">
                      {s.why}
                    </span>
                  )}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 库外补货（13）：库里没有，就给一条真的能补上的路。
          放在"换个问法"之后、"最近邻"之前——先给库内的两条低成本出路，
          再给"把它补进库"这条能改变下一次结果的出路，最后才是看近邻这种纯解释。 */}
      <div className="mt-3 border-t border-[var(--line)] pt-3">
        <ExternalRecommend
          question={q.question || empty.title}
          searchText={q.search_text}
          onAsk={onAsk}
        />
      </div>

      {/* 最近邻：说清"系统看到了什么"，并明确它们不是证据 */}
      {empty.nearest.length > 0 && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowNearest((v) => !v)}
            className="mono text-[11.5px] text-[var(--ink-3)] hover:text-[var(--signal)]"
          >
            {showNearest ? '收起' : `看看最接近的 ${empty.nearest.length} 段（不是证据）`}
          </button>
          {showNearest && (
            <div className="mt-1.5 space-y-1">
              {empty.nearest.map((n) => (
                <button
                  key={n.chunk_id}
                  type="button"
                  onClick={() =>
                    onOpenRef({
                      arxiv_id: n.arxiv_id, title: n.title, page: n.page,
                      bbox: null, rects: null, precision: null, text: null,
                      ref_id: null, match_score: n.score,
                    })
                  }
                  className="block w-full rounded border border-dashed border-[var(--line-strong)] bg-[var(--surface-2)] px-2 py-1.5 text-left"
                >
                  <span className="mono mb-0.5 block truncate text-[11.5px] text-[var(--ink-3)]">
                    {n.arxiv_id} · p.{n.page} · {n.kind} · 分 {n.score}
                  </span>
                  <span className="line-clamp-1 block text-[12.5px] leading-snug text-[var(--ink-2)]">
                    {n.preview}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

/* ---------------------------------------------------------------------------
 * 2. 证据冲突
 * ------------------------------------------------------------------------- */

/**
 * 冲突面板：左右并列，**中间不写任何倾向性的话**。
 *
 * 排版上刻意做成完全对称（同样的宽度、同样的字号、同样的按钮），
 * 因为不对称会被读成"左边是主、右边是次"，那就是在替用户下判断。
 */
export function ConflictPanel({
  conflicts, onOpenRef,
}: {
  conflicts: Conflict[];
  onOpenRef: OpenPageRef;
}) {
  if (!conflicts.length) return null;
  return (
    <section className="rounded-lg border bg-[var(--surface)] p-4" style={{ borderColor: VIOLET.bd }}>
      <SectionHead
        label="CONFLICT · 证据冲突"
        right={
          <span className="mono text-[11.5px] text-[var(--ink-3)]">{conflicts.length} 处</span>
        }
      />
      <div className="space-y-4">
        {conflicts.map((c) => (
          <div key={c.id}>
            <div className="mb-2 flex flex-wrap items-baseline gap-2">
              <span
                className="mono rounded border px-1.5 py-[1px] text-[11.5px]"
                style={{ color: VIOLET.fg, background: VIOLET.bg, borderColor: VIOLET.bd }}
              >
                {c.type_label}
              </span>
              <span className="text-[15.5px] font-semibold text-[var(--ink)]">{c.dimension}</span>
              <span className="text-[12.5px] text-[var(--ink-3)]">{c.why}</span>
            </div>

            <div className="grid gap-px overflow-hidden rounded border border-[var(--line)] bg-[var(--line)] sm:grid-cols-2">
              {c.sides.map((s, i) => (
                <div key={i} className="flex flex-col bg-[var(--surface)] p-2.5">
                  <div className="mono mb-1 text-[11px] tracking-[0.14em] text-[var(--ink-3)]">
                    结论 {String.fromCharCode(65 + i)}
                  </div>
                  <div className="mono mb-1.5 break-words text-[14.5px] font-medium leading-snug text-[var(--ink)]">
                    {s.conclusion}
                  </div>
                  <div className="mb-1 space-y-0.5">
                    {s.papers.map((p) => (
                      <div key={`${p.arxiv_id}-${p.page}`} className="truncate text-[12.5px] text-[var(--ink-2)]">
                        {p.title || p.arxiv_id}
                        {p.page ? <span className="mono text-[var(--ink-3)]"> · p.{p.page}</span> : null}
                      </div>
                    ))}
                  </div>
                  {s.quotes[0] && (
                    <p className="mono mb-1.5 line-clamp-3 rounded bg-[var(--surface-2)] px-1.5 py-1 text-[11.5px] leading-relaxed text-[var(--ink-2)]">
                      {s.quotes[0].text}
                    </p>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      const q = s.quotes[0];
                      const p = s.papers[0];
                      onOpenRef({
                        arxiv_id: q?.arxiv_id || p?.arxiv_id || '',
                        title: q?.title || p?.title || null,
                        page: q?.page || p?.page || 0,
                        bbox: null, rects: null, precision: null,
                        text: q?.text || null, ref_id: null, match_score: null,
                      });
                    }}
                    className="mono mt-auto self-start rounded border border-[var(--line-strong)] px-1.5 py-[1px] text-[11.5px] text-[var(--ink-2)] transition hover:border-[var(--signal)] hover:text-[var(--signal)]"
                  >
                    看原文 ↗
                  </button>
                </div>
              ))}
            </div>

            {/* 不下判断：这句话必须存在，否则左右并排天然被读成"左对右错" */}
            <p className="mt-1.5 flex items-start gap-1.5 text-[12.5px] leading-relaxed text-[var(--ink-2)]">
              <span className="mono mt-[1px] shrink-0 text-[11px] tracking-[0.12em] text-[var(--ink-3)]">
                不下判断
              </span>
              {c.not_judged}
            </p>

            {c.ambiguous && c.ambiguous.length > 0 && (
              <p className="mono mt-1 text-[11.5px] leading-relaxed text-[var(--ink-3)]">
                另有 {c.ambiguous.length} 篇用同一个符号表示别的量（
                {c.ambiguous.map((a) => `${a.arxiv_id} p.${a.page} ${a.unit}`).join('、')}），
                不计入冲突。
              </p>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------------------
 * 3. 表格 / 图表未识别
 * ------------------------------------------------------------------------- */

const KIND_TAG: Record<string, string> = {
  table_empty: '表格',
  figure_caption_only: '图',
  figure_synthetic: '图',
  scan_page: '扫描件',
};

/**
 * 未识别提示：一条一行。
 *
 * 被引用的那条用实线红边、排在前面——它意味着"你刚看到的那句话，
 * 支撑它的原文本身是残缺的"。其余用虚线灰边，只是"顺带一提"。
 */
export function UnreadableNotice({
  items, onOpenRef,
}: {
  items: UnreadableItem[];
  onOpenRef: OpenPageRef;
}) {
  if (!items.length) return null;
  return (
    <section className="rounded-lg border border-[var(--line)] bg-[var(--surface)] p-3">
      <SectionHead
        label="UNREADABLE · 表格与图表"
        right={
          <span className="mono text-[11.5px] text-[var(--ink-3)]">
            {items.length} 处 ·{' '}
            {items.some((x) => x.cited)
              ? `${items.filter((x) => x.cited).length} 处影响本次回答`
              : '均未影响本次回答'}
          </span>
        }
      />
      <div className="space-y-1">
        {items.map((x) => {
          const tone = x.cited ? RED : SLATE;
          return (
            <div
              key={x.id}
              className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded border px-2 py-1.5"
              style={{
                borderColor: tone.bd,
                borderStyle: x.cited ? 'solid' : 'dashed',
                background: x.cited ? tone.bg : 'var(--surface-2)',
              }}
            >
              <span className="mono shrink-0 rounded border border-current px-1 text-[11px]" style={{ color: tone.fg }}>
                {KIND_TAG[x.kind] || x.kind}
              </span>
              <span className="shrink-0 text-[13.5px] font-medium" style={{ color: tone.fg }}>
                {x.title}
              </span>
              <span className="mono flex min-w-0 max-w-[260px] shrink items-baseline gap-1 text-[11.5px] text-[var(--ink-3)]">
                <span className="shrink-0">p.{x.page}</span>
                {x.paper_title && <span className="truncate">{x.paper_title}</span>}
              </span>
              {x.cited && (
                <span className="mono shrink-0 rounded px-1 text-[11px] text-white" style={{ background: RED.fg }}>
                  本次回答引用了它
                </span>
              )}
              <span className="min-w-0 flex-1 truncate text-[12.5px] text-[var(--ink-2)]">{x.detail}</span>
              <button
                type="button"
                onClick={() =>
                  onOpenRef({
                    arxiv_id: x.arxiv_id, title: x.paper_title || null, page: x.page,
                    bbox: null, rects: null, precision: null, text: null,
                    ref_id: null, match_score: null,
                  })
                }
                className="mono shrink-0 rounded border border-[var(--line-strong)] px-1.5 py-[1px] text-[11.5px] text-[var(--ink-2)] transition hover:border-[var(--signal)] hover:text-[var(--signal)]"
              >
                查看原页 ↗
              </button>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------------------
 * 4. 判定依据（可展开）
 * ------------------------------------------------------------------------- */

/**
 * 判定条件原样展示。
 *
 * 三类状态都是"系统在说一件关于自己的事"，那它必须能被质询：
 * 凭什么说库里没有？凭什么说这是冲突？把判据放在界面上，比写进文档更有约束力——
 * 代码改了但这里没改，界面上一眼能看出来。`checked` 还区分了"没检测"与"检测了没发现"。
 */
export function UncertaintyRules({ uncertainty }: { uncertainty: Uncertainty }) {
  const [open, setOpen] = useState(false);
  const checked = uncertainty.checked || {};
  const ran = [
    checked.empty ? '空检索 已检测' : '空检索 未检测',
    checked.conflict?.ran
      ? `冲突 已检测（${checked.conflict.symbols?.length ? `符号 ${checked.conflict.symbols.join('、')}` : `${checked.conflict.claims} 条主张`}）`
      : '冲突 未检测',
    checked.unreadable?.ran ? `未识别 已检测（${checked.unreadable.sources} 段）` : '未识别 未检测',
  ];

  return (
    <section className="rounded border border-[var(--line)] bg-[var(--surface-2)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
      >
        <span className="mono text-[11.5px] tracking-[0.14em] text-[var(--ink-3)]">判定依据</span>
        <span className="mono truncate text-[11.5px] text-[var(--ink-3)]">
          {ran.join(' · ')} · {uncertainty.version}
        </span>
        <span className="h-px flex-1 bg-[var(--line)]" />
        <span className="text-[11.5px] text-[var(--ink-3)]">{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-[var(--line)] px-2.5 py-2">
          {uncertainty.rules.map((r) => (
            <div key={r.state}>
              <div className="mb-0.5 flex items-baseline gap-1.5">
                <span className="mono text-[11.5px] text-[var(--signal)]">{r.state}</span>
                <span className="text-[13px] font-medium text-[var(--ink)]">{r.title}</span>
              </div>
              <div className="text-[12px] leading-relaxed text-[var(--ink-2)]">
                <span className="text-[var(--ink-3)]">判据 </span>
                {r.when}
              </div>
              <div className="text-[12px] leading-relaxed text-[var(--ink-2)]">
                <span className="text-[var(--ink-3)]">为什么 </span>
                {r.why}
              </div>
              <div className="text-[12px] leading-relaxed text-[var(--ink-2)]">
                <span className="text-[var(--ink-3)]">下一步 </span>
                {r.next}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
