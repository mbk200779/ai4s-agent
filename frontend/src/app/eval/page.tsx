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
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-500/15 text-indigo-300">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-6 w-6">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </span>
          <div>
            <h2 className="text-xl font-bold text-white">评测报告面板</h2>
            <p className="text-sm text-slate-400">自动运行全套质量检查，可复现可审计</p>
          </div>
        </div>
        <button
          onClick={runEval}
          disabled={running}
          className="btn-press btn-primary rounded-xl px-4 py-2 font-medium disabled:opacity-50"
        >
          {running ? '运行中...' : '运行新评测'}
        </button>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        <div className="rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-4">
          <h3 className="mb-3 font-semibold text-white">历史报告</h3>
          {loading ? (
            <div className="text-sm text-slate-500">加载中...</div>
          ) : reports.length === 0 ? (
            <div className="text-sm text-slate-500">暂无报告</div>
          ) : (
            <ul className="space-y-2">
              {reports.map((r) => (
                <li key={r.key}>
                  <button
                    onClick={() => loadReport(r.key)}
                    className={`w-full rounded-xl border p-2 text-left text-sm transition hover:border-white/20 ${
                      selected?.key === r.key
                        ? 'border-indigo-400/50 bg-indigo-500/15 text-indigo-200'
                        : 'border-white/[0.08] text-slate-400'
                    }`}
                  >
                    <div className="truncate font-medium">{r.key.replace(/^eval\//, '')}</div>
                    <div className="text-xs text-slate-500">{r.last_modified || ''}</div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="md:col-span-2 rounded-2xl border border-white/[0.08] bg-[#1c2740]/80 p-4">
          <h3 className="mb-3 font-semibold text-white">报告详情</h3>
          {selected ? (
            <div className="space-y-4">
              <div className="text-sm text-slate-400">
                ID: <span className="text-slate-200">{selected.id}</span>
                <br />
                时间: <span className="text-slate-200">{selected.timestamp}</span>
              </div>
              <div className="space-y-3">
                {Object.entries(selected.checks).map(([name, result]) => (
                  <div
                    key={name}
                    className="rounded-xl border border-white/[0.08] bg-[#1c2740] p-3 text-sm"
                  >
                    <div className="mb-1 flex items-center gap-2 font-medium text-slate-200">
                      <span
                        className={`inline-block h-2 w-2 rounded-full ${
                          (result as any).ok ? 'bg-emerald-400' : 'bg-red-400'
                        }`}
                      />
                      {name}
                    </div>
                    <pre className="overflow-auto rounded-lg bg-[#161f33] p-2 text-xs text-slate-300">
                      {JSON.stringify(result, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="text-sm text-slate-500">点击左侧报告或运行新评测查看结果。</div>
          )}
        </div>
      </div>
    </div>
  );
}
