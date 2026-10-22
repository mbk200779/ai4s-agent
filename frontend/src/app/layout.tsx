import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'AI4S 学术研究 Agent',
  description: '可验证、可复现的科研辅助平台',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen">
        <header className="border-b border-slate-800 bg-surface px-6 py-4">
          <div className="mx-auto flex max-w-6xl items-center justify-between">
            <h1 className="text-xl font-bold text-white">AI4S 学术研究 Agent</h1>
            <nav className="flex gap-4 text-sm text-muted">
              <a href="/" className="hover:text-white">上传</a>
              <a href="/qa" className="hover:text-white">问答</a>
              <a href="/graph" className="hover:text-white">图谱</a>
              <a href="/review" className="hover:text-white">综述</a>
              <a href="/writing" className="hover:text-white">写作</a>
              <a href="/eval" className="hover:text-white">评测</a>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
