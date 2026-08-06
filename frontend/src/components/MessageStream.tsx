import { Bot, Sparkles } from 'lucide-react';
import type { ReactNode } from 'react';

import { LiveReasoningBlock, MarkdownContent } from './MessageContent';
import { MessageItem } from './MessageItem';
import { MessageList } from './MessageList';
import type { MessagePart, MessageRecord, TokenUsage } from '../types';

type Props = {
  messages: MessageRecord[];
  liveDelta: string;
  liveReasoningDelta: string;
  subagentLiveDeltas: Record<string, string>;
  subagentLiveReasoningDeltas: Record<string, string>;
  renderParts: (parts: MessagePart[], isAssistant: boolean) => ReactNode;
  renderStepFinish: (part: MessagePart, key: string) => ReactNode;
  formatTime: (timestamp: number) => string;
  formatTokenUsage: (tokens: TokenUsage) => string;
  emptyContent?: ReactNode;
};

export function MessageStream({
  messages,
  liveDelta,
  liveReasoningDelta,
  subagentLiveDeltas,
  subagentLiveReasoningDeltas,
  renderParts,
  renderStepFinish,
  formatTime,
  formatTokenUsage,
  emptyContent,
}: Props) {
  return (
    <MessageList
      messages={messages}
      hasLiveOutput={Boolean(liveDelta || liveReasoningDelta || Object.keys(subagentLiveDeltas).length || Object.keys(subagentLiveReasoningDeltas).length)}
      renderEmpty={() => emptyContent || <div className="empty-state"><Sparkles size={20} /><p>选择 Agent、Provider 与 Model 后，在底部输入任务开始会话。</p></div>}
      renderMessage={(message, index) => <MessageItem key={String(message.info?.id || index)} message={message} index={index} renderParts={renderParts} renderStepFinish={renderStepFinish} formatTime={formatTime} formatTokenUsage={formatTokenUsage} />}
      renderLiveOutput={() => <>
        {liveDelta || liveReasoningDelta ? (
          <article className="message-card assistant streaming-card">
            <div className="message-meta">
              <span className="role-badge assistant"><Bot size={13} />assistant</span>
              <span className="muted-inline">streaming</span>
            </div>
            <LiveReasoningBlock text={liveReasoningDelta} />
            {liveDelta ? <MarkdownContent className="message-live-text" text={liveDelta} /> : null}
          </article>
        ) : null}
        {Array.from(new Set([...Object.keys(subagentLiveDeltas), ...Object.keys(subagentLiveReasoningDeltas)])).map((key) => (
          <article className="message-card assistant streaming-card subagent-card" key={key}>
            <div className="message-meta">
              <span className="role-badge assistant"><Bot size={13} />subagent</span>
              <span className="muted-inline">{key}</span>
            </div>
            <LiveReasoningBlock text={subagentLiveReasoningDeltas[key] || ''} />
            {subagentLiveDeltas[key] ? <MarkdownContent className="message-live-text" text={subagentLiveDeltas[key]} /> : null}
          </article>
        ))}
      </>}
    />
  );
}
