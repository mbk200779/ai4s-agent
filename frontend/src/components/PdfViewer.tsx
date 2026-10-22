'use client';

import { useEffect, useRef, useState } from 'react';

interface PdfViewerProps {
  pdfUrl: string;
  page: number;
  bbox?: number[] | null;
  scale?: number;
}

export default function PdfViewer({ pdfUrl, page, bbox, scale = 1.5 }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [highlight, setHighlight] = useState<{
    left: number;
    top: number;
    width: number;
    height: number;
  } | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;

    const render = async () => {
      setLoaded(false);
      setError('');
      setHighlight(null);

      try {
        // 动态导入避免 SSR 问题；worker 使用 CDN，版本与 package 一致
        const pdfjsLib = await import('pdfjs-dist');
        pdfjsLib.GlobalWorkerOptions.workerSrc =
          'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.worker.mjs';

        const pdf = await pdfjsLib.getDocument(pdfUrl).promise;
        const p = await pdf.getPage(page);
        const viewport = p.getViewport({ scale });

        const canvas = canvasRef.current;
        if (!canvas || cancelled) return;

        canvas.width = viewport.width;
        canvas.height = viewport.height;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        await p.render({ canvasContext: ctx, viewport }).promise;
        if (!cancelled) setLoaded(true);

        if (bbox && bbox.length === 4 && !cancelled) {
          const vr = viewport.convertToViewportRectangle(bbox);
          setHighlight({
            left: Math.min(vr[0], vr[2]),
            top: Math.min(vr[1], vr[3]),
            width: Math.abs(vr[2] - vr[0]),
            height: Math.abs(vr[3] - vr[1]),
          });
        }
      } catch (e: any) {
        if (!cancelled) setError(e.message || 'PDF 渲染失败');
      }
    };

    render();
    return () => {
      cancelled = true;
    };
  }, [pdfUrl, page, scale]);

  return (
    <div className="relative inline-block">
      <canvas ref={canvasRef} className="rounded border border-slate-700 bg-white" />
      {loaded && highlight && (
        <div
          className="pointer-events-none absolute border-2 border-yellow-400 bg-yellow-300/30"
          style={{
            left: highlight.left,
            top: highlight.top,
            width: highlight.width,
            height: highlight.height,
          }}
        />
      )}
      {error && <div className="mt-2 text-sm text-red-400">{error}</div>}
    </div>
  );
}
