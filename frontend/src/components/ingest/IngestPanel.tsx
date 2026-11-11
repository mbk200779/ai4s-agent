'use client';

/**
 * 论文入库面板：上传本地 PDF / 导入 arXiv。
 *
 * 这块功能原来长在首页，现在整体搬到论文库页——「导入」与「看库里有什么」
 * 本来就是一件事的两头，分在两个页面时用户传完看不到结果，只能靠一句
 * 「上传成功」猜。搬到一起后，入库成功即刷新下方清单。
 *
 * 逻辑与拆出来之前逐字一致，唯一新增的是 `onIngested`：上传入队、命中复用、
 * 按选择跳过、解析完成这四种时点都会通知父组件刷新清单。
 */

import { useCallback, useState } from 'react';
import IngestProgressPanel, { type IngestProgress } from '@/components/ingest/IngestProgress';
import DedupePrompt, {
  type DedupeAction,
  type PrecheckResult,
  recordDecision,
  runPrecheck,
} from '@/components/ingest/DedupePrompt';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

export default function IngestPanel({ onIngested }: { onIngested?: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [arxivId, setArxivId] = useState('');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  // 上传只拿到 task_id，解析在后台跑；进度条挂在这个 id 上
  const [taskId, setTaskId] = useState<string | null>(null);
  const [versionId, setVersionId] = useState<string | null>(null);
  // 版本标签：用户自己声明的，用于预检时判断"哪个更新"（不猜，也不从文件名猜）
  const [versionLabel, setVersionLabel] = useState('');
  // 预检结果非空 = 弹窗打开中
  const [precheck, setPrecheck] = useState<PrecheckResult | null>(null);

  // 进度组件把这个回调放进 effect 依赖里，身份每变一次就会重连一次 SSE/轮询，
  // 所以显式固定住。
  const handleDone = useCallback(
    (p: IngestProgress) => {
      setMessage(`解析完成：${p.chunks} 个片段已入库`);
      onIngested?.();
    },
    [onIngested],
  );

  /** 真正发起上传；`action` 来自弹窗选择或上次记住的选择。 */
  const submitUpload = async (
    pre: PrecheckResult | null,
    action?: DedupeAction,
    suppress = true,
  ) => {
    if (!file) return;
    setLoading(true);
    setMessage('');
    setTaskId(null);
    setVersionId(null);
    const form = new FormData();
    form.append('file', file);
    const peer = pre?.primary ?? null;
    if (action) {
      form.append('dedupe_action', action);
      form.append('dedupe_relation', peer?.relation ?? '');
      form.append('peer_version_id', peer?.version_id ?? '');
    }
    // project_id / version 是 query 参数，塞进 form 会被忽略
    const qs = new URLSearchParams();
    if (versionLabel.trim()) qs.set('version', versionLabel.trim());
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/papers/upload${qs.toString() ? `?${qs}` : ''}`,
        { method: 'POST', body: form },
      );
      const data = await res.json();
      if (!res.ok) {
        setMessage(`上传失败：${data.detail || res.status}`);
        return;
      }
      setTaskId(data.task_id || null);
      setVersionId(data.version_id || null);
      // 判定记录在上传之后写：这时才知道本次落成了哪个 version_id。
      // 写失败不影响入库，所以不 await 结果。
      if (pre && action) {
        await recordDecision(pre, action, suppress, { newVersionId: data.version_id || '' });
      }
      if (data.stage === 'skipped') {
        setMessage(data.detail || '按你的选择跳过入库');
      } else if (data.reused) {
        setMessage(`命中已解析文件，秒级复用：${data.detail || data.paper_id}`);
      } else {
        const actionLabel = peer?.actions.find((a) => a.action === action)?.label ?? action;
        const savedLabel = data.version || versionLabel.trim();
        setMessage(
          action
            ? `已按「${actionLabel}」入库${peer ? `：${peer.paper_title}${savedLabel ? ` · ${savedLabel}` : ''}` : `：${data.paper_id}`}`
            : `已入队：${data.paper_id}，任务 ${String(data.task_id).slice(0, 8)}`,
        );
      }
      // 无论入队还是复用，库里都已经多了一条（或更新了一条），先让清单对齐
      onIngested?.();
    } catch (e) {
      setMessage('上传失败');
    } finally {
      setLoading(false);
    }
  };

  const uploadPdf = async () => {
    if (!file) return;
    setLoading(true);
    setMessage('');
    setTaskId(null);
    setVersionId(null);
    // 先预检再上传：解析要几十秒，等解析完再问"要不要保留"等于让用户白等一遍
    const pre = await runPrecheck(file, { version: versionLabel.trim() });
    setLoading(false);
    if (pre?.exact_duplicate) {
      // 字节级相同：没有"关系"要判，直接告诉用户这个文件已经在库里
      const dup = pre.exact_duplicate;
      setMessage(
        `这个文件已经入库过（${dup.version_label} · ${dup.paper_title}），没有重复解析`,
      );
      return;
    }
    if (pre?.ask && pre.primary) {
      setPrecheck(pre);
      return;
    }
    // 没得问、或者这对内容之前已经定过 → 直接按上次的选择走，不再打扰
    const peer = pre?.primary;
    const remembered = peer?.memorized;
    const rememberedAction =
      pre?.suppressed && remembered && ['keep_new', 'keep_both', 'link_only'].includes(remembered.action)
        ? (remembered.action as DedupeAction)
        : undefined;
    await submitUpload(pre, rememberedAction);
  };

  const importArxiv = async () => {
    if (!arxivId.trim()) return;
    setLoading(true);
    setMessage('');
    setTaskId(null);
    setVersionId(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/papers/arxiv/import?arxiv_id=${arxivId}`, {
        method: 'POST',
      });
      const data = await res.json();
      if (!res.ok) {
        setMessage(`导入失败：${data.detail || res.status}`);
        return;
      }
      setTaskId(data.task_id);
      setVersionId(data.version_id || null);
      setMessage(
        data.reused
          ? `命中已解析文件，秒级复用：${data.paper_id}`
          : `已入队：${data.paper_id}，任务 ${String(data.task_id).slice(0, 8)}`,
      );
      onIngested?.();
    } catch (e) {
      setMessage('导入失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="grid gap-6 md:grid-cols-2">
        {/* 上传 PDF */}
        <div className="card card-tick p-6">
          <div className="mb-4 flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--line)] text-[var(--signal)]">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m6.75 12H9m1.5-3H9m7.5 6H5.625c-.621 0-1.125-.504-1.125-1.125V4.125C4.5 3.504 5.004 3 5.625 3h7.875c.298 0 .585.119.796.33l4.5 4.5c.211.211.33.498.33.796v10.5c0 .621-.504 1.125-1.125 1.125z" />
              </svg>
            </span>
            <div>
              <h2 className="font-semibold text-[var(--ink)]">上传本地 PDF</h2>
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
          <input
            type="text"
            placeholder="版本标签，如 v1（可留空）"
            value={versionLabel}
            onChange={(e) => setVersionLabel(e.target.value)}
            className="field mono mt-2"
          />
          <button onClick={uploadPdf} disabled={!file || loading} className="btn-primary mt-4">
            {loading ? '检查中…' : '上传并解析'}
          </button>
        </div>

        {/* 导入 arXiv */}
        <div className="card card-tick p-6">
          <div className="mb-4 flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--line)] text-[var(--signal)]">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m-18.432 0A8.959 8.959 0 013 12c0-.778.099-1.533.284-2.253" />
              </svg>
            </span>
            <div>
              <h2 className="font-semibold text-[var(--ink)]">导入 arXiv 论文</h2>
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

      {/* 解析进度：541 页教材要跑几分钟，没有进度条用户只能反复刷新。
          key 必不可少：连续传两篇时 taskId 会变，若复用同一个组件实例，
          组件内的 doneRef 还是上一轮的 true，这一轮的 done 就再也不会回调
          父页面，表现是「第二个文件解析完了但清单不刷新」。 */}
      {(taskId || versionId) && (
        <IngestProgressPanel
          key={taskId || versionId || ''}
          taskId={taskId}
          versionId={versionId}
          onDone={handleDone}
        />
      )}

      {precheck && (
        <DedupePrompt
          result={precheck}
          busy={loading}
          onCancel={() => setPrecheck(null)}
          onConfirm={async (action, suppress) => {
            const pre = precheck;
            setPrecheck(null);
            await submitUpload(pre, action, suppress);
          }}
        />
      )}
    </div>
  );
}
