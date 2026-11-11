'use client';

import { useEffect, useRef, useState } from 'react';
import SuggestionChips from '@/components/SuggestionChips';
import Markdown from '@/components/Markdown';
import { useToast } from '@/components/Toast';
import ClaimTable from '@/components/review/ClaimTable';
import ConflictList from '@/components/review/ConflictList';
import { ReviewResponse } from '@/types/review';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

const STAGES = ['抽取结论', '归并为观点簇', '复核冲突', '撰写正文'];

/** 从 Content-Disposition 取文件名：优先 RFC 5987 的 `filename*=UTF-8''…`。 */
function filenameFrom(disposition: string | null): string | null {
  if (!disposition) return null;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''));
    } catch {
      /* 编码坏了就走下面的 ASCII 名 */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1].trim() : null;
}

/** 拿不到响应头时的兜底名，按 Windows 的禁用字符清洗。 */
function fallbackName(topic: string): string {
  const stem = topic
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, '_')
    .slice(0, 80)
    .replace(/^[._-]+|[._-]+$/g, '');
  return `${stem || 'review'}.md`;
}

export default function ReviewPage() {
  const [topic, setTopic] = useState('');
  const [review, setReview] = useState<ReviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const { toast } = useToast();

  // 综述要走「抽 claim → 归并 → 判冲突 → 写正文」四次模型调用，实测 50~90 秒。
  // 没有进度事件可订阅（接口是一次性的），所以只报"已经等了多久"，
  // 不去猜"现在到哪一步了"——假进度条比没有进度更让人恼火。
  useEffect(() => {
    if (!loading) return;
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 500);
    return () => window.clearInterval(id);
  }, [loading]);

  const generate = async () => {
    if (!topic.trim()) return;
    setLoading(true);
    setReview(null);
    setElapsed(0);
    const t0 = Date.now();
    try {
      const res = await fetch(`${API_BASE}/api/v1/review/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, top_k: 10 }),
      });
      if (!res.ok) {
        // 后端 500 的响应体是纯文本 detail，带上它才分得清是超时、参数错还是模型挂了
        const detail = (await res.text().catch(() => '')).trim().slice(0, 120);
        const waited = Math.round((Date.now() - t0) / 1000);
        throw new Error(`HTTP ${res.status}${detail ? ` · ${detail}` : ''}（等待 ${waited}s）`);
      }
      const data: ReviewResponse = await res.json();
      setReview(data);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast(`生成失败：${msg}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  const exportMarkdown = async () => {
    if (!review || !review.markdown) return;
    try {
      const res = await fetch(`${API_BASE}/api/v1/review/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topic: review.topic,
          markdown: review.markdown,
          // 带上编号对照：页面里 [引#N] 能点开，导出的文件不能
          refs: review.refs,
        }),
      });
      if (!res.ok) {
        const detail = (await res.text().catch(() => '')).trim().slice(0, 120);
        throw new Error(`HTTP ${res.status}${detail ? ` · ${detail}` : ''}`);
      }
      const blob = await res.blob();
      if (blob.size === 0) throw new Error('文件为空');

      // 文件名以响应头为准。中文主题在前端自己拼会在带 / : 等字符时被浏览器丢弃，
      // 服务端已按 RFC 5987 编码过，这里解回来即可。
      const name = filenameFrom(res.headers.get('Content-Disposition')) || fallbackName(review.topic);

      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 立刻 revoke 会让句柄在下载启动前失效（Chrome 下表现为"点了没反应"），
      // 10 秒后回收足够覆盖慢盘与大文件
      window.setTimeout(() => window.URL.revokeObjectURL(url), 10000);
      toast(`已导出 ${name}`, 'success');
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast(`导出失败：${msg}`, 'error');
    }
  };

  const stats = review?.stats;
  const allSingle = !!stats && stats.cluster_count > 0 && stats.single_paper === stats.cluster_count;

  return (
    <div className="space-y-6">
      {/* 页头 */}
      <div className="flex items-center gap-4">
        <span className="flex h-11 w-11 items-center justify-center rounded-lg border border-[var(--line)] bg-[var(--surface)] text-[var(--signal)]">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-5 w-5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9zm3.75 11.625a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" />
          </svg>
        </span>
        <div>
          <h1 className="display text-3xl">综述生成</h1>
          <p className="mt-1 text-sm text-[var(--ink-2)]">
            按观点簇组织，不按检索顺序拼接；相反结论单独标注，不做「研究表明」式合并。
          </p>
        </div>
      </div>

      <div className="flex gap-2">
        <input
          ref={inputRef}
          type="text"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !loading) generate();
          }}
          placeholder="输入综述主题..."
          className="field flex-1"
        />
        <button onClick={generate} disabled={loading} className="btn-primary whitespace-nowrap">
          {loading ? `生成中 ${elapsed}s` : '生成'}
        </button>
      </div>

      <SuggestionChips
        title="试试这些主题"
        items={[
          'Transformer 架构的能力边界与适用条件',
          '预训练-微调范式是否优于从头训练',
          '大模型幻觉缓解方法综述',
          '面向科学文献的检索增强生成',
          '参数高效微调的证据强度',
        ]}
        onPick={(t) => {
          setTopic(t);
          inputRef.current?.focus();
        }}
      />

      {loading && (
        <div className="card p-5">
          <div className="flex items-center gap-3">
            <span className="h-2 w-2 animate-pulse rounded-full bg-[var(--signal)]" />
            <span className="text-sm text-[var(--ink)]">已用时 {elapsed}s</span>
            <span className="text-xs text-[var(--ink-3)]">典型耗时 50~90 秒</span>
          </div>
          <div className="mono mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--ink-3)]">
            {STAGES.map((s, i) => (
              <span key={s}>
                {i > 0 && <span className="mr-3 text-[var(--line-strong)]">→</span>}
                {s}
              </span>
            ))}
          </div>
        </div>
      )}

      {review && (
        <div className="space-y-5">
          {review.rejected ? (
            <div className="rounded-lg border border-[var(--danger)] bg-red-50 p-4 text-[var(--danger)]">
              {review.reject_reason || '无法生成综述'}
            </div>
          ) : (
            <>
              {/* 概览条 */}
              {stats && (
                <div className="card grid grid-cols-2 gap-3 p-4 md:grid-cols-6">
                  {[
                    { k: '文献', v: stats.paper_count },
                    { k: '结论', v: stats.raw_claims },
                    { k: '观点簇', v: stats.cluster_count },
                    { k: '冲突', v: stats.conflicts_confirmed },
                    { k: '仅单篇支持', v: stats.single_paper, warn: true },
                    { k: '立场', v: `${stats.support}/${stats.oppose}/${stats.conditional}` },
                  ].map((it) => (
                    <div key={it.k}>
                      <div className="text-xs text-[var(--ink-3)]">{it.k}</div>
                      <div
                        className="mono mt-0.5 text-lg"
                        style={{ color: it.warn && Number(it.v) > 0 ? 'var(--warn)' : 'var(--ink)' }}
                      >
                        {it.v}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {(allSingle || review.degraded || (stats?.violations ?? 0) > 0) && (
                <div className="rounded-lg border border-[var(--warn)] bg-[rgba(217,119,6,0.08)] p-3 text-xs leading-relaxed text-[var(--ink-2)]">
                  {allSingle && (
                    <div>
                      <b>本次所有观点都只有单篇文献支持。</b>
                      这不是漏聚类——是语料里确实没有第二篇文献讨论同一维度。
                      结论照常给出，但证据强度就这么多，建议读者自行补第二篇来源。
                    </div>
                  )}
                  {review.degraded && <div className="mt-1">部分段落由模板兜底生成（模型未返回可用句子），文字会比较生硬。</div>}
                  {(stats?.violations ?? 0) > 0 && (
                    <div className="mt-1">
                      有 {stats?.violations} 句因出现「研究表明／存在争议」这类抹平分歧的措辞被删除
                      （综述里存在已确认冲突时，这类句子被视为违规）。
                    </div>
                  )}
                </div>
              )}

              {/* 正文 */}
              <div className="card card-tick p-6">
                <div className="mb-4 flex items-center justify-between">
                  <h3 className="display text-lg">{review.topic}</h3>
                  <button onClick={exportMarkdown} className="btn-ghost px-3 py-1 text-sm">
                    导出 Markdown
                  </button>
                </div>
                <Markdown source={review.markdown} hideTitle={review.topic} />
              </div>

              {/* claim 表 */}
              <ClaimTable claims={review.claims} />

              {/* 冲突 */}
              <div>
                <h3 className="display mb-3 text-lg">冲突与分歧</h3>
                <ConflictList conflicts={review.conflicts} claims={review.claims} />
              </div>

              {/* 版本与统计 */}
              <div className="mono flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--ink-3)]">
                {review.config && (
                  <span>
                    prompt {review.config.prompt_version} · 配置 {review.config.version}/
                    {review.config.fingerprint}
                  </span>
                )}
                <span>引用论文：{review.referenced_arxiv_ids.join(', ') || '无'}</span>
                {stats && stats.dropped_sentences > 0 && (
                  <span>无引用被删句：{stats.dropped_sentences}</span>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
