import { createParser, type EventSourceMessage } from 'eventsource-parser';
import { ApiError, getCsrf } from '../api.js';
import type { ToolTraceEntry } from './types.js';

/**
 * The message stream.
 *
 * `EventSource` cannot POST, so this reads the same SSE frames the rest of the
 * app already speaks off a `fetch` body instead. The wire format is parsed by
 * `eventsource-parser` rather than by hand (decisions.md LIB-1) — comments,
 * CRLF endings and split frames are its business, not ours.
 */

export interface StreamHandlers {
  onToken: (text: string) => void;
  onToolStart: (call: { call_id: string; name: string; arguments: Record<string, unknown> }) => void;
  onToolEnd: (call: { call_id: string; name: string; ok: boolean; summary: string }) => void;
  onDone: (done: { message_id: string; content: string; tool_trace: ToolTraceEntry[] }) => void;
  onError: (error: { code: string; message: string }) => void;
}

export async function streamChat(
  conversationId: string,
  body: { content: string; attachment_ids?: string[]; model?: string },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const csrf = getCsrf();
  const res = await fetch(`/api/chat/conversations/${conversationId}/messages`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(csrf ? { 'x-csrf-token': csrf } : {}),
    },
    body: JSON.stringify(body),
    credentials: 'same-origin',
    signal,
  });

  if (!res.ok || !res.body) {
    const parsed = (await res.json().catch(() => ({}))) as {
      error?: { code?: string; message?: string };
    };
    throw new ApiError(
      parsed.error?.code ?? 'unknown',
      parsed.error?.message ?? 'the stream did not open',
      res.status,
    );
  }

  const parser = createParser({
    onEvent: (message: EventSourceMessage) => {
      const kind = message.event;
      if (!kind || !message.data) return;
      const payload = JSON.parse(message.data) as never;
      if (kind === 'token') handlers.onToken((payload as { text: string }).text);
      else if (kind === 'tool.start') handlers.onToolStart(payload);
      else if (kind === 'tool.end') handlers.onToolEnd(payload);
      else if (kind === 'done') handlers.onDone(payload);
      else if (kind === 'error') handlers.onError(payload);
    },
  });

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parser.feed(decoder.decode(value, { stream: true }));
  }
}

/** Read a picked file into the JSON the attachment route accepts. */
export function encodeImage(file: File): Promise<{ media_type: string; data: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('could not read the file'));
    reader.onload = () => {
      const url = reader.result as string;
      const comma = url.indexOf(',');
      resolve({ media_type: file.type, data: url.slice(comma + 1) });
    };
    reader.readAsDataURL(file);
  });
}
