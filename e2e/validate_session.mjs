// 真实 MJPEG：正常 EOF、突然断开、其他订阅仍在线、静止帧、横屏和乱序状态；仅用本地假手机。
import assert from 'node:assert/strict';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html = await readFile(new URL('../index.html', import.meta.url));
const frames = await Promise.all(['portrait', 'landscape', 'landscape-new'].map(name => readFile(new URL('fixtures/' + name + '.jpg', import.meta.url))));
const state = { instance: 'boot-1', since: 1, online: true, rotation: 0, frame: 0,
                streams: new Map(), others: new Set(), requests: 0, hold: false, deferred: null, statusCode: 200 };
const touches = [], errors = [];
function snapshot() {
  const clients = [...state.streams.keys(), ...state.others];
  return { instance: state.instance, default: '9e24cca', phones: [{ serial: '9e24cca', name: 'P1',
    connected: state.online, size: [1080, 2340], rotation: state.rotation, since: state.since,
    error: state.online ? null : 'offline',
    stream: { state: clients.length ? 'streaming' : 'idle', clients, viewers: clients.length, error: null, frame_age: 600 } }] };
}
function packet(index) {
  const frame = frames[index];
  return Buffer.concat([Buffer.from('--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + frame.length + '\r\n\r\n'), frame, Buffer.from('\r\n')]);
}
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); res.end(html); }
  else if (url.pathname === '/phones') {
    const body = JSON.stringify(snapshot()), code = state.statusCode;
    if (state.hold) { state.hold = false; state.deferred = { res, body, code }; }
    else { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(body); }
  } else if (url.pathname === '/stream') {
    state.requests++;
    if (!state.online) { res.writeHead(503, { 'Content-Type': 'application/json' }); res.end('{"detail":"offline"}'); return; }
    const client = url.searchParams.get('client');
    assert.ok(client);
    state.streams.set(client, res);
    let count = 0, last = state.frame;
    // 使用 close-delimited 响应：这是 Chromium 容易保留旧图、不触发 error 的断流方式。
    res.useChunkedEncodingByDefault = false;
    res.writeHead(200, { 'Content-Type': 'multipart/x-mixed-replace; boundary=frame', 'Connection': 'close' });
    res.write(packet(last));
    const timer = setInterval(() => {
      if (last !== state.frame) count = 0;
      if (count++ < 2) { last = state.frame; if (!res.destroyed) res.write(packet(last)); }
    }, 80);
    res.on('close', () => { clearInterval(timer); state.streams.delete(client); });
  } else if (url.pathname === '/lease') {
    res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}');
  } else if (url.pathname === '/touch') {
    let body = '';
    req.on('data', data => { body += data; });
    req.on('end', () => { touches.push(JSON.parse(body)); res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}'); });
  } else { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true, executablePath: process.env.DROIDHUB_CHROMIUM, args: ['--no-sandbox'] });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const reports = [];
function closeStreams(mode) {
  for (const [client, res] of [...state.streams]) {
    state.streams.delete(client);
    if (mode === 'eof') res.end(); else res.destroy();
  }
}
function releaseSnapshot() {
  const { res, body, code } = state.deferred;
  state.deferred = null;
  res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(body);
}
async function pixel(page) {
  return page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d'); ctx.drawImage(document.querySelector('#img'), 0, 0, 1, 1);
    return ctx.getImageData(0, 0, 1, 1).data[0];
  });
}
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:' + server.address().port, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => feeds.get(serial)?.ready && document.querySelector('#img').naturalWidth === 736);
  await page.evaluate(() => status()); // 收到本页订阅标识。

  const staticRequests = state.requests;
  await delay(3200); // 不再发送新帧，保持有效连接；frame_age 故意远大于旧超时。
  await page.evaluate(() => status());
  assert.equal(state.requests, staticRequests);
  assert.equal(await page.locator('#badge').textContent(), '已连接');
  reports.push('静止帧保持订阅');

  // 同一条 MJPEG 连接从竖屏变横屏；方向元数据尚未更新时，禁止新手势。
  touches.length = 0;
  const rotationRequests = state.requests;
  state.frame = 1;
  await page.waitForFunction(() => document.querySelector('#img').naturalWidth === 1600);
  await page.waitForFunction(() => {
    const r = document.querySelector('#screen').getBoundingClientRect();
    return Math.abs(r.width / r.height - 1600 / 736) < .01;
  });
  await page.locator('#screen').click({ position: { x: 150, y: 150 } });
  assert.equal(touches.length, 0, '新方向未确认前不能误点');
  state.rotation = 1;
  await page.evaluate(() => status());
  const mapped = await page.evaluate(() => {
    const r = screenEl.getBoundingClientRect();
    return toPhone({ clientX: r.left + r.width * .25, clientY: r.top + r.height * .75 });
  });
  assert.ok(Math.abs(mapped.x - 270) <= 1 && Math.abs(mapped.y - 585) <= 1, JSON.stringify(mapped));
  assert.equal(state.requests, rotationRequests);
  assert.equal(await page.locator('#img').evaluate(img => getComputedStyle(img).objectFit), 'contain');

  const bounds = await page.locator('#screen').boundingBox();
  await page.mouse.move(bounds.x + bounds.width * .25, bounds.y + bounds.height * .75);
  await page.mouse.down();
  await page.waitForFunction(() => finger !== null && !sending);
  state.rotation = 3; // 方向翻转但横屏尺寸相同，也须结束旧手势。
  await page.evaluate(() => status());
  await page.waitForFunction(() => finger === null && !sending);
  await page.mouse.up();
  assert.deepEqual(touches.map(t => t.type), ['down', 'up']);
  for (const [rotation, frame, expected] of [[3, 1, [810, 1755]], [2, 0, [810, 585]], [0, 0, [270, 1755]]]) {
    state.rotation = rotation; state.frame = frame;
    await page.evaluate(() => status());
    await page.waitForFunction(expected => document.querySelector('#img').naturalWidth === expected, frame === 0 ? 736 : 1600);
    const point = await page.evaluate(() => {
      const r = screenEl.getBoundingClientRect();
      return toPhone({ clientX: r.left + r.width * .25, clientY: r.top + r.height * .75 });
    });
    assert.ok(Math.abs(point.x - expected[0]) <= 1 && Math.abs(point.y - expected[1]) <= 1, JSON.stringify({ rotation, point }));
  }
  assert.equal(state.requests, rotationRequests);
  reports.push('横屏无畸变，逆旋转坐标与方向变化保护');

  // 服务重启后另一页已开始看新流，仍须重连本页的旧画面。
  const beforeRestart = state.requests;
  state.instance = 'boot-2'; state.others.add('another-page'); state.frame = 2; state.rotation = 1;
  closeStreams('eof');
  await delay(100);
  await page.evaluate(() => status());
  await page.waitForFunction(() => feeds.get(serial)?.ready);
  assert.equal(state.requests, beforeRestart + 1);
  assert.ok(await pixel(page) > 170);
  reports.push('正常 EOF 后识别服务重启');

  // 不重启服务，只断本页连接；其他订阅还在，不可仅依据全局 streaming 判定。
  const beforeClose = state.requests;
  state.frame = 1;
  closeStreams('abrupt');
  await delay(100);
  await page.evaluate(() => status());
  await page.waitForFunction(() => feeds.get(serial)?.ready);
  assert.equal(state.requests, beforeClose + 1);
  assert.ok(await pixel(page) < 50);
  reports.push('其他订阅在线时恢复本页突然断流');

  const beforeIdle = state.requests;
  state.others.clear(); state.frame = 2;
  closeStreams('eof');
  await delay(100);
  await page.evaluate(() => status());
  await page.waitForFunction(() => feeds.get(serial)?.ready);
  assert.equal(state.requests, beforeIdle + 1);
  assert.ok(await pixel(page) > 170);
  reports.push('idle 且没有 error 时恢复旧画面');

  const beforePhone = state.requests;
  state.since++;
  await page.evaluate(() => status());
  await page.waitForFunction(() => feeds.get(serial)?.ready);
  assert.equal(state.requests, beforePhone + 1);
  reports.push('手机应用通道重建后换订阅');

  // 即使 fetch 忽略 abort，晚到的旧响应也不能覆盖较新状态或错误提示。
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = (url, options) => original(url, String(url).startsWith('/phones') ? { ...options, signal: undefined } : options);
  });
  state.hold = true;
  await page.evaluate(() => { window.heldStatus = status(); });
  while (!state.deferred) await delay(10);
  state.online = false;
  await page.evaluate(() => status());
  assert.equal(await page.evaluate(() => phones[0].connected), false);
  releaseSnapshot();
  await page.evaluate(() => window.heldStatus);
  assert.equal(await page.evaluate(() => phones[0].connected), false);

  state.statusCode = 503; state.hold = true;
  await page.evaluate(() => { window.heldStatus = status(); });
  while (!state.deferred) await delay(10);
  state.statusCode = 200; state.online = true;
  await page.evaluate(() => status());
  await page.waitForFunction(() => feeds.get(serial)?.ready && document.querySelector('#badge').textContent === '已连接');
  releaseSnapshot();
  await page.evaluate(() => window.heldStatus);
  assert.equal(await page.locator('#badge').textContent(), '已连接');
  reports.push('旧成功、旧失败响应均不能覆盖新状态');

  state.hold = true;
  await page.evaluate(() => { window.heldStatus = status(); });
  while (!state.deferred) await delay(10);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  releaseSnapshot();
  await page.evaluate(() => window.heldStatus);
  assert.equal(await page.locator('#screen img[src]').count(), 0);
  state.online = false;
  await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
  await page.waitForFunction(() => phones[0].connected === false);
  assert.equal(await page.locator('#screen img[src]').count(), 0);
  reports.push('后台取消轮询，回前台读取新状态');
  assert.deepEqual(errors, []);
  await page.close();
  console.log('PASS:', reports.join('；'));
} finally {
  await browser.close();
  closeStreams('abrupt');
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
