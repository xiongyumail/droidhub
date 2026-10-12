// 用真实 Web Audio 验证 PCM 解码、声源切换、后台释放和失败提示；不操作手机。
// PLAYWRIGHT_MODULE=/path/to/playwright DROIDHUB_CHROMIUM=/path/to/chromium node e2e/validate_audio.mjs
import assert from 'node:assert/strict';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html = await readFile(new URL('../index.html', import.meta.url));
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2uoAAAAASUVORK5CYII=', 'base64');
const active = new Set();
let requests = 0, deny = false;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(html);
  } else if (url.pathname === '/phones') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ default: '9e24cca', phones: [
      { serial: '9e24cca', name: 'P1', connected: true, size: [1080, 2340] },
      { serial: 'b02683d', name: 'P2', connected: true, size: [1080, 2340] },
    ] }));
  } else if (url.pathname === '/stream') {
    res.writeHead(200, { 'Content-Type': 'image/png' }); res.end(pixel);
  } else if (url.pathname === '/audio') {
    requests++;
    if (deny) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: '采音权限测试错误' })); return;
    }
    const serial = url.searchParams.get('serial');
    const session = { serial }; active.add(session);
    res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store',
      'X-Audio-Format': 's16le', 'X-Audio-Sample-Rate': '48000', 'X-Audio-Channels': '2' });
    const pcm = Buffer.alloc(3840);
    const [left, right] = serial === '9e24cca' ? [8192, -16384] : [16384, -8192];
    for (let offset = 0; offset < pcm.length; offset += 4) {
      pcm.writeInt16LE(left, offset); pcm.writeInt16LE(right, offset + 2);
    }
    // 立体声帧在奇数字节处拆开；随后每 20ms 提供一段声音。
    res.write(pcm.subarray(0, 3));
    const second = setTimeout(() => res.write(pcm.subarray(3, 8)), 20);
    const third = setTimeout(() => res.write(pcm.subarray(8)), 40);
    let interval;
    const fourth = setTimeout(() => { res.write(pcm); interval = setInterval(() => res.write(pcm), 20); }, 60);
    res.on('close', () => {
      clearTimeout(second); clearTimeout(third); clearTimeout(fourth); clearInterval(interval); active.delete(session);
    });
  } else { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true, executablePath: process.env.DROIDHUB_CHROMIUM,
  args: ['--no-sandbox'] });
async function waitForActive(serial) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const expected = serial ? [serial] : [];
    if (JSON.stringify([...active].map(s => s.serial).sort()) === JSON.stringify(expected)) return;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.fail('音频连接未释放或串台: ' + JSON.stringify([...active]));
}
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await context.addInitScript(() => {
    window.audioProbe = { buffers: [], stopped: 0 };
    const create = AudioContext.prototype.createBufferSource;
    AudioContext.prototype.createBufferSource = function () {
      const source = create.call(this), start = source.start, stop = source.stop;
      source.start = function (...args) {
        audioProbe.buffers.push({ serial: window.eval('serial'), left: this.buffer.getChannelData(0)[0],
          right: this.buffer.getChannelData(1)[0], rate: this.buffer.sampleRate,
          channels: this.buffer.numberOfChannels, frames: this.buffer.length });
        return start.apply(this, args);
      };
      source.stop = function (...args) { audioProbe.stopped++; return stop.apply(this, args); };
      return source;
    };
  });
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:' + server.address().port, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('#tabs button').length === 2);
  assert.equal(requests, 0, '默认不采音');
  await page.getByRole('button', { name: '开启声音', exact: true }).click();
  await page.waitForFunction(() => audioProbe.buffers.length >= 2);
  await waitForActive('9e24cca');
  const p1 = await page.evaluate(() => audioProbe.buffers.slice());
  assert.ok(p1.every(b => b.left === .25 && b.right === -.5 && b.rate === 48000 && b.channels === 2));
  assert.equal(await page.locator('#sound').textContent(), '声音开');
  await page.evaluate(() => { audioProbe.buffers = []; });
  await page.getByRole('button', { name: 'P2', exact: true }).click();
  await page.waitForFunction(() => audioProbe.buffers.some(b => b.serial === 'b02683d'));
  await waitForActive('b02683d');
  assert.ok(await page.evaluate(() => audioProbe.buffers.filter(b => b.serial === 'b02683d')
    .every(b => b.left === .5 && b.right === -.25)), '只播放当前手机');
  assert.ok(await page.evaluate(() => audioProbe.stopped > 0), '切换时取消旧手机排队的声音');

  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await waitForActive(null);
  await page.waitForFunction(() => audioContext.state === 'suspended');
  await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
  await waitForActive('b02683d');
  await page.waitForFunction(() => document.querySelector('#sound').textContent === '声音开');
  await page.getByRole('button', { name: '关闭声音', exact: true }).click();
  await waitForActive(null);
  assert.equal(await page.locator('#sound').textContent(), '声音关');

  deny = true;
  const count = requests;
  await page.getByRole('button', { name: '开启声音', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#sound').textContent === '声音关'
    && document.querySelector('#toast').textContent.includes('采音权限测试错误'));
  assert.equal(requests, count + 1, '失败不反复自动重连');
  await waitForActive(null);
  deny = false;
  await page.getByRole('button', { name: '开启声音', exact: true }).click();
  await waitForActive('b02683d');
  await context.close();
  await waitForActive(null);
  assert.deepEqual(errors, []);
  console.log('PASS: 实际 Web Audio PCM 双声道解码、P1/P2 切换、旧声音停止、后台/关闭释放、错误提示');
} finally {
  await browser.close(); server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
