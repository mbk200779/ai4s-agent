import React from 'react';

/**
 * 轻量 Markdown 渲染器。
 *
 * 关键设计决策：不引入 react-markdown / remark 之类的依赖。
 * 本项目只需要处理模型返回的少量语法（标题、引用、列表、加粗、行内代码），
 * 自己实现约 100 行即可，既能去掉直接暴露给用户的 `#`、`**`、`>`
 * 和代码围栏残渣，又把体积与构建时间控制在零成本。
 */

type Block =
  | { kind: 'h'; level: number; text: string }
  | { kind: 'p'; text: string }
  | { kind: 'quote'; text: string }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; items: string[] }
  | { kind: 'hr' };

/** 去掉模型爱加的代码围栏，以及围栏被截断后残留的 ```markdown / "markdown 行。 */
function stripFences(src: string): string {
  return src
    .replace(/\r\n/g, '\n')
    .replace(/^[ \t]*(?:`{3,}|~{3,})[ \t]*[a-zA-Z]*[ \t]*$/gm, '')
    .replace(/^[ \t]*["'`]{0,3}[ \t]*(?:markdown|md)[ \t]*$/gim, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// 兼容模型实际输出的三种引用写法：
//   [引#3]              —— qa/大纲最常见的紧凑形式
//   [引#3: xxx.pdf p1]  —— 带来源文件名（可能是半角或全角冒号）
//   [3]                 —— 只写序号的学术写法
const INLINE =
  /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*|[\[［]引#\d+(?:[:：][^\]］\n]*)?[\]］]|[\[［]\d{1,2}[\]］])/g;
const CITE_NAMED = /^[\[［]引#(\d+)(?:[:：]\s*([^\]］]*))?[\]］]$/;
const CITE_PLAIN = /^[\[［](\d{1,2})[\]］]$/;

function renderInline(text: string, key: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  INLINE.lastIndex = 0;

  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];

    if (tok.startsWith('**')) {
      out.push(
        <strong key={`${key}-b${i}`} className="font-semibold text-[var(--ink)]">
          {tok.slice(2, -2)}
        </strong>,
      );
    } else if (tok.startsWith('`')) {
      out.push(
        <code
          key={`${key}-c${i}`}
          className="mono rounded bg-[var(--surface-2)] px-1 py-0.5 text-[0.9em] text-[var(--ink-2)]"
        >
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith('*')) {
      out.push(
        <em key={`${key}-i${i}`} className="italic">
          {tok.slice(1, -1)}
        </em>,
      );
    } else {
      const named = CITE_NAMED.exec(tok);
      const plain = CITE_PLAIN.exec(tok);
      const n = named?.[1] ?? plain?.[1] ?? '';
      const detail = named?.[2]?.trim();
      out.push(
        <sup
          key={`${key}-r${i}`}
          title={detail ? `来源：${detail}` : `引文片段 ${n}`}
          className="mono ml-0.5 cursor-help text-[10px] font-medium text-[var(--signal)]"
        >
          [{n}]
        </sup>,
      );
    }
    last = m.index + tok.length;
    i += 1;
  }

  if (last < text.length) out.push(text.slice(last));
  return out;
}

function parseBlocks(src: string): Block[] {
  const lines = src.split('\n');
  const blocks: Block[] = [];
  let para: string[] = [];
  let i = 0;

  const flush = () => {
    if (para.length) {
      blocks.push({ kind: 'p', text: para.join(' ') });
      para = [];
    }
  };

  while (i < lines.length) {
    const raw = lines[i];
    const t = raw.trim();

    if (!t) {
      flush();
      i += 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(t);
    if (heading) {
      flush();
      blocks.push({ kind: 'h', level: heading[1].length, text: heading[2].trim() });
      i += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) {
      flush();
      blocks.push({ kind: 'hr' });
      i += 1;
      continue;
    }

    if (/^>\s?/.test(t)) {
      flush();
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
        buf.push(lines[i].trim().replace(/^>\s?/, ''));
        i += 1;
      }
      blocks.push({ kind: 'quote', text: buf.join(' ') });
      continue;
    }

    if (/^[-*+]\s+/.test(t)) {
      flush();
      const items: string[] = [];
      while (i < lines.length && /^[-*+]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*+]\s+/, ''));
        i += 1;
      }
      blocks.push({ kind: 'ul', items });
      continue;
    }

    if (/^\d+[.)]\s+/.test(t)) {
      flush();
      const items: string[] = [];
      while (i < lines.length && /^\d+[.)]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+[.)]\s+/, ''));
        i += 1;
      }
      blocks.push({ kind: 'ol', items });
      continue;
    }

    para.push(t);
    i += 1;
  }

  flush();
  return blocks;
}

const H_CLASS: Record<number, string> = {
  1: 'display mt-6 mb-3 text-2xl first:mt-0',
  2: 'display mt-6 mb-2.5 text-xl first:mt-0',
  3: 'mt-5 mb-2 text-[15px] font-semibold text-[var(--ink)] first:mt-0',
  4: 'mt-4 mb-1.5 text-sm font-semibold text-[var(--ink)] first:mt-0',
};

export default function Markdown({
  source,
  hideTitle,
  className = '',
}: {
  source: string;
  /** 页面上已有同名标题时传入，避免正文里再重复一次大标题 */
  hideTitle?: string;
  className?: string;
}) {
  const blocks = parseBlocks(stripFences(source || ''));
  const seenH1 = new Set<string>();
  const titleKey = hideTitle?.replace(/\s+/g, '') ?? '';
  if (titleKey) seenH1.add(titleKey);

  return (
    <div className={`text-[15px] leading-7 text-[var(--ink)] ${className}`}>
      {blocks.map((b, idx) => {
        const key = `blk-${idx}`;

        switch (b.kind) {
          case 'h': {
            const level = Math.min(b.level, 4);
            // 模型常把同一个标题重复两次（围栏截断导致）；与页面标题同名也一并去掉
            const norm = b.text.replace(/\s+/g, '');
            if (level <= 2) {
              if (seenH1.has(norm)) return null;
              seenH1.add(norm);
            }
            const Tag = `h${level}` as keyof JSX.IntrinsicElements;
            return (
              <Tag key={key} className={H_CLASS[level]}>
                {renderInline(b.text, key)}
              </Tag>
            );
          }

          case 'quote':
            return (
              <blockquote
                key={key}
                className="my-4 border-l-2 border-[var(--signal)] pl-4 text-[13px] leading-6 text-[var(--ink-3)]"
              >
                {renderInline(b.text, key)}
              </blockquote>
            );

          case 'ul':
            return (
              <ul key={key} className="my-3 list-disc space-y-1.5 pl-5 marker:text-[var(--ink-3)]">
                {b.items.map((it, j) => (
                  <li key={`${key}-${j}`}>{renderInline(it, `${key}-${j}`)}</li>
                ))}
              </ul>
            );

          case 'ol':
            return (
              <ol key={key} className="my-3 list-decimal space-y-1.5 pl-5 marker:text-[var(--ink-3)]">
                {b.items.map((it, j) => (
                  <li key={`${key}-${j}`}>{renderInline(it, `${key}-${j}`)}</li>
                ))}
              </ol>
            );

          case 'hr':
            return <hr key={key} className="my-6 border-t border-[var(--line)]" />;

          default:
            return (
              <p key={key} className="my-3">
                {renderInline(b.text, key)}
              </p>
            );
        }
      })}
    </div>
  );
}
