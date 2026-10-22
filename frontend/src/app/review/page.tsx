'use client';

import { useState } from 'react';

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
      alert('生成失败');
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
      <h2 className="text-lg font-semibold">综述生成（句句有出处）</h2>
      <div className="flex gap-2">
        <input
          type="text"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="输入综述主题..."
          className="flex-1 rounded border border-slate-700 bg-white p-3 text-slate-900"
        />
        <button
          onClick={generate}
          disabled={loading}
          className="rounded bg-primary px-6 py-2 font-medium text-white hover:bg-blue-700"
        >
          {loading ? '生成中...' : '生成'}
        </button>
      </div>

      {review && (
        <div className="space-y-4">
          {review.rejected ? (
            <div className="rounded border border-red-800 bg-red-900/20 p-4 text-red-200">
              {review.reject_reason || '无法生成综述'}
            </div>
          ) : (
            <>
              <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-6">
                <div className="mb-4 flex items-center justify-between">
                  <h3 className="font-semibold text-primary">{review.topic}</h3>
                  <button
                    onClick={exportMarkdown}
                    className="rounded bg-slate-700 px-3 py-1 text-sm text-white hover:bg-slate-600"
                  >
                    导出 Markdown
                  </button>
                </div>
                <article className="prose prose-invert max-w-none whitespace-pre-wrap text-slate-200">
                  {review.markdown}
                </article>
              </div>
              <div className="text-sm text-muted">
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
