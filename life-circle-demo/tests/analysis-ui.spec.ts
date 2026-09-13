import { test, expect, type Page } from '@playwright/test';
import { resultFixture } from '../src/analysis/testFixtures';

/** Contract-only browser tests. All external requests are blocked; no backend/AK is used. */
async function setup(page: Page, options: { failOnce?: boolean; unavailable?: boolean; mismatch?: boolean } = {}) {
  let submitted: { center: { lng: number; lat: number }; budget: number };
  let count = 0;
  await page.addInitScript(() => {
    const audit = { creations: 0, active: 0, paths: [] as string[][], click: undefined as undefined | ((e: unknown) => void) };
    class Overlay { addEventListener() {} removeEventListener() {} }
    class Point { constructor(public lng: number, public lat: number) {} }
    class Polygon extends Overlay { constructor(public rings: string[]) { super(); } }
    class Map {
      constructor(el: HTMLElement) {
        audit.creations++; audit.active++;
        el.addEventListener('click', () => audit.click?.({ latlng: { lng: 116.405, lat: 39.916 } }));
      }
      centerAndZoom() {} panTo() {} enableScrollWheelZoom() {}
      addEventListener(_event: string, handler: (e: unknown) => void) { audit.click = handler; }
      addOverlay(overlay: Polygon) { if (overlay.rings) audit.paths.push(overlay.rings); }
      clearOverlays() { audit.paths = []; }
      destroy() { audit.active--; }
    }
    Object.assign(window, { BMapGL: { Map, Point, Polygon, Marker: Overlay }, __mapAudit: audit });
  });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.hostname !== '127.0.0.1') return route.abort();
    if (!url.pathname.startsWith('/api/analyses')) return route.continue();
    if (url.pathname === '/api/analyses') {
      submitted = route.request().postDataJSON(); count++;
      if (options.failOnce) { options.failOnce = false; return route.fulfill({ status: 503, json: {} }); }
    }
    const taskId = `task-${count}`;
    if (url.pathname.endsWith('/result')) {
      const result = resultFixture();
      result.taskId = taskId; result.center = { ...submitted.center };
      result.isochrone.config = { ...result.isochrone.config, budget: submitted.budget,
        origin: [submitted.center.lng, submitted.center.lat] };
      result.isochrone.quality = 'partial';
      if (options.unavailable) { result.isochrone.geometry = null; result.isochrone.quality = 'insufficient'; }
      if (options.mismatch) {
        result.center.lng = 120;
        result.isochrone.config.origin[0] = 120;
      }
      return route.fulfill({ json: result });
    }
    return route.fulfill({ status: route.request().method() === 'POST' ? 202 : 200,
      json: { taskId, status: 'completed', stage: 'completed', requests: 200, networkRequests: 0,
        budget: submitted.budget, elapsedSeconds: 1, dataSource: 'synthetic', error: null } });
  });
}

test('validated partial result drives map and automatic report, preserving holes and unknown counts', async ({ page }) => {
  await setup(page);
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toBeVisible();
  await expect(report).toContainText('部分体检结果');
  await expect(report).toContainText('证据质量：部分结果');
  await expect(page.getByTestId('analysis-facility-stats').getByRole('cell', { name: '无法确定', exact: true })).toHaveCount(3);
  const audit = await page.evaluate(() => (window as any).__mapAudit);
  expect(audit.active).toBe(1);
  expect(audit.creations).toBe(1);
  expect(audit.paths).toEqual(resultFixture().isochrone.geometry!.coordinates.map(p => p.map(r => r.map(x => x.join(',')).join(';'))));
  await page.keyboard.press('Escape');
  await page.getByRole('checkbox', { name: '可达区域', exact: true }).uncheck();
  expect(await page.evaluate(() => (window as any).__mapAudit.paths.length)).toBe(0);
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('已重建 2 个可达分量');
  expect(errors).toEqual([]);
});

test('map selection and failed retry retain the old report until a valid replacement arrives', async ({ page }) => {
  const options = { failOnce: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByTestId('algorithm-map').click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue('116.405000');
  options.failOnce = true;
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('后端步行服务未就绪，请检查 AK 和 QPS 配置', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('分析条件已修改');
  await expect(report).toContainText('最近一次分析未成功');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(report).toBeVisible();
  await expect(report).toContainText('116.405000, 39.916000');
  await expect(report).not.toContainText('分析条件已修改');
  await expect(report).not.toContainText('最近一次分析未成功');
});

test('insufficient evidence does not replace a prior report or automatically open a new one', async ({ page }) => {
  const options = { unavailable: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  await page.keyboard.press('Escape');
  options.unavailable = true;
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.407');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('本次步行证据不足，未生成新的体检报告。', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toContainText('116.404000, 39.915000');
});

test('a structurally valid response for different input is rejected before rendering', async ({ page }) => {
  await setup(page, { mismatch: true });
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析结果与提交条件不一致，请检查服务版本', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '查看分析报告', exact: true })).toBeDisabled();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
});
