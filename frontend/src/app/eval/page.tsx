'use client';

import { useEffect, useState } from 'react';
import { useToast } from '@/components/Toast';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

interface ReportItem {
  key: string;
  size: number;
  last_modified?: string;
}

interface ReportDetail {
  id: string;
  timestamp: string;
  checks: Record<string, unknown>;
  key?: string;
}

export default function EvalPage() {
  const [reports, setReports] = useState<ReportItem[]>([]);
  const [selected, setSelected] = useState<ReportDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const { toast } = useToast();

  const fetchReports = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/eval/reports?limit=50`);
      const data = await res.json();
      setReports(data.reports || []);
    } catch (e) {
      toast('获取评测报告列表失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const runEval = async () => {
    setRunning(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/eval/run`, { method: 'POST' });
      const report: ReportDetail = await res.json();
      setSelected(report);
      await fetchReports();
    } catch (e) {
      toast('运行评测失败', 'error');
    } finally {
      setRunning(false);
    }
  };

  const loadReport = async (key: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/eval/reports/${encodeURIComponent(key)}`);
      const data: ReportDetail = await res.json();
      setSelected(data);
    } catch (e) {
      toast('读取报告失败', 'error');
    }
  };

  useEffect(() => {
    fetchReports();
  }, []);

  return (
    <div className="space-y-6">
      {/* 页头 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <span className="flex h-11 w-11 items-center justify-center rounded-lg border border-[var(--line)] bg-[var(--surface)] text-[var(--signal)]">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </span>
          <div>
            <span className="eyebrow">Evaluation</span>
            <h1 className="display text-2xl">评测报告面板</h1>
          </div>
        </div>
        <button onClick={runEval} disabled={running} className="btn-primary">
          {running ? '运行中...' : '运行新评测'}
        </button>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        <div className="card card-tick p-4">
          <h3 className="mb-3 font-semibold text-[var(--ink)]">历史报告</h3>
          {loading ? (
            <div className="text-sm text-[var(--ink-3)]">加载中...</div>
          ) : reports.length === 0 ? (
            <div className="text-sm text-[var(--ink-3)]">暂无报告</div>
          ) : (
            <ul className="space-y-2">
              {reports.map((r) => (
                <li key={r.key}>
                  <button
                    onClick={() => loadReport(r.key)}
                    className={`w-full rounded-lg border p-2 text-left text-sm transition ${
                      selected?.key === r.key
                        ? 'border-[var(--signal)] bg-[var(--signal-soft)] text-[var(--signal-hover)]'
                        : 'border-[var(--line)] text-[var(--ink-2)] hover:border-[var(--line-strong)]'
                    }`}
                  >
                    <div className="mono truncate font-medium">{r.key.replace(/^eval\//, '')}</div>
                    <div className="text-xs text-[var(--ink-3)]">{r.last_modified || ''}</div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="card card-tick md:col-span-2 p-4">
          <h3 className="mb-3 font-semibold text-[var(--ink)]">报告详情</h3>
          {selected ? (
            <div className="space-y-4">
              <div className="mono text-sm text-[var(--ink-2)]">
                ID: <span className="text-[var(--ink)]">{selected.id}</span>
                <br />
                时间: <span className="text-[var(--ink)]">{selected.timestamp}</span>
              </div>
              <div className="space-y-3">
                {Object.entries(selected.checks).map(([name, result]) => (
                  <div key={name} className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-3 text-sm">
                    <div className="mb-1 flex items-center gap-2 font-medium text-[var(--ink)]">
                      <span
                        className={`inline-block h-2 w-2 rounded-full ${
                          (result as any).ok ? 'bg-[var(--ok)]' : 'bg-[var(--danger)]'
                        }`}
                      />
                      {name}
                    </div>
                    <pre className="mono overflow-auto rounded-lg bg-[var(--surface-3)] p-2 text-xs text-[var(--ink)]">
                      {JSON.stringify(result, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="text-sm text-[var(--ink-3)]">点击左侧报告或运行新评测查看结果。</div>
          )}
        </div>
      </div>
    </div>
  );
}
