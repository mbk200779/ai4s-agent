'use client';

import { useEffect, useState } from 'react';

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

  const fetchReports = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/eval/reports?limit=50`);
      const data = await res.json();
      setReports(data.reports || []);
    } catch (e) {
      alert('获取评测报告列表失败');
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
      alert('运行评测失败');
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
      alert('读取报告失败');
    }
  };

  useEffect(() => {
    fetchReports();
  }, []);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">评测报告面板</h2>
        <button
          onClick={runEval}
          disabled={running}
          className="rounded bg-primary px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {running ? '运行中...' : '运行新评测'}
        </button>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="mb-3 font-semibold">历史报告</h3>
          {loading ? (
            <div className="text-sm text-muted">加载中...</div>
          ) : reports.length === 0 ? (
            <div className="text-sm text-muted">暂无报告</div>
          ) : (
            <ul className="space-y-2">
              {reports.map((r) => (
                <li key={r.key}>
                  <button
                    onClick={() => loadReport(r.key)}
                    className={`w-full rounded border p-2 text-left text-sm hover:border-slate-600 ${
                      selected?.key === r.key
                        ? 'border-primary bg-primary/10 text-primary'
                        : 'border-slate-800 text-slate-300'
                    }`}
                  >
                    <div className="truncate font-medium">{r.key.replace(/^eval\//, '')}</div>
                    <div className="text-xs text-muted">{r.last_modified || ''}</div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="md:col-span-2 rounded-xl border border-slate-800 bg-slate-900/50 p-4">
          <h3 className="mb-3 font-semibold">报告详情</h3>
          {selected ? (
            <div className="space-y-4">
              <div className="text-sm text-muted">
                ID: <span className="text-slate-300">{selected.id}</span>
                <br />
                时间: <span className="text-slate-300">{selected.timestamp}</span>
              </div>
              <div className="space-y-3">
                {Object.entries(selected.checks).map(([name, result]) => (
                  <div
                    key={name}
                    className="rounded border border-slate-800 bg-slate-900/30 p-3 text-sm"
                  >
                    <div className="mb-1 flex items-center gap-2 font-medium">
                      <span
                        className={`inline-block h-2 w-2 rounded-full ${
                          (result as any).ok ? 'bg-green-500' : 'bg-red-500'
                        }`}
                      />
                      {name}
                    </div>
                    <pre className="overflow-auto rounded bg-slate-950 p-2 text-xs text-slate-300">
                      {JSON.stringify(result, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="text-sm text-muted">点击左侧报告或运行新评测查看结果。</div>
          )}
        </div>
      </div>
    </div>
  );
}
