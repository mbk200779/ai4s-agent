'use client';

import { useState } from 'react';
import type { LibraryGraphPayload } from './libraryTypes';

/**
 * 「指标计算口径」与「标签判定规则」两张卡。
 *
 * 单独抽出来是为了放在**图谱正下方**（左栏），而不是塞进右栏的长列表末尾——
 * 需求里"界面要能看到计算口径"指的是：看完图有疑问，视线往下一挪就能查，
 * 而不是先滚过一屏节点解读才找到。两张卡默认折叠，展开后是完整的公式与条件。
 */
export default function GraphMethodCards({ payload }: { payload: LibraryGraphPayload }) {
  const [showMethod, setShowMethod] = useState(false);
  const [showRules, setShowRules] = useState(false);

  return (
    <div className="grid gap-3 md:grid-cols-2">
      <section className="card p-3.5">
        <button
          type="button"
          className="flex w-full items-center justify-between text-[13px] font-semibold text-[var(--ink)]"
          onClick={() => setShowMethod((v) => !v)}
        >
          指标计算口径
          <span className="mono text-[11px] text-[var(--ink-3)]">{showMethod ? '收起' : '展开'}</span>
        </button>
        {showMethod ? (
          <div className="mt-2 space-y-2.5">
            {Object.entries(payload.methods).map(([key, m]) => (
              <div key={key} className="rounded-md border border-[var(--line)] bg-[var(--surface-2)] p-2">
                <div className="flex items-center justify-between">
                  <span className="text-[12px] font-medium text-[var(--ink)]">{m.name}</span>
                  <span className="mono text-[10px] text-[var(--ink-3)]">{key}</span>
                </div>
                <div className="mono mt-1 whitespace-pre-line break-words text-[10.5px] leading-relaxed text-[var(--ink-2)]">
                  {m.formula}
                </div>
                <div className="mt-1 text-[11px] leading-relaxed text-[var(--ink-3)]">
                  <div>范围：{m.scope}</div>
                  <div>读法：{m.reads}</div>
                  <div>注意：{m.caveat}</div>
                </div>
              </div>
            ))}
            <p className="text-[11px] leading-relaxed text-[var(--ink-2)]">
              被引次数与 PageRank 经常同向，但<strong className="font-medium">不是一回事</strong>：
              前者只数「几篇引了我」，后者还看「引我的人自己有多少分量」。
              库里只收 5 篇时，被引 3 次就是第一，这个数字不能拿去和 Google Scholar 比。
            </p>
          </div>
        ) : (
          <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--ink-3)]">
            PageRank / betweenness / 入度 / 边权，四个口径都展开可查（含公式、范围、局限）。
          </p>
        )}
      </section>

      <section className="card p-3.5">
        <button
          type="button"
          className="flex w-full items-center justify-between text-[13px] font-semibold text-[var(--ink)]"
          onClick={() => setShowRules((v) => !v)}
        >
          标签判定规则
          <span className="mono text-[11px] text-[var(--ink-3)]">{showRules ? '收起' : '展开'}</span>
        </button>
        {showRules ? (
          <table className="mt-2 w-full text-left text-[11px]">
            <thead>
              <tr className="text-[var(--ink-3)]">
                <th className="pb-1 font-normal">标签</th>
                <th className="pb-1 font-normal">条件</th>
                <th className="pb-1 font-normal">依据</th>
              </tr>
            </thead>
            <tbody className="align-top">
              {payload.rules.map((r) => (
                <tr key={r.label} className="border-t border-[var(--line)]">
                  <td className="py-1.5 pr-2 whitespace-nowrap font-medium text-[var(--ink)]">{r.label}</td>
                  <td className="py-1.5 pr-2 text-[var(--ink-2)]">{r.cond}</td>
                  <td className="py-1.5 text-[var(--ink-3)]">{r.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--ink-3)]">
            六类标签的触发条件与依据，规则可核对，不是模型打分。
          </p>
        )}
      </section>
    </div>
  );
}
