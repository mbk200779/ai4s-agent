'use client';

import { WritingReference, WritingSentence } from '@/types/writing';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

const ARXIV_RE = /^\d{4}\.\d{4,5}$/;

function pdfHref(arxivId: string, page: number): string | null {
  // 本地上传的论文 arxiv_id 是 upload_<hash>，没有 arXiv PDF 端点，只能看正文块。
  if (!ARXIV_RE.test(arxivId)) return null;
  return `${API_BASE}/api/v1/papers/${arxivId}/pdf#page=${page}`;
}

export default function SourcePanel({
  sentence,
  activeCite,
  onPickCite,
  references,
}: {
  sentence: WritingSentence | null;
  activeCite: number | null;
  onPickCite: (n: number) => void;
  references: WritingReference[];
}) {
  const refByNo = new Map(references.map((r) => [r.ref_no, r]));
  const active =
    sentence?.evidence.find((e) => e.n === activeCite) || sentence?.evidence[0] || null;

  return (
    <aside className="lg:sticky lg:top-4 self-start">
      <div className="card p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="display text-base">来源</h3>
          {sentence && (
            <span className="mono text-[11px] text-[var(--ink-3)]">
              {sentence.type === 'bridge' ? '无需引用' : `${sentence.cite_nos.length} 处引用`}
            </span>
          )}
        </div>

        {!sentence && (
          <p className="text-sm leading-relaxed text-[var(--ink-3)]">
            点正文里任意一句话或 <span className="mono text-[var(--signal)]">[引#N]</span>，
            这里会展开它引的原文、页码与对应文献。
          </p>
        )}

        {sentence && (
          <div className="space-y-3">
            <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-3">
              <p className="text-sm leading-relaxed text-[var(--ink)]">{sentence.text}</p>
              {sentence.type === 'bridge' && (
                <p className="mt-2 text-xs leading-relaxed text-[var(--ink-3)]">
                  {sentence.source === 'server_bridge'
                    ? '这句过渡由系统插入，只说明「上一节讲了什么、本节讲什么」，不含任何事实断言，因此无需引用。'
                    : '模型判定为过渡/连接句，不含数字与比较，因此无需引用。'}
                </p>
              )}
            </div>

            {sentence.cite_nos.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {sentence.evidence.map((e) => (
                  <button
                    key={e.n}
                    type="button"
                    onClick={() => onPickCite(e.n)}
                    className={`mono rounded border px-1.5 py-0.5 text-[11px] transition ${
                      active?.n === e.n
                        ? 'border-[var(--signal)] bg-[var(--signal-soft)] text-[var(--signal)]'
                        : 'border-[var(--line)] text-[var(--ink-3)] hover:border-[var(--signal)]'
                    }`}
                  >
                    引#{e.n}
                    {e.ref_no ? ` · [${e.ref_no}]` : ''}
                  </button>
                ))}
              </div>
            )}

            {active && (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-[var(--ink-3)]">
                  <span className="mono text-[var(--signal)]">引#{active.n}</span>
                  <span>p.{active.page}</span>
                  {active.section && <span>· {active.section}</span>}
                  {active.ref_no && <span className="mono">→ [{active.ref_no}]</span>}
                  {active.relevance != null && (
                    <span title="与主题的跨语言语义相似度">相关度 {active.relevance.toFixed(2)}</span>
                  )}
                </div>

                <p className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-[var(--line)] bg-white p-3 text-xs leading-relaxed text-[var(--ink-2)]">
                  {active.text}
                  {active.truncated ? ' …' : ''}
                </p>

                <div className="space-y-1 text-[11px] text-[var(--ink-3)]">
                  <p className="mono break-all">chunk {active.chunk_id}</p>
                  <p className="break-all">{active.title}</p>
                  <div className="flex items-center gap-3 pt-1">
                    {pdfHref(active.arxiv_id, active.page) ? (
                      <a
                        href={pdfHref(active.arxiv_id, active.page) as string}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[var(--signal)] hover:underline"
                      >
                        打开 PDF 第 {active.page} 页 ↗
                      </a>
                    ) : (
                      <span>本地上传论文，无在线 PDF</span>
                    )}
                    {ARXIV_RE.test(active.arxiv_id) && (
                      <a
                        href={`https://arxiv.org/abs/${active.arxiv_id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="mono text-[var(--signal)] hover:underline"
                      >
                        arXiv:{active.arxiv_id}
                      </a>
                    )}
                  </div>
                </div>

                {active.ref_no && refByNo.get(active.ref_no) && (
                  <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-2 text-[11px] leading-relaxed text-[var(--ink-2)]">
                    <span className="mono text-[var(--signal)]">[{active.ref_no}]</span>{' '}
                    {(refByNo.get(active.ref_no)?.authors || []).slice(0, 3).join(', ')}
                    {(refByNo.get(active.ref_no)?.authors || []).length > 3 ? ' et al.' : ''}{' '}
                    {refByNo.get(active.ref_no)?.title}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
