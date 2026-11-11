'use client';

/**
 * 常规问题示例（胶囊按钮组）
 *
 * 样式与写作页「试试这些主题」保持一致：card + card-tick 容器、
 * 圆角胶囊按钮、hover 时边框转信号青并浅底高亮。
 * 点击后只把值交给父组件的 onPick，由父组件写入对应的输入框状态，
 * 不改动输入框自身的布局与交互。
 */

export interface Suggestion {
  /** 按钮上显示的文字 */
  label: string;
  /** 点击后填入输入框的值 */
  value: string;
}

type Item = string | Suggestion;

function normalize(items: Item[]): Suggestion[] {
  return items.map((it) => (typeof it === 'string' ? { label: it, value: it } : it));
}

export default function SuggestionChips({
  title = '试试这些主题',
  items,
  onPick,
}: {
  title?: string;
  items: Item[];
  onPick: (value: string) => void;
}) {
  const list = normalize(items);
  if (list.length === 0) return null;

  return (
    <div className="card card-tick p-6">
      <h3 className="mb-4 flex items-center gap-2 text-base font-semibold text-[var(--ink)]">
        {/* 灯泡：提示这是“可选项”，与写作页原样式一致 */}
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--signal)"
          strokeWidth="1.8"
          className="h-4 w-4"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18"
          />
        </svg>
        {title}
      </h3>
      <div className="flex flex-wrap gap-2">
        {list.map((s) => (
          <button
            key={s.value}
            type="button"
            onClick={() => onPick(s.value)}
            title={s.label === s.value ? undefined : `填入：${s.value}`}
            className="rounded-full border border-[var(--line-strong)] bg-[var(--surface-2)] px-4 py-2 text-sm font-medium text-[var(--ink-2)] transition hover:border-[var(--signal)] hover:bg-[var(--signal-soft)] hover:text-[var(--signal-hover)]"
          >
            {s.label}
          </button>
        ))}
      </div>
    </div>
  );
}
