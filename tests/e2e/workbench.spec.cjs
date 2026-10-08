const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

const appPath = '/backtest/permanent-investment/';
const fixtures = () => JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../output/playwright/fixture.json'), 'utf8')).runs;
const browserErrors = new WeakMap();

async function openHistory(page) {
  const mobile = page.viewportSize().width <= 1100;
  const button = page.locator(mobile ? '#mobileHistoryToggle' : '#historyToggle');
  if (await button.getAttribute('aria-expanded') !== 'true') await button.click();
  await expect(page.locator('#historyPanel')).not.toHaveAttribute('inert', '');
}

async function loadRun(page, runId) {
  await openHistory(page);
  await page.locator(`[data-history-replay="${runId}"]`).click();
  await expect(page.locator('#workspaceMessage')).toContainText('已回放');
  await expect(page.locator('#rebalanceTable tbody tr').first()).toBeVisible();
}

async function openParameters(page) {
  if (page.viewportSize().width <= 1100) await page.locator('#mobileParameterToggle').click();
  await expect(page.locator('#parameterPanel')).not.toHaveAttribute('inert', '');
}

async function noPageOverflow(page) {
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 1);
}

async function openResearch(page) {
  await page.locator('#openResearchSection').click();
  await expect(page.locator('#researchSection')).toHaveAttribute('open', '');
  await expect(page.locator('#researchBaseline')).toContainText('2019');
}

test.beforeEach(async ({ page }, testInfo) => {
  browserErrors.set(page, []);
  page.on('pageerror', error => browserErrors.get(page).push(error.message));
  await page.goto(appPath);
  await page.locator('#identityKeyInput').fill(`e2e-${testInfo.project.name}-${testInfo.title}-${Date.now()}`);
  await page.locator('#identitySubmit').click();
  await expect(page.locator('#identityGate')).toBeHidden();
  await expect(page.locator('#workspaceMessage')).toContainText('准备就绪');
  await loadRun(page, fixtures()[0].run_id);
});

test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === testInfo.expectedStatus) {
    await noPageOverflow(page);
    expect(browserErrors.get(page)).toEqual([]);
  }
});

test('exact percentage input stays synchronized and rejects invalid values', async ({ page }, testInfo) => {
  await openParameters(page);
  await page.locator('#rebalanceFrequency').selectOption('yearly');
  await page.locator('#dipBuyEnabled').check();
  await page.locator('#dipBuyAssetCapEnabled').check();
  await page.locator('[data-repo-mode="fixed_bucket"]').click();
  const pairs = [
    ['rebalanceBand', '17.5'], ['repoFixedRatio', '22.5'],
    ['dipBuyDrawdown', '7.5'], ['dipBuyAssetCapRatio', '37.5'],
  ];
  for (const [id, value] of pairs) {
    const number = page.locator(`#${id}Percent`);
    await number.fill(value);
    await number.press('Tab');
    await expect(page.locator(`#${id}`)).toHaveValue(String(Number(value) / 100));
    expect(await number.evaluate(el => el.checkValidity())).toBe(true);
    if (testInfo.project.name !== 'desktop') {
      const bounds = await number.boundingBox();
      expect(bounds.height).toBeGreaterThanOrEqual(44);
    }
  }
  await page.locator('#monthlySpendAnnualGrowth').fill('2.5');
  await page.locator('#rebalanceBandPercent').fill('101');
  let starts = 0;
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/backtest/start')) starts++; });
  await page.locator('#runBtn').click();
  expect(starts).toBe(0);
  expect(await page.locator('#rebalanceBandPercent').evaluate(el => el.checkValidity())).toBe(false);
  await page.locator('#rebalanceBandPercent').fill('17.5');
  await page.locator('#rebalanceBandPercent').press('Tab');
  await page.locator('#rebalanceBandPercent').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('exact-inputs.png') });
  if (testInfo.project.name !== 'desktop') {
    await page.keyboard.press('Escape');
    await expect(page.locator('#mobileParameterToggle')).toBeFocused();
  }
});

test('rebalance date links chart, actual weights and same-day transactions', async ({ page }, testInfo) => {
  const chart = await (await page.request.get(`/api/backtest/${fixtures()[0].run_id}/chart-series`)).json();
  const datesInTable = await page.locator('#rebalanceTable [data-rebalance-date]').evaluateAll(nodes => nodes.map(node => node.dataset.rebalanceDate));
  const date = datesInTable.find(value => !chart.chart.dates.includes(value));
  expect(date, 'fixture must exercise a date omitted by chart sampling').toBeTruthy();
  const dateButton = page.locator(`[data-rebalance-date="${date}"]`);
  await dateButton.click();
  await expect(page.locator('#rebalanceEventDetail')).toBeVisible();
  await expect(page.locator('#rebalanceEventDetail')).toContainText(date);
  await expect(page.locator('#rebalanceEventDetail')).toContainText('权重');
  await expect(page.locator('#rebalanceEventDetail')).toContainText('手续费');
  await expect(page.locator('#eventChartPosition')).toContainText(`精确定位 ${date}`);
  await expect(page.locator('#eventChartPosition')).not.toContainText('最接近');
  const marker = await page.evaluate(() => window.echarts.getInstanceByDom(document.getElementById('assetChart')).getOption().series[0].markLine.data[0].xAxis);
  expect(marker).toBe(date);
  await expect(page.locator('#rebalanceEventDetail')).not.toContainText('scheduled_open');
  expect(await page.locator('.event-weight-grid > span').count()).toBeLessThanOrEqual(18);
  await page.locator('#rebalanceEventDetail').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('event-detail.png') });
  await page.locator('[data-show-event-trades]').click();
  await expect(page.locator('#tradeDateFilterLabel')).toContainText(date);
  const dates = await page.locator('#tradesTable tbody tr td:first-child').allTextContents();
  expect(dates.length).toBeGreaterThan(0);
  expect(dates.every(value => value.trim() === date)).toBe(true);
  await page.locator('#clearTradeDateFilter').click();
  await expect(page.locator('#tradeDateFilter')).toBeHidden();
  const allDates = await page.locator('#tradesTable tbody tr td:first-child').allTextContents();
  expect(new Set(allDates).size).toBeGreaterThan(1);
  await page.locator('#recordTabRebalance').click();
  await expect(page.locator('#rebalanceTable')).not.toContainText('带内，无需调仓');
});

test('names, notes and favorites persist through reload with safe text rendering', async ({ page }, testInfo) => {
  await openHistory(page);
  const item = page.locator('.history-item').filter({ has: page.locator(`[data-history-replay="${fixtures()[0].run_id}"]`) });
  await item.getByRole('button', { name: /编辑|命名/ }).click();
  await expect(page.locator('#metadataDialog')).toBeVisible();
  const name = `生活费方案 ${testInfo.project.name}`;
  const note = '<b>保留低频，观察消费缺口</b>';
  await page.locator('#metadataName').fill(name);
  await page.locator('#metadataNote').fill(note);
  await page.locator('#metadataFavorite').check();
  await page.locator('#saveMetadata').click();
  await expect(page.locator('#metadataDialog')).not.toBeVisible();
  await expect(page.locator('#historyList')).toContainText(name);
  await page.reload();
  await expect(page.locator('#workspaceMessage')).toContainText('准备就绪');
  await openHistory(page);
  await page.locator('#historyFavoritesOnly').check();
  await expect(page.locator('#historyList .history-item')).toHaveCount(1);
  await expect(page.locator('#historyList')).toContainText(name);
  await expect(page.locator('#historyList')).toContainText(note);
  await page.locator('#historySearch').fill('观察消费');
  await expect(page.locator('#historyList .history-item')).toHaveCount(1);
  const detail = await (await page.request.get(`/api/backtest/${fixtures()[0].run_id}`)).json();
  expect(detail.metadata).toMatchObject({ name, note, favorite: true });
  await page.screenshot({ path: testInfo.outputPath('favorite-history.png') });
  await page.locator('#closeHistoryPanel').click();
  await page.locator(testInfo.project.name === 'desktop' ? '#identityKeyButton' : '#mobileIdentityKeyButton').click();
  await page.locator('#identityKeyInput').fill(`e2e-other-${testInfo.project.name}-${Date.now()}`);
  await page.locator('#identitySubmit').click();
  await expect(page.locator('#identityGate')).toBeHidden();
  await openHistory(page);
  await page.locator('#historySearch').fill('');
  await expect(page.locator('#historyList')).not.toContainText(name);
  await page.locator('#historyFavoritesOnly').check();
  await expect(page.locator('#historyList .history-item')).toHaveCount(0);
});

test('different-period comparison explains differences and recomputes common dates', async ({ page }, testInfo) => {
  const mainMessage = await page.locator('#workspaceMessage').innerText();
  await openHistory(page);
  await page.locator(`[data-history-compare="${fixtures()[1].run_id}"]`).click();
  const comparison = page.locator('#historyComparison');
  await expect(comparison).toContainText('2019');
  await expect(comparison).toContainText('2020');
  await expect(comparison).toContainText('费用');
  await expect(comparison).toContainText('调仓');
  await comparison.locator('.comparison-differences > summary').click();
  await expect(comparison.locator('.comparison-differences')).toContainText('佣金');
  await expect(comparison.locator('.comparison-differences')).not.toContainText('commission_rate');
  await expect(comparison.locator('[data-compare-common]')).toBeVisible();
  await comparison.locator('[data-compare-common]').click();
  await expect(comparison).toContainText(/同期|共同区间/);
  await expect(comparison).toContainText('2020-02');
  await expect(comparison).toContainText('按共同区间重跑的结果', { timeout: 80_000 });
  await expect(comparison).toContainText('实际区间一致');
  await expect(page.locator('#workspaceMessage')).toHaveText(mainMessage);
  await comparison.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('same-period-comparison.png') });
});

test('frequency-by-band research uses saved config and reports real trading differences', async ({ page }, testInfo) => {
  await openParameters(page);
  await page.locator('#rebalanceBandPercent').fill('80');
  await page.locator('#rebalanceBandPercent').press('Tab');
  if (testInfo.project.name !== 'desktop') await page.locator('#closeParameterPanel').click();
  await openResearch(page);
  await page.locator('#rebalanceResearchBands').fill('0,25');
  const starting = page.waitForResponse(response => response.url().endsWith('/api/research/start') && response.request().method() === 'POST');
  await page.locator('#startRebalanceResearch').click();
  const start = await (await starting).json();
  await expect(page.locator('#rebalanceResearchStatus')).toContainText('完成', { timeout: 80_000 });
  const job = await (await page.request.get(`/api/research/jobs/${start.job_id}`)).json();
  expect(job.run_id).toBe(fixtures()[0].run_id);
  expect(job.status).toBe('completed');
  expect(job.rows).toHaveLength(6);
  expect(job.rows.every(row => row.status === 'success')).toBe(true);
  expect(new Set(job.rows.map(row => row.rebalance_trade_count)).size).toBeGreaterThan(1);
  await expect(page.locator('#rebalanceResearchTable tbody tr')).toHaveCount(6);
  await page.locator(testInfo.project.name === 'desktop' ? '#rebalanceResearchTable' : '#rebalanceResearchCards').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('rebalance-research.png') });
});

test('withdrawal stress reveals first shortfall and never creates ordinary history', async ({ page }, testInfo) => {
  const before = await (await page.request.get('/api/backtest/history')).json();
  await openResearch(page);
  await page.locator('#withdrawalResearchSpends').fill('1000,500000');
  await page.locator('#withdrawalResearchGrowthRates').fill('0,5');
  await page.locator('#withdrawalResearchStartYears').fill('2019');
  const starting = page.waitForResponse(response => response.url().endsWith('/api/research/start') && response.request().method() === 'POST');
  await page.locator('#startWithdrawalResearch').click();
  const start = await (await starting).json();
  await expect(page.locator('#withdrawalResearchStatus')).toContainText('完成', { timeout: 80_000 });
  const job = await (await page.request.get(`/api/research/jobs/${start.job_id}`)).json();
  expect(job.rows).toHaveLength(4);
  expect(job.rows.every(row => row.status === 'success')).toBe(true);
  const stressed = job.rows.filter(row => row.inputs.monthly_spend_cny === 500000);
  expect(stressed.every(row => row.first_spend_shortfall_date && row.total_spend_shortfall_cny > 0)).toBe(true);
  expect(job.rows.some(row => row.inputs.monthly_spend_cny === 1000 && !row.first_spend_shortfall_date)).toBe(true);
  const after = await (await page.request.get('/api/backtest/history')).json();
  expect(after.records.map(row => row.run_id)).toEqual(before.records.map(row => row.run_id));
  await page.locator(testInfo.project.name === 'desktop' ? '#withdrawalResearchTable' : '#withdrawalResearchCards').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('withdrawal-stress.png') });
});

test('research validates input, cancels computation and permits another run', async ({ page }) => {
  await openResearch(page);
  await page.locator('#rebalanceResearchBands').fill('101');
  await page.locator('#startRebalanceResearch').click();
  await expect(page.locator('#rebalanceResearchStatus')).toContainText(/100|范围|容忍带/);
  await page.locator('#rebalanceResearchBands').fill('0,1,2,3,4,5,6,7');
  await page.locator('#startRebalanceResearch').click();
  await expect(page.locator('#cancelRebalanceResearch')).toBeVisible();
  await page.locator('#cancelRebalanceResearch').click();
  await expect(page.locator('#rebalanceResearchStatus')).toContainText('取消', { timeout: 80_000 });
  await expect(page.locator('#startRebalanceResearch')).toBeEnabled();
  await page.locator('#rebalanceResearchBands').fill('25');
  await page.locator('#startRebalanceResearch').click();
  await expect(page.locator('#rebalanceResearchStatus')).toContainText('完成', { timeout: 80_000 });
  await expect(page.locator('#rebalanceResearchTable tbody tr')).toHaveCount(3);
});
