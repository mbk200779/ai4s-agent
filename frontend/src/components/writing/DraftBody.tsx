'use client';

import { WritingSection, WritingSentence } from '@/types/writing';

/**
 * 正文：一句一个可点单元。
 *
 * 两个视觉约定：
 * - `claim` 句后面的 `[引#N]` 是**按钮**，点它右侧面板直接落到那一段原文；
 * - `bridge` 句带虚线底 + 「无需引用」小标。不标的话读者会以为作者漏了引用——
 *   这正是"过渡句要标记为无需引用"这条要求的界面落点。
 */
export default function DraftBody({
  sections,
  activeSentence,
  activeCite,
  onSelect,
}: {
  sections: WritingSection[];
  activeSentence: string | null;
  activeCite: number | null;
  onSelect: (sentence: WritingSentence, cite?: number) => void;
}) {
  return (
    <div className="space-y-6">
      {sections.map((section) => (
        <section key={section.id}>
          <div className="mb-2 flex items-baseline gap-2">
            <h3 className="display text-lg">
              <span className="mono mr-1 text-sm text-[var(--ink-3)]">{section.no}.</span>
              {section.title}
            </h3>
            {!section.placeholder && (
              <span className="mono text-[11px] text-[var(--ink-3)]">
                {section.sentences.length} 句 · 证据 {section.evidence.length} 段
              </span>
            )}
          </div>

          {section.intent && !section.placeholder && (
            <p className="mb-2 text-xs text-[var(--ink-3)]">本节要回答：{section.intent}</p>
          )}

          {section.placeholder ? (
            <div className="rounded-lg border border-[var(--warn)]/50 bg-amber-50/70 p-4">
              <p className="mb-1 flex items-center gap-2 text-sm font-semibold text-[var(--warn)]">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                  />
                </svg>
                本段暂无库内证据支撑，未生成正文
              </p>
              <p className="text-sm leading-relaxed text-[var(--ink-2)]">
                {section.gap || '（未说明缺口）'}
              </p>
              <p className="mt-2 text-xs text-[var(--ink-3)]">
                占位而不是编造：库里没有能支撑这一段的原文，写出来的每一句都会是无源之谈。
                补上相关文献后重新生成，这一段才会出现。
              </p>
            </div>
          ) : (
            <p className="text-[15px] leading-8 text-[var(--ink)]">
              {section.sentences.map((sentence) => {
                const isBridge = sentence.type === 'bridge';
                const selected = activeSentence === sentence.id;
                return (
                  <span
                    key={sentence.id}
                    onClick={() => onSelect(sentence)}
                    className={`cursor-pointer rounded transition-colors ${
                      selected ? 'bg-[var(--signal-soft)]' : 'hover:bg-[var(--surface-2)]'
                    } ${isBridge ? 'border-b border-dashed border-[var(--ink-3)]/60' : ''}`}
                    title={isBridge ? '过渡/连接句 · 无需引用' : '陈述结论 · 点开看引用原文'}
                  >
                    {sentence.text}
                    {isBridge && (
                      <span className="mono ml-1 select-none align-middle text-[10px] tracking-tight text-[var(--ink-3)]">
                        无需引用
                      </span>
                    )}
                    {sentence.cite_nos.map((n) => (
                      <button
                        key={n}
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelect(sentence, n);
                        }}
                        className={`mono ml-0.5 align-super text-[11px] transition ${
                          selected && activeCite === n
                            ? 'font-bold text-[var(--signal)]'
                            : 'text-[var(--signal)]/80 hover:text-[var(--signal)] hover:underline'
                        }`}
                      >
                        [引#{n}]
                      </button>
                    ))}
                  </span>
                );
              })}
            </p>
          )}
        </section>
      ))}
    </div>
  );
}
