'use client';

import { useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

const FEATURES = [
  {
    href: '/qa',
    title: '可溯源问答',
    desc: '基于论文原文回答，每个答案都能回跳原句',
    color: 'from-indigo-400 to-indigo-500',
    bg: 'bg-indigo-500/15',
    text: 'text-indigo-300',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-7 w-7">
        <path strokeLinecap="round" strokeLinejoin="round" d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.76 9.76 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
      </svg>
    ),
  },
  {
    href: '/graph',
    title: '引用图谱',
    desc: '可视化论文引用网络，影响力一目了然',
    color: 'from-indigo-400 to-indigo-500',
    bg: 'bg-indigo-500/15',
    text: 'text-indigo-300',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-7 w-7">
        <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6.75l3.75 3.75M12 5.25a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0zm-7.5 10.5a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zm15 0a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zm-4.5-10.5a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zM7.5 8.25l3 2.25m0 3.75l3-2.25" />
      </svg>
    ),
  },
  {
    href: '/review',
    title: '综述生成',
    desc: '一句话主题，自动生成句句有出处的综述',
    color: 'from-indigo-400 to-indigo-500',
    bg: 'bg-indigo-500/15',
    text: 'text-indigo-300',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-7 w-7">
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9zm3.75 11.625a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" />
      </svg>
    ),
  },
  {
    href: '/writing',
    title: '写作辅助',
    desc: '论文大纲 + 可复现科研图表一站式生成',
    color: 'from-indigo-400 to-indigo-500',
    bg: 'bg-indigo-500/15',
    text: 'text-indigo-300',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-7 w-7">
        <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10" />
      </svg>
    ),
  },
  {
    href: '/eval',
    title: '可信评测',
    desc: '自动运行全套质量检查，可复现可审计',
    color: 'from-indigo-400 to-indigo-500',
    bg: 'bg-indigo-500/15',
    text: 'text-indigo-300',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-7 w-7">
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
    ),
  },
  {
    href: '/qa',
    title: '论文入库',
    desc: '上传 PDF 或导入 arXiv，一键解析入库',
    color: 'from-indigo-400 to-indigo-500',
    bg: 'bg-indigo-500/15',
    text: 'text-indigo-300',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-7 w-7">
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
      </svg>
    ),
  },
];

export default function HomePage() {
  const [file, setFile] = useState<File | null>(null);
  const [arxivId, setArxivId] = useState('');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');

  const scrollToUpload = () => {
    document.getElementById('upload-section')?.scrollIntoView({ behavior: 'smooth' });
  };

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
    <div className="space-y-12">
      {/* Hero 区 */}
      <section className="relative overflow-hidden rounded-3xl border border-white/[0.08] bg-gradient-to-br from-[#1c2845] via-[#1b2440] to-[#161f33] px-8 py-14 shadow-2xl shadow-black/40">
        {/* 装饰光斑 */}
        <div className="pointer-events-none absolute -right-16 -top-16 h-64 w-64 rounded-full bg-indigo-500/20 blur-3xl" />
        <div className="pointer-events-none absolute -bottom-20 -left-10 h-72 w-72 rounded-full bg-indigo-500/10 blur-3xl" />
        {/* 装饰网格圆点 */}
        <svg className="pointer-events-none absolute right-8 top-8 h-32 w-32 text-white/10" viewBox="0 0 100 100" fill="currentColor">
          <circle cx="10" cy="10" r="2" />
          <circle cx="40" cy="10" r="2" />
          <circle cx="70" cy="10" r="2" />
          <circle cx="25" cy="40" r="2" />
          <circle cx="55" cy="40" r="2" />
          <circle cx="85" cy="40" r="2" />
          <circle cx="10" cy="70" r="2" />
          <circle cx="40" cy="70" r="2" />
          <circle cx="70" cy="70" r="2" />
        </svg>

        <div className="relative max-w-2xl">
          <span className="mb-4 inline-flex items-center gap-2 rounded-full border border-indigo-400/30 bg-indigo-400/10 px-4 py-1.5 text-sm font-medium text-indigo-300 backdrop-blur">
            <svg viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4 text-indigo-300">
              <path fillRule="evenodd" d="M10.788 3.21c.448-1.077 1.976-1.077 2.424 0l2.082 5.007 5.404.433c1.164.093 1.636 1.545.749 2.305l-4.117 3.527 1.257 5.273c.271 1.136-.964 2.033-1.96 1.425L12 18.354 7.373 21.18c-.996.608-2.231-.29-1.96-1.425l1.257-5.273-4.117-3.527c-.887-.76-.415-2.212.749-2.305l5.404-.433 2.082-5.006z" clipRule="evenodd" />
            </svg>
            可验证 · 可复现 · 可溯源
          </span>
          <h1 className="text-4xl font-extrabold leading-tight tracking-tight text-white sm:text-5xl">
            让科研检索的每一句
            <br />
            都<span className="bg-gradient-to-r from-indigo-200 to-indigo-300 bg-clip-text text-transparent">有据可查</span>
          </h1>
          <p className="mt-4 text-lg text-slate-300">
            面向学术研究的 AI 助手，融合检索增强生成与可溯源机制，
            从论文解析到综述写作，全链路保证结论可复现、引用可回跳。
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <button
              onClick={scrollToUpload}
              className="btn-press btn-primary rounded-xl px-6 py-3 font-semibold"
            >
              开始使用
            </button>
            <a
              href="/qa"
              className="btn-press rounded-xl border border-white/20 bg-white/5 px-6 py-3 font-semibold text-white backdrop-blur hover:bg-white/10"
            >
              直接提问 →
            </a>
          </div>
        </div>
      </section>

      {/* 六宫格功能导航 */}
      <section>
        <div className="mb-6 flex items-center gap-3">
          <h2 className="text-2xl font-bold text-white">核心能力</h2>
          <span className="h-px flex-1 bg-gradient-to-r from-white/15 to-transparent" />
        </div>
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f) => (
            <a
              key={f.title}
              href={f.href}
              className="group relative overflow-hidden rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-6 transition duration-300 hover:-translate-y-1 hover:border-indigo-400/40 hover:bg-[#24304d] hover:shadow-xl hover:shadow-indigo-500/10"
            >
              <div className={`absolute inset-x-0 top-0 h-1 bg-gradient-to-r ${f.color}`} />
              <div className={`mb-4 inline-flex h-14 w-14 items-center justify-center rounded-2xl ${f.bg} ${f.text}`}>
                {f.icon}
              </div>
              <h3 className="mb-1.5 text-lg font-bold text-white">{f.title}</h3>
              <p className="text-sm leading-relaxed text-slate-400">{f.desc}</p>
              <span className={`mt-4 inline-flex items-center gap-1 text-sm font-semibold ${f.text} transition group-hover:gap-2`}>
                前往
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-4 w-4">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
                </svg>
              </span>
            </a>
          ))}
        </div>
      </section>

      {/* 上传 / 导入区 */}
      <section id="upload-section" className="scroll-mt-24">
        <div className="mb-6 flex items-center gap-3">
          <h2 className="text-2xl font-bold text-white">论文入库</h2>
          <span className="h-px flex-1 bg-gradient-to-r from-white/15 to-transparent" />
        </div>
        <div className="grid gap-5 md:grid-cols-2">
          <div className="rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-6">
            <div className="mb-4 flex items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-500/15 text-indigo-300">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-5 w-5">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m6.75 12H9m1.5-3H9m7.5 6H5.625c-.621 0-1.125-.504-1.125-1.125V4.125C4.5 3.504 5.004 3 5.625 3h7.875c.298 0 .585.119.796.33l4.5 4.5c.211.211.33.498.33.796v10.5c0 .621-.504 1.125-1.125 1.125z" />
                </svg>
              </span>
              <h3 className="font-semibold text-white">上传本地 PDF</h3>
            </div>
            <input
              type="file"
              accept="application/pdf"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
              className="block w-full rounded-xl border border-white/10 bg-[#1c2740] p-2 text-sm text-slate-300 outline-none transition file:mr-3 file:rounded-lg file:border-0 file:bg-indigo-500/15 file:px-3 file:py-1.5 file:text-sm file:font-semibold file:text-indigo-300 hover:file:bg-indigo-500/25 focus:border-indigo-400 focus:shadow-ring"
            />
            <button
              onClick={uploadPdf}
              disabled={!file || loading}
              className="btn-press btn-primary mt-4 rounded-xl px-4 py-2 font-medium"
            >
                {loading ? '上传中...' : '上传并解析'}
              </button>
            </div>

            <div className="rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-6">
              <div className="mb-4 flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-500/15 text-indigo-300">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-5 w-5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m-18.432 0A8.959 8.959 0 013 12c0-.778.099-1.533.284-2.253" />
                  </svg>
                </span>
                <h3 className="font-semibold text-white">导入 arXiv 论文</h3>
              </div>
              <input
                type="text"
                placeholder="输入 arXiv ID，例如 2401.12345"
                value={arxivId}
                onChange={(e) => setArxivId(e.target.value)}
                className="block w-full rounded-xl border border-white/10 bg-[#1c2740] p-2 text-sm text-slate-300 outline-none transition placeholder:text-slate-500 focus:border-indigo-400 focus:shadow-ring"
              />
              <button
                onClick={importArxiv}
                disabled={!arxivId || loading}
                className="btn-press btn-primary mt-4 rounded-xl px-4 py-2 font-medium"
              >
                {loading ? '导入中...' : '拉取并解析'}
              </button>
            </div>
          </div>

        {message && (
          <div className="mt-5 rounded-xl border border-indigo-400/30 bg-indigo-400/10 p-4 text-sm text-indigo-200">
            {message}
          </div>
        )}
      </section>

      {/* 底部工作流示意 */}
      <section className="rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-8">
        <h2 className="mb-6 text-center text-2xl font-bold text-white">研究工作流</h2>
        <div className="grid gap-4 sm:grid-cols-5">
          {[
            { n: '1', t: '入库', d: '上传/导入论文', c: 'text-indigo-300 bg-indigo-500/15' },
            { n: '2', t: '解析', d: '向量化切分', c: 'text-indigo-300 bg-indigo-500/15' },
            { n: '3', t: '检索', d: '混合召回排序', c: 'text-indigo-300 bg-indigo-500/15' },
            { n: '4', t: '生成', d: '带引用回答/综述', c: 'text-indigo-300 bg-indigo-500/15' },
            { n: '5', t: '评测', d: '自动质量审计', c: 'text-indigo-300 bg-indigo-500/15' },
          ].map((s) => (
            <div key={s.n} className="relative flex flex-col items-center text-center">
              <span className={`flex h-12 w-12 items-center justify-center rounded-full text-lg font-bold ${s.c}`}>
                {s.n}
              </span>
              <div className="mt-3 font-semibold text-white">{s.t}</div>
              <div className="mt-1 text-xs text-slate-400">{s.d}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
