'use client';

import { useState } from 'react';

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
      alert('生成失败');
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
        alert('结构化数据 JSON 格式错误');
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
      alert('绘图生成失败');
    } finally {
      setDrawingLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex gap-4 border-b border-slate-700 pb-2">
        <button
          onClick={() => setActiveTab('outline')}
          className={`font-medium ${activeTab === 'outline' ? 'text-primary' : 'text-slate-400'}`}
        >
          论文大纲
        </button>
        <button
          onClick={() => setActiveTab('drawing')}
          className={`font-medium ${activeTab === 'drawing' ? 'text-primary' : 'text-slate-400'}`}
        >
          绘图辅助
        </button>
      </div>

      {activeTab === 'outline' && (
        <div className="space-y-6">
          <h2 className="text-lg font-semibold">写作辅助：论文大纲</h2>
          <div className="flex gap-2">
            <input
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="输入论文主题..."
              className="flex-1 rounded border border-slate-700 bg-white p-3 text-slate-900"
            />
            <button
              onClick={generateOutline}
              disabled={outlineLoading}
              className="rounded bg-primary px-6 py-2 font-medium text-white hover:bg-blue-700"
            >
              {outlineLoading ? '生成中...' : '生成大纲'}
            </button>
          </div>

          {outline && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-6">
              <h3 className="mb-4 font-semibold text-primary">{outline.topic}</h3>
              <article className="prose prose-invert max-w-none whitespace-pre-wrap text-slate-200">
                {outline.outline}
              </article>
            </div>
          )}
        </div>
      )}

      {activeTab === 'drawing' && (
        <div className="space-y-6">
          <h2 className="text-lg font-semibold">绘图辅助：可复现图表</h2>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-4">
              <div>
                <label className="mb-1 block text-sm text-slate-300">图表类型</label>
                <select
                  value={chartType}
                  onChange={(e) => setChartType(e.target.value)}
                  className="w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
                >
                  {CHART_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-300">标题</label>
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="例如：模型准确率随训练轮数变化"
                  className="w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-300">数据/结论描述</label>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={4}
                  placeholder="描述要表达的数据、坐标轴含义、数据趋势等..."
                  className="w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-300">
                  结构化数据（可选，JSON）
                </label>
                <textarea
                  value={dataText}
                  onChange={(e) => setDataText(e.target.value)}
                  rows={4}
                  placeholder='{"x": [1,2,3], "y": [0.8,0.9,0.95]}'
                  className="w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
                />
              </div>
            </div>
            <div className="space-y-4">
              <div>
                <label className="mb-1 block text-sm text-slate-300">检索查询（可选）</label>
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="从文献库检索支撑片段，用于自动标注来源"
                  className="w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-300">
                  显式来源 arXiv ID（可选，逗号/换行分隔）
                </label>
                <textarea
                  value={sourcesText}
                  onChange={(e) => setSourcesText(e.target.value)}
                  rows={3}
                  placeholder="2401.12345, 2402.67890"
                  className="w-full rounded border border-slate-700 bg-white p-2 text-slate-900"
                />
              </div>
              <button
                onClick={generateDrawing}
                disabled={drawingLoading}
                className="w-full rounded bg-primary px-6 py-2 font-medium text-white hover:bg-blue-700"
              >
                {drawingLoading ? '生成中...' : '生成图表'}
              </button>
            </div>
          </div>

          {drawing?.rejected && (
            <div className="rounded-lg border border-red-700 bg-red-900/30 p-4 text-red-200">
              生成被拒绝：{drawing.reject_reason}
            </div>
          )}

          {drawing && !drawing.rejected && (
            <div className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/50 p-6">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-primary">{drawing.title}</h3>
                <div className="flex gap-2">
                  {drawing.image_url_png && (
                    <a
                      href={drawing.image_url_png}
                      download={`${drawing.cache_key}.png`}
                      className="rounded bg-slate-700 px-3 py-1 text-sm text-white hover:bg-slate-600"
                    >
                      下载 PNG
                    </a>
                  )}
                  {drawing.image_url_svg && (
                    <a
                      href={drawing.image_url_svg}
                      download={`${drawing.cache_key}.svg`}
                      className="rounded bg-slate-700 px-3 py-1 text-sm text-white hover:bg-slate-600"
                    >
                      下载 SVG
                    </a>
                  )}
                </div>
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                {drawing.image_url_png && (
                  <div>
                    <p className="mb-1 text-sm text-slate-400">PNG 预览</p>
                    <img
                      src={drawing.image_url_png}
                      alt={drawing.title}
                      className="rounded border border-slate-700 bg-white"
                    />
                  </div>
                )}
                <div className={drawing.image_url_png ? '' : 'md:col-span-2'}>
                  <p className="mb-1 text-sm text-slate-400">生成代码</p>
                  <pre className="max-h-96 overflow-auto rounded bg-slate-950 p-4 text-xs text-slate-200">
                    <code>{drawing.code}</code>
                  </pre>
                </div>
              </div>

              {drawing.sources.length > 0 && (
                <div>
                  <p className="mb-1 text-sm text-slate-400">数据来源</p>
                  <ul className="list-disc space-y-1 pl-5 text-sm text-slate-300">
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
