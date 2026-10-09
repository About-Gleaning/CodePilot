import type { ApiIssue } from '../../api/client';

export function AgentErrorDetails({ message, code, issues = [] }: { message: string; code?: string; issues?: ApiIssue[] }) {
  return <div className="agent-error-details">
    <strong>{message}</strong>
    {issues.length > 0 ? <ul>{issues.map((issue, index) => <li key={`${issue.code}:${index}`}>
      <span>{issue.message}</span>
      {issue.suggestion ? <p>{issue.suggestion}</p> : null}
    </li>)}</ul> : null}
    {code ? <details><summary>技术详情</summary><code>{code}</code></details> : null}
  </div>;
}
