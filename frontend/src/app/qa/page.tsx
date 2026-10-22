'use client';

import { useState } from 'react';
import PdfViewer from '@/components/PdfViewer';

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
      alert('请求失败');
    } finally {
      setLoading(false);
    }
  };

  const openCitation = async (citation: Citation) => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/papers/${citation.arxiv_id}/pdf-url`);
      const data = await res.json();
      setViewer({
        pdfUrl: data.pdf_url,
        page: citation.page,
        bbox: citation.bbox,
        title: citation.title,
      });
    } catch (e) {
      alert('获取 PDF 链接失败');
    }
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
            className="mx-0.5 rounded bg-yellow-500/20 px-1 text-sm text-yellow-300 hover:bg-yellow-500/40 disabled:opacity-50"
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
      <h2 className="text-lg font-semibold">可溯源问答</h2>
      <div className="flex gap-2">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="输入科研问题..."
          className="flex-1 rounded border border-slate-700 bg-white p-3 text-slate-900"
          onKeyDown={(e) => e.key === 'Enter' && ask()}
        />
        <button
          onClick={ask}
          disabled={loading}
          className="rounded bg-primary px-6 py-2 font-medium text-white hover:bg-blue-700"
        >
          {loading ? '思考中...' : '提问'}
        </button>
      </div>

      <div className="space-y-6">
        {messages.map((msg, i) => (
          <div key={i} className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/50 p-6">
            <div>
              <h3 className="mb-1 text-sm text-muted">问题</h3>
              <p className="text-slate-200">{msg.question}</p>
            </div>

            {msg.response.rejected ? (
              <div className="rounded border border-red-700 bg-red-900/30 p-4 text-red-200">
                拒答：{msg.response.reject_reason}
              </div>
            ) : (
              <div>
                <h3 className="mb-2 font-semibold text-primary">回答</h3>
                <p className="leading-relaxed text-slate-200">
                  {renderAnswer(msg.response.answer, msg.response.citations)}
                </p>
              </div>
            )}

            {msg.response.citations.length > 0 && (
              <div>
                <h3 className="mb-2 text-sm font-semibold text-muted">引用来源（点击引用可高亮回跳原文）</h3>
                <div className="space-y-3">
                  {msg.response.citations.map((c, idx) => (
                    <button
                      key={idx}
                      onClick={() => openCitation(c)}
                      className="w-full rounded border border-slate-800 bg-slate-900/30 p-4 text-left text-sm hover:border-slate-600"
                    >
                      <div className="mb-1 text-muted">
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
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={() => setViewer(null)}
        >
          <div
            className="max-h-[90vh] max-w-[90vw] overflow-auto rounded-xl border border-slate-700 bg-slate-900 p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-semibold text-primary">{viewer.title}</h3>
              <button
                onClick={() => setViewer(null)}
                className="rounded bg-slate-700 px-3 py-1 text-sm text-white hover:bg-slate-600"
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
