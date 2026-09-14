import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import os from 'node:os';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '../..');
const backend = path.join(root, 'backend');
const frontend = path.join(root, 'frontend');
const stateFile = path.join(os.tmpdir(), 'codepilot-playwright-state.json');
const origin = 'https://127.0.0.1:5443';
const password = 'browser-test-password';

export default async function globalSetup() {
  const temp = mkdtempSync(path.join(os.tmpdir(), 'codepilot-browser-'));
  const home = path.join(temp, 'home');
  const key = path.join(temp, 'key.pem');
  const cert = path.join(temp, 'cert.pem');
  const python = path.join(backend, '.venv/bin/python');
  const children: ChildProcess[] = [];
  const environment = {
    ...process.env,
    CODEPILOT_HOME: home,
    CODEPILOT_AUTH_MODE: 'lan_https',
    CODEPILOT_PUBLIC_ORIGIN: origin,
    OPENAI_API_KEY: 'browser-test-only',
    PYTHONPATH: path.join(backend, 'src'),
  };

  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=127.0.0.1', '-keyout', key, '-out', cert], { stdio: 'ignore' });
  execFileSync(python, ['-c', [
    'from pathlib import Path',
    'from codepilot.auth import AuthStore, UserRole',
    `s=AuthStore(Path(${JSON.stringify(home)})/'auth.sqlite3', idle_timeout_seconds=43200, absolute_timeout_seconds=604800)`,
    `s.create_user('alice', ${JSON.stringify(password)}, role=UserRole.ADMIN)`,
    `s.create_user('bob', ${JSON.stringify(password)}, role=UserRole.USER)`,
  ].join(';')], { cwd: backend, env: environment, stdio: 'ignore' });

  const backendProcess = spawn(python, ['-m', 'uvicorn', 'codepilot.main:app', '--app-dir', 'src', '--host', '127.0.0.1', '--port', '8010'], {
    cwd: backend,
    env: environment,
    stdio: ['ignore', 'ignore', 'ignore'],
  });
  children.push(backendProcess);
  await waitForHttp('http://127.0.0.1:8010/api/health/live', false);

  const proxyProcess = spawn(process.execPath, [path.join(frontend, 'e2e/https-proxy.mjs'), key, cert, path.join(frontend, 'dist'), '5443', '8010'], {
    cwd: frontend,
    stdio: ['ignore', 'ignore', 'ignore'],
  });
  children.push(proxyProcess);
  await waitForHttp(`${origin}/api/health/live`, true);
  writeFileSync(stateFile, JSON.stringify({ home, password }), { mode: 0o600 });

  return async () => {
    for (const child of children.reverse()) child.kill('SIGTERM');
    rmSync(stateFile, { force: true });
    rmSync(temp, { recursive: true, force: true });
  };
}

async function waitForHttp(url: string, insecure: boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const ready = await new Promise<boolean>((resolve) => {
      const request = httpsOrHttpGet(url, insecure, (status) => resolve(status > 0 && status < 500));
      request.on('error', () => resolve(false));
      request.setTimeout(500, () => { request.destroy(); resolve(false); });
    });
    if (ready) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`测试服务启动超时：${url}`);
}

function httpsOrHttpGet(url: string, insecure: boolean, done: (status: number) => void) {
  if (url.startsWith('https:')) return https.get(url, { rejectUnauthorized: !insecure }, (response) => { response.resume(); done(response.statusCode || 0); });
  return importHttpGet(url, done);
}

function importHttpGet(url: string, done: (status: number) => void) {
  return http.get(url, (response) => { response.resume(); done(response.statusCode || 0); });
}

export function readE2eState(): { home: string; password: string } {
  return JSON.parse(readFileSync(stateFile, 'utf-8'));
}
