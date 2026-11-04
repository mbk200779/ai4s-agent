'use client';

import { useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

// 功能入口（六宫格 → 改为带编号的仪器式列表）
const FEATURES = [
  {
    href: '/qa',
    code: '01',
    title: '可溯源问答',
    desc: '基于论文原文回答，每个答案都能回跳原句',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4">
        <path strokeLinecap="round" strokeLinejoin="round" d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.76 9.76 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
      </svg>
    ),
  },
  {
    href: '/graph',
    code: '02',
    title: '引用图谱',
    desc: '可视化论文引用网络，影响力一目了然',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4">
        <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6.75l3.75 3.75M12 5.25a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0zm-7.5 10.5a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zm15 0a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zm-4.5-10.5a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zM7.5 8.25l3 2.25m0 3.75l3-2.25" />
      </svg>
    ),
  },
  {
    href: '/review',
    code: '03',
    title: '综述生成',
    desc: '一句话主题，自动生成句句有出处的综述',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4">
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9zm3.75 11.625a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" />
      </svg>
    ),
  },
  {
    href: '/writing',
    code: '04',
    title: '写作辅助',
    desc: '论文大纲 + 可复现科研图表一站式生成',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4">
        <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10" />
      </svg>
    ),
  },
  {
    href: '/eval',
    code: '05',
    title: '可信评测',
    desc: '自动运行全套质量检查，可复现可审计',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4">
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
    ),
  },
  {
    href: '/qa',
    code: '06',
    title: '论文入库',
    desc: '上传 PDF 或导入 arXiv，一键解析入库',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4">
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
      </svg>
    ),
  },
];

// 研究工作流（底部）
const WORKFLOW = [
  { n: '01', t: '入库', d: '上传 / 导入论文' },
  { n: '02', t: '解析', d: '结构化切分向量化' },
  { n: '03', t: '检索', d: '混合召回 + RRF' },
  { n: '04', t: '生成', d: '带引用回答 / 综述' },
  { n: '05', t: '评测', d: '自动质量审计' },
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
    <div className="space-y-14">
      {/* ============ Hero ============ */}
      <section className="relative overflow-hidden border border-[var(--line)] bg-[var(--surface)] px-8 py-14">
        <div className="relative max-w-2xl">
          {/* 状态指示 */}
          <div className="mb-5 flex items-center gap-2 mono text-xs text-[var(--ink-3)]">
            <span className="status-dot" />
            <span>SYSTEM ONLINE · v1.0.0</span>
          </div>

          <h1 className="display text-4xl leading-tight sm:text-[3.2rem] sm:leading-[1.15]">
            让科研检索的每一句，
            <br />
            <span className="text-[var(--signal)]">都有据可查</span>
          </h1>

          <p className="mt-5 max-w-xl text-base text-[var(--ink-2)]">
            面向学术研究的 AI 助手，融合检索增强生成与可溯源机制，
            从论文解析到综述写作，全链路保证结论可复现、引用可回跳。
          </p>

          <div className="mt-8 flex flex-wrap items-center gap-3">
            <button onClick={scrollToUpload} className="btn-primary">
              开始使用
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-4 w-4">
                <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
              </svg>
            </button>
            <a href="/qa" className="btn-ghost">
              直接提问
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-4 w-4">
                <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
              </svg>
            </a>
          </div>

          {/* 三个信任指标 */}
          <div className="mt-10 grid max-w-lg grid-cols-3 gap-4 border-t border-[var(--line)] pt-6">
            {[
              { v: '100%', k: '引用可回跳' },
              { v: '0.03', k: '拒答阈值 RRF' },
              { v: 'diff=0', k: '评测可复现' },
            ].map((m) => (
              <div key={m.k}>
                <div className="mono text-xl font-semibold text-[var(--signal)]">{m.v}</div>
                <div className="mt-1 text-xs text-[var(--ink-3)]">{m.k}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ============ 核心能力 ============ */}
      <section>
        <div className="mb-6 flex items-baseline gap-4">
          <span className="eyebrow">Capabilities</span>
          <span className="h-px flex-1 bg-[var(--line)]" />
        </div>
        <div className="grid gap-px bg-[var(--line)] sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f, i) => (
            <a
              key={f.title}
              href={f.href}
              className="group relative bg-[var(--surface)] p-6 transition-colors hover:bg-[var(--signal-soft)]"
              style={{ animationDelay: `${i * 60}ms` }}
            >
              <div className="mb-4 flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-[var(--line)] text-[var(--signal)] transition-colors group-hover:border-[var(--signal)] group-hover:bg-[var(--surface)]">
                    {f.icon}
                  </span>
                  <h3 className="text-lg font-semibold text-[var(--ink)]">{f.title}</h3>
                </div>
                <span className="mono text-xs text-[var(--ink-3)]">{f.code}</span>
              </div>
              <p className="text-base leading-relaxed text-[var(--ink-2)]">{f.desc}</p>
              <span className="mt-5 inline-flex items-center gap-1.5 text-base font-medium text-[var(--signal)]">
                <span className="mono text-sm">进入</span>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-4 w-4 transition-transform group-hover:translate-x-0.5">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
                </svg>
              </span>
            </a>
          ))}
        </div>
      </section>

      {/* ============ 论文入库 ============ */}
      <section id="upload-section" className="scroll-mt-24">
        <div className="mb-6 flex items-baseline gap-4">
          <span className="eyebrow">Ingest</span>
          <span className="h-px flex-1 bg-[var(--line)]" />
        </div>
        <div className="grid gap-6 md:grid-cols-2">
          {/* 上传 PDF */}
          <div className="card card-tick p-6">
            <div className="mb-5 flex items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--line)] text-[var(--signal)]">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m6.75 12H9m1.5-3H9m7.5 6H5.625c-.621 0-1.125-.504-1.125-1.125V4.125C4.5 3.504 5.004 3 5.625 3h7.875c.298 0 .585.119.796.33l4.5 4.5c.211.211.33.498.33.796v10.5c0 .621-.504 1.125-1.125 1.125z" />
                </svg>
              </span>
              <div>
                <h3 className="font-semibold text-[var(--ink)]">上传本地 PDF</h3>
                <div className="mono text-xs text-[var(--ink-3)]">UPLOAD.SOURCE</div>
              </div>
            </div>
            <input
              type="file"
              accept="application/pdf"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
              className="field"
              style={{ padding: '6px' }}
            />
            <button
              onClick={uploadPdf}
              disabled={!file || loading}
              className="btn-primary mt-4"
            >
              {loading ? '上传中…' : '上传并解析'}
            </button>
          </div>

          {/* 导入 arXiv */}
          <div className="card card-tick p-6">
            <div className="mb-5 flex items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--line)] text-[var(--signal)]">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m-18.432 0A8.959 8.959 0 013 12c0-.778.099-1.533.284-2.253" />
                </svg>
              </span>
              <div>
                <h3 className="font-semibold text-[var(--ink)]">导入 arXiv 论文</h3>
                <div className="mono text-xs text-[var(--ink-3)]">IMPORT.ARXIV</div>
              </div>
            </div>
            <input
              type="text"
              placeholder="输入 arXiv ID，例如 2401.12345"
              value={arxivId}
              onChange={(e) => setArxivId(e.target.value)}
              className="field mono"
            />
            <button
              onClick={importArxiv}
              disabled={!arxivId || loading}
              className="btn-primary mt-4"
            >
              {loading ? '导入中…' : '拉取并解析'}
            </button>
          </div>
        </div>

        {message && (
          <div className="mt-5 flex items-start gap-3 border border-[var(--signal)] bg-[var(--signal-soft)] p-4 text-sm text-[var(--ink)]">
            <svg viewBox="0 0 24 24" fill="none" stroke="var(--signal)" strokeWidth="1.8" className="mt-0.5 h-5 w-5 shrink-0">
              <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
            </svg>
            <span className="mono">{message}</span>
          </div>
        )}
      </section>

      {/* ============ 研究工作流 ============ */}
      <section className="card p-8">
        <div className="mb-8 flex items-baseline gap-4">
          <span className="eyebrow">Pipeline</span>
          <span className="h-px flex-1 bg-[var(--line)]" />
        </div>
        <div className="grid gap-0 sm:grid-cols-5">
          {WORKFLOW.map((s, i) => (
            <div
              key={s.n}
              className={`relative flex flex-col gap-3 px-4 py-2 sm:px-5 ${
                i > 0 ? 'sm:border-l sm:border-[var(--line)]' : ''
              }`}
            >
              <span className="mono text-xs text-[var(--signal)]">{s.n}</span>
              <div className="font-semibold text-[var(--ink)]">{s.t}</div>
              <div className="text-xs leading-relaxed text-[var(--ink-3)]">{s.d}</div>
              {i < WORKFLOW.length - 1 && (
                <svg viewBox="0 0 24 24" fill="none" stroke="var(--line-strong)" strokeWidth="1.5" className="absolute -right-3 top-2 hidden h-4 w-4 sm:block">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                </svg>
              )}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
