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
      {/* 页头 */}
      <div className="flex items-center gap-4">
        <span className="flex h-11 w-11 items-center justify-center rounded-lg border border-[var(--line)] bg-[var(--surface)] text-[var(--signal)]">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9zm3.75 11.625a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" />
          </svg>
        </span>
        <div>
          <span className="eyebrow">Survey Generation</span>
          <h1 className="display text-2xl">综述生成</h1>
        </div>
      </div>

      <div className="flex gap-2">
        <input
          type="text"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="输入综述主题..."
          className="field flex-1"
        />
        <button onClick={generate} disabled={loading} className="btn-primary">
          {loading ? '生成中...' : '生成'}
        </button>
      </div>

      {review && (
        <div className="space-y-4">
          {review.rejected ? (
            <div className="rounded-lg border border-[var(--danger)] bg-red-50 p-4 text-[var(--danger)]">
              {review.reject_reason || '无法生成综述'}
            </div>
          ) : (
            <>
              <div className="card card-tick p-6">
                <div className="mb-4 flex items-center justify-between">
                  <h3 className="display text-lg">{review.topic}</h3>
                  <button onClick={exportMarkdown} className="btn-ghost px-3 py-1 text-sm">
                    导出 Markdown
                  </button>
                </div>
                <article className="prose max-w-none whitespace-pre-wrap text-[var(--ink)]">
                  {review.markdown}
                </article>
              </div>
              <div className="mono text-sm text-[var(--ink-3)]">
                引用论文：{review.referenced_arxiv_ids.join(', ') || '无'}
                {review.invalid_citations.length > 0 && (
                  <span className="ml-4 text-[var(--danger)]">
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
