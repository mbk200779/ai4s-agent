'use client';

import { useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

export default function HomePage() {
  const [file, setFile] = useState<File | null>(null);
  const [arxivId, setArxivId] = useState('');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');

  const uploadPdf = async () => {
    if (!file) return;
    setLoading(true);
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await fetch(`${API_BASE}/api/v1/papers/upload`, {
        method: 'POST',
        body: form,
      });
      const data = await res.json();
      setMessage(`已接收：${data.paper_id}，任务：${data.task_id}`);
    } catch (e) {
      setMessage('上传失败');
    } finally {
      setLoading(false);
    }
  };

  const importArxiv = async () => {
    if (!arxivId.trim()) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/papers/arxiv/import?arxiv_id=${arxivId}`, {
        method: 'POST',
      });
      const data = await res.json();
      setMessage(`已导入：${data.paper_id}，任务：${data.task_id}`);
    } catch (e) {
      setMessage('导入失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-8">
      <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-6">
        <h2 className="mb-4 text-lg font-semibold">上传本地 PDF</h2>
        <input
          type="file"
          accept="application/pdf"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
          className="block w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
        />
        <button
          onClick={uploadPdf}
          disabled={!file || loading}
          className="mt-4 rounded bg-primary px-4 py-2 font-medium text-white hover:bg-blue-700"
        >
          {loading ? '上传中...' : '上传并解析'}
        </button>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-6">
        <h2 className="mb-4 text-lg font-semibold">导入 arXiv 论文</h2>
        <input
          type="text"
          placeholder="输入 arXiv ID，例如 2401.12345"
          value={arxivId}
          onChange={(e) => setArxivId(e.target.value)}
          className="block w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
        />
        <button
          onClick={importArxiv}
          disabled={!arxivId || loading}
          className="mt-4 rounded bg-primary px-4 py-2 font-medium text-white hover:bg-blue-700"
        >
          {loading ? '导入中...' : '拉取并解析'}
        </button>
      </section>

      {message && (
        <div className="rounded border border-slate-700 bg-slate-800 p-4 text-sm text-slate-200">
          {message}
        </div>
      )}
    </div>
  );
}
