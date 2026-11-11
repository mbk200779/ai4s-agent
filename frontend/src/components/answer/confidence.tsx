'use client';

/**
 * 三档置信的视觉层：一处定义，句子标记、角标、汇总都从这里取。
 *
 * 为什么要单独成文件而不是写在 StreamAnswer 里：
 * 三档的颜色/文案/工具提示会在"句子下划线、角标、汇总条、待核清单"四个地方出现，
 * 分头写必然漂移（改了角标忘了下划线，两种颜色对不上，用户会以为它们是两件事）。
 */

import type { ClaimEvent, Confidence, ConfidenceLevel, MissingItem } from '@/types/stream';

export interface LevelStyle {
  /** 句末那个小角标的颜色 */
  color: string;
  bg: string;
  border: string;
  /** 句子本身的装饰（下划线） */
  textClass: string;
  /** 汇总条上的圆点 */
  dot: string;
  /** 汇总条容器 */
  pill: string;
  label: string;
  hint: string;
}

export const LEVEL_STYLE: Record<ConfidenceLevel, LevelStyle> = {
  verified: {
    color: '#0f766e',
    bg: '#eefbf8',
    border: '#7fc9bd',
    textClass: '',
    dot: 'bg-[#0f766e]',
    pill: 'border-[#7fc9bd] bg-[#eefbf8] text-[#0f766e]',
    label: '已验证',
    hint: '原文有直接证据，可据此判定',
  },
  needs_check: {
    color: '#a16207',
    bg: '#fdf6e8',
    border: '#e0b872',
    // 虚线下划线而不是实线：实线在正文里像超链接，会被误认为"点开有别的页面"
    textClass:
      'underline decoration-dashed decoration-1 underline-offset-[3px] decoration-[#d9a441]',
    dot: 'bg-[#c2872a]',
    pill: 'border-[#e0b872] bg-[#fdf6e8] text-[#a16207]',
    label: '需核对',
    hint: '有相关证据，但需人工核对后才能采信',
  },
  unsupported: {
    color: '#b42318',
    bg: '#fef3f2',
    border: '#f0a9a2',
    textClass:
      'underline decoration-dotted decoration-1 underline-offset-[3px] decoration-[#e08a82]',
    dot: 'bg-[#b42318]',
    pill: 'border-[#f0a9a2] bg-[#fef3f2] text-[#b42318]',
    label: '证据不足',
    hint: '没有找到能支撑这句话的原文',
  },
};

export function levelOf(claim: ClaimEvent): ConfidenceLevel {
  // 老服务端（06 之前）没有 level 字段：有据即 verified，无据即 unsupported。
  // 不抛错、不留空——流式过程中任何一次 undefined 都会让整句失去标记。
  return claim.level ?? (claim.status === 'supported' ? 'verified' : 'unsupported');
}

/** 缺项的紧凑文案：只取第一条，完整清单在待核面板里 */
export function shortMissing(missing: MissingItem[] | undefined): string | null {
  if (!missing || !missing.length) return null;
  return missing[0].text;
}

export function missingTitle(missing: MissingItem[] | undefined): string | undefined {
  if (!missing || !missing.length) return undefined;
  return missing.map((m) => `· ${m.text}`).join('\n');
}

/**
 * 汇总条：一句话说清"这条回答能不能直接用"。
 *
 * 为什么档位标签要和说明文案一起显示：只给一个「需核对」的色块，
 * 用户还是得自己猜"核什么"。这行的作用是把三档的定义写在它旁边。
 */
export function ConfidencePill({
  confidence,
  expanded,
  onToggle,
}: {
  confidence: Confidence;
  expanded: boolean;
  onToggle: () => void;
}) {
  if (!confidence.applicable || !confidence.level) {
    return (
      <span className="mono rounded border border-[var(--line-strong)] bg-[var(--surface-2)] px-2 py-[2px] text-[11.5px] text-[var(--ink-3)]">
        {confidence.label || '不适用'}
      </span>
    );
  }
  const style = LEVEL_STYLE[confidence.level];
  const pending = confidence.counts.needs_check + confidence.counts.unsupported;

  return (
    <button
      type="button"
      onClick={onToggle}
      className={`flex items-center gap-2 rounded border px-2.5 py-1 text-left transition hover:brightness-[0.98] ${style.pill}`}
      title={confidence.notes.join('\n') || style.hint}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      <span className="text-[12.5px] font-medium">{confidence.label}</span>
      <span className="hidden text-[12.5px] opacity-80 sm:inline">
        {confidence.notes[0] || style.hint}
      </span>
      {pending > 0 && (
        <span className="mono shrink-0 text-[11.5px] opacity-70">
          {expanded ? '收起' : `${pending} 处待看`}
        </span>
      )}
    </button>
  );
}

/**
 * 待核清单：把每一句的档位、命中分、缺什么摊开。
 *
 * 「不得静默降级」在界面上的落点就是这块——置信不是一个小色块，
 * 而是可以逐句展开、能看到"为什么这句只到需核对"的账。
 */
export function ConfidenceDetail({ claims }: { claims: ClaimEvent[] }) {
  const flagged = claims.filter((c) => levelOf(c) !== 'verified');
  if (!flagged.length) {
    return (
      <div className="rounded border border-[var(--line)] bg-[var(--surface-2)] px-3 py-2 text-[12.5px] text-[var(--ink-3)]">
        每一句都能在原文中找到对应句子，没有待核项。
      </div>
    );
  }
  return (
    <div className="space-y-1.5 rounded border border-[var(--line)] bg-[var(--surface-2)] p-2.5">
      <div className="mono text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">需人工核对的部分</div>
      {flagged.map((c) => {
        const level = levelOf(c);
        const style = LEVEL_STYLE[level];
        const conf = c.confidence;
        const sig = conf?.signals as
          | { best_score?: number; sem_score?: number | null; effective_score?: number }
          | undefined;
        return (
          <div key={c.claim_id} className="rounded border border-[var(--line)] bg-white px-2.5 py-1.5">
            <div className="flex items-start gap-2">
              <span
                className="mono mt-[1px] shrink-0 rounded border px-1 py-[1px] text-[11px] leading-none"
                style={{ color: style.color, background: style.bg, borderColor: style.border }}
              >
                {style.label}
              </span>
              <span className="flex-1 text-[13.5px] leading-relaxed text-[var(--ink)]">{c.text}</span>
            </div>
            <div className="mono mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 pl-1 text-[11.5px] text-[var(--ink-3)]">
              {conf?.locate?.note && <span>{conf.locate.note}</span>}
              {sig?.effective_score != null && (
                <span>
                  支持度 {Number(sig.effective_score).toFixed(2)}
                  {sig.sem_score != null ? `（语义 ${Number(sig.sem_score).toFixed(2)}）` : ''}
                </span>
              )}
              {c.citations.filter((e) => e.status === 'supported').length > 0 && (
                <span>{c.citations.filter((e) => e.status === 'supported').length} 条证据</span>
              )}
            </div>
            {(c.missing?.length ?? 0) > 0 ? (
              <ul className="mt-1 space-y-0.5 pl-1">
                {c.missing.map((m) => (
                  <li key={m.code} className="flex items-start gap-1.5 text-[12.5px] leading-snug text-[#8a5a13]">
                    <span className="mt-[5px] h-1 w-1 shrink-0 rounded-full bg-[#c2872a]" />
                    <span>{m.text}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="mt-1 pl-1 text-[12.5px] text-[var(--ink-3)]">
                {c.reason || '这句话没有挂到任何原文句子'}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** 阈值版本：调了阈值必须能看出来，否则两次评测的结论不可比 */
export function ConfidenceConfigLine({ confidence }: { confidence: Confidence }) {
  const cfg = confidence.config;
  if (!cfg) return null;
  const t = cfg.thresholds || {};
  return (
    <span className="mono text-[11.5px] text-[var(--ink-3)]">
      {cfg.version} · fp {cfg.fingerprint} · 验证线 {t.verified_score ?? '-'}
    </span>
  );
}
