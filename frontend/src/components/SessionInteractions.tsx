import { ChevronDown, ShieldCheck } from 'lucide-react';

import type { ApprovalRequest } from '../types';

export function ScrollToBottomButton({ className, onClick }: { className: string; onClick: () => void }) {
  return <button type="button" className={className} onClick={onClick} title="滚动到底部 (Alt+End)" aria-label="滚动到底部"><ChevronDown size={17} /></button>;
}

export function ApprovalPanel({ approvalRequest, approvalComment, onApprovalCommentChange }: {
  approvalRequest: ApprovalRequest;
  approvalComment: string;
  onApprovalCommentChange: (nextValue: string) => void;
}) {
  return (
    <section className="composer-approval mobile-approval" aria-label="人工审批">
      <div className="approval-heading"><ShieldCheck size={16} /><strong>人工审批</strong><code>{approvalRequest.approval_id}</code></div>
      <p className="approval-reason">{approvalRequest.reason}</p>
      <pre className="code-block">{JSON.stringify(approvalRequest.action || {}, null, 2)}</pre>
      <textarea rows={3} placeholder="审批备注" value={approvalComment} onChange={(event) => onApprovalCommentChange(event.target.value)} />
    </section>
  );
}
