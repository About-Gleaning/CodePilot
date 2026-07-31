import type { MessageRecord, StreamEvent } from '../../types';

export type AgentSummary = {
  agent_id: string;
  revision_id: string;
  name: string;
  description?: string;
  source: 'builtin' | 'custom';
  archived: boolean;
  readonly?: boolean;
  validation_status: 'valid' | 'legacy_warning' | 'invalid';
  validation_issues?: Array<{ code: string; field?: string | null; message: string }>;
  default_provider?: string | null;
  default_model?: string | null;
  default_thinking_value?: string | null;
};

export type AgentDetail = AgentSummary & {
  system_prompt?: string;
  tool_names?: string[];
  mcp_server_names?: string[];
};

export type AgentRuntime = {
  agent_id: string;
  desired_state: 'STOPPED' | 'RUNNING';
  lifecycle_state: 'STOPPED' | 'STARTING' | 'RUNNING' | 'STOPPING' | 'ERROR';
  recent_session_id: string | null;
  active_run_count: number;
  waiting_human_count: number;
  error_code: string | null;
};

export type RuntimeCapacity = {
  started_agents: number;
  max_started_agents: number;
  active_runs: number;
  max_active_runs: number;
};

export type RuntimeOverview = {
  runtimes: AgentRuntime[];
  capacity: RuntimeCapacity;
  cursor: string;
};

export type SessionSummary = {
  session_id: string;
  agent_id?: string;
  title?: string | null;
  created_at: string;
  updated_at: string;
  status: string;
  agent_name: string;
  provider: string | null;
  model: string | null;
  message_count: number;
  preview: string;
  source?: string | null;
  schedule_task_id?: string | null;
  schedule_run_id?: string | null;
  schedule_task_name?: string | null;
};

export type RunRef = {
  agent_id: string;
  session_id: string;
  run_id: string;
  revision_id: string;
};

export type ActiveRun = {
  run_id: string;
  status: string;
  revision_id: string;
  started_at: string | null;
};

export type PendingInteraction = {
  interaction_id: string;
  run_id: string;
  kind: 'approval' | 'question';
  request: Record<string, unknown>;
};

export type SessionRuntime = {
  status: string;
  provider: string | null;
  model: string | null;
  thinking_value: string | null;
  active_run: ActiveRun | null;
  pending_interaction: PendingInteraction | null;
};

export type ReplayResponse = {
  session: { data?: Record<string, unknown> } | null;
  messages: MessageRecord[];
  records: Array<Record<string, unknown>>;
  pending_question?: Record<string, unknown> | null;
  latest_event_seq: number;
  runtime: SessionRuntime;
};

export type ProviderCapability = {
  provider: string;
  label: string;
  models: string[];
  model_capabilities?: Record<string, {
    thinking?: {
      allowed_values: string[];
      default_value: string;
    } | null;
  }>;
};

export type AgentCapabilities = {
  providers: ProviderCapability[];
  tools: Array<{
    name: string;
    description: string;
    requires_approval?: boolean;
    side_effect: string;
    assignable: boolean;
    reason?: string | null;
  }>;
  mcp_servers: Array<{
    name: string;
    status: 'available' | 'unavailable' | 'disabled';
    requires_approval: boolean;
    description?: string;
  }>;
};

export type SessionViewState = {
  messages: MessageRecord[];
  events: StreamEvent[];
  liveDelta: string;
  liveReasoningDelta: string;
  subagentLiveDeltas: Record<string, string>;
  subagentLiveReasoningDeltas: Record<string, string>;
};
