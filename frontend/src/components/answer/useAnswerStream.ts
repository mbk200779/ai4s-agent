'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  ClaimEvent, DoneEvent, RawEvent, RetrievalEvent, StageEvent, StreamState, Uncertainty,
} from '@/types/stream';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

const INITIAL: StreamState = {
  status: 'idle',
  traceId: null,
  stages: [],
  retrieval: null,
  text: '',
  claims: [],
  uncertainty: null,
  done: null,
  error: null,
  heartbeats: 0,
  events: [],
};

/**
 * 逐帧解析 SSE。
 *
 * 为什么不用 EventSource：它只支持 GET，问题文本就得塞进 query string
 * （中文要编码、会进访问日志、长度也受限）。fetch + ReadableStream 没有这些约束。
 *
 * 为什么要手写分帧：`fetch` 给的是任意切分的字节流，一个 TCP 块里可能有
 * 三条半事件。按 `\n\n` 切、余下的留在缓冲里，是唯一稳的做法；
 * 直接把每块 `JSON.parse` 会在"事件被切开"时随机炸。
 */
async function pumpSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (name: string, data: Record<string, unknown>) => void,
  onHeartbeat: () => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let cut = buffer.indexOf('\n\n');
    while (cut >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);

      let name = '';
      let pinged = false;
      const dataLines: string[] = [];
      for (const line of frame.split('\n')) {
        // `: ping` 是心跳注释行，不产生事件；但要计数——
        // 它归零说明反代在攒包，用户看到的"卡住"就是这么来的。
        if (line.startsWith(':')) {
          pinged = true;
          continue;
        }
        if (line.startsWith('event: ')) name = line.slice(7).trim();
        else if (line.startsWith('data: ')) dataLines.push(line.slice(6));
      }
      if (pinged) onHeartbeat();
      if (name && dataLines.length) {
        try {
          onEvent(name, JSON.parse(dataLines.join('\n')) as Record<string, unknown>);
        } catch {
          // 单帧坏掉不该把整条流带崩：丢掉这一帧，后面的照常渲染
        }
      }
      cut = buffer.indexOf('\n\n');
    }
  }
}

export interface UseAnswerStream {
  state: StreamState;
  /** 重发同一个问题（按钮「重试」用） */
  restart: () => void;
}

/**
 * 订阅一次流式回答。
 *
 * `question` 变化即重新发起（一条消息对应一个实例，历史消息各自保留自己的状态）。
 * 组件卸载时 abort：用户切走页面后还继续生成纯属浪费 token。
 */
export function useAnswerStream(question: string, topK = 8): UseAnswerStream {
  const [state, setState] = useState<StreamState>(INITIAL);
  const [nonce, setNonce] = useState(0);
  const abortRef = useRef<AbortController | null>(null);

  const restart = useCallback(() => {
    setState(INITIAL);
    setNonce((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!question) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setState({ ...INITIAL, status: 'streaming' });

    const patch = (fn: (prev: StreamState) => StreamState) => setState(fn);

    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/v1/qa/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify({ question, top_k: topK }),
          signal: controller.signal,
        });
        if (!res.ok || !res.body) {
          throw new Error(`HTTP ${res.status}`);
        }

        await pumpSse(
          res.body,
          (name, raw) => {
            const data = raw as unknown as Record<string, never>;
            patch((prev) => {
              const events: RawEvent[] = [...prev.events, { event: name, data: raw }];
              const traceId = (raw.trace_id as string) || prev.traceId;

              switch (name) {
                case 'stage':
                  return { ...prev, traceId, events, stages: [...prev.stages, data as unknown as StageEvent] };
                case 'retrieval':
                  return { ...prev, traceId, events, retrieval: data as unknown as RetrievalEvent };
                case 'token':
                  // 正文只做拼接，不在这里插角标：角标必须等 claim 到达
                  return { ...prev, traceId, events, text: prev.text + ((raw.text as string) || '') };
                case 'claim':
                  return { ...prev, traceId, events, claims: [...prev.claims, data as unknown as ClaimEvent] };
                case 'uncertainty':
                  // 检索后先到的那半份（空态 / 未识别）。它比 done 早几秒，
                  // 读者不必等正文生成完才知道"这段材料本身有问题"。
                  return { ...prev, traceId, events, uncertainty: data as unknown as Uncertainty };
                case 'done': {
                  const done = data as unknown as DoneEvent;
                  return {
                    ...prev, traceId, events, status: 'done', done,
                    // done 里那份是权威版（多了冲突），覆盖早到的那份
                    uncertainty: done.uncertainty ?? prev.uncertainty,
                  };
                }
                case 'error':
                  return { ...prev, traceId, events, status: 'error', error: (raw.message as string) || '生成中断' };
                default:
                  return { ...prev, traceId, events };
              }
            });
          },
          () => patch((prev) => ({ ...prev, heartbeats: prev.heartbeats + 1 })),
        );

        // 流正常闭合但没有 done：说明服务端在生成中途没了（进程重启/反代掐断）。
        // 这种情况必须显式标错，否则界面会停在一段"看起来像答完了"的半截文字上。
        patch((prev) =>
          prev.status === 'streaming' ? { ...prev, status: 'error', error: '事件流提前结束（未收到 done）' } : prev,
        );
      } catch (err) {
        if (controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : '请求失败';
        patch((prev) => ({
          ...prev,
          status: 'error',
          error: prev.text ? `${message}（已保留已生成的部分）` : message,
        }));
      }
    })();

    return () => controller.abort();
  }, [question, topK, nonce]);

  return { state, restart };
}
