import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ImagePlus, MessageSquareText, Plus, RefreshCw, Send, Trash2, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../api.js';
import { encodeImage, streamChat } from './stream.js';
import type { ChatConversation, ChatMessage, ChatModels, LiveToolCall, ToolTraceEntry } from './types.js';

/**
 * The assistant, in a side panel.
 *
 * A floating toggle, a right-anchored panel, and one conversation on screen at
 * a time. The transcript is server truth fetched like everything else; the
 * only client-side state is the turn in flight — the user line just sent and
 * the assistant line growing token by token, with its tool calls as chips.
 * When the stream ends the queries are invalidated and the draft hands over to
 * the stored rows, so a reload shows exactly what the stream showed.
 */

interface Draft {
  user: { content: string; previews: string[] } | null;
  text: string;
  tools: LiveToolCall[];
  failed: string | null;
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
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [showBackfill, setShowBackfill] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

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
  });

  const previews = useMemo(() => files.map((f) => URL.createObjectURL(f)), [files]);
  useEffect(() => () => previews.forEach((url) => URL.revokeObjectURL(url)), [previews]);

  const rows = conversationId ? (messages.data?.messages ?? []) : [];
  const streaming = draft !== null && draft.failed === null && draftIsLive(draft, rows);

  // Follow the conversation as it grows — a stream that scrolls out of view
  // reads as a hang.
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [rows.length, draft?.text, draft?.tools.length]);

  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/api/chat/conversations/${id}`),
    onSuccess: () => {
      setConversationId(null);
      setDraft(null);
      void queryClient.invalidateQueries({ queryKey: ['chat-conversations'] });
    },
  });

  const send = async (): Promise<void> => {
    const content = input.trim();
    if ((!content && files.length === 0) || streaming) return;

    setInput('');
    const chosen = files;
    setFiles([]);
    setDraft({
      user: { content, previews: chosen.map((f) => URL.createObjectURL(f)) },
      text: '',
      tools: [],
      failed: null,
    });

    try {
      let id = conversationId;
      if (!id) {
        const created = await api.post<ChatConversation>('/api/chat/conversations', {
          model: model ?? undefined,
        });
        id = created.id;
        setConversationId(id);
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

      await streamChat(id, {
        content,
        ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
        ...(model ? { model } : {}),
      }, {
        onToken: (text) => setDraft((d) => (d ? { ...d, text: d.text + text } : d)),
        onToolStart: (call) =>
          setDraft((d) =>
            d ? { ...d, tools: [...d.tools, { call_id: call.call_id, name: call.name }] } : d,
          ),
        onToolEnd: (call) =>
          setDraft((d) =>
            d
              ? {
                  ...d,
                  tools: d.tools.map((t) =>
                    t.call_id === call.call_id ? { ...t, ok: call.ok, summary: call.summary } : t,
                  ),
                }
              : d,
          ),
        onDone: () => {
          void Promise.all([
            queryClient.invalidateQueries({ queryKey: ['chat-messages', id] }),
            queryClient.invalidateQueries({ queryKey: ['chat-conversations'] }),
          ]).then(() => setDraft(null));
        },
        onError: (error) =>
          setDraft((d) => (d ? { ...d, failed: error.message || error.code } : d)),
      });
    } catch (err) {
      setDraft((d) => (d ? { ...d, failed: (err as Error).message } : d));
    }
  };

  const configured = models.data?.configured ?? true;
  const modelValue = model ?? currentModel(conversations.data?.conversations, conversationId) ?? models.data?.default ?? '';

  return (
    <aside className="chat-panel" role="complementary" aria-label="Assistant">
      <div className="chat-head">
        <span className="eyebrow">Assistant</span>
        <select
          className="chat-select"
          aria-label="Conversation"
          value={conversationId ?? ''}
          onChange={(e) => {
            setConversationId(e.target.value || null);
            setDraft(null);
          }}
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
            onClick={() => {
              setConversationId(null);
              setDraft(null);
            }}
          >
            <Plus size={15} />
          </button>
          <button
            className="chat-icon-btn"
            aria-label="Rescan the mailbox"
            aria-pressed={showBackfill}
            onClick={() => setShowBackfill((v) => !v)}
          >
            <RefreshCw size={14} />
          </button>
          {conversationId ? (
            <button
              className="chat-icon-btn"
              aria-label="Delete this conversation"
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
        {conversationId === null && !draft ? (
          <p className="chat-note" style={{ border: 0 }}>
            Ask about your applications, the statistics behind the board, or what an email
            actually said. The assistant reads — it never sends, moves or deletes anything.
          </p>
        ) : null}

        {rows.map((m) =>
          m.role === 'user' ? (
            <UserLine key={m.id} content={m.content} images={m.attachment_ids.map((id) => `/api/chat/attachments/${id}`)} />
          ) : (
            <AssistantLine key={m.id} content={m.content} trace={m.tool_trace} />
          ),
        )}

        {draft?.user && draftIsLive(draft, rows) ? (
          <UserLine content={draft.user.content} images={draft.user.previews} />
        ) : null}
        {draft && draftIsLive(draft, rows) ? (
          <div className="chat-line">
            <ToolChips tools={draft.tools} />
            {draft.text ? (
              <div className="chat-assistant">
                <Markdown content={draft.text} />
                <span className="chat-cursor" />
              </div>
            ) : draft.failed === null ? (
              <div className="chat-assistant muted-55">thinking…</div>
            ) : null}
            {draft.failed !== null ? (
              <div className="chat-note" role="alert">
                The assistant could not answer: {draft.failed}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

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
          disabled={!configured || streaming}
          onClick={() => fileInput.current?.click()}
        >
          <ImagePlus size={16} />
        </button>
        <textarea
          className="chat-input"
          rows={1}
          placeholder={configured ? 'Ask about your pipeline…' : 'No model configured'}
          value={input}
          disabled={!configured}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <button
          className="chat-icon-btn chat-send"
          aria-label="Send"
          disabled={!configured || streaming || (!input.trim() && files.length === 0)}
          onClick={() => void send()}
        >
          <Send size={16} />
        </button>
      </div>

      {models.data && models.data.models.length > 1 ? (
        <div className="chat-model-row">
          <span className="eyebrow">Model</span>
          <select
            className="chat-select"
            aria-label="Model"
            value={modelValue}
            onChange={(e) => setModel(e.target.value)}
          >
            {models.data.models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
      ) : null}
    </aside>
  );
}

/** The draft belongs on screen until the refetch lands its stored twin. */
function draftIsLive(draft: Draft, rows: ChatMessage[]): boolean {
  if (!draft.user) return true;
  const last = rows[rows.length - 1];
  return !(last?.role === 'assistant' && rows.some((m) => m.role === 'user' && m.content === draft.user?.content));
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
    <div className="chat-line" style={{ alignItems: 'flex-end' }}>
      {images.length ? (
        <div className="chat-attach-row" style={{ padding: 0 }}>
          {images.map((src) => (
            <img key={src} src={src} alt="attachment" className="chat-thumb" />
          ))}
        </div>
      ) : null}
      {content ? <div className="chat-user">{content}</div> : null}
    </div>
  );
}

function AssistantLine({ content, trace }: { content: string; trace: ToolTraceEntry[] }) {
  return (
    <div className="chat-line">
      <ToolChips
        tools={trace.map((t, i) => ({ call_id: String(i), name: t.name, ok: t.ok, summary: t.summary }))}
      />
      <div className="chat-assistant">
        <Markdown content={content} />
      </div>
    </div>
  );
}

/** Model answers are markdown; `react-markdown` renders it rather than a
 * hand-rolled approximation (decisions.md LIB-1). Links open away from the
 * app, and raw HTML stays what react-markdown makes of it by default: text. */
function Markdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

/**
 * Tool calls, visibly. The same reason every derived claim on the board
 * carries provenance: the answer is only trustworthy if you can see where the
 * assistant looked.
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
