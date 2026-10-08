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


async function expandArchiveActions(item) {
  const toggle = item.locator('.history-more-toggle');
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(item.locator('.history-item-secondary')).toBeVisible();
}

async function assertArchiveActionHierarchy(page, item) {
  const actions = item.locator('.history-item-actions');
  const toggle = item.locator('.history-more-toggle');
  const secondary = item.locator('.history-item-secondary');
  await item.scrollIntoViewIfNeeded();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(secondary).toBeHidden();
  const primary = actions.locator('button:visible');
  await expect(primary).toHaveCount(3);
  await expect(primary.nth(0)).toHaveText('查看结果');
  await expect(primary.nth(1)).toHaveText('对比');
  await expect(primary.nth(2)).toContainText('更多');
  const boxes = await primary.evaluateAll(buttons => buttons.map(button => {
    const { x, y, width, height } = button.getBoundingClientRect();
    return { x, y, width, height };
  }));
  for (const box of boxes) {
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(Math.abs(box.y - boxes[0].y), 'primary buttons must remain on one line').toBeLessThanOrEqual(1);
  }
  expect(boxes[0].x + boxes[0].width).toBeLessThanOrEqual(boxes[1].x + 1);
  expect(boxes[1].x + boxes[1].width).toBeLessThanOrEqual(boxes[2].x + 1);

  await expandArchiveActions(item);
  const items = secondary.locator('button:visible');
  await expect(items).toHaveCount(4);
  const grid = await items.evaluateAll(buttons => buttons.map(button => {
    const { x, y, height } = button.getBoundingClientRect();
    return { x, y, height };
  }));
  expect(Math.abs(grid[0].y - grid[1].y)).toBeLessThanOrEqual(1);
  expect(Math.abs(grid[2].y - grid[3].y)).toBeLessThanOrEqual(1);
  expect(grid[2].y).toBeGreaterThan(grid[0].y);
  expect(Math.abs(grid[0].x - grid[2].x)).toBeLessThanOrEqual(1);
  expect(Math.abs(grid[1].x - grid[3].x)).toBeLessThanOrEqual(1);
  expect(grid.every(box => box.height >= 44)).toBe(true);

  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(secondary).toBeHidden();
  await expandArchiveActions(item);
  await items.first().focus();
  await page.keyboard.press('Escape');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(secondary).toBeHidden();
  await expect(toggle).toBeFocused();
  await expect(page.locator('#historyPanel')).not.toHaveAttribute('inert', '');
  await expect(primary).toHaveCount(3);
}

async function selectSeededLeaderboard(page, testInfo) {
  await page.locator('#closeHistoryPanel').click();
  await page.locator(testInfo.project.name === 'desktop' ? '#identityKeyButton' : '#mobileIdentityKeyButton').click();
  // This identity is pre-populated only in the disposable E2E database.
  await page.locator('#identityKeyInput').fill('e2e-test');
  await page.locator('#identitySubmit').click();
  await expect(page.locator('#identityGate')).toBeHidden();
  await openHistory(page);
  await page.locator('#leaderboardTab').click();
  await expect(page.locator('#leaderboardSection')).toBeVisible();
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
  await item.locator('.history-more-toggle').click();
  await expect(item.locator('.history-item-secondary')).toBeVisible();
  await item.locator('[data-history-metadata]').click();
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
  for (const row of job.rows) {
    expect(row.rebalance_evaluated_count).toBe(row.rebalance_positive_count + row.rebalance_negative_count + row.rebalance_flat_count);
    expect(row.rebalance_evaluated_count).toBeLessThanOrEqual(row.rebalance_trade_count);
    if (row.rebalance_evaluated_count) expect(row.rebalance_positive_ratio).toBeCloseTo(row.rebalance_positive_count / row.rebalance_evaluated_count, 12);
    else expect(row.rebalance_positive_ratio).toBeNull();
  }
  await expect(page.locator('#rebalanceResearchTable tbody tr')).toHaveCount(6);
  await expect(page.locator('#rebalanceResearchTable thead')).toContainText('盈利调仓占比');
  await expect(page.locator('#rebalanceResearchTable .research-win-rate').first()).toContainText('盈利');
  await expect(page.locator('#rebalanceResearchMethodology')).toContainText('持平也计入分母');
  if (testInfo.project.name !== 'desktop') await expect(page.locator('#rebalanceResearchCards .research-card-win').first()).toBeVisible();
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
  let accepted = false;
  for (let retry = 0; retry <= 5; retry += 1) {
    if (retry > 0) {
      // Cancellation can finish before the worker releases its computation slot.
      await expect(page.locator('#rebalanceResearchStatus')).toContainText('上一计算正在退出，请稍后再次点击运行');
      await expect(page.locator('#startRebalanceResearch')).toBeEnabled();
      await page.waitForTimeout(500);
    }
    const starting = page.waitForResponse(response => response.url().endsWith('/api/research/start') && response.request().method() === 'POST');
    await page.locator('#startRebalanceResearch').click();
    const response = await starting;
    if (response.status() !== 429) {
      expect(response.status()).toBe(202);
      accepted = true;
      break;
    }
    await expect(page.locator('#rebalanceResearchStatus')).toContainText('上一计算正在退出，请稍后再次点击运行');
  }
  expect(accepted, 'research should restart after at most five worker-exit retries').toBe(true);
  await expect(page.locator('#rebalanceResearchStatus')).toContainText('完成', { timeout: 80_000 });
  await expect(page.locator('#rebalanceResearchTable tbody tr')).toHaveCount(3);
});


for (const archive of ['history', 'leaderboard']) {
  test(`${archive} cards keep actions in one primary row with accessible inline details`, async ({ page }, testInfo) => {
    await openHistory(page);
    if (archive === 'leaderboard') await selectSeededLeaderboard(page, testInfo);
    const runId = fixtures()[1].run_id;
    const list = page.locator(archive === 'history' ? '#historyList' : '#leaderboardList');
    const item = list.locator('.history-item').filter({ has: page.locator(`[data-${archive}-replay="${runId}"]`) });
    await expect(item).toHaveCount(1);
    await assertArchiveActionHierarchy(page, item);

    // Primary comparison remains directly accessible without opening secondary actions.
    await item.locator(`[data-${archive}-compare]`).click();
    await expect(page.locator('#historyComparison')).toBeVisible();
    await expect(item.locator(`[data-${archive}-compare]`)).toContainText('取消对比');
    await item.locator(`[data-${archive}-compare]`).click();
    await expect(page.locator('#historyComparison')).toBeHidden();

    await expandArchiveActions(item);
    await item.locator('[data-history-metadata]').click();
    await expect(page.locator('#metadataDialog')).toBeVisible();
    await page.locator('#cancelMetadata').click();
    await expect(page.locator('#metadataDialog')).toBeHidden();

    // The secondary favorite action still writes the same record and can be undone.
    const initial = await (await page.request.get(`/api/backtest/${runId}`)).json();
    const wasFavorite = Boolean(initial.metadata.favorite);
    for (const desired of [!wasFavorite, wasFavorite]) {
      await expandArchiveActions(item);
      const saved = page.waitForResponse(response => response.url().endsWith(`/api/backtest/${runId}/metadata`) && response.request().method() === 'POST');
      await item.locator('[data-history-favorite]').click();
      const result = await (await saved).json();
      expect(result.metadata.favorite).toBe(desired);
    }

    await expandArchiveActions(item);
    let deletionPrompt = '';
    page.once('dialog', async dialog => { deletionPrompt = dialog.message(); await dialog.dismiss(); });
    await item.locator(`[data-${archive}-delete]`).click();
    expect(deletionPrompt).toContain('删除');
    await expect(item).toHaveCount(1);

    await expandArchiveActions(item);
    await item.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`${archive}-action-hierarchy.png`) });
    const resultBefore = await page.locator('#resultConfigSummary').innerText();
    page.once('dialog', dialog => dialog.accept());
    await item.locator('[data-history-copy]').click();
    await expect(page.locator('#rebalanceFrequency')).toHaveValue(fixtures()[1].config.rebalance_frequency);
    await expect(page.locator('#startDate')).toHaveValue(fixtures()[1].config.start_date);
    await expect(page.locator('#resultConfigSummary')).toHaveText(resultBefore);
    if (testInfo.project.name !== 'desktop') await page.locator('#closeParameterPanel').click();

    // Replay remains a primary action after the card is rendered again.
    await openHistory(page);
    await item.locator(`[data-${archive}-replay]`).click();
    await expect(page.locator('#workspaceMessage')).toContainText('已回放');
    await expect(page.locator('#resultConfigSummary')).toContainText('2020');
  });
}
