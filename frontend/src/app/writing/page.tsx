'use client';

import { useState } from 'react';
import { useToast } from '@/components/Toast';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

interface OutlineResponse {
  topic: string;
  outline: string;
}

interface SourceInfo {
  arxiv_id?: string;
  title?: string;
  page?: number;
  paragraph?: number;
  text?: string;
}

interface DrawingResponse {
  chart_type: string;
  title: string;
  code: string;
  code_language: string;
  image_url_png?: string;
  image_url_svg?: string;
  sources: SourceInfo[];
  cache_key: string;
  provenance_key: string;
  rejected?: boolean;
  reject_reason?: string;
}

const CHART_TYPES = [
  { value: 'matplotlib_line', label: 'Matplotlib 折线图' },
  { value: 'matplotlib_bar', label: 'Matplotlib 柱状图' },
  { value: 'matplotlib_scatter', label: 'Matplotlib 散点图' },
  { value: 'matplotlib_histogram', label: 'Matplotlib 直方图' },
  { value: 'mermaid_flowchart', label: 'Mermaid 流程图' },
  { value: 'mermaid_sequence', label: 'Mermaid 时序图' },
  { value: 'mermaid_mindmap', label: 'Mermaid 思维导图' },
];

export default function WritingPage() {
  const [activeTab, setActiveTab] = useState<'outline' | 'drawing'>('outline');
  const { toast } = useToast();

  // outline states
  const [topic, setTopic] = useState('');
  const [outline, setOutline] = useState<OutlineResponse | null>(null);
  const [outlineLoading, setOutlineLoading] = useState(false);

  // drawing states
  const [chartType, setChartType] = useState('matplotlib_line');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [dataText, setDataText] = useState('');
  const [query, setQuery] = useState('');
  const [sourcesText, setSourcesText] = useState('');
  const [drawing, setDrawing] = useState<DrawingResponse | null>(null);
  const [drawingLoading, setDrawingLoading] = useState(false);

  const generateOutline = async () => {
    if (!topic.trim()) return;
    setOutlineLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/writing/outline`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, top_k: 8 }),
      });
      const data: OutlineResponse = await res.json();
      setOutline(data);
    } catch (e) {
      toast('生成失败，请检查后端服务', 'error');
    } finally {
      setOutlineLoading(false);
    }
  };

  const generateDrawing = async () => {
    if (!title.trim() || !description.trim()) return;
    let data: unknown = null;
    if (dataText.trim()) {
      try {
        data = JSON.parse(dataText);
      } catch {
        toast('结构化数据 JSON 格式错误', 'error');
        return;
      }
    }
    setDrawingLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/drawing/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chart_type: chartType,
          title,
          description,
          data,
          query: query.trim() || undefined,
          sources: sourcesText
            .split(/[,，\n]+/)
            .map((s) => s.trim())
            .filter(Boolean),
          top_k: 6,
          formats: ['png', 'svg'],
        }),
      });
      const respData: DrawingResponse = await res.json();
      setDrawing(respData);
    } catch (e) {
      toast('绘图生成失败，请检查后端服务', 'error');
    } finally {
      setDrawingLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* 页头 */}
      <div className="flex items-center gap-4">
        <span className="flex h-11 w-11 items-center justify-center rounded-lg border border-[var(--line)] bg-[var(--surface)] text-[var(--signal)]">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10" />
          </svg>
        </span>
        <div>
          <span className="eyebrow">Writing & Figures</span>
          <h1 className="display text-2xl">写作辅助</h1>
        </div>
      </div>

      {/* Tab 切换 */}
      <div className="flex gap-1 border-b border-[var(--line)] pb-1">
        <button
          onClick={() => setActiveTab('outline')}
          className={`flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-semibold transition ${
            activeTab === 'outline'
              ? 'border-[var(--signal)] text-[var(--signal)]'
              : 'border-transparent text-[var(--ink-3)] hover:text-[var(--ink)]'
          }`}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" />
          </svg>
          论文大纲
        </button>
        <button
          onClick={() => setActiveTab('drawing')}
          className={`flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-semibold transition ${
            activeTab === 'drawing'
              ? 'border-[var(--signal)] text-[var(--signal)]'
              : 'border-transparent text-[var(--ink-3)] hover:text-[var(--ink)]'
          }`}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
          </svg>
          绘图辅助
        </button>
      </div>

      {activeTab === 'outline' && (
        <div className="space-y-6">
          <div className="flex max-w-xl gap-2">
            <input
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="输入论文主题..."
              className="field flex-1"
              onKeyDown={(e) => e.key === 'Enter' && generateOutline()}
            />
            <button onClick={generateOutline} disabled={outlineLoading} className="btn-primary">
              {outlineLoading ? '生成中...' : '生成大纲'}
            </button>
          </div>

          {outline && (
            <div className="card card-tick p-6">
              <h3 className="display mb-4 text-lg">{outline.topic}</h3>
              <article className="prose max-w-none whitespace-pre-wrap text-[var(--ink)]">
                {outline.outline}
              </article>
            </div>
          )}

          {!outline && (
            <div className="space-y-6">
              {/* 示例主题 */}
              <div className="card card-tick p-6">
                <h3 className="mb-4 flex items-center gap-2 text-base font-semibold text-[var(--ink)]">
                  <svg viewBox="0 0 24 24" fill="none" stroke="var(--signal)" strokeWidth="1.8" className="h-4 w-4">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" />
                  </svg>
                  试试这些主题
                </h3>
                <div className="flex flex-wrap gap-2">
                  {[
                    '大模型幻觉缓解方法',
                    '检索增强生成（RAG）综述',
                    '科学文献自动摘要',
                    '知识图谱构建技术',
                    '多模态学习在科学发现中的应用',
                  ].map((t) => (
                    <button
                      key={t}
                      onClick={() => setTopic(t)}
                      className="rounded-full border border-[var(--line-strong)] bg-[var(--surface-2)] px-4 py-2 text-sm font-medium text-[var(--ink-2)] transition hover:border-[var(--signal)] hover:bg-[var(--signal-soft)] hover:text-[var(--signal-hover)]"
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </div>

              {/* 使用步骤 */}
              <div className="grid gap-4 sm:grid-cols-3">
                {[
                  {
                    n: '1',
                    t: '先入库文献',
                    d: '在首页上传 PDF 或导入 arXiv 论文，作为大纲的素材库',
                  },
                  {
                    n: '2',
                    t: '输入主题',
                    d: '输入研究方向或论文主题，可点击上方示例快速填入',
                  },
                  {
                    n: '3',
                    t: '生成大纲',
                    d: '系统检索文献库，生成章节结构清晰的研究大纲',
                  },
                ].map((s) => (
                  <div key={s.n} className="card card-tick p-5">
                    <span className="mb-3 flex h-9 w-9 items-center justify-center rounded-full border border-[var(--signal)] bg-[var(--signal-soft)] mono text-sm font-bold text-[var(--signal)]">
                      {s.n}
                    </span>
                    <h4 className="mb-1 font-semibold text-[var(--ink)]">{s.t}</h4>
                    <p className="text-sm leading-relaxed text-[var(--ink-2)]">{s.d}</p>
                  </div>
                ))}
              </div>

              {/* 提示条 */}
              <div className="flex items-start gap-3 rounded-lg border border-[var(--warn)] bg-amber-50 p-4 text-sm text-[var(--ink)]">
                <svg viewBox="0 0 24 24" fill="none" stroke="var(--warn)" strokeWidth="1.8" className="mt-0.5 h-5 w-5 shrink-0">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                </svg>
                <div>
                  <span className="font-semibold">提示：</span>
                  文献库越丰富，大纲质量越高。如果文献库中没有相关论文，系统会明确告知而不是凭空编造——这正是可溯源设计的核心。
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {activeTab === 'drawing' && (
        <div className="space-y-6">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-4">
              <div>
                <label className="mb-1 block text-sm text-[var(--ink-2)]">图表类型</label>
                <select
                  value={chartType}
                  onChange={(e) => setChartType(e.target.value)}
                  className="field"
                >
                  {CHART_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-sm text-[var(--ink-2)]">标题</label>
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="例如：模型准确率随训练轮数变化"
                  className="field"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-[var(--ink-2)]">数据/结论描述</label>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={4}
                  placeholder="描述要表达的数据、坐标轴含义、数据趋势等..."
                  className="field"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-[var(--ink-2)]">
                  结构化数据（可选，JSON）
                </label>
                <textarea
                  value={dataText}
                  onChange={(e) => setDataText(e.target.value)}
                  rows={4}
                  placeholder='{"x": [1,2,3], "y": [0.8,0.9,0.95]}'
                  className="field mono"
                />
              </div>
            </div>
            <div className="space-y-4">
              <div>
                <label className="mb-1 block text-sm text-[var(--ink-2)]">检索查询（可选）</label>
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="从文献库检索支撑片段，用于自动标注来源"
                  className="field"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-[var(--ink-2)]">
                  显式来源 arXiv ID（可选，逗号/换行分隔）
                </label>
                <textarea
                  value={sourcesText}
                  onChange={(e) => setSourcesText(e.target.value)}
                  rows={3}
                  placeholder="2401.12345, 2402.67890"
                  className="field mono"
                />
              </div>
              <button
                onClick={generateDrawing}
                disabled={drawingLoading}
                className="btn-primary w-full"
              >
                {drawingLoading ? '生成中...' : '生成图表'}
              </button>
            </div>
          </div>

          {drawing?.rejected && (
            <div className="rounded-lg border border-[var(--danger)] bg-red-50 p-4 text-[var(--danger)]">
              生成被拒绝：{drawing.reject_reason}
            </div>
          )}

          {drawing && !drawing.rejected && (
            <div className="card card-tick space-y-4 p-6">
              <div className="flex items-center justify-between">
                <h3 className="display text-lg">{drawing.title}</h3>
                <div className="flex gap-2">
                  {drawing.image_url_png && (
                    <a
                      href={`${API_BASE}${drawing.image_url_png}`}
                      download={`${drawing.cache_key}.png`}
                      className="btn-ghost px-3 py-1 text-sm"
                    >
                      下载 PNG
                    </a>
                  )}
                  {drawing.image_url_svg && (
                    <a
                      href={`${API_BASE}${drawing.image_url_svg}`}
                      download={`${drawing.cache_key}.svg`}
                      className="btn-ghost px-3 py-1 text-sm"
                    >
                      下载 SVG
                    </a>
                  )}
                </div>
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                {drawing.image_url_png && (
                  <div>
                    <p className="mb-1 text-sm text-[var(--ink-3)]">PNG 预览</p>
                    <img
                      src={`${API_BASE}${drawing.image_url_png}`}
                      alt={drawing.title}
                      className="rounded-lg border border-[var(--line)] bg-white"
                    />
                  </div>
                )}
                <div className={drawing.image_url_png ? '' : 'md:col-span-2'}>
                  <p className="mb-1 text-sm text-[var(--ink-3)]">生成代码</p>
                  <pre className="mono max-h-96 overflow-auto rounded-lg bg-[var(--surface-3)] p-4 text-xs text-[var(--ink)]">
                    <code>{drawing.code}</code>
                  </pre>
                </div>
              </div>

              {drawing.sources.length > 0 && (
                <div>
                  <p className="mb-1 text-sm text-[var(--ink-3)]">数据来源</p>
                  <ul className="list-disc space-y-1 pl-5 text-sm text-[var(--ink-2)]">
                    {drawing.sources.map((s, idx) => (
                      <li key={idx}>
                        {s.arxiv_id ? `arXiv:${s.arxiv_id}` : '未知来源'}
                        {s.page !== undefined && ` p.${s.page}`}
                        {s.text && ` — ${s.text}`}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
