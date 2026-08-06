import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, Archive, Bot, Brain, CalendarClock, Check, ChevronDown, CircleDot,
  History, Menu, MessageSquareText, PanelLeft, PanelRight, Play, Plus, Radio, RefreshCw,
  Search, Send, Settings2, ShieldCheck, Square, Terminal, X,
} from 'lucide-react';

import { ApiError, apiRequest } from '../../api/client';
import { AttachmentPicker, AttachmentTray } from '../../components/Attachments';
import { MessageStream } from '../../components/MessageStream';
import { ReasoningBlock, TextBlock } from '../../components/MessageContent';
import { ThemeToggle } from '../../components/ThemeToggle';
import { useQuestionInteraction } from '../../hooks/useQuestionInteraction';
import { useTheme } from '../../hooks/useTheme';
import type { MessagePart, PendingAttachment, TokenUsage } from '../../types';
import { AgentConfigPanel } from './AgentConfigPanel';
import { AutomationPanel } from './AutomationPanel';
import type { AgentRuntime, AgentSummary, PendingInteraction, ProviderConfig } from './types';
import { useAgentCatalog } from './useAgentCatalog';
import { isUnknownRequest, useAgentSession } from './useAgentSession';

type InspectorTab = 'sessions' | 'automation';
type StudioView = 'conversation' | 'agent-config';
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

type ComposerAutocomplete = {
  kind: 'skill' | 'file';
  trigger: '$' | '@';
  start: number;
  end: number;
  query: string;
  activeIndex: number;
};

type ComposerOption = { value: string; label: string; description?: string };
type SkillOption = { name: string; description: string };

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
  const { theme, toggleTheme } = useTheme();
  const catalog = useAgentCatalog();
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [studioView, setStudioView] = useState<StudioView>('conversation');
  const [configAgentId, setConfigAgentId] = useState<string | null>(null);
  const [sessionByAgent, setSessionByAgent] = useState<Record<string, string | null>>({});
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('sessions');
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>(null);
  const [agentRailCollapsed, setAgentRailCollapsed] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [agentSearch, setAgentSearch] = useState('');
  const [agentFilter, setAgentFilter] = useState<'active' | 'archived' | 'error'>('active');
  const [historyLimit, setHistoryLimit] = useState(50);
  const [configDirty, setConfigDirty] = useState(false);
  const [sending, setSending] = useState(false);
  const [interactionBusy, setInteractionBusy] = useState(false);
  const [localError, setLocalError] = useState('');
  const [skills, setSkills] = useState<SkillOption[]>([]);
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [composerAutocomplete, setComposerAutocomplete] = useState<ComposerAutocomplete | null>(null);
  const [fileSuggestions, setFileSuggestions] = useState<ComposerOption[]>([]);
  const [fileSuggestionsLoading, setFileSuggestionsLoading] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const draftsRef = useRef(drafts);

  const selectedAgent = selectedAgentId ? catalog.agentsById[selectedAgentId] || null : null;
  const selectedRuntime = selectedAgentId ? catalog.runtimes[selectedAgentId] || stoppedRuntime(selectedAgentId) : null;
  const configAgent = configAgentId ? catalog.agentsById[configAgentId] || null : null;
  const configRuntime = configAgentId ? catalog.runtimes[configAgentId] || stoppedRuntime(configAgentId) : null;
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
    if (selectedAgentId || !catalog.agents.length) return;
    const preferred = catalog.agents.find((agent) => agent.name === 'build' && !agent.archived)
      || catalog.agents.find((agent) => !agent.archived)
      || catalog.agents[0];
    setSelectedAgentId(preferred.agent_id);
  }, [catalog.agents, selectedAgentId]);

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

  useEffect(() => {
    const controller = new AbortController();
    void apiRequest<{ skills: SkillOption[]; activated_providers: ProviderConfig[] }>('/api/config', { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        setSkills(value.skills || []);
        setProviders(value.activated_providers || []);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setSkills([]);
        setProviders([]);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (composerAutocomplete?.kind !== 'file') {
      setFileSuggestions([]);
      setFileSuggestionsLoading(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setFileSuggestionsLoading(true);
      const params = new URLSearchParams({ q: composerAutocomplete.query, limit: '40' });
      void apiRequest<{ files: Array<{ path: string }> }>(`/api/workspace/files?${params}`, { signal: controller.signal })
        .then((value) => { if (!controller.signal.aborted) setFileSuggestions(value.files.map((file) => ({ value: file.path, label: file.path }))); })
        .catch(() => { if (!controller.signal.aborted) setFileSuggestions([]); })
        .finally(() => { if (!controller.signal.aborted) setFileSuggestionsLoading(false); });
    }, 120);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [composerAutocomplete?.kind, composerAutocomplete?.query]);

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

  const composerOptions = !composerAutocomplete ? [] : composerAutocomplete.kind === 'file'
    ? fileSuggestions
    : buildSkillComposerOptions(skills, composerAutocomplete.query);
  const overrideProvider = providers.find((item) => item.provider === draft.provider) || null;
  const overrideModels = overrideProvider?.models || [];
  const overrideThinking = draft.model ? overrideProvider?.model_capabilities?.[draft.model]?.thinking || null : null;
  const defaultOverrideProvider = providers.find((item) => item.provider === selectedAgent?.default_provider) || providers[0] || null;
  const defaultOverrideModel = defaultOverrideProvider?.models.includes(selectedAgent?.default_model || '')
    ? selectedAgent?.default_model || ''
    : '';

  const updateComposerAutocomplete = (value: string, caret: number | null) => {
    setComposerAutocomplete(detectComposerAutocomplete(value, caret ?? value.length));
  };

  const pickComposerOption = (option: ComposerOption) => {
    if (!composerAutocomplete) return;
    const replacement = `${composerAutocomplete.trigger}${option.value} `;
    const content = `${draft.content.slice(0, composerAutocomplete.start)}${replacement}${draft.content.slice(composerAutocomplete.end)}`;
    const nextCaret = composerAutocomplete.start + replacement.length;
    updateDraft({ content, clientRequestId: null });
    setComposerAutocomplete(null);
    window.requestAnimationFrame(() => {
      composerRef.current?.focus();
      composerRef.current?.setSelectionRange(nextCaret, nextCaret);
    });
  };

  const selectAgent = (agentId: string) => {
    if (studioView === 'agent-config' && configDirty && !window.confirm('当前配置存在未保存修改，仍要切换 Agent 吗？')) return;
    setStudioView('conversation');
    setConfigAgentId(null);
    setSelectedAgentId(agentId);
    setHistoryLimit(50);
    setConfigDirty(false);
    setLocalError('');
    setMobilePanel(null);
  };

  const changeInspectorTab = (tab: InspectorTab) => {
    setInspectorTab(tab);
  };

  const openAgentConfig = (agentId: string | null) => {
    if (studioView === 'agent-config' && configDirty && !window.confirm('当前配置存在未保存修改，仍要打开其他配置吗？')) return;
    setConfigDirty(false);
    setConfigAgentId(agentId);
    setStudioView('agent-config');
    setInspectorOpen(false);
    setMobilePanel(null);
    setLocalError('');
  };

  const closeAgentConfig = () => {
    if (configDirty && !window.confirm('当前配置存在未保存修改，仍要返回会话吗？')) return;
    setConfigDirty(false);
    setConfigAgentId(null);
    setStudioView('conversation');
  };

  const newSession = () => {
    if (!selectedAgentId) return;
    setSessionByAgent((current) => ({ ...current, [selectedAgentId]: null }));
    setInspectorTab('sessions');
    setLocalError('');
    setMobilePanel(null);
  };

  const startSuggestedTask = (content: string) => {
    updateDraft({ content, clientRequestId: null });
    window.requestAnimationFrame(() => composerRef.current?.focus());
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
        content: translateSkillShortcuts(draft.content.trim(), skills),
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
    <div className={`agent-studio-shell ${agentRailCollapsed ? 'is-agent-rail-collapsed' : ''} ${inspectorOpen ? 'is-inspector-open' : ''}`}>
      {runtimeRecoveryBlocked ? (
        <div className="global-runtime-banner">
          <Radio size={14} />
          <span>运行态恢复不完整：已禁止新 Run。历史回放与关闭 Agent 仍可使用，请检查运行日志后重启服务。</span>
        </div>
      ) : null}
      <header className="studio-mobile-header">
        <button type="button" onClick={() => setMobilePanel('agents')} aria-label="打开 Agent 导航"><Menu size={18} /></button>
        <div>
          <strong>{studioView === 'agent-config' ? configAgent?.name || '创建 Agent' : selectedAgent?.name || 'Agent Studio'}</strong>
          <span>{studioView === 'agent-config' ? 'Agent 配置' : selectedSessionId ? shortId(selectedSessionId) : '新会话'}</span>
        </div>
        <div className="studio-mobile-actions">
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          {studioView === 'conversation' ? <button type="button" onClick={() => setMobilePanel('inspector')} aria-label="打开检查器"><PanelRight size={18} /></button> : null}
        </div>
      </header>

      <aside className={`studio-agent-rail ${mobilePanel === 'agents' ? 'is-mobile-open' : ''}`}>
          <div className="studio-brand">
          <div className="brand-mark"><Terminal size={18} /></div>
          <div><span>CODEPILOT</span><strong>Agent Studio</strong></div>
          <div className="studio-brand-actions">
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
            <button type="button" className="rail-collapse-button" onClick={() => setAgentRailCollapsed((value) => !value)} aria-label={agentRailCollapsed ? '展开 Agent 导航' : '收起 Agent 导航'} title={agentRailCollapsed ? '展开导航' : '收起导航'}><PanelLeft size={16} /></button>
          </div>
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
          openAgentConfig(null);
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

      {studioView === 'agent-config' ? (
        <main className="studio-config-workspace">
          <AgentConfigPanel
            key={configAgentId || 'create'}
            agent={configAgent}
            activeRunCount={configRuntime?.active_run_count || 0}
            onBack={closeAgentConfig}
            onDirtyChange={setConfigDirty}
            onSaved={(changed, created) => {
              setConfigDirty(false);
              void catalog.refresh().then(() => {
                if (created) {
                  setSelectedAgentId(changed.agent_id);
                  setConfigAgentId(null);
                  setStudioView('conversation');
                  return;
                }
                setConfigAgentId(changed.agent_id);
              });
            }}
          />
        </main>
      ) : (
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
            <button type="button" className="studio-icon-button" disabled={!selectedAgent} onClick={() => openAgentConfig(selectedAgentId)} aria-label={`配置 ${selectedAgent?.name || 'Agent'}`} title="配置 Agent"><Settings2 size={15} /></button>
            <button type="button" className="studio-icon-button inspector-toggle" onClick={() => setInspectorOpen((value) => !value)} aria-label={inspectorOpen ? '收起会话与自动化面板' : '打开会话与自动化面板'} title={inspectorOpen ? '收起侧栏' : '会话与自动化'}><PanelRight size={15} /></button>
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
          {session.runtime?.last_run ? <span><CircleDot size={13} />最近 Run：{session.runtime.last_run.status}{session.runtime.last_run.error_code ? ` · ${session.runtime.last_run.error_code}` : ''}</span> : null}
          {session.streamOffline ? <em>Session 流离线，正在重连</em> : null}
        </div>
        {(catalog.error || session.error || localError || selectedRuntime?.error_code || session.runtime?.last_run?.error_summary) ? (
          <div className="chat-local-error" role="alert">
            <CircleDot size={14} />
            <span>{localError || session.error || selectedRuntime?.error_code || session.runtime?.last_run?.error_summary || catalog.error}</span>
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
              emptyContent={<WelcomePanel agentName={selectedAgent?.name} onChoose={startSuggestedTask} />}
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
              <div className="composer-input-shell">
                <textarea
                  ref={composerRef}
                  rows={3}
                  value={draft.content}
                  disabled={!selectedAgent}
                  onChange={(event) => {
                    updateDraft({ content: event.target.value, clientRequestId: null });
                    updateComposerAutocomplete(event.target.value, event.target.selectionStart);
                  }}
                  onKeyDown={(event) => {
                    if (composerAutocomplete) {
                      if (event.key === 'Escape') { event.preventDefault(); setComposerAutocomplete(null); return; }
                      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                        event.preventDefault();
                        if (composerOptions.length) setComposerAutocomplete((current) => current ? {
                          ...current,
                          activeIndex: (current.activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + composerOptions.length) % composerOptions.length,
                        } : current);
                        return;
                      }
                      if (event.key === 'Enter' || event.key === 'Tab') {
                        event.preventDefault();
                        const option = composerOptions[Math.min(composerAutocomplete.activeIndex, composerOptions.length - 1)];
                        if (option) pickComposerOption(option);
                        return;
                      }
                    }
                    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                  placeholder={selectedRuntime?.lifecycle_state === 'RUNNING' ? '输入任务；Enter 发送，Shift+Enter 换行。' : '启动 Agent 后开始对话。'}
                />
                <ComposerAutocompletePanel
                  state={composerAutocomplete}
                  options={composerOptions}
                  loading={fileSuggestionsLoading}
                  onHover={(activeIndex) => setComposerAutocomplete((current) => current ? { ...current, activeIndex } : null)}
                  onPick={pickComposerOption}
                />
              </div>
              <AttachmentTray attachments={draft.attachments} onRemove={removeAttachment} />
              {!selectedSessionId ? (
                <div className="model-override">
                  <label><input type="checkbox" checked={draft.overrideModel} onChange={(event) => {
                    const enabled = event.target.checked;
                    updateDraft({
                      overrideModel: enabled,
                      provider: enabled && !draft.provider ? defaultOverrideProvider?.provider || '' : draft.provider,
                      model: enabled && !draft.model ? defaultOverrideModel : draft.model,
                      clientRequestId: null,
                    });
                  }} />覆盖 Agent 默认模型</label>
                  {draft.overrideModel ? (
                    <div>
                      <select aria-label="Provider" value={draft.provider} onChange={(event) => updateDraft({ provider: event.target.value, model: '', thinkingValue: '', clientRequestId: null })}>
                        <option value="">选择 Provider</option>
                        {providers.map((item) => <option value={item.provider} key={item.provider}>{item.label}</option>)}
                      </select>
                      <select aria-label="Model" value={draft.model} disabled={!overrideProvider} onChange={(event) => updateDraft({ model: event.target.value, thinkingValue: '', clientRequestId: null })}>
                        <option value="">选择 Model</option>
                        {overrideModels.map((model) => <option value={model} key={model}>{model}</option>)}
                      </select>
                      {overrideThinking ? <select aria-label="Thinking（可选）" value={draft.thinkingValue} onChange={(event) => updateDraft({ thinkingValue: event.target.value, clientRequestId: null })}>
                        <option value="">使用默认 Thinking</option>
                        {overrideThinking.allowed_values.map((value) => <option value={value} key={value}>{value}</option>)}
                      </select> : null}
                    </div>
                  ) : <span>使用 {selectedAgent?.default_provider || '-'} / {selectedAgent?.default_model || '-'}</span>}
                </div>
              ) : null}
              <div className="composer-footer">
                <div>
                  <AttachmentPicker onFiles={(files) => void handleFiles(files)} />
                  {draft.clientRequestId ? <small>将复用未确认请求 {draft.clientRequestId.slice(-8)}</small> : null}
                </div>
                <button type="submit" className="studio-button primary send-button" disabled={!draft.content.trim() || sending || selectedRuntime?.lifecycle_state !== 'RUNNING' || runtimeRecoveryBlocked || (draft.overrideModel && (!draft.provider || !draft.model))}>
                  <Send size={15} />{sending ? '发送中…' : draft.clientRequestId ? '重试原请求' : '发送'}
                </button>
              </div>
            </>
          ) : null}
        </form>
      </main>
      )}

      <aside className={`studio-inspector ${mobilePanel === 'inspector' ? 'is-mobile-open' : ''}`}>
        <nav className="inspector-tabs">
          <button type="button" className={inspectorTab === 'sessions' ? 'is-active' : ''} onClick={() => changeInspectorTab('sessions')}><History size={14} />会话</button>
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
          <AutomationPanel
            active={inspectorTab === 'automation'}
            agents={catalog.agents}
            onOpenSession={(agentId, sessionId) => {
              setStudioView('conversation');
              setConfigAgentId(null);
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

function WelcomePanel({ agentName, onChoose }: { agentName?: string; onChoose: (content: string) => void }) {
  const suggestions = [
    ['梳理一个想法', '帮我把这个想法拆成清晰的目标、步骤和下一步行动：'],
    ['分析一份材料', '请阅读并提炼这份材料的重点、风险与待确认事项：'],
    ['从任务开始', '我想完成下面这件事，请先给出一个可执行方案：'],
  ] as const;

  return (
    <section className="studio-welcome" aria-label="开始新任务">
      <span className="welcome-kicker">CODEPILOT · YOUR THINKING SPACE</span>
      <h1>{agentName ? `和 ${agentName} 一起，把想法推进一步。` : '把想法推进一步。'}</h1>
      <p>从一个问题、一份材料，或一个还不完整的念头开始。你始终可以在发送前补充技能、文件和图片。</p>
      <div className="welcome-suggestions">
        {suggestions.map(([title, content]) => <button type="button" key={title} onClick={() => onChoose(content)}><strong>{title}</strong><span>{content}</span></button>)}
      </div>
    </section>
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

function ComposerAutocompletePanel({ state, options, loading, onHover, onPick }: {
  state: ComposerAutocomplete | null;
  options: ComposerOption[];
  loading: boolean;
  onHover: (index: number) => void;
  onPick: (option: ComposerOption) => void;
}) {
  if (!state) return null;
  return (
    <div className="composer-autocomplete" role="listbox" aria-label={`${state.kind === 'skill' ? 'Skills' : 'Files'} 补全`}>
      <div className="autocomplete-heading"><span>{state.trigger}</span><strong>{state.kind === 'skill' ? 'Skills' : 'Files'}</strong>{state.query ? <code>{state.query}</code> : null}</div>
      {loading ? <div className="autocomplete-empty">搜索中...</div> : null}
      {!loading && !options.length ? <div className="autocomplete-empty">无匹配结果</div> : null}
      {!loading && options.length ? <div className="autocomplete-list">{options.map((option, index) => (
        <button type="button" className={`autocomplete-option ${index === state.activeIndex ? 'is-active' : ''}`} key={`${state.kind}:${option.value}`} onMouseEnter={() => onHover(index)} onMouseDown={(event) => { event.preventDefault(); onPick(option); }} role="option" aria-selected={index === state.activeIndex}>
          <span>{option.label}</span>{option.description ? <small>{option.description}</small> : null}
        </button>
      ))}</div> : null}
    </div>
  );
}

function InteractionPanel({ interaction, busy, onReply }: {
  interaction: PendingInteraction | null;
  busy: boolean;
  onReply: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const [comment, setComment] = useState('');
  const question = useQuestionInteraction({
    onSubmit: async (_request, answers) => { await onReply({ type: 'question_reply', answers }); return true; },
    onDecline: async () => { await onReply({ type: 'question_decline' }); return true; },
  });
  useEffect(() => {
    setComment('');
    question.restorePendingQuestion(interaction?.kind === 'question' ? interaction.request : null);
  }, [interaction?.interaction_id]);
  useEffect(() => {
    if (interaction?.kind !== 'question' || !question.questionRequest) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLElement && event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (event.key === 'ArrowLeft') { event.preventDefault(); question.moveActiveQuestion(-1); }
      if (event.key === 'ArrowRight') { event.preventDefault(); question.moveActiveQuestion(1); }
      if (event.key === 'Escape') { event.preventDefault(); void question.handleQuestionDecline(); }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [interaction?.kind, question.questionRequest, question.activeQuestionIndex]);
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
        <div className="interaction-actions"><button type="button" className="studio-button danger" disabled={busy} onClick={() => void onReply({ type: 'human_reply', approved: false, comment })}><X size={14} />拒绝</button><button type="button" className="studio-button primary" disabled={busy} onClick={() => void onReply({ type: 'human_reply', approved: true, comment })}><Check size={14} />同意</button></div>
      </section>
    );
  }
  const activeQuestion = question.questionRequest?.questions[question.activeQuestionIndex] || null;
  const activeAnswer = activeQuestion ? question.questionAnswers[activeQuestion.id] || { values: [], note: '' } : null;
  if (!question.questionRequest || !activeQuestion || !activeAnswer) {
    return (
      <section className="studio-interaction composer-question" aria-label="用户回答" role="alert">
        <header><MessageSquareText size={16} /><strong>无法展示问题</strong><code>{shortId(interaction.interaction_id)}</code></header>
        <p>问题数据不完整，请退出本次提问后重新发起。</p>
        <div className="interaction-actions"><button type="button" className="studio-button danger" disabled={busy} onClick={() => void onReply({ type: 'question_decline' })}><X size={14} />退出</button></div>
      </section>
    );
  }
  return (
    <section className="studio-interaction composer-question" aria-label="用户回答">
      <header><MessageSquareText size={16} /><strong>Agent 正在等待回答</strong><code>{shortId(interaction.interaction_id)}</code></header>
      <div className="question-flow">
        <div className="question-progress" aria-label="问题进度">{question.questionRequest.questions.map((item, index) => <button type="button" key={item.id} className={`question-step ${index === question.activeQuestionIndex ? 'is-active' : ''} ${question.questionAnswers[item.id]?.values.length ? 'is-done' : ''}`} aria-current={index === question.activeQuestionIndex ? 'step' : undefined} onClick={() => { question.setActiveQuestionIndex(index); question.setActiveQuestionOptionIndex(0); question.setQuestionError(''); question.focusQuestionOptions(); }}>{index + 1}</button>)}</div>
        <fieldset className="question-fieldset"><legend><span>第 {question.activeQuestionIndex + 1} / {question.questionRequest.questions.length} 题</span>{activeQuestion.question}</legend>
          <div className="question-options" ref={question.questionOptionsRef} role={activeQuestion.multiple ? 'group' : 'radiogroup'} aria-label={activeQuestion.question} tabIndex={0} onKeyDown={(event) => question.handleQuestionOptionsKeyDown(event, activeQuestion)}>
            {activeQuestion.options.map((option, index) => <label className={`question-option ${index === question.activeQuestionOptionIndex ? 'is-keyboard-active' : ''}`} key={option.value}><input type={activeQuestion.multiple ? 'checkbox' : 'radio'} name={activeQuestion.id} checked={activeAnswer.values.includes(option.value)} tabIndex={-1} onChange={(event) => { question.setActiveQuestionOptionIndex(index); question.updateQuestionChoice(activeQuestion, option.value, event.target.checked); }} /><span>{option.label}</span></label>)}
          </div>
          <textarea ref={question.questionNoteRef} rows={3} placeholder="备注（可选）" value={activeAnswer.note} onChange={(event) => question.updateQuestionNote(activeQuestion.id, event.target.value)} onKeyDown={question.handleQuestionNoteKeyDown} />
          {question.questionError ? <p className="question-error">{question.questionError}</p> : null}
        </fieldset>
      </div>
      <div className="interaction-actions"><button type="button" className="studio-button danger" disabled={busy} onClick={() => void question.handleQuestionDecline()}><X size={14} />退出</button><button type="button" className="studio-button secondary" disabled={busy || question.activeQuestionIndex === 0} onClick={() => question.moveActiveQuestion(-1)}>上一题</button><button type="button" className="studio-button primary" disabled={busy} onClick={() => void question.confirmActiveQuestion()}>{question.activeQuestionIndex === question.questionRequest.questions.length - 1 ? '提交回答' : '下一题'}</button></div>
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

function detectComposerAutocomplete(value: string, caret: number): ComposerAutocomplete | null {
  const beforeCaret = value.slice(0, caret);
  const match = /(^|\s)([$@])([^\s$@]*)$/.exec(beforeCaret);
  if (!match) return null;
  const trigger = match[2] as '$' | '@';
  const query = match[3] || '';
  return { kind: trigger === '$' ? 'skill' : 'file', trigger, start: caret - query.length - 1, end: caret, query, activeIndex: 0 };
}

function buildSkillComposerOptions(skills: SkillOption[], query: string): ComposerOption[] {
  const normalized = query.trim().toLowerCase();
  return skills.filter((skill) => !normalized || skill.name.toLowerCase().includes(normalized) || skill.description.toLowerCase().includes(normalized)).slice(0, 20).map((skill) => ({ value: skill.name, label: skill.name, description: skill.description }));
}

function translateSkillShortcuts(value: string, skills: SkillOption[]) {
  const names = new Map(skills.map((skill) => [skill.name.toLowerCase(), skill.name]));
  return value.replace(/(^|\s)\$([A-Za-z0-9_.-]+)/g, (raw, prefix: string, name: string) => {
    const skill = names.get(name.toLowerCase());
    return skill ? `${prefix}${skill} skill` : raw;
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
