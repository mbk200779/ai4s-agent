'use client';

import { useEffect, useState } from 'react';

const COLORS = [
  { name: '深蓝', value: '#141b2e' },
  { name: '蓝灰', value: '#1a2233' },
  { name: '暗靛', value: '#171d31' },
];

export default function ThemeSwitcher() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState('#141b2e');

  // 挂载时同步一次当前背景
  useEffect(() => {
    const existing = document.documentElement.style.getPropertyValue('--background');
    if (existing) setCurrent(existing.trim());
  }, []);

  const apply = (value: string) => {
    setCurrent(value);
    // 统一通过 CSS 变量驱动，避免内联 style 与 CSS 变量优先级冲突
    document.documentElement.style.setProperty('--background', value);
    document.body.style.backgroundColor = value;
    // 强制浏览器重绘，解决独显 + 外接屏合成器不刷新的问题
    void document.body.offsetHeight;
  };

  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col items-end gap-2">
      {open && (
        <div className="rounded-2xl border border-white/10 bg-[#1c2740] p-3 shadow-2xl shadow-black/50">
          <p className="mb-2 text-xs font-semibold text-slate-400">选择背景色</p>
          <div className="grid grid-cols-3 gap-2">
            {COLORS.map((c) => (
              <button
                key={c.value}
                onClick={() => apply(c.value)}
                className={`flex h-16 w-16 flex-col items-center justify-end rounded-xl border-2 p-1 text-[10px] text-slate-300 transition ${
                  current === c.value ? 'border-indigo-400' : 'border-white/10 hover:border-white/25'
                }`}
                style={{ backgroundColor: c.value }}
              >
                {c.name}
              </button>
            ))}
          </div>
        </div>
      )}
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex h-12 w-12 items-center justify-center rounded-full bg-[#24304d] text-slate-300 shadow-lg ring-1 ring-white/10 transition hover:scale-105"
        title="切换背景色"
        aria-label="切换背景色"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-5 w-5">
          <path strokeLinecap="round" strokeLinejoin="round" d="M9.53 16.122a3 3 0 00-5.78 1.128 2.25 2.25 0 01-2.4 2.245 4.5 4.5 0 008.4-2.245c0-.399-.078-.78-.22-1.128zm0 0a15.998 15.998 0 003.388-1.62m-5.043-.025a15.994 15.994 0 011.622-3.395m3.42 3.42a15.995 15.995 0 004.764-4.648l3.876-5.814a1.151 1.151 0 00-1.597-1.597L14.146 6.32a15.996 15.996 0 00-4.649 4.763m3.42 3.42a6.776 6.776 0 00-3.42-3.42" />
        </svg>
      </button>
    </div>
  );
}
