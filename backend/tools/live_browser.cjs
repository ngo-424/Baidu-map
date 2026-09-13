// Real SDK inspection only. No route analysis, traces, HAR, HTML or URL logs.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(require.resolve('@playwright/test', { paths: [path.resolve(__dirname, '../../life-circle-demo')] }));

async function main() {
  const runDir = process.argv[2];
  if (!runDir || !path.isAbsolute(runDir) || !fs.existsSync(runDir)) throw new Error('run_directory_required');
  const temp = path.join(runDir, 'browser-temp');
  fs.mkdirSync(temp, { recursive: true });
  process.env.TEMP = temp; process.env.TMP = temp;
  const fixture = process.argv.includes('--fixture');
  const report = { mode: fixture ? 'independent_fixture' : 'center_check', sdkLoads: 0, pageErrors: 0, dialogs: 0, responses: {} };
  const browser = await chromium.launch({ channel: 'msedge', headless: true,
    args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--disk-cache-size=1'] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.on('pageerror', () => report.pageErrors++);
    page.on('dialog', async dialog => { report.dialogs++; await dialog.dismiss(); });
    page.on('response', response => {
      const url = new URL(response.url());
      if (url.hostname.endsWith('baidu.com') || url.hostname.endsWith('bdimg.com')) {
        const key = `${url.hostname}:${response.status()}`;
        report.responses[key] = (report.responses[key] || 0) + 1;
        if (url.hostname === 'api.map.baidu.com' && url.pathname === '/api') report.sdkLoads++;
      }
    });
    await page.goto('http://127.0.0.1:8019/live/map', { waitUntil: 'domcontentloaded', timeout: 20000 });
    try { await page.waitForFunction(() => window.liveMapStatus?.loaded || window.liveMapStatus?.error, undefined, { timeout: 25000 }); } catch {}
    await page.waitForTimeout(5000);
    report.state = await page.evaluate(() => window.liveMapStatus || {});
    if (fixture && report.state.loaded) {
      await page.getByRole('button', { name: '显示独立孔洞 / 多分量夹具' }).click();
      await page.waitForTimeout(1500);
    }
    async function screenshot(name) {
      // Fail closed when visible text resembles a key; never archive HTML or logs.
      if (/[A-Za-z0-9_-]{25,}/.test(await page.locator('body').innerText())) throw new Error('unsafe_visible_text');
      await page.screenshot({ path: path.join(runDir, name), fullPage: true });
    }
    await screenshot(fixture ? 'map-fixture-desktop.png' : 'map-center-desktop.png');
    if (report.state.loaded && !fixture) {
      await page.evaluate(() => window.liveMap.setZoom(17));
      await page.waitForTimeout(1200);
      await screenshot('map-center-context.png');
    }
    await page.setViewportSize({ width: 390, height: 844 });
    // Reinitialize the SDK at the mobile viewport; desktop canvas resizing can
    // retain its old pixel origin and produce a misleading cropped screenshot.
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 20000 });
    try { await page.waitForFunction(() => window.liveMapStatus?.loaded || window.liveMapStatus?.error, undefined, { timeout: 25000 }); } catch {}
    await page.waitForTimeout(5000);
    if (fixture && await page.evaluate(() => !!window.liveMapStatus?.loaded)) {
      await page.getByRole('button', { name: '显示独立孔洞 / 多分量夹具' }).click();
      await page.waitForTimeout(1500);
    }
    await screenshot(fixture ? 'map-fixture-mobile.png' : 'map-center-mobile.png');
    report.state = await page.evaluate(() => window.liveMapStatus || {});
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(runDir, fixture ? 'map-fixture.json' : 'map-check.json'), JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify(report));
}
main().catch(() => { console.error('Browser check stopped; sensitive details suppressed.'); process.exitCode = 1; });
