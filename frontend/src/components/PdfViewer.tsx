'use client';

import { useEffect, useRef, useState } from 'react';

interface PdfViewerProps {
  pdfUrl: string;
  page: number;
  bbox?: number[] | null;
  /** 句子/单元格级矩形（左上原点、y 向下）。有它就按句画框，没有才退回 bbox 整段。 */
  rects?: number[][] | null;
  /** sentence | cell | block | line | ocr —— 只影响框的样式与角标文案 */
  precision?: string | null;
  scale?: number;
}

interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

const STYLE: Record<string, { bg: string; border: string; dashed: boolean; label: string }> = {
  sentence: { bg: 'rgba(13,148,136,0.20)', border: 'rgba(13,148,136,0.85)', dashed: false, label: '句级定位' },
  line: { bg: 'rgba(13,148,136,0.18)', border: 'rgba(13,148,136,0.75)', dashed: false, label: '行级定位' },
  cell: { bg: 'rgba(124,58,237,0.16)', border: 'rgba(124,58,237,0.85)', dashed: true, label: '表格单元格' },
  block: { bg: 'rgba(217,119,6,0.14)', border: 'rgba(217,119,6,0.8)', dashed: true, label: '整段定位' },
  ocr: { bg: 'rgba(217,119,6,0.12)', border: 'rgba(217,119,6,0.9)', dashed: true, label: 'OCR 定位' },
};

export default function PdfViewer({
  pdfUrl,
  page,
  bbox,
  rects,
  precision,
  scale = 1.5,
}: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [boxes, setBoxes] = useState<Box[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;

    const render = async () => {
      setLoaded(false);
      setError('');
      setBoxes([]);

      try {
        // 动态导入避免 SSR 问题；worker 版本必须与安装的 pdfjs-dist 一致
        const pdfjsLib = await import('pdfjs-dist');
        pdfjsLib.GlobalWorkerOptions.workerSrc =
          'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs';

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

        // 坐标系换算：解析期给的矩形是 PDF 用户空间但**左上原点、y 向下**
        // （PyMuPDF/pdfplumber 的口径），而 pdf.js 的 convertToViewportRectangle
        // 要的是左下原点、y 向上。不翻一下就画，框会整体上下镜像——
        // 那种"高亮位置差半页"的 bug 看起来像坐标算错，其实是原点没换。
        const view = p.view as number[];
        const height = view[3] - view[1];
        const raw = (rects && rects.length ? rects : bbox ? [bbox] : []).filter(
          (r) => Array.isArray(r) && r.length === 4,
        );
        const next: Box[] = [];
        for (const r of raw) {
          const flipped: [number, number, number, number] = [r[0], height - r[3], r[2], height - r[1]];
          const vr = viewport.convertToViewportRectangle(flipped);
          next.push({
            left: Math.min(vr[0], vr[2]),
            top: Math.min(vr[1], vr[3]),
            width: Math.abs(vr[2] - vr[0]),
            height: Math.abs(vr[3] - vr[1]),
          });
        }
        if (!cancelled) setBoxes(next);
      } catch (e: unknown) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'PDF 渲染失败');
      }
    };

    render();
    return () => {
      cancelled = true;
    };
  }, [pdfUrl, page, scale, rects, bbox]);

  const style = STYLE[precision || 'sentence'] || STYLE.sentence;
  // 整页大小的框等于没定位：宁可只画一条页边标记，也不糊住整页
  const pageArea = (canvasRef.current?.width || 1) * (canvasRef.current?.height || 1);
  const fullPage = boxes.some((b) => (b.width * b.height) / pageArea > 0.9);

  return (
    <div className="relative inline-block">
      <canvas ref={canvasRef} className="block rounded border border-[var(--line)] bg-white" />
      {loaded &&
        !fullPage &&
        boxes.map((b, i) => (
          <div
            key={i}
            className="pointer-events-none absolute"
            style={{
              left: b.left,
              top: b.top,
              width: b.width,
              height: b.height,
              background: style.bg,
              border: `${style.dashed ? '1.5px dashed' : '1.5px solid'} ${style.border}`,
              borderRadius: 2,
            }}
          />
        ))}
      {loaded && fullPage && boxes[0] && (
        <div
          className="pointer-events-none absolute"
          style={{ left: boxes[0].left, top: boxes[0].top, width: boxes[0].width, height: 3, background: style.border }}
        />
      )}
      {loaded && boxes.length > 0 && (
        <div
          className="mono pointer-events-none absolute rounded px-1.5 py-[1px] text-[10px] text-white"
          style={{ left: boxes[0].left, top: Math.max(0, boxes[0].top - 18), background: style.border }}
        >
          {fullPage ? '页级定位（不铺满整页）' : style.label}
        </div>
      )}
      {error && <div className="mt-2 text-sm text-red-500">{error}</div>}
    </div>
  );
}
