import { FormEvent, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronRight,
  CircleDot,
  Clock3,
  FileText,
  GitBranch,
  HardDrive,
  ListTree,
  MessageSquareText,
  OctagonAlert,
  Play,
  Radio,
  RefreshCcw,
  Send,
  Server,
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
  messages: Array<Record<string, unknown>>;
  records: Array<Record<string, unknown>>;
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

const EVENT_LABELS: Record<string, string> = {
  session_started: '会话启动',
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
  parts?: Array<Record<string, unknown>>;
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
      const [configRes, statusRes, replayRes] = await Promise.all([
        fetchJson<ConfigResponse>('/api/config'),
        fetchJson<StatusResponse>('/api/session/status'),
        fetchJson<ReplayResponse>('/api/session/replay'),
      ]);
      setConfig(configRes);
      setStatus(statusRes);
      setCurrentSessionId(statusRes.session_id);
      applyProviderAndModelState(statusRes);
      setAgentName(statusRes.agent_name || 'build');
      setMessages(replayRes.messages);
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
    setEvents((prev) => [...prev, event].slice(-200));
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
      event.event_type === 'session_status_changed' ||
      event.event_type === 'session_finished' ||
      event.event_type === 'session_failed'
    ) {
      void refreshStatus();
    }
  }

  async function refreshStatus() {
    const next = await fetchJson<StatusResponse>('/api/session/status');
    setStatus(next);
    setCurrentSessionId(next.session_id);
    applyProviderAndModelState(next);
    setAgentName(next.agent_name || 'build');
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

        <section className="panel compact-panel">
          <PanelTitle icon={<HardDrive size={16} />} title="工作区" badge={config?.workspace_id || 'loading'} />
          <InfoRow label="root" value={config?.workspace_path || '-'} mono />
          <InfoRow label="home" value={config?.codepilot_home || '-'} mono />
        </section>

        <section className="panel compact-panel">
          <PanelTitle icon={<Radio size={16} />} title="会话" badge={`seq ${lastSeq}`} />
          <div className={`status-pill ${getStatusClass(statusText)}`}>
            <CircleDot size={12} />
            <span>{statusText}</span>
          </div>
          <InfoRow label="session" value={status?.session_id || '-'} mono />
          <InfoRow label="agent" value={status?.agent_name || agentName || '-'} />
        </section>

        <section className="panel run-panel">
          <PanelTitle icon={<Bot size={16} />} title="运行配置" badge={`${config?.activated_providers.length || 0} providers`} />
          <label className="field">
            <span>Agent</span>
            <select value={agentName} onChange={(e) => setAgentName(e.target.value)}>
              {(config?.agents || ['build', 'plan']).map((agent) => (
                <option key={agent} value={agent}>
                  {agent}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Provider</span>
            <select value={provider} onChange={(e) => handleProviderChange(e.target.value)}>
              <option value="">请选择 provider</option>
              {(config?.activated_providers || []).map((item) => (
                <option key={item.provider} value={item.provider}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Model</span>
            <select value={model} onChange={(e) => setModel(e.target.value)} disabled={!provider}>
              <option value="">请选择 model</option>
              {modelOptions.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="button secondary full" onClick={handleNewTask}>
            <RefreshCcw size={15} />
            新任务
          </button>
        </section>
      </aside>

      <main className="terminal-stage">
        <header className="stage-header">
          <div>
            <span className="eyebrow">interactive session</span>
            <h2>消息流</h2>
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
              <pre>{liveDelta}</pre>
            </article>
          ) : null}
        </section>

        <form className="composer" onSubmit={handleStart}>
          <div className="composer-context">
            <span>
              <Bot size={13} />
              {agentName || '-'}
            </span>
            <span>
              <Server size={13} />
              {provider || 'provider 未选'}
            </span>
            <span>
              <GitBranch size={13} />
              {model || 'model 未选'}
            </span>
          </div>
          <textarea
            rows={4}
            placeholder="输入任务。若要体验审批演示，可在文本中加入 [[approve]]。"
            value={task}
            onChange={(e) => setTask(e.target.value)}
          />
          <div className="composer-actions">
            <button type="button" className="button secondary" onClick={handleStop}>
              <Square size={14} />
              停止
            </button>
            <button type="submit" className="button primary">
              <Send size={15} />
              发送
            </button>
          </div>
        </form>
      </main>

      <aside className="rail rail-right">
        <section className={`panel approval-panel ${approvalRequest ? 'is-active' : ''}`}>
          <PanelTitle icon={<ShieldCheck size={16} />} title="人工审批" badge={approvalRequest ? 'pending' : 'clear'} />
          {approvalRequest ? (
            <>
              <p className="approval-reason">{approvalRequest.reason}</p>
              <pre className="code-block">{JSON.stringify(approvalRequest.action || {}, null, 2)}</pre>
              <textarea
                rows={3}
                placeholder="审批备注"
                value={approvalComment}
                onChange={(e) => setApprovalComment(e.target.value)}
              />
              <div className="split-actions">
                <button type="button" className="button primary" onClick={() => handleApproval(true)}>
                  <Check size={15} />
                  同意
                </button>
                <button type="button" className="button danger" onClick={() => handleApproval(false)}>
                  <X size={15} />
                  拒绝
                </button>
              </div>
            </>
          ) : (
            <p className="quiet-copy">当前没有待处理的审批请求。</p>
          )}
        </section>

        <section className="panel compact-panel">
          <PanelTitle icon={<Clock3 size={16} />} title="运行参数" badge="sse" />
          <InfoRow label="heartbeat" value={`${config?.sse.heartbeat_seconds ?? '-'}s`} />
          <InfoRow label="replay" value={String(config?.sse.replay_on_connect ?? '-')} />
          <InfoRow label="provider" value={status?.provider || provider || '-'} />
          <InfoRow label="model" value={status?.model || model || '-'} mono />
        </section>

        <section className="panel event-panel">
          <PanelTitle icon={<ListTree size={16} />} title="事件流" badge={`${events.length}/200`} />
          <div className="event-list">
            {events.length === 0 ? (
              <p className="quiet-copy">等待 SSE 事件。</p>
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

function PanelTitle({ icon, title, badge }: { icon: React.ReactNode; title: string; badge?: string }) {
  return (
    <div className="panel-title">
      <div>
        {icon}
        <span>{title}</span>
      </div>
      {badge ? <code>{badge}</code> : null}
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
  const text = renderMessage(message);

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
      <pre>{text || '（空消息）'}</pre>
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

function renderMessage(message: MessageRecord) {
  return renderParts(message.parts);
}

function renderParts(parts?: Array<Record<string, unknown>>) {
  if (!parts?.length) {
    return '';
  }
  return parts
    .map((part) => {
      if (part.type === 'text') {
        return String(part.text || '');
      }
      if (part.type === 'reasoning') {
        return `【reasoning】\n${String(part.text || '')}`;
      }
      if (part.type === 'tool') {
        return JSON.stringify(
          {
            tool: part.tool,
            call_id: part.call_id,
            state: part.state,
          },
          null,
          2,
        );
      }
      return JSON.stringify(part, null, 2);
    })
    .join('\n\n');
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
