import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, Archive, Bot, Brain, CalendarClock, Check, ChevronDown, CircleDot,
  History, Menu, MessageSquareText, PanelRight, Play, Plus, Radio, RefreshCw,
  Search, Send, Settings2, ShieldCheck, Square, Terminal, X,
} from 'lucide-react';

import { ApiError } from '../../api/client';
import { AttachmentPicker, AttachmentTray } from '../../components/Attachments';
import { MessageStream } from '../../components/MessageStream';
import { ReasoningBlock, TextBlock } from '../../components/MessageContent';
import type { MessagePart, PendingAttachment, QuestionItem, TokenUsage } from '../../types';
import { AgentConfigPanel } from './AgentConfigPanel';
import { AutomationPanel } from './AutomationPanel';
import type { AgentRuntime, AgentSummary, PendingInteraction } from './types';
import { useAgentCatalog } from './useAgentCatalog';
import { isUnknownRequest, useAgentSession } from './useAgentSession';

type InspectorTab = 'sessions' | 'config' | 'automation';
type MobilePanel = 'agents' | 'inspector' | null;
type Draft = {
  content: string;
  attachments: PendingAttachment[];
  overrideModel: boolean;
  provider: string;
  model: string;
  thinkingValue: string;
  clientRequestId: string | null;
};

const EMPTY_DRAFT: Draft = {
  content: '',
  attachments: [],
  overrideModel: false,
  provider: '',
  model: '',
  thinkingValue: '',
  clientRequestId: null,
};

export default function AgentStudio() {
  const catalog = useAgentCatalog();
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [creatingAgent, setCreatingAgent] = useState(false);
  const [sessionByAgent, setSessionByAgent] = useState<Record<string, string | null>>({});
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('sessions');
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>(null);
  const [agentSearch, setAgentSearch] = useState('');
  const [agentFilter, setAgentFilter] = useState<'active' | 'archived' | 'error'>('active');
  const [historyLimit, setHistoryLimit] = useState(50);
  const [configDirty, setConfigDirty] = useState(false);
  const [sending, setSending] = useState(false);
  const [interactionBusy, setInteractionBusy] = useState(false);
  const [localError, setLocalError] = useState('');
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const draftsRef = useRef(drafts);

  const selectedAgent = selectedAgentId ? catalog.agentsById[selectedAgentId] || null : null;
  const selectedRuntime = selectedAgentId ? catalog.runtimes[selectedAgentId] || stoppedRuntime(selectedAgentId) : null;
  const selectedSessionId = selectedAgentId ? sessionByAgent[selectedAgentId] ?? null : null;
  const draft = selectedAgentId ? drafts[selectedAgentId] || EMPTY_DRAFT : EMPTY_DRAFT;
  const session = useAgentSession(
    selectedAgentId,
    selectedSessionId,
    (sessionId) => {
      if (!selectedAgentId) return;
      setSessionByAgent((current) => ({ ...current, [selectedAgentId]: sessionId }));
    },
    () => void catalog.refresh(),
  );

  useEffect(() => {
    if (selectedAgentId || creatingAgent || !catalog.agents.length) return;
    const preferred = catalog.agents.find((agent) => agent.name === 'build' && !agent.archived)
      || catalog.agents.find((agent) => !agent.archived)
      || catalog.agents[0];
    setSelectedAgentId(preferred.agent_id);
  }, [catalog.agents, creatingAgent, selectedAgentId]);

  useEffect(() => {
    if (
      !selectedAgentId
      || session.sessionsAgentId !== selectedAgentId
      || Object.prototype.hasOwnProperty.call(sessionByAgent, selectedAgentId)
    ) return;
    const recent = catalog.runtimes[selectedAgentId]?.recent_session_id;
    const candidate = session.sessions.find((item) => item.session_id === recent)?.session_id
      || session.sessions[0]?.session_id
      || null;
    setSessionByAgent((current) => ({ ...current, [selectedAgentId]: candidate }));
  }, [catalog.runtimes, selectedAgentId, session.sessions, session.sessionsAgentId, sessionByAgent]);

  useEffect(() => {
    const element = messagesRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [
    session.view.messages.length,
    session.view.liveDelta.length,
    session.view.liveReasoningDelta.length,
  ]);

  useEffect(() => {
    draftsRef.current = drafts;
  }, [drafts]);

  useEffect(() => () => {
    Object.values(draftsRef.current)
      .flatMap((item) => item.attachments)
      .forEach((item) => URL.revokeObjectURL(item.previewUrl));
  }, []);

  const updateDraft = (patch: Partial<Draft>) => {
    if (!selectedAgentId) return;
    setDrafts((current) => ({
      ...current,
      [selectedAgentId]: { ...(current[selectedAgentId] || EMPTY_DRAFT), ...patch },
    }));
  };

  const selectAgent = (agentId: string) => {
    if (configDirty && !window.confirm('当前配置存在未保存修改，仍要切换 Agent 吗？')) return;
    setCreatingAgent(false);
    setSelectedAgentId(agentId);
    setHistoryLimit(50);
    setConfigDirty(false);
    setLocalError('');
    setMobilePanel(null);
  };

  const changeInspectorTab = (tab: InspectorTab) => {
    if (inspectorTab === 'config' && configDirty && tab !== 'config' && !window.confirm('配置尚未保存，仍要离开吗？')) return;
    setInspectorTab(tab);
  };

  const newSession = () => {
    if (!selectedAgentId) return;
    setSessionByAgent((current) => ({ ...current, [selectedAgentId]: null }));
    setInspectorTab('sessions');
    setLocalError('');
    setMobilePanel(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!selectedAgent || !selectedRuntime || sending || !draft.content.trim()) return;
    if (selectedRuntime.lifecycle_state !== 'RUNNING') {
      setLocalError('Agent 尚未启动，请先启动后再发送。');
      return;
    }
    if (selectedAgent.archived) {
      setLocalError('Agent 已归档，当前历史可查看，但不能创建新 Run。');
      return;
    }
    const clientRequestId = draft.clientRequestId || `web_${crypto.randomUUID().replace(/-/g, '')}`;
    updateDraft({ clientRequestId });
    setSending(true);
    setLocalError('');
    try {
      await session.send({
        content: draft.content.trim(),
        attachments: draft.attachments,
        provider: draft.overrideModel ? draft.provider : undefined,
        model: draft.overrideModel ? draft.model : undefined,
        thinkingValue: draft.overrideModel ? draft.thinkingValue : undefined,
        clientRequestId,
      });
      draft.attachments.forEach((item) => URL.revokeObjectURL(item.previewUrl));
      updateDraft({ content: '', attachments: [], clientRequestId: null });
    } catch (reason) {
      if (!isUnknownRequest(reason)) updateDraft({ clientRequestId: null });
      setLocalError(
        reason instanceof ApiError && reason.code === 'run_capacity_exceeded'
          ? `并行 Run 已达 ${catalog.capacity.max_active_runs} 个上限，请在 ${reason.retryAfter || 1} 秒后重试。`
          : reason instanceof Error
            ? `${reason.message}${isUnknownRequest(reason) ? ' 可直接重试，页面会复用本次请求标识。' : ''}`
            : '发送失败',
      );
    } finally {
      setSending(false);
    }
  };

  const handleFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    const accepted = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
    const next: PendingAttachment[] = [];
    for (const file of Array.from(files).slice(0, Math.max(0, 4 - draft.attachments.length))) {
      if (!accepted.includes(file.type) || file.size > 5 * 1024 * 1024) {
        setLocalError('仅支持 png/jpeg/webp/gif，单图不能超过 5MB。');
        continue;
      }
      next.push({
        id: crypto.randomUUID(),
        filename: file.name,
        mime: file.type,
        data_base64: await fileToBase64(file),
        previewUrl: URL.createObjectURL(file),
        size: file.size,
      });
    }
    updateDraft({ attachments: [...draft.attachments, ...next], clientRequestId: null });
  };

  const removeAttachment = (id: string) => {
    const item = draft.attachments.find((attachment) => attachment.id === id);
    if (item) URL.revokeObjectURL(item.previewUrl);
    updateDraft({ attachments: draft.attachments.filter((attachment) => attachment.id !== id), clientRequestId: null });
  };

  const runtimeRecoveryBlocked = Object.values(catalog.runtimes).some(
    (runtime) => runtime.error_code === 'runtime_recovery_incomplete',
  );
  const filteredAgents = catalog.agents.filter((agent) => {
    const runtime = catalog.runtimes[agent.agent_id];
    const matchesSearch = `${agent.name} ${agent.description || ''}`.toLowerCase().includes(agentSearch.toLowerCase());
    const matchesFilter = agentFilter === 'archived'
      ? agent.archived
      : agentFilter === 'error'
        ? runtime?.lifecycle_state === 'ERROR' || agent.validation_status === 'invalid'
        : !agent.archived;
    return matchesSearch && matchesFilter;
  });

  return (
    <div className="agent-studio-shell">
      <div className="studio-grain" />
      {runtimeRecoveryBlocked ? (
        <div className="global-runtime-banner">
          <Radio size={14} />
          <span>运行态恢复不完整：已禁止新 Run。历史回放与关闭 Agent 仍可使用，请检查运行日志后重启服务。</span>
        </div>
      ) : null}
      <header className="studio-mobile-header">
        <button type="button" onClick={() => setMobilePanel('agents')} aria-label="打开 Agent 导航"><Menu size={18} /></button>
        <div><strong>{selectedAgent?.name || 'Agent Studio'}</strong><span>{selectedSessionId ? shortId(selectedSessionId) : '新会话'}</span></div>
        <button type="button" onClick={() => setMobilePanel('inspector')} aria-label="打开检查器"><PanelRight size={18} /></button>
      </header>

      <aside className={`studio-agent-rail ${mobilePanel === 'agents' ? 'is-mobile-open' : ''}`}>
        <div className="studio-brand">
          <div className="brand-mark"><Terminal size={18} /></div>
          <div><span>CODEPILOT</span><strong>Agent Studio</strong></div>
          <small>CONTROL / 52</small>
        </div>
        <div className="agent-search">
          <Search size={14} />
          <input value={agentSearch} onChange={(event) => setAgentSearch(event.target.value)} placeholder="搜索 Agent" />
        </div>
        <div className="agent-filter" role="tablist">
          {(['active', 'archived', 'error'] as const).map((filter) => (
            <button type="button" className={agentFilter === filter ? 'is-active' : ''} onClick={() => setAgentFilter(filter)} key={filter}>
              {filter === 'active' ? '活动' : filter === 'archived' ? '归档' : '异常'}
            </button>
          ))}
        </div>
        <div className="agent-list">
          {filteredAgents.map((agent) => (
            <AgentNavigationItem
              key={agent.agent_id}
              agent={agent}
              runtime={catalog.runtimes[agent.agent_id] || stoppedRuntime(agent.agent_id)}
              selected={agent.agent_id === selectedAgentId}
              onClick={() => selectAgent(agent.agent_id)}
            />
          ))}
          {!filteredAgents.length ? <p className="studio-empty-copy">没有符合条件的 Agent。</p> : null}
        </div>
        <button type="button" className="new-agent-button" onClick={() => {
          if (configDirty && !window.confirm('配置尚未保存，仍要新建 Agent 吗？')) return;
          setCreatingAgent(true);
          setSelectedAgentId(null);
          setInspectorTab('config');
          setMobilePanel('inspector');
        }}>
          <Plus size={15} />新建 Agent
        </button>
        <div className="capacity-board">
          <div><span>AGENTS</span><strong>{catalog.capacity.started_agents}/{catalog.capacity.max_started_agents}</strong></div>
          <div><span>RUNS</span><strong>{catalog.capacity.active_runs}/{catalog.capacity.max_active_runs}</strong></div>
          <i style={{ '--capacity': `${(catalog.capacity.active_runs / Math.max(1, catalog.capacity.max_active_runs)) * 100}%` } as React.CSSProperties} />
          <small>{catalog.offline ? '控制流离线，保留最后快照' : '控制流已连接'}</small>
        </div>
      </aside>

      <main className="studio-chat">
        <header className="chat-command-bar">
          <div className="chat-identity">
            <span className={`runtime-dot state-${statusTone(selectedRuntime)}`} />
            <div>
              <span className="eyebrow">ACTIVE CONTEXT</span>
              <strong>{selectedAgent?.name || '选择 Agent'}</strong>
            </div>
            {selectedAgent?.revision_id ? <code>rev {selectedAgent.revision_id.slice(0, 8)}</code> : null}
          </div>
          <div className="chat-runtime-actions">
            {selectedRuntime?.lifecycle_state !== 'RUNNING' ? (
              <button type="button" className="studio-button primary" disabled={!selectedAgent || selectedAgent.archived || catalog.mutating.has(selectedAgent.agent_id)} onClick={() => selectedAgent && void catalog.startAgent(selectedAgent.agent_id).catch(showError(setLocalError))}>
                <Play size={14} />启动 Agent
              </button>
            ) : (
              <>
                <button type="button" className="studio-button" disabled={!session.runtime?.active_run} onClick={() => void session.cancel().catch(showError(setLocalError))}>
                  <Square size={13} />取消本轮
                </button>
                <button type="button" className="studio-button danger" onClick={() => {
                  if (!selectedAgent) return;
                  const count = selectedRuntime.active_run_count;
                  if (window.confirm(`关闭 ${selectedAgent.name}？将取消该 Agent 的 ${count} 个活动 Run。`)) void catalog.stopAgent(selectedAgent.agent_id).catch(showError(setLocalError));
                }}>
                  <X size={14} />关闭 Agent
                </button>
              </>
            )}
          </div>
        </header>
        <div className="context-strip">
          <span><MessageSquareText size={13} />{selectedSessionId ? shortId(selectedSessionId) : '草稿 / 首条消息后创建 Session'}</span>
          <span><Brain size={13} />{session.runtime?.provider || selectedAgent?.default_provider || '-'} / {session.runtime?.model || selectedAgent?.default_model || '-'}</span>
          <span><Activity size={13} />{runtimeLabel(selectedRuntime)}</span>
          {session.streamOffline ? <em>Session 流离线，正在重连</em> : null}
        </div>
        {(catalog.error || session.error || localError) ? (
          <div className="chat-local-error" role="alert">
            <CircleDot size={14} />
            <span>{localError || session.error || catalog.error}</span>
            <button type="button" onClick={() => { setLocalError(''); session.setError(''); void catalog.refresh(); }}><RefreshCw size={13} />刷新</button>
          </div>
        ) : null}
        <div className="studio-message-scroll" ref={messagesRef}>
          {session.loading ? <div className="studio-panel-loading"><Radio size={18} /><strong>恢复 Session</strong><span>正在建立 replay / SSE 一致性边界…</span></div> : (
            <MessageStream
              messages={session.view.messages}
              liveDelta={session.view.liveDelta}
              liveReasoningDelta={session.view.liveReasoningDelta}
              subagentLiveDeltas={session.view.subagentLiveDeltas}
              subagentLiveReasoningDeltas={session.view.subagentLiveReasoningDeltas}
              renderParts={renderMessageParts}
              renderStepFinish={(part, key) => <div className="step-finish" key={key}>步骤完成 · {String(part.reason || 'done')}</div>}
              formatTime={formatTime}
              formatTokenUsage={formatTokenUsage}
            />
          )}
        </div>
        <form className="studio-composer" onSubmit={submit}>
          <InteractionPanel
            interaction={session.runtime?.pending_interaction || null}
            busy={interactionBusy}
            onReply={async (payload) => {
              setInteractionBusy(true);
              try {
                await session.replyInteraction(payload);
              } catch (reason) {
                setLocalError(reason instanceof Error ? reason.message : '提交人工交互失败');
              } finally {
                setInteractionBusy(false);
              }
            }}
          />
          {!session.runtime?.pending_interaction ? (
            <>
              <textarea
                rows={3}
                value={draft.content}
                disabled={!selectedAgent}
                onChange={(event) => updateDraft({ content: event.target.value, clientRequestId: null })}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder={selectedRuntime?.lifecycle_state === 'RUNNING' ? '输入任务；Enter 发送，Shift+Enter 换行。' : '启动 Agent 后开始对话。'}
              />
              <AttachmentTray attachments={draft.attachments} onRemove={removeAttachment} />
              {!selectedSessionId ? (
                <div className="model-override">
                  <label><input type="checkbox" checked={draft.overrideModel} onChange={(event) => updateDraft({ overrideModel: event.target.checked, clientRequestId: null })} />覆盖 Agent 默认模型</label>
                  {draft.overrideModel ? (
                    <div>
                      <input placeholder="Provider" value={draft.provider} onChange={(event) => updateDraft({ provider: event.target.value, clientRequestId: null })} />
                      <input placeholder="Model" value={draft.model} onChange={(event) => updateDraft({ model: event.target.value, clientRequestId: null })} />
                      <input placeholder="Thinking（可选）" value={draft.thinkingValue} onChange={(event) => updateDraft({ thinkingValue: event.target.value, clientRequestId: null })} />
                    </div>
                  ) : <span>使用 {selectedAgent?.default_provider || '-'} / {selectedAgent?.default_model || '-'}</span>}
                </div>
              ) : null}
              <div className="composer-footer">
                <div>
                  <AttachmentPicker onFiles={(files) => void handleFiles(files)} />
                  {draft.clientRequestId ? <small>将复用未确认请求 {draft.clientRequestId.slice(-8)}</small> : null}
                </div>
                <button type="submit" className="studio-button primary send-button" disabled={!draft.content.trim() || sending || selectedRuntime?.lifecycle_state !== 'RUNNING' || runtimeRecoveryBlocked}>
                  <Send size={15} />{sending ? '发送中…' : draft.clientRequestId ? '重试原请求' : '发送'}
                </button>
              </div>
            </>
          ) : null}
        </form>
      </main>

      <aside className={`studio-inspector ${mobilePanel === 'inspector' ? 'is-mobile-open' : ''}`}>
        <nav className="inspector-tabs">
          <button type="button" className={inspectorTab === 'sessions' ? 'is-active' : ''} onClick={() => changeInspectorTab('sessions')}><History size={14} />会话</button>
          <button type="button" className={inspectorTab === 'config' ? 'is-active' : ''} onClick={() => changeInspectorTab('config')}><Settings2 size={14} />配置</button>
          <button type="button" className={inspectorTab === 'automation' ? 'is-active' : ''} onClick={() => changeInspectorTab('automation')}><CalendarClock size={14} />自动化</button>
        </nav>
        <div className="inspector-content">
          {inspectorTab === 'sessions' ? (
            <>
              <div className="inspector-heading">
                <div><span className="eyebrow">SESSION INDEX</span><strong>{selectedAgent?.name || '未选择 Agent'}</strong></div>
                <button type="button" className="studio-icon-button" disabled={!selectedAgent} onClick={newSession} title="新会话"><Plus size={15} /></button>
              </div>
              <div className="session-list">
                {session.sessions.slice(0, historyLimit).map((item) => (
                  <button type="button" className={item.session_id === selectedSessionId ? 'is-active' : ''} key={item.session_id} onClick={() => {
                    if (!selectedAgentId) return;
                    setSessionByAgent((current) => ({ ...current, [selectedAgentId]: item.session_id }));
                    setMobilePanel(null);
                  }}>
                    <span className={`runtime-dot state-${sessionTone(item.status)}`} />
                    <span><strong>{item.title || item.preview || '未命名会话'}</strong><small>{item.source === 'schedule' ? `自动化 · ${item.schedule_task_name || ''}` : `${item.provider || '-'} / ${item.model || '-'}`}</small></span>
                    <em>{formatDate(item.updated_at)}</em>
                  </button>
                ))}
                {!session.sessions.length ? <p className="studio-empty-copy">该 Agent 尚无历史 Session。</p> : null}
              </div>
              {historyLimit < session.sessions.length ? <button type="button" className="load-more-button" onClick={() => setHistoryLimit((value) => value + 50)}><ChevronDown size={14} />再加载 50 条</button> : null}
              <div className="session-diagnostics">
                <span>已获取 {session.sessions.length}</span>
                <span>DOM {Math.min(historyLimit, session.sessions.length)}</span>
                <span>当前 {session.runtime?.status || 'DRAFT'}</span>
              </div>
            </>
          ) : null}
          {inspectorTab === 'config' ? (
            <AgentConfigPanel
              key={selectedAgent?.agent_id || 'create'}
              agent={selectedAgent}
              activeRunCount={selectedRuntime?.active_run_count || 0}
              onDirtyChange={setConfigDirty}
              onChanged={(changed) => {
                setConfigDirty(false);
                void catalog.refresh().then(() => {
                  if (changed?.agent_id) {
                    setCreatingAgent(false);
                    setSelectedAgentId(changed.agent_id);
                  }
                });
              }}
            />
          ) : null}
          <AutomationPanel
            active={inspectorTab === 'automation'}
            agents={catalog.agents}
            onOpenSession={(agentId, sessionId) => {
              setCreatingAgent(false);
              setSelectedAgentId(agentId);
              setSessionByAgent((current) => ({ ...current, [agentId]: sessionId }));
              setInspectorTab('sessions');
              setMobilePanel(null);
            }}
          />
        </div>
      </aside>
      {mobilePanel ? <button type="button" className="studio-mobile-backdrop" aria-label="关闭抽屉" onClick={() => setMobilePanel(null)} /> : null}
    </div>
  );
}

function AgentNavigationItem({ agent, runtime, selected, onClick }: {
  agent: AgentSummary;
  runtime: AgentRuntime;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button type="button" className={`agent-nav-item ${selected ? 'is-selected' : ''}`} onClick={onClick}>
      <span className={`runtime-dot state-${statusTone(runtime)}`} />
      <span className="agent-nav-copy">
        <strong>{agent.name}</strong>
        <small>{agent.description || '未填写描述'}</small>
        <em>{runtimeLabel(runtime)} · {agent.default_model || '未配置模型'}</em>
      </span>
      <span className="agent-nav-counts">
        {runtime.active_run_count ? <b>{runtime.active_run_count} run</b> : null}
        {runtime.waiting_human_count ? <b className="is-waiting">{runtime.waiting_human_count} 等待</b> : null}
        {agent.archived ? <Archive size={12} /> : null}
      </span>
    </button>
  );
}

function InteractionPanel({ interaction, busy, onReply }: {
  interaction: PendingInteraction | null;
  busy: boolean;
  onReply: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const [comment, setComment] = useState('');
  const [answers, setAnswers] = useState<Record<string, { values: string[]; note: string }>>({});
  useEffect(() => {
    setComment('');
    setAnswers({});
  }, [interaction?.interaction_id]);
  if (!interaction) return null;
  if (interaction.kind === 'approval') {
    const reason = String(interaction.request.reason || 'Agent 请求执行需要人工确认的操作。');
    const action = interaction.request.action && typeof interaction.request.action === 'object' ? interaction.request.action : {};
    return (
      <section className="studio-interaction">
        <header><ShieldCheck size={16} /><strong>等待人工审批</strong><code>{shortId(interaction.interaction_id)}</code></header>
        <p>{reason}</p>
        {Object.keys(action as object).length ? <pre>{JSON.stringify(action, null, 2)}</pre> : null}
        <textarea rows={2} placeholder="审批备注（可选）" value={comment} onChange={(event) => setComment(event.target.value)} />
        <div><button type="button" className="studio-button danger" disabled={busy} onClick={() => void onReply({ type: 'human_reply', approved: false, comment })}><X size={14} />拒绝</button><button type="button" className="studio-button primary" disabled={busy} onClick={() => void onReply({ type: 'human_reply', approved: true, comment })}><Check size={14} />同意</button></div>
      </section>
    );
  }
  const questions = Array.isArray(interaction.request.questions) ? interaction.request.questions as QuestionItem[] : [];
  return (
    <section className="studio-interaction">
      <header><MessageSquareText size={16} /><strong>Agent 正在等待回答</strong><code>{shortId(interaction.interaction_id)}</code></header>
      {questions.map((question) => (
        <fieldset key={question.id}>
          <legend>{question.question}</legend>
          {question.options.map((option) => {
            const current = answers[question.id] || { values: [], note: '' };
            const checked = current.values.includes(option.value);
            return <label key={option.value}><input type={question.multiple ? 'checkbox' : 'radio'} name={question.id} checked={checked} onChange={(event) => {
              const values = question.multiple
                ? event.target.checked ? [...current.values, option.value] : current.values.filter((value) => value !== option.value)
                : [option.value];
              setAnswers((items) => ({ ...items, [question.id]: { ...current, values } }));
            }} />{option.label}</label>;
          })}
          <input placeholder="备注（可选）" value={answers[question.id]?.note || ''} onChange={(event) => setAnswers((items) => ({ ...items, [question.id]: { ...(items[question.id] || { values: [] }), note: event.target.value } }))} />
        </fieldset>
      ))}
      <div><button type="button" className="studio-button danger" disabled={busy} onClick={() => void onReply({ type: 'question_decline' })}><X size={14} />退出</button><button type="button" className="studio-button primary" disabled={busy || questions.some((question) => !(answers[question.id]?.values.length))} onClick={() => void onReply({ type: 'question_reply', answers })}><Check size={14} />提交回答</button></div>
    </section>
  );
}

function renderMessageParts(parts: MessagePart[], isAssistant: boolean): ReactNode {
  if (!parts.length) return <p className="empty-message">（空消息）</p>;
  return parts.map((part, index) => {
    const key = `${part.type || 'part'}-${index}`;
    if (part.type === 'text') return <TextBlock key={key} text={String(part.text || '')} />;
    if (part.type === 'reasoning') return <ReasoningBlock key={key} text={String(part.text || '')} />;
    if (part.type === 'file') {
      const url = String(part.url || '');
      const filename = String(part.filename || '附件');
      return <a className="studio-file-part" href={url} target="_blank" rel="noreferrer" key={key}>{filename}</a>;
    }
    if (part.type === 'tool') {
      const state = part.state && typeof part.state === 'object' ? part.state as Record<string, unknown> : {};
      return (
        <details className={`studio-tool-part tool-${String(state.status || 'pending')}`} open={state.status === 'running' || state.status === 'error'} key={key}>
          <summary><Bot size={13} /><strong>{String(part.tool || 'tool')}</strong><span>{String(state.status || 'pending')}</span></summary>
          <pre>{JSON.stringify({ input: state.input, output: state.output, error: state.error }, null, 2)}</pre>
        </details>
      );
    }
    if (part.type === 'step-start' || part.type === 'step-finish') return null;
    return isAssistant ? <details className="studio-unknown-part" key={key}><summary>{String(part.type || 'unknown')}</summary><pre>{JSON.stringify(part, null, 2)}</pre></details> : <TextBlock key={key} text={JSON.stringify(part, null, 2)} />;
  });
}

function stoppedRuntime(agentId: string): AgentRuntime {
  return {
    agent_id: agentId,
    desired_state: 'STOPPED',
    lifecycle_state: 'STOPPED',
    recent_session_id: null,
    active_run_count: 0,
    waiting_human_count: 0,
    error_code: null,
  };
}

function statusTone(runtime: AgentRuntime | null) {
  if (!runtime) return 'stopped';
  if (runtime.lifecycle_state === 'ERROR') return 'error';
  if (runtime.waiting_human_count > 0) return 'waiting';
  if (runtime.active_run_count > 0) return 'running';
  return runtime.lifecycle_state.toLowerCase();
}

function runtimeLabel(runtime: AgentRuntime | null) {
  if (!runtime) return '未选择';
  if (runtime.lifecycle_state === 'ERROR') return '运行异常';
  if (runtime.waiting_human_count > 0) return '等待确认';
  if (runtime.active_run_count > 0) return '执行中';
  return {
    STOPPED: '已停止',
    STARTING: '启动中',
    RUNNING: '空闲',
    STOPPING: '正在关闭',
    ERROR: '运行异常',
  }[runtime.lifecycle_state];
}

function sessionTone(status: string) {
  const normalized = status.toLowerCase();
  if (normalized.includes('wait')) return 'waiting';
  if (normalized.includes('run') || normalized.includes('start')) return 'running';
  if (normalized.includes('fail') || normalized.includes('error')) return 'error';
  if (normalized.includes('complete')) return 'complete';
  return 'stopped';
}

function shortId(value: string) {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function formatTime(timestamp: number) {
  return new Date(timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatTokenUsage(tokens: TokenUsage) {
  const total = Number(tokens.input || 0) + Number(tokens.output || 0) + Number(tokens.reasoning || 0);
  return `${total.toLocaleString('zh-CN')} tokens`;
}

async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function showError(setter: (message: string) => void) {
  return (reason: unknown) => setter(reason instanceof Error ? reason.message : '操作失败');
}
