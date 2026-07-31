export type SelectOption = { value: string; label: string };

export type PendingAttachment = {
  id: string;
  filename: string;
  mime: string;
  data_base64: string;
  previewUrl: string;
  size: number;
};

export type StreamEvent = {
  seq: number;
  event_id: string;
  event_type: string;
  agent_id?: string | null;
  session_id: string | null;
  run_id?: string | null;
  run_seq?: number;
  created_at: string;
  data: Record<string, unknown>;
};

export type QuestionOption = { value: string; label: string };
export type QuestionItem = { id: string; question: string; multiple?: boolean; options: QuestionOption[] };
export type QuestionRequest = { question_id: string; questions: QuestionItem[] };
export type QuestionAnswer = { values: string[]; note: string };

export type ApprovalRequest = {
  approval_id: string;
  reason: string;
  action?: Record<string, unknown>;
};

export type MessagePart = Record<string, unknown> & { type?: string };

export type MessageRecord = {
  info?: {
    id?: string;
    session_id?: string;
    role?: string;
    time?: { created?: number; completed?: number };
    agent?: string;
    agent_kind?: string;
    context_id?: string | null;
    parent_call_id?: string | null;
    parent_id?: string;
    model?: { provider_id?: string; model_id?: string };
    tokens?: TokenUsage | null;
    path?: { cwd?: string; root?: string };
  };
  parts?: MessagePart[];
};

export type TokenUsage = {
  input?: number | null;
  output?: number | null;
  reasoning?: number | null;
  cache?: { read?: number | null; write?: number | null } | null;
};
