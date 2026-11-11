'use client';

/**
 * 论文库管理页。
 *
 * 这一页回答的问题只有一个：「我的数据库里现在到底有什么，我要删掉哪几篇」。
 * 所以三个决定：
 * 1. 以 SQLite 的 papers 表为准列清单，而不是 `/papers/`（那个读向量 meta，
 *    只能看到「向量建成了的」）；解析中、解析失败、没有 PDF 的条目也要露出来；
 * 2. 数量与列表来自同一个响应（`summary` + `items`），删除后用返回的新 summary
 *    直接覆盖，再静默重拉一次对齐——不做「数字先动、列表后动」的两段式更新；
 * 3. 删除必须经过确认弹窗，且弹窗里逐条列出要删的论文（见 DeletePaperDialog），
 *    因为「删了会连带清掉向量和 PDF」这件事，不能等到删完才让用户知道。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useToast } from '@/components/Toast';
import IngestPanel from '@/components/ingest/IngestPanel';
import DeletePaperDialog from '@/components/library/DeletePaperDialog';
import type { DeleteResponse, LibraryPaper, LibraryResponse, LibrarySummary } from '@/types/library';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

const EMPTY_SUMMARY: LibrarySummary = {
  papers: 0,
  ready: 0,
  running: 0,
  failed: 0,
  chunks: 0,
  pages: 0,
  versions: 0,
  projects: 0,
  without_pdf: 0,
};

const STATUS_STYLE: Record<string, { fg: string; bg: string; border: string }> = {
  ready: { fg: 'var(--signal)', bg: 'var(--signal-soft)', border: 'var(--signal)' },
  running: { fg: 'var(--warn)', bg: 'rgba(217,119,6,0.10)', border: 'var(--warn)' },
  failed: { fg: 'var(--danger)', bg: 'rgba(220,38,38,0.08)', border: 'var(--danger)' },
  duplicate: { fg: 'var(--ink-3)', bg: 'var(--surface-3)', border: 'var(--line-strong)' },
  unknown: { fg: 'var(--ink-3)', bg: 'var(--surface-3)', border: 'var(--line-strong)' },
};

function pad(n: number): string {
  return n < 10 ? `0${n}` : `${n}`;
}

/**
 * 存储时间统一按 UTC+8 展示。
 * 为什么手算而不是 `toLocaleString()`：这个组件在服务端也会渲染一次，
 * 服务端容器的时区是 UTC、浏览器是东八区，直接本地化会让两边的文本不一致，
 * 触发 hydration 警告。固定偏移 + UTC 取值，两边的输出逐字相同。
 */
function fmtTime(iso?: string | null): string {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso.slice(0, 16).replace('T', ' ');
  const d = new Date(ms + 8 * 3600 * 1000);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(
    d.getUTCHours(),
  )}:${pad(d.getUTCMinutes())}`;
}

function fmtAuthors(authors: string[]): string {
  if (!authors.length) return '作者未知';
  if (authors.length <= 2) return authors.join('、');
  return `${authors.slice(0, 2).join('、')} 等 ${authors.length} 人`;
}

function fmtNum(n: number): string {
  return n.toLocaleString('zh-CN');
}

export default function LibraryPage() {
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [q, setQ] = useState('');
  const [project, setProject] = useState('all');
  const [status, setStatus] = useState('all');

  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState<LibraryPaper[] | null>(null);
  const [deleting, setDeleting] = useState(false);
  const { toast } = useToast();

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/library/papers`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body: LibraryResponse = await res.json();
      setData(body);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : '未知错误');
      if (!silent) toast('读取论文库失败', 'error');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    load();
  }, [load]);

  // 从首页「论文入库」/「开始使用」进来时带着 #import，客户端渲染首屏还没挂上节点，
  // 浏览器自己滚不过去，这里补一次。
  useEffect(() => {
    if (typeof window === 'undefined' || window.location.hash !== '#import') return;
    const timer = setTimeout(() => {
      document.getElementById('import')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 80);
    return () => clearTimeout(timer);
  }, []);

  const jumpToImport = () =>
    document.getElementById('import')?.scrollIntoView({ behavior: 'smooth', block: 'start' });

  // 入库面板拿它当「库里变了」的信号：静默重拉，避免把正在看的清单闪成骨架屏。
  // 用 useCallback 固定引用，否则本页每次选择/筛选都会让进度组件的订阅重建一遍。
  const reloadAfterIngest = useCallback(() => {
    load(true);
  }, [load]);

  const items = data?.items ?? [];
  const summary = data?.summary ?? EMPTY_SUMMARY;
  const runningCount = summary.running;

  // ---------- 解析中自动刷新 ----------
  // 解析一篇要 40 秒到 9 分钟，让用户守着「刷新」按钮不现实：
  // 只要清单里还有 running，就每 3 秒静默重拉一次；全部落地后定时器自己停。
  // 为什么放在页面这一层而不是进度条里：入口不止一个（arXiv 复用、另一个标签页
  // 导入、上次没关的进度条），进度条看不见的时候，列表状态也得自己跟上。
  const loadRef = useRef(load);
  loadRef.current = load;
  const prevRunning = useRef(0);

  useEffect(() => {
    if (!runningCount) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      if (!alive) return;
      // 后台标签页放慢到 10 秒一次：浏览器本来就会节流，这里不硬撑。
      if (typeof document !== 'undefined' && document.hidden) {
        timer = setTimeout(tick, 10000);
        return;
      }
      await loadRef.current(true);
      if (alive) timer = setTimeout(tick, 3000);
    };

    timer = setTimeout(tick, 3000);

    // 从别的标签页切回来时立刻拉一次，不等下一个 3 秒。
    const onVisible = () => {
      if (typeof document === 'undefined' || document.hidden || !alive) return;
      clearTimeout(timer);
      timer = setTimeout(tick, 0);
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      alive = false;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [runningCount]);

  // 全部落地时报一次：用户可能正看着清单，不提醒就容易错过状态已经变了。
  useEffect(() => {
    if (prevRunning.current > 0 && runningCount === 0) {
      toast('解析已完成，清单已更新', 'success');
    }
    prevRunning.current = runningCount;
  }, [runningCount, toast]);

  // 筛选在本地做：库里最多几百篇，一次拉全量比每次敲键都发请求快得多，
  // 也不会出现「请求还没回来，列表闪一下」。
  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return items.filter((p) => {
      if (project !== 'all' && p.project_id !== project) return false;
      if (status !== 'all' && p.status !== status) return false;
      if (!kw) return true;
      const hay = [p.title, p.arxiv_id || '', ...p.authors, p.project_name].join(' ').toLowerCase();
      return hay.includes(kw);
    });
  }, [items, q, project, status]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const selectedPapers = useMemo(
    () => items.filter((p) => selectedSet.has(p.id)),
    [items, selectedSet],
  );
  const selectableFiltered = filtered.filter((p) => p.deletable);
  const allSelected =
    selectableFiltered.length > 0 && selectableFiltered.every((p) => selectedSet.has(p.id));

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const toggleAll = () => {
    if (allSelected) {
      const drop = new Set(selectableFiltered.map((p) => p.id));
      setSelected((prev) => prev.filter((x) => !drop.has(x)));
    } else {
      setSelected((prev) => Array.from(new Set([...prev, ...selectableFiltered.map((p) => p.id)])));
    }
  };

  const confirmDelete = async () => {
    if (!pending?.length) return;
    const ids = pending.map((p) => p.id);
    setDeleting(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/library/papers/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      const body = (await res.json()) as DeleteResponse & {
        detail?: string | { message?: string; blocked?: { title: string; status_label: string }[] };
      };
      if (!res.ok) {
        const detail = body?.detail;
        const msg =
          typeof detail === 'string'
            ? detail
            : detail?.blocked?.length
              ? `${detail.message}：` +
                detail.blocked.map((b) => `《${b.title}》${b.status_label}`).join('、')
              : detail?.message || `删除失败（HTTP ${res.status}）`;
        throw new Error(msg);
      }

      // 先本地摘掉并立刻用服务端返回的 summary 覆盖计数，界面不留「已删除的行」；
      // 随后静默重拉一次，把其它标签页/后台任务造成的差异一并对齐。
      const gone = new Set(ids);
      setData((prev) =>
        prev
          ? { ...prev, items: prev.items.filter((i) => !gone.has(i.id)), summary: body.summary }
          : prev,
      );
      setSelected((prev) => prev.filter((x) => !gone.has(x)));
      setPending(null);

      const r = body.removed;
      // 只报真实发生的清理：向量 0 个、原件不存在时不该在提示里占位，
      // 否则用户会以为「删了 2 篇却一个向量都没清」是系统没删干净。
      const parts = [`已删除 ${body.deleted_count} 篇论文`];
      if (r.vectors) parts.push(`${r.vectors} 个片段向量`);
      if (r.pdfs) parts.push(`${r.pdfs} 个 PDF 原件`);
      toast(parts.join(' · '), 'success');
      (body.warnings || []).forEach((w) => toast(w, 'info'));
      if (body.deleted_count < ids.length) {
        toast(`有 ${ids.length - body.deleted_count} 篇未能删除，请重试`, 'error');
      }
      await load(true);
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error');
    } finally {
      setDeleting(false);
    }
  };

  const summaryTiles = [
    { k: '论文总数', v: summary.papers, hint: `${summary.projects} 个课题` },
    { k: '已入库', v: summary.ready, hint: summary.running ? `${summary.running} 篇解析中 · 自动刷新` : '全部可检索' },
    { k: '片段总数', v: summary.chunks, hint: `${fmtNum(summary.pages)} 页正文` },
    { k: '解析失败', v: summary.failed, hint: summary.failed ? '检查解析日志' : '无' },
  ];

  return (
    <div className="space-y-6">
      {/* ---------- 头部 ---------- */}
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="eyebrow">LIBRARY · 论文数据库</div>
          <h1 className="display mt-2 text-3xl">论文库</h1>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--ink-2)]">
            库内所有论文的完整清单。删除会同时清除该论文的片段向量与 PDF 原件，
            问答、综述与图谱随即不再检索到它。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => load()}
            disabled={loading}
            className="inline-flex items-center gap-2 rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-3 py-2 text-sm font-medium text-[var(--ink-2)] transition hover:bg-[var(--surface-3)] disabled:opacity-50"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.7"
              className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M16.023 9.348h4.992V4.356M4.985 19.644v-4.992h4.992M6.34 6.34a8.25 8.25 0 0112.6 2.34M17.66 17.66a8.25 8.25 0 01-12.6-2.34"
              />
            </svg>
            刷新
          </button>
          <a
            href="#import"
            onClick={(e) => {
              e.preventDefault();
              jumpToImport();
            }}
            className="inline-flex items-center gap-2 rounded-md bg-[var(--signal)] px-3 py-2 text-sm font-semibold text-white transition hover:bg-[var(--signal-hover)]"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" className="h-4 w-4">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            导入论文
          </a>
        </div>
      </header>

      {/* ---------- 导入（原首页 INGEST 区块整体搬到这里） ---------- */}
      <section id="import" className="scroll-mt-24">
        <div className="mb-4 flex flex-wrap items-baseline gap-4">
          <span className="eyebrow">Ingest · 导入</span>
          <span className="h-px flex-1 bg-[var(--line)]" />
          <span className="mono text-xs text-[var(--ink-3)]">
            PDF 与 arXiv 两种来源，解析完成后自动出现在下方清单
          </span>
        </div>
        <IngestPanel onIngested={reloadAfterIngest} />
      </section>

      {/* ---------- 计数条 ---------- */}
      <section className="grid grid-cols-2 gap-px overflow-hidden rounded-[var(--radius-card)] border border-[var(--line)] bg-[var(--line)] sm:grid-cols-4">
        {summaryTiles.map((t) => (
          <div key={t.k} className="bg-[var(--surface)] px-4 py-3.5">
            <div className="text-xs text-[var(--ink-3)]">{t.k}</div>
            <div className="mono mt-1 text-2xl font-semibold leading-none text-[var(--ink)]">
              {loading && !data ? '—' : fmtNum(t.v)}
            </div>
            <div className="mt-1.5 truncate text-xs text-[var(--ink-3)]">{t.hint}</div>
          </div>
        ))}
      </section>

      {/* ---------- 工具条 ---------- */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[16rem] flex-1">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="var(--ink-3)"
            strokeWidth="1.7"
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 11-13 0 6.5 6.5 0 0113 0z" />
          </svg>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="按标题、作者或 arXiv ID 筛选"
            aria-label="筛选论文"
            className="w-full rounded-md border border-[var(--line)] bg-[var(--surface)] py-2 pl-9 pr-8 text-sm text-[var(--ink)] outline-none transition focus:border-[var(--signal)]"
          />
          {q && (
            <button
              type="button"
              onClick={() => setQ('')}
              aria-label="清空筛选"
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-[var(--ink-3)] hover:bg-[var(--surface-3)]"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-3.5 w-3.5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>

        <select
          value={project}
          onChange={(e) => setProject(e.target.value)}
          aria-label="按课题筛选"
          className="rounded-md border border-[var(--line)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--ink-2)] outline-none focus:border-[var(--signal)]"
        >
          <option value="all">全部课题（{items.length}）</option>
          {(data?.projects ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}（{p.papers}）
            </option>
          ))}
        </select>

        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="按状态筛选"
          className="rounded-md border border-[var(--line)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--ink-2)] outline-none focus:border-[var(--signal)]"
        >
          <option value="all">全部状态</option>
          <option value="ready">已入库</option>
          <option value="running">解析中</option>
          <option value="failed">解析失败</option>
        </select>

        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={toggleAll}
            disabled={!selectableFiltered.length}
            className="rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--ink-2)] transition hover:bg-[var(--surface-3)] disabled:opacity-40"
          >
            {allSelected ? '取消全选' : `全选当前 ${selectableFiltered.length} 篇`}
          </button>
          <button
            type="button"
            onClick={() => setPending(selectedPapers)}
            disabled={!selectedPapers.length}
            className="inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
            style={{
              color: selectedPapers.length ? '#fff' : 'var(--ink-3)',
              background: selectedPapers.length ? 'var(--danger)' : 'var(--surface-2)',
              border: selectedPapers.length ? '1px solid var(--danger)' : '1px solid var(--line)',
            }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.2v.916m7.5 0a48.667 48.667 0 00-7.5 0"
              />
            </svg>
            删除所选{selectedPapers.length ? `（${selectedPapers.length}）` : ''}
          </button>
        </div>
      </div>

      {/* ---------- 清单 ---------- */}
      <section className="card overflow-hidden">
        {/* 表头：只在 ≥sm 显示。窄屏下这排固定列宽会把「论文」挤成一列竖排字，
            所以手机上改成行内两行（标题 + 元信息），不强行摆表头。 */}
        <div className="hidden items-center gap-3 border-b border-[var(--line)] bg-[var(--surface-2)] px-4 py-2 text-xs text-[var(--ink-3)] sm:flex">
          <span className="w-4 shrink-0" />
          <span className="min-w-0 flex-1">论文（标题 / 作者 / 标识）</span>
          <span className="hidden w-28 shrink-0 lg:block">课题</span>
          <span className="hidden w-24 shrink-0 text-right md:block">规模</span>
          <span className="w-36 shrink-0 text-right">存储时间</span>
          <span className="w-20 shrink-0 text-center">状态</span>
          <span className="w-20 shrink-0 text-right">操作</span>
        </div>

        {loading && !data ? (
          <div className="space-y-2 px-4 py-10">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-10 animate-pulse rounded bg-[var(--surface-2)]" />
            ))}
          </div>
        ) : loadError ? (
          <div className="px-4 py-12 text-center">
            <p className="text-sm text-[var(--danger)]">读取失败：{loadError}</p>
            <button
              type="button"
              onClick={() => load()}
              className="mt-3 rounded-md border border-[var(--line-strong)] px-3 py-1.5 text-sm text-[var(--ink-2)] hover:bg-[var(--surface-3)]"
            >
              重试
            </button>
          </div>
        ) : !items.length ? (
          <div className="px-4 py-14 text-center">
            <div className="display text-lg">数据库里还没有论文</div>
            <p className="mt-2 text-sm text-[var(--ink-3)]">
              在上方「导入」区上传 PDF 或导入 arXiv 论文，解析完成后就会出现在这里。
            </p>
            <a
              href="#import"
              onClick={(e) => {
                e.preventDefault();
                jumpToImport();
              }}
              className="mt-4 inline-block rounded-md bg-[var(--signal)] px-4 py-2 text-sm font-semibold text-white hover:bg-[var(--signal-hover)]"
            >
              去导入论文
            </a>
          </div>
        ) : !filtered.length ? (
          <div className="px-4 py-12 text-center text-sm text-[var(--ink-3)]">
            没有符合当前筛选条件的论文
            <button
              type="button"
              onClick={() => {
                setQ('');
                setProject('all');
                setStatus('all');
              }}
              className="ml-2 text-[var(--signal)] underline-offset-2 hover:underline"
            >
              清空筛选
            </button>
          </div>
        ) : (
          <div className="divide-y divide-[var(--line)]">
            {filtered.map((p) => {
              const tone = STATUS_STYLE[p.status] ?? STATUS_STYLE.unknown;
              const checked = selectedSet.has(p.id);
              return (
                <div
                  key={p.id}
                  className={`flex items-center gap-3 px-4 py-3 transition-colors ${
                    checked ? 'bg-[var(--signal-soft)]/50' : 'hover:bg-[var(--surface-2)]'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(p.id)}
                    disabled={!p.deletable}
                    title={p.deletable ? '选择这篇论文' : p.blocked_reason || '暂不可删除'}
                    aria-label={`选择《${p.title}》`}
                    className="h-4 w-4 shrink-0 accent-[var(--signal)] disabled:opacity-40"
                  />

                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-[var(--ink)]" title={p.title}>
                      {p.title}
                    </div>
                    <div className="mono mt-0.5 flex flex-wrap items-center gap-x-2 truncate text-xs text-[var(--ink-3)]">
                      <span className="truncate">{fmtAuthors(p.authors)}</span>
                      <span className="text-[var(--line-strong)]">/</span>
                      <span>{p.arxiv_id || '无标识'}</span>
                      {p.version_count > 1 && (
                        <>
                          <span className="text-[var(--line-strong)]">/</span>
                          <span>{p.version_count} 个版本</span>
                        </>
                      )}
                      {!p.has_pdf && (
                        <>
                          <span className="text-[var(--line-strong)]">/</span>
                          <span style={{ color: 'var(--warn)' }}>无 PDF 原件</span>
                        </>
                      )}
                      {/* 窄屏：时间、规模与状态都并进第二层，把宽度留给标题 */}
                      <span className="hidden text-[var(--line-strong)] sm:hidden">/</span>
                      <span className="sm:hidden" style={{ color: tone.fg }}>
                        {p.status_label}
                      </span>
                      <span className="sm:hidden">{fmtTime(p.stored_at)}</span>
                      <span className="sm:hidden">
                        {p.pages ? `${p.pages} 页` : '—'} · {p.chunks} 片段
                      </span>
                    </div>
                  </div>

                  <div className="hidden w-28 shrink-0 lg:block">
                    <span className="mono truncate text-xs text-[var(--ink-2)]" title={p.project_name}>
                      {p.project_name}
                    </span>
                  </div>

                  <div className="mono hidden w-24 shrink-0 text-right text-xs text-[var(--ink-2)] md:block">
                    {p.pages ? `${p.pages} 页` : '—'}
                    <span className="block text-[var(--ink-3)]">{p.chunks} 片段</span>
                  </div>

                  <div className="hidden w-36 shrink-0 text-right sm:block">
                    <span className="mono block text-xs text-[var(--ink-2)]">{fmtTime(p.stored_at)}</span>
                    <span className="mono block text-[10px] text-[var(--ink-3)]">
                      {p.version_label ? `版本 ${p.version_label}` : '—'}
                    </span>
                  </div>

                  <div className="hidden w-20 shrink-0 text-center sm:block">
                    <span
                      className="inline-block rounded border px-1.5 py-0.5 text-xs"
                      style={{ color: tone.fg, background: tone.bg, borderColor: tone.border }}
                      title={p.blocked_reason || p.status_label}
                    >
                      {p.status_label}
                    </span>
                  </div>

                  <div className="flex w-12 shrink-0 items-center justify-end gap-2.5 sm:w-20">
                    {p.has_pdf && (
                      <a
                        href={`${API_BASE}/api/v1/papers/${encodeURIComponent(p.arxiv_id || '')}/pdf`}
                        target="_blank"
                        rel="noreferrer"
                        className="hidden text-xs text-[var(--ink-3)] transition hover:text-[var(--signal)] sm:inline"
                        title="在新标签页打开 PDF 原件"
                      >
                        原文
                      </a>
                    )}
                    <button
                      type="button"
                      onClick={() => setPending([p])}
                      disabled={!p.deletable}
                      title={p.deletable ? '删除这篇论文' : p.blocked_reason || '暂不可删除'}
                      className="text-xs transition disabled:cursor-not-allowed disabled:opacity-40"
                      style={{ color: p.deletable ? 'var(--danger)' : 'var(--ink-3)' }}
                    >
                      删除
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* 底部统计：筛选后仍要说清「看到几篇、库里共几篇」 */}
        {items.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--line)] bg-[var(--surface-2)] px-4 py-2 text-xs text-[var(--ink-3)]">
            <span className="mono">
              显示 {filtered.length} / {items.length} 篇 · 共 {fmtNum(summary.chunks)} 个片段 ·{' '}
              {fmtNum(summary.pages)} 页
            </span>
            <span className="mono">
              {summary.running > 0
                ? `${summary.running} 篇解析中，完成后可删除`
                : summary.without_pdf > 0
                  ? `${summary.without_pdf} 篇缺 PDF 原件`
                  : '全部可删除'}
            </span>
          </div>
        )}
      </section>

      {pending && pending.length > 0 && (
        <DeletePaperDialog
          papers={pending}
          busy={deleting}
          onCancel={() => setPending(null)}
          onConfirm={confirmDelete}
        />
      )}
    </div>
  );
}
