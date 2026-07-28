import { FormEvent, RefObject, useEffect, useRef, useState } from 'react';
import { useAutoScroll } from './hooks/useAutoScroll';
import { useQuestionInteraction } from './hooks/useQuestionInteraction';
import { useSessionStream } from './hooks/useSessionStream';
import { ConfigSelect } from './components/ConfigSelect';
import { AttachmentPicker, AttachmentTray } from './components/Attachments';
import { ApprovalPanel, ScrollToBottomButton } from './components/SessionInteractions';
import { EventItem } from './components/EventItem';
import { MessageStream } from './components/MessageStream';
import { ReasoningBlock, TextBlock } from './components/MessageContent';
import type { ApprovalRequest, MessagePart, MessageRecord, PendingAttachment, QuestionAnswer, QuestionItem, QuestionRequest, SelectOption, StreamEvent, TokenUsage } from './types';
import {
  AlertTriangle,
  Brain,
  CalendarClock,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Pencil,
  FileText,
  HardDrive,
  History,
  ListTree,
  MessageSquareText,
  OctagonAlert,
  Play,
  Plus,
  Radio,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  Terminal,
  Trash2,
  Zap,
  X,
} from 'lucide-react';

type ProviderOption = {
  provider: string;
  label: string;
  models: string[];
  model_capabilities?: Record<string, ModelCapability>;
};

type ThinkingCapability = {
  kind: 'reasoning_effort' | 'extra_body_boolean';
  allowed_values: string[];
  default_value: string;
  extra_body_key?: string | null;
};

type ModelCapability = {
  thinking?: ThinkingCapability | null;
};

type ConfigResponse = {
  workspace_id: string;
  workspace_path: string;
  codepilot_home: string;
  activated_providers: ProviderOption[];
  agents: string[];
  skills: SkillOption[];
  sse: {
    heartbeat_seconds: number;
    replay_on_connect: boolean;
  };
};

type SkillOption = {
  name: string;
  description: string;
};

type WorkspaceFileOption = {
  path: string;
};

type WorkspaceFilesResponse = {
  files: WorkspaceFileOption[];
};

type ComposerAutocomplete = {
  kind: 'skill' | 'file';
  trigger: '$' | '@';
  start: number;
  end: number;
  query: string;
  activeIndex: number;
};

type ComposerOption = {
  value: string;
  label: string;
  description?: string;
};

type StatusResponse = {
  workspace_id: string;
  workspace_path: string;
  session_id: string | null;
  status: string;
  agent_name: string;
  provider: string | null;
  model: string | null;
  thinking_enabled: boolean;
  thinking_value?: string | null;
  pending_human_type?: string | null;
  pending_human_request?: Record<string, unknown> | null;
};

type ReplayResponse = {
  session: Record<string, unknown> | null;
  messages: MessageRecord[];
  records: Array<Record<string, unknown>>;
  pending_question?: QuestionRequest | null;
};

type SessionSummary = {
  session_id: string;
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

type SessionsResponse = {
  sessions: SessionSummary[];
};

type LoadSessionResponse = ReplayResponse & {
  ok: boolean;
  session: Record<string, unknown> | null;
};

type ScheduleTrigger = {
  kind: 'once' | 'interval' | 'daily' | 'weekly';
  run_at?: string | null;
  interval_seconds?: number | null;
  time_of_day?: string | null;
  day_of_week?: number | null;
  timezone?: string | null;
};

type ScheduleTask = {
  id: string;
  name: string;
  prompt: string;
  agent_name: string;
  provider: string;
  model: string;
  trigger: ScheduleTrigger;
  working_dir: string;
  enabled: boolean;
  next_run_at?: string | null;
  last_run_at?: string | null;
};

type ScheduleRun = {
  id: string;
  task_id: string;
  task_name: string;
  session_id?: string | null;
  status: string;
  scheduled_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  pid?: number | null;
  working_dir: string;
  error?: string | null;
  summary?: string | null;
};

type ScheduleRunsResponse = {
  active: ScheduleRun[];
  recent: ScheduleRun[];
};

type SchedulesResponse = {
  schedules: ScheduleTask[];
};

type ScheduleFormState = {
  editing_id: string | null;
  name: string;
  prompt: string;
  agent_name: string;
  provider: string;
  model: string;
  working_dir: string;
  trigger_kind: 'once' | 'interval' | 'daily' | 'weekly';
  run_at: string;
  interval_seconds: string;
  time_of_day: string;
  day_of_week: string;
  timezone: string;
  enabled: boolean;
};

const WEEKDAY_OPTIONS: SelectOption[] = [
  { value: '1', label: '周一' },
  { value: '2', label: '周二' },
  { value: '3', label: '周三' },
  { value: '4', label: '周四' },
  { value: '5', label: '周五' },
  { value: '6', label: '周六' },
  { value: '7', label: '周日' },
];

const EVENT_LABELS: Record<string, string> = {
  session_started: '会话启动',
  session_title_updated: '标题更新',
  session_status_changed: '状态变化',
  session_finished: '会话结束',
  session_failed: '会话失败',
  user_message_created: '用户消息',
  assistant_message_started: '助手开始输出',
  assistant_message_completed: '助手输出完成',
  llm_delta: '流式增量',
  llm_reasoning_delta: '推理增量',
  tool_call_started: '工具调用开始',
  tool_call_finished: '工具调用完成',
  tool_call_failed: '工具调用失败',
  context_compacted: '上下文压缩',
  human_approval_required: '需要人工确认',
  human_approval_resolved: '人工确认已处理',
  human_question_required: '需要用户回答',
  human_question_resolved: '用户回答已处理',
  error: '错误',
};

const KEY_EVENT_TYPES = new Set([
  'session_started',
  'session_title_updated',
  'session_status_changed',
  'session_finished',
  'session_failed',
  'tool_call_started',
  'tool_call_finished',
  'tool_call_failed',
  'context_compacted',
  'human_approval_required',
  'human_approval_resolved',
  'human_question_required',
  'human_question_resolved',
  'error',
]);

const PENDING_TOOL_MESSAGE_PREFIX = 'pending-tools:';

function formatDateTimeLocal(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function createDefaultScheduleForm(workspacePath = ''): ScheduleFormState {
  const runAt = formatDateTimeLocal(new Date(Date.now() + 5 * 60 * 1000));
  return {
    editing_id: null,
    name: '',
    prompt: '',
    agent_name: 'build',
    provider: '',
    model: '',
    working_dir: workspacePath,
    trigger_kind: 'once',
    run_at: runAt,
    interval_seconds: '3600',
    time_of_day: '09:00',
    day_of_week: '5',
    timezone: '',
    enabled: true,
  };
}

function scheduleToForm(task: ScheduleTask): ScheduleFormState {
  return {
    editing_id: task.id,
    name: task.name,
    prompt: task.prompt,
    agent_name: task.agent_name,
    provider: task.provider,
    model: task.model,
    working_dir: task.working_dir,
    trigger_kind: task.trigger.kind,
    run_at: task.trigger.run_at ? formatDateTimeLocal(new Date(task.trigger.run_at)) : createDefaultScheduleForm(task.working_dir).run_at,
    interval_seconds: String(task.trigger.interval_seconds || 3600),
    time_of_day: task.trigger.time_of_day || '09:00',
    day_of_week: String(task.trigger.day_of_week || 5),
    timezone: task.trigger.timezone || '',
    enabled: task.enabled,
  };
}

function buildScheduleTrigger(form: ScheduleFormState): ScheduleTrigger {
  if (form.trigger_kind === 'once') {
    return { kind: 'once', run_at: new Date(form.run_at).toISOString() };
  }
  if (form.trigger_kind === 'interval') {
    return { kind: 'interval', interval_seconds: Number(form.interval_seconds) };
  }
  if (form.trigger_kind === 'weekly') {
    return { kind: 'weekly', day_of_week: Number(form.day_of_week), time_of_day: form.time_of_day, timezone: form.timezone || null };
  }
  return { kind: 'daily', time_of_day: form.time_of_day, timezone: form.timezone || null };
}

type TokenSummary = {
  input: number;
  output: number;
  reasoning: number;
  cacheRead: number;
  cacheWrite: number;
};

type ToolState = Record<string, unknown> & {
  status?: string;
  input?: Record<string, unknown>;
  output?: Record<string, unknown> | null;
  error?: Record<string, unknown> | null;
};

type TodoItem = {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  priority: 'low' | 'medium' | 'high';
};

type DiffLineKind = 'context' | 'add' | 'delete' | 'hunk' | 'meta';

type DiffLine = {
  kind: DiffLineKind;
  content: string;
  oldLine: number | null;
  newLine: number | null;
};

type DiffFile = {
  oldPath: string;
  newPath: string;
  lines: DiffLine[];
  additions: number;
  deletions: number;
};

type EventTone = 'neutral' | 'running' | 'ok' | 'warn' | 'danger';

type EventViewModel = {
  title: string;
  summary: string;
  tone: EventTone;
  icon: React.ReactNode;
  chips: string[];
  time: string;
};

type EventStats = {
  tools: number;
  failed: number;
  blockers: number;
  compacted: number;
  latest: StreamEvent | null;
};

type MobileTab = 'messages' | 'events' | 'history' | 'action';

const RADAR_PAGE_SIZE = 14;

function buildMessageStreamSignal(
  messages: MessageRecord[],
  liveDelta: string,
  liveReasoningDelta: string,
  subagentLiveDeltas: Record<string, string>,
  subagentLiveReasoningDeltas: Record<string, string>,
) {
  const subagentDeltaLength = Object.values(subagentLiveDeltas).reduce((total, text) => total + text.length, 0);
  const subagentReasoningLength = Object.values(subagentLiveReasoningDeltas).reduce((total, text) => total + text.length, 0);
  return [
    messages.length,
    messages[messages.length - 1]?.info?.id || '',
    liveDelta.length,
    liveReasoningDelta.length,
    subagentDeltaLength,
    subagentReasoningLength,
  ].join(':');
}

type MobileConsoleProps = {
  config: ConfigResponse | null;
  status: StatusResponse | null;
  currentSessionId: string | null;
  provider: string;
  model: string;
  agentName: string;
  task: string;
  attachments: PendingAttachment[];
  formError: string;
  approvalComment: string;
  approvalRequest: ApprovalRequest | null;
  questionRequest: QuestionRequest | null;
  questionAnswers: Record<string, QuestionAnswer>;
  activeQuestionIndex: number;
  activeQuestionOptionIndex: number;
  questionError: string;
  messages: MessageRecord[];
  events: StreamEvent[];
  sessionHistory: SessionSummary[];
  loadingSessionId: string | null;
  liveDelta: string;
  subagentLiveDeltas: Record<string, string>;
  liveReasoningDelta: string;
  subagentLiveReasoningDeltas: Record<string, string>;
  thinkingValue: string;
  modelOptions: string[];
  thinkingOptions: SelectOption[];
  selectedThinking: ThinkingCapability | null;
  statusText: string;
  activeQuestion: QuestionItem | null;
  activeAnswer: QuestionAnswer | null;
  tokenSummary: TokenSummary;
  totalTokens: number;
  eventStats: EventStats;
  latestEventView: EventViewModel | null;
  taskInputRef: React.RefObject<HTMLTextAreaElement>;
  composerAutocomplete: ComposerAutocomplete | null;
  composerOptions: ComposerOption[];
  isFileSuggestionLoading: boolean;
  questionOptionsRef: React.RefObject<HTMLDivElement>;
  questionNoteRef: React.RefObject<HTMLTextAreaElement>;
  onAgentChange: (nextValue: string) => void;
  onProviderChange: (nextValue: string) => void;
  onModelChange: (nextValue: string) => void;
  onThinkingChange: (nextValue: string) => void;
  onTaskChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => void;
  onComposerOptionHover: (index: number) => void;
  onComposerOptionPick: (option: ComposerOption) => void;
  onAttachmentFiles: (files: FileList | null) => void;
  onRemoveAttachment: (id: string) => void;
  onTaskKeyDown: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  onStart: (event: FormEvent<HTMLFormElement>) => void;
  onStop: () => void;
  onNewTask: () => void;
  onLoadSession: (sessionId: string) => void;
  onApproval: (approved: boolean) => void;
  onApprovalCommentChange: (nextValue: string) => void;
  onQuestionDecline: () => void;
  onMoveActiveQuestion: (step: number) => void;
  onConfirmActiveQuestion: () => void;
  onQuestionOptionsKeyDown: (event: React.KeyboardEvent<HTMLDivElement>, question: QuestionItem) => void;
  onQuestionChoiceChange: (question: QuestionItem, value: string, checked: boolean) => void;
  onQuestionNoteChange: (questionId: string, note: string) => void;
  onQuestionNoteKeyDown: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  onSetActiveQuestionIndex: (index: number) => void;
  onSetActiveQuestionOptionIndex: (index: number) => void;
  onClearQuestionError: () => void;
  onFocusQuestionOptions: () => void;
};

function App() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [agentName, setAgentName] = useState('build');
  const [task, setTask] = useState('');
  const [composerAutocomplete, setComposerAutocomplete] = useState<ComposerAutocomplete | null>(null);
  const [fileSuggestions, setFileSuggestions] = useState<WorkspaceFileOption[]>([]);
  const [isFileSuggestionLoading, setIsFileSuggestionLoading] = useState(false);
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [formError, setFormError] = useState('');
  const [approvalComment, setApprovalComment] = useState('');
  const [approvalRequest, setApprovalRequest] = useState<ApprovalRequest | null>(null);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [sessionHistory, setSessionHistory] = useState<SessionSummary[]>([]);
  const scheduleManagement = useScheduleManagement(config, handleLoadSession);
  const [isHistoryOpen, setIsHistoryOpen] = useState(false);
  const [loadingSessionId, setLoadingSessionId] = useState<string | null>(null);
  const [liveDelta, setLiveDelta] = useState('');
  const [subagentLiveDeltas, setSubagentLiveDeltas] = useState<Record<string, string>>({});
  const [liveReasoningDelta, setLiveReasoningDelta] = useState('');
  const [subagentLiveReasoningDeltas, setSubagentLiveReasoningDeltas] = useState<Record<string, string>>({});
  const [thinkingValue, setThinkingValue] = useState('');
  const [lastSeq, setLastSeq] = useState(0);
  const lastSeqRef = useRef(0);
  const currentSessionIdRef = useRef<string | null>(null);
  const questionPanelRef = useRef<HTMLElement | null>(null);
  const taskInputRef = useRef<HTMLTextAreaElement | null>(null);
  const {
    questionRequest,
    questionAnswers,
    activeQuestionIndex,
    activeQuestionOptionIndex,
    questionError,
    questionOptionsRef,
    questionNoteRef,
    setActiveQuestionIndex,
    setActiveQuestionOptionIndex,
    setQuestionError,
    restorePendingQuestion,
    clearQuestion,
    updateQuestionChoice,
    updateQuestionNote,
    focusQuestionOptions,
    moveActiveQuestion,
    confirmActiveQuestion,
    handleQuestionOptionsKeyDown,
    handleQuestionNoteKeyDown,
    handleQuestionDecline,
  } = useQuestionInteraction({
    onSubmit: async (request, answers) => {
      if (!request.question_id) {
        setFormError('回答信息仍在恢复，请稍候重试。');
        return false;
      }
      setFormError('');
      try {
        await postJson('/api/session/input', { type: 'question_reply', question_id: request.question_id, answers });
        await refreshStatus();
        return true;
      } catch (error) {
        setFormError(error instanceof Error ? error.message : '提交回答失败');
        return false;
      }
    },
    onDecline: async (request) => {
      setFormError('');
      try {
        await postJson('/api/session/input', { type: 'question_decline', question_id: request.question_id });
        await refreshStatus();
        return true;
      } catch (error) {
        setFormError(error instanceof Error ? error.message : '退出回答失败');
        return false;
      }
    },
  });
  const { connectStream, closeStream } = useSessionStream({
    eventTypes: Object.keys(EVENT_LABELS),
    lastSeqRef,
    onEvent: onStreamEvent,
  });
  const messageStreamSignal = buildMessageStreamSignal(messages, liveDelta, liveReasoningDelta, subagentLiveDeltas, subagentLiveReasoningDeltas);
  const desktopScroll = useAutoScroll(messageStreamSignal);

  useEffect(() => {
    void bootstrap();
    return closeStream;
  }, []);

  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  useEffect(() => {
    if (!questionRequest) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isTextEntryTarget(event.target)) {
        return;
      }
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        moveActiveQuestion(-1);
        return;
      }
      if (event.key === 'ArrowRight') {
        event.preventDefault();
        moveActiveQuestion(1);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        void handleQuestionDecline();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [questionRequest, activeQuestionIndex]);

  useEffect(() => {
    const question = questionRequest?.questions[activeQuestionIndex];
    if (!question) {
      setActiveQuestionOptionIndex(0);
      return;
    }
    const answer = questionAnswers[question.id] || { values: [], note: '' };
    const selectedIndex = question.options.findIndex((option) => answer.values.includes(option.value));
    setActiveQuestionOptionIndex(selectedIndex >= 0 ? selectedIndex : 0);
    focusQuestionOptions();
  }, [questionRequest, activeQuestionIndex]);

  useEffect(() => {
    const capability = getThinkingCapability(findProviderOption(provider), model);
    if (!capability) {
      if (thinkingValue) {
        setThinkingValue('');
      }
      return;
    }
    if (!capability.allowed_values.includes(thinkingValue)) {
      setThinkingValue(capability.default_value);
    }
  }, [config, provider, model]);

  useEffect(() => {
    if (composerAutocomplete?.kind !== 'file') {
      setFileSuggestions([]);
      setIsFileSuggestionLoading(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setIsFileSuggestionLoading(true);
      try {
        const params = new URLSearchParams({ q: composerAutocomplete.query, limit: '40' });
        const response = await fetchJson<WorkspaceFilesResponse>(`/api/workspace/files?${params}`, { signal: controller.signal });
        setFileSuggestions(response.files);
      } catch (error) {
        if (!controller.signal.aborted) {
          setFileSuggestions([]);
        }
      } finally {
        if (!controller.signal.aborted) {
          setIsFileSuggestionLoading(false);
        }
      }
    }, 120);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [composerAutocomplete?.kind, composerAutocomplete?.query]);

  async function bootstrap() {
    try {
      const [configRes, statusRes, replayRes, sessionsRes] = await Promise.all([
        fetchJson<ConfigResponse>('/api/config'),
        fetchJson<StatusResponse>('/api/session/status'),
        fetchJson<ReplayResponse>('/api/session/replay'),
        fetchJson<SessionsResponse>('/api/sessions'),
      ]);
      setConfig(configRes);
      setStatus(statusRes);
      setCurrentSessionId(statusRes.session_id);
      applyProviderAndModelState(statusRes);
      setThinkingValue(statusRes.thinking_value || '');
      setAgentName(statusRes.agent_name || 'build');
      const replayMessages = statusRes.session_id && getReplaySessionId(replayRes) === statusRes.session_id ? replayRes.messages : [];
      setMessages(replayMessages);
      restorePendingQuestion(
        statusRes.status === 'WAITING_HUMAN' && statusRes.pending_human_type === 'question'
          ? statusRes.pending_human_request
          : null,
      );
      setSubagentLiveDeltas({});
      setSubagentLiveReasoningDeltas({});
      setSessionHistory(sessionsRes.sessions);
      connectStream(0);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '初始化失败');
    }
  }

  function onStreamEvent(event: StreamEvent) {
    const activeSessionId = currentSessionIdRef.current;
    const eventSessionId = event.session_id;
    if (activeSessionId && eventSessionId && eventSessionId !== activeSessionId) {
      return;
    }
    if (!activeSessionId) {
      if (event.event_type !== 'session_started' || !eventSessionId) {
        return;
      }
      currentSessionIdRef.current = eventSessionId;
      setCurrentSessionId(eventSessionId);
    }
    setLastSeq((prev) => {
      const next = Math.max(prev, event.seq);
      lastSeqRef.current = next;
      return next;
    });
    // 事件面板只展示关键节点；增量 token 等高频过程事件仍用于实时输出，但不进入列表。
    if (KEY_EVENT_TYPES.has(event.event_type)) {
      setEvents((prev) => [...prev, event].slice(-200));
    }
    if (event.event_type === 'llm_delta') {
      const text = String(event.data.text || '');
      if (event.data.agent_kind === 'subagent') {
        const key = String(event.data.context_id || event.data.parent_call_id || 'subagent');
        setSubagentLiveDeltas((prev) => ({ ...prev, [key]: (prev[key] || '') + text }));
      } else {
        setLiveDelta((prev) => prev + text);
      }
    }
    if (event.event_type === 'llm_reasoning_delta') {
      const text = String(event.data.text || '');
      if (event.data.agent_kind === 'subagent') {
        const key = String(event.data.context_id || event.data.parent_call_id || 'subagent');
        setSubagentLiveReasoningDeltas((prev) => ({ ...prev, [key]: (prev[key] || '') + text }));
      } else {
        setLiveReasoningDelta((prev) => prev + text);
      }
    }
    if (event.event_type === 'tool_call_started') {
      setMessages((prev) => upsertRunningToolMessage(prev, event));
    }
    if (event.event_type === 'assistant_message_completed') {
      if (event.data.message && typeof event.data.message === 'object') {
        const message = event.data.message as MessageRecord;
        if (message.info?.agent_kind === 'subagent') {
          const key = String(message.info.context_id || message.info.parent_call_id || 'subagent');
          setSubagentLiveDeltas((prev) => {
            const next = { ...prev };
            delete next[key];
            return next;
          });
          setSubagentLiveReasoningDeltas((prev) => {
            const next = { ...prev };
            delete next[key];
            return next;
          });
        } else {
          setLiveDelta('');
          setLiveReasoningDelta('');
        }
        setMessages((prev) => upsertMessage(removePendingToolMessagesForAssistant(prev, message), message));
      }
    }
    if (event.event_type === 'user_message_created') {
      if (event.data.message && typeof event.data.message === 'object') {
        setMessages((prev) => upsertMessage(prev, event.data.message as MessageRecord));
      }
    }
    if (event.event_type === 'human_approval_required') {
      setApprovalRequest(event.data as ApprovalRequest);
    }
    if (event.event_type === 'human_approval_resolved') {
      setApprovalRequest(null);
      setApprovalComment('');
    }
    if (event.event_type === 'human_question_required') {
      restorePendingQuestion(event.data);
    }
    if (event.event_type === 'human_question_resolved') {
      clearQuestion();
    }
    if (
      event.event_type === 'session_started' ||
      event.event_type === 'session_title_updated' ||
      event.event_type === 'session_status_changed' ||
      event.event_type === 'session_finished' ||
      event.event_type === 'session_failed'
    ) {
      void refreshStatus();
      void refreshSessionHistory();
    }
  }

  async function refreshStatus() {
    const next = await fetchJson<StatusResponse>('/api/session/status');
    setStatus(next);
    setCurrentSessionId(next.session_id);
    applyProviderAndModelState(next);
    setThinkingValue(next.thinking_value || '');
    setAgentName(next.agent_name || 'build');
    restorePendingQuestion(
      next.status === 'WAITING_HUMAN' && next.pending_human_type === 'question'
        ? next.pending_human_request
        : null,
    );
  }

  async function refreshSessionHistory() {
    const next = await fetchJson<SessionsResponse>('/api/sessions');
    setSessionHistory(next.sessions);
  }


  function applyProviderAndModelState(statusRes: StatusResponse) {
    if (statusRes.provider && statusRes.model) {
      setProvider(statusRes.provider);
      setModel(statusRes.model);
      return;
    }
    setProvider('');
    setModel('');
  }

  function findProviderOption(nextProvider: string) {
    return config?.activated_providers.find((item) => item.provider === nextProvider) || null;
  }

  function handleProviderChange(nextProvider: string) {
    setProvider(nextProvider);
    const providerOption = findProviderOption(nextProvider);
    if (!providerOption) {
      setModel('');
      setThinkingValue('');
      return;
    }
    if (!providerOption.models.includes(model)) {
      setModel('');
      setThinkingValue('');
      return;
    }
    setThinkingValue(resolveNextThinkingValue(providerOption, model, thinkingValue));
  }

  function handleModelChange(nextModel: string) {
    setModel(nextModel);
    setThinkingValue(resolveNextThinkingValue(findProviderOption(provider), nextModel, thinkingValue));
  }

  function updateComposerAutocomplete(value: string, caret: number | null) {
    const next = detectComposerAutocomplete(value, caret ?? value.length);
    setComposerAutocomplete(next);
    if (next?.kind !== 'file') {
      setFileSuggestions([]);
    }
  }

  function handleTaskChange(event: React.ChangeEvent<HTMLTextAreaElement>) {
    setTask(event.target.value);
    updateComposerAutocomplete(event.target.value, event.target.selectionStart);
  }

  function currentComposerOptions(): ComposerOption[] {
    if (!composerAutocomplete) {
      return [];
    }
    if (composerAutocomplete.kind === 'skill') {
      return buildSkillComposerOptions(config?.skills || [], composerAutocomplete.query);
    }
    return fileSuggestions.map((file) => ({ value: file.path, label: file.path }));
  }

  function moveComposerOption(step: number) {
    setComposerAutocomplete((prev) => {
      if (!prev) {
        return prev;
      }
      const optionCount = currentComposerOptions().length;
      if (optionCount <= 0) {
        return prev;
      }
      return { ...prev, activeIndex: (prev.activeIndex + step + optionCount) % optionCount };
    });
  }

  function applyComposerOption(option: ComposerOption) {
    if (!composerAutocomplete) {
      return;
    }
    const replacement = `${composerAutocomplete.trigger}${option.value} `;
    const nextTask = `${task.slice(0, composerAutocomplete.start)}${replacement}${task.slice(composerAutocomplete.end)}`;
    const nextCaret = composerAutocomplete.start + replacement.length;
    setTask(nextTask);
    setComposerAutocomplete(null);
    window.requestAnimationFrame(() => {
      taskInputRef.current?.focus();
      taskInputRef.current?.setSelectionRange(nextCaret, nextCaret);
    });
  }

  async function submitTask() {
    setFormError('');
    if (!task.trim()) {
      return;
    }
    if (!provider) {
      setFormError('请先选择 provider');
      return;
    }
    if (!model) {
      setFormError('请先选择 model');
      return;
    }
    const isStartingNewSession = !currentSessionIdRef.current;
    try {
      const payload = {
        type: 'user_message',
        content: translateSkillShortcuts(task, config?.skills || []),
        agent_name: agentName,
        provider,
        model,
        attachments: attachments.map(({ filename, mime, data_base64 }) => ({ filename, mime, data_base64 })),
        metadata: thinkingValue ? { thinking_value: thinkingValue } : {},
        ...(currentSessionId ? { session_id: currentSessionId } : {}),
      };
      const response = await postJson<{ ok: boolean; session: StatusResponse | null }>('/api/session/input', payload);
      const nextSessionId = response.session?.session_id || null;
      currentSessionIdRef.current = nextSessionId;
      setCurrentSessionId(nextSessionId);
      if (isStartingNewSession && nextSessionId) {
        connectStream(0);
      }
      setTask('');
      clearAttachments();
      await refreshStatus();
      await refreshSessionHistory();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '提交任务失败');
    }
  }

  function handleStart(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitTask();
  }

  async function handleAttachmentFiles(files: FileList | null) {
    if (!files?.length) {
      return;
    }
    setFormError('');
    try {
      const nextAttachments = await Promise.all(Array.from(files).map(fileToPendingAttachment));
      setAttachments((prev) => {
        const merged = [...prev, ...nextAttachments];
        const kept = merged.slice(0, 4);
        merged.slice(4).forEach((item) => URL.revokeObjectURL(item.previewUrl));
        return kept;
      });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '读取附件失败');
    }
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => {
      const target = prev.find((item) => item.id === id);
      if (target) {
        URL.revokeObjectURL(target.previewUrl);
      }
      return prev.filter((item) => item.id !== id);
    });
  }

  function clearAttachments() {
    setAttachments((prev) => {
      prev.forEach((item) => URL.revokeObjectURL(item.previewUrl));
      return [];
    });
  }

  function handleTaskKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (composerAutocomplete) {
      const options = currentComposerOptions();
      if (event.key === 'Escape') {
        event.preventDefault();
        setComposerAutocomplete(null);
        return;
      }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveComposerOption(event.key === 'ArrowDown' ? 1 : -1);
        return;
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault();
        if (options.length > 0) {
          applyComposerOption(options[Math.min(composerAutocomplete.activeIndex, options.length - 1)]);
        }
        return;
      }
    }
    // 输入法组合态下的 Enter 应交给输入法确认候选，不能触发任务发送。
    if (event.nativeEvent.isComposing || event.keyCode === 229) {
      return;
    }
    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }
    event.preventDefault();
    void submitTask();
  }

  function handleNewTask() {
    closeStream();
    currentSessionIdRef.current = null;
    setCurrentSessionId(null);
    setStatus((prev) => (prev ? { ...prev, session_id: null, status: 'IDLE' } : prev));
    setMessages([]);
    setEvents([]);
    setLiveDelta('');
    setSubagentLiveDeltas({});
    setLiveReasoningDelta('');
    setSubagentLiveReasoningDeltas({});
    setThinkingValue('');
    setApprovalRequest(null);
    setApprovalComment('');
    clearQuestion();
    setFormError('');
    clearAttachments();
    setLastSeq(0);
    lastSeqRef.current = 0;
  }

  async function handleLoadSession(sessionId: string) {
    setFormError('');
    setLoadingSessionId(sessionId);
    try {
      const replay = await postJson<LoadSessionResponse>('/api/session/load', { session_id: sessionId });
      const nextStatus = await fetchJson<StatusResponse>('/api/session/status');
      setStatus(nextStatus);
      setCurrentSessionId(nextStatus.session_id);
      applyProviderAndModelState(nextStatus);
      setThinkingValue(nextStatus.thinking_value || '');
      setAgentName(nextStatus.agent_name || 'build');
      setMessages(replay.messages);
      setEvents([]);
      setLiveDelta('');
      setSubagentLiveDeltas({});
      setLiveReasoningDelta('');
      setSubagentLiveReasoningDeltas({});
      setApprovalRequest(null);
      setApprovalComment('');
      // 历史会话会被后端取消运行态，不能把旧的待回答表单当成可提交请求恢复。
      restorePendingQuestion(null);
      setLastSeq(0);
      lastSeqRef.current = 0;
      connectStream(0);
      await refreshSessionHistory();
      setIsHistoryOpen(false);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '加载会话失败');
    } finally {
      setLoadingSessionId(null);
    }
  }

  async function handleStop() {
    setFormError('');
    try {
      await postJson('/api/session/input', { type: 'stop' });
      await refreshStatus();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '停止任务失败');
    }
  }

  async function handleApproval(approved: boolean) {
    if (!approvalRequest) {
      return;
    }
    setFormError('');
    try {
      await postJson('/api/session/input', {
        type: 'human_reply',
        approval_id: approvalRequest.approval_id,
        approved,
        comment: approvalComment,
      });
      await refreshStatus();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '处理审批失败');
    }
  }

  const selectedProvider = findProviderOption(provider);
  const modelOptions = selectedProvider?.models || [];
  const selectedThinking = getThinkingCapability(selectedProvider, model);
  const thinkingOptions = selectedThinking
    ? selectedThinking.allowed_values.map((item) => ({ value: item, label: formatThinkingValue(item) }))
    : [{ value: '', label: '不可用' }];
  const statusText = status?.status || 'IDLE';
  const activeQuestion = questionRequest?.questions[activeQuestionIndex] || null;
  const activeAnswer = activeQuestion ? questionAnswers[activeQuestion.id] || { values: [], note: '' } : null;
  const tokenSummary = summarizeTokenUsage(messages);
  const totalTokens = tokenSummary.input + tokenSummary.output + tokenSummary.reasoning;
  const eventStats = summarizeEventStats(events);
  const latestEventView = eventStats.latest ? buildEventViewModel(eventStats.latest) : null;
  const composerOptions = currentComposerOptions();
  const isMobileEntry = window.location.pathname === '/mobile';

  if (isMobileEntry) {
    return (
      <MobileConsole
        config={config}
        status={status}
        currentSessionId={currentSessionId}
        provider={provider}
        model={model}
        agentName={agentName}
        task={task}
        attachments={attachments}
        formError={formError}
        approvalComment={approvalComment}
        approvalRequest={approvalRequest}
        questionRequest={questionRequest}
        questionAnswers={questionAnswers}
        activeQuestionIndex={activeQuestionIndex}
        activeQuestionOptionIndex={activeQuestionOptionIndex}
        questionError={questionError}
        messages={messages}
        events={events}
        sessionHistory={sessionHistory}
        loadingSessionId={loadingSessionId}
        liveDelta={liveDelta}
        subagentLiveDeltas={subagentLiveDeltas}
        liveReasoningDelta={liveReasoningDelta}
        subagentLiveReasoningDeltas={subagentLiveReasoningDeltas}
        thinkingValue={thinkingValue}
        modelOptions={modelOptions}
        thinkingOptions={thinkingOptions}
        selectedThinking={selectedThinking}
        statusText={statusText}
        activeQuestion={activeQuestion}
        activeAnswer={activeAnswer}
        tokenSummary={tokenSummary}
        totalTokens={totalTokens}
        eventStats={eventStats}
        latestEventView={latestEventView}
        taskInputRef={taskInputRef}
        composerAutocomplete={composerAutocomplete}
        composerOptions={composerOptions}
        isFileSuggestionLoading={isFileSuggestionLoading}
        questionOptionsRef={questionOptionsRef}
        questionNoteRef={questionNoteRef}
        onAgentChange={setAgentName}
        onProviderChange={handleProviderChange}
        onModelChange={handleModelChange}
        onThinkingChange={setThinkingValue}
        onTaskChange={handleTaskChange}
        onComposerOptionHover={(index) => setComposerAutocomplete((prev) => (prev ? { ...prev, activeIndex: index } : prev))}
        onComposerOptionPick={applyComposerOption}
        onAttachmentFiles={(files) => void handleAttachmentFiles(files)}
        onRemoveAttachment={removeAttachment}
        onTaskKeyDown={handleTaskKeyDown}
        onStart={handleStart}
        onStop={handleStop}
        onNewTask={handleNewTask}
        onLoadSession={handleLoadSession}
        onApproval={(approved) => void handleApproval(approved)}
        onApprovalCommentChange={setApprovalComment}
        onQuestionDecline={() => void handleQuestionDecline()}
        onMoveActiveQuestion={moveActiveQuestion}
        onConfirmActiveQuestion={() => void confirmActiveQuestion()}
        onQuestionOptionsKeyDown={handleQuestionOptionsKeyDown}
        onQuestionChoiceChange={updateQuestionChoice}
        onQuestionNoteChange={updateQuestionNote}
        onQuestionNoteKeyDown={handleQuestionNoteKeyDown}
        onSetActiveQuestionIndex={setActiveQuestionIndex}
        onSetActiveQuestionOptionIndex={setActiveQuestionOptionIndex}
        onClearQuestionError={() => setQuestionError('')}
        onFocusQuestionOptions={focusQuestionOptions}
      />
    );
  }

  return (
    <div className="workspace-shell">
      <aside className="rail rail-left">
        <section className="brand-block">
          <div className="brand-mark" aria-hidden="true">
            <Terminal size={18} />
          </div>
          <div>
            <h1>CodePilot</h1>
            <p>agent terminal workspace</p>
          </div>
        </section>

        <section className="panel compact-panel session-panel">
          <PanelTitle
            icon={<Radio size={16} />}
            title="会话"
            action={
              <>
                <button
                  type="button"
                  className="button secondary icon-button session-history-button"
                  onClick={() => setIsHistoryOpen(true)}
                  title="历史记录"
                  aria-label="打开历史记录"
                >
                  <History size={14} />
                  <span>{sessionHistory.length}</span>
                </button>
                <button
                  type="button"
                  className="button secondary session-new-button"
                  onClick={handleNewTask}
                  title="新会话"
                  aria-label="新会话"
                >
                  <Plus size={14} />
                </button>
              </>
            }
          />
          <div className="session-status-card">
            <div className={`status-pill ${getStatusClass(statusText)}`}>
              <CircleDot size={12} />
              <span>{statusText}</span>
            </div>
            <div className="session-id-block">
              <span>session</span>
              <code title={status?.session_id || '-'}>{status?.session_id || '-'}</code>
            </div>
          </div>
          <RuntimeSignal statusText={statusText} latestEvent={latestEventView} />
          <div className="session-metrics runtime-metrics" aria-label="运行统计">
            <Metric icon={<MessageSquareText size={14} />} label="消息" value={String(messages.length)} />
            <Metric icon={<ListTree size={14} />} label="事件" value={String(events.length)} />
            <Metric icon={<Play size={14} />} label="工具" value={String(eventStats.tools)} />
            <Metric icon={<AlertTriangle size={14} />} label="失败" value={String(eventStats.failed)} />
            <Metric icon={<ShieldCheck size={14} />} label="阻塞" value={String(eventStats.blockers)} />
            <Metric icon={<FileText size={14} />} label="压缩" value={String(eventStats.compacted)} />
          </div>
          <TokenMeter summary={tokenSummary} total={totalTokens} />
        </section>
      </aside>

      <main className="terminal-stage">
        <header className="stage-header">
          <div>
            <h2>消息流</h2>
          </div>
        </header>

        {formError ? (
          <section className="error-strip" role="alert">
            <OctagonAlert size={16} />
            <span>{formError}</span>
          </section>
        ) : null}

        <div className="message-scroll-shell">
          <section className="message-viewport" aria-label="会话消息" ref={desktopScroll.anchorRef}>
            <MessageStream
              messages={messages}
              liveDelta={liveDelta}
              liveReasoningDelta={liveReasoningDelta}
              subagentLiveDeltas={subagentLiveDeltas}
              subagentLiveReasoningDeltas={subagentLiveReasoningDeltas}
              renderParts={renderMessageParts}
              renderStepFinish={(part, key) => <StepFinishView key={key} part={part} />}
              formatTime={formatTime}
              formatTokenUsage={formatTokenUsage}
            />
          </section>
          {!desktopScroll.isAtBottom ? (
            <ScrollToBottomButton className="message-scroll-button" onClick={desktopScroll.scrollToBottom} />
          ) : null}
        </div>

        <form className={`composer ${approvalRequest || questionRequest ? 'has-approval' : ''}`} onSubmit={handleStart}>
          <div className="composer-config">
            <label className="field compact-field">
              <span>Agent</span>
              <ConfigSelect
                value={agentName}
                options={(config?.agents || ['build', 'plan']).map((agent) => ({ value: agent, label: agent }))}
                onChange={setAgentName}
              />
            </label>
            <label className="field compact-field">
              <span>Provider</span>
              <ConfigSelect
                value={provider}
                options={[
                  { value: '', label: '选择 provider' },
                  ...(config?.activated_providers || []).map((item) => ({ value: item.provider, label: item.label })),
                ]}
                onChange={handleProviderChange}
              />
            </label>
            <label className="field compact-field model-field">
              <span>Model</span>
              <ConfigSelect
                value={model}
                options={[{ value: '', label: '选择 model' }, ...modelOptions.map((item) => ({ value: item, label: item }))]}
                onChange={handleModelChange}
                disabled={!provider}
              />
            </label>
            <label className="field compact-field thinking-field">
              <span>思考</span>
              <div className="thinking-select">
                <Brain size={14} aria-hidden="true" />
                <ConfigSelect
                  value={selectedThinking ? thinkingValue || selectedThinking.default_value : ''}
                  options={thinkingOptions}
                  onChange={setThinkingValue}
                  disabled={!selectedThinking || selectedThinking.allowed_values.length <= 1}
                />
              </div>
            </label>
          </div>

          {approvalRequest ? (
            <section className="composer-approval" aria-label="人工审批">
              <div className="approval-heading">
                <ShieldCheck size={16} />
                <strong>人工审批</strong>
                <code>{approvalRequest.approval_id}</code>
              </div>
              <p className="approval-reason">{approvalRequest.reason}</p>
              <pre className="code-block">{JSON.stringify(approvalRequest.action || {}, null, 2)}</pre>
              <textarea
                rows={3}
                placeholder="审批备注"
                value={approvalComment}
                onChange={(e) => setApprovalComment(e.target.value)}
              />
            </section>
          ) : questionRequest ? (
            <section className="composer-question" aria-label="用户回答" ref={questionPanelRef}>
              <div className="approval-heading">
                <MessageSquareText size={16} />
                <strong>需要回答</strong>
                <code>{questionRequest.question_id}</code>
              </div>
              {activeQuestion && activeAnswer ? (
                <div className="question-flow">
                  <div className="question-progress" aria-label="问题进度">
                    {questionRequest.questions.map((question, index) => {
                      const answer = questionAnswers[question.id] || { values: [], note: '' };
                      const isActive = index === activeQuestionIndex;
                      const isDone = answer.values.length > 0;
                      return (
                        <button
                          type="button"
                          className={`question-step ${isActive ? 'is-active' : ''} ${isDone ? 'is-done' : ''}`}
                          key={question.id}
                          onClick={() => {
                            setActiveQuestionIndex(index);
                            setActiveQuestionOptionIndex(0);
                            setQuestionError('');
                            focusQuestionOptions();
                          }}
                          aria-current={isActive ? 'step' : undefined}
                        >
                          {index + 1}
                        </button>
                      );
                    })}
                  </div>

                  <fieldset className="question-fieldset">
                    <legend>
                      <span>
                        第 {activeQuestionIndex + 1} / {questionRequest.questions.length} 题
                      </span>
                      {activeQuestion.question}
                    </legend>
                    <div
                      className="question-options"
                      ref={questionOptionsRef}
                      role={activeQuestion.multiple ? 'group' : 'radiogroup'}
                      aria-label={activeQuestion.question}
                      tabIndex={0}
                      onKeyDown={(event) => handleQuestionOptionsKeyDown(event, activeQuestion)}
                    >
                      {activeQuestion.options.map((option, optionIndex) => {
                        const inputType = activeQuestion.multiple ? 'checkbox' : 'radio';
                        const checked = activeAnswer.values.includes(option.value);
                        const isKeyboardActive = optionIndex === activeQuestionOptionIndex;
                        return (
                          <label className={`question-option ${isKeyboardActive ? 'is-keyboard-active' : ''}`} key={option.value}>
                            <input
                              type={inputType}
                              name={activeQuestion.id}
                              checked={checked}
                              tabIndex={-1}
                              onChange={(e) => {
                                setActiveQuestionOptionIndex(optionIndex);
                                updateQuestionChoice(activeQuestion, option.value, e.target.checked);
                              }}
                            />
                            <span>{option.label}</span>
                          </label>
                        );
                      })}
                    </div>
                    <textarea
                      ref={questionNoteRef}
                      rows={2}
                      placeholder="备注"
                      value={activeAnswer.note}
                      onChange={(e) => updateQuestionNote(activeQuestion.id, e.target.value)}
                      onKeyDown={handleQuestionNoteKeyDown}
                    />
                    {questionError ? <p className="question-error">{questionError}</p> : null}
                  </fieldset>
                </div>
              ) : null}
            </section>
          ) : (
            <>
              <div className="composer-input-shell">
                <textarea
                  ref={taskInputRef}
                  rows={4}
                  placeholder="输入任务。输入 $ 选择 skill，输入 @ 引用文件。"
                  value={task}
                  onChange={handleTaskChange}
                  onKeyDown={handleTaskKeyDown}
                />
                <ComposerAutocompletePanel
                  state={composerAutocomplete}
                  options={composerOptions}
                  loading={isFileSuggestionLoading}
                  onHover={(index) => setComposerAutocomplete((prev) => (prev ? { ...prev, activeIndex: index } : prev))}
                  onPick={applyComposerOption}
                />
              </div>
              <AttachmentTray attachments={attachments} onRemove={removeAttachment} />
            </>
          )}

          <div className="composer-actions">
            {approvalRequest ? (
              <>
                <button type="button" className="button primary" onClick={() => handleApproval(true)}>
                  <Check size={15} />
                  同意
                </button>
                <button type="button" className="button danger" onClick={() => handleApproval(false)}>
                  <X size={15} />
                  拒绝
                </button>
              </>
            ) : questionRequest ? (
              <>
                <button type="button" className="button secondary" onClick={handleQuestionDecline}>
                  <X size={15} />
                  退出
                </button>
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => moveActiveQuestion(-1)}
                  disabled={activeQuestionIndex === 0}
                >
                  <ChevronLeft size={15} />
                  上一题
                </button>
                <button type="button" className="button primary" onClick={() => void confirmActiveQuestion()}>
                  {activeQuestionIndex === questionRequest.questions.length - 1 ? <Check size={15} /> : <ChevronRight size={15} />}
                  {activeQuestionIndex === questionRequest.questions.length - 1 ? '提交' : '下一题'}
                </button>
              </>
            ) : (
              <>
                <AttachmentPicker onFiles={(files) => void handleAttachmentFiles(files)} />
                <button type="button" className="button secondary" onClick={handleStop}>
                  <Square size={14} />
                  停止
                </button>
                <button type="submit" className="button primary">
                  <Send size={15} />
                  发送
                </button>
              </>
            )}
          </div>

          <div className="workspace-strip" aria-label="工作区">
            <span>
              <HardDrive size={13} />
              {config?.workspace_id || 'loading'}
            </span>
            <code>{config?.workspace_path || '-'}</code>
            <code>{config?.codepilot_home || '-'}</code>
          </div>
        </form>
      </main>

      <aside className="rail rail-right">
        <SchedulePanel
          schedules={scheduleManagement.schedules}
          runs={scheduleManagement.scheduleRuns}
          form={scheduleManagement.scheduleForm}
          error={scheduleManagement.scheduleError}
          isFormOpen={scheduleManagement.isScheduleFormOpen}
          config={config}
          onToggleForm={scheduleManagement.handleOpenScheduleForm}
          onFormChange={scheduleManagement.updateScheduleForm}
          onSubmit={scheduleManagement.handleSaveSchedule}
          onCancelForm={scheduleManagement.handleCancelScheduleEdit}
          onEdit={scheduleManagement.handleEditSchedule}
          onToggleTask={(task) => void scheduleManagement.handleToggleSchedule(task)}
          onDelete={(task) => void scheduleManagement.handleDeleteSchedule(task)}
          onRunClick={(run) => void scheduleManagement.handleScheduleRunClick(run)}
        />

        <EventRadarPanel events={events} stats={eventStats} latestEvent={latestEventView} />
      </aside>

      <HistoryModal
        isOpen={isHistoryOpen}
        sessions={sessionHistory}
        currentSessionId={currentSessionId}
        loadingSessionId={loadingSessionId}
        onClose={() => setIsHistoryOpen(false)}
        onLoad={handleLoadSession}
      />
    </div>
  );
}

function useScheduleManagement(config: ConfigResponse | null, onLoadSession: (sessionId: string) => Promise<void>) {
  const [schedules, setSchedules] = useState<ScheduleTask[]>([]);
  const [scheduleRuns, setScheduleRuns] = useState<ScheduleRunsResponse>({ active: [], recent: [] });
  const [isScheduleFormOpen, setIsScheduleFormOpen] = useState(false);
  const [scheduleForm, setScheduleForm] = useState<ScheduleFormState>(() => createDefaultScheduleForm());
  const [scheduleError, setScheduleError] = useState('');
  const refreshSchedules = async () => setSchedules((await fetchJson<SchedulesResponse>('/api/schedules')).schedules);
  const refreshScheduleRuns = async () => {
    try {
      const [runs, tasks] = await Promise.all([fetchJson<ScheduleRunsResponse>('/api/schedule-runs'), fetchJson<SchedulesResponse>('/api/schedules')]);
      setScheduleRuns(runs); setSchedules(tasks.schedules);
    } catch (error) { setScheduleError(error instanceof Error ? error.message : '刷新定时任务失败'); }
  };
  useEffect(() => { void refreshScheduleRuns(); }, []);
  useEffect(() => setScheduleForm((prev) => ({ ...prev, working_dir: prev.working_dir || config?.workspace_path || '' })), [config?.workspace_path]);
  useEffect(() => {
    let stopped = false;
    const poll = async () => { await refreshScheduleRuns(); if (!stopped) timer = window.setTimeout(poll, scheduleRuns.active.length ? 3000 : 30000); };
    let timer = window.setTimeout(poll, scheduleRuns.active.length ? 3000 : 30000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [scheduleRuns.active.length]);
  useEffect(() => { const onFocus = () => void refreshScheduleRuns(); window.addEventListener('focus', onFocus); return () => window.removeEventListener('focus', onFocus); }, []);
  const reset = () => { setScheduleForm(createDefaultScheduleForm(config?.workspace_path || '')); setIsScheduleFormOpen(false); };
  const handleSaveSchedule = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setScheduleError('');
    if (!scheduleForm.provider || !scheduleForm.model) { setScheduleError(`请先选择 ${scheduleForm.provider ? 'model' : 'provider'}`); return; }
    try {
      const payload = { name: scheduleForm.name, prompt: scheduleForm.prompt, agent_name: scheduleForm.agent_name, provider: scheduleForm.provider, model: scheduleForm.model, working_dir: scheduleForm.working_dir, enabled: scheduleForm.enabled, trigger: buildScheduleTrigger(scheduleForm) };
      if (scheduleForm.editing_id) await requestJson(`/api/schedules/${scheduleForm.editing_id}`, { method: 'PATCH', body: payload }); else await postJson('/api/schedules', payload);
      reset(); await refreshSchedules(); await refreshScheduleRuns();
    } catch (error) { setScheduleError(error instanceof Error ? error.message : '保存定时任务失败'); }
  };
  const updateScheduleForm = (patch: Partial<ScheduleFormState>) => setScheduleForm((prev) => {
    const next = { ...prev, ...patch };
    if (patch.provider !== undefined && !config?.activated_providers.find((item) => item.provider === patch.provider)?.models.includes(next.model)) next.model = '';
    return next;
  });
  const handleEditSchedule = (task: ScheduleTask) => { setScheduleError(''); setScheduleForm(scheduleToForm(task)); setIsScheduleFormOpen(true); };
  const handleOpenScheduleForm = () => { setScheduleError(''); setScheduleForm(createDefaultScheduleForm(config?.workspace_path || '')); setIsScheduleFormOpen(true); };
  const handleToggleSchedule = async (task: ScheduleTask) => { setScheduleError(''); try { await requestJson(`/api/schedules/${task.id}`, { method: 'PATCH', body: { enabled: !task.enabled } }); await refreshSchedules(); await refreshScheduleRuns(); } catch (error) { setScheduleError(error instanceof Error ? error.message : '更新定时任务状态失败'); } };
  const handleDeleteSchedule = async (task: ScheduleTask) => { if (!window.confirm(`删除定时任务「${task.name}」？未启动的等待任务会被取消，正在运行的 worker 不会被终止。`)) return; setScheduleError(''); try { await requestJson(`/api/schedules/${task.id}`, { method: 'DELETE' }); if (scheduleForm.editing_id === task.id) reset(); await refreshSchedules(); await refreshScheduleRuns(); } catch (error) { setScheduleError(error instanceof Error ? error.message : '删除定时任务失败'); } };
  const handleScheduleRunClick = async (run: ScheduleRun) => { if (!run.session_id) { if (run.error) setScheduleError(run.error); return; } await onLoadSession(run.session_id); };
  return { schedules, scheduleRuns, isScheduleFormOpen, scheduleForm, scheduleError, handleOpenScheduleForm, updateScheduleForm, handleSaveSchedule, handleEditSchedule, handleCancelScheduleEdit: reset, handleToggleSchedule, handleDeleteSchedule, handleScheduleRunClick };
}

function SchedulePanel({
  schedules,
  runs,
  form,
  error,
  isFormOpen,
  config,
  onToggleForm,
  onFormChange,
  onSubmit,
  onCancelForm,
  onEdit,
  onToggleTask,
  onDelete,
  onRunClick,
}: {
  schedules: ScheduleTask[];
  runs: ScheduleRunsResponse;
  form: ScheduleFormState;
  error: string;
  isFormOpen: boolean;
  config: ConfigResponse | null;
  onToggleForm: () => void;
  onFormChange: (patch: Partial<ScheduleFormState>) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancelForm: () => void;
  onEdit: (task: ScheduleTask) => void;
  onToggleTask: (task: ScheduleTask) => void;
  onDelete: (task: ScheduleTask) => void;
  onRunClick: (run: ScheduleRun) => void;
}) {
  const providerOption = config?.activated_providers.find((item) => item.provider === form.provider) || null;
  const modelOptions = providerOption?.models || [];
  const visibleRuns = dedupeRuns([...runs.active, ...runs.recent]).slice(0, 8);
  const isEditing = Boolean(form.editing_id);
  const enabledCount = schedules.filter((task) => task.enabled).length;
  const nextTask = schedules
    .filter((task) => task.next_run_at)
    .sort((left, right) => String(left.next_run_at).localeCompare(String(right.next_run_at)))[0];
  return (
    <section className="panel schedule-panel">
      <PanelTitle
        icon={<CalendarClock size={16} />}
        title="定时任务"
        badge={`${runs.active.length}/${schedules.length}`}
        action={
          <button type="button" className="button secondary icon-button" onClick={onToggleForm} title="创建定时任务" aria-label="创建定时任务">
            <Plus size={14} />
          </button>
        }
      />
      {error ? (
        <div className="schedule-error" role="alert">
          <OctagonAlert size={14} />
          <span>{error}</span>
        </div>
      ) : null}
      <div className="schedule-overview" aria-label="定时任务概览">
        <span>
          <strong>{enabledCount}</strong>
          启用
        </span>
        <span>
          <strong>{runs.active.length}</strong>
          运行中
        </span>
        <span title={nextTask?.next_run_at || '-'}>
          <strong>{nextTask ? formatSessionTime(nextTask.next_run_at || '') : '-'}</strong>
          下次
        </span>
      </div>
      {isFormOpen ? (
        <form className="schedule-form" onSubmit={onSubmit}>
          <div className="schedule-form-title">
            <strong>{isEditing ? '编辑任务' : '创建任务'}</strong>
            <button type="button" className="button ghost icon-button" onClick={onCancelForm} title="取消" aria-label="取消">
              <X size={14} />
            </button>
          </div>
          <label className="field">
            <span>任务名</span>
            <input value={form.name} onChange={(event) => onFormChange({ name: event.target.value })} />
          </label>
          <label className="field">
            <span>Prompt</span>
            <textarea rows={3} value={form.prompt} onChange={(event) => onFormChange({ prompt: event.target.value })} />
          </label>
          <div className="schedule-grid">
            <label className="field">
              <span>Agent</span>
              <ConfigSelect
                value={form.agent_name}
                options={(config?.agents || ['build']).map((agent) => ({ value: agent, label: agent }))}
                onChange={(value) => onFormChange({ agent_name: value })}
              />
            </label>
            <label className="field">
              <span>Provider</span>
              <ConfigSelect
                value={form.provider}
                options={[
                  { value: '', label: '选择' },
                  ...(config?.activated_providers || []).map((item) => ({ value: item.provider, label: item.label })),
                ]}
                onChange={(value) => onFormChange({ provider: value })}
              />
            </label>
          </div>
          <label className="field">
            <span>Model</span>
            <ConfigSelect
              value={form.model}
              options={[{ value: '', label: '选择 model' }, ...modelOptions.map((item) => ({ value: item, label: item }))]}
              onChange={(value) => onFormChange({ model: value })}
              disabled={!form.provider}
            />
          </label>
          <label className="field">
            <span>执行目录</span>
            <input value={form.working_dir} onChange={(event) => onFormChange({ working_dir: event.target.value })} />
          </label>
          <div className="schedule-grid">
            <label className="field">
              <span>触发</span>
              <ConfigSelect
                value={form.trigger_kind}
                options={[
                  { value: 'once', label: '单次' },
                  { value: 'interval', label: '间隔' },
                  { value: 'daily', label: '每日' },
                  { value: 'weekly', label: '每周' },
                ]}
                onChange={(value) => onFormChange({ trigger_kind: value as ScheduleFormState['trigger_kind'] })}
              />
            </label>
            {form.trigger_kind === 'once' ? (
              <label className="field">
                <span>执行时间</span>
                <input type="datetime-local" value={form.run_at} onChange={(event) => onFormChange({ run_at: event.target.value })} />
              </label>
            ) : null}
            {form.trigger_kind === 'interval' ? (
              <label className="field">
                <span>秒</span>
                <input value={form.interval_seconds} onChange={(event) => onFormChange({ interval_seconds: event.target.value })} />
              </label>
            ) : null}
            {form.trigger_kind === 'weekly' ? (
              <label className="field">
                <span>星期</span>
                <ConfigSelect value={form.day_of_week} options={WEEKDAY_OPTIONS} onChange={(value) => onFormChange({ day_of_week: value })} />
              </label>
            ) : null}
            {form.trigger_kind === 'daily' || form.trigger_kind === 'weekly' ? (
              <label className="field">
                <span>时间</span>
                <input value={form.time_of_day} onChange={(event) => onFormChange({ time_of_day: event.target.value })} />
              </label>
            ) : null}
          </div>
          {form.trigger_kind === 'daily' || form.trigger_kind === 'weekly' ? (
            <label className="field">
              <span>Timezone</span>
              <input placeholder="Asia/Shanghai" value={form.timezone} onChange={(event) => onFormChange({ timezone: event.target.value })} />
            </label>
          ) : null}
          <label className="schedule-toggle">
            <input type="checkbox" checked={form.enabled} onChange={(event) => onFormChange({ enabled: event.target.checked })} />
            <span>启用</span>
          </label>
          <button type="submit" className="button primary">
            <Check size={15} />
            {isEditing ? '保存' : '创建'}
          </button>
        </form>
      ) : null}
      <div className="schedule-section-title">
        <span>任务</span>
        <code>{schedules.length}</code>
      </div>
      <div className="schedule-task-list">
        {schedules.length === 0 ? (
          <p className="quiet-copy">暂无定时任务。</p>
        ) : (
          schedules.map((task) => (
            <div className="schedule-task-card" key={task.id}>
              <div className="schedule-task-main">
                <span className={`schedule-task-state ${task.enabled ? 'is-enabled' : 'is-disabled'}`}>{task.enabled ? '已启用' : '已关闭'}</span>
                <strong>{task.name}</strong>
                <span>{formatScheduleTrigger(task.trigger)}</span>
                <code title={task.working_dir}>{task.working_dir}</code>
                <span>下次：{task.next_run_at ? formatSessionTime(task.next_run_at) : '-'}</span>
              </div>
              <div className="schedule-task-actions">
                <button type="button" className="button ghost icon-button" onClick={() => onEdit(task)} title="编辑" aria-label="编辑">
                  <Pencil size={14} />
                </button>
                <button
                  type="button"
                  className="button ghost icon-button"
                  onClick={() => onToggleTask(task)}
                  title={task.enabled ? '关闭' : '开启'}
                  aria-label={task.enabled ? '关闭' : '开启'}
                >
                  {task.enabled ? <Square size={14} /> : <Play size={14} />}
                </button>
                <button type="button" className="button ghost icon-button danger" onClick={() => onDelete(task)} title="删除" aria-label="删除">
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))
        )}
      </div>
      <div className="schedule-section-title">
        <span>最近运行</span>
        <code>{visibleRuns.length}</code>
      </div>
      <div className="schedule-run-list">
        {visibleRuns.length === 0 ? (
          <p className="quiet-copy">暂无运行记录。</p>
        ) : (
          visibleRuns.map((run) => (
            <button type="button" className="schedule-run-card" key={run.id} onClick={() => onRunClick(run)}>
              <span className={`schedule-run-status ${getScheduleRunClass(run.status)}`}>{run.status}</span>
              <strong>{run.task_name}</strong>
              <span>{formatSessionTime(run.finished_at || run.started_at || run.scheduled_at)}</span>
              <code title={run.working_dir}>{run.working_dir}</code>
              {run.summary ? <p>{run.summary}</p> : null}
            </button>
          ))
        )}
      </div>
    </section>
  );
}

function PanelTitle({ icon, title, badge, action }: { icon: React.ReactNode; title: string; badge?: string; action?: React.ReactNode }) {
  return (
    <div className="panel-title">
      <div>
        {icon}
        <span>{title}</span>
      </div>
      <div className="panel-title-actions">
        {badge ? <code>{badge}</code> : null}
        {action}
      </div>
    </div>
  );
}

function MobileConsole(props: MobileConsoleProps) {
  const [activeTab, setActiveTab] = useState<MobileTab>('messages');
  const [isConfigOpen, setIsConfigOpen] = useState(false);
  const mobileContentRef = useRef<HTMLElement | null>(null);
  const hasHumanAction = Boolean(props.approvalRequest || props.questionRequest);
  const messageStreamSignal = buildMessageStreamSignal(
    props.messages,
    props.liveDelta,
    props.liveReasoningDelta,
    props.subagentLiveDeltas,
    props.subagentLiveReasoningDeltas,
  );
  const mobileScroll = useAutoScroll(messageStreamSignal, mobileContentRef);
  const mobileTabs: Array<{ value: MobileTab; label: string; icon: React.ReactNode; count?: number }> = [
    { value: 'messages', label: '消息', icon: <MessageSquareText size={15} />, count: props.messages.length },
    { value: 'events', label: '事件', icon: <ListTree size={15} />, count: props.events.length },
    { value: 'history', label: '历史', icon: <History size={15} />, count: props.sessionHistory.length },
    {
      value: 'action',
      label: hasHumanAction ? '待处理' : '运行',
      icon: hasHumanAction ? <ShieldCheck size={15} /> : <Radio size={15} />,
      count: props.eventStats.blockers || undefined,
    },
  ];

  useEffect(() => {
    if (hasHumanAction) {
      setActiveTab('action');
    }
  }, [hasHumanAction]);

  return (
    <div className="mobile-shell">
      <header className="mobile-topbar">
        <div className="mobile-brand">
          <div className="brand-mark" aria-hidden="true">
            <Terminal size={17} />
          </div>
          <div>
            <h1>CodePilot</h1>
            <p>{props.config?.workspace_id || 'mobile console'}</p>
          </div>
        </div>
        <div className="mobile-top-actions">
          <button type="button" className="mobile-icon-action" onClick={props.onNewTask} title="新会话" aria-label="新会话">
            <Plus size={16} />
          </button>
          <div className={`status-pill ${getStatusClass(props.statusText)}`}>
            <CircleDot size={12} />
            <span>{props.statusText}</span>
          </div>
        </div>
      </header>

      {props.formError ? (
        <section className="mobile-error" role="alert">
          <OctagonAlert size={15} />
          <span>{props.formError}</span>
        </section>
      ) : null}

      <main className="mobile-content" ref={mobileContentRef}>
        {activeTab === 'messages' ? (
          <MobileMessagePanel
            messages={props.messages}
            liveDelta={props.liveDelta}
            liveReasoningDelta={props.liveReasoningDelta}
            subagentLiveDeltas={props.subagentLiveDeltas}
            subagentLiveReasoningDeltas={props.subagentLiveReasoningDeltas}
            anchorRef={mobileScroll.anchorRef}
          />
        ) : null}

        {activeTab === 'events' ? (
          <EventRadarPanel events={props.events} stats={props.eventStats} latestEvent={props.latestEventView} variant="mobile" />
        ) : null}

        {activeTab === 'history' ? (
          <section className="mobile-panel">
            <div className="mobile-panel-heading">
              <PanelTitle icon={<History size={16} />} title="历史会话" badge={String(props.sessionHistory.length)} />
              <button type="button" className="button secondary" onClick={props.onNewTask}>
                <Plus size={14} />
                新会话
              </button>
            </div>
            <SessionHistoryList
              sessions={props.sessionHistory}
              currentSessionId={props.currentSessionId}
              loadingSessionId={props.loadingSessionId}
              onLoad={props.onLoadSession}
              variant="modal"
            />
          </section>
        ) : null}

        {activeTab === 'action' ? (
          <section className="mobile-panel">
            <PanelTitle icon={hasHumanAction ? <ShieldCheck size={16} /> : <Radio size={16} />} title={hasHumanAction ? '人工处理' : '运行详情'} />
            {props.approvalRequest ? (
              <ApprovalPanel
                approvalRequest={props.approvalRequest}
                approvalComment={props.approvalComment}
                onApprovalCommentChange={props.onApprovalCommentChange}
              />
            ) : props.questionRequest ? (
              <QuestionPanel {...props} />
            ) : (
              <>
                <RuntimeSignal statusText={props.statusText} latestEvent={props.latestEventView} />
                <div className="mobile-metrics" aria-label="运行统计">
                  <Metric icon={<MessageSquareText size={14} />} label="消息" value={String(props.messages.length)} />
                  <Metric icon={<Play size={14} />} label="工具" value={String(props.eventStats.tools)} />
                  <Metric icon={<AlertTriangle size={14} />} label="失败" value={String(props.eventStats.failed)} />
                  <Metric icon={<Zap size={14} />} label="tokens" value={formatNumber(props.totalTokens)} />
                </div>
                <div className="session-status-card mobile-session-card">
                  <div className={`status-pill ${getStatusClass(props.statusText)}`}>
                    <CircleDot size={12} />
                    <span>{props.statusText}</span>
                  </div>
                  <div className="session-id-block">
                    <span>session</span>
                    <code title={props.status?.session_id || '-'}>{props.status?.session_id || '-'}</code>
                  </div>
                </div>
                <TokenMeter summary={props.tokenSummary} total={props.totalTokens} />
              </>
            )}
          </section>
        ) : null}
        {activeTab === 'messages' && !mobileScroll.isAtBottom ? (
          <ScrollToBottomButton className="message-scroll-button mobile-scroll-button" onClick={mobileScroll.scrollToBottom} />
        ) : null}
      </main>

      <nav className="mobile-tabs" aria-label="手机控制台视图">
        {mobileTabs.map((tab) => (
          <button
            type="button"
            className={activeTab === tab.value ? 'is-active' : ''}
            key={tab.value}
            onClick={() => setActiveTab(tab.value)}
          >
            {tab.icon}
            <span>{tab.label}</span>
            {typeof tab.count === 'number' ? <code>{tab.count}</code> : null}
          </button>
        ))}
      </nav>

      <form className={`mobile-composer ${hasHumanAction ? 'has-human-action' : ''}`} onSubmit={props.onStart}>
        <div className="mobile-composer-head">
          <button
            type="button"
            className={`mobile-config-toggle ${isConfigOpen ? 'is-open' : ''}`}
            onClick={() => setIsConfigOpen((prev) => !prev)}
            aria-expanded={isConfigOpen}
          >
            <SlidersHorizontal size={15} />
            <span>{props.agentName || 'agent'} · {props.model || 'model'}</span>
            <ChevronDown size={15} aria-hidden="true" />
          </button>
          {!hasHumanAction ? (
            <button type="button" className="mobile-stop-button" onClick={props.onStop} title="停止" aria-label="停止任务">
              <Square size={14} />
            </button>
          ) : null}
        </div>

        {isConfigOpen ? (
          <div className="mobile-config-grid">
            <label className="field compact-field">
              <span>Agent</span>
              <ConfigSelect
                value={props.agentName}
                options={(props.config?.agents || ['build', 'plan']).map((agent) => ({ value: agent, label: agent }))}
                onChange={props.onAgentChange}
              />
            </label>
            <label className="field compact-field">
              <span>Provider</span>
              <ConfigSelect
                value={props.provider}
                options={[
                  { value: '', label: '选择 provider' },
                  ...(props.config?.activated_providers || []).map((item) => ({ value: item.provider, label: item.label })),
                ]}
                onChange={props.onProviderChange}
              />
            </label>
            <label className="field compact-field model-field">
              <span>Model</span>
              <ConfigSelect
                value={props.model}
                options={[{ value: '', label: '选择 model' }, ...props.modelOptions.map((item) => ({ value: item, label: item }))]}
                onChange={props.onModelChange}
                disabled={!props.provider}
              />
            </label>
            <label className="field compact-field thinking-field">
              <span>思考</span>
              <div className="thinking-select">
                <Brain size={14} aria-hidden="true" />
                <ConfigSelect
                  value={props.selectedThinking ? props.thinkingValue || props.selectedThinking.default_value : ''}
                  options={props.thinkingOptions}
                  onChange={props.onThinkingChange}
                  disabled={!props.selectedThinking || props.selectedThinking.allowed_values.length <= 1}
                />
              </div>
            </label>
          </div>
        ) : null}

        {props.approvalRequest ? (
          <div className="mobile-human-actions">
            <button type="button" className="button primary" onClick={() => props.onApproval(true)}>
              <Check size={15} />
              同意
            </button>
            <button type="button" className="button danger" onClick={() => props.onApproval(false)}>
              <X size={15} />
              拒绝
            </button>
          </div>
        ) : props.questionRequest ? (
          <div className="mobile-human-actions">
            <button type="button" className="button secondary" onClick={props.onQuestionDecline}>
              <X size={15} />
              退出
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={() => props.onMoveActiveQuestion(-1)}
              disabled={props.activeQuestionIndex === 0}
            >
              <ChevronLeft size={15} />
              上一题
            </button>
            <button type="button" className="button primary" onClick={props.onConfirmActiveQuestion}>
              {props.activeQuestionIndex === props.questionRequest.questions.length - 1 ? <Check size={15} /> : <ChevronRight size={15} />}
              {props.activeQuestionIndex === props.questionRequest.questions.length - 1 ? '提交' : '下一题'}
            </button>
          </div>
        ) : (
          <>
            <AttachmentTray attachments={props.attachments} onRemove={props.onRemoveAttachment} compact />
            <div className="mobile-input-row">
              <AttachmentPicker onFiles={props.onAttachmentFiles} compact />
              <div className="mobile-input-shell">
                <textarea
                  ref={props.taskInputRef}
                  rows={2}
                  placeholder="输入任务..."
                  value={props.task}
                  onChange={props.onTaskChange}
                  onKeyDown={props.onTaskKeyDown}
                />
                <ComposerAutocompletePanel
                  state={props.composerAutocomplete}
                  options={props.composerOptions}
                  loading={props.isFileSuggestionLoading}
                  onHover={props.onComposerOptionHover}
                  onPick={props.onComposerOptionPick}
                  compact
                />
              </div>
              <button type="submit" className="button primary mobile-send-button" title="发送" aria-label="发送">
                <Send size={15} />
              </button>
            </div>
          </>
        )}
      </form>
    </div>
  );
}

function ComposerAutocompletePanel({
  state,
  options,
  loading,
  onHover,
  onPick,
  compact = false,
}: {
  state: ComposerAutocomplete | null;
  options: ComposerOption[];
  loading: boolean;
  onHover: (index: number) => void;
  onPick: (option: ComposerOption) => void;
  compact?: boolean;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const activeOptionRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const list = listRef.current;
    const activeOption = activeOptionRef.current;
    if (!list || !activeOption) {
      return;
    }
    const listBounds = list.getBoundingClientRect();
    const optionBounds = activeOption.getBoundingClientRect();
    if (optionBounds.top < listBounds.top) {
      list.scrollTop -= listBounds.top - optionBounds.top;
    } else if (optionBounds.bottom > listBounds.bottom) {
      list.scrollTop += optionBounds.bottom - listBounds.bottom;
    }
  }, [state?.activeIndex]);

  if (!state) {
    return null;
  }
  const title = state.kind === 'skill' ? 'Skills' : 'Files';
  return (
    <div className={`composer-autocomplete ${compact ? 'is-compact' : ''}`} role="listbox" aria-label={`${title} 补全`}>
      <div className="autocomplete-heading">
        <span>{state.trigger}</span>
        <strong>{title}</strong>
        {state.query ? <code>{state.query}</code> : null}
      </div>
      {loading ? <div className="autocomplete-empty">搜索中...</div> : null}
      {!loading && options.length === 0 ? <div className="autocomplete-empty">无匹配结果</div> : null}
      {!loading && options.length > 0 ? (
        <div className="autocomplete-list" ref={listRef}>
          {options.map((option, index) => (
            <button
              type="button"
              className={`autocomplete-option ${index === state.activeIndex ? 'is-active' : ''}`}
              key={`${state.kind}:${option.value}`}
              ref={index === state.activeIndex ? activeOptionRef : null}
              onMouseEnter={() => onHover(index)}
              onMouseDown={(event) => {
                event.preventDefault();
                onPick(option);
              }}
              role="option"
              aria-selected={index === state.activeIndex}
            >
              <span>{option.label}</span>
              {option.description ? <small>{option.description}</small> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function MobileMessagePanel({
  messages,
  liveDelta,
  liveReasoningDelta,
  subagentLiveDeltas,
  subagentLiveReasoningDeltas,
  anchorRef,
}: {
  messages: MessageRecord[];
  liveDelta: string;
  liveReasoningDelta: string;
  subagentLiveDeltas: Record<string, string>;
  subagentLiveReasoningDeltas: Record<string, string>;
  anchorRef: RefObject<HTMLDivElement>;
}) {
  return (
    <section className="mobile-panel mobile-message-list" aria-label="会话消息" ref={anchorRef}>
      <MessageStream
        messages={messages}
        liveDelta={liveDelta}
        liveReasoningDelta={liveReasoningDelta}
        subagentLiveDeltas={subagentLiveDeltas}
        subagentLiveReasoningDeltas={subagentLiveReasoningDeltas}
        renderParts={renderMessageParts}
        renderStepFinish={(part, key) => <StepFinishView key={key} part={part} />}
        formatTime={formatTime}
        formatTokenUsage={formatTokenUsage}
      />
    </section>
  );
}

function QuestionPanel(props: MobileConsoleProps) {
  if (!props.questionRequest || !props.activeQuestion || !props.activeAnswer) {
    return null;
  }

  return (
    <section className="composer-question mobile-question" aria-label="用户回答">
      <div className="approval-heading">
        <MessageSquareText size={16} />
        <strong>需要回答</strong>
        <code>{props.questionRequest.question_id}</code>
      </div>
      <div className="question-flow">
        <div className="question-progress" aria-label="问题进度">
          {props.questionRequest.questions.map((question, index) => {
            const answer = props.questionAnswers[question.id] || { values: [], note: '' };
            const isActive = index === props.activeQuestionIndex;
            const isDone = answer.values.length > 0;
            return (
              <button
                type="button"
                className={`question-step ${isActive ? 'is-active' : ''} ${isDone ? 'is-done' : ''}`}
                key={question.id}
                onClick={() => {
                  props.onSetActiveQuestionIndex(index);
                  props.onSetActiveQuestionOptionIndex(0);
                  props.onClearQuestionError();
                  props.onFocusQuestionOptions();
                }}
                aria-current={isActive ? 'step' : undefined}
              >
                {index + 1}
              </button>
            );
          })}
        </div>

        <fieldset className="question-fieldset">
          <legend>
            <span>
              第 {props.activeQuestionIndex + 1} / {props.questionRequest.questions.length} 题
            </span>
            {props.activeQuestion.question}
          </legend>
          <div
            className="question-options"
            ref={props.questionOptionsRef}
            role={props.activeQuestion.multiple ? 'group' : 'radiogroup'}
            aria-label={props.activeQuestion.question}
            tabIndex={0}
            onKeyDown={(event) => props.onQuestionOptionsKeyDown(event, props.activeQuestion as QuestionItem)}
          >
            {props.activeQuestion.options.map((option, optionIndex) => {
              const inputType = props.activeQuestion?.multiple ? 'checkbox' : 'radio';
              const checked = props.activeAnswer?.values.includes(option.value) || false;
              const isKeyboardActive = optionIndex === props.activeQuestionOptionIndex;
              return (
                <label className={`question-option ${isKeyboardActive ? 'is-keyboard-active' : ''}`} key={option.value}>
                  <input
                    type={inputType}
                    name={props.activeQuestion?.id}
                    checked={checked}
                    tabIndex={-1}
                    onChange={(event) => {
                      props.onSetActiveQuestionOptionIndex(optionIndex);
                      props.onQuestionChoiceChange(props.activeQuestion as QuestionItem, option.value, event.target.checked);
                    }}
                  />
                  <span>{option.label}</span>
                </label>
              );
            })}
          </div>
          <textarea
            ref={props.questionNoteRef}
            rows={3}
            placeholder="备注"
            value={props.activeAnswer.note}
            onChange={(event) => props.onQuestionNoteChange((props.activeQuestion as QuestionItem).id, event.target.value)}
            onKeyDown={props.onQuestionNoteKeyDown}
          />
          {props.questionError ? <p className="question-error">{props.questionError}</p> : null}
        </fieldset>
      </div>
    </section>
  );
}

function SessionHistoryList({
  sessions,
  currentSessionId,
  loadingSessionId,
  onLoad,
  variant = 'compact',
}: {
  sessions: SessionSummary[];
  currentSessionId: string | null;
  loadingSessionId: string | null;
  onLoad: (sessionId: string) => void;
  variant?: 'compact' | 'modal';
}) {
  return (
    <div className={`session-history session-history-${variant}`}>
      <div className="session-history-heading">
        <span>历史记录</span>
        <code>{sessions.length}</code>
      </div>
      <div className="session-list">
        {sessions.length === 0 ? (
          <p className="quiet-copy">暂无历史会话。</p>
        ) : (
          sessions.map((session) => {
            const isCurrent = session.session_id === currentSessionId;
            const isLoading = session.session_id === loadingSessionId;
            return (
              <article className={`session-item ${isCurrent ? 'is-current' : ''}`} key={session.session_id}>
                <div className="session-item-main">
                  <strong>
                    {session.source === 'schedule' ? <span className="session-source-badge">定时任务</span> : null}
                    {session.source === 'schedule' && session.schedule_task_name
                      ? session.schedule_task_name
                      : session.title || session.preview || session.session_id}
                  </strong>
                  {variant === 'modal' && session.preview ? <p>{session.preview}</p> : null}
                  <span>{formatSessionTime(session.updated_at || session.created_at)}</span>
                </div>
                <div className="session-item-meta">
                  <span>{session.status || 'UNKNOWN'}</span>
                  <span>{session.message_count} 条</span>
                </div>
                <button
                  type="button"
                  className="button secondary session-load-button"
                  disabled={isCurrent || Boolean(loadingSessionId)}
                  onClick={() => onLoad(session.session_id)}
                >
                  {isLoading ? '加载中' : isCurrent ? '当前' : '加载'}
                </button>
              </article>
            );
          })
        )}
      </div>
    </div>
  );
}

function HistoryModal({
  isOpen,
  sessions,
  currentSessionId,
  loadingSessionId,
  onClose,
  onLoad,
}: {
  isOpen: boolean;
  sessions: SessionSummary[];
  currentSessionId: string | null;
  loadingSessionId: string | null;
  onClose: () => void;
  onLoad: (sessionId: string) => void;
}) {
  useEffect(() => {
    if (!isOpen) {
      return;
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        onClose();
      }
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="history-modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <section className="history-modal panel" role="dialog" aria-modal="true" aria-labelledby="history-modal-title">
        <header className="history-modal-header">
          <div>
            <span className="eyebrow">session archive</span>
            <h2 id="history-modal-title">历史记录</h2>
          </div>
          <button type="button" className="button secondary icon-button" onClick={onClose} title="关闭" aria-label="关闭历史记录">
            <X size={15} />
          </button>
        </header>
        <SessionHistoryList
          sessions={sessions}
          currentSessionId={currentSessionId}
          loadingSessionId={loadingSessionId}
          onLoad={onLoad}
          variant="modal"
        />
      </section>
    </div>
  );
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="metric">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function RuntimeSignal({ statusText, latestEvent }: { statusText: string; latestEvent: EventViewModel | null }) {
  return (
    <section className={`runtime-signal ${latestEvent ? `tone-${latestEvent.tone}` : 'tone-neutral'}`} aria-label="当前运行信号">
      <div>
        <span>当前信号</span>
        <strong>{latestEvent?.title || statusText}</strong>
      </div>
      <p title={latestEvent?.summary || statusText}>{latestEvent?.summary || '尚未收到关键事件。'}</p>
    </section>
  );
}

function EventRadarPanel({
  events,
  stats,
  latestEvent,
  variant = 'desktop',
}: {
  events: StreamEvent[];
  stats: EventStats;
  latestEvent: EventViewModel | null;
  variant?: 'desktop' | 'mobile';
}) {
  const [isVisible, setIsVisible] = useState(true);
  const [page, setPage] = useState(1);
  const totalPages = Math.max(1, Math.ceil(events.length / RADAR_PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const pageStart = (currentPage - 1) * RADAR_PAGE_SIZE;
  const visibleEvents = events.slice().reverse().slice(pageStart, pageStart + RADAR_PAGE_SIZE);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  const rootClassName = `${variant === 'mobile' ? 'mobile-panel' : 'panel'} event-panel ${isVisible ? '' : 'is-collapsed'}`;
  const rangeLabel =
    events.length === 0
      ? '0'
      : `${pageStart + 1}-${Math.min(pageStart + visibleEvents.length, events.length)}`;

  return (
    <section className={rootClassName}>
      <PanelTitle
        icon={<ListTree size={16} />}
        title="任务雷达"
        badge={`${events.length}/200`}
        action={
          <button
            type="button"
            className={`button secondary icon-button radar-visibility-toggle ${isVisible ? 'is-open' : ''}`}
            onClick={() => setIsVisible((prev) => !prev)}
            title={isVisible ? '隐藏任务雷达' : '显示任务雷达'}
            aria-label={isVisible ? '隐藏任务雷达' : '显示任务雷达'}
            aria-expanded={isVisible}
          >
            <ChevronDown size={14} />
          </button>
        }
      />
      {isVisible ? (
        <>
          <EventRadarHeader stats={stats} latestEvent={latestEvent} />
          <div className="event-list">
            {events.length === 0 ? (
              <p className="quiet-copy">等待关键事件。</p>
            ) : (
              visibleEvents.map((event) => <EventItem key={event.seq} event={event} buildView={buildEventViewModel} />)
            )}
          </div>
          {totalPages > 1 ? (
            <div className="radar-pagination" aria-label="任务雷达分页">
              <span>
                {rangeLabel} / {events.length}
              </span>
              <div>
                <button
                  type="button"
                  className="button ghost icon-button"
                  onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                  disabled={currentPage === 1}
                  title="上一页"
                  aria-label="上一页"
                >
                  <ChevronLeft size={14} />
                </button>
                <code>{currentPage}/{totalPages}</code>
                <button
                  type="button"
                  className="button ghost icon-button"
                  onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
                  disabled={currentPage === totalPages}
                  title="下一页"
                  aria-label="下一页"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          ) : null}
        </>
      ) : (
        <button type="button" className="radar-collapsed-strip" onClick={() => setIsVisible(true)}>
          <span>已隐藏</span>
          <strong>{latestEvent?.title || '暂无关键事件'}</strong>
          <code>{events.length} events</code>
        </button>
      )}
    </section>
  );
}

function EventRadarHeader({ stats, latestEvent }: { stats: EventStats; latestEvent: EventViewModel | null }) {
  const headline = stats.failed > 0 ? '存在失败节点' : stats.blockers > 0 ? '等待人工处理' : latestEvent?.title || '监听关键事件';
  const detail =
    latestEvent?.summary ||
    '工具调用、人工交互、上下文压缩和会话状态会在这里汇总。';

  return (
    <section className={`event-radar-header ${stats.failed > 0 ? 'tone-danger' : stats.blockers > 0 ? 'tone-warn' : 'tone-neutral'}`}>
      <div className="radar-orbit" aria-hidden="true">
        <span />
      </div>
      <div className="radar-copy">
        <span>run radar</span>
        <strong>{headline}</strong>
        <p title={detail}>{detail}</p>
      </div>
      <div className="radar-stats" aria-label="事件统计">
        <span>tools {stats.tools}</span>
        <span>fail {stats.failed}</span>
        <span>hold {stats.blockers}</span>
      </div>
    </section>
  );
}

function TokenMeter({ summary, total }: { summary: TokenSummary; total: number }) {
  const parts = [
    { key: 'input', label: 'in', value: summary.input, className: 'input' },
    { key: 'output', label: 'out', value: summary.output, className: 'output' },
    { key: 'reasoning', label: 'reason', value: summary.reasoning, className: 'reasoning' },
  ];
  const cacheHitRate = summary.input > 0 ? Math.min(summary.cacheRead / summary.input, 1) : 0;
  const cacheHitPercent = cacheHitRate * 100;

  return (
    <section className="token-meter" aria-label="token 用量">
      <div className="token-meter-heading">
        <span>
          <Zap size={13} />
          tokens
        </span>
        <strong>{formatNumber(total)}</strong>
      </div>
      <div className="cache-hitline">
        <span>cache hit</span>
        <strong>{formatPercent(cacheHitPercent)}</strong>
      </div>
      <div className="cache-meter" aria-label={`缓存命中率 ${formatPercent(cacheHitPercent)}`}>
        <span style={{ width: `${cacheHitPercent}%` }} />
      </div>
      <div className="token-legend">
        {parts.map((part) => (
          <span className={part.className} key={part.key}>
            {part.label} {formatNumber(part.value)}
          </span>
        ))}
        <span className="cache">
          cache <b>{formatNumber(summary.cacheRead)}</b> / in
        </span>
      </div>
    </section>
  );
}

function renderMessageParts(parts: MessagePart[] | undefined, isAssistant: boolean) {
  if (!parts?.length) {
    return <p className="empty-message">（空消息）</p>;
  }
  const reasoningParts = parts.filter((part) => part.type === 'reasoning');
  const remainingParts = parts.filter((part) => part.type !== 'reasoning');
  const visibleParts: React.ReactNode[] = [];
  reasoningParts.forEach((part, index) => {
    const rendered = renderPart(part, index, isAssistant);
    if (rendered !== null) {
      visibleParts.push(rendered);
    }
  });

  for (let index = 0; index < remainingParts.length; index += 1) {
    const part = remainingParts[index];
    if (part.type !== 'tool') {
      const rendered = renderPart(part, index + reasoningParts.length, isAssistant);
      if (rendered !== null) {
        visibleParts.push(rendered);
      }
      continue;
    }

    const groupId = getToolGroupId(part);
    const toolParts = [part];
    let nextIndex = index + 1;
    while (groupId && remainingParts[nextIndex]?.type === 'tool' && getToolGroupId(remainingParts[nextIndex]) === groupId) {
      toolParts.push(remainingParts[nextIndex]);
      nextIndex += 1;
    }
    if (groupId && toolParts.length > 1) {
      visibleParts.push(<ToolGroupView key={`tool-group-${index}`} parts={toolParts} />);
    } else {
      visibleParts.push(<ToolPartView key={`tool-${index}`} part={part} />);
    }
    index = nextIndex - 1;
  }
  return visibleParts.length ? visibleParts : <p className="empty-message">（无可展示内容）</p>;
}

function renderPart(part: MessagePart, index: number, isAssistant: boolean): React.ReactNode {
  const key = `${String(part.type || 'part')}-${index}`;
  if (part.type === 'text') {
    return <TextBlock key={key} text={stringValue(part.text)} />;
  }
  if (part.type === 'reasoning') {
    return <ReasoningBlock key={key} text={stringValue(part.text)} />;
  }
  if (part.type === 'tool') {
    return <ToolPartView key={key} part={part} />;
  }
  if (part.type === 'step-start') {
    return null;
  }
  if (part.type === 'step-finish') {
    return null;
  }
  if (part.type === 'file') {
    return <FilePartView key={key} part={part} />;
  }
  if (part.type === 'patch') {
    return <PatchPartView key={key} part={part} />;
  }
  if (part.type === 'snapshot') {
    return <SnapshotPartView key={key} part={part} />;
  }
  if (part.type === 'retry') {
    return <RetryPartView key={key} part={part} />;
  }
  if (part.type === 'compaction') {
    return <CompactNote key={key} tone="neutral" title="上下文已压缩" value={boolValue(part.auto) ? '自动触发' : '手动触发'} />;
  }
  if (part.type === 'subtask') {
    return <CompactNote key={key} tone="neutral" title="子任务" value={stringValue(part.description) || stringValue(part.prompt)} />;
  }
  if (part.type === 'agent') {
    return <CompactNote key={key} tone="neutral" title="Agent" value={stringValue(part.name)} />;
  }
  return isAssistant ? <UnknownPartView key={key} part={part} /> : <TextBlock key={key} text={jsonPretty(part)} />;
}

function ToolGroupView({ parts }: { parts: MessagePart[] }) {
  const statuses = parts.map((part) => getToolStatus(part));
  const failed = statuses.filter((status) => getToolTone(status) === 'tool-error').length;
  const running = statuses.filter((status) => status === 'pending' || status === 'running').length;
  const completed = parts.length - failed - running;
  const tone = failed > 0 ? 'tool-error' : running > 0 ? 'tool-pending' : 'tool-ok';

  return (
    <section className={`tool-group ${tone}`} aria-label={`并发工具组，共 ${parts.length} 个工具`}>
      <header className="tool-group-header">
        <div>
          <ListTree size={14} />
          <strong>并发工具组</strong>
          <span>{parts.length} 个工具同时执行</span>
        </div>
        <div className="tool-group-meter" aria-label={`完成 ${completed}，运行中 ${running}，失败 ${failed}`}>
          {parts.map((part, index) => (
            <span className={getToolTone(getToolStatus(part))} key={String(part.call_id || index)} />
          ))}
        </div>
      </header>
      <div className="tool-group-grid">
        {parts.map((part, index) => (
          <ToolPartView key={String(part.call_id || index)} part={part} index={index + 1} />
        ))}
      </div>
    </section>
  );
}

function ToolPartView({ part, index }: { part: MessagePart; index?: number }) {
  const state = asRecord(part.state) as ToolState;
  const output = asRecord(state.output);
  const input = asRecord(state.input);
  const tool = stringValue(part.tool) || 'unknown_tool';
  const status = stringValue(state.status) || 'pending';
  const tone = getToolTone(status);

  if (isTodoTool(tool)) {
    return <TodoToolView tool={tool} input={input} output={output} state={state} status={status} tone={tone} index={index} />;
  }
  if (tool === 'question') {
    return <QuestionToolView input={input} output={output} state={state} status={status} tone={tone} index={index} />;
  }

  const display = buildToolDisplay(tool, input, output);
  const command = stringValue(output.command) || stringValue(input.command);
  const description = stringValue(input.description) || stringValue(output.description);
  const cwd = stringValue(output.cwd) || stringValue(input.cwd);
  const filePath = stringValue(output.file_path) || stringValue(input.file_path);
  const url = stringValue(output.url) || stringValue(input.url);
  const finalUrl = stringValue(output.final_url);
  const operation = buildToolOperation(output);
  const errorMessage = buildToolErrorMessage(state, output);
  const resultText = stringValue(output.output);
  const diff = stringValue(output.diff);
  const stdout = stringValue(output.stdout);
  const stderr = stringValue(output.stderr);

  return (
    <article className={`tool-card ${tone}`}>
      <header className="tool-card-header">
        <div className="tool-title">
          {tone === 'tool-error' ? <AlertTriangle size={15} /> : <Play size={14} />}
          {index ? <em>{index}</em> : null}
          <span title={display.title}>{display.title}</span>
          {display.tag ? <small className="tool-name-tag">{display.tag}</small> : null}
        </div>
        <div className="tool-chips">
          <StatusChip status={status} />
          {typeof output.duration_ms === 'number' ? <span>{output.duration_ms}ms</span> : null}
          {typeof output.exit_code === 'number' ? <span>exit {output.exit_code}</span> : null}
        </div>
      </header>

      <div className="tool-meta-grid">
        {command ? <KeyValue label="command" value={command} mono /> : null}
        {cwd ? <KeyValue label="cwd" value={cwd} mono /> : null}
        {url && tool === 'webfetch' ? <KeyValue label="url" value={url} mono /> : null}
        {finalUrl && finalUrl !== url ? <KeyValue label="final" value={finalUrl} mono /> : null}
        {filePath ? <KeyValue label="file" value={filePath} mono /> : null}
        {operation ? <KeyValue label="operation" value={operation} /> : null}
        {description && tool !== 'bash_tool' ? <KeyValue label="description" value={description} /> : null}
      </div>

      {errorMessage ? (
        <div className="tool-error-message">
          <AlertTriangle size={14} />
          <span>{errorMessage}</span>
        </div>
      ) : null}

      {resultText ? <OutputBlock label="结果" value={resultText} /> : null}
      {stdout ? <OutputBlock label="stdout" value={stdout} truncated={boolValue(output.stdout_truncated)} /> : null}
      {stderr ? <OutputBlock label="stderr" value={stderr} tone="warn" truncated={boolValue(output.stderr_truncated)} /> : null}
      {diff ? <DiffOutputBlock value={diff} /> : null}
    </article>
  );
}

function QuestionToolView({
  input,
  output,
  state,
  status,
  tone,
  index,
}: {
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  state: ToolState;
  status: string;
  tone: string;
  index?: number;
}) {
  const questions = extractQuestionItems(output.questions) || extractQuestionItems(input.questions) || [];
  const answers = asRecord(output.answers);
  const questionId = stringValue(output.question_id) || stringValue(input.question_id);
  const errorMessage = buildToolErrorMessage(state, output);
  const declined = boolValue(output.declined);
  const summary = stringValue(output.output);
  const hasAnswers = Object.keys(answers).length > 0;

  return (
    <article className={`tool-card question-tool-card ${tone}`}>
      <header className="tool-card-header">
        <div className="tool-title">
          {tone === 'tool-error' ? <AlertTriangle size={15} /> : <MessageSquareText size={14} />}
          {index ? <em>{index}</em> : null}
          <span>{declined ? '用户拒绝回答' : hasAnswers ? '用户已回答问题' : '等待用户回答'}</span>
          <small className="tool-name-tag">question</small>
        </div>
        <div className="tool-chips">
          <StatusChip status={status} />
          {questions.length > 0 ? <span>{questions.length} 题</span> : null}
          {questionId ? <span>{questionId}</span> : null}
        </div>
      </header>

      {errorMessage ? (
        <div className="tool-error-message">
          <AlertTriangle size={14} />
          <span>{errorMessage}</span>
        </div>
      ) : null}

      {questions.length > 0 ? (
        <ol className="question-render-list" aria-label="question 工具问题">
          {questions.map((question, questionIndex) => (
            <li className="question-render-item" key={question.id || questionIndex}>
              <div className="question-render-title">
                <span>{questionIndex + 1}</span>
                <strong>{question.question || question.id || `问题 ${questionIndex + 1}`}</strong>
              </div>
              <div className="question-render-options">
                {question.options.map((option) => {
                  const selected = isQuestionOptionSelected(answers, question.id, option.value);
                  return (
                    <span className={selected ? 'is-selected' : ''} key={option.value || option.label}>
                      {option.label || option.value}
                    </span>
                  );
                })}
              </div>
              <QuestionAnswerView answer={asRecord(answers[question.id])} />
            </li>
          ))}
        </ol>
      ) : null}

      {!questions.length && summary ? <OutputBlock label="回答摘要" value={summary} /> : null}
      {questions.length > 0 && summary ? <OutputBlock label="回答摘要" value={summary} /> : null}
    </article>
  );
}

function QuestionAnswerView({ answer }: { answer: Record<string, unknown> }) {
  const values = Array.isArray(answer.values) ? answer.values.map(String).filter(Boolean) : [];
  const note = stringValue(answer.note).trim();
  if (!values.length && !note) {
    return <p className="question-render-empty">尚未记录回答。</p>;
  }
  return (
    <div className="question-render-answer">
      {values.length > 0 ? (
        <div>
          <span>回答</span>
          <code>{values.join('、')}</code>
        </div>
      ) : null}
      {note ? (
        <div>
          <span>备注</span>
          <code>{note}</code>
        </div>
      ) : null}
    </div>
  );
}

function TodoToolView({
  tool,
  input,
  output,
  state,
  status,
  tone,
  index,
}: {
  tool: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  state: ToolState;
  status: string;
  tone: string;
  index?: number;
}) {
  const todos = extractTodos(tool, input, output);
  const summary = summarizeTodos(todos);
  const errorMessage = buildToolErrorMessage(state, output);
  const action = tool === 'todo_write' ? '更新 TODO' : '读取 TODO';

  return (
    <article className={`tool-card todo-tool-card ${tone}`}>
      <header className="tool-card-header">
        <div className="tool-title">
          {tone === 'tool-error' ? <AlertTriangle size={15} /> : <ListTree size={14} />}
          {index ? <em>{index}</em> : null}
          <span>{action}</span>
          <small className="tool-name-tag">{tool}</small>
        </div>
        <div className="tool-chips">
          <StatusChip status={status} />
          <span>{summary.total} 项</span>
          <span>{summary.inProgress} 进行中</span>
          <span>{summary.completed} 完成</span>
        </div>
      </header>

      {errorMessage ? (
        <div className="tool-error-message">
          <AlertTriangle size={14} />
          <span>{errorMessage}</span>
        </div>
      ) : null}

      <div className="todo-summary-strip" aria-label="TODO 汇总">
        <span className="todo-summary-item todo-pending">待办 {summary.pending}</span>
        <span className="todo-summary-item todo-in-progress">进行中 {summary.inProgress}</span>
        <span className="todo-summary-item todo-completed">完成 {summary.completed}</span>
      </div>

      {todos.length > 0 ? (
        <ol className="todo-render-list" aria-label="TODO 列表">
          {todos.map((todo, todoIndex) => (
            <li className={`todo-render-item ${todo.status}`} key={`${todo.status}-${todo.priority}-${todo.content}-${todoIndex}`}>
              <span className="todo-status-mark" aria-hidden="true">
                {todo.status === 'completed' ? <Check size={12} /> : todo.status === 'in_progress' ? <Play size={11} /> : <CircleDot size={11} />}
              </span>
              <span className="todo-render-content">{todo.content}</span>
              <span className={`todo-priority ${todo.priority}`}>{formatTodoPriority(todo.priority)}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="todo-empty">当前没有 TODO。</p>
      )}
    </article>
  );
}

function StatusChip({ status }: { status: string }) {
  return <span className={`status-chip ${getToolTone(status)}`}>{status}</span>;
}

function KeyValue({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="tool-kv">
      <span>{label}</span>
      <code className={mono ? 'mono' : undefined}>{value}</code>
    </div>
  );
}

function OutputBlock({
  label,
  value,
  tone = 'normal',
  truncated = false,
}: {
  label: string;
  value: string;
  tone?: 'normal' | 'warn' | 'diff';
  truncated?: boolean;
}) {
  return (
    <details className={`tool-output ${tone}`}>
      <summary>
        <span>
          <ChevronRight size={12} />
          {label}
        </span>
        <code>{formatNumber(value.length)} chars</code>
        {truncated ? <small>已截断</small> : null}
      </summary>
      <pre>{value}</pre>
    </details>
  );
}

function DiffOutputBlock({ value }: { value: string }) {
  const files = parseUnifiedDiff(value);
  if (!files.length) {
    return <OutputBlock label="diff" value={value} tone="diff" />;
  }
  const additions = files.reduce((total, file) => total + file.additions, 0);
  const deletions = files.reduce((total, file) => total + file.deletions, 0);
  return (
    <details className="tool-output diff diff-review" open>
      <summary>
        <span>
          <ChevronRight size={12} />
          diff
        </span>
        <code>
          {files.length} 文件 · +{formatNumber(additions)} -{formatNumber(deletions)}
        </code>
        <code>{formatNumber(value.length)} chars</code>
      </summary>
      <div className="diff-file-list">
        {files.map((file, index) => (
          <DiffFileView file={file} key={`${file.oldPath}-${file.newPath}-${index}`} />
        ))}
      </div>
    </details>
  );
}

function DiffFileView({ file }: { file: DiffFile }) {
  const title = file.newPath || file.oldPath || '变更文件';
  const subtitle = file.oldPath && file.newPath && file.oldPath !== file.newPath ? `${file.oldPath} -> ${file.newPath}` : '';
  return (
    <section className="diff-file" aria-label={`文件变更 ${title}`}>
      <header className="diff-file-header">
        <div>
          <FileText size={13} />
          <strong title={title}>{title}</strong>
          {subtitle ? <span title={subtitle}>{subtitle}</span> : null}
        </div>
        <code>
          +{formatNumber(file.additions)} -{formatNumber(file.deletions)}
        </code>
      </header>
      <div className="diff-table" role="table" aria-label={`${title} diff`}>
        {file.lines.map((line, index) => (
          <DiffLineView line={line} key={`${line.kind}-${line.oldLine ?? 'x'}-${line.newLine ?? 'x'}-${index}`} />
        ))}
      </div>
    </section>
  );
}

function DiffLineView({ line }: { line: DiffLine }) {
  const marker = line.kind === 'add' ? '+' : line.kind === 'delete' ? '-' : line.kind === 'hunk' ? '@@' : '';
  return (
    <div className={`diff-line ${line.kind}`} role="row">
      <span className="diff-line-number" role="cell">
        {line.oldLine ?? ''}
      </span>
      <span className="diff-line-number" role="cell">
        {line.newLine ?? ''}
      </span>
      <span className="diff-line-marker" role="cell">
        {marker}
      </span>
      <code className="diff-line-code" role="cell">
        {line.content || ' '}
      </code>
    </div>
  );
}

function StepFinishView({ part }: { part: MessagePart }) {
  const reason = stringValue(part.reason);
  return <div className={`step-finish-note ${reason && reason !== 'completed' ? 'has-reason' : ''}`} aria-label="步骤结束" />;
}

function FilePartView({ part }: { part: MessagePart }) {
  const filename = stringValue(part.filename) || '文件';
  const mime = stringValue(part.mime);
  const url = stringValue(part.url);
  const source = asRecord(part.source);
  const sourceValue = stringValue(source.value);
  if (mime.startsWith('image/') && url) {
    return (
      <figure className="message-image-attachment">
        <img src={url} alt={filename} />
        <figcaption>
          <FileText size={13} />
          <span title={filename}>{filename}</span>
          <small>{mime}</small>
        </figcaption>
      </figure>
    );
  }
  return <CompactNote tone="file" title={filename} value={[mime, sourceValue].filter(Boolean).join(' · ')} />;
}

function PatchPartView({ part }: { part: MessagePart }) {
  const files = Array.isArray(part.files) ? part.files.map(String) : [];
  return <CompactNote tone="file" title="代码补丁" value={files.length ? files.join(', ') : stringValue(part.hash)} />;
}

function SnapshotPartView({ part }: { part: MessagePart }) {
  const snapshot = stringValue(part.snapshot);
  if (!snapshot) {
    return null;
  }
  return (
    <details className="raw-details snapshot-details">
      <summary>
        <FileText size={13} />
        会话快照
      </summary>
      <pre>{snapshot}</pre>
    </details>
  );
}

function RetryPartView({ part }: { part: MessagePart }) {
  const attempt = typeof part.attempt === 'number' ? `第 ${part.attempt} 次` : '重试';
  const error = asRecord(part.error);
  return <CompactNote tone="warn" title={attempt} value={stringValue(error.message) || '助手正在重试'} />;
}

function CompactNote({ tone, title, value }: { tone: 'neutral' | 'warn' | 'file'; title: string; value: string }) {
  return (
    <div className={`compact-note ${tone}`}>
      <FileText size={13} />
      <strong>{title}</strong>
      {value ? <span>{value}</span> : null}
    </div>
  );
}

function UnknownPartView({ part }: { part: MessagePart }) {
  return (
    <details className="raw-details">
      <summary>未识别片段：{String(part.type || 'unknown')}</summary>
      <pre>{jsonPretty(part)}</pre>
    </details>
  );
}

function buildToolDisplay(tool: string, input: Record<string, unknown>, output: Record<string, unknown>) {
  if (tool === 'bash_tool') {
    return {
      title: stringValue(input.description) || stringValue(output.description) || stringValue(output.command) || stringValue(input.command) || tool,
      tag: tool,
    };
  }
  if (tool === 'load_skill') {
    const skillName = stringValue(output.name) || stringValue(input.name);
    return {
      title: skillName ? `加载 skill：${skillName}` : '加载 skill',
      tag: tool,
    };
  }
  if (tool === 'webfetch') {
    const url = stringValue(output.url) || stringValue(input.url);
    return {
      title: url ? `获取 URL：${url}` : '获取 URL',
      tag: tool,
    };
  }
  return { title: tool, tag: '' };
}

function buildToolOperation(output: Record<string, unknown>) {
  const operation = stringValue(output.operation);
  const replacedCount = typeof output.replaced_count === 'number' ? `，处理 ${output.replaced_count} 处` : '';
  const bytesWritten = typeof output.bytes_written === 'number' ? `，写入 ${output.bytes_written} 字节` : '';
  if (operation) {
    return `${operation}${replacedCount}`;
  }
  if (bytesWritten) {
    return bytesWritten.slice(1);
  }
  return '';
}

function buildToolErrorMessage(state: ToolState, output: Record<string, unknown>) {
  const stateError = asRecord(state.error);
  return stringValue(stateError.message) || stringValue(output.error_message);
}

function getToolTone(status: string) {
  if (status === 'error') {
    return 'tool-error';
  }
  if (status === 'pending' || status === 'running') {
    return 'tool-pending';
  }
  return 'tool-ok';
}

function getToolStatus(part: MessagePart) {
  const state = asRecord(part.state) as ToolState;
  return stringValue(state.status) || 'pending';
}

function getToolGroupId(part: MessagePart) {
  const metadata = asRecord(part.metadata);
  return stringValue(metadata.execution_group);
}

function isTodoTool(tool: string) {
  return tool === 'todo_write' || tool === 'todo_read';
}

function extractQuestionItems(source: unknown): QuestionItem[] | null {
  if (!Array.isArray(source)) {
    return null;
  }
  const questions = source.flatMap((item) => {
    const record = asRecord(item);
    const id = stringValue(record.id);
    const question = stringValue(record.question);
    const rawOptions = Array.isArray(record.options) ? record.options : [];
    const options = rawOptions.flatMap((rawOption) => {
      const option = asRecord(rawOption);
      const value = stringValue(option.value);
      const label = stringValue(option.label);
      if (!value && !label) {
        return [];
      }
      return [{ value: value || label, label: label || value }];
    });
    if (!id && !question) {
      return [];
    }
    return [{ id, question, multiple: boolValue(record.multiple), options }];
  });
  return questions.length ? questions : null;
}

function isQuestionOptionSelected(answers: Record<string, unknown>, questionId: string, optionValue: string) {
  const answer = asRecord(answers[questionId]);
  const values = Array.isArray(answer.values) ? answer.values.map(String) : [];
  return values.includes(optionValue);
}

function extractTodos(tool: string, input: Record<string, unknown>, output: Record<string, unknown>) {
  const source = tool === 'todo_write' ? input.todos : output.todos ?? input.todos;
  if (!Array.isArray(source)) {
    return [];
  }
  return source.flatMap((item) => {
    const todo = asRecord(item);
    const content = stringValue(todo.content).trim();
    const status = stringValue(todo.status);
    const priority = stringValue(todo.priority);
    if (!content || !isTodoStatus(status) || !isTodoPriority(priority)) {
      return [];
    }
    return [{ content, status, priority }];
  });
}

function summarizeTodos(todos: TodoItem[]) {
  return {
    total: todos.length,
    pending: todos.filter((todo) => todo.status === 'pending').length,
    inProgress: todos.filter((todo) => todo.status === 'in_progress').length,
    completed: todos.filter((todo) => todo.status === 'completed').length,
  };
}

function isTodoStatus(value: string): value is TodoItem['status'] {
  return value === 'pending' || value === 'in_progress' || value === 'completed';
}

function isTodoPriority(value: string): value is TodoItem['priority'] {
  return value === 'low' || value === 'medium' || value === 'high';
}

function formatTodoPriority(priority: TodoItem['priority']) {
  if (priority === 'high') {
    return '高';
  }
  if (priority === 'medium') {
    return '中';
  }
  return '低';
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value : '';
}

function boolValue(value: unknown) {
  return value === true;
}

function parseUnifiedDiff(value: string): DiffFile[] {
  const lines = value.split('\n').map((line) => line.replace(/\r$/, ''));
  if (lines[lines.length - 1] === '') {
    lines.pop();
  }
  const files: DiffFile[] = [];
  let oldLine = 0;
  let newLine = 0;
  let hasHunk = false;

  const startFile = (oldPath: string, newPath: string) => {
    files.push({ oldPath: cleanDiffPath(oldPath), newPath: cleanDiffPath(newPath), lines: [], additions: 0, deletions: 0 });
  };
  const currentFile = () => files[files.length - 1] || null;
  const ensureFile = () => {
    const file = currentFile();
    if (file) {
      return file;
    }
    startFile('', '');
    return currentFile();
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const nextLine = lines[index + 1] || '';
    // 只把标准 unified diff 的文件头和 hunk 当作结构信号，避免误解析普通输出。
    if (line.startsWith('--- ') && nextLine.startsWith('+++ ')) {
      startFile(line.slice(4), nextLine.slice(4));
      index += 1;
      continue;
    }

    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line);
    if (hunk) {
      const file = ensureFile();
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      hasHunk = true;
      file?.lines.push({ kind: 'hunk', content: line, oldLine: null, newLine: null });
      continue;
    }

    const current = currentFile();
    if (!current) {
      continue;
    }

    if (line.startsWith('\\ ')) {
      current.lines.push({ kind: 'meta', content: line, oldLine: null, newLine: null });
      continue;
    }
    if (line.startsWith('+')) {
      current.additions += 1;
      current.lines.push({ kind: 'add', content: line.slice(1), oldLine: null, newLine });
      newLine += 1;
      continue;
    }
    if (line.startsWith('-')) {
      current.deletions += 1;
      current.lines.push({ kind: 'delete', content: line.slice(1), oldLine, newLine: null });
      oldLine += 1;
      continue;
    }

    const content = line.startsWith(' ') ? line.slice(1) : line;
    current.lines.push({ kind: 'context', content, oldLine, newLine });
    oldLine += 1;
    newLine += 1;
  }

  return hasHunk ? files.filter((file) => file.lines.length > 0) : [];
}

function cleanDiffPath(value: string) {
  const path = value.split('\t')[0]?.trim() || '';
  if (path === '/dev/null') {
    return path;
  }
  if (path.startsWith('a/') || path.startsWith('b/')) {
    return path.slice(2);
  }
  return path;
}

async function fileToPendingAttachment(file: File): Promise<PendingAttachment> {
  const supported = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  if (!supported.has(file.type)) {
    throw new Error('当前仅支持 png/jpeg/webp/gif 图片。');
  }
  if (file.size > 5 * 1024 * 1024) {
    throw new Error('单张图片不能超过 5MB。');
  }
  const dataUrl = await readFileAsDataUrl(file);
  const marker = ';base64,';
  const markerIndex = dataUrl.indexOf(marker);
  if (markerIndex < 0) {
    throw new Error('图片编码失败。');
  }
  return {
    id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(16).slice(2)}`,
    filename: file.name,
    mime: file.type,
    data_base64: dataUrl.slice(markerIndex + marker.length),
    previewUrl: URL.createObjectURL(file),
    size: file.size,
  };
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('读取图片失败'));
    reader.readAsDataURL(file);
  });
}

function formatBytes(value: number) {
  if (value < 1024) {
    return `${value}B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)}KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)}MB`;
}

function numberValue(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function summarizeTokenUsage(messages: MessageRecord[]): TokenSummary {
  return messages.reduce<TokenSummary>(
    (summary, message) => {
      if (message.info?.role !== 'assistant') {
        return summary;
      }
      const tokens = message.info.tokens;
      if (!tokens) {
        return summary;
      }
      return {
        input: summary.input + numberValue(tokens.input),
        output: summary.output + numberValue(tokens.output),
        reasoning: summary.reasoning + numberValue(tokens.reasoning),
        cacheRead: summary.cacheRead + numberValue(tokens.cache?.read),
        cacheWrite: summary.cacheWrite + numberValue(tokens.cache?.write),
      };
    },
    { input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0 },
  );
}

function formatNumber(value: number) {
  return new Intl.NumberFormat('en-US').format(value);
}

function formatPercent(value: number) {
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)}%`;
}

function totalTokensForUsage(tokens: TokenUsage) {
  return numberValue(tokens.input) + numberValue(tokens.output) + numberValue(tokens.reasoning);
}

function formatTokenUsage(tokens: TokenUsage) {
  const total = totalTokensForUsage(tokens);
  const cacheRead = numberValue(tokens.cache?.read);
  const reasoning = numberValue(tokens.reasoning);
  const details = [
    reasoning > 0 ? `reason ${formatNumber(reasoning)}` : '',
    cacheRead > 0 ? `cache ${formatNumber(cacheRead)}` : '',
  ].filter(Boolean);
  return details.length ? `${formatNumber(total)} tokens · ${details.join(' · ')}` : `${formatNumber(total)} tokens`;
}

function jsonPretty(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function getThinkingCapability(provider: ProviderOption | null, model: string): ThinkingCapability | null {
  return provider?.model_capabilities?.[model]?.thinking || null;
}

function resolveNextThinkingValue(provider: ProviderOption | null, model: string, currentValue: string) {
  const capability = getThinkingCapability(provider, model);
  if (!capability) {
    return '';
  }
  return capability.allowed_values.includes(currentValue) ? currentValue : capability.default_value;
}

function formatThinkingValue(value: string) {
  const labels: Record<string, string> = {
    none: 'none',
    minimal: 'minimal',
    low: 'low',
    medium: 'medium',
    high: 'high',
    xhigh: 'xhigh',
    on: '开启',
    off: '关闭',
  };
  return labels[value] || value;
}

function upsertMessage(prev: MessageRecord[], next: MessageRecord): MessageRecord[] {
  const nextId = next.info?.id;
  if (!nextId) {
    return [...prev, next];
  }
  const index = prev.findIndex((item) => item.info?.id === nextId);
  if (index < 0) {
    return [...prev, next];
  }
  const copy = [...prev];
  copy[index] = next;
  return copy;
}

function upsertRunningToolMessage(prev: MessageRecord[], event: StreamEvent): MessageRecord[] {
  const data = event.data || {};
  const toolName = stringValue(data.tool_name);
  const callId = stringValue(data.tool_call_id);
  if (!toolName || !callId) {
    return prev;
  }
  const messageId = buildPendingToolMessageId(data, event.session_id);
  const part = buildRunningToolPart(toolName, callId, asRecord(data.args), event.created_at);
  const messageIndex = prev.findIndex((item) => item.info?.id === messageId);
  if (messageIndex < 0) {
    return [...prev, buildPendingToolMessage(messageId, event)];
  }

  const message = prev[messageIndex];
  const parts = message.parts || [];
  const partIndex = parts.findIndex((item) => item.type === 'tool' && stringValue(item.call_id) === callId);
  const nextParts = [...parts];
  if (partIndex >= 0) {
    nextParts[partIndex] = part;
  } else {
    nextParts.push(part);
  }
  const copy = [...prev];
  copy[messageIndex] = { ...message, parts: nextParts };
  return copy;
}

function removePendingToolMessagesForAssistant(prev: MessageRecord[], message: MessageRecord): MessageRecord[] {
  if (message.info?.role !== 'assistant') {
    return prev;
  }
  const messageId = buildPendingToolMessageId(message.info, message.info.session_id);
  return prev.filter((item) => item.info?.id !== messageId);
}

function buildPendingToolMessage(messageId: string, event: StreamEvent): MessageRecord {
  const data = event.data || {};
  const createdAt = Date.parse(event.created_at);
  const toolName = stringValue(data.tool_name);
  const callId = stringValue(data.tool_call_id);

  return {
    info: {
      id: messageId,
      role: 'assistant',
      session_id: event.session_id || '',
      time: { created: Number.isFinite(createdAt) ? createdAt : Date.now() },
      agent: stringValue(data.agent),
      agent_kind: stringValue(data.agent_kind) || 'agent',
      context_id: stringValue(data.context_id) || 'main',
      parent_call_id: stringValue(data.parent_call_id) || null,
    },
    parts: [buildRunningToolPart(toolName, callId, asRecord(data.args), event.created_at)],
  };
}

function buildRunningToolPart(toolName: string, callId: string, args: Record<string, unknown>, createdAt: string): MessagePart {
  return {
    type: 'tool',
    call_id: callId,
    tool: toolName,
    state: {
      status: 'running',
      input: args,
      raw: JSON.stringify(args, null, 2),
      time: { start: createdAt },
    },
    metadata: { temporary: true },
  };
}

function buildPendingToolMessageId(data: Record<string, unknown>, sessionId?: string | null) {
  const agentKind = stringValue(data.agent_kind) || 'agent';
  const contextId = stringValue(data.context_id) || 'main';
  const parentCallId = stringValue(data.parent_call_id);
  return `${PENDING_TOOL_MESSAGE_PREFIX}${sessionId || 'session'}:${agentKind}:${contextId}:${parentCallId}`;
}

function summarizeEventStats(events: StreamEvent[]): EventStats {
  return events.reduce<EventStats>(
    (stats, event) => ({
      tools: stats.tools + (event.event_type === 'tool_call_finished' || event.event_type === 'tool_call_failed' ? 1 : 0),
      failed: stats.failed + (event.event_type === 'tool_call_failed' || event.event_type === 'session_failed' || event.event_type === 'error' ? 1 : 0),
      blockers: stats.blockers + (event.event_type === 'human_approval_required' || event.event_type === 'human_question_required' ? 1 : 0),
      compacted: stats.compacted + (event.event_type === 'context_compacted' ? 1 : 0),
      latest: event,
    }),
    { tools: 0, failed: 0, blockers: 0, compacted: 0, latest: null },
  );
}

function buildEventViewModel(event: StreamEvent): EventViewModel {
  const data = event.data || {};
  const agent = stringValue(data.agent);
  const agentKind = stringValue(data.agent_kind);
  const chips = [
    agentKind === 'subagent' ? 'subagent' : agent,
    stringValue(data.tool_name),
    stringValue(data.status),
  ].filter(Boolean);
  const base = {
    chips: Array.from(new Set(chips)).slice(0, 3),
    time: formatIsoTime(event.created_at),
  };

  if (event.event_type === 'tool_call_started') {
    const args = asRecord(data.args);
    const toolName = stringValue(data.tool_name) || '工具';
    const display = buildToolDisplay(toolName, args, {});
    return {
      ...base,
      title: display.title,
      summary: summarizeToolInput(args) || '正在执行，等待返回结果。',
      tone: 'running',
      icon: <Play size={14} />,
    };
  }

  if (event.event_type === 'tool_call_finished' || event.event_type === 'tool_call_failed') {
    const result = asRecord(data.result);
    const args = asRecord(data.args);
    const toolName = stringValue(data.tool_name) || stringValue(result.tool_name) || '工具';
    const failed = event.event_type === 'tool_call_failed';
    const display = buildToolDisplay(toolName, args, result);
    return {
      ...base,
      title: display.title,
      summary: summarizeToolResult(result, failed),
      tone: failed ? 'danger' : 'ok',
      icon: failed ? <AlertTriangle size={14} /> : <Check size={14} />,
      chips: addEventChips(base.chips, [
        typeof result.duration_ms === 'number' ? `${result.duration_ms}ms` : '',
        typeof result.exit_code === 'number' ? `exit ${result.exit_code}` : '',
      ]),
    };
  }

  if (event.event_type === 'human_approval_required') {
    return {
      ...base,
      title: '等待人工审批',
      summary: stringValue(data.reason) || '工具执行需要人工确认。',
      tone: 'warn',
      icon: <ShieldCheck size={14} />,
      chips: addEventChips(base.chips, [stringValue(data.approval_id)]),
    };
  }

  if (event.event_type === 'human_approval_resolved') {
    const approved = boolValue(data.approved);
    return {
      ...base,
      title: approved ? '审批已同意' : '审批已拒绝',
      summary: stringValue(data.comment) || (approved ? '会话继续执行。' : '会话将按拒绝结果处理。'),
      tone: approved ? 'ok' : 'danger',
      icon: approved ? <Check size={14} /> : <X size={14} />,
      chips: addEventChips(base.chips, [stringValue(data.approval_id)]),
    };
  }

  if (event.event_type === 'human_question_required') {
    const questions = Array.isArray(data.questions) ? data.questions.length : 0;
    return {
      ...base,
      title: '等待用户回答',
      summary: questions > 0 ? `Agent 需要 ${questions} 个问题的答案。` : 'Agent 需要用户补充信息。',
      tone: 'warn',
      icon: <MessageSquareText size={14} />,
      chips: addEventChips(base.chips, [stringValue(data.question_id)]),
    };
  }

  if (event.event_type === 'human_question_resolved') {
    const declined = boolValue(data.declined);
    return {
      ...base,
      title: declined ? '用户跳过回答' : '用户已回答',
      summary: declined ? '会话收到跳过信号。' : '会话收到用户答案，继续推进。',
      tone: declined ? 'warn' : 'ok',
      icon: declined ? <X size={14} /> : <Check size={14} />,
      chips: addEventChips(base.chips, [stringValue(data.question_id)]),
    };
  }

  if (event.event_type === 'context_compacted') {
    const before = numberValue(data.before_tokens);
    const after = numberValue(data.after_tokens);
    const ratio = before > 0 && after > 0 ? `，压缩 ${formatPercent((1 - after / before) * 100)}` : '';
    return {
      ...base,
      title: '上下文已压缩',
      summary: before > 0 || after > 0 ? `${formatNumber(before)} -> ${formatNumber(after)} tokens${ratio}` : '会话上下文已压缩。',
      tone: 'running',
      icon: <FileText size={14} />,
    };
  }

  if (event.event_type === 'session_started') {
    return {
      ...base,
      title: '会话已启动',
      summary: summarizeSessionEvent(data) || '新的 Agent 会话开始运行。',
      tone: 'running',
      icon: <Radio size={14} />,
    };
  }

  if (event.event_type === 'session_finished') {
    return {
      ...base,
      title: '会话已完成',
      summary: summarizeSessionEvent(data) || '本轮任务已结束。',
      tone: 'ok',
      icon: <Check size={14} />,
    };
  }

  if (event.event_type === 'session_failed' || event.event_type === 'error') {
    return {
      ...base,
      title: event.event_type === 'error' ? '运行错误' : '会话失败',
      summary: stringValue(data.message) || stringValue(data.error) || summarizeSessionEvent(data) || '运行过程中发生错误。',
      tone: 'danger',
      icon: <OctagonAlert size={14} />,
    };
  }

  if (event.event_type === 'session_status_changed') {
    return {
      ...base,
      title: '状态已变化',
      summary: stringValue(data.status) || '会话状态发生变化。',
      tone: eventToneFromStatus(stringValue(data.status)),
      icon: <CircleDot size={14} />,
    };
  }

  if (event.event_type === 'session_title_updated') {
    return {
      ...base,
      title: '标题已更新',
      summary: stringValue(data.title) || '会话标题已更新。',
      tone: 'neutral',
      icon: <FileText size={14} />,
    };
  }

  return {
    ...base,
    title: EVENT_LABELS[event.event_type] || event.event_type,
    summary: buildEventSummary(event),
    tone: eventToneFromType(event.event_type),
    icon: <CircleDot size={14} />,
  };
}

function addEventChips(current: string[], extra: string[]) {
  return Array.from(new Set([...current, ...extra.filter(Boolean)])).slice(0, 4);
}

function summarizeToolInput(input: Record<string, unknown>) {
  return (
    stringValue(input.description) ||
    stringValue(input.name) ||
    stringValue(input.url) ||
    stringValue(input.command) ||
    stringValue(input.file_path) ||
    stringValue(input.path) ||
    stringValue(input.task) ||
    summarizeRecordKeys(input)
  );
}

function summarizeToolResult(result: Record<string, unknown>, failed: boolean) {
  const errorText = firstLine(stringValue(result.error_message) || stringValue(result.stderr));
  if (failed && errorText) {
    return errorText;
  }
  const operation = buildToolOperation(result);
  const output = firstLine(stringValue(result.output) || stringValue(result.stdout));
  const filePath = stringValue(result.file_path);
  if (operation && filePath) {
    return `${operation} · ${filePath}`;
  }
  return operation || output || filePath || (failed ? '工具返回错误结果。' : '工具执行成功。');
}

function summarizeSessionEvent(data: Record<string, unknown>) {
  return [stringValue(data.agent_name), stringValue(data.provider), stringValue(data.model), stringValue(data.status)]
    .filter(Boolean)
    .join(' · ');
}

function summarizeRecordKeys(record: Record<string, unknown>) {
  const keys = Object.keys(record).slice(0, 3);
  return keys.length ? `参数：${keys.join(', ')}` : '';
}

function firstLine(value: string) {
  return value.split(/\r?\n/).find((line) => line.trim())?.trim() || '';
}

function eventToneFromStatus(status: string): EventTone {
  const normalized = status.toLowerCase();
  if (normalized.includes('fail') || normalized.includes('error') || normalized.includes('cancel')) {
    return 'danger';
  }
  if (normalized.includes('wait') || normalized.includes('approval')) {
    return 'warn';
  }
  if (normalized.includes('finish') || normalized.includes('complete')) {
    return 'ok';
  }
  if (normalized.includes('run') || normalized.includes('start')) {
    return 'running';
  }
  return 'neutral';
}

function eventToneFromType(eventType: string): EventTone {
  if (eventType.includes('failed') || eventType === 'error') {
    return 'danger';
  }
  if (eventType.includes('approval') || eventType.includes('question')) {
    return 'warn';
  }
  if (eventType.includes('finished') || eventType.includes('completed')) {
    return 'ok';
  }
  if (eventType.includes('started')) {
    return 'running';
  }
  return 'neutral';
}

function getStatusClass(status: string) {
  const normalized = status.toLowerCase();
  if (normalized.includes('fail') || normalized.includes('error')) {
    return 'status-error';
  }
  if (normalized.includes('wait') || normalized.includes('approval')) {
    return 'status-waiting';
  }
  if (normalized.includes('run') || normalized.includes('start')) {
    return 'status-running';
  }
  if (normalized.includes('finish') || normalized.includes('complete')) {
    return 'status-finished';
  }
  return 'status-idle';
}

function getScheduleRunClass(status: string) {
  if (status === 'completed') {
    return 'is-ok';
  }
  if (status === 'failed' || status === 'timeout' || status === 'interrupted') {
    return 'is-danger';
  }
  if (status === 'running') {
    return 'is-running';
  }
  if (status === 'pending') {
    return 'is-waiting';
  }
  return 'is-muted';
}

function formatScheduleTrigger(trigger: ScheduleTrigger) {
  if (trigger.kind === 'once') {
    return `单次 ${trigger.run_at ? formatSessionTime(trigger.run_at) : '-'}`;
  }
  if (trigger.kind === 'interval') {
    return `每 ${trigger.interval_seconds || '-'} 秒`;
  }
  if (trigger.kind === 'weekly') {
    const weekday = WEEKDAY_OPTIONS.find((item) => item.value === String(trigger.day_of_week))?.label || '-';
    return `每${weekday} ${trigger.time_of_day || '-'}${trigger.timezone ? ` ${trigger.timezone}` : ''}`;
  }
  return `每日 ${trigger.time_of_day || '-'}${trigger.timezone ? ` ${trigger.timezone}` : ''}`;
}

function dedupeRuns(runs: ScheduleRun[]) {
  const seen = new Set<string>();
  const result: ScheduleRun[] = [];
  for (const run of runs) {
    if (seen.has(run.id)) {
      continue;
    }
    seen.add(run.id);
    result.push(run);
  }
  return result;
}

function getEventTone(eventType: string) {
  if (eventType.includes('failed') || eventType === 'error') {
    return 'tone-danger';
  }
  if (eventType.includes('approval')) {
    return 'tone-warn';
  }
  if (eventType.includes('finished') || eventType.includes('completed')) {
    return 'tone-ok';
  }
  return 'tone-neutral';
}

function buildEventSummary(event: StreamEvent) {
  const agentPrefix = event.data.agent ? `${String(event.data.agent)} · ` : '';
  if (event.event_type === 'llm_delta') {
    const text = String(event.data.text || '');
    return `${agentPrefix}${text ? text.slice(0, 48) : 'delta'}`;
  }
  if (event.data.status) {
    return `${agentPrefix}${String(event.data.status)}`;
  }
  if (event.data.reason) {
    return `${agentPrefix}${String(event.data.reason).slice(0, 48)}`;
  }
  if (event.event_type === 'context_compacted') {
    const beforeTokens = event.data.before_tokens ?? '-';
    const afterTokens = event.data.after_tokens ?? '-';
    return `${beforeTokens} -> ${afterTokens} tokens`;
  }
  if (event.data.message && typeof event.data.message === 'object') {
    const message = event.data.message as MessageRecord;
    return `${agentPrefix}${String(message.info?.role || 'message')}`;
  }
  return `${agentPrefix}${event.session_id || event.created_at}`;
}

function formatTime(timestamp: number) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return String(timestamp);
  }
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatSessionTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || '-';
  }
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatIsoTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || '-';
  }
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function isTextEntryTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest('input, textarea, select, [contenteditable="true"]'));
}

function getReplaySessionId(replay: ReplayResponse) {
  const sessionData = replay.session?.data;
  if (!sessionData || typeof sessionData !== 'object') {
    return null;
  }
  const sessionId = (sessionData as Record<string, unknown>).session_id;
  return typeof sessionId === 'string' ? sessionId : null;
}

function detectComposerAutocomplete(value: string, caret: number): ComposerAutocomplete | null {
  const beforeCaret = value.slice(0, caret);
  const match = /(^|\s)([$@])([^\s$@]*)$/.exec(beforeCaret);
  if (!match) {
    return null;
  }
  const prefix = match[1] || '';
  const trigger = match[2] as '$' | '@';
  const query = match[3] || '';
  const start = caret - query.length - 1;
  if (start > 0 && !/\s/.test(prefix)) {
    return null;
  }
  return {
    kind: trigger === '$' ? 'skill' : 'file',
    trigger,
    start,
    end: caret,
    query,
    activeIndex: 0,
  };
}

function buildSkillComposerOptions(skills: SkillOption[], query: string): ComposerOption[] {
  const normalizedQuery = query.trim().toLowerCase();
  return skills
    .filter((skill) => {
      if (!normalizedQuery) {
        return true;
      }
      return skill.name.toLowerCase().includes(normalizedQuery) || skill.description.toLowerCase().includes(normalizedQuery);
    })
    .slice(0, 20)
    .map((skill) => ({ value: skill.name, label: skill.name, description: skill.description }));
}

function translateSkillShortcuts(value: string, skills: SkillOption[]) {
  if (!skills.length) {
    return value;
  }
  const skillNames = new Map(skills.map((skill) => [skill.name.toLowerCase(), skill.name]));
  return value.replace(/(^|\s)\$([A-Za-z0-9_.-]+)/g, (raw, prefix: string, name: string) => {
    const skillName = skillNames.get(name.toLowerCase());
    if (!skillName) {
      return raw;
    }
    return `${prefix}${skillName} skill`;
  });
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    throw new Error(`请求失败: ${url}`);
  }
  return response.json() as Promise<T>;
}

async function requestJson<T>(url: string, { method, body }: { method: string; body?: unknown }): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `请求失败: ${url}`);
  }
  return response.json() as Promise<T>;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  return requestJson<T>(url, { method: 'POST', body });
}

export default App;
