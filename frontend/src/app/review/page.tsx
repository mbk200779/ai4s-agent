'use client';

import { useState } from 'react';
import { useToast } from '@/components/Toast';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

interface ReviewResponse {
  topic: string;
  markdown: string;
  rejected: boolean;
  reject_reason?: string;
  invalid_citations: string[];
  referenced_arxiv_ids: string[];
  export_key?: string;
}

export default function ReviewPage() {
  const [topic, setTopic] = useState('');
  const [review, setReview] = useState<ReviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const { toast } = useToast();

  const generate = async () => {
    if (!topic.trim()) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/review/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, top_k: 10 }),
      });
      const data: ReviewResponse = await res.json();
      setReview(data);
    } catch (e) {
      toast('生成失败，请检查后端服务', 'error');
    } finally {
      setLoading(false);
    }
  };

  const exportMarkdown = async () => {
    if (!review || !review.markdown) return;
    const res = await fetch(`${API_BASE}/api/v1/review/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic: review.topic, markdown: review.markdown }),
    });
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${review.topic.replace(/\s+/g, '_')}.md`;
    a.click();
    window.URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-500/15 text-indigo-300">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-6 w-6">
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9zm3.75 11.625a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" />
          </svg>
        </span>
        <div>
          <h2 className="text-xl font-bold text-white">综述生成</h2>
          <p className="text-sm text-slate-400">一句话主题，自动生成句句有出处的综述</p>
        </div>
      </div>
      <div className="flex gap-2">
        <input
          type="text"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="输入综述主题..."
          className="flex-1 rounded-xl border border-white/10 bg-[#1c2740] p-3 text-slate-200 outline-none transition placeholder:text-slate-500 focus:border-indigo-400 focus:shadow-ring"
        />
        <button
          onClick={generate}
          disabled={loading}
          className="btn-press btn-primary rounded-xl px-6 py-2 font-medium"
        >
          {loading ? '生成中...' : '生成'}
        </button>
      </div>

      {review && (
        <div className="space-y-4">
          {review.rejected ? (
            <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-300">
              {review.reject_reason || '无法生成综述'}
            </div>
          ) : (
            <>
              <div className="rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-6">
                <div className="mb-4 flex items-center justify-between">
                  <h3 className="font-semibold text-indigo-300">{review.topic}</h3>
                  <button
                    onClick={exportMarkdown}
                    className="rounded-lg bg-white/10 px-3 py-1 text-sm text-slate-200 hover:bg-white/20"
                  >
                    导出 Markdown
                  </button>
                </div>
                <article className="prose max-w-none whitespace-pre-wrap text-slate-200">
                  {review.markdown}
                </article>
              </div>
              <div className="text-sm text-slate-400">
                引用论文：{review.referenced_arxiv_ids.join(', ') || '无'}
                {review.invalid_citations.length > 0 && (
                  <span className="ml-4 text-red-400">
                    无效引用：{review.invalid_citations.join(', ')}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
