const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

const appPath = '/backtest/permanent-investment/';
const fixtures = () => JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../output/playwright/fixture.json'), 'utf8')).runs;
const browserErrors = new WeakMap();
const primaryColumns = ['执行日', '当年盈亏', '当年最大回撤'];

async function replayRun(page, runId) {
  const toggle = page.locator(page.viewportSize().width <= 1100 ? '#mobileHistoryToggle' : '#historyToggle');
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
  const records = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/api/backtest/${runId}/rebalance`));
  await page.locator(`[data-history-replay="${runId}"]`).click();
  const response = await records;
  expect(response.ok()).toBe(true);
  await expect(page.locator('#workspaceMessage')).toContainText('已回放');
  await expect(page.locator('#rebalanceTable tbody tr').first()).toBeVisible();
  await page.locator('a[href="#recordsSection"]').click();
  return response.json();
}

async function assertPrimaryColumnsFit(table) {
  const geometry = await table.evaluate(element => {
    const scroll = element.closest('.table-scroll');
    const bounds = scroll.getBoundingClientRect();
    const measure = cell => {
      const box = cell.getBoundingClientRect();
      return { left: box.left, right: box.right, label: cell.dataset.label || cell.textContent };
    };
    return {
      scrollLeft: scroll.scrollLeft,
      left: bounds.left + scroll.clientLeft,
      right: bounds.left + scroll.clientLeft + scroll.clientWidth,
      cells: [...element.tHead.rows[0].cells].slice(0, 3).map(measure)
        .concat([...element.tBodies[0].rows[0].cells].slice(0, 3).map(measure)),
    };
  });
  expect(geometry.scrollLeft, 'the three key columns must be visible before horizontal scrolling').toBe(0);
  for (const cell of geometry.cells) {
    expect(cell.left, `${cell.label} starts inside the table viewport`).toBeGreaterThanOrEqual(geometry.left - 1);
    expect(cell.right, `${cell.label} fits inside the table viewport`).toBeLessThanOrEqual(geometry.right + 1);
  }
}

test.beforeEach(async ({ page }, testInfo) => {
  browserErrors.set(page, []);
  page.on('pageerror', error => browserErrors.get(page).push(error.message));
  await page.goto(appPath);
  await page.locator('#identityKeyInput').fill(`e2e-rebalance-${testInfo.project.name}-${testInfo.title}-${Date.now()}`);
  await page.locator('#identitySubmit').click();
  await expect(page.locator('#identityGate')).toBeHidden();
  await expect(page.locator('#workspaceMessage')).toContainText('准备就绪');
});

test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus) return;
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  expect(browserErrors.get(page)).toEqual([]);
});

test('rebalance key metrics are immediately readable and keep date and horizontal-scroll interactions', async ({ page }, testInfo) => {
  const records = await replayRun(page, fixtures()[0].run_id);
  const table = page.locator('#rebalanceTable');
  const firstRow = table.locator('tbody tr').first();
  const headers = table.locator('thead th');
  expect((await headers.allTextContents()).slice(0, 3)).toEqual(primaryColumns);
  await assertPrimaryColumnsFit(table);

  const dateButton = firstRow.locator('[data-rebalance-date]');
  const date = await dateButton.getAttribute('data-rebalance-date');
  const source = records.rebalance.find(row => row.rebalance_date === date);
  expect(source).toBeTruthy();
  const profit = firstRow.locator('.table-year-profit');
  const drawdown = firstRow.locator('td[data-label="当年最大回撤"] .table-metric');
  for (const metric of [profit.locator('strong'), drawdown]) {
    const style = await metric.evaluate(element => {
      const value = getComputedStyle(element);
      return { weight: Number(value.fontWeight), size: parseFloat(value.fontSize) };
    });
    expect(style.weight, 'key financial values should stand out from supporting text').toBeGreaterThanOrEqual(600);
    expect(style.size).toBeGreaterThanOrEqual(15);
  }
  expect(await headers.evaluateAll(nodes => nodes.slice(0, 3).every(node => Number(getComputedStyle(node).fontWeight) >= 600))).toBe(true);
  const dateStyle = await dateButton.evaluate(element => ({ height: element.getBoundingClientRect().height, background: getComputedStyle(element).backgroundColor, underline: getComputedStyle(element).textDecorationLine }));
  expect(dateStyle.height).toBeGreaterThanOrEqual(44);
  expect(dateStyle.background).not.toBe('rgba(0, 0, 0, 0)');
  expect(dateStyle.underline).toContain('underline');

  const amount = Number(source.payload.year_profit_cny);
  const sign = amount > 0 ? '+' : amount < 0 ? '−' : '';
  const fullAmount = `${sign}￥${Math.abs(amount).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`;
  expect(await profit.getAttribute('title')).toContain(fullAmount);
  await expect(profit.locator('.visually-hidden')).toHaveText(fullAmount);
  const decisionDate = source.payload.decision_date || date;
  await expect(profit.locator('.rebalance-year-context')).toContainText(`${source.payload.year_label || decisionDate.slice(0, 4)}年`);
  await expect(profit.locator('.rebalance-year-context')).toContainText(`截至${decisionDate.slice(5)}`);
  expect(await profit.getAttribute('title')).toContain(decisionDate);
  if (testInfo.project.name === 'desktop') {
    await expect(profit.locator('.profit-full')).toBeVisible();
    await expect(profit.locator('.profit-full')).toHaveText(fullAmount);
    await expect(profit.locator('.profit-compact')).toBeHidden();
  } else {
    await expect(profit.locator('.profit-full')).toBeHidden();
    await expect(profit.locator('.profit-compact')).toBeVisible();
    expect(Math.abs(amount), 'real fixture should exercise compact mobile amounts').toBeGreaterThanOrEqual(10_000);
    await expect(profit.locator('.profit-compact')).toContainText(/万|亿/);
  }
  await expect(drawdown).toHaveText(`${(source.payload.year_max_drawdown * 100).toFixed(2)}%`);
  await page.screenshot({ path: testInfo.outputPath('rebalance-primary-columns.png') });

  // Later columns remain available without replacing the table or losing the date reference.
  const scroll = page.locator('#rebalancePanel .table-scroll');
  await scroll.evaluate(element => { element.scrollLeft = element.scrollWidth; });
  expect(await scroll.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
  const lastColumn = await headers.last().boundingBox();
  const viewport = await scroll.boundingBox();
  expect(lastColumn.left ?? lastColumn.x).toBeGreaterThanOrEqual(viewport.x - 1);
  expect(lastColumn.x + lastColumn.width).toBeLessThanOrEqual(viewport.x + viewport.width + 1);
  await expect(firstRow.locator('td').last()).not.toBeEmpty();
  const stickyDate = await dateButton.boundingBox();
  expect(stickyDate.x).toBeGreaterThanOrEqual(viewport.x - 1);
  expect(stickyDate.x + stickyDate.width).toBeLessThanOrEqual(viewport.x + viewport.width + 1);
  await scroll.evaluate(element => { element.scrollLeft = 0; });

  await dateButton.click();
  await expect(page.locator('#rebalanceEventDetail')).toBeVisible();
  await expect(page.locator('#rebalanceEventDetail')).toContainText(date);
  await expect(page.locator('#eventChartPosition')).toContainText(`精确定位 ${date}`);
  const marker = await page.evaluate(() => window.echarts.getInstanceByDom(document.getElementById('assetChart')).getOption().series[0].markLine.data[0].xAxis);
  expect(marker).toBe(date);
  await page.locator('[data-show-event-trades]').click();
  await expect(page.locator('#tradeDateFilterLabel')).toContainText(date);
  const dates = await page.locator('#tradesTable tbody tr td:first-child').allTextContents();
  expect(dates.length).toBeGreaterThan(0);
  expect(dates.every(value => value.trim() === date)).toBe(true);
  await page.locator('#clearTradeDateFilter').click();
  await expect(page.locator('#tradeDateFilter')).toBeHidden();
});

test('missing annual drawdown stays unknown while a genuine zero is rendered as a percentage', async ({ page }, testInfo) => {
  const runId = fixtures()[0].run_id;
  let mode = 'missing';
  let changedDate;
  await page.route(url => url.pathname.endsWith(`/api/backtest/${runId}/rebalance`), async route => {
    const response = await route.fetch();
    const body = await response.json();
    // Keep the real engine response, editing only the newest traded row's drawdown fields.
    const row = [...body.rebalance].reverse().find(record => record.payload?.rebalanced === true);
    expect(row).toBeTruthy();
    changedDate = row.rebalance_date;
    if (mode === 'missing') delete row.payload.year_max_drawdown;
    else row.payload.year_max_drawdown = 0;
    row.payload.period_max_drawdown = -0.4321;
    await route.fulfill({ response, json: body });
  });

  await replayRun(page, runId);
  const row = page.locator('#rebalanceTable tbody tr').filter({ has: page.locator(`[data-rebalance-date="${changedDate}"]`) });
  const drawdown = row.locator('td[data-label="当年最大回撤"] .table-metric');
  await expect(drawdown).toHaveText('—');
  await expect(drawdown).not.toContainText('0.00%');
  await expect(drawdown).not.toContainText('43.21%');
  await assertPrimaryColumnsFit(page.locator('#rebalanceTable'));

  mode = 'zero';
  await replayRun(page, runId);
  await expect(drawdown).toHaveText('0.00%');
  await assertPrimaryColumnsFit(page.locator('#rebalanceTable'));
  await page.screenshot({ path: testInfo.outputPath('rebalance-zero-drawdown.png') });
});
