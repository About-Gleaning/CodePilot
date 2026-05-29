import { FormEvent, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleDot,
  FileText,
  HardDrive,
  ListTree,
  MessageSquareText,
  OctagonAlert,
  Play,
  Plus,
  Radio,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  Terminal,
  X,
} from 'lucide-react';

type ProviderOption = {
  provider: string;
  label: string;
  models: string[];
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
  tool_call_started: '工具调用开始',
  tool_call_finished: '工具调用完成',
  tool_call_failed: '工具调用失败',
  context_compacted: '上下文压缩',
  human_approval_required: '需要人工确认',
  human_approval_resolved: '人工确认已处理',
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
    parent_id?: string;
    model?: {
      provider_id?: string;
      model_id?: string;
    };
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

type ToolState = Record<string, unknown> & {
  status?: string;
  input?: Record<string, unknown>;
  output?: Record<string, unknown> | null;
  error?: Record<string, unknown> | null;
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
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [sessionHistory, setSessionHistory] = useState<SessionSummary[]>([]);
  const [loadingSessionId, setLoadingSessionId] = useState<string | null>(null);
  const [liveDelta, setLiveDelta] = useState('');
  const [lastSeq, setLastSeq] = useState(0);
  const lastSeqRef = useRef(0);
  const eventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    void bootstrap();
    return () => {
      eventSourceRef.current?.close();
    };
  }, []);

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
      setAgentName(statusRes.agent_name || 'build');
      setMessages(replayRes.messages);
      setSessionHistory(sessionsRes.sessions);
      connectStream(0);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '初始化失败');
    }
  }

  function connectStream(afterSeq: number) {
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
      source.close();
      window.setTimeout(() => connectStream(lastSeqRef.current), 1200);
    };
    eventSourceRef.current = source;
  }

  function onStreamEvent(event: StreamEvent) {
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
      setLiveDelta((prev) => prev + String(event.data.text || ''));
    }
    if (event.event_type === 'assistant_message_completed') {
      setLiveDelta('');
      if (event.data.message && typeof event.data.message === 'object') {
        setMessages((prev) => upsertMessage(prev, event.data.message as MessageRecord));
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
      return;
    }
    if (!providerOption.models.includes(model)) {
      setModel('');
    }
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
    try {
      const payload = {
        type: 'user_message',
        content: task,
        agent_name: agentName,
        provider,
        model,
        metadata: {},
        ...(currentSessionId ? { session_id: currentSessionId } : {}),
      };
      const response = await postJson<{ ok: boolean; session: StatusResponse | null }>('/api/session/input', payload);
      setCurrentSessionId(response.session?.session_id || null);
      setTask('');
      await refreshStatus();
      await refreshSessionHistory();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '提交任务失败');
    }
  }

  function handleNewTask() {
    setCurrentSessionId(null);
    setMessages([]);
    setEvents([]);
    setLiveDelta('');
    setApprovalRequest(null);
    setApprovalComment('');
    setFormError('');
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
      setAgentName(nextStatus.agent_name || 'build');
      setMessages(replay.messages);
      setEvents([]);
      setLiveDelta('');
      setApprovalRequest(null);
      setApprovalComment('');
      setLastSeq(0);
      lastSeqRef.current = 0;
      connectStream(0);
      await refreshSessionHistory();
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
  const statusText = status?.status || 'IDLE';

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
            badge={`seq ${lastSeq}`}
            action={
              <button type="button" className="button secondary session-new-button" onClick={handleNewTask} title="新会话" aria-label="新会话">
                <Plus size={14} />
              </button>
            }
          />
          <div className={`status-pill ${getStatusClass(statusText)}`}>
            <CircleDot size={12} />
            <span>{statusText}</span>
          </div>
          <InfoRow label="session" value={status?.session_id || '-'} mono />
          <SessionHistoryList
            sessions={sessionHistory}
            currentSessionId={currentSessionId}
            loadingSessionId={loadingSessionId}
            onLoad={handleLoadSession}
          />
        </section>
      </aside>

      <main className="terminal-stage">
        <header className="stage-header">
          <div>
            <span className="eyebrow">interactive session</span>
            <h2>消息流</h2>
            <p className="stage-subtitle">实时观察 Agent 输出、工具执行和人工审批状态。</p>
          </div>
          <div className="header-metrics">
            <Metric icon={<MessageSquareText size={14} />} label="messages" value={String(messages.length)} />
            <Metric icon={<ListTree size={14} />} label="events" value={String(events.length)} />
          </div>
        </header>

        {formError ? (
          <section className="error-strip" role="alert">
            <OctagonAlert size={16} />
            <span>{formError}</span>
          </section>
        ) : null}

        <section className="message-viewport" aria-label="会话消息">
          {messages.length === 0 && !liveDelta ? (
            <div className="empty-state">
              <Sparkles size={20} />
              <p>选择 Agent、Provider 与 Model 后，在底部输入任务开始会话。</p>
            </div>
          ) : null}

          {messages.map((message, index) => (
            <MessageItem key={String(message.info?.id || index)} message={message} index={index} />
          ))}

          {liveDelta ? (
            <article className="message-card assistant streaming-card">
              <div className="message-meta">
                <span className="role-badge assistant">
                  <Bot size={13} />
                  assistant
                </span>
                <span className="muted-inline">streaming</span>
              </div>
              <MarkdownContent className="message-live-text" text={liveDelta} />
            </article>
          ) : null}
        </section>

        <form className={`composer ${approvalRequest ? 'has-approval' : ''}`} onSubmit={handleStart}>
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
                onChange={setModel}
                disabled={!provider}
              />
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
          <PanelTitle icon={<ListTree size={16} />} title="事件流" badge={`${events.length}/200`} />
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

function InfoRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="info-row">
      <span>{label}</span>
      <strong className={mono ? 'mono' : undefined}>{value}</strong>
    </div>
  );
}

function SessionHistoryList({
  sessions,
  currentSessionId,
  loadingSessionId,
  onLoad,
}: {
  sessions: SessionSummary[];
  currentSessionId: string | null;
  loadingSessionId: string | null;
  onLoad: (sessionId: string) => void;
}) {
  return (
    <div className="session-history">
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

function MessageItem({ message, index }: { message: MessageRecord; index: number }) {
  const role = String(message.info?.role || 'unknown');
  const isAssistant = role === 'assistant';

  return (
    <article className={`message-card ${isAssistant ? 'assistant' : 'user'}`}>
      <div className="message-meta">
        <span className={`role-badge ${isAssistant ? 'assistant' : 'user'}`}>
          {isAssistant ? <Bot size={13} /> : <Terminal size={13} />}
          {role}
        </span>
        <span className="muted-inline">#{index + 1}</span>
        {message.info?.time?.created ? (
          <span className="muted-inline">{formatTime(message.info.time.created)}</span>
        ) : null}
      </div>
      <div className="message-body">{renderMessageParts(message.parts, isAssistant)}</div>
    </article>
  );
}

function EventItem({ event }: { event: StreamEvent }) {
  const label = EVENT_LABELS[event.event_type] || event.event_type;
  const tone = getEventTone(event.event_type);

  return (
    <details className={`event-item ${tone}`}>
      <summary>
        <span className="event-seq">#{event.seq}</span>
        <span className="event-name">
          <ChevronRight size={13} />
          {label}
        </span>
        <span className="event-summary">{buildEventSummary(event)}</span>
      </summary>
      <pre>{JSON.stringify(event.data, null, 2)}</pre>
    </details>
  );
}

function renderMessageParts(parts: MessagePart[] | undefined, isAssistant: boolean) {
  if (!parts?.length) {
    return <p className="empty-message">（空消息）</p>;
  }
  const visibleParts = parts
    .map((part, index) => renderPart(part, index, isAssistant))
    .filter((item): item is React.ReactNode => item !== null);
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
    return <StepFinishView key={key} part={part} />;
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

function ToolPartView({ part }: { part: MessagePart }) {
  const state = asRecord(part.state) as ToolState;
  const output = asRecord(state.output);
  const input = asRecord(state.input);
  const tool = stringValue(part.tool) || 'unknown_tool';
  const status = stringValue(state.status) || 'pending';
  const tone = getToolTone(status);
  const title = buildToolTitle(tool, status, output);
  const command = stringValue(output.command) || stringValue(input.command);
  const cwd = stringValue(output.cwd) || stringValue(input.cwd);
  const filePath = stringValue(output.file_path) || stringValue(input.file_path);
  const errorMessage = buildToolErrorMessage(state, output);
  const resultText = stringValue(output.output);
  const diff = stringValue(output.diff);
  const stdout = stringValue(output.stdout);
  const stderr = stringValue(output.stderr);
  const hasDetails = hasMeaningfulDetails({ state, output });

  return (
    <article className={`tool-card ${tone}`}>
      <header className="tool-card-header">
        <div className="tool-title">
          {tone === 'tool-error' ? <AlertTriangle size={15} /> : <Play size={14} />}
          <span>{title}</span>
        </div>
        <div className="tool-chips">
          <StatusChip status={status} />
          {typeof output.duration_ms === 'number' ? <span>{output.duration_ms}ms</span> : null}
          {typeof output.exit_code === 'number' ? <span>exit {output.exit_code}</span> : null}
        </div>
      </header>

      {command ? <KeyValue label="command" value={command} mono /> : null}
      {cwd ? <KeyValue label="cwd" value={cwd} mono /> : null}
      {filePath ? <KeyValue label="file" value={filePath} mono /> : null}
      {buildToolOperation(output) ? <KeyValue label="operation" value={buildToolOperation(output)} /> : null}

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

      {hasDetails ? (
        <details className="raw-details">
          <summary>查看原始详情</summary>
          <pre>{jsonPretty({ tool, call_id: part.call_id, state })}</pre>
        </details>
      ) : null}
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
    <section className={`tool-output ${tone}`}>
      <div>
        <span>{label}</span>
        {truncated ? <small>已截断</small> : null}
      </div>
      <pre>{value}</pre>
    </section>
  );
}

function StepFinishView({ part }: { part: MessagePart }) {
  const reason = stringValue(part.reason);
  if (!reason || reason === 'completed') {
    return null;
  }
  return <CompactNote tone="neutral" title="步骤结束" value={reason} />;
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

function buildToolTitle(tool: string, status: string, output: Record<string, unknown>) {
  if (status === 'pending') {
    return `${tool} 等待执行`;
  }
  if (status === 'running') {
    return `${tool} 正在执行`;
  }
  if (status === 'error' || output.status === 'error') {
    return `${tool} 执行失败`;
  }
  return `${tool} 执行完成`;
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

function hasMeaningfulDetails({ state, output }: { state: ToolState; output: Record<string, unknown> }) {
  const input = asRecord(state.input);
  const inputKeys = Object.keys(input);
  const outputKeys = Object.keys(output);
  return inputKeys.length > 0 || outputKeys.some((key) => !KNOWN_OUTPUT_FIELDS.has(key));
}

const KNOWN_OUTPUT_FIELDS = new Set([
  'status',
  'tool_name',
  'command',
  'cwd',
  'exit_code',
  'stdout',
  'stderr',
  'timed_out',
  'stdout_truncated',
  'stderr_truncated',
  'duration_ms',
  'file_path',
  'output',
  'is_empty',
  'operation',
  'replaced_count',
  'diff',
  'bytes_written',
  'error_type',
  'error_message',
  'recoverable',
]);

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value : '';
}

function boolValue(value: unknown) {
  return value === true;
}

function jsonPretty(value: unknown) {
  return JSON.stringify(value, null, 2);
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
  if (event.event_type === 'llm_delta') {
    const text = String(event.data.text || '');
    return text ? text.slice(0, 48) : 'delta';
  }
  if (event.data.status) {
    return String(event.data.status);
  }
  if (event.data.reason) {
    return String(event.data.reason).slice(0, 48);
  }
  if (event.event_type === 'context_compacted') {
    const beforeTokens = event.data.before_tokens ?? '-';
    const afterTokens = event.data.after_tokens ?? '-';
    return `${beforeTokens} -> ${afterTokens} tokens`;
  }
  if (event.data.message && typeof event.data.message === 'object') {
    const message = event.data.message as MessageRecord;
    return String(message.info?.role || 'message');
  }
  return event.session_id || event.created_at;
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
