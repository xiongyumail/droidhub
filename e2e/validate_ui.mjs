// 只读验证名称、切换和序列号路由；点屏请求截获，不操作真实手机。
// PLAYWRIGHT_MODULE=/path/to/playwright node e2e/validate_ui.mjs
// DROIDHUB_URL=http://<地址>:8791 为线上复验；不设时验本地 HTML。
import assert from 'node:assert/strict';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
let server, mockPhones = null;   // 本地假服务的手机列表；线上复验时为 null
let url = process.env.DROIDHUB_URL;
if (!url) {
  const page = await readFile(new URL('../index.html', import.meta.url));
  const portrait = await readFile(new URL('fixtures/portrait.jpg', import.meta.url));
  const phones = mockPhones = { default: '9e24cca', phones: [
    { serial: '9e24cca', name: 'P1', connected: true, size: [1080, 2340] },
    { serial: 'b02683d', name: 'P2', connected: true, size: [1080, 2340] },
  ] };
  server = http.createServer((req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname;
    if (path === '/') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(page);
    } else if (path === '/phones') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(phones));
    } else if (path === '/lease') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
    } else if (path === '/stream') {
      res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Cache-Control': 'no-store' });
      res.end(portrait);
    } else {
      res.writeHead(404); res.end();
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  url = 'http://127.0.0.1:' + server.address().port;
}
const browser = await chromium.launch({ headless: true, executablePath: process.env.DROIDHUB_CHROMIUM,
                                        args: ['--no-sandbox'] });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const touches = [], keys = [], inputs = [], shots = [], owners = new Set();
  await context.route('**/touch**', async route => {
    owners.add(new URL(route.request().url()).searchParams.get('owner'));
    const action = { serial: new URL(route.request().url()).searchParams.get('serial'),
                     type: route.request().postDataJSON().type };
    touches.push(action); inputs.push(action);
    await route.fulfill({ contentType: 'application/json', body: '{"ok":true}' });
  });
  await context.route('**/key**', async route => {
    const action = { serial: new URL(route.request().url()).searchParams.get('serial'),
                     key: route.request().postDataJSON().key };
    keys.push(action); inputs.push(action);
    await route.fulfill({ contentType: 'application/json', body: '{"ok":true}' });
  });
  await context.route('**/shot**', async route => {
    shots.push(new URL(route.request().url()).searchParams.get('serial'));
    await new Promise(resolve => setTimeout(resolve, 100));
    await route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2uoAAAAASUVORK5CYII=', 'base64') });
  });
  const page = await context.newPage();
  async function clickTool(id) {
    if (!await page.locator('#' + id).isVisible()) await page.getByRole('button', { name: '更多操作', exact: true }).click();
    await page.locator('#' + id).click();
  }
  const errors = [];
  const streamRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/stream') streamRequests.push(request.url());
  });
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  assert.equal(await page.locator('h1').textContent(), 'DroidHub');
  await page.waitForFunction(() => document.querySelectorAll('#tabs button').length === 2);
  assert.deepEqual(await page.locator('#tabs button').allTextContents(), ['P1', 'P2']);
  await page.getByRole('button', { name: 'P2', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#tabs button.on')?.textContent === 'P2'
                                  && document.querySelector('#img').src.includes('serial=b02683d'));
  assert.equal(await page.evaluate(() => localStorage.getItem('serial')), 'b02683d');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('#tabs button.on')?.textContent === 'P2');
  await page.waitForFunction(() => document.querySelector('#img').naturalWidth > 0, null, { timeout: 15000 });
  await page.locator('#screen').click();
  await page.waitForFunction(() => !sending);
  assert.deepEqual(touches, [{ serial: 'b02683d', type: 'down' }, { serial: 'b02683d', type: 'up' }]);
  await page.getByRole('button', { name: 'P1', exact: true }).click();

  // 快捷键必须排在原手机的松手之后；切换不能把排队操作发给另一台。
  inputs.length = 0;
  await page.evaluate(() => {
    finger = { id: 1, x: 100, y: 200 };
    send({ type: 'down', x: 100, y: 200 });
    send({ type: 'move', x: 120, y: 220 });
    document.querySelector('#home').click();
    pick('b02683d');
    document.querySelector('#back').click();
  });
  await page.waitForFunction(() => !sending);
  assert.deepEqual(inputs.filter(input => input.serial === '9e24cca'), [
    ...['down', 'move', 'up'].map(type => ({ serial: '9e24cca', type })),
    { serial: '9e24cca', key: 'HOME' },
  ]);
  assert.deepEqual(keys.filter(input => input.serial === 'b02683d'), [{ serial: 'b02683d', key: 'BACK' }]);

  // 截图下载过程中切换，文件名和请求仍对应发起截图的手机。
  const downloadPromise = page.waitForEvent('download');
  await clickTool('shot');
  await page.getByRole('button', { name: 'P1', exact: true }).click();
  const download = await downloadPromise;
  assert.ok(download.suggestedFilename().startsWith('DroidHub-P2-'));
  assert.deepEqual(shots, ['b02683d']);
  assert.ok((await readFile(await download.path())).subarray(0, 4).equals(Buffer.from([137, 80, 78, 71])));

  // 轮询保持键盘焦点；方向键切换设备，选中状态可被辅助技术读取。
  await page.getByRole('button', { name: 'P1', exact: true }).focus();
  await page.evaluate(() => status());
  assert.equal(await page.evaluate(() => document.activeElement.dataset.serial), '9e24cca');
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('button', { name: 'P2', exact: true }).getAttribute('aria-pressed'), 'true');
  await page.keyboard.press('ArrowLeft');

  // 真实全屏 API；Esc/退出后按钮状态恢复。
  if (await page.evaluate(() => document.fullscreenEnabled)) {
    await page.getByRole('button', { name: '进入全屏', exact: true }).click();
    await page.waitForFunction(() => !!document.fullscreenElement);
    assert.equal(await page.locator('#fullscreen').getAttribute('aria-pressed'), 'true');
    await page.getByRole('button', { name: '退出全屏', exact: true }).click();
    await page.waitForFunction(() => !document.fullscreenElement);
  }

  for (const [width, height] of [[320, 780], [390, 844], [1280, 720], [844, 390]]) {
    await page.setViewportSize({ width, height });
    const clipped = await page.evaluate(() => [...document.querySelectorAll('header, .brand, .right, .tools, #screen')]
      .filter(element => {
        const r = element.getBoundingClientRect();
        return r.left < -.5 || r.right > innerWidth + .5 || r.top < -.5 || r.bottom > innerHeight + .5;
      }).map(element => element.className || element.tagName));
    assert.deepEqual(clipped, [], '布局超出视口 ' + width + '×' + height);
  }
  await page.setViewportSize({ width: 390, height: 844 });

  // 真机比例的画面至少恢复到旧版面积；操作菜单不挤占画面，点画面收起菜单不点手机。
  const mobile = await page.evaluate(() => {
    const r = screenEl.getBoundingClientRect(), image = document.querySelector('#img');
    return { width: r.width, height: r.height, ratio: image.naturalWidth / image.naturalHeight,
             header: document.querySelector('header').getBoundingClientRect().height };
  });
  assert.equal(mobile.header, 44);
  assert.ok(mobile.width >= Math.min(390 - 32, (844 - 44 - 12) * mobile.ratio) - .5, JSON.stringify(mobile));
  const beforeDismiss = touches.length;
  await page.getByRole('button', { name: '更多操作', exact: true }).click();
  assert.equal(await page.locator('#toolsToggle').getAttribute('aria-expanded'), 'true');
  assert.ok(Math.abs((await page.locator('#screen').boundingBox()).width - mobile.width) < .5);
  await page.locator('#screen').click({ position: { x: 20, y: 180 } });
  assert.equal(await page.locator('#toolsToggle').getAttribute('aria-expanded'), 'false');
  assert.equal(touches.length, beforeDismiss, '收起菜单的点击不能穿透到手机');

  // 不支持系统全屏时仍能铺满网页；有退出入口和 Esc，触摸手势在切换前释放。
  await page.evaluate(() => Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: false }));
  await page.evaluate(() => { finger = { id: 1, x: 100, y: 200 }; send({ type: 'down', x: 100, y: 200 }); });
  await page.getByRole('button', { name: '进入全屏', exact: true }).click();
  await page.waitForFunction(() => mainEl.classList.contains('view-full') && !sending);
  assert.equal(await page.evaluate(() => document.fullscreenElement), null);
  assert.deepEqual(touches.slice(-2).map(input => input.type), ['down', 'up']);
  const full = await page.evaluate(() => {
    const r = screenEl.getBoundingClientRect();
    return { width: r.width, height: r.height, expected: Math.min(innerWidth, innerHeight * document.querySelector('#img').naturalWidth / document.querySelector('#img').naturalHeight) };
  });
  assert.ok(Math.abs(full.width - full.expected) < .5, JSON.stringify(full));
  assert.ok(full.width >= mobile.width && full.height >= mobile.height);
  assert.equal(await page.locator('header').isVisible(), false);
  await page.getByRole('button', { name: '退出全屏', exact: true }).click();
  assert.equal(await page.locator('header').isVisible(), true);
  await page.evaluate(() => delete document.fullscreenEnabled);

  // API 存在但被浏览器拒绝，也保持可用的铺满模式。
  await page.evaluate(() => { mainEl.requestFullscreen = () => Promise.reject(new Error('fullscreen denied')); });
  await page.getByRole('button', { name: '进入全屏', exact: true }).click();
  await page.waitForFunction(() => mainEl.classList.contains('view-full') && !fullscreenBusy);
  assert.equal(await page.evaluate(() => document.fullscreenElement), null);
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => mainEl.classList.contains('view-full')), false);
  await page.evaluate(() => delete mainEl.requestFullscreen);

  if (server) {
    await page.route('**/phones', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ default: '9e24cca', phones: [
      { serial: '9e24cca', name: 'P1', connected: true, size: [1080, 2340] },
      { serial: 'b02683d', name: 'P2', connected: false, error: "adb: device unauthorized. This adb server's $ADB_VENDOR_KEYS is not set" },
    ] }) }));
    await page.evaluate(() => status());
    await page.getByRole('button', { name: 'P2', exact: true }).click();
    assert.equal(await page.locator('#veilTitle').textContent(), '等待手机授权');
    assert.ok(!(await page.locator('#veil').textContent()).includes('ADB_VENDOR_KEYS'));
    for (const id of ['back', 'home', 'shot', 'sound']) assert.equal(await page.locator('#' + id).isDisabled(), true);
    const before = inputs.length;
    await page.evaluate(() => { document.querySelector('#back').click(); document.querySelector('#home').click(); });
    assert.equal(inputs.length, before);
    await page.unroute('**/phones');
    await page.evaluate(() => status());
    await page.getByRole('button', { name: 'P1', exact: true }).click();
  }
  await page.waitForFunction(() => document.querySelector('#tabs button.on')?.textContent === 'P1'
                                  && document.querySelector('#img').src.includes('serial=9e24cca'));
  await page.waitForFunction(() => document.querySelector('#veil').classList.contains('hide')
                                  && document.querySelector('#img').naturalWidth > 0, null, { timeout: 15000 });
  assert.equal(await page.evaluate(() => localStorage.getItem('serial')), '9e24cca');

  // 两路都加载好后，切换必须复用同一个实时图像，不发 /phones 或新的 /stream。
  await page.waitForFunction(() => [...document.querySelectorAll('#screen img')].length === 2
                                  && [...document.querySelectorAll('#screen img')].every(img => img.naturalWidth > 0),
                            null, { timeout: 15000 });
  await page.route('**/phones', route => route.abort());
  const connections = streamRequests.length;
  const switches = await page.evaluate(async () => {
    const images = new Map([...document.querySelectorAll('#screen img')].map(img => [new URL(img.src).searchParams.get('serial'), img]));
    const results = [];
    for (const [name, target] of [['P2', 'b02683d'], ['P1', '9e24cca'], ['P2', 'b02683d'], ['P1', '9e24cca']]) {
      const started = performance.now();
      [...document.querySelectorAll('#tabs button')].find(button => button.textContent === name).click();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      results.push({ name, ms: Math.round(performance.now() - started),
                     reused: document.querySelector('#img') === images.get(target),
                     ready: document.querySelector('#veil').classList.contains('hide'),
                     visible: [...document.querySelectorAll('#screen img')].filter(img => !img.hidden).length });
    }
    return results;
  });
  assert.ok(switches.every(s => s.reused && s.ready && s.visible === 1), JSON.stringify(switches));
  assert.equal(streamRequests.length, connections);
  await page.unroute('**/phones');

  // 按住 P1 时切到 P2，排队的 move/up 仍发给 P1。
  touches.length = 0;
  await page.evaluate(() => {
    finger = { id: 1, x: 100, y: 200 };
    send({ type: 'down', x: 100, y: 200 });
    send({ type: 'move', x: 120, y: 220 });
    pick('b02683d');
  });
  await page.waitForFunction(() => !sending);
  assert.deepEqual(touches, ['down', 'move', 'up'].map(type => ({ serial: '9e24cca', type })));
  await page.getByRole('button', { name: 'P1', exact: true }).click();
  assert.deepEqual([...owners], ['web']);

  // 别的程序占着时只能看：角标显示占用方，按钮禁用，点画面不发输入。
  if (mockPhones) {
  mockPhones.phones[0].lease = { owner: 'tygbot', expires_in: 20 };
  await page.evaluate(() => status());
  await page.waitForFunction(() => document.querySelector('#badge').textContent === 'tygbot 使用中');
  assert.equal(await page.locator('#home').isDisabled(), true);
  touches.length = 0;
  await page.locator('#screen').click();
  await page.waitForFunction(() => !sending);
  assert.deepEqual(touches, []);
  assert.equal(await page.locator('#img').evaluate(img => img.naturalWidth > 0), true);
  delete mockPhones.phones[0].lease;
  await page.evaluate(() => status());
  await page.waitForFunction(() => document.querySelector('#badge').textContent === '已连接');
  }

  // 后台释放全部连接，回到前台再连。
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  assert.equal(await page.locator('#screen img[src]').count(), 0);
  await page.evaluate(() => {
    delete document.hidden;
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForFunction(() => document.querySelector('#veil').classList.contains('hide')
                                  && document.querySelector('#img').naturalWidth > 0, null, { timeout: 15000 });
  assert.deepEqual(errors, []);
  if (process.env.DROIDHUB_SCREENSHOT) await page.screenshot({ path: process.env.DROIDHUB_SCREENSHOT });
  console.log('PASS: DroidHub 名称、切换复用、输入/快捷键路由、截图下载、系统全屏/铺满回退、手机画面面积、菜单无点击穿透、焦点、响应式布局、断线提示与后台释放、他人占用时只读；手机输入已截获');
  console.log('切换至下一次绘制（两次 requestAnimationFrame）:', JSON.stringify(switches));
  await context.close();
} finally {
  await browser.close();
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}
