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
            className="mx-0.5 rounded bg-amber-500/20 px-1 text-sm text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
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
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-500/15 text-indigo-300">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-6 w-6">
            <path strokeLinecap="round" strokeLinejoin="round" d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.76 9.76 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
          </svg>
        </span>
        <div>
          <h2 className="text-xl font-bold text-white">可溯源问答</h2>
          <p className="text-sm text-slate-400">基于论文原文回答，引用可回跳原文</p>
        </div>
      </div>
      <div className="flex gap-2">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="输入科研问题..."
          className="flex-1 rounded-xl border border-white/10 bg-[#1c2740] p-3 text-slate-200 outline-none transition placeholder:text-slate-500 focus:border-indigo-400 focus:shadow-ring"
          onKeyDown={(e) => e.key === 'Enter' && ask()}
        />
        <button
          onClick={ask}
          disabled={loading}
          className="btn-press btn-primary rounded-xl px-6 py-2 font-medium"
        >
          {loading ? '思考中...' : '提问'}
        </button>
      </div>

      <div className="space-y-6">
        {messages.map((msg, i) => (
          <div key={i} className="space-y-4 rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-6">
            <div>
              <h3 className="mb-1 text-sm text-slate-500">问题</h3>
              <p className="text-slate-100">{msg.question}</p>
            </div>

            {msg.response.rejected ? (
              <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-300">
                拒答：{msg.response.reject_reason}
              </div>
            ) : (
              <div>
                <h3 className="mb-2 font-semibold text-indigo-300">回答</h3>
                <p className="leading-relaxed text-slate-200">
                  {renderAnswer(msg.response.answer, msg.response.citations)}
                </p>
              </div>
            )}

            {msg.response.citations.length > 0 && (
              <div>
                <h3 className="mb-2 text-sm font-semibold text-slate-500">引用来源（点击引用可高亮回跳原文）</h3>
                <div className="space-y-3">
                  {msg.response.citations.map((c, idx) => (
                    <button
                      key={idx}
                      onClick={() => openCitation(c)}
                      className="w-full rounded-xl border border-white/[0.08] bg-[#1c2740] p-4 text-left text-sm transition hover:border-indigo-400/40 hover:bg-[#24304d]"
                    >
                      <div className="mb-1 text-slate-400">
                        [{idx + 1}] {c.title} (arXiv:{c.arxiv_id}, 页 {c.page})
                        {c.bbox && ' · 含 bbox 高亮'}
                      </div>
                      <div className="text-slate-300">{c.chunk_text}</div>
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
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
          onClick={() => setViewer(null)}
        >
          <div
            className="max-h-[90vh] max-w-[90vw] overflow-auto rounded-xl border border-white/10 bg-[#1c2740] p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-semibold text-indigo-300">{viewer.title}</h3>
              <button
                onClick={() => setViewer(null)}
                className="rounded-lg bg-white/10 px-3 py-1 text-sm text-slate-200 hover:bg-white/20"
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
