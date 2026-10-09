import type { MessageRecord, StreamEvent } from '../../types';

export type AgentSummary = {
  agent_id: string;
  revision_id: string;
  name: string;
  description?: string;
  source: 'builtin' | 'custom';
  visibility?: 'builtin' | 'shared' | 'private';
  publication_id?: string | null;
  archived: boolean;
  readonly?: boolean;
  kind?: 'agent' | 'subagent';
  launch_modes?: Array<'direct' | 'delegated'>;
  can_delegate?: boolean;
  validation_status: 'valid' | 'legacy_warning' | 'invalid' | 'needs_configuration';
  validation_issues?: Array<{ code: string; field?: string | null; message: string; suggestion?: string }>;
  default_provider?: string | null;
  default_model?: string | null;
  default_thinking_value?: string | null;
};

export type AgentDetail = AgentSummary & {
  tool_ids?: string[];
  skill_ids?: string[] | null;
  working_directory?: string | null;
  connection_ids?: string[] | null;
  max_iterations?: number;
  memory_enabled?: boolean;
  hook_ids?: string[] | null;
  hook_parameters?: Record<string, Record<string, string | number | boolean>>;
  delegate_agent_ids?: string[] | null;
  subagent_ids?: string[] | null;
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
  stop_reason?: string | null;
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

export type RecentRun = ActiveRun & {
  ref?: RunRef;
  ended_at: string | null;
  error_code: string | null;
  error_summary: string | null;
};

export type PendingInteraction = {
  interaction_id: string;
  run_id: string;
  kind: 'approval' | 'question';
  request: Record<string, unknown>;
};

export type SessionRuntime = {
  source?: 'schedule' | 'web';
  schedule_run_id?: string;
  schedule_task_name?: string;
  worker_exited?: boolean;
  can_send?: boolean;
  can_stop?: boolean;
  phase?: string;
  iteration?: number;
  max_iterations?: number;
  error_summary?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  status: string;
  stop_reason?: string | null;
  provider: string | null;
  model: string | null;
  thinking_value: string | null;
  active_run: ActiveRun | null;
  last_run?: RecentRun | null;
  pending_interaction: PendingInteraction | null;
};

export type ProviderConfig = {
  provider: string;
  label: string;
  models: string[];
  model_capabilities?: Record<string, {
    thinking?: { allowed_values: string[]; default_value: string } | null;
  }>;
};

export type ReplayResponse = {
  workbench_events?: StreamEvent[];
  session: { data?: Record<string, unknown> } | null;
  messages: MessageRecord[];
  latest_event_seq: number;
  runtime: SessionRuntime;
  submissions?: MessageSubmission[];
};

export type MessageSubmission = {
  mode: 'started' | 'appended';
  message_id: string;
  status: 'accepted' | 'pending' | 'included';
  sequence?: number;
  message?: MessageRecord;
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
  hooks?: Array<{ hook_id: string; name?: string; hook_type: string; plugin_type: string; available: boolean; parameters?: Record<string, { type: 'string' | 'integer' | 'boolean'; required: boolean; choices: string[] }> }>;
  subagents?: Array<{ agent_id: string; name: string; description: string }>;
  delegates?: Array<{ agent_id: string; name: string; description: string; launch_modes?: Array<'direct' | 'delegated'> }>;
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
  submissions?: MessageSubmission[];
  messages: MessageRecord[];
  events: StreamEvent[];
  liveDelta: string;
  liveReasoningDelta: string;
  subagentLiveDeltas: Record<string, string>;
  subagentLiveReasoningDeltas: Record<string, string>;
};
