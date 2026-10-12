// 假手机验证故障隔离，并用真实 Python HTTP 处理器回归浏览器跨站控制。
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html = await readFile(new URL('../index.html', import.meta.url));
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2uoAAAAASUVORK5CYII=', 'base64');
const requests = [];
let held = null, unavailable = false;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); res.end(html); }
  else if (url.pathname === '/phones') {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ default: '9e24cca', phones: ['9e24cca', 'b02683d'].map((serial, i) => ({
      serial, name: 'P' + (i + 1), connected: !(i === 1 && unavailable), size: [1080, 2340],
    })) }));
  } else if (url.pathname === '/stream') { res.setHeader('Content-Type', 'image/png'); res.end(pixel); }
  else if (url.pathname === '/lease') { res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}'); }
  else if (url.pathname === '/touch') {
    let body = '';
    req.on('data', data => body += data);
    req.on('end', () => {
      const event = JSON.parse(body), serial = url.searchParams.get('serial');
      requests.push({ serial, type: event.type });
      if (requests.length === 1) held = res;
      else { res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}'); }
    });
  } else { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true, executablePath: process.env.DROIDHUB_CHROMIUM,
  args: ['--no-sandbox'] });
let backend;
async function waitUntil(predicate, timeout = 2000) {
  const end = Date.now() + timeout;
  while (!predicate() && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 20));
  assert.ok(predicate());
}
try {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:' + server.address().port, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => feeds.size === 2 && [...feeds.values()].every(feed => feed.ready));
  await page.evaluate(() => {
    finger = { id: 1, x: 100, y: 100 };
    send({ type: 'down', x: 100, y: 100 });
  });
  await waitUntil(() => held);
  await page.getByRole('button', { name: 'P2', exact: true }).click();
  await page.locator('#screen').click();
  await waitUntil(() => requests.filter(r => r.serial === 'b02683d').length === 2);
  assert.equal(held.writableEnded, false, 'P1 还在等待时 P2 已能操作');
  assert.deepEqual(requests.filter(r => r.serial === 'b02683d').map(r => r.type), ['down', 'up']);
  await page.waitForFunction(() => document.querySelector('#toast').textContent === '操作超时', null, { timeout: 7000 });
  await page.waitForFunction(() => !sending);
  assert.equal(requests.filter(r => r.serial === '9e24cca' && r.type === 'down').length, 1, '不重放超时动作');
  assert.ok(requests.some(r => r.serial === '9e24cca' && r.type === 'up'), '失败后仍发送松手');

  unavailable = true;
  await page.evaluate(() => status());
  assert.equal(await page.locator('#badge').textContent(), '未连接');
  const previous = requests.length;
  await page.locator('#screen').click();
  assert.equal(requests.length, previous, '未连接的手机不接受画面点击');
  unavailable = false;
  await page.evaluate(() => status());
  await page.waitForFunction(() => feeds.get('b02683d')?.ready);
  assert.deepEqual(errors, []);

  backend = spawn('python3', ['-u', '-c', `
import sys,json,threading
from unittest.mock import MagicMock
import app
p=app.Phone('test',27183)
p.ensure=MagicMock(return_value=True)
p.touch=MagicMock(w=1080,h=2340)
p.error=None
app.fleet.phones['test']=p
class Handler(app.Handler):
 def do_GET(self):
  if self.path=='/evidence': return self._json(200,{'calls':p.touch.tap.call_count})
  return super().do_GET()
server=app.HTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True)
thread.start()
print(server.server_port,flush=True)
sys.stdin.read()
server.shutdown();server.server_close()
`], { cwd: fileURLToPath(new URL('..', import.meta.url)), stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: backend.stdout });
  const [line] = await once(lines, 'line'), port = Number(line);
  assert.ok(port > 0);
  const target = 'http://127.0.0.1:' + port;
  const type = await page.evaluate(async target => (await fetch(target + '/tap', {
    method: 'POST', mode: 'no-cors', headers: { 'Content-Type': 'text/plain' },
    body: JSON.stringify({ x: 100, y: 100 }),
  })).type, target);
  assert.equal(type, 'opaque');
  const evidence = await new Promise((resolve, reject) => {
    http.get(target + '/evidence', response => {
      let body = ''; response.on('data', data => body += data);
      response.on('end', () => resolve(JSON.parse(body)));
    }).on('error', reject);
  });
  assert.equal(evidence.calls, 0, '跨站浏览器请求未执行手机动作');
  lines.close();
  console.log('PASS: P1 超时期间 P2 可操作、超时不重放且松手、离线禁止触摸、真实浏览器跨站请求被阻止');
} finally {
  await browser.close();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  if (backend) { const exit = once(backend, 'exit'); backend.stdin.end(); await exit; }
}
