import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import { readFileSync } from 'node:fs';

const [keyFile, certFile, rawPort, rawBackendPort, rawFrontendPort] = process.argv.slice(2);
const port = Number(rawPort);
const backendPort = Number(rawBackendPort);
const frontendPort = Number(rawFrontendPort);

function targetFor(pathname) {
  return pathname.startsWith('/api/') ? backendPort : frontendPort;
}

function localHeaders(headers, port) {
  return { ...headers, host: `127.0.0.1:${port}`, 'x-forwarded-for': '127.0.0.1' };
}

const server = https.createServer({ key: readFileSync(keyFile), cert: readFileSync(certFile) }, (request, response) => {
  const targetPort = targetFor(request.url || '/');
  const upstream = http.request({
    hostname: '127.0.0.1', port: targetPort, method: request.method, path: request.url,
    headers: localHeaders(request.headers, targetPort),
  }, (upstreamResponse) => {
    response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
    upstreamResponse.pipe(response);
  });
  upstream.on('error', () => { if (!response.headersSent) response.writeHead(502); response.end(); });
  request.pipe(upstream);
});

// Vite 热更新与 SSE 需要保留升级连接，不能只代理普通 HTTP 请求。
server.on('upgrade', (request, socket, head) => {
  const targetPort = targetFor(request.url || '/');
  const upstream = net.connect(targetPort, '127.0.0.1', () => {
    const lines = [`${request.method} ${request.url} HTTP/${request.httpVersion}`];
    for (const [name, value] of Object.entries(localHeaders(request.headers, targetPort))) {
      if (value !== undefined) lines.push(`${name}: ${Array.isArray(value) ? value.join(', ') : value}`);
    }
    upstream.write(`${lines.join('\r\n')}\r\n\r\n`);
    if (head.length) upstream.write(head);
    socket.pipe(upstream).pipe(socket);
  });
  upstream.on('error', () => socket.destroy());
});

server.listen(port, '0.0.0.0');
