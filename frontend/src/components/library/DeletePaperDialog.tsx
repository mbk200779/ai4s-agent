'use client';

/**
 * 删除确认弹窗。
 *
 * 为什么不直接用 `window.confirm`：它只能放一句纯文本，说不清「删了会连带清掉什么」。
 * 而删除论文的代价恰恰不在那一行标题上——向量、PDF 原件、引用关系会一起消失，
 * 而且删完之后问答与综述的检索结果会立刻变。这些必须写在用户点「确认」之前，
 * 否则他确认的是一个自己并不了解的后果。
 *
 * 所以这里做三件事：
 * 1. 把要删的论文**逐条列出来**（超过 6 条折叠计数），避免「我选了 3 篇，不知道删的是哪 3 篇」；
 * 2. 明确写出会连带清除的附属物与数量（片段向量 / PDF 原件 / 引用关系）；
 * 3. 不可撤销的事只用一次红色强调，不做倒计时、不默认聚焦在危险按钮上。
 */

import { useEffect } from 'react';
import type { LibraryPaper } from '@/types/library';

interface Props {
  papers: LibraryPaper[];
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export default function DeletePaperDialog({ papers, busy, onCancel, onConfirm }: Props) {
  const count = papers.length;
  const chunks = papers.reduce((n, p) => n + (p.chunks || 0), 0);
  const pdfs = papers.filter((p) => p.has_pdf).length;
  const shown = papers.slice(0, 6);
  const rest = count - shown.length;

  // Esc 关闭；打开时锁住页面滚动，避免弹窗后面的列表跟着滚
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel();
    };
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [busy, onCancel]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-[2px]"
      onClick={() => !busy && onCancel()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-dialog-title"
        className="card w-full max-w-xl p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="eyebrow">LIBRARY.DELETE</div>
        <h2 id="delete-dialog-title" className="display mt-2 text-xl">
          删除 {count} 篇论文？
        </h2>
        <p className="mt-1 text-sm text-[var(--ink-2)]">
          这些论文会从数据库、向量索引与对象存储中一并移除，删除后无法恢复。
        </p>

        {/* 逐条列出，让用户核对「删的是不是这几篇」 */}
        <ul className="mt-4 max-h-64 space-y-1.5 overflow-y-auto rounded-md border border-[var(--line)] bg-[var(--surface-2)] p-3">
          {shown.map((p) => (
            <li key={p.id} className="flex items-start gap-2 text-sm">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--danger)]" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[var(--ink)]" title={p.title}>
                  {p.title}
                </span>
                <span className="mono block truncate text-xs text-[var(--ink-3)]">
                  {p.project_name} · {p.version_label ? `版本 ${p.version_label}` : '无版本'} ·{' '}
                  {p.chunks} 片段
                </span>
              </span>
            </li>
          ))}
          {rest > 0 && (
            <li className="mono pl-3.5 text-xs text-[var(--ink-3)]">… 另外 {rest} 篇</li>
          )}
        </ul>

        {/* 后果写清楚：这才是用户真正在确认的东西 */}
        <div
          className="mt-4 rounded-md border px-3 py-2.5 text-xs leading-relaxed"
          style={{ borderColor: 'rgba(220,38,38,0.28)', background: 'rgba(220,38,38,0.06)', color: 'var(--ink-2)' }}
        >
          将同时清除 <b className="mono">{chunks}</b> 个片段向量
          {pdfs > 0 && (
            <>
              、<b className="mono">{pdfs}</b> 个 PDF 原件
            </>
          )}
          ，以及这些论文参与的去重关系与引用记录。删除后问答与综述将不再检索到它们。
        </div>

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-4 py-2 text-sm font-medium text-[var(--ink-2)] transition hover:bg-[var(--surface-3)] disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-md px-4 py-2 text-sm font-semibold text-white transition disabled:opacity-60"
            style={{ background: 'var(--danger)' }}
          >
            {busy && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
            )}
            {busy ? '正在删除…' : `确认删除 ${count} 篇`}
          </button>
        </div>
      </div>
    </div>
  );
}
