import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Check,
  Copy,
  Eye,
  ImagePlus,
  MessageSquareText,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Square,
  Trash2,
  Wrench,
  X,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, type ApplicationDetail } from '../api.js';
import { encodeImage, retryChat, streamChat, type StreamHandlers } from './stream.js';
import { useViewedApplication } from './viewing.js';
import type { ChatConversation, ChatMessage, ChatModels, LiveToolCall, ToolTraceEntry } from './types.js';

/**
 * The assistant, in a side panel.
 *
 * The transcript has one source of truth: the messages query. A turn in flight
 * writes the user's line into that cache optimistically — the gateway has
 * already stored it by the time the stream opens, so any refetch replaces the
 * placeholder with the real row rather than adding a second copy of it. What
 * lives outside the cache is only the answer being written, which has no row
 * to be until it ends.
 *
 * Everything else here is what a chat window is expected to do: copy an
 * answer, ask again, see what was looked up, stop a runaway, and stand beside
 * the record it is being asked about rather than on top of it.
 */

interface Stream {
  text: string;
  tools: LiveToolCall[];
  failed: string | null;
  stopped: boolean;
}

/**
 * Which conversation was last open.
 *
 * The panel unmounts when it closes, and a thread the user cannot get back to
 * is a thread the assistant appears to have forgotten — so the id outlives the
 * component. Storage can be unavailable (private mode, or a browser told to
 * refuse it), and a chat that will not open is worse than one that starts
 * fresh, so every access here fails quietly.
 */
const REMEMBERED = 'loop.chat.conversation';

function remembered(): string | null {
  try {
    return localStorage.getItem(REMEMBERED);
  } catch {
    return null;
  }
}

function remember(id: string | null): void {
  try {
    if (id) localStorage.setItem(REMEMBERED, id);
    else localStorage.removeItem(REMEMBERED);
  } catch {
    /* nothing to do about it, and nothing depends on it */
  }
}

export function ChatWidget() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  // The panel takes its width out of the page rather than covering it: the
  // record you are asking about has to stay readable while you ask.
  useEffect(() => {
    document.body.classList.toggle('chat-open', open);
    return () => document.body.classList.remove('chat-open');
  }, [open]);

  // The toggle stands down while the panel is up — it would sit exactly on
  // the send button — and the panel carries its own close.
  return open ? (
    <ChatPanel onClose={() => setOpen(false)} />
  ) : (
    <button
      className="chat-toggle"
      aria-label="Open the assistant"
      aria-expanded={false}
      onClick={() => setOpen(true)}
    >
      <MessageSquareText size={19} />
    </button>
  );
}

function ChatPanel({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [conversationId, setConversationId] = useState<string | null>(remembered);
  const [model, setModel] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [stream, setStream] = useState<Stream | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showBackfill, setShowBackfill] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const running = useRef<AbortController | null>(null);

  const models = useQuery({
    queryKey: ['chat-models'],
    queryFn: () => api.get<ChatModels>('/api/chat/models'),
    staleTime: 5 * 60_000,
  });
  const conversations = useQuery({
    queryKey: ['chat-conversations'],
    queryFn: () => api.get<{ conversations: ChatConversation[] }>('/api/chat/conversations'),
  });
  const messages = useQuery({
    queryKey: ['chat-messages', conversationId],
    queryFn: () => api.get<{ messages: ChatMessage[] }>(`/api/chat/conversations/${conversationId}`),
    enabled: conversationId !== null,
    retry: false,
  });

  // Show a conversation, and remember it past the panel closing.
  const show = (id: string | null): void => {
    running.current?.abort();
    setConversationId(id);
    remember(id);
    setStream(null);
    setNotice(null);
  };

  // The remembered thread may have been deleted since — from another tab, or
  // by this user last week. Falling back to a new one beats an empty panel.
  useEffect(() => {
    if (messages.isError) show(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.isError]);

  // What the user has open elsewhere in the app, which the assistant is told
  // about so "this one" resolves. The name comes from the cache the detail
  // view already filled; without it the chip still says something true.
  const viewing = useViewedApplication();
  const viewed = useQuery<ApplicationDetail>({
    queryKey: ['application', viewing ?? ''],
    enabled: false,
  });

  const previews = useMemo(() => files.map((f) => URL.createObjectURL(f)), [files]);
  useEffect(() => () => previews.forEach((url) => URL.revokeObjectURL(url)), [previews]);

  const rows = conversationId ? (messages.data?.messages ?? []) : [];
  const streaming = stream !== null && stream.failed === null && !stream.stopped;

  // Follow the conversation as it grows — a stream that scrolls out of view
  // reads as a hang.
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [rows.length, stream?.text, stream?.tools.length]);

  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/api/chat/conversations/${id}`),
    onSuccess: () => {
      show(null);
      void queryClient.invalidateQueries({ queryKey: ['chat-conversations'] });
    },
  });

  /** The handlers every turn shares: tokens in, tools traced, answer stored. */
  const handlers = (id: string): StreamHandlers => ({
    onToken: (text) => setStream((s) => (s ? { ...s, text: s.text + text } : s)),
    onToolStart: (call) =>
      setStream((s) =>
        s ? { ...s, tools: [...s.tools, { call_id: call.call_id, name: call.name }] } : s,
      ),
    onToolEnd: (call) =>
      setStream((s) =>
        s
          ? {
              ...s,
              tools: s.tools.map((t) =>
                t.call_id === call.call_id ? { ...t, ok: call.ok, summary: call.summary } : t,
              ),
            }
          : s,
      ),
    // Outlives the turn on purpose: "that model cannot see images" is still
    // true once the answer has landed, which is when the user notices the
    // picture went unmentioned.
    onNotice: (said) => setNotice(said.message),
    onDone: () => {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['chat-messages', id] }),
        queryClient.invalidateQueries({ queryKey: ['chat-conversations'] }),
      ]).then(() => setStream(null));
    },
    onError: (error) =>
      setStream((s) => (s ? { ...s, failed: error.message || error.code } : s)),
  });

  const send = async (): Promise<void> => {
    const content = input.trim();
    if ((!content && files.length === 0) || streaming) return;

    setInput('');
    setNotice(null);
    const chosen = files;
    setFiles([]);
    setStream({ text: '', tools: [], failed: null, stopped: false });

    try {
      let id = conversationId;
      if (!id) {
        const created = await api.post<ChatConversation>('/api/chat/conversations', {
          model: model ?? undefined,
        });
        id = created.id;
        setConversationId(id);
        remember(id);
      }
      const attachmentIds: string[] = [];
      for (const file of chosen) {
        const encoded = await encodeImage(file);
        const uploaded = await api.post<{ id: string }>(
          `/api/chat/conversations/${id}/attachments`,
          encoded,
        );
        attachmentIds.push(uploaded.id);
      }

      // On screen before the first token, and replaced — not duplicated — by
      // the row the gateway has already written.
      queryClient.setQueryData<{ messages: ChatMessage[] }>(['chat-messages', id], (old) => ({
        messages: [
          ...(old?.messages ?? []),
          {
            id: `pending-${attachmentIds.join('-')}-${content.length}`,
            role: 'user',
            content,
            tool_trace: [],
            model: null,
            created_at: new Date().toISOString(),
            attachment_ids: attachmentIds,
          },
        ],
      }));

      const controller = new AbortController();
      running.current = controller;
      await streamChat(
        id,
        {
          content,
          ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
          ...(model ? { model } : {}),
          ...(viewing ? { application_id: viewing } : {}),
        },
        handlers(id),
        controller.signal,
      );
    } catch (err) {
      stopped(err, setStream);
    }
  };

  const again = async (): Promise<void> => {
    const id = conversationId;
    if (!id || streaming) return;
    setNotice(null);
    setStream({ text: '', tools: [], failed: null, stopped: false });
    // The answer goes now rather than when the replacement lands: leaving it
    // on screen under a spinner reads as a second answer being written.
    queryClient.setQueryData<{ messages: ChatMessage[] }>(['chat-messages', id], (old) =>
      old ? { messages: old.messages.slice(0, -1) } : old,
    );
    try {
      const controller = new AbortController();
      running.current = controller;
      await retryChat(
        id,
        { ...(model ? { model } : {}), ...(viewing ? { application_id: viewing } : {}) },
        handlers(id),
        controller.signal,
      );
    } catch (err) {
      stopped(err, setStream);
    }
  };

  const configured = models.data?.configured ?? true;
  const modelValue =
    model ?? currentModel(conversations.data?.conversations, conversationId) ?? models.data?.default ?? '';
  const blind =
    (models.data?.vision_by_model?.[modelValue] ?? models.data?.vision ?? null) === false;
  // The picker shows even for a single-model server — which is what llama.cpp
  // usually is — because knowing what is answering is worth a row. A model the
  // thread already runs on stays in the list even if the server stopped
  // offering it, so the select never blanks itself.
  const choices = useMemo(() => {
    const served = models.data?.models ?? [];
    return modelValue && !served.includes(modelValue) ? [modelValue, ...served] : served;
  }, [models.data?.models, modelValue]);

  const answered = rows.length > 0 && rows[rows.length - 1]?.role === 'assistant';

  return (
    <aside className="chat-panel" role="complementary" aria-label="Assistant">
      <div className="chat-head">
        <span className="eyebrow">Assistant</span>
        <select
          className="chat-select"
          aria-label="Conversation"
          value={conversationId ?? ''}
          onChange={(e) => show(e.target.value || null)}
        >
          <option value="">New conversation</option>
          {(conversations.data?.conversations ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.title ?? 'Untitled'}
            </option>
          ))}
        </select>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-2)' }}>
          <button
            className="chat-icon-btn"
            aria-label="New conversation"
            // Nothing to start when the panel is already on an empty new one:
            // a button that does nothing should look like it.
            disabled={conversationId === null && stream === null}
            title="Start a new conversation"
            onClick={() => show(null)}
          >
            <Plus size={15} />
          </button>
          <button
            className="chat-icon-btn"
            aria-label="Rescan the mailbox"
            aria-pressed={showBackfill}
            title="Re-read the mailbox"
            onClick={() => setShowBackfill((v) => !v)}
          >
            <RefreshCw size={14} />
          </button>
          {conversationId ? (
            <button
              className="chat-icon-btn"
              aria-label="Delete this conversation"
              title="Delete this conversation"
              onClick={() => remove.mutate(conversationId)}
            >
              <Trash2 size={14} />
            </button>
          ) : null}
          <button className="chat-icon-btn" aria-label="Close the assistant" onClick={onClose}>
            <X size={15} />
          </button>
        </div>
      </div>

      {showBackfill ? <BackfillStrip onDone={() => setShowBackfill(false)} /> : null}

      {!configured ? (
        <p className="chat-note">
          No local model is configured. Set <code>MODEL_BASE_URL</code> to a llama.cpp server and
          the assistant switches on — same switch as extraction rung 3.
        </p>
      ) : null}

      <div className="chat-messages" ref={scroller}>
        {rows.length === 0 && stream === null ? (
          <p className="chat-note" style={{ border: 0 }}>
            Ask about your applications, the statistics behind the board, or what an email
            actually said. The assistant reads — it never sends, moves or deletes anything.
          </p>
        ) : null}

        {rows.map((m, i) =>
          m.role === 'user' ? (
            <UserLine
              key={m.id}
              content={m.content}
              images={m.attachment_ids.map((id) => `/api/chat/attachments/${id}`)}
            />
          ) : (
            <AssistantLine
              key={m.id}
              content={m.content}
              trace={m.tool_trace}
              // Only the last answer can be replaced: redoing an earlier one
              // would rewrite what everything after it was said in reply to.
              onRetry={i === rows.length - 1 && answered && !streaming ? () => void again() : undefined}
            />
          ),
        )}

        {stream !== null ? (
          <div className="chat-line">
            <ToolChips tools={stream.tools} />
            {stream.text ? (
              <div className="chat-assistant">
                <Markdown content={stream.text} />
                {streaming ? <span className="chat-cursor" /> : null}
              </div>
            ) : stream.failed === null && !stream.stopped ? (
              <div className="chat-assistant muted-55">thinking…</div>
            ) : null}
            {stream.stopped ? <p className="chat-stopped">Stopped.</p> : null}
            {stream.failed !== null ? (
              <div className="chat-note" role="alert">
                The assistant could not answer: {stream.failed}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      {notice ? (
        <p className="chat-note" role="status">
          {notice}
        </p>
      ) : null}

      {viewing ? (
        <div className="chat-context">
          <Eye size={13} />
          <span>
            Asking about <strong>{viewed.data?.company ?? 'the open application'}</strong>
          </span>
        </div>
      ) : null}

      {previews.length ? (
        <div className="chat-attach-row">
          {previews.map((url, i) => (
            <span key={url} className="chat-thumb-wrap">
              <img src={url} alt="" className="chat-thumb" />
              <button
                aria-label="Remove image"
                onClick={() => setFiles((f) => f.filter((_x, j) => j !== i))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      ) : null}

      <div className="chat-input-row">
        <input
          ref={fileInput}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          multiple
          hidden
          onChange={(e) => {
            const picked = Array.from(e.target.files ?? []);
            setFiles((f) => [...f, ...picked].slice(0, 4));
            e.target.value = '';
          }}
        />
        <button
          className="chat-icon-btn"
          aria-label="Attach an image"
          // A model with no projector answers a picture with a 500. When the
          // server says so, say so here instead of finding out on send.
          disabled={!configured || streaming || blind}
          title={
            blind
              ? 'This model cannot see images — serve one with a vision projector, '
                + 'e.g. llama-server -hf ggml-org/gemma-3-4b-it-GGUF'
              : 'Attach an image'
          }
          onClick={() => fileInput.current?.click()}
        >
          <ImagePlus size={16} />
        </button>
        <textarea
          ref={composer}
          className="chat-input"
          rows={1}
          placeholder={configured ? 'Ask about your pipeline…' : 'No model configured'}
          value={input}
          disabled={!configured}
          onChange={(e) => {
            setInput(e.target.value);
            const box = composer.current;
            if (box) {
              box.style.height = 'auto';
              box.style.height = `${Math.min(box.scrollHeight, 120)}px`;
            }
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {streaming ? (
          <button
            className="chat-icon-btn chat-send"
            aria-label="Stop"
            title="Stop"
            onClick={() => running.current?.abort()}
          >
            <Square size={14} />
          </button>
        ) : (
          <button
            className="chat-icon-btn chat-send"
            aria-label="Send"
            disabled={!configured || (!input.trim() && files.length === 0)}
            onClick={() => void send()}
          >
            <Send size={16} />
          </button>
        )}
      </div>

      {models.data?.configured ? (
        <div className="chat-model-row">
          <span className="eyebrow">Model</span>
          <select
            className="chat-select"
            aria-label="Model"
            value={modelValue}
            onChange={(e) => setModel(e.target.value)}
          >
            {choices.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          {blind ? <span className="muted-55">text only</span> : null}
        </div>
      ) : null}
    </aside>
  );
}

/** An aborted turn is the user's own doing, not a failure to report as one. */
function stopped(
  error: unknown,
  setStream: (update: (s: Stream | null) => Stream | null) => void,
): void {
  const aborted = error instanceof DOMException && error.name === 'AbortError';
  setStream((s) =>
    s ? { ...s, stopped: aborted, failed: aborted ? null : (error as Error).message } : s,
  );
}

function currentModel(
  conversations: ChatConversation[] | undefined,
  id: string | null,
): string | null {
  if (!id) return null;
  return conversations?.find((c) => c.id === id)?.model ?? null;
}

function UserLine({ content, images }: { content: string; images: string[] }) {
  return (
    <div className="chat-line chat-line-user">
      {images.length ? (
        <div className="chat-attach-row" style={{ padding: 0 }}>
          {images.map((src) => (
            <img key={src} src={src} alt="attachment" className="chat-thumb" />
          ))}
        </div>
      ) : null}
      {content ? <div className="chat-user">{content}</div> : null}
      <MessageActions content={content} />
    </div>
  );
}

function AssistantLine({
  content,
  trace,
  onRetry,
}: {
  content: string;
  trace: ToolTraceEntry[];
  onRetry?: () => void;
}) {
  return (
    <div className="chat-line">
      <div className="chat-assistant">
        <Markdown content={content} />
      </div>
      <MessageActions content={content} trace={trace} onRetry={onRetry} />
    </div>
  );
}

/**
 * What you can do with a message once it exists: take it, ask again, or look
 * at where it came from.
 *
 * The provenance is behind a disclosure rather than always open — the chips
 * were noise on every answer — but it is one click away on every answer that
 * used a tool, because an answer you cannot check is an answer you cannot use.
 */
function MessageActions({
  content,
  trace,
  onRetry,
}: {
  content: string;
  trace?: ToolTraceEntry[];
  onRetry?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(false);
  const used = trace?.length ?? 0;

  const copy = async (): Promise<void> => {
    await copyText(content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  if (!content && !used) return null;
  return (
    <>
      <div className="chat-actions">
        {content ? (
          <button className="chat-action" onClick={() => void copy()} title="Copy">
            {copied ? <Check size={13} /> : <Copy size={13} />}
            <span>{copied ? 'Copied' : 'Copy'}</span>
          </button>
        ) : null}
        {onRetry ? (
          <button className="chat-action" onClick={onRetry} title="Answer again">
            <RotateCcw size={13} />
            <span>Retry</span>
          </button>
        ) : null}
        {used ? (
          <button
            className="chat-action"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            title="What it looked at"
          >
            <Wrench size={13} />
            <span>
              {used} {used === 1 ? 'tool' : 'tools'}
            </span>
          </button>
        ) : null}
      </div>
      {open && trace ? (
        <div className="chat-trace">
          {trace.map((t, i) => (
            <div key={`${t.name}-${i}`} className={t.ok ? '' : 'chat-trace-failed'}>
              <span className="chat-tool-name">{t.name.replace(/_/g, ' ')}</span>
              {t.summary ? <span className="muted-65"> · {t.summary}</span> : null}
              {Object.keys(t.arguments ?? {}).length ? (
                <code>{JSON.stringify(t.arguments)}</code>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

/** Clipboard, with the fallback a page served over plain http still needs. */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    /* not a secure context, or permission refused */
  }
  const box = document.createElement('textarea');
  box.value = text;
  box.style.position = 'fixed';
  box.style.opacity = '0';
  document.body.appendChild(box);
  box.select();
  try {
    document.execCommand('copy');
  } finally {
    document.body.removeChild(box);
  }
}

/** Model answers are markdown; `react-markdown` renders it rather than a
 * hand-rolled approximation (decisions.md LIB-1). Links open away from the
 * app, and raw HTML stays what react-markdown makes of it by default: text. */
function Markdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        // `node` is react-markdown's hast element, not a DOM attribute — it
        // has to come off before the rest is spread onto the anchor.
        a: ({ node, ...props }) => (
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

/**
 * Tool calls while they happen. The finished trace lives behind the message's
 * own disclosure; this is the part that has to be visible as it runs, because
 * a panel that shows nothing for eight seconds looks broken.
 */
function ToolChips({ tools }: { tools: LiveToolCall[] }) {
  if (tools.length === 0) return null;
  return (
    <div className="chat-tools">
      {tools.map((t) => (
        <span key={t.call_id} className={`chat-tool ${t.ok === false ? 'chat-tool-failed' : ''}`}>
          <span className="chat-tool-name">{t.name.replace(/_/g, ' ')}</span>
          {t.summary ? <span className="muted-55"> · {t.summary}</span> : null}
          {t.ok === undefined ? <span className="muted-55"> · running…</span> : null}
        </span>
      ))}
    </div>
  );
}

/**
 * The manual backfill, from the panel. The same endpoint as onboarding's
 * "how far back?" — the connector holds the credentials, this only says when.
 * Progress arrives over the app's SSE stream as `scan.progress`, which
 * `useLiveUpdates` already writes into the `['scan']` key.
 */
function BackfillStrip({ onDone }: { onDone: () => void }) {
  const [months, setMonths] = useState(12);
  const scan = useQuery<{ read: number; remaining: number }>({ queryKey: ['scan'], enabled: false });
  const start = useMutation({
    mutationFn: () => api.post('/api/mailboxes/backfill', { months }),
  });

  return (
    <div className="chat-backfill">
      <label className="eyebrow" htmlFor="chat-backfill-months">
        Re-read the mailbox
      </label>
      <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
        <input
          id="chat-backfill-months"
          className="chat-months"
          type="number"
          min={1}
          max={60}
          value={months}
          onChange={(e) => setMonths(Math.max(1, Math.min(60, Number(e.target.value) || 1)))}
        />
        <span className="muted-65" style={{ fontSize: 12.5 }}>months back</span>
        <button
          className="filter-chip"
          style={{ marginLeft: 'auto' }}
          disabled={start.isPending}
          onClick={() => start.mutate()}
        >
          {start.isSuccess ? 'Requested' : 'Start'}
        </button>
        <button className="filter-chip" style={{ border: 0 }} onClick={onDone}>
          Close
        </button>
      </div>
      {start.isSuccess ? (
        <p className="muted-55" style={{ fontSize: 12, margin: '4px 0 0' }}>
          {scan.data
            ? `Reading — ${scan.data.read} read, ${scan.data.remaining} to go.`
            : 'The connector picks it up on its next beat; progress shows here.'}
        </p>
      ) : null}
      {start.isError ? (
        <p className="muted-55" style={{ fontSize: 12, margin: '4px 0 0' }} role="alert">
          The rescan could not be requested — is a mailbox connected?
        </p>
      ) : null}
    </div>
  );
}
