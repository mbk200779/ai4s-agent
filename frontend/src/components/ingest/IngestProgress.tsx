'use client';

/**
 * 解析进度组件。
 *
 * 两种取数方式：优先 SSE（阶段/页码一变就推），连不上就退回轮询。
 * 为什么两种都留：SSE 走某些反向代理会被缓冲成"最后一次性吐出来"，
 * 而轮询虽然多一点请求，但在任何网络环境下都能推进度条。
 * 两者的数据源是同一个接口，所以显示口径不会不一致。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { IngestProgress } from '@/types/document';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

export type { IngestProgress };

const STAGE_ORDER = ['queued', 'downloading', 'parsing', 'structuring', 'indexed'];

function fmtDuration(ms?: number | null) {
  if (!ms || ms < 0) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m${String(s % 60).padStart(2, '0')}s`;
}

export default function IngestProgressPanel({
  taskId,
  versionId,
  onDone,
}: {
  taskId?: string | null;
  versionId?: string | null;
  onDone?: (p: IngestProgress) => void;
}) {
  const [progress, setProgress] = useState<IngestProgress | null>(null);
  const [usingSse, setUsingSse] = useState(false);
  const [resuming, setResuming] = useState(false);
  // 续跑之后要重新把轮询拉起来：effect 依赖里加一个手动计数。
  const [pollEpoch, setPollEpoch] = useState(0);
  const doneRef = useRef(false);
  // SSE 是否还活着：半开连接不会触发 onerror，只能靠「多久没收到消息」判断。
  const esRef = useRef<EventSource | null>(null);
  const lastMsgRef = useRef(0);

  const key = taskId || versionId;
  const query = taskId
    ? `${API_BASE}/api/v1/ingest/tasks/${taskId}/progress`
    : versionId
      ? `${API_BASE}/api/v1/ingest/versions/${versionId}/progress`
      : '';

  const apply = useCallback(
    (p: IngestProgress) => {
      setProgress(p);
      if (p.done && !doneRef.current) {
        doneRef.current = true;
        onDone?.(p);
      }
    },
    [onDone],
  );

  // 轮询兜底：SSE 没连上（或已被看门狗判定失效）时按 1.2s 一次拉取。
  // 拿到终态就停：任务结束后没有新状态可等，继续拉只是白耗请求。
  useEffect(() => {
    if (!query) return;
    let timer: ReturnType<typeof setTimeout>;
    let alive = true;

    const poll = async () => {
      if (!alive || doneRef.current) return;
      try {
        const res = await fetch(query, { cache: 'no-store' });
        if (res.ok) apply((await res.json()) as IngestProgress);
      } catch {
        /* 网络抖动不打断进度条，下一轮再试 */
      }
      if (!alive || doneRef.current) return;
      timer = setTimeout(poll, 1200);
    };

    if (!usingSse) poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [query, usingSse, apply, pollEpoch]);

  // SSE：有 task_id 才能连；连不上就交给上面的轮询
  useEffect(() => {
    if (!taskId || typeof window === 'undefined' || !('EventSource' in window)) return;
    const url = `${API_BASE}/api/v1/ingest/tasks/${taskId}/events`;
    const es = new EventSource(url);
    let failures = 0;
    esRef.current = es;
    lastMsgRef.current = Date.now();

    es.onmessage = (evt) => {
      try {
        apply(JSON.parse(evt.data) as IngestProgress);
        setUsingSse(true);
        lastMsgRef.current = Date.now();
        failures = 0;
      } catch {
        /* 忽略半包 */
      }
    };
    es.addEventListener('done', () => es.close());
    // 心跳只用来续命：更新活跃时间，不碰界面状态。
    es.addEventListener('ping', () => {
      lastMsgRef.current = Date.now();
    });
    es.onerror = () => {
      failures += 1;
      if (failures >= 2) {
        setUsingSse(false);
        es.close();
        esRef.current = null;
      }
    };
    return () => {
      es.close();
      esRef.current = null;
    };
  }, [taskId, apply]);

  /**
   * SSE 看门狗。
   *
   * 为什么要它：链路抖动（比如公网代理丢包）时 EventSource 变成半开连接——
   * 既不报错也不再来消息，进度条就停在某一页，`done` 永远收不到，`onDone`
   * 也就不触发，父页面看起来「入库卡住了」，其实任务早就跑完了。
   * 所以只要 25 秒没有任何事件（后端每 10 秒发一次心跳，容许丢两次），
   * 就认定这条连接已死，关掉它并回落轮询。
   */
  useEffect(() => {
    if (!usingSse) return;
    const timer = setInterval(() => {
      if (Date.now() - lastMsgRef.current > 25000) {
        esRef.current?.close();
        esRef.current = null;
        setUsingSse(false);
      }
    }, 3000);
    return () => clearInterval(timer);
  }, [usingSse]);

  if (!key) return null;
  if (!progress) {
    return (
      <div className="mt-5 border border-[var(--line)] bg-[var(--surface)] p-4 text-sm text-[var(--ink-3)]">
        <span className="mono">等待 worker 领取任务…</span>
      </div>
    );
  }

  const failed = progress.stage === 'failed';
  const pct = Math.max(1, Math.min(100, progress.pct));
  const stageIdx = STAGE_ORDER.indexOf(progress.stage);

  const resume = async () => {
    if (!progress.version_id) return;
    setResuming(true);
    try {
      await fetch(`${API_BASE}/api/v1/ingest/versions/${progress.version_id}/resume`, {
        method: 'POST',
      });
      doneRef.current = false;
      setProgress((p) => (p ? { ...p, done: false, stage: 'parsing' } : p));
      setPollEpoch((n) => n + 1);
    } finally {
      setResuming(false);
    }
  };

  return (
    <div className="mt-5 border border-[var(--line)] bg-[var(--surface)] p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="mono text-xs uppercase tracking-wide text-[var(--signal)]">
            {progress.stage}
          </span>
          <span className="text-sm font-semibold text-[var(--ink)]">{progress.stage_label}</span>
          {progress.reused_from && (
            <span className="mono rounded-sm border border-[var(--line)] px-1.5 py-0.5 text-[10px] text-[var(--ink-3)]">
              复用
            </span>
          )}
        </div>
        <span className="mono text-xs text-[var(--ink-3)]">
          {progress.total_pages
            ? `${progress.page}/${progress.total_pages} 页`
            : '—'}
          {' · '}
          {pct}%
          {progress.eta_ms ? ` · 预计剩余 ${fmtDuration(progress.eta_ms)}` : ''}
        </span>
      </div>

      {/* 进度条：失败时整条变红，避免"绿条 + 错误信息"的自相矛盾 */}
      <div className="mt-3 h-1.5 w-full overflow-hidden bg-[var(--line)]">
        <div
          className="h-full transition-[width] duration-500"
          style={{
            width: `${pct}%`,
            background: failed ? 'var(--danger, #c0392b)' : 'var(--signal)',
          }}
        />
      </div>

      {/* 阶段刻度：让"卡在 structuring"一眼可见，而不是只有一个百分比 */}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {STAGE_ORDER.map((s, i) => (
          <span
            key={s}
            className="mono text-[10px]"
            style={{
              color:
                i < stageIdx || progress.done
                  ? 'var(--ink-2)'
                  : i === stageIdx
                    ? 'var(--signal)'
                    : 'var(--ink-3)',
              opacity: i <= stageIdx || progress.done ? 1 : 0.45,
            }}
          >
            {i < stageIdx || progress.done ? '✓' : i === stageIdx ? '●' : '○'} {s}
          </span>
        ))}
      </div>

      <div className="mono mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-[var(--ink-3)]">
        <span>片段 {progress.chunks}</span>
        <span>文献 {progress.refs}</span>
        <span>引用位置 {progress.citations}</span>
        <span>用时 {fmtDuration(progress.elapsed_ms)}</span>
        <span>
          尝试 {progress.attempt}/{progress.attempt_limit}
        </span>
        <span>{usingSse ? 'SSE' : '轮询'}</span>
      </div>

      {failed && (
        <div className="mt-4 border-l-2 border-[var(--danger,#c0392b)] bg-[var(--surface-2,transparent)] py-1 pl-3">
          <div className="text-sm text-[var(--ink)]">解析失败</div>
          <div className="mono mt-1 text-xs leading-relaxed text-[var(--ink-3)]">
            {progress.error || '未知错误'}
          </div>
          {progress.version_id && (
            <button
              onClick={resume}
              disabled={resuming}
              className="btn-ghost mt-3"
              style={{ padding: '6px 12px' }}
            >
              {resuming ? '正在续跑…' : `从第 ${progress.page + 1} 页续跑`}
            </button>
          )}
        </div>
      )}

      {progress.done && !failed && (
        <div className="mono mt-3 text-xs text-[var(--signal)]">
          {progress.stage === 'reused'
            ? '已复用既有解析结果，未重新解析'
            : `解析完成：${progress.page} 页 · ${progress.chunks} 个片段 · ${progress.refs} 条文献 · ${progress.citations} 处引用`}
        </div>
      )}
    </div>
  );
}
