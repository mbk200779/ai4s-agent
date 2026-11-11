'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import SuggestionChips from '@/components/SuggestionChips';
import { useToast } from '@/components/Toast';
import DraftBody from '@/components/writing/DraftBody';
import ReferenceList from '@/components/writing/ReferenceList';
import SourcePanel from '@/components/writing/SourcePanel';
import { DROP_REASON_LABEL, WritingResponse, WritingSentence } from '@/types/writing';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

const STAGES = ['规划段落', '检索证据', '逐段写作', '校验引用与文献'];

interface DrawingResponse {
  chart_type: string;
  title: string;
  code: string;
  code_language: string;
  image_url_png?: string;
  image_url_svg?: string;
  sources: { arxiv_id?: string; title?: string; page?: number; text?: string }[];
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

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div>
      <p className="text-[11px] text-[var(--ink-3)]">{label}</p>
      <p className={`mono mt-0.5 text-lg ${tone || 'text-[var(--ink)]'}`}>{value}</p>
    </div>
  );
}

export default function WritingPage() {
  const [activeTab, setActiveTab] = useState<'draft' | 'drawing'>('draft');
  const { toast } = useToast();

  // ---- 初稿
  const [topic, setTopic] = useState('');
  const [nSections, setNSections] = useState(4);
  const [draft, setDraft] = useState<WritingResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [selected, setSelected] = useState<{ sentence: WritingSentence; cite: number | null } | null>(
    null,
  );
  const topicRef = useRef<HTMLInputElement>(null);

  // ---- 绘图（沿用 04 的实现，不动）
  const [chartType, setChartType] = useState('matplotlib_line');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [dataText, setDataText] = useState('');
  const [query, setQuery] = useState('');
  const [sourcesText, setSourcesText] = useState('');
  const [drawing, setDrawing] = useState<DrawingResponse | null>(null);
  const [drawingLoading, setDrawingLoading] = useState(false);

  // 写作要走「规划 → 检索 → 逐段写 → 校验」四步，实测 25~40 秒。
  // 没有进度事件可订阅（接口一次性返回），只报"已经等了多久"，不假装知道进度。
  useEffect(() => {
    if (!loading) return;
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 500);
    return () => window.clearInterval(id);
  }, [loading]);

  const generateDraft = async () => {
    if (!topic.trim()) return;
    setLoading(true);
    setDraft(null);
    setSelected(null);
    setElapsed(0);
    try {
      const res = await fetch(`${API_BASE}/api/v1/writing/draft`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, n_sections: nSections }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: WritingResponse = await res.json();
      setDraft(data);
      const first = data.sections.find((s) => !s.placeholder && s.sentences.length > 0);
      if (first) setSelected({ sentence: first.sentences[0], cite: null });
    } catch (e) {
      toast('生成失败，请检查后端服务', 'error');
    } finally {
      setLoading(false);
    }
  };

  const exportMarkdown = async () => {
    if (!draft?.markdown) return;
    const res = await fetch(`${API_BASE}/api/v1/writing/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic: draft.topic, markdown: draft.markdown }),
    });
    // 不查状态码就会把 500 的 "Internal Server Error" 当 .md 存下来，
    // 用户以为导出成功、打开却是一行英文——比直接报错更糟。
    if (!res.ok) {
      toast('导出失败，请重试', 'error');
      return;
    }
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${draft.topic.replace(/\s+/g, '_')}.md`;
    a.click();
    window.URL.revokeObjectURL(url);
  };

  const exportJson = async () => {
    if (!draft) return;
    const res = await fetch(`${API_BASE}/api/v1/writing/export/json`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic: draft.topic, payload: draft }),
    });
    if (!res.ok) {
      toast('导出溯源 JSON 失败，请重试', 'error');
      return;
    }
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${draft.topic.replace(/\s+/g, '_')}.trace.json`;
    a.click();
    window.URL.revokeObjectURL(url);
  };

  /** 从参考文献/映射表里点 `引#N`：找到引它的那句并选中，面板落到同一个 N 上。 */
  const selectByCite = (n: number) => {
    if (!draft) return;
    for (const section of draft.sections) {
      for (const sentence of section.sentences) {
        if (sentence.cite_nos.includes(n)) {
          setSelected({ sentence, cite: n });
          return;
        }
      }
    }
  };

  const stats = draft?.stats;
  const citedMap = useMemo(
    () => (draft?.ref_map || []).filter((r) => draft?.sections.some((s) => s.sentences.some((x) => x.cite_nos.includes(r.n)))),
    [draft],
  );
  const dropReasons = Object.entries(stats?.dropped_by_reason || {});

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
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10"
            />
          </svg>
        </span>
        <div>
          <h1 className="display text-3xl">写作辅助</h1>
          <p className="mt-1 text-sm text-[var(--ink-2)]">
            每一句都带来源：结论句标 <span className="mono text-[var(--signal)]">[引#N]</span>，
            过渡句标「无需引用」，参考文献只由库内记录生成。
          </p>
        </div>
      </div>

      {/* Tab */}
      <div className="flex gap-1 border-b border-[var(--line)] pb-1">
        {[
          { key: 'draft' as const, label: '论文初稿', icon: 'M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z' },
          { key: 'drawing' as const, label: '绘图辅助', icon: 'M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z' },
        ].map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-semibold transition ${
              activeTab === tab.key
                ? 'border-[var(--signal)] text-[var(--signal)]'
                : 'border-transparent text-[var(--ink-3)] hover:text-[var(--ink)]'
            }`}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
              <path strokeLinecap="round" strokeLinejoin="round" d={tab.icon} />
            </svg>
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'draft' && (
        <div className="space-y-6">
          <div className="flex flex-wrap gap-2">
            <input
              ref={topicRef}
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !loading) generateDraft();
              }}
              placeholder="输入写作主题，例如：注意力架构的能力边界"
              className="field min-w-[280px] flex-1"
            />
            <select
              value={nSections}
              onChange={(e) => setNSections(Number(e.target.value))}
              className="field w-28"
              title="段落数"
            >
              {[3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  {n} 段
                </option>
              ))}
            </select>
            <button onClick={generateDraft} disabled={loading} className="btn-primary whitespace-nowrap">
              {loading ? `生成中 ${elapsed}s` : '生成初稿'}
            </button>
            {draft && !draft.rejected && (
              <>
                <button onClick={exportMarkdown} className="btn-ghost whitespace-nowrap">
                  导出 Markdown
                </button>
                <button onClick={exportJson} className="btn-ghost whitespace-nowrap">
                  导出溯源 JSON
                </button>
              </>
            )}
          </div>

          {!draft && !loading && (
            <SuggestionChips
              title="试试这些主题"
              items={[
                '注意力架构的能力边界：从 Transformer 到 ViT',
                '参数高效微调：LoRA 的能力边界',
                'BERT 到 ViT：预训练范式的迁移',
                'Transformer 的并行化与训练成本',
                '量子纠错码的表面码容错阈值',
              ]}
              onPick={(t) => {
                setTopic(t);
                topicRef.current?.focus();
              }}
            />
          )}

          {loading && (
            <div className="card p-5">
              <p className="text-sm text-[var(--ink-2)]">
                已等待 <span className="mono text-[var(--signal)]">{elapsed}s</span>
                ，写作比问答慢：要规划段落、逐段检索证据、逐句写作，再对每句做引用与数值校验。
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                {STAGES.map((s, i) => (
                  <span
                    key={s}
                    className="mono rounded border border-[var(--line)] px-2 py-0.5 text-[11px] text-[var(--ink-3)]"
                  >
                    {i + 1}. {s}
                  </span>
                ))}
              </div>
            </div>
          )}

          {draft && (
            <>
              {/* 概览 */}
              <div className="card card-tick p-5">
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
                  <Stat label="段落" value={stats?.sections ?? 0} />
                  <Stat label="句子" value={stats?.sentences ?? 0} />
                  <Stat label="结论句" value={stats?.claim_sentences ?? 0} />
                  <Stat label="连接句" value={stats?.bridge_sentences ?? 0} />
                  <Stat label="参考文献" value={stats?.refs ?? 0} />
                  <Stat
                    label="无证据占位"
                    value={stats?.placeholders ?? 0}
                    tone={(stats?.placeholders ?? 0) > 0 ? 'text-[var(--warn)]' : undefined}
                  />
                  <Stat
                    label="被筛掉的句子"
                    value={stats?.sentences_dropped ?? 0}
                    tone={(stats?.sentences_dropped ?? 0) > 0 ? 'text-[var(--ink-2)]' : undefined}
                  />
                </div>
                {dropReasons.length > 0 && (
                  <p className="mt-3 text-xs text-[var(--ink-3)]">
                    筛除原因：
                    {dropReasons
                      .map(([k, v]) => `${DROP_REASON_LABEL[k] || k} ${v}`)
                      .join(' · ')}
                  </p>
                )}
              </div>

              {draft.rejected && (
                <div className="rounded-lg border border-[var(--danger)]/40 bg-red-50 p-4 text-sm text-[var(--danger)]">
                  <p className="font-semibold">本次没有生成正文</p>
                  <p className="mt-1 text-[var(--ink-2)]">{draft.reject_reason}</p>
                  <p className="mt-1 text-xs text-[var(--ink-3)]">
                    按约定：宁可留空占位，也不写没有依据的句子。下面依然给出每段缺什么。
                  </p>
                </div>
              )}

              {(stats?.fabricated_refs ?? 0) > 0 && (
                <div className="rounded-lg border border-[var(--danger)]/40 bg-red-50 p-4 text-sm text-[var(--danger)]">
                  <p className="font-semibold">
                    已从正文删除 {stats?.fabricated_refs} 处库外文献标识
                  </p>
                  <p className="mt-1 text-xs text-[var(--ink-2)]">
                    模型写进正文的 arXiv 号 / DOI 不属于任何一条库内参考文献，已按约定清理。
                  </p>
                </div>
              )}

              {(stats?.refs_rejected ?? 0) > 0 && (
                <div className="rounded-lg border border-[var(--warn)]/50 bg-amber-50 p-4 text-sm text-[var(--warn)]">
                  <p className="font-semibold">有 {stats?.refs_rejected} 条参考文献复核未通过，已拒收</p>
                  <p className="mt-1 text-xs text-[var(--ink-2)]">
                    生成时引用的论文在 <span className="mono">papers</span> 表里查不到，或标题对不上。
                    宁可少一条，也不留来路不明的条目。
                  </p>
                </div>
              )}

              {(stats?.uncited_claim ?? 0) > 0 && (
                <div className="rounded-lg border border-[var(--danger)]/40 bg-red-50 p-3 text-xs text-[var(--danger)]">
                  发现 {stats?.uncited_claim} 句结论没有引用——这是渲染器不该放过的状态，请报告。
                </div>
              )}

              {draft.degraded && (
                <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-3 text-xs text-[var(--ink-2)]">
                  本轮有段落走了降级路径（模型未返回可解析 JSON，改用原文摘录），
                  文本为英文原句，但每句都能查到出处。
                </div>
              )}

              {/* 正文 + 来源面板 */}
              {draft.sections.length > 0 && (
                <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
                  <div className="card card-tick p-6">
                    <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
                      <h2 className="display text-xl">{draft.topic}</h2>
                      <span className="mono text-[11px] text-[var(--ink-3)]">
                        {stats?.prompt_version} · 配置 {stats?.config_version}/{stats?.fingerprint}
                      </span>
                    </div>
                    <DraftBody
                      sections={draft.sections}
                      activeSentence={selected?.sentence.id ?? null}
                      activeCite={selected?.cite ?? null}
                      onSelect={(sentence, cite) =>
                        setSelected({ sentence, cite: cite ?? null })
                      }
                    />
                  </div>

                  <SourcePanel
                    sentence={selected?.sentence ?? null}
                    activeCite={selected?.cite ?? null}
                    onPickCite={(n) => selected && setSelected({ sentence: selected.sentence, cite: n })}
                    references={draft.references}
                  />
                </div>
              )}

              {draft.references.length > 0 && (
                <ReferenceList
                  references={draft.references}
                  refMap={citedMap}
                  stats={draft.stats}
                  onPickCite={selectByCite}
                />
              )}
            </>
          )}

          {!draft && !loading && (
            <div className="grid gap-4 sm:grid-cols-3">
              {[
                { n: '1', t: '先入库文献' },
                { n: '2', t: '生成初稿' },
                { n: '3', t: '逐句核对' },
              ].map((s) => (
                <div key={s.n} className="card card-tick p-5">
                  <span className="mono mb-3 flex h-9 w-9 items-center justify-center rounded-full border border-[var(--signal)] bg-[var(--signal-soft)] text-sm font-bold text-[var(--signal)]">
                    {s.n}
                  </span>
                  <h4 className="font-semibold text-[var(--ink)]">{s.t}</h4>
                </div>
              ))}
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
                <select value={chartType} onChange={(e) => setChartType(e.target.value)} className="field">
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
              <button onClick={generateDrawing} disabled={drawingLoading} className="btn-primary w-full">
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
                    {/* eslint-disable-next-line @next/next/no-img-element */}
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
