const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

test('market regimes show actual signed monthly returns and records omit data status', async ({ page }, testInfo) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/backtest/permanent-investment/');
  await page.locator('#identityKeyInput').fill(`capture-${testInfo.project.name}`);
  await page.locator('#identitySubmit').click();
  await expect(page.locator('#workspaceMessage')).toContainText('准备就绪');
  const run = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../output/playwright/fixture.json'), 'utf8')).runs[0];
  const detailUrl = `/api/backtest/${run.run_id}`;
  const saved = await (await page.request.get(detailUrl)).json();
  for (const side of ['up', 'down']) {
    expect(saved.summary[`${side}_market_strategy_monthly_return`]).toEqual(expect.any(Number));
    expect(saved.summary[`${side}_market_benchmark_monthly_return`]).toEqual(expect.any(Number));
  }
  // Deterministic edge cases from the user's report: losing during up markets,
  // and a very small loss during down markets. Real APIs exercise the rest of replay.
  await page.route(`**${detailUrl}`, async route => {
    await route.fulfill({ json: { ...saved, summary: { ...saved.summary,
      up_market_months: 5, up_market_strategy_monthly_return: -0.0123, up_market_benchmark_monthly_return: 0.042,
      upside_capture_ratio: -0.2949, down_market_months: 3, down_market_strategy_monthly_return: -0.000002,
      down_market_benchmark_monthly_return: -0.04, downside_capture_ratio: 0.0005,
    } } });
  });
  await page.locator(testInfo.project.name === 'desktop' ? '#historyToggle' : '#mobileHistoryToggle').click();
  await page.locator(`[data-history-replay="${run.run_id}"]`).click();
  await expect(page.locator('#workspaceMessage')).toContainText('已回放');
  await page.locator('#riskDetails > summary').click();
  const card = page.locator('.capture-card');
  await expect(card).toBeVisible();
  await card.scrollIntoViewIfNeeded();
  await expect(card).toContainText('大盘涨，组合反而下跌');
  await expect(card).toContainText('−1.23%');
  await expect(card).toContainText('+4.20%');
  await expect(card).toContainText('组合跌得比大盘少');
  await expect(card).toContainText('−0.0002%');
  await expect(card.locator('.capture-track')).toHaveCount(0);
  await expect(card.locator('details')).not.toHaveAttribute('open', '');
  const captureBounds = await card.boundingBox();
  await page.screenshot({ path: testInfo.outputPath('market-regimes.png'), fullPage: false });
  expect(captureBounds.width).toBeLessThanOrEqual(page.viewportSize().width);
  await card.locator('summary').click();
  await expect(card.locator('details')).toContainText('-29.49%');
  await expect(card.locator('details')).toContainText('0.05%');
  await expect(card.locator('details')).toContainText('100% 表示两者同组年化收益相同');
  await expect(page.locator('#statusTable, #statusPanel, #recordTabStatus')).toHaveCount(0);
  await expect(page.locator('#recordTabRebalance')).toHaveAttribute('aria-selected', 'true');
  for (const name of ['Trades', 'Rolling', 'Months', 'Rebalance']) {
    await page.locator(`#recordTab${name}`).click();
    await expect(page.locator(`#recordTab${name}`)).toHaveAttribute('aria-selected', 'true');
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
  expect(errors).toEqual([]);
});
