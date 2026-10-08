const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "../app/static/app.js"), "utf8")
  .replace(/^init\(\)\.catch\([^\n]+\);\s*$/m, "");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function node(id = "") {
  const classes = new Set();
  const listeners = new Map();
  return {
    id, value: "", checked: false, disabled: false, hidden: false,
    textContent: "", innerHTML: "", min: "", max: "", dataset: {}, style: {},
    validationMessage: "", shown: 0, closed: 0, reports: 0, clicks: 0,
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle(name, on = !classes.has(name)) { on ? classes.add(name) : classes.delete(name); return on; },
    },
    setCustomValidity(message) { this.validationMessage = message; },
    checkValidity() { return !this.validationMessage; },
    reportValidity() { this.reports += 1; return this.checkValidity(); },
    showModal() { this.shown += 1; this.open = true; },
    close() { this.closed += 1; this.open = false; },
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; },
    toggleAttribute(name, on) { if (on) this[name] = ""; else delete this[name]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    appendChild() {}, focus() {}, remove() {},
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    dispatchEvent(event) {
      for (const listener of listeners.get(event.type) || []) listener({ target: this, ...event });
      return true;
    },
    click() { this.clicks += 1; },
  };
}

function fixtureConfig() {
  return {
    initial_capital_cny: 1000000, start_date: "2020-01-01", end_date: "2020-02-28",
    rebalance_frequency: "yearly", annual_rebalance_month: 1, rolling_window_years: 3,
    rebalance_month_analysis_enabled: true, rebalance_band: 0.05, rebalance_to_target: true,
    monthly_spend_cny: 0, monthly_spend_annual_growth: 0, repo_target_mode: "residual_weight", repo_fixed_target_cny: 0,
    repo_fixed_target_ratio: 0, repo_symbol: "204001", dip_buy_enabled: true,
    dip_buy_drawdown: 0.05, dip_buy_total_parts: 10, dip_buy_level_mode: "fixed",
    dip_buy_cost_basis_mode: "current_average", dip_buy_recovery_sell_enabled: true,
    dip_buy_asset_cap_enabled: false, dip_buy_asset_cap_ratio: 0.5,
    dip_buy_blackout_enabled: true, dip_buy_blackout_months: 1,
    assets: [{ key: "gold", symbol: "GOLD", name: "Gold", enabled: true, target_weight: 0.25 }],
    repo_options: [{ symbol: "204001", name: "1 day repo", instrument_type: "repo" }],
    fees: {
      cn_etf: { commission_rate: 0.001 }, ibkr_us_etf: { plan: "pro_fixed" },
      fx: { bank_out_spread_bps: 10, bank_in_spread_bps: 10 },
      hk_connect_etf: { broker_commission_rate: 0.001, fx_spread_bps: 10, portfolio_fee_annual_rate: 0.01 },
      tax: { us_dividend_withholding_rate: 0.1 },
    },
  };
}

function harness() {
  const elements = new Map();
  const selectors = new Map();
  const downloads = [];
  const toasts = [];
  const rendered = [];
  const el = (id) => {
    if (!elements.has(id)) elements.set(id, node(id));
    return elements.get(id);
  };
  const document = {
    getElementById: el,
    querySelectorAll: (selector) => selectors.get(selector) || [],
    querySelector: () => null,
    createElement: (tag) => {
      const element = node(tag);
      if (tag === "a") downloads.push(element);
      return element;
    },
    body: node("body"), head: node("head"), addEventListener() {}, visibilityState: "visible",
  };
  const context = vm.createContext({
    document, structuredClone, AbortController, URLSearchParams, Blob,
    URL: { createObjectURL: () => "blob:csv", revokeObjectURL() {} },
    console: { warn() {}, error() {}, log() {} },
    window: {
      document, location: { pathname: "/backtest/permanent-investment/" },
      innerWidth: 1440, matchMedia: () => ({ matches: false }),
      confirm: () => true, setTimeout: (fn) => { fn(); return 1; },
      clearTimeout() {}, requestAnimationFrame: (fn) => fn(), addEventListener() {},
    },
    fetch: async () => { throw new Error("Unexpected fetch"); },
    __toasts: toasts, __rendered: rendered,
  });
  const evaluate = (source) => vm.runInContext(source, context);
  vm.runInContext(appSource, context, { filename: "app.js" });
  const replace = (name, fn) => {
    context.__replacement = fn;
    evaluate(`${name} = __replacement`);
  };
  const assign = (name, value) => {
    context.__value = value;
    evaluate(`${name} = __value`);
  };
  replace("showToast", (message) => toasts.push(message));
  replace("loadChartLibrary", async () => ({}));
  replace("scheduleArchiveRefresh", () => {});
  replace("refreshBacktestArchiveSafely", async () => {});
  replace("setHistoryPanel", () => {});
  replace("selectRecordPanel", () => {});
  replace("renderControls", () => {});
  replace("renderCharts", () => rendered.push(evaluate("currentRunId")));
  replace("renderBacktestRecords", () => {});
  replace("loadBacktestResultSections", async () => ({ series: [], rebalance: { rebalance: [] }, trades: { trades: [] } }));
  evaluate("renderSummary = (summary) => { currentSummary = summary; }; ");

  function controls(config = fixtureConfig()) {
    assign("config", structuredClone(config));
    assign("defaultConfigSnapshot", structuredClone(config));
    const names = {
      initialCapital: "initial_capital_cny", startDate: "start_date", endDate: "end_date",
      rebalanceFrequency: "rebalance_frequency", annualRebalanceMonth: "annual_rebalance_month",
      rollingWindowYears: "rolling_window_years", rebalanceBand: "rebalance_band",
      monthlySpend: "monthly_spend_cny", repoTargetMode: "repo_target_mode",
      repoFixedTarget: "repo_fixed_target_cny", repoFixedRatio: "repo_fixed_target_ratio", repoSymbol: "repo_symbol",
      dipBuyDrawdown: "dip_buy_drawdown", dipBuyTotalParts: "dip_buy_total_parts",
      dipBuyLevelMode: "dip_buy_level_mode", dipBuyCostBasisMode: "dip_buy_cost_basis_mode",
      dipBuyAssetCapRatio: "dip_buy_asset_cap_ratio", dipBuyBlackoutMonths: "dip_buy_blackout_months",
    };
    for (const [id, key] of Object.entries(names)) el(id).value = String(config[key]);
    el("monthlySpendAnnualGrowth").value = String(Number(config.monthly_spend_annual_growth || 0) * 100);
    for (const [id, min, max] of [["rebalanceBand",0,100],["repoFixedRatio",0,100],["dipBuyDrawdown",1,30],["dipBuyAssetCapRatio",10,100]]) {
      const percent = el(`${id}Percent`); percent.value = String(Number(el(id).value) * 100); percent.min = String(min); percent.max = String(max);
    }
    const checks = {
      rebalanceMonthAnalysisEnabled: "rebalance_month_analysis_enabled", rebalanceToTarget: "rebalance_to_target",
      dipBuyEnabled: "dip_buy_enabled", dipBuyRecoverySellEnabled: "dip_buy_recovery_sell_enabled",
      dipBuyAssetCapEnabled: "dip_buy_asset_cap_enabled", dipBuyBlackoutEnabled: "dip_buy_blackout_enabled",
    };
    for (const [id, key] of Object.entries(checks)) el(id).checked = config[key];
    el("enabled_gold").checked = true;
    el("weight_gold").value = "0.25";
    const fees = {
      cnCommission: config.fees.cn_etf.commission_rate, ibkrPlan: config.fees.ibkr_us_etf.plan,
      fxOutBps: 10, fxInBps: 10, hkCommission: 0.001, hkFxBps: 10, hkPortfolioFee: 0.01, usDividendTax: 0.1,
    };
    for (const [id, value] of Object.entries(fees)) el(id).value = String(value);
  }
  controls();
  return { el, selectors, downloads, toasts, rendered, context, evaluate, replace, assign, controls };
}

const entry = (id) => ({ run_id: id, config: fixtureConfig(), summary: { marker: id, analysis_status: "completed" } });
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("history replay uses the latest selection when detail responses finish out of order", async () => {
  const h = harness();
  const older = deferred();
  h.replace("api", (url) => url.endsWith("/older") ? older.promise : Promise.resolve(entry("newer")));
  const first = h.evaluate('replayHistoryRun("older")');
  await h.evaluate('replayHistoryRun("newer")');
  older.resolve(entry("older"));
  await first;
  assert.equal(h.evaluate("currentRunId"), "newer");
  assert.equal(h.evaluate("currentSummary.marker"), "newer");
  assert.deepEqual(h.rendered, ["newer"]);
  assert.equal(h.evaluate("pendingReplayRunId"), null);
});

test("a replay waiting for chart data cannot overwrite a newer displayed run", async () => {
  const h = harness();
  const sections = deferred();
  h.replace("api", async (url) => entry(url.split("/").at(-1)));
  h.replace("loadBacktestResultSections", (id) => id === "older" ? sections.promise : Promise.resolve({ series: [], rebalance: {}, trades: {} }));
  const first = h.evaluate('replayHistoryRun("older")');
  await tick();
  await h.evaluate('replayHistoryRun("newer")');
  sections.resolve({ series: [], rebalance: {}, trades: {} });
  await first;
  assert.equal(h.evaluate("currentRunId"), "newer");
  assert.deepEqual(h.rendered, ["newer"]);
});

test("starting a new calculation invalidates an earlier history replay", async () => {
  const h = harness();
  const detail = deferred();
  h.replace("api", (url) => url.endsWith("/start") ? Promise.resolve({ job_id: "job-new" }) : detail.promise);
  h.replace("waitForBacktestJob", async () => entry("calculated"));
  const replay = h.evaluate('replayHistoryRun("older")');
  await h.evaluate("runBacktest()");
  detail.resolve(entry("older"));
  await replay;
  assert.equal(h.evaluate("currentRunId"), "calculated");
  assert.deepEqual(h.rendered, ["calculated"]);
  assert.equal(h.evaluate("runInProgress"), false);
});

test("busy calculation blocks duplicate runs, history replay, and deletion", async () => {
  const h = harness();
  const job = deferred();
  const calls = [];
  h.replace("api", async (url, options) => { calls.push({ url, options }); return { job_id: "job-one" }; });
  h.replace("waitForBacktestJob", () => job.promise);
  const running = h.evaluate("runBacktest()");
  await tick();
  for (const id of ["runBtn", "mobileRunBtn", "workspaceRunBtn"]) assert.equal(h.el(id).disabled, true);
  await h.evaluate("runBacktest()");
  await h.evaluate('replayHistoryRun("older")');
  await h.evaluate('deleteHistoryRun("older")');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/backtest/start");
  job.resolve(entry("calculated"));
  await running;
  assert.equal(h.evaluate("currentRunId"), "calculated");
  for (const id of ["runBtn", "mobileRunBtn", "workspaceRunBtn"]) assert.equal(h.el(id).disabled, false);
});

test("deletion waits until an in-flight replay finishes", async () => {
  const h = harness();
  const detail = deferred();
  const calls = [];
  h.replace("api", (url, options = {}) => { calls.push({ url, method: options.method }); return detail.promise; });
  const replay = h.evaluate('replayHistoryRun("saved")');
  await h.evaluate('deleteHistoryRun("saved")');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, undefined);
  detail.resolve(entry("saved"));
  await replay;
  assert.equal(h.evaluate("currentRunId"), "saved");
});

test("analysis response is ignored if the selected run changes during the request", async () => {
  const h = harness();
  const response = deferred();
  let recordsRendered = 0;
  h.assign("currentRunId", "older");
  h.assign("currentSummary", { marker: "older" });
  h.replace("sleep", async () => {});
  h.replace("api", () => response.promise);
  h.replace("renderBacktestRecords", () => { recordsRendered += 1; });
  const watching = h.evaluate('watchBacktestAnalysis("older", {}, {})');
  await tick();
  h.assign("currentRunId", "newer");
  h.assign("currentSummary", { marker: "newer" });
  response.resolve(entry("older"));
  await watching;
  assert.equal(h.evaluate("currentSummary.marker"), "newer");
  assert.equal(recordsRendered, 0);
  assert.equal(h.el("analysisMessage").textContent, "");
});

test("restarting the same-run analysis watcher invalidates its older outstanding response", async () => {
  const h = harness();
  const oldResponse = deferred();
  let requests = 0;
  h.assign("currentRunId", "same");
  h.replace("sleep", async () => {});
  h.replace("api", () => ++requests === 1 ? oldResponse.promise : Promise.resolve({ summary: { marker: "fresh", analysis_status: "completed" } }));
  const older = h.evaluate('watchBacktestAnalysis("same", {}, {})');
  await tick();
  await h.evaluate('watchBacktestAnalysis("same", {}, {})');
  oldResponse.resolve({ summary: { marker: "stale", analysis_status: "completed" } });
  await older;
  assert.equal(h.evaluate("currentSummary.marker"), "fresh");
});

test("deleting the displayed run clears charts, summary, records, export, and diagnostics", async () => {
  const h = harness();
  const clears = [];
  const tables = [];
  let summaryGroups;
  h.assign("currentRunId", "deleted");
  h.assign("currentRunConfig", fixtureConfig());
  h.assign("currentSummary", { final_asset_cny: 1234567 });
  h.assign("exportRunId", "deleted");
  h.assign("comparisonRunId", "deleted");
  for (const id of ["assetChart", "dailyPnlChart", "strategyDiagnosticsChart"]) {
    h.context.__chart = { clear: () => clears.push(id) };
    h.evaluate(`charts[${JSON.stringify(id)}] = __chart; pendingChartOptions[${JSON.stringify(id)}] = { stale: true }`);
  }
  h.evaluate('strategyDiagnosticsCache.set("deleted:all", {}); dailyPnlData = { stale: true }');
  h.replace("api", async () => ({ deleted: "deleted" }));
  h.replace("renderSummaryGroups", (primary, secondary) => { summaryGroups = [...primary, ...secondary]; });
  h.replace("renderTable", (id, columns, rows) => tables.push({ id, count: rows.length }));
  await h.evaluate('deleteHistoryRun("deleted")');
  for (const name of ["currentRunId", "currentSummary", "currentRunConfig", "exportRunId", "comparisonRunId", "dailyPnlData"]) assert.equal(h.evaluate(name), null, name);
  assert.equal(h.evaluate("strategyDiagnosticsCache.size"), 0);
  assert.equal(h.evaluate("Object.keys(pendingChartOptions).length"), 0);
  assert.ok(["assetChart", "dailyPnlChart", "strategyDiagnosticsChart"].every((id) => clears.includes(id)));
  assert.equal(summaryGroups.length, 14);
  assert.ok(summaryGroups.every((metric) => metric.value === "--"));
  for (const id of ["rollingTable", "monthsTable", "rebalanceTable", "tradesTable", "rebalanceResearchTable", "withdrawalResearchTable"]) assert.ok(tables.some((table) => table.id === id && table.count === 0), `${id} cleared`);
  assert.ok(tables.every((table) => table.count === 0));
  assert.equal(h.el("csvExportDialog").closed, 1);
  assert.equal(h.el("openCsvExport").disabled, true);
});

test("nonannual submissions disable annual-only strategies without losing the user's checkbox choice", () => {
  const h = harness();
  for (const frequency of ["daily", "weekly", "monthly", "quarterly", "semiannual"]) {
    h.el("rebalanceFrequency").value = frequency;
    const config = h.evaluate("readConfig()");
    assert.equal(config.rebalance_frequency, frequency);
    assert.equal(config.dip_buy_enabled, false);
    assert.equal(config.rebalance_month_analysis_enabled, false);
    assert.equal(h.el("dipBuyEnabled").checked, true);
  }
  h.el("rebalanceFrequency").value = "yearly";
  assert.equal(h.evaluate("readConfig().dip_buy_enabled"), true);
  assert.equal(h.evaluate("readConfig().rebalance_month_analysis_enabled"), true);
});

test("CSV uses the saved run and accepts corrected dates after a validation error", async () => {
  const h = harness();
  const saved = entry("saved");
  saved.config.start_date = "2019-01-01";
  saved.config.end_date = "2019-12-31";
  h.assign("currentRunId", "saved");
  h.replace("api", async () => saved);
  h.selectors.set('#csvExportAssets input[type="checkbox"]:checked', [{ value: "GOLD" }, { value: "REPO" }]);
  const requests = [];
  h.context.fetch = async (url) => { requests.push(url); return { ok: true, blob: async () => new Blob(["csv"]) }; };
  await h.evaluate("openCsvExportDialog()");
  assert.equal(h.el("csvExportStart").min, "2019-01-01");
  assert.equal(h.el("csvExportEnd").max, "2019-12-31");
  assert.equal(h.el("csvExportDialog").shown, 1);
  h.el("csvExportStart").value = "2019-10-01";
  h.el("csvExportEnd").value = "2019-02-01";
  await h.evaluate("downloadCsvExport()");
  assert.equal(requests.length, 0);
  assert.ok(h.el("csvExportEnd").validationMessage);
  h.el("csvExportEnd").value = "2019-11-01";
  await h.evaluate("downloadCsvExport()");
  assert.equal(requests.length, 1);
  const downloaded = new URL(requests[0], "https://example.test");
  assert.equal(downloaded.pathname, "/backtest/permanent-investment/api/backtest/saved/export.csv");
  assert.equal(downloaded.searchParams.get("start_date"), "2019-10-01");
  assert.equal(downloaded.searchParams.get("symbols"), "GOLD,REPO");
  assert.equal(h.el("csvExportStart").validationMessage, "");
  assert.equal(h.el("csvExportEnd").validationMessage, "");
  assert.equal(h.downloads[0].clicks, 1);
  assert.equal(h.el("downloadCsv").disabled, false);
});

test("CSV cannot export a previously opened run after the displayed run changes", async () => {
  const h = harness();
  h.assign("currentRunId", "old");
  h.replace("api", async () => entry("old"));
  await h.evaluate("openCsvExportDialog()");
  h.assign("currentRunId", "new");
  let fetched = false;
  h.context.fetch = async () => { fetched = true; };
  await h.evaluate("downloadCsvExport()");
  assert.equal(fetched, false);
  assert.equal(h.downloads.length, 0);
  assert.equal(h.toasts.length, 1);
});

test("late CSV configuration cannot reopen an export for an obsolete run", async () => {
  const h = harness();
  const saved = deferred();
  h.assign("currentRunId", "old");
  h.replace("api", () => saved.promise);
  const opening = h.evaluate("openCsvExportDialog()");
  h.assign("currentRunId", "new");
  saved.resolve(entry("old"));
  await opening;
  assert.equal(h.evaluate("exportRunId"), null);
  assert.equal(h.el("csvExportDialog").shown, 0);
});

test("fixed cash bucket previews all capital as cash when no risk assets are enabled", () => {
  const h = harness();
  h.el("repoTargetMode").value = "fixed_bucket";
  h.el("repoFixedTarget").value = "200000";
  h.el("repoFixedRatio").value = "0.1";
  h.el("enabled_gold").checked = false;
  h.evaluate("updateRepoWeight()");
  const plan = h.evaluate('currentRepoPlan("fixed_bucket", currentAssetControls().reduce((sum, asset) => sum + (asset.enabled ? asset.weight : 0), 0))');
  assert.equal(plan.repoTargetValue, 1000000);
  assert.equal(plan.repoWeight, 1);
  assert.equal(plan.remainingWeight, 0);
  assert.match(h.el("repoWeight").textContent, /100\.00%/);
  assert.equal(h.el("effective_gold").textContent, "未启用");

  // Re-enabling an asset returns to the requested fixed amount plus ratio.
  h.el("enabled_gold").checked = true;
  h.evaluate("updateRepoWeight()");
  assert.equal(h.evaluate('currentRepoPlan("fixed_bucket", 0.25).repoWeight'), 0.3);
  assert.equal(h.el("effective_gold").textContent, "实际配置 70.00%");
});

test("full-period preset restores application defaults after replaying a shorter historical run", async () => {
  const h = harness();
  const historical = entry("short-history");
  historical.config.start_date = "2016-04-01";
  historical.config.end_date = "2016-09-30";
  h.replace("api", async () => historical);
  h.replace("renderControls", () => {
    h.el("startDate").value = h.evaluate("config.start_date");
    h.el("endDate").value = h.evaluate("config.end_date");
  });
  await h.evaluate('replayHistoryRun("short-history")');
  assert.equal(h.el("startDate").value, "2020-01-01", "viewing history preserves the draft");
  assert.equal(h.evaluate("currentRunConfig.end_date"), "2016-09-30");
  h.evaluate('applyDatePreset("all")');
  assert.equal(h.el("startDate").value, "2020-01-01");
  assert.equal(h.el("endDate").value, "2020-02-28");
  // The visible saved result retains its dates until the new draft is run.
  assert.equal(h.evaluate("currentRunConfig.start_date"), "2016-04-01");
  assert.equal(h.el("draftNotice").hidden, false);
});

test("percentage input keeps allocations above eighty percent and supports one hundred percent", () => {
  const h = harness();
  const range = h.el("weight_gold");
  const percent = h.el("weight_percent_gold");
  h.context.__row = { querySelector: (selector) => selector === "#weight_gold" ? range : percent };
  h.evaluate('bindAssetWeightInputs(__row, "gold")');
  for (const value of [81, 97.25, 100]) {
    percent.value = String(value);
    percent.dispatchEvent({ type: "input" });
    assert.equal(Number(range.value), value / 100);
    assert.equal(h.evaluate("readConfig().assets[0].target_weight"), value / 100);
  }
  assert.equal(h.el("repoWeight").textContent, "0.00%");
  range.value = "0.955";
  range.dispatchEvent({ type: "input" });
  assert.equal(percent.value, "95.5");
});

test("reversed dates prevent a run from reaching the server", async () => {
  const h = harness();
  let submitted = 0;
  h.replace("api", async () => { submitted += 1; return { job_id: "unexpected" }; });
  h.el("startDate").value = "2020-03-01";
  h.el("endDate").value = "2020-02-28";
  await h.evaluate("runBacktest()");
  assert.equal(submitted, 0);
  assert.equal(h.evaluate("runInProgress"), false);
  assert.equal(h.el("endDate").reports, 1);
  assert.match(h.el("message").textContent, /结束日期不能早于开始日期/);
});

test("combined asset weights above one hundred percent prevent submission", async () => {
  const h = harness();
  const config = fixtureConfig();
  config.assets.push({ key: "bond", symbol: "BOND", name: "Bond", enabled: true, target_weight: 0.6 });
  h.assign("config", config);
  h.el("weight_gold").value = "0.6";
  h.el("enabled_bond").checked = true;
  h.el("weight_bond").value = "0.6";
  const percent = h.el("weight_percent_gold");
  h.el("assetControls").querySelector = () => percent;
  let submitted = 0;
  h.replace("api", async () => { submitted += 1; return { job_id: "unexpected" }; });
  await h.evaluate("runBacktest()");
  assert.equal(submitted, 0);
  assert.equal(h.evaluate("runInProgress"), false);
  assert.equal(percent.reports, 1);
  assert.match(h.el("message").textContent, /120\.00%/);
});


test("exact strategy percentage editors synchronize both ways and never silently clamp invalid text", async () => {
  const h = harness();
  for (const [id, valid] of [["rebalanceBand",17.5],["repoFixedRatio",22.5],["dipBuyDrawdown",7.5],["dipBuyAssetCapRatio",37.5]]) {
    h.evaluate(`bindPercentControl(${JSON.stringify(id)})`);
    h.el(`${id}Percent`).value = String(valid);
    h.el(`${id}Percent`).dispatchEvent({ type: "input" });
    assert.equal(Number(h.el(id).value), valid / 100);
    h.el(id).value = id === "dipBuyDrawdown" ? "0.11" : "0.25";
    h.el(id).dispatchEvent({ type: "input" });
    assert.equal(Number(h.el(`${id}Percent`).value), Number(h.el(id).value) * 100);
  }
  h.el("rebalanceBandPercent").value = "101";
  h.el("rebalanceBandPercent").dispatchEvent({ type: "input" });
  assert.equal(h.el("rebalanceBand").value, "0.25");
  assert.equal(h.el("rebalanceBandPercent").value, "101");
  let requests = 0;
  h.replace("api", async () => { requests++; });
  await h.evaluate("runBacktest()");
  assert.equal(requests, 0);
  assert.match(h.el("rebalanceBandPercent").validationMessage, /0.*100/);
  h.el("rebalanceBandPercent").value = "17.5";
  h.el("rebalanceBandPercent").dispatchEvent({ type: "input" });
  assert.equal(h.el("rebalanceBandPercent").validationMessage, "");
});

test("annual consumption growth is sent as a fraction and old configs normalize to zero", () => {
  const h = harness();
  h.el("monthlySpendAnnualGrowth").value = "2.5";
  assert.equal(h.evaluate("readConfig().monthly_spend_annual_growth"), 0.025);
  h.context.__old = fixtureConfig(); delete h.context.__old.monthly_spend_annual_growth;
  h.context.__new = { ...h.context.__old, monthly_spend_annual_growth: 0 };
  assert.equal(h.evaluate("configFingerprint(__old)"), h.evaluate("configFingerprint(__new)"));
});

function researchControls(h) {
  h.assign("currentRunId", "saved-base"); h.assign("currentRunConfig", fixtureConfig());
  h.selectors.set('#rebalanceResearchFrequencies input:checked', ["monthly", "quarterly", "yearly"].map(value => ({ value })));
  h.el("rebalanceResearchBands").value = "0, 10, 25";
  h.el("withdrawalResearchSpends").value = "1000, 5000";
  h.el("withdrawalResearchGrowthRates").value = "0, 5";
  h.el("withdrawalResearchStartYears").value = "2019, 2020";
}

test("research parses percentages, deduplicates inputs, and rejects invalid or oversized grids", () => {
  const h = harness(); researchControls(h);
  h.el("rebalanceResearchBands").value = "0，10,25,25";
  const request = JSON.parse(h.evaluate('JSON.stringify(buildResearchRequest("rebalance_grid"))'));
  assert.equal(request.count, 9);
  assert.deepEqual(request.request.bands, [0,.1,.25]);
  const stress = JSON.parse(h.evaluate('JSON.stringify(buildResearchRequest("withdrawal_stress"))'));
  assert.deepEqual(stress.request.annual_growth_rates, [0,.05]);
  assert.equal(stress.count, 8);
  for (const text of ["101", "-1", "no", ""]) {
    h.el("rebalanceResearchBands").value = text;
    assert.throws(() => h.evaluate('buildResearchRequest("rebalance_grid")'));
  }
  h.el("rebalanceResearchBands").value = "0,1,2,3,4,5,6,7,8";
  assert.throws(() => h.evaluate('buildResearchRequest("rebalance_grid")'), /27.*24/);
  h.el("withdrawalResearchStartYears").value = "2019.5";
  assert.throws(() => h.evaluate('buildResearchRequest("withdrawal_stress")'), /整数/);
});

test("research posts the saved run, preserves a changed draft, and does not paint results onto another run", async () => {
  const h = harness(); researchControls(h);
  const waiting = deferred(); const requested = [];
  h.el("rebalanceBand").value = ".8";
  h.replace("sleep", async () => {});
  h.replace("api", async (path, options) => {
    requested.push({ path, body: options?.body && JSON.parse(options.body) });
    if (path === "/api/research/start") return { job_id: "job-a", run_id: "saved-base", kind: "rebalance_grid", status: "running", completed: 0, total: 9, rows: [] };
    return waiting.promise;
  });
  const pending = h.evaluate('startResearch("rebalance_grid")'); await tick();
  assert.equal(requested[0].body.run_id, "saved-base");
  assert.equal("config" in requested[0].body, false);
  assert.equal(h.evaluate('researchTasks.get("saved-base:rebalance_grid").baseline.rebalance_band'), .05);
  h.assign("currentRunId", "another-run"); h.evaluate("renderResearch()");
  waiting.resolve({ job_id: "job-a", run_id: "saved-base", kind: "rebalance_grid", status: "completed", completed: 9, total: 9, rows: [] });
  await pending;
  assert.match(h.el("rebalanceResearchStatus").textContent, /尚无研究/);
  assert.equal(h.el("rebalanceBand").value, ".8");
  h.assign("currentRunId", "saved-base"); h.evaluate("renderResearch()");
  assert.match(h.el("rebalanceResearchStatus").textContent, /已完成.*9 \/ 9/);
  assert.equal(h.evaluate("runHistory.length"), 0);
});

test("research cancellation wins over an older in-flight progress response", async () => {
  const h = harness(); researchControls(h);
  const pendingGet = deferred(); h.replace("sleep", async () => {});
  h.evaluate('researchTasks.set("saved-base:rebalance_grid", {run_id:"saved-base",kind:"rebalance_grid",job_id:"j",status:"running",rows:[],total:9})');
  h.replace("api", async (path) => path.endsWith("/cancel") ? { status: "cancelled", completed: 2, total: 9, rows: [] } : pendingGet.promise);
  const watching = h.evaluate('pollResearchTask(researchTasks.get("saved-base:rebalance_grid"))'); await tick();
  await h.evaluate('cancelResearch("rebalance_grid")');
  pendingGet.resolve({ status: "running", completed: 1 }); await watching;
  assert.equal(h.evaluate('researchTasks.get("saved-base:rebalance_grid").status'), "cancelled");
  assert.match(h.el("rebalanceResearchStatus").textContent, /已取消/);
  assert.equal(h.el("startRebalanceResearch").disabled, false);
});

test("old-engine research failure offers explicit baseline loading and never starts a backtest", async () => {
  const h = harness(); researchControls(h); let requests = 0;
  h.replace("api", async () => { requests++; const error = new Error("stale engine"); error.status = 409; throw error; });
  await h.evaluate('startResearch("rebalance_grid")');
  assert.equal(requests, 1);
  assert.equal(h.el("rebalanceResearchRebase").hidden, false);
  assert.match(h.el("rebalanceResearchStatus").textContent, /已保存基准.*重新运行/);
  assert.equal(h.el("rebalanceBand").value, "0.05");
});

test("favorites fetch includes older records and metadata renders user text without HTML execution", async () => {
  const h = harness(); const records = Array.from({ length: 25 }, (_, i) => ({ ...entry(`old-${i}`), metadata: { favorite: true, name: '<img src=x onerror=alert(1)>', note: '<b>memo</b>' } }));
  h.assign("favoritesOnly", true); let requested;
  h.replace("api", async (path) => { requested = path; return { records }; });
  await h.evaluate("refreshRecentArchive()");
  assert.equal(requested, "/api/backtest/history?favorites=1");
  assert.equal(h.evaluate("runHistory.length"), 25);
  assert.match(h.el("historyList").innerHTML, /&lt;img/);
  assert.match(h.el("historyList").innerHTML, /&lt;b&gt;memo/);
  assert.ok(!h.el("historyList").innerHTML.includes('<img src=x'));
  h.assign("archiveFilter", "memo");
  assert.equal(h.evaluate('filteredArchiveEntries(runHistory,"newest").length'), 25);
});

test("event dates filter actual transactions and old events do not fabricate before-after weights", () => {
  const h = harness(); const rendered = [];
  h.replace("renderTable", (id, columns, rows) => rendered.push({ id, rows }));
  h.assign("currentTradeRecords", [{ trade_date:"2020-01-02", symbol:"GOLD", side:"buy",quantity:10,price:2,gross_amount:20,fee:1,currency:"CNY",reason:"rebalance" },{ trade_date:"2020-01-03", symbol:"GOLD",side:"sell",quantity:2,price:3,gross_amount:6,fee:.5,currency:"CNY",reason:"rebalance" }]);
  h.assign("selectedTradeDate", "2020-01-02"); h.evaluate("renderTradeRecords()");
  assert.equal(rendered.at(-1).rows.length, 1);
  assert.equal(rendered.at(-1).rows[0]["交易日期"], "2020-01-02");
  h.evaluate("clearRebalanceEvent()"); assert.equal(rendered.at(-1).rows.length, 2);
  assert.equal(h.evaluate('eventWeightRows({payload:{weights:{GOLD:.5}}})'), null);
  const weights = JSON.parse(h.evaluate('JSON.stringify(eventWeightRows({payload:{before_weights:{GOLD:.3,REPO:.7},after_weights:{GOLD:.25,REPO:.75}}}))'));
  assert.deepEqual(weights[0], { symbol:"GOLD", before:.3, after:.25 });
});

test("common-period comparison submits both saved configs with shared dates without modifying the draft", async () => {
  const h = harness(); const left = entry("left"), right = entry("right");
  left.config.start_date = "2019-01-01"; right.config.start_date = "2020-01-01";
  left.config.end_date = "2021-12-31"; right.config.end_date = "2022-12-31";
  right.config.rebalance_band = .25;
  h.assign("runHistory", [left,right]);h.assign("currentRunId","left");h.assign("currentRunConfig",left.config);h.assign("comparisonRunId","right");
  const draftBefore = h.evaluate("JSON.stringify(readConfig())"); const posts = [];
  h.replace("api", async (path, options) => {
    if (path === "/api/backtest/start") { posts.push(JSON.parse(options.body)); return { job_id: `j${posts.length}` }; }
    return path.endsWith("/left") ? left : right;
  });
  h.replace("waitForBacktestJob", async (id) => ({ run_id:id, summary:{ start_date:"2020-01-02",end_date:"2021-12-31",total_fees_cny:123,rebalance_trade_count:4 } }));
  await h.evaluate("runCommonComparison()");
  assert.equal(posts.length, 2);
  assert.ok(posts.every(post => post.config.start_date === "2020-01-01" && post.config.end_date === "2021-12-31"));
  assert.deepEqual(posts.map(post => post.config.rebalance_band), [.05,.25]);
  assert.equal(h.evaluate("currentRunId"), "left");
  assert.equal(h.evaluate("JSON.stringify(readConfig())"), draftBefore);
  assert.equal(h.evaluate("commonComparison.entries.length"), 2);
  assert.match(h.el("historyComparison").innerHTML, /总费用/);
  assert.match(h.el("historyComparison").innerHTML, /2020-01-02/);
});


test("event weights omit unused assets and scheduled triggers explain their actual frequency and threshold", () => {
  const h = harness();
  const rows = JSON.parse(h.evaluate('JSON.stringify(eventWeightRows({payload:{before_weights:{GOLD:.3,REPO:.7,UNUSED:0},after_weights:{GOLD:.25,REPO:.75,UNUSED:0}}}))'));
  assert.deepEqual(rows.map(row=>row.symbol), ["GOLD","REPO"]);
  assert.match(h.evaluate('rebalanceEventReason({rebalance_reason:"scheduled_open",rebalance_frequency:"monthly",rebalance_band:.25,threshold_exceeded:true})'), /每月检查.*超出25\.00%相对容忍带/);
  assert.match(h.evaluate('rebalanceEventReason({event_type:"initial_allocation",rebalance_reason:"initial"})'), /首次配置建仓/);
});


test("exact event focus loads only absent dates and ignores an older date response", async () => {
  const h = harness(); const first = deferred(), second = deferred(), calls = [], actions = [];
  h.context.window.echarts = {};
  h.assign("currentRunId","a"); h.assign("currentChartSeries",[{trade_date:"2020-01-01"}]);
  h.context.__chart = { dispatchAction: action=>actions.push(action), setOption() {} };
  h.evaluate('charts.assetChart=__chart');
  h.replace("renderCharts",rows=>h.assign("currentChartSeries",rows));
  h.replace("expandChartSeries",data=>data.series);h.replace("computeSeriesMetrics",rows=>rows);
  h.replace("api",path=>{calls.push(path);return path.includes("2020-01-02")?first.promise:second.promise;});
  h.assign("selectedTradeDate","2020-01-02");const pendingFirst=h.evaluate('focusRebalanceChart("2020-01-02","assetChart")');
  h.assign("selectedTradeDate","2020-01-03");const pendingSecond=h.evaluate('focusRebalanceChart("2020-01-03","assetChart")');
  second.resolve({series:[{trade_date:"2020-01-01"},{trade_date:"2020-01-03"}]});await pendingSecond;
  first.resolve({series:[{trade_date:"2020-01-01"},{trade_date:"2020-01-02"}]});await pendingFirst;
  assert.equal(h.evaluate("currentChartSeries[1].trade_date"),"2020-01-03");
  assert.match(h.el("eventChartPosition").textContent,/精确定位 2020-01-03/);
  await h.evaluate('focusRebalanceChart("2020-01-03","assetChart")');
  assert.equal(calls.length,2,"existing date does not reload series");
  assert.ok(actions.some(action=>action.type==="showTip"&&action.dataIndex===1));
});

test("event focus response cannot replace a newly selected run and exposes retry on failure", async () => {
  const h=harness();const late=deferred();h.context.window.echarts={};
  h.assign("currentRunId","a");h.assign("selectedTradeDate","2020-01-02");h.assign("currentChartSeries",[]);
  h.replace("api",()=>late.promise);let rendered=0;h.replace("renderCharts",()=>rendered++);
  const pending=h.evaluate('focusRebalanceChart("2020-01-02","assetChart")');
  h.assign("currentRunId","b");late.resolve({series:[]});await pending;assert.equal(rendered,0);
  h.replace("api",async()=>{throw new Error("missing date")});
  await h.evaluate('focusRebalanceChart("2020-01-02","assetChart")');
  assert.match(h.el("eventChartPosition").innerHTML,/重试定位当天/);
  assert.ok(!h.el("eventChartPosition").innerHTML.includes("最接近"));
});

test("identity switch clears private caches and rejects a late metadata response", async () => {
  const h=harness();const late=deferred();h.assign("runHistory",[{...entry("a"),metadata:{name:"private-A",note:"A",favorite:true}}]);
  h.evaluate('researchTasks.set("a:rebalance_grid",{run_id:"a",kind:"rebalance_grid",status:"running",identityVersion:0});commonComparison={key:"private-A"};metadataEditingRunId="a"');
  h.replace("api",(path)=>path.endsWith("/metadata")?late.promise:Promise.resolve({records:[{...entry("a"),metadata:{name:"",note:"",favorite:false}}]}));
  const save=h.evaluate('updateHistoryMetadata("a",{name:"late-private-A"})');
  h.evaluate("resetLeaderboardForIdentity()");await tick();
  late.resolve({metadata:{name:"late-private-A",note:"secret",favorite:true}});await save;
  assert.equal(h.evaluate("researchTasks.size"),0);assert.equal(h.evaluate("commonComparison"),null);
  assert.equal(h.evaluate("metadataEditingRunId"),null);
  assert.ok(!h.el("historyList").innerHTML.includes("private-A"));
  assert.equal(h.evaluate("runHistory[0].metadata.favorite"),false);
});

test("loading saved parameters respects latest selection and cannot overwrite a started calculation", async () => {
  const h=harness();const a=deferred(),b=deferred();
  h.replace("api",path=>path.endsWith("/a")?a.promise:b.promise);
  const first=h.evaluate('copySavedRunToDraft("a")'),second=h.evaluate('copySavedRunToDraft("b")');
  const ca=entry("a"),cb=entry("b");ca.config.initial_capital_cny=111111;cb.config.initial_capital_cny=222222;
  b.resolve(cb);await second;a.resolve(ca);await first;
  assert.equal(h.evaluate("config.initial_capital_cny"),222222);
  const delayed=deferred();h.replace("api",()=>delayed.promise);
  const pending=h.evaluate('copySavedRunToDraft("a")');h.assign("runInProgress",true);delayed.resolve(ca);await pending;
  assert.equal(h.evaluate("config.initial_capital_cny"),222222);
});

test("mobile research cards prioritize outcomes while methodology excludes internal engine labels", () => {
  const h=harness();researchControls(h);
  h.context.__task={status:"completed",completed:1,total:1,rows:[{id:"rebalance_grid:1",status:"success",inputs:{rebalance_frequency:"monthly",rebalance_band:.25},annualized_return:.08,max_drawdown:-.12,total_fees_cny:123,rebalance_trade_count:9,start_date:"2019-01-02",end_date:"2024-12-31"}],methodology:{engine:"same_backtest_engine",fixed_conditions:"固定基准",annual_only_rules:"年度专用规则",withdrawal_growth:"消费增长说明"}};
  h.evaluate('researchTasks.set("saved-base:rebalance_grid",__task);renderResearch()');
  assert.match(h.el("rebalanceResearchCards").innerHTML,/每月.*容忍带 25.00%/);
  assert.match(h.el("rebalanceResearchCards").innerHTML,/年化收益/);
  assert.match(h.el("rebalanceResearchCards").innerHTML,/总费用/);
  assert.match(h.el("rebalanceResearchStatus").textContent,/成功 1 \/ 失败 0/);
  assert.ok(!h.el("rebalanceResearchStatus").textContent.includes("固定基准"));
  assert.ok(!h.el("rebalanceResearchMethodology").innerHTML.includes("same_backtest_engine"));
  assert.ok(!h.el("rebalanceResearchMethodology").innerHTML.includes("消费增长说明"));
  assert.ok(!h.el("rebalanceResearchTable").innerHTML.includes("rebalance_grid:1"));
});

test("drawer close restores the explicit trigger even when Safari leaves activeElement on body", () => {
  const h=harness();h.replace("syncDrawerAccessibility",()=>{});
  let focused=0;h.el("mobileParameterToggle").focus=()=>focused++;
  h.context.document.activeElement=h.context.document.body;
  h.evaluate('setParameterPanel(true,$("mobileParameterToggle"));setParameterPanel(false)');
  assert.equal(focused,1);
});


test("research navigation opens its inline section", () => {
  const h=harness();h.el("researchSection").open=false;h.evaluate("setupResearchInteractions()");
  h.el("openResearchSection").dispatchEvent({type:"click"});assert.equal(h.el("researchSection").open,true);
});


test("common-period progress including network recovery stays in the comparison area", async () => {
  const h=harness(),left=entry("left"),right=entry("right");
  h.assign("runHistory",[left,right]);h.assign("currentRunId","left");h.assign("currentRunConfig",left.config);h.assign("comparisonRunId","right");
  h.el("message").textContent="已回放保存的回测结果";h.el("workspaceMessage").textContent="已回放保存的回测结果";
  const progress=[],polls=new Map();let jobs=0;
  h.replace("renderHistoryComparison",()=>progress.push(h.evaluate("commonComparison?.message")));
  h.replace("sleep",async()=>{});
  h.replace("api",async(path)=>{
    if(path==="/api/backtest/start")return {job_id:`j${++jobs}`};
    if(path.includes("/jobs/")){
      const n=(polls.get(path)||0)+1;polls.set(path,n);
      if(n===1){const error=new Error("temporary network issue");error.network=true;throw error;}
      if(n===2)return {status:"running",message:"正在检查数据并运行回测"};
      return {status:"completed",result:{run_id:path,summary:{annualized_return:.05}}};
    }
    return path.endsWith("/left")?left:right;
  });
  await h.evaluate("runCommonComparison()");
  assert.ok(progress.some(text=>text?.includes("网络短暂波动")));
  assert.ok(progress.some(text=>text?.includes("第 2 / 2 组 · 正在检查数据并运行回测")));
  assert.match(h.evaluate("commonComparison.message"),/两组同期回测已完成/);
  assert.equal(h.el("message").textContent,"已回放保存的回测结果");
  assert.equal(h.el("workspaceMessage").textContent,"已回放保存的回测结果");
});

test("ordinary backtest polling still sends default progress to the workspace", async()=>{
  const h=harness();let polls=0;h.replace("sleep",async()=>{});
  h.replace("api",async()=>++polls===1?{status:"running",message:"普通回测计算中"}:{status:"completed",result:{run_id:"normal"}});
  const result=await h.evaluate('waitForBacktestJob("normal")');
  assert.equal(result.run_id,"normal");
  assert.equal(h.el("workspaceMessage").textContent,"普通回测计算中");
});

test("stale common-period progress cannot overwrite another comparison state",async()=>{
  const h=harness(),left=entry("left"),right=entry("right");
  h.assign("runHistory",[left,right]);h.assign("currentRunId","left");h.assign("currentRunConfig",left.config);h.assign("comparisonRunId","right");
  h.replace("api",async(path)=>path==="/api/backtest/start"?{job_id:"j"}:path.endsWith("/left")?left:right);
  h.replace("waitForBacktestJob",async(_id,onProgress)=>{
    h.evaluate('commonComparisonVersion+=1;commonComparison={key:"new:pair",message:"新比较状态"}');
    onProgress("旧任务迟到进度");return {run_id:"old",summary:{}};
  });
  await h.evaluate("runCommonComparison()");
  assert.equal(h.evaluate("commonComparison.message"),"新比较状态");
});


test("parameter fee differences show only changed leaves with precise readable units", () => {
  const h=harness();const a=fixtureConfig(),b=structuredClone(a);
  a.fees.cn_etf.commission_rate=.00025;b.fees.cn_etf.commission_rate=.0003;
  a.fees.ibkr_us_etf.fixed_per_share_usd=.005;b.fees.ibkr_us_etf.fixed_per_share_usd=.0065;
  b.fees.ibkr_us_etf.plan="pro_tiered";
  b.fees.fx.bank_out_spread_bps=25;
  a.fees.hk_connect_etf.portfolio_fee_annual_rate=.00008;b.fees.hk_connect_etf.portfolio_fee_annual_rate=.0001;
  a.fees.hk_connect_etf.afrc_transaction_levy_rate=.0000015;b.fees.hk_connect_etf.afrc_transaction_levy_rate=.000002;
  b.fees.tax.us_dividend_withholding_rate=.3;
  h.context.__a=a;h.context.__b=b;
  const rows=JSON.parse(h.evaluate('JSON.stringify(feeDifferenceRows(__a.fees,__b.fees))'));
  const byName=Object.fromEntries(rows.map(([label,...values])=>[label,values]));
  assert.deepEqual(byName["境内基金佣金率"],["0.025% / 边","0.03% / 边"]);
  assert.equal(h.evaluate('feeDifferenceRows({cn_etf:{commission_rate:.000025}},{cn_etf:{commission_rate:.00003}})[0][1]'),"0.0025% / 边");
  assert.deepEqual(byName["美股固定佣金（每股）"],["0.005 美元 / 股","0.0065 美元 / 股"]);
  assert.deepEqual(byName["美国券商费率类型"],["固定费率","阶梯费率"]);
  assert.deepEqual(byName["出金购汇点差"],["10 基点","25 基点"]);
  assert.deepEqual(byName["港股通组合费年率"],["0.008% / 年","0.01% / 年"]);
  assert.deepEqual(byName["港股通会财局征费率"],["0.00015% / 边","0.0002% / 边"]);
  assert.deepEqual(byName["美国分红预扣税率"],["10%","30%"]);
  const html=h.evaluate('configDifferenceMarkup(__a,__b)');
  assert.ok(!html.includes("bank_in_spread_bps"));assert.ok(!html.includes("入金结汇点差"));
  assert.ok(!html.includes('"cn_etf"'));assert.ok(!html.includes("pro_tiered"));
});

test("parameter differences translate strategy choices and cash instruments, and escape unknown fee fields",()=>{
  const h=harness();const a=fixtureConfig(),b=structuredClone(a);
  a.repo_options=[{symbol:"204001",name:"1天国债逆回购"},{symbol:"511990.SH",name:"华宝添益货币ETF"}];
  b.repo_target_mode="fixed_bucket";b.repo_symbol="511990.SH";b.dip_buy_level_mode="multiplier";b.dip_buy_cost_basis_mode="initial";
  a.fees.extra={special:'<script>old</script>'};b.fees.extra={special:'<script>new</script>'};
  h.context.__a=a;h.context.__b=b;
  const html=h.evaluate('configDifferenceMarkup(__a,__b)');
  for(const value of ["按剩余权重","固定消费池","每档补 1 份","第 N 档补 N 份","目前持仓成本","最初成本","1天国债逆回购（204001）","华宝添益货币ETF（511990.SH）","其他费用 · extra.special"]) assert.ok(html.includes(value),value);
  for(const internal of ["residual_weight","fixed_bucket","current_average","multiplier","<script>"]) assert.ok(!html.includes(internal),internal);
  assert.ok(html.includes("&lt;script&gt;new&lt;/script&gt;"));
});
