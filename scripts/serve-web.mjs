// 前端静态服务：托管 apps/web/out，并把 /api /v1 /v2 /healthz 反代到控制面。
// 页面与控制面分端口时浏览器仍然只面对一个源，Cookie 与 SSE 都不受跨域影响。
// 用法：node scripts/serve-web.mjs --port 18100 --backend 18101 --dir apps/web/out
import http from 'node:http';
import {createReadStream, existsSync, statSync} from 'node:fs';
import {extname, join, normalize, resolve, sep} from 'node:path';

const flags = new Map();
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i += 2) {
  const key = argv[i]?.replace(/^--/, '');
  if (key) flags.set(key, argv[i + 1]);
}

const PORT = Number(flags.get('port') || 18100);
const BACKEND_PORT = Number(flags.get('backend') || 18101);
const WEB_DIR = resolve(flags.get('dir') || 'apps/web/out');
const BACKEND = {host: '127.0.0.1', port: BACKEND_PORT};
const PROXY_PREFIXES = ['/api/', '/v1/', '/v2/', '/healthz', '/metrics'];

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.wasm': 'application/wasm',
  '.mp4': 'video/mp4',
  '.webmanifest': 'application/manifest+json',
};

const isProxied = (path) =>
  PROXY_PREFIXES.some((prefix) => path === prefix.replace(/\/$/, '') || path.startsWith(prefix));

/** 原样转发请求与响应；SSE 分块照抄，不在此层攒包。 */
const proxy = (req, res) => {
  const upstream = http.request(
    {
      ...BACKEND,
      method: req.method,
      path: req.url,
      headers: {...req.headers, host: `${BACKEND.host}:${BACKEND.port}`},
    },
    (upRes) => {
      res.writeHead(upRes.statusCode || 502, upRes.headers);
      upRes.pipe(res);
    },
  );
  upstream.setTimeout(0);
  upstream.on('error', (err) => {
    if (res.headersSent) {
      res.destroy(err);
      return;
    }
    res.writeHead(502, {'content-type': 'application/json; charset=utf-8'});
    res.end(
      JSON.stringify({
        ok: false,
        error: {
          code: 'web_proxy_unreachable',
          message: `控制面 ${BACKEND.host}:${BACKEND.port} 不可达：${err.message}`,
        },
      }),
    );
  });
  req.on('aborted', () => upstream.destroy());
  req.pipe(upstream);
};

const sendFile = (req, res, file) => {
  const headers = {'content-type': MIME[extname(file).toLowerCase()] || 'application/octet-stream'};
  res.writeHead(200, headers);
  if (req.method === 'HEAD') {
    res.end();
    return;
  }
  createReadStream(file).pipe(res);
};

const serveStatic = (req, res) => {
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  } catch {
    res.writeHead(400).end('bad request');
    return;
  }
  const rel = normalize(pathname).replace(/^([/\\])+/, '');
  const target = resolve(join(WEB_DIR, rel));
  if (target !== WEB_DIR && !target.startsWith(WEB_DIR + sep)) {
    res.writeHead(403).end('forbidden');
    return;
  }
  // Next 静态导出用 trailingSlash：/login/ 落成 /login/index.html，/login 也能命中
  const candidates = [target, join(target, 'index.html'), `${target}.html`];
  for (const file of candidates) {
    if (existsSync(file) && statSync(file).isFile()) {
      sendFile(req, res, file);
      return;
    }
  }
  const notFound = join(WEB_DIR, '404.html');
  if (existsSync(notFound)) {
    res.writeHead(404, {'content-type': 'text/html; charset=utf-8'});
    createReadStream(notFound).pipe(res);
    return;
  }
  res.writeHead(404, {'content-type': 'text/plain; charset=utf-8'}).end('404 Not Found');
};

const server = http.createServer((req, res) =>
  isProxied(req.url.split('?')[0]) ? proxy(req, res) : serveStatic(req, res),
);
// 流式响应可能持续很久，别让 Node 的默认空闲超时把连接切掉
server.timeout = 0;
server.requestTimeout = 0;

server.listen(PORT, '127.0.0.1', () => {
  console.log(`前端静态服务 http://127.0.0.1:${PORT}/ -> 控制面 127.0.0.1:${BACKEND_PORT}`);
});
