'use client';

import { useRef, useState } from 'react';
import PdfViewer from '@/components/PdfViewer';
import StreamAnswer, { OpenRef } from '@/components/answer/StreamAnswer';
import SuggestionChips from '@/components/SuggestionChips';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

interface Message {
  id: string;
  question: string;
}

interface ViewerState extends OpenRef {
  pdfUrl: string;
}

export default function QAPage() {
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [viewer, setViewer] = useState<ViewerState | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const seqRef = useRef(0);

  /**
   * 提交一个问题。
   *
   * 独立成函数是因为空检索卡片上的"换个问法"要能直接调它——
   * 那三个按钮如果只是把文字填回输入框，用户还得再按一次回车，
   * "下一步动作"就退化成了"下一步提示"。
   */
  const askQuestion = (raw: string) => {
    const q = (raw || '').trim();
    if (!q) return;
    seqRef.current += 1;
    setMessages((prev) => [...prev, { id: `m${seqRef.current}`, question: q }]);
    setQuestion('');
  };

  const ask = () => askQuestion(question);

  /**
   * 打开原文。角标带的是"哪一句"（ref_id + rects），所以这里直接按句画框；
   * 只有历史文档没有锚点时才退回 bbox 整段定位——两种情况的框样式不同，
   * 用户一眼能看出"这句是精确找到的"还是"只定位到这一段"。
   */
  const openRef = (ref: OpenRef) => {
    if (!ref.arxiv_id) return;
    setViewer({
      ...ref,
      pdfUrl: `${API_BASE}/api/v1/papers/${ref.arxiv_id}/pdf`,
    });
  };

  return (
    <div className="space-y-6">
      {/* 页头：图标保留、去掉外层边框容器，直接作为页头标记 */}
      <div className="flex items-center gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            className="h-8 w-8 shrink-0 text-[var(--signal)]"
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.76 9.76 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
          </svg>
          <div className="min-w-0">
            <h1 className="display text-3xl leading-tight">可溯源问答</h1>
            <p className="mt-1 text-[13px] leading-snug text-[var(--ink-3)] sm:text-sm">
              流式作答 · 逐句校验 · 角标可回跳原文
            </p>
          </div>
        </div>

        <div className="hidden flex-1 items-center gap-3 sm:flex">
          <span className="h-px flex-1 bg-[var(--line)]" />
          <span className="mono shrink-0 text-[10px] tracking-[0.2em] text-[var(--ink-3)]">
            RAG · SSE · TRACEABLE
          </span>
        </div>
      </div>

      <div className="flex items-end gap-2">
        {/* 输入区：形象从输入框顶边探出，下半身藏在输入框之后，不占用输入区域 */}
        <div className="relative min-w-0 flex-1 pt-[52px] sm:pt-[68px]">
          <div className="pointer-events-none absolute left-1 top-0 hidden h-[68px] select-none items-center gap-3 sm:flex">
            <span className="mono text-[10px] tracking-[0.22em] text-[var(--ink-3)]">
              QUERY · CH.01
            </span>
            <span className="h-3 w-px bg-[var(--line-strong)]" />
            <span className="text-xs text-[var(--ink-3)]">
              按 Enter 发送，正文边生成边显示，角标在校验完成后回填
            </span>
          </div>
          <div
            aria-hidden="true"
            className="pointer-events-none absolute right-4 top-0 z-0 h-16 w-16 select-none sm:right-8 sm:h-20 sm:w-20"
          >
            <img
              src="/mascot-avatar.png"
              alt=""
              width={512}
              height={512}
              className="relative h-full w-full object-contain"
            />
            <svg
              viewBox="0 0 200 56"
              fill="none"
              className="absolute -left-[134px] top-0 h-[35px] w-[124px] sm:left-auto sm:-right-[30px] sm:-top-[17px]"
            >
              <path
                d="M182 36 C 150 44, 70 6, 6 42"
                stroke="#0d9488"
                strokeOpacity="0.5"
                strokeWidth="2.2"
                strokeDasharray="5 6"
                strokeLinecap="round"
              />
              <circle cx="144" cy="25" r="1.9" fill="#0d9488" opacity="0.32" />
              <circle cx="100" cy="19" r="1.6" fill="#0d9488" opacity="0.24" />
              <circle cx="53" cy="24" r="1.3" fill="#0d9488" opacity="0.16" />
              <path
                d="M200 5 L162 24 L176 31 Z"
                fill="#eff8f5"
                stroke="#0f5f57"
                strokeOpacity="0.5"
                strokeWidth="1.9"
                strokeLinejoin="round"
              />
              <path
                d="M200 5 L176 31 L170 48 Z"
                fill="#a9d8d0"
                stroke="#0f5f57"
                strokeOpacity="0.5"
                strokeWidth="1.9"
                strokeLinejoin="round"
              />
              <path d="M200 5 L176 31" stroke="#0f5f57" strokeOpacity="0.28" strokeWidth="1.7" strokeLinecap="round" />
            </svg>
          </div>
          <input
            ref={inputRef}
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="输入科研问题..."
            className="field relative z-10 w-full"
            onKeyDown={(e) => e.key === 'Enter' && ask()}
          />
        </div>
        <button onClick={ask} className="btn-primary">
          提问
        </button>
      </div>

      <SuggestionChips
        title="试试这些问题"
        items={[
          'β2 的取值是多少？',
          '注意力机制的计算复杂度是多少？',
          'Transformer 相比 RNN 的核心改进是什么？',
          'BERT base 与 BERT large 的层数和隐藏维度分别是多少？',
          'LoRA 的秩 r 和缩放系数 α 通常取多少？',
        ]}
        onPick={(q) => {
          setQuestion(q);
          inputRef.current?.focus();
        }}
      />

      {/* 空态说明：尚未提问时展示链路，避免首屏留白 */}
      {messages.length === 0 && (
        <div>
          <div className="mb-4 flex items-baseline gap-4">
            <span className="eyebrow">How it streams</span>
            <span className="h-px flex-1 bg-[var(--line)]" />
            <span className="mono shrink-0 text-[10px] tracking-[0.18em] text-[var(--ink-3)]">
              SSE · 4 STAGES
            </span>
          </div>
          <div className="grid gap-px bg-[var(--line)] sm:grid-cols-4">
            {[
              { n: '01', t: '理解 · 检索' },
              { n: '02', t: '精排' },
              { n: '03', t: '流式正文' },
              { n: '04', t: '角标回填' },
            ].map((s) => (
              <div key={s.n} className="group bg-[var(--surface)] px-5 py-4">
                <div className="flex items-center gap-2">
                  <span className="mono text-[10px] tracking-[0.2em] text-[var(--signal)]">
                    {s.n}
                  </span>
                  <span className="h-px w-5 bg-[var(--signal)] opacity-40" />
                  <span className="text-sm font-medium text-[var(--ink)]">{s.t}</span>
                </div>
              </div>
            ))}
          </div>

          <div className="mt-4 flex items-start gap-3 rounded-lg border border-[var(--line)] bg-[var(--surface-2)] px-4 py-3">
            <span className="mono mt-0.5 shrink-0 text-[10px] tracking-[0.16em] text-[var(--ink-3)]">
              TIP
            </span>
            <p className="text-xs leading-relaxed text-[var(--ink-2)]">
              每个角标都带 <code className="mono">chunk_id + ref_id</code>，点击后定位到 PDF 的那一句话；
              同一片段被引用多次时，角标各自指向自己的那句，不会互相覆盖。
            </p>
          </div>
        </div>
      )}

      <div className="space-y-6">
        {messages.map((msg, i) => (
          <div key={msg.id} className="card card-tick space-y-4 p-6">
            <div>
              <div className="mb-1 flex items-center gap-2">
                <span className="mono text-[11.5px] tracking-[0.2em] text-[var(--ink-3)]">
                  Q.{String(i + 1).padStart(2, '0')}
                </span>
                <span className="h-px w-4 bg-[var(--line-strong)]" />
              </div>
              <p className="text-[18px] font-semibold leading-snug text-[var(--ink)]">{msg.question}</p>
            </div>
            <StreamAnswer question={msg.question} onOpenRef={openRef} onAsk={askQuestion} />
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3 border-t border-[var(--line)] pt-4">
        <span className="status-dot" />
        <span className="mono text-[10px] tracking-[0.18em] text-[var(--ink-3)]">
          GROUNDED IN INDEXED PAPERS
        </span>
        <span className="h-px flex-1 bg-[var(--line)]" />
        <span className="hidden text-xs text-[var(--ink-3)] sm:inline">
          每个角标都对应一条可回放的事件
        </span>
      </div>

      {viewer && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setViewer(null)}
        >
          <div
            className="card max-h-[90vh] max-w-[92vw] overflow-auto p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-2 flex items-start justify-between gap-6">
              <div className="min-w-0">
                <h3 className="truncate font-semibold text-[var(--ink)]">
                  {viewer.title || viewer.arxiv_id}
                </h3>
                <p className="mono mt-0.5 text-[12.5px] text-[var(--ink-3)]">
                  第 {viewer.page} 页
                  {viewer.ref_id ? ` · ${viewer.ref_id}` : ''}
                  {viewer.precision ? ` · ${viewer.precision}` : ''}
                  {viewer.match_score != null ? ` · 命中 ${viewer.match_score}` : ''}
                </p>
              </div>
              <button onClick={() => setViewer(null)} className="btn-ghost shrink-0 px-3 py-1 text-sm">
                关闭
              </button>
            </div>
            <PdfViewer
              pdfUrl={viewer.pdfUrl}
              page={viewer.page}
              bbox={viewer.bbox}
              rects={viewer.rects}
              precision={viewer.precision}
            />
            {viewer.text && (
              <div className="mt-3 max-w-[820px] rounded border border-[var(--line)] bg-[var(--surface-2)] px-3 py-2">
                <div className="mono mb-1 text-[11.5px] tracking-[0.16em] text-[var(--ink-3)]">
                  被引用的那一句
                </div>
                <p className="text-[14.5px] leading-relaxed text-[var(--ink-2)]">{viewer.text}</p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
