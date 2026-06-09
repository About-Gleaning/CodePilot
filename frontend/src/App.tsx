import { FormEvent, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  AlertTriangle,
  Bot,
  Brain,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleDot,
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
  Sparkles,
  Square,
  Terminal,
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
  sse: {
    heartbeat_seconds: number;
    replay_on_connect: boolean;
  };
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
};

type ReplayResponse = {
  session: Record<string, unknown> | null;
  messages: MessageRecord[];
  records: Array<Record<string, unknown>>;
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
};

type SessionsResponse = {
  sessions: SessionSummary[];
};

type LoadSessionResponse = ReplayResponse & {
  ok: boolean;
  session: Record<string, unknown> | null;
};

type StreamEvent = {
  seq: number;
  event_id: string;
  event_type: string;
  session_id: string | null;
  created_at: string;
  data: Record<string, unknown>;
};

type ApprovalRequest = {
  approval_id: string;
  reason: string;
  action?: Record<string, unknown>;
};

type QuestionOption = {
  value: string;
  label: string;
};

type QuestionItem = {
  id: string;
  question: string;
  multiple?: boolean;
  options: QuestionOption[];
};

type QuestionRequest = {
  question_id: string;
  questions: QuestionItem[];
};

type QuestionAnswer = {
  values: string[];
  note: string;
};

type SelectOption = {
  value: string;
  label: string;
};

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

type MessageRecord = {
  info?: {
    id?: string;
    role?: string;
    time?: {
      created?: number;
      completed?: number;
    };
    agent?: string;
    agent_kind?: string;
    context_id?: string | null;
    parent_call_id?: string | null;
    parent_id?: string;
    model?: {
      provider_id?: string;
      model_id?: string;
    };
    tokens?: TokenUsage | null;
    path?: {
      cwd?: string;
      root?: string;
    };
  };
  parts?: MessagePart[];
};

type MessagePart = Record<string, unknown> & {
  type?: string;
};

type TokenUsage = {
  input?: number | null;
  output?: number | null;
  reasoning?: number | null;
  cache?: {
    read?: number | null;
    write?: number | null;
  } | null;
};

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

type MobileConsoleProps = {
  config: ConfigResponse | null;
  status: StatusResponse | null;
  currentSessionId: string | null;
  provider: string;
  model: string;
  agentName: string;
  task: string;
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
  questionOptionsRef: React.RefObject<HTMLDivElement>;
  questionNoteRef: React.RefObject<HTMLTextAreaElement>;
  onAgentChange: (nextValue: string) => void;
  onProviderChange: (nextValue: string) => void;
  onModelChange: (nextValue: string) => void;
  onThinkingChange: (nextValue: string) => void;
  onTaskChange: (nextValue: string) => void;
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
  const [formError, setFormError] = useState('');
  const [approvalComment, setApprovalComment] = useState('');
  const [approvalRequest, setApprovalRequest] = useState<ApprovalRequest | null>(null);
  const [questionRequest, setQuestionRequest] = useState<QuestionRequest | null>(null);
  const [questionAnswers, setQuestionAnswers] = useState<Record<string, QuestionAnswer>>({});
  const [activeQuestionIndex, setActiveQuestionIndex] = useState(0);
  const [activeQuestionOptionIndex, setActiveQuestionOptionIndex] = useState(0);
  const [questionError, setQuestionError] = useState('');
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [sessionHistory, setSessionHistory] = useState<SessionSummary[]>([]);
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
  const eventSourceRef = useRef<EventSource | null>(null);
  const streamReconnectTimerRef = useRef<number | null>(null);
  const questionPanelRef = useRef<HTMLElement | null>(null);
  const questionOptionsRef = useRef<HTMLDivElement | null>(null);
  const questionNoteRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    void bootstrap();
    return () => {
      clearStreamReconnectTimer();
      eventSourceRef.current?.close();
    };
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
      setMessages(statusRes.session_id && getReplaySessionId(replayRes) === statusRes.session_id ? replayRes.messages : []);
      setSubagentLiveDeltas({});
      setSubagentLiveReasoningDeltas({});
      setSessionHistory(sessionsRes.sessions);
      connectStream(0);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '初始化失败');
    }
  }

  function connectStream(afterSeq: number) {
    clearStreamReconnectTimer();
    eventSourceRef.current?.close();
    const source = new EventSource(`/api/session/stream?after_seq=${afterSeq}`);
    source.onmessage = (event) => {
      const payload: StreamEvent = JSON.parse(event.data);
      onStreamEvent(payload);
    };
    Object.keys(EVENT_LABELS).forEach((eventType) => {
      source.addEventListener(eventType, (event) => {
        const payload: StreamEvent = JSON.parse((event as MessageEvent).data);
        onStreamEvent(payload);
      });
    });
    source.onerror = () => {
      if (eventSourceRef.current !== source) {
        return;
      }
      source.close();
      streamReconnectTimerRef.current = window.setTimeout(() => connectStream(lastSeqRef.current), 1200);
    };
    eventSourceRef.current = source;
  }

  function clearStreamReconnectTimer() {
    if (streamReconnectTimerRef.current === null) {
      return;
    }
    window.clearTimeout(streamReconnectTimerRef.current);
    streamReconnectTimerRef.current = null;
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
        setMessages((prev) => upsertMessage(prev, message));
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
      const request = event.data as QuestionRequest;
      setQuestionRequest(request);
      setQuestionAnswers(initQuestionAnswers(request));
      setActiveQuestionIndex(0);
      setActiveQuestionOptionIndex(0);
      setQuestionError('');
    }
    if (event.event_type === 'human_question_resolved') {
      setQuestionRequest(null);
      setQuestionAnswers({});
      setActiveQuestionIndex(0);
      setActiveQuestionOptionIndex(0);
      setQuestionError('');
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

  async function handleStart(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError('');
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
        content: task,
        agent_name: agentName,
        provider,
        model,
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
      await refreshStatus();
      await refreshSessionHistory();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '提交任务失败');
    }
  }

  function handleNewTask() {
    clearStreamReconnectTimer();
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
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
    setQuestionRequest(null);
    setQuestionAnswers({});
    setActiveQuestionIndex(0);
    setActiveQuestionOptionIndex(0);
    setQuestionError('');
    setFormError('');
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
      setQuestionRequest(null);
      setQuestionAnswers({});
      setActiveQuestionIndex(0);
      setActiveQuestionOptionIndex(0);
      setQuestionError('');
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

  function initQuestionAnswers(request: QuestionRequest): Record<string, QuestionAnswer> {
    return Object.fromEntries(request.questions.map((question) => [question.id, { values: [], note: '' }]));
  }

  function updateQuestionChoice(question: QuestionItem, value: string, checked: boolean) {
    setQuestionAnswers((prev) => {
      const current = prev[question.id] || { values: [], note: '' };
      const values = question.multiple
        ? checked
          ? Array.from(new Set([...current.values, value]))
          : current.values.filter((item) => item !== value)
        : [value];
      return { ...prev, [question.id]: { ...current, values } };
    });
    setQuestionError('');
  }

  function updateQuestionNote(questionId: string, note: string) {
    setQuestionAnswers((prev) => {
      const current = prev[questionId] || { values: [], note: '' };
      return { ...prev, [questionId]: { ...current, note } };
    });
  }

  function focusQuestionOptions() {
    window.requestAnimationFrame(() => questionOptionsRef.current?.focus());
  }

  function focusQuestionNote() {
    window.requestAnimationFrame(() => questionNoteRef.current?.focus());
  }

  function moveActiveQuestion(step: number) {
    if (!questionRequest) {
      return;
    }
    setActiveQuestionIndex((prev) => Math.min(Math.max(prev + step, 0), questionRequest.questions.length - 1));
    setActiveQuestionOptionIndex(0);
    setQuestionError('');
    focusQuestionOptions();
  }

  function handleQuestionOptionsKeyDown(event: React.KeyboardEvent<HTMLDivElement>, question: QuestionItem) {
    if (question.options.length === 0) {
      return;
    }
    const lastIndex = question.options.length - 1;
    const chooseIndex = (nextIndex: number, shouldSelect: boolean) => {
      const boundedIndex = Math.min(Math.max(nextIndex, 0), lastIndex);
      setActiveQuestionOptionIndex(boundedIndex);
      const option = question.options[boundedIndex];
      if (option && shouldSelect) {
        updateQuestionChoice(question, option.value, true);
      }
    };

    if (event.key === 'Tab') {
      event.preventDefault();
      focusQuestionNote();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      chooseIndex(activeQuestionOptionIndex >= lastIndex ? 0 : activeQuestionOptionIndex + 1, !question.multiple);
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      chooseIndex(activeQuestionOptionIndex <= 0 ? lastIndex : activeQuestionOptionIndex - 1, !question.multiple);
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      chooseIndex(0, !question.multiple);
      return;
    }
    if (event.key === 'End') {
      event.preventDefault();
      chooseIndex(lastIndex, !question.multiple);
      return;
    }
    if (event.key === ' ' || event.key === 'Enter') {
      event.preventDefault();
      const option = question.options[activeQuestionOptionIndex];
      const current = questionAnswers[question.id] || { values: [], note: '' };
      if (!option) {
        return;
      }
      if (event.key === ' ') {
        updateQuestionChoice(question, option.value, question.multiple ? !current.values.includes(option.value) : true);
        return;
      }
      if (current.values.length === 0) {
        updateQuestionChoice(question, option.value, true);
        return;
      }
      void confirmActiveQuestion();
    }
  }

  function handleQuestionNoteKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Tab') {
      return;
    }
    event.preventDefault();
    focusQuestionOptions();
  }

  async function confirmActiveQuestion() {
    if (!questionRequest) {
      return;
    }
    const question = questionRequest.questions[activeQuestionIndex];
    if (!question) {
      return;
    }
    const answer = questionAnswers[question.id] || { values: [], note: '' };
    if (answer.values.length === 0) {
      setQuestionError('请先选择一个选项。');
      return;
    }
    setQuestionError('');
    if (activeQuestionIndex < questionRequest.questions.length - 1) {
      setActiveQuestionIndex((prev) => prev + 1);
      setActiveQuestionOptionIndex(0);
      focusQuestionOptions();
      return;
    }
    await handleQuestionReply();
  }

  async function handleQuestionReply() {
    if (!questionRequest) {
      return;
    }
    setFormError('');
    try {
      await postJson('/api/session/input', {
        type: 'question_reply',
        question_id: questionRequest.question_id,
        answers: questionAnswers,
      });
      await refreshStatus();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '提交回答失败');
    }
  }

  async function handleQuestionDecline() {
    if (!questionRequest) {
      return;
    }
    setFormError('');
    try {
      await postJson('/api/session/input', {
        type: 'question_decline',
        question_id: questionRequest.question_id,
      });
      setQuestionRequest(null);
      setQuestionAnswers({});
      setActiveQuestionIndex(0);
      setActiveQuestionOptionIndex(0);
      setQuestionError('');
      await refreshStatus();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '退出回答失败');
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
        questionOptionsRef={questionOptionsRef}
        questionNoteRef={questionNoteRef}
        onAgentChange={setAgentName}
        onProviderChange={handleProviderChange}
        onModelChange={handleModelChange}
        onThinkingChange={setThinkingValue}
        onTaskChange={setTask}
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
            <span className="eyebrow">interactive session</span>
            <h2>消息流</h2>
            <p className="stage-subtitle">实时观察 Agent 输出、工具执行和人工审批状态。</p>
          </div>
        </header>

        {formError ? (
          <section className="error-strip" role="alert">
            <OctagonAlert size={16} />
            <span>{formError}</span>
          </section>
        ) : null}

        <section className="message-viewport" aria-label="会话消息">
          {messages.length === 0 && !liveDelta && !liveReasoningDelta ? (
            <div className="empty-state">
              <Sparkles size={20} />
              <p>选择 Agent、Provider 与 Model 后，在底部输入任务开始会话。</p>
            </div>
          ) : null}

          {messages.map((message, index) => (
            <MessageItem key={String(message.info?.id || index)} message={message} index={index} />
          ))}

          {liveDelta || liveReasoningDelta ? (
            <article className="message-card assistant streaming-card">
              <div className="message-meta">
                <span className="role-badge assistant">
                  <Bot size={13} />
                  assistant
                </span>
                <span className="muted-inline">streaming</span>
              </div>
              <LiveReasoningBlock text={liveReasoningDelta} />
              {liveDelta ? <MarkdownContent className="message-live-text" text={liveDelta} /> : null}
            </article>
          ) : null}
          {Array.from(new Set([...Object.keys(subagentLiveDeltas), ...Object.keys(subagentLiveReasoningDeltas)])).map((key) => (
            <article className="message-card assistant streaming-card subagent-card" key={key}>
              <div className="message-meta">
                <span className="role-badge assistant">
                  <Bot size={13} />
                  subagent
                </span>
                <span className="muted-inline">{key}</span>
              </div>
              <LiveReasoningBlock text={subagentLiveReasoningDeltas[key] || ''} />
              {subagentLiveDeltas[key] ? <MarkdownContent className="message-live-text" text={subagentLiveDeltas[key]} /> : null}
            </article>
          ))}
        </section>

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
            <textarea
              rows={4}
              placeholder="输入任务。若要触发人工审批，可在文本中加入 [[approve]]。"
              value={task}
              onChange={(e) => setTask(e.target.value)}
            />
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
        <section className="panel event-panel">
          <PanelTitle icon={<ListTree size={16} />} title="任务雷达" badge={`${events.length}/200`} />
          <EventRadarHeader stats={eventStats} latestEvent={latestEventView} />
          <div className="event-list">
            {events.length === 0 ? (
              <p className="quiet-copy">等待关键事件。</p>
            ) : (
              events
                .slice()
                .reverse()
                .map((event) => <EventItem key={event.seq} event={event} />)
            )}
          </div>
        </section>
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
  const hasHumanAction = Boolean(props.approvalRequest || props.questionRequest);
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

      <main className="mobile-content">
        {activeTab === 'messages' ? (
          <MobileMessagePanel
            messages={props.messages}
            liveDelta={props.liveDelta}
            liveReasoningDelta={props.liveReasoningDelta}
            subagentLiveDeltas={props.subagentLiveDeltas}
            subagentLiveReasoningDeltas={props.subagentLiveReasoningDeltas}
          />
        ) : null}

        {activeTab === 'events' ? (
          <section className="mobile-panel">
            <PanelTitle icon={<ListTree size={16} />} title="任务雷达" badge={`${props.events.length}/200`} />
            <EventRadarHeader stats={props.eventStats} latestEvent={props.latestEventView} />
            <div className="event-list mobile-event-list">
              {props.events.length === 0 ? (
                <p className="quiet-copy">等待关键事件。</p>
              ) : (
                props.events
                  .slice()
                  .reverse()
                  .map((event) => <EventItem key={event.seq} event={event} />)
              )}
            </div>
          </section>
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
            <div className="mobile-input-row">
              <textarea
                rows={2}
                placeholder="输入任务..."
                value={props.task}
                onChange={(event) => props.onTaskChange(event.target.value)}
              />
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

function MobileMessagePanel({
  messages,
  liveDelta,
  liveReasoningDelta,
  subagentLiveDeltas,
  subagentLiveReasoningDeltas,
}: {
  messages: MessageRecord[];
  liveDelta: string;
  liveReasoningDelta: string;
  subagentLiveDeltas: Record<string, string>;
  subagentLiveReasoningDeltas: Record<string, string>;
}) {
  return (
    <section className="mobile-panel mobile-message-list" aria-label="会话消息">
      {messages.length === 0 && !liveDelta && !liveReasoningDelta ? (
        <div className="empty-state">
          <Sparkles size={20} />
          <p>选择 Agent、Provider 与 Model 后，在底部输入任务开始会话。</p>
        </div>
      ) : null}

      {messages.map((message, index) => (
        <MessageItem key={String(message.info?.id || index)} message={message} index={index} />
      ))}

      {liveDelta || liveReasoningDelta ? (
        <article className="message-card assistant streaming-card">
          <div className="message-meta">
            <span className="role-badge assistant">
              <Bot size={13} />
              assistant
            </span>
            <span className="muted-inline">streaming</span>
          </div>
          <LiveReasoningBlock text={liveReasoningDelta} />
          {liveDelta ? <MarkdownContent className="message-live-text" text={liveDelta} /> : null}
        </article>
      ) : null}

      {Array.from(new Set([...Object.keys(subagentLiveDeltas), ...Object.keys(subagentLiveReasoningDeltas)])).map((key) => (
        <article className="message-card assistant streaming-card subagent-card" key={key}>
          <div className="message-meta">
            <span className="role-badge assistant">
              <Bot size={13} />
              subagent
            </span>
            <span className="muted-inline">{key}</span>
          </div>
          <LiveReasoningBlock text={subagentLiveReasoningDeltas[key] || ''} />
          {subagentLiveDeltas[key] ? <MarkdownContent className="message-live-text" text={subagentLiveDeltas[key]} /> : null}
        </article>
      ))}
    </section>
  );
}

function ApprovalPanel({
  approvalRequest,
  approvalComment,
  onApprovalCommentChange,
}: {
  approvalRequest: ApprovalRequest;
  approvalComment: string;
  onApprovalCommentChange: (nextValue: string) => void;
}) {
  return (
    <section className="composer-approval mobile-approval" aria-label="人工审批">
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
        onChange={(event) => onApprovalCommentChange(event.target.value)}
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
                  <strong>{session.title || session.preview || session.session_id}</strong>
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

// 原生 select 的弹层在部分浏览器中无法稳定套用暗色主题，这里只为配置区保留轻量自绘下拉。
function ConfigSelect({
  value,
  options,
  onChange,
  disabled = false,
}: {
  value: string;
  options: SelectOption[];
  onChange: (nextValue: string) => void;
  disabled?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const selected = options.find((item) => item.value === value) || options[0];

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    function handlePointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen]);

  function handleChoose(nextValue: string) {
    onChange(nextValue);
    setIsOpen(false);
  }

  return (
    <div className={`select-shell ${isOpen ? 'is-open' : ''} ${disabled ? 'is-disabled' : ''}`} ref={rootRef}>
      <button
        type="button"
        className="select-trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        onClick={() => setIsOpen((prev) => !prev)}
      >
        <span>{selected?.label || '-'}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {isOpen ? (
        <div className="select-menu" role="listbox">
          {options.map((item) => {
            const isSelected = item.value === value;
            return (
              <button
                key={item.value || '__empty__'}
                type="button"
                className={`select-option ${isSelected ? 'is-selected' : ''}`}
                role="option"
                aria-selected={isSelected}
                onClick={() => handleChoose(item.value)}
              >
                <span>{item.label}</span>
                {isSelected ? <Check size={13} aria-hidden="true" /> : null}
              </button>
            );
          })}
        </div>
      ) : null}
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
      <div className={`token-bar ${total > 0 ? '' : 'is-empty'}`} aria-hidden="true">
        {total > 0 ? (
          parts.map((part) =>
            part.value > 0 ? (
              <span
                className={`token-segment ${part.className}`}
                key={part.key}
                style={{ width: `${(part.value / total) * 100}%` }}
              />
            ) : null,
          )
        ) : (
          <span className="token-bar-empty" />
        )}
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

function MessageItem({ message, index }: { message: MessageRecord; index: number }) {
  const role = String(message.info?.role || 'unknown');
  const isAssistant = role === 'assistant';
  const agentKind = String(message.info?.agent_kind || 'agent');
  const agentName = String(message.info?.agent || '');
  const parts = message.parts || [];
  const stepFinishParts = parts.filter((part) => part.type === 'step-finish');
  const bodyParts = stepFinishParts.length ? parts.filter((part) => part.type !== 'step-finish') : parts;
  const shouldRenderCard = bodyParts.length > 0 || stepFinishParts.length === 0;

  return (
    <>
      {shouldRenderCard ? (
        <article className={`message-card ${isAssistant ? 'assistant' : 'user'} ${agentKind === 'subagent' ? 'subagent-card' : ''}`}>
          <div className="message-meta">
            <span className={`role-badge ${isAssistant ? 'assistant' : 'user'}`}>
              {isAssistant ? <Bot size={13} /> : <Terminal size={13} />}
              {agentKind === 'subagent' ? 'subagent' : role}
            </span>
            {agentName ? <span className="muted-inline">{agentName}</span> : null}
            <span className="muted-inline">#{index + 1}</span>
            {message.info?.parent_call_id ? <span className="muted-inline">task {message.info.parent_call_id}</span> : null}
            {message.info?.time?.created ? (
              <span className="muted-inline">{formatTime(message.info.time.created)}</span>
            ) : null}
            {isAssistant && message.info?.tokens ? (
              <span className="muted-inline">{formatTokenUsage(message.info.tokens)}</span>
            ) : null}
          </div>
          <div className="message-body">{renderMessageParts(bodyParts, isAssistant)}</div>
        </article>
      ) : null}
      {stepFinishParts.map((part, partIndex) => (
        <StepFinishView key={`step-finish-${String(message.info?.id || index)}-${partIndex}`} part={part} />
      ))}
    </>
  );
}

function EventItem({ event }: { event: StreamEvent }) {
  const view = buildEventViewModel(event);

  return (
    <details className={`event-item tone-${view.tone}`}>
      <summary>
        <span className="event-rail">
          <ChevronRight size={13} />
          <span className="event-icon">{view.icon}</span>
        </span>
        <span className="event-main">
          <span className="event-name">{view.title}</span>
          <span className="event-summary" title={view.summary}>
            {view.summary}
          </span>
        </span>
        <span className="event-meta">
          {view.chips.map((chip) => (
            <span key={chip} title={chip}>
              {chip}
            </span>
          ))}
          <time>{view.time}</time>
        </span>
      </summary>
      <pre>{JSON.stringify(event.data, null, 2)}</pre>
    </details>
  );
}

function renderMessageParts(parts: MessagePart[] | undefined, isAssistant: boolean) {
  if (!parts?.length) {
    return <p className="empty-message">（空消息）</p>;
  }
  const visibleParts: React.ReactNode[] = [];
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (part.type !== 'tool') {
      const rendered = renderPart(part, index, isAssistant);
      if (rendered !== null) {
        visibleParts.push(rendered);
      }
      continue;
    }

    const groupId = getToolGroupId(part);
    const toolParts = [part];
    let nextIndex = index + 1;
    while (groupId && parts[nextIndex]?.type === 'tool' && getToolGroupId(parts[nextIndex]) === groupId) {
      toolParts.push(parts[nextIndex]);
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

function TextBlock({ text }: { text: string }) {
  if (!text) {
    return <p className="empty-message">（空文本）</p>;
  }
  return <MarkdownContent className="message-text" text={text} />;
}

function MarkdownContent({ text, className }: { text: string; className: string }) {
  return (
    <div className={`markdown-body ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function ReasoningBlock({ text }: { text: string }) {
  if (!text) {
    return null;
  }
  return (
    <details className="reasoning-block">
      <summary>
        <span>
          <CircleDot size={12} />
          推理摘要
        </span>
        <small>{text.length} chars</small>
      </summary>
      <pre>{text}</pre>
    </details>
  );
}

function LiveReasoningBlock({ text }: { text: string }) {
  if (!text) {
    return null;
  }
  return (
    <details className="reasoning-block live-reasoning-block" open>
      <summary>
        <span>
          <CircleDot size={12} />
          实时推理
        </span>
        <small>{text.length} chars</small>
      </summary>
      <pre>{text}</pre>
    </details>
  );
}

function ToolPartView({ part, index }: { part: MessagePart; index?: number }) {
  const state = asRecord(part.state) as ToolState;
  const output = asRecord(state.output);
  const input = asRecord(state.input);
  const tool = stringValue(part.tool) || 'unknown_tool';
  const status = stringValue(state.status) || 'pending';
  const tone = getToolTone(status);
  const display = buildToolDisplay(tool, input, output);
  const command = stringValue(output.command) || stringValue(input.command);
  const description = stringValue(input.description) || stringValue(output.description);
  const cwd = stringValue(output.cwd) || stringValue(input.cwd);
  const filePath = stringValue(output.file_path) || stringValue(input.file_path);
  const url = stringValue(output.url) || stringValue(input.url);
  const finalUrl = stringValue(output.final_url);
  const skillName = stringValue(output.name) || stringValue(input.name);
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
        {skillName && tool === 'load_skill' ? <KeyValue label="skill" value={skillName} /> : null}
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
      {diff ? <OutputBlock label="diff" value={diff} tone="diff" /> : null}
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

function StepFinishView({ part }: { part: MessagePart }) {
  const reason = stringValue(part.reason);
  const label = reason && reason !== 'completed' ? reason : 'completed';
  return (
    <div className={`step-finish-note ${reason && reason !== 'completed' ? 'has-reason' : ''}`}>
      <span aria-hidden="true">
        <Check size={12} />
      </span>
      <strong>步骤结束</strong>
      <code>{label}</code>
    </div>
  );
}

function FilePartView({ part }: { part: MessagePart }) {
  const filename = stringValue(part.filename) || '文件';
  const mime = stringValue(part.mime);
  const source = asRecord(part.source);
  const sourceValue = stringValue(source.value);
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

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value : '';
}

function boolValue(value: unknown) {
  return value === true;
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

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`请求失败: ${url}`);
  }
  return response.json() as Promise<T>;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `请求失败: ${url}`);
  }
  return response.json() as Promise<T>;
}

export default App;
