import { FormEvent, useEffect, useState } from 'react';
import { LogOut, ShieldCheck } from 'lucide-react';

import { AUTH_EXPIRED_EVENT, apiJson, apiRequest } from './api/client';
import AgentStudio from './features/agent-studio/AgentStudio';

type User = { user_id: string; username: string; role: 'admin' | 'user' };

export default function AuthGate() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const expired = () => {
      setUser(null);
      setError('登录已失效，请重新登录。');
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    void apiRequest<{ user: User }>('/api/auth/me')
      .then((value) => setUser(value.user))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  }, []);

  if (loading) return <main className="auth-shell" aria-busy="true" />;
  if (!user) return <LoginView error={error} onError={setError} onAuthenticated={setUser} />;

  const logout = async () => {
    try {
      await apiJson('/api/auth/logout', 'POST');
    } finally {
      setUser(null);
      setError('');
    }
  };

  return (
    <div className="authenticated-app">
      <div className="identity-bar">
        <span><ShieldCheck size={15} aria-hidden="true" />{user.username}</span>
        <span className="identity-role">{user.role === 'admin' ? '管理员' : '用户'}</span>
        <button type="button" className="identity-logout" onClick={() => void logout()} title="退出登录" aria-label="退出登录">
          <LogOut size={16} aria-hidden="true" />
        </button>
      </div>
      <AgentStudio />
    </div>
  );
}

function LoginView({ error, onError, onAuthenticated }: {
  error: string;
  onError: (value: string) => void;
  onAuthenticated: (user: User) => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    onError('');
    try {
      const value = await apiJson<{ user: User }>('/api/auth/login', 'POST', { username, password });
      setPassword('');
      onAuthenticated(value.user);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : '登录失败。');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="auth-shell">
      <form className="login-panel" onSubmit={(event) => void submit(event)}>
        <div className="login-mark"><ShieldCheck size={20} aria-hidden="true" /></div>
        <h1>CodePilot</h1>
        <label>用户名<input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required /></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        {error ? <p className="login-error" role="alert">{error}</p> : null}
        <button type="submit" disabled={busy}>{busy ? '登录中...' : '登录'}</button>
      </form>
    </main>
  );
}
