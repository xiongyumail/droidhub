// 网页登录：未登录打开页面转到登录页，密码正确后回到原页面并获得一年有效的 Cookie；Cookie 失效后请求返回 401 时回到登录页；登录后只跳回本站地址。
// 启动真实的 HTTP 服务（不接手机），密码为 pw。
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = fileURLToPath(new URL('..', import.meta.url));
const server = spawn('python3', ['-B', '-c', [
  'import app, config',
  "app.PASSWORD = config.hash_password('pw')",
  "s = app.HTTPServer(('127.0.0.1', 0), app.Handler)",
  'print(s.server_port, flush=True)',
  's.serve_forever()',
].join('\n')], { cwd: root, env: { ...process.env, DROIDHUB_CONFIG: '/nonexistent/config.json' },
                 stdio: ['ignore', 'pipe', 'inherit'] });
const port = await new Promise((resolve, reject) => {
  server.stdout.once('data', data => resolve(Number(String(data).trim())));
  server.once('exit', code => reject(new Error('服务退出：' + code)));
});
const url = 'http://127.0.0.1:' + port;
const browser = await chromium.launch({ headless: true, executablePath: process.env.DROIDHUB_CHROMIUM,
                                        args: ['--no-sandbox'] });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();

  await page.goto(url + '/?phone=P1');
  assert.equal(new URL(page.url()).pathname, '/auth');
  assert.equal(new URL(page.url()).searchParams.get('next'), '/?phone=P1');

  await page.fill('#password', 'bad');
  await page.click('#submit');
  await page.waitForFunction(() => document.getElementById('error').textContent === '密码错误');

  await page.fill('#password', 'pw');
  await Promise.all([page.waitForURL(url + '/?phone=P1'), page.click('#submit')]);
  const [cookie] = await context.cookies();
  assert.equal(cookie.name, 'droidhub_session');
  assert.ok(cookie.httpOnly);
  const days = (cookie.expires - Date.now() / 1000) / 86400;
  assert.ok(days > 364 && days <= 365, '有效期 ' + days + ' 天');

  await page.reload();
  assert.equal(new URL(page.url()).pathname, '/');

  await context.clearCookies();
  await Promise.all([page.waitForURL(/\/auth\?next=/), page.evaluate(() => fetch('/phones'))]);

  // next 指向别的网站时回到首页
  await page.goto(url + '/auth?next=' + encodeURIComponent('/\\example.com/'));
  await page.fill('#password', 'pw');
  await Promise.all([page.waitForURL(url + '/'), page.click('#submit')]);
  console.log('PASS: 未登录转到登录页、密码错误提示、登录后回到原页面、Cookie 一年有效、失效后回到登录页、不跳到别的网站');
} finally {
  await browser.close();
  server.kill();
}
