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
    monthly_spend_cny: 0, repo_target_mode: "residual_weight", repo_fixed_target_cny: 0,
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
  assert.deepEqual(tables.map((table) => table.count), [0, 0, 0, 0]);
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
  assert.equal(h.el("startDate").value, "2016-04-01");
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
