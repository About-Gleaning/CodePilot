import type { MessagePart } from './types';

export function buildMessageCopyText(parts: MessagePart[]) {
  return parts.filter((part) => part.type === 'text').map((part) => typeof part.text === 'string' ? part.text.trim() : '').filter(Boolean).join('\n\n');
}

export async function copyPlainText(text: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 部分浏览器或非安全上下文会拒绝 Clipboard API，继续走 textarea 兜底。
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.top = '-1000px';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  try { return document.execCommand('copy'); } finally { document.body.removeChild(textarea); }
}
