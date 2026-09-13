// One real frontend analysis, then read-only rendering of the same completed job.
// No traces, HAR, HTML or raw browser/network error logs.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(require.resolve('@playwright/test', { paths: [path.resolve(__dirname, '../../life-circle-demo')] }));
const base = 'http://127.0.0.1:8019';
const origin = { lng: 121.513926, lat: 31.313077 };

async function main() {
  const runDir = process.argv[2];
  if (!runDir || !path.isAbsolute(runDir) || !fs.existsSync(runDir)) throw new Error('run_directory_required');
  const save = (name, value) => fs.writeFileSync(path.join(runDir, name), JSON.stringify(value, null, 2));
  const temp = path.join(runDir, 'browser-temp'); fs.mkdirSync(temp, { recursive: true });
  process.env.TEMP = temp; process.env.TMP = temp;
  const report = { stage: 'starting', sdkLoads: 0, pageErrors: 0, dialogs: 0, creates: 0, polls: [], checks: {} };
  const renderOnly = process.argv.includes('--render-only');
  const state = await (await fetch(base + '/live/status')).json();
  if (!renderOnly && (!state.cancel_passed || state.halted || state.analysis_started)) throw new Error('analysis_not_allowed');
  const browser = await chromium.launch({ channel: 'msedge', headless: true,
    args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--disk-cache-size=1'] });
  let result;
  try {
    async function openPage(viewport, replayTask) {
      const context = await browser.newContext({ viewport });
      await context.addInitScript(() => {
        let callback;
        Object.defineProperty(window, '__baiduMapReady__', { configurable: true,
          get() { return callback; }, set(fn) {
            callback = function (...args) {
              const B = window.BMapGL;
              const polygons = new WeakMap();
              B.Polygon = new Proxy(B.Polygon, { construct(Target, args) {
                const polygon = Reflect.construct(Target, args);
                polygons.set(polygon, { paths: args[0], options: args[1] }); return polygon;
              }});
              B.Map = new Proxy(B.Map, { construct(Target, args) {
                const map = Reflect.construct(Target, args);
                window.__liveMap = map; window.__livePolygons = [];
                const add = map.addOverlay, clear = map.clearOverlays;
                map.addOverlay = function (overlay) {
                  if (polygons.has(overlay)) window.__livePolygons.push(polygons.get(overlay));
                  return add.call(this, overlay);
                };
                map.clearOverlays = function () { window.__livePolygons = []; return clear.call(this); };
                return map;
              }});
              return fn.apply(this, args);
            };
          }
        });
      });
      const page = await context.newPage();
      page.on('pageerror', () => report.pageErrors++);
      page.on('dialog', async dialog => { report.dialogs++; await dialog.dismiss(); });
      page.on('response', async response => {
        const url = new URL(response.url());
        if (url.hostname === 'api.map.baidu.com' && url.pathname === '/api') report.sdkLoads++;
        if (!replayTask && url.origin === base && url.pathname === '/api/analyses' && response.request().method() === 'POST') report.creates++;
        if (!replayTask && url.origin === base && /^\/api\/analyses\/[^/]+$/.test(url.pathname) && response.request().method() === 'GET') {
          try { report.polls.push({ at: Date.now(), http: response.status(), task: await response.json() }); } catch {}
        }
      });
      if (replayTask) {
        // Render the existing task through the unchanged UI; never POST a new job.
        await page.route(base + '/api/analyses', route => route.request().method() === 'POST'
          ? route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(replayTask) }) : route.continue());
      }
      await page.goto('http://127.0.0.1:5173', { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => !!window.__liveMap, undefined, { timeout: 20000 });
      if (replayTask) await page.evaluate(() => {
        const banner = document.createElement('div'); banner.textContent = '已完成真实任务的显示复核 · 不创建新任务';
        banner.style = 'padding:10px;background:#fff4cd;text-align:center;font-size:13px'; document.body.prepend(banner);
      });
      await page.getByRole('spinbutton', { name: '经度', exact: true }).fill(String(origin.lng));
      await page.getByRole('spinbutton', { name: '纬度', exact: true }).fill(String(origin.lat));
      await page.getByRole('combobox', { name: '调用预算' }).click();
      await page.getByText('200 次', { exact: true }).last().click();
      return page;
    }
    async function screenshot(page, name) {
      if (/[A-Za-z0-9_-]{40,}/.test(await page.locator('body').innerText())) throw new Error('unsafe_visible_text');
      await page.screenshot({ path: path.join(runDir, name), fullPage: true });
    }
    let task;
    if (renderOnly) {
      result = JSON.parse(fs.readFileSync(path.join(runDir, 'T03-result.json'), 'utf8'));
      task = await (await fetch(base + `/api/analyses/${result.taskId}`)).json();
    }
    report.stage = 'desktop';
    const page = await openPage({ width: 1440, height: 1000 }, task);
    const responseReady = page.waitForResponse(r => new URL(r.url()).origin === base && /\/result$/.test(new URL(r.url()).pathname) && r.status() === 200, { timeout: 650000 });
    // Exactly one click; no retry on an analysis error or timeout.
    await page.getByRole('button', { name: '开始分析', exact: true }).click();
    report.stage = 'waiting_for_result'; save('T03-browser-progress.json', report);
    const response = await responseReady; result = await response.json(); save('T03-result.json', result);
    await page.getByText('分析完成', { exact: true }).waitFor({ timeout: 10000 });
    report.stage = 'checking_layers';
    report.checks.source = result.dataSource === 'baidu_walking';
    report.checks.center = JSON.stringify(result.center) === JSON.stringify(origin);
    report.checks.coordinateSystem = result.isochrone.coordinateSystem === 'bd09ll';
    report.checks.facilities = await page.getByText('设施统计尚未接入', { exact: true }).isVisible();
    await page.waitForTimeout(1500);
    await screenshot(page, 'analysis-desktop.png');
    const colors = { '可达区域': '#2da990', '未知区域': '#64748b', '不确定区域': '#facc15', '计算范围': null };
    const expected = { '可达区域': result.isochrone.geometry, '未知区域': result.isochrone.unknownRegion,
      '不确定区域': result.isochrone.uncertainRegion, '计算范围': result.isochrone.computationExtent };
    report.layers = {};
    for (const [label, color] of Object.entries(colors)) {
      const control = page.getByRole('checkbox', { name: label, exact: true });
      await control.check(); await page.waitForTimeout(100);
      const paths = await page.evaluate(color => window.__livePolygons.filter(p => color ? p.options.fillColor === color : p.options.fillOpacity === 0).map(p => p.paths), color);
      const expectedPaths = (expected[label]?.coordinates || []).map(polygon => polygon.map(ring => ring.map(point => point.join(',')).join(';')));
      await control.uncheck(); await page.waitForTimeout(100);
      const hidden = await page.evaluate(color => !window.__livePolygons.some(p => color ? p.options.fillColor === color : p.options.fillOpacity === 0), color);
      report.layers[label] = { coordinatesMatch: JSON.stringify(paths) === JSON.stringify(expectedPaths), hidden, components: paths.length };
      await control.check();
    }
    await page.evaluate(() => window.__liveMap.centerAndZoom(new window.BMapGL.Point(121.513926, 31.313077), 14));
    await page.waitForTimeout(1200); await screenshot(page, 'analysis-layers-and-extent.png');
    task = await (await fetch(base + `/api/analyses/${result.taskId}`)).json();
    report.stage = 'mobile_replay';
    const mobile = await openPage({ width: 390, height: 844 }, task);
    await mobile.getByRole('button', { name: '开始分析', exact: true }).click();
    await mobile.getByText('分析完成', { exact: true }).waitFor({ timeout: 15000 });
    await mobile.waitForTimeout(1500);
    report.checks.mobileNoOverflow = await mobile.evaluate(() => document.documentElement.scrollWidth <= 390);
    await screenshot(mobile, 'analysis-mobile.png');
    // Selecting a point after completion must update coordinates without creating another task.
    const beforeLng = await page.getByRole('spinbutton', { name: '经度', exact: true }).inputValue();
    const box = await page.getByTestId('algorithm-map').boundingBox();
    await page.mouse.click(box.x + box.width * .6, box.y + box.height * .5);
    await page.waitForTimeout(300);
    const afterLng = await page.getByRole('spinbutton', { name: '经度', exact: true }).inputValue();
    report.checks.mapPickUpdatesCenter = beforeLng !== afterLng;
    report.stage = 'finished';
  } catch {
    report.error = 'browser_check_did_not_complete';
  } finally {
    await browser.close();
    await fetch(base + '/live/export');
    report.finalLedger = await (await fetch(base + '/live/status')).json();
    save(renderOnly ? 'T04-render-replay.json' : 'T03-browser.json', report);
  }
  console.log(JSON.stringify({ stage: report.stage, error: report.error, checks: report.checks, layers: report.layers,
    polls: report.polls.length, creates: report.creates, counts: report.finalLedger?.counts, quality: result?.isochrone?.quality }));
}
main().catch(() => { console.error('Live UI check stopped; sensitive details suppressed.'); process.exitCode = 1; });
