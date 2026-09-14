import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';

const [keyFile, certFile, distDir, rawPort, rawBackendPort] = process.argv.slice(2);
const port = Number(rawPort);
const backendPort = Number(rawBackendPort);
const mime = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.html', 'text/html; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.svg', 'image/svg+xml'],
]);

https.createServer(
  { key: readFileSync(keyFile), cert: readFileSync(certFile) },
  (request, response) => {
    if ((request.url || '').startsWith('/api/')) {
      const upstream = http.request({
        hostname: '127.0.0.1',
        port: backendPort,
        method: request.method,
        path: request.url,
        headers: { ...request.headers, host: `127.0.0.1:${backendPort}`, 'x-forwarded-for': '127.0.0.1' },
      }, (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
        upstreamResponse.pipe(response);
      });
      upstream.on('error', () => {
        if (!response.headersSent) response.writeHead(502);
        response.end();
      });
      request.pipe(upstream);
      return;
    }

    const pathname = decodeURIComponent(new URL(request.url || '/', 'https://localhost').pathname);
    const requested = pathname === '/' ? 'index.html' : pathname.slice(1);
    let target = path.resolve(distDir, requested);
    if (!target.startsWith(`${path.resolve(distDir)}${path.sep}`) || !existsSync(target) || statSync(target).isDirectory()) {
      target = path.resolve(distDir, 'index.html');
    }
    response.setHeader('content-type', mime.get(path.extname(target)) || 'application/octet-stream');
    createReadStream(target).pipe(response);
  },
).listen(port, '127.0.0.1');
