import type { ReactNode } from 'react';

import type { MessageRecord } from '../types';

type MessageListProps = {
  messages: MessageRecord[];
  hasLiveOutput: boolean;
  renderEmpty: () => ReactNode;
  renderMessage: (message: MessageRecord, index: number) => ReactNode;
  renderLiveOutput: () => ReactNode;
};

export function MessageList({ messages, hasLiveOutput, renderEmpty, renderMessage, renderLiveOutput }: MessageListProps) {
  return (
    <>
      {messages.length === 0 && !hasLiveOutput ? renderEmpty() : null}
      {messages.map(renderMessage)}
      {renderLiveOutput()}
    </>
  );
}
