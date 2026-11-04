import type { Metadata } from 'next';
import './globals.css';
import NavBar from '@/components/NavBar';
import ToastProvider from '@/components/Toast';

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
        <ToastProvider>
          <NavBar />
          <main className="mx-auto max-w-6xl px-6 py-12">{children}</main>
        </ToastProvider>
      </body>
    </html>
  );
}
