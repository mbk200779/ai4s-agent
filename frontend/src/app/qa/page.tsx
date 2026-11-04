'use client';

import { useState } from 'react';
import PdfViewer from '@/components/PdfViewer';
import { useToast } from '@/components/Toast';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

interface Citation {
  paper_id: string;
  arxiv_id: string;
  title: string;
  chunk_text: string;
  page: number;
  bbox?: number[];
}

interface QAResponse {
  answer: string;
  rejected?: boolean;
  reject_reason?: string;
  citations: Citation[];
  invalid_citations?: string[];
  provenance_key?: string;
}

interface Message {
  question: string;
  response: QAResponse;
}

interface ViewerState {
  pdfUrl: string;
  page: number;
  bbox?: number[];
  title: string;
}

const CITE_RE = /(\[(引#\d+(?::[^\]]+)?)\])/g;

export default function QAPage() {
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(false);
  const [viewer, setViewer] = useState<ViewerState | null>(null);
  const { toast } = useToast();

  const ask = async () => {
    if (!question.trim()) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/qa/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, top_k: 8, rrf_k: 60 }),
      });
      const data: QAResponse = await res.json();
      setMessages((prev) => [...prev, { question, response: data }]);
      setQuestion('');
    } catch (e) {
      toast('请求失败，请检查后端服务', 'error');
    } finally {
      setLoading(false);
    }
  };

  const openCitation = async (citation: Citation) => {
    // 直接走后端 PDF 代理端点，无需 presigned URL
    setViewer({
      pdfUrl: `${API_BASE}/api/v1/papers/${citation.arxiv_id}/pdf`,
      page: citation.page,
      bbox: citation.bbox,
      title: citation.title,
    });
  };

  const renderAnswer = (answer: string, citations: Citation[]) => {
    const parts = answer.split(CITE_RE);
    return parts.map((part, idx) => {
      const match = part.match(/^\[(引#(\d+)(?::[^\]]+)?)\]$/);
      if (match) {
        const index = parseInt(match[2], 10) - 1;
        const citation = citations[index];
        return (
          <button
            key={idx}
            onClick={() => citation && openCitation(citation)}
            disabled={!citation}
            className="mx-0.5 rounded bg-[var(--signal-soft)] px-1 text-sm font-medium text-[var(--signal-hover)] hover:bg-[#d2e9e4] disabled:opacity-50"
            title={citation ? `跳转 ${citation.title} 第 ${citation.page} 页` : '未知引用'}
          >
            {part}
          </button>
        );
      }
      return <span key={idx}>{part}</span>;
    });
  };

  return (
    <div className="space-y-6">
      {/* 页头 */}
      <div className="flex items-center gap-4">
        <span className="flex h-11 w-11 items-center justify-center rounded-lg border border-[var(--line)] bg-[var(--surface)] text-[var(--signal)]">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.76 9.76 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
          </svg>
        </span>
        <div>
          <span className="eyebrow">Question & Answer</span>
          <h1 className="display text-2xl">可溯源问答</h1>
        </div>
      </div>

      <div className="flex gap-2">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="输入科研问题..."
          className="field flex-1"
          onKeyDown={(e) => e.key === 'Enter' && ask()}
        />
        <button onClick={ask} disabled={loading} className="btn-primary">
          {loading ? '思考中...' : '提问'}
        </button>
      </div>

      <div className="space-y-6">
        {messages.map((msg, i) => (
          <div key={i} className="card card-tick space-y-4 p-6">
            <div>
              <div className="eyebrow mb-1">Question</div>
              <p className="text-base font-medium text-[var(--ink)]">{msg.question}</p>
            </div>

            {msg.response.rejected ? (
              <div className="rounded-lg border border-[var(--danger)] bg-red-50 p-4 text-[var(--danger)]">
                拒答：{msg.response.reject_reason}
              </div>
            ) : (
              <div>
                <div className="eyebrow mb-2">Answer</div>
                <p className="leading-relaxed text-[var(--ink)]">
                  {renderAnswer(msg.response.answer, msg.response.citations)}
                </p>
              </div>
            )}

            {msg.response.citations.length > 0 && (
              <div>
                <div className="mb-2 flex items-center gap-2">
                  <span className="mono text-xs text-[var(--ink-3)]">CITATIONS</span>
                  <span className="text-xs text-[var(--ink-3)]">点击引用可高亮回跳原文</span>
                </div>
                <div className="space-y-2">
                  {msg.response.citations.map((c, idx) => (
                    <button
                      key={idx}
                      onClick={() => openCitation(c)}
                      className="w-full rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-4 text-left text-sm transition hover:border-[var(--signal)] hover:bg-[var(--signal-soft)]"
                    >
                      <div className="mb-1 mono text-xs text-[var(--ink-3)]">
                        [{idx + 1}] {c.title} (arXiv:{c.arxiv_id}, 页 {c.page})
                        {c.bbox && ' · 含 bbox 高亮'}
                      </div>
                      <div className="text-[var(--ink-2)]">{c.chunk_text}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {viewer && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setViewer(null)}
        >
          <div
            className="card max-h-[90vh] max-w-[90vw] overflow-auto p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-semibold text-[var(--ink)]">{viewer.title}</h3>
              <button
                onClick={() => setViewer(null)}
                className="btn-ghost px-3 py-1 text-sm"
              >
                关闭
              </button>
            </div>
            <PdfViewer pdfUrl={viewer.pdfUrl} page={viewer.page} bbox={viewer.bbox} />
          </div>
        </div>
      )}
    </div>
  );
}
