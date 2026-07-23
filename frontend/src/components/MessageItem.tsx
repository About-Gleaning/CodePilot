import { Bot, Check, Copy, Terminal } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';

import { buildMessageCopyText, copyPlainText } from '../messageUtils';
import type { MessagePart, MessageRecord, TokenUsage } from '../types';

type Props = {
  message: MessageRecord;
  index: number;
  renderParts: (parts: MessagePart[], isAssistant: boolean) => ReactNode;
  renderStepFinish: (part: MessagePart, key: string) => ReactNode;
  formatTime: (timestamp: number) => string;
  formatTokenUsage: (tokens: TokenUsage) => string;
};

export function MessageItem({ message, index, renderParts, renderStepFinish, formatTime, formatTokenUsage }: Props) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const role = String(message.info?.role || 'unknown');
  const isAssistant = role === 'assistant';
  const agentKind = String(message.info?.agent_kind || 'agent');
  const agentName = String(message.info?.agent || '');
  const parts = message.parts || [];
  const stepFinishParts = parts.filter((part) => part.type === 'step-finish');
  const bodyParts = stepFinishParts.length ? parts.filter((part) => part.type !== 'step-finish') : parts;
  const copyText = buildMessageCopyText(parts);
  const canCopy = copyText.length > 0;

  const handleCopy = async () => {
    if (!canCopy) return;
    setCopyState(await copyPlainText(copyText) ? 'copied' : 'failed');
    window.setTimeout(() => setCopyState('idle'), 1400);
  };

  return <>
    {bodyParts.length > 0 || stepFinishParts.length === 0 ? <article className={`message-card ${isAssistant ? 'assistant' : 'user'} ${agentKind === 'subagent' ? 'subagent-card' : ''}`}>
      <div className="message-meta">
        <span className={`role-badge ${isAssistant ? 'assistant' : 'user'}`}>{isAssistant ? <Bot size={13} /> : <Terminal size={13} />}{agentKind === 'subagent' ? 'subagent' : role}</span>
        {agentName ? <span className="muted-inline">{agentName}</span> : null}<span className="muted-inline">#{index + 1}</span>
        {message.info?.parent_call_id ? <span className="muted-inline">task {message.info.parent_call_id}</span> : null}
        {message.info?.time?.created ? <span className="muted-inline">{formatTime(message.info.time.created)}</span> : null}
        {isAssistant && message.info?.tokens ? <span className="muted-inline">{formatTokenUsage(message.info.tokens)}</span> : null}
        {canCopy ? <button type="button" className={`message-copy-button ${copyState === 'copied' ? 'is-copied' : ''} ${copyState === 'failed' ? 'is-failed' : ''}`} onClick={() => void handleCopy()} title={copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制消息'} aria-label="复制消息">{copyState === 'copied' ? <Check size={13} /> : <Copy size={13} />}</button> : null}
      </div><div className="message-body">{renderParts(bodyParts, isAssistant)}</div>
    </article> : null}
    {stepFinishParts.map((part, partIndex) => renderStepFinish(part, `step-finish-${String(message.info?.id || index)}-${partIndex}`))}
  </>;
}
