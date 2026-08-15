/**
 * The chat surface's wire shapes, mirrored from `loop/api/routes/chat.py`.
 */

export interface ChatModels {
  configured: boolean;
  reachable: boolean;
  default: string;
  models: string[];
  /** Whether the model that answers by default can look at an image. Null when
   * the server did not say, which is not the same as no — only a definite
   * false is a warning. */
  vision: boolean | null;
  /** The same answer per model, since it is really a fact about the server
   * each one is loaded in. */
  vision_by_model: Record<string, boolean>;
}

export interface ChatConversation {
  id: string;
  title: string | null;
  model: string | null;
  created_at: string;
  last_message_at: string;
  message_count: number;
}

/** One tool call the assistant made: its name, arguments and a one-line
 * outcome. Never the tool's output — that never leaves the turn it ran in. */
export interface ToolTraceEntry {
  name: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  summary: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  tool_trace: ToolTraceEntry[];
  model: string | null;
  created_at: string;
  attachment_ids: string[];
}

/** A tool call in flight, rendered as a chip while the stream runs. */
export interface LiveToolCall {
  call_id: string;
  name: string;
  ok?: boolean;
  summary?: string;
}
