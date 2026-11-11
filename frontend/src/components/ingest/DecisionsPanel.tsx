'use client';

/**
 * 判定记录面板。
 *
 * 存在的意义不是"再看一遍弹窗"，而是让用户确认一件事：**这次的选择被记住了**。
 * 所以每行必须能看到三样东西：判定成了什么关系、当时选了什么、以后还问不问。
 * `记录方式` 显示 user / remembered —— remembered 表示这条是重传同一对内容时
 * 系统依据历史选择自动套用的，不会再弹窗。
 */

import { useEffect, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

interface DecisionRow {
  id: string;
  relation: string;
  relation_label: string;
  action: string;
  confidence: number;
  new_label: string;
  peer_label: string;
  new_paper_title?: string | null;
  peer_paper_title?: string | null;
  decided_by: string;
  suppress: boolean;
  created_at: string;
}

// 同一 action 在不同关系下含义不同，标签必须跟着关系走
const ACTION_LABEL: Record<string, Record<string, string>> = {
  version_of: { keep_new: '设为当前版本', keep_both: '两版并存', link_only: '跳过本次' },
  possible_duplicate: { keep_new: '合并为同一工作', keep_both: '作为两篇并存', link_only: '仅标注关联' },
  related: { keep_both: '正常入库', link_only: '入库并标注' },
};

function actionLabel(relation: string, action: string) {
  return ACTION_LABEL[relation]?.[action] || action;
}

function relationTone(relation: string) {
  if (relation === 'version_of') return { fg: 'var(--signal)', bg: 'var(--signal-soft)' };
  if (relation === 'possible_duplicate') return { fg: 'var(--warn)', bg: '#fdf3e3' };
  return { fg: 'var(--ink-2)', bg: 'var(--surface-2)' };
}

export default function DecisionsPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const [rows, setRows] = useState<DecisionRow[]>([]);

  useEffect(() => {
    let alive = true;
    fetch(`${API_BASE}/api/v1/dedupe/decisions?limit=6`)
      .then((r) => (r.ok ? r.json() : { rows: [] }))
      .then((d) => alive && setRows(d.rows || []))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  if (rows.length === 0) return null;

  return (
    <div className="mt-5 border border-[var(--line)]">
      <div className="flex items-center justify-between border-b border-[var(--line)] bg-[var(--surface-2)] px-3 py-2">
        <span className="mono text-[10px] uppercase tracking-wider text-[var(--ink-3)]">
          入库判定记录
        </span>
        <span className="mono text-[10px] text-[var(--ink-3)]">
          同一对内容再次上传时按记录处理，不再弹窗
        </span>
      </div>
      <div className="divide-y divide-[var(--line)]">
        {rows.map((r) => {
          const tone = relationTone(r.relation);
          return (
            <div key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-xs">
              <span
                className="rounded-full px-2 py-0.5 text-[11px] font-medium"
                style={{ color: tone.fg, background: tone.bg }}
              >
                {r.relation_label || r.relation}
              </span>
              <span className="text-[var(--ink)]">{actionLabel(r.relation, r.action)}</span>
              <span className="mono text-[var(--ink-2)]">
                {r.new_label || '本次'} ⇄ {r.peer_label || '库内'}
              </span>
              <span className="mono text-[var(--ink-3)]">
                置信度 {(r.confidence * 100).toFixed(0)}%
              </span>
              <span className="mono rounded border border-[var(--line)] px-1 text-[10px] text-[var(--ink-3)]">
                {r.decided_by === 'remembered' ? '按记录自动套用' : '用户选择'}
              </span>
              {r.suppress && (
                <span className="mono text-[10px] text-[var(--signal)]">不再提示</span>
              )}
              <span className="mono ml-auto truncate text-[10px] text-[var(--ink-3)]">
                {(r.peer_paper_title || '').slice(0, 40)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
