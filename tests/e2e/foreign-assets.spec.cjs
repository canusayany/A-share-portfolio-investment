const { test, expect } = require('@playwright/test');

test('A50 and foreign ETF choices preserve the selected asset and exclusive groups', async ({ page }, testInfo) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/backtest/permanent-investment/');
  await page.locator('#identityKeyInput').fill(`e2e-foreign-${testInfo.project.name}-${Date.now()}`);
  await page.locator('#identitySubmit').click();
  await expect(page.locator('#identityGate')).toBeHidden();
  await expect(page.locator('#workspaceMessage')).toContainText('准备就绪');
  if (testInfo.project.name === 'phone') await page.locator('#mobileParameterToggle').click();
  await expect(page.locator('#parameterPanel')).not.toHaveAttribute('inert', '');

  const defaults = await (await page.request.get('/api/default-config')).json();
  const a50 = defaults.assets.find(asset => asset.key === 'cn_a50_etf');
  expect(a50.symbol).toBe('159593.SZ');
  expect(a50.exclusive_group).toBe('cn_broad_etf');
  const hk = defaults.assets.find(asset => asset.key === 'hk_sp500_connect');
  expect(hk.symbol).toBe('03195.HK');
  expect(hk.market).toBe('HK');
  expect(hk.currency).toBe('HKD');

  await expect(page.locator('#broadEtfType option[value="cn_a50_etf"]')).toHaveText('中证A50 159593');
  await page.locator('#enabled_cn_broad_etf_group').check();
  await page.locator('#broadEtfType').selectOption('cn_a50_etf');
  await page.locator('#weight_percent_cn_broad_etf_group').fill('12');
  await page.locator('#weight_percent_cn_broad_etf_group').press('Tab');
  await expect(page.locator('#broadEtfRoute')).toContainText('中证A50');
  await expect(page.locator('#sp500Type option[value="us_sp500"]')).toContainText('VOO');
  await page.locator('#enabled_sp500_group').check();
  await page.locator('#sp500Type').selectOption('us_sp500');
  await page.locator('#weight_percent_sp500_group').fill('10');
  await page.locator('#weight_percent_sp500_group').press('Tab');
  await expect(page.locator('#sp500Route')).toContainText('USD/CNY');
  await expect(page.locator('#broadEtfType')).toHaveValue('cn_a50_etf');
  await page.locator('.asset-control-group').filter({ has: page.locator('#broadEtfType') }).screenshot({ path: testInfo.outputPath('a50-selection.png') });
  await page.locator('.asset-control-group').filter({ has: page.locator('#sp500Type') }).screenshot({ path: testInfo.outputPath('voo-selection.png') });

  const fullConfig = await page.evaluate(() => readConfig());
  const broad = fullConfig.assets.filter(asset => asset.exclusive_group === 'cn_broad_etf');
  expect(broad.filter(asset => asset.enabled).map(asset => asset.symbol)).toEqual(['159593.SZ']);
  expect(broad.filter(asset => asset.symbol !== '159593.SZ').every(asset => asset.target_weight === 0)).toBe(true);

  let submitted;
  await page.route('**/api/backtest/start', async route => {
    submitted = route.request().postDataJSON().config;
    await route.fulfill({ status: 422, json: { error: 'UI payload verified' } });
  });
  const request = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/api/backtest/start'));
  await page.locator('#runBtn').click();
  await request;
  await expect(page.locator('#workspaceMessage')).toContainText('UI payload verified');
  const broadKeys = new Set(broad.map(asset => asset.key));
  expect(submitted.assets.filter(asset => broadKeys.has(asset.key) && asset.enabled).map(asset => asset.key)).toEqual(['cn_a50_etf']);
  expect(submitted.assets.find(asset => asset.key === 'cn_a50_etf').target_weight).toBe(0.12);
  expect(submitted.assets.filter(asset => ['us_sp500', 'hk_sp500_connect', 'cn_sp500_etf'].includes(asset.key) && asset.enabled).map(asset => asset.key)).toEqual(['us_sp500']);

  if (testInfo.project.name === 'phone') await page.locator('#mobileParameterToggle').click();
  await page.locator('#sp500Type').selectOption('hk_sp500_connect');
  await expect(page.locator('#sp500Type option:checked')).toContainText('03195');
  await expect(page.locator('#sp500Route')).toContainText('港股通');
  await expect(page.locator('#sp500Route')).toContainText('HKD/CNY');
  const hkConfig = await page.evaluate(() => readConfig());
  expect(hkConfig.assets.filter(asset => asset.exclusive_group === 'sp500' && asset.enabled).map(asset => asset.symbol)).toEqual(['03195.HK']);
  await page.locator('.asset-control-group').filter({ has: page.locator('#sp500Type') }).screenshot({ path: testInfo.outputPath('hk-selection.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]);
});
