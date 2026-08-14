import { ApiError, getCsrf } from '../api.js';
import type { ToolTraceEntry } from './types.js';

/**
 * The message stream.
 *
 * `EventSource` cannot POST, so this reads the same SSE frames the rest of the
 * app already speaks off a `fetch` body instead. Frames are separated by a
 * blank line; a frame with no `event:` line is a comment and is dropped.
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

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const dispatch = (frame: string): void => {
    let kind = '';
    let data = '';
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) kind = line.slice(6).trim();
      else if (line.startsWith('data:')) data += line.slice(5).trim();
    }
    if (!kind || !data) return;
    const payload = JSON.parse(data) as never;
    if (kind === 'token') handlers.onToken((payload as { text: string }).text);
    else if (kind === 'tool.start') handlers.onToolStart(payload);
    else if (kind === 'tool.end') handlers.onToolEnd(payload);
    else if (kind === 'done') handlers.onDone(payload);
    else if (kind === 'error') handlers.onError(payload);
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // Frames end in a blank line; whatever trails the last one stays buffered.
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) dispatch(frame);
  }
  if (buffer.trim()) dispatch(buffer);
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
