import { CircleDot } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function TextBlock({ text }: { text: string }) {
  return text ? <MarkdownContent className="message-text" text={text} /> : <p className="empty-message">（空文本）</p>;
}

export function MarkdownContent({ text, className }: { text: string; className: string }) {
  return <div className={`markdown-body ${className}`}><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>;
}

export function ReasoningBlock({ text }: { text: string }) {
  return text ? <details className="reasoning-block"><summary><span><CircleDot size={12} />推理摘要</span><small>{text.length} chars</small></summary><pre>{text}</pre></details> : null;
}

export function LiveReasoningBlock({ text }: { text: string }) {
  return text ? <details className="reasoning-block live-reasoning-block" open><summary><span><CircleDot size={12} />实时推理</span><small>{text.length} chars</small></summary><pre>{text}</pre></details> : null;
}
