let config = null;
let currentRunId = null;
const charts = {};
const APP_BASE_PATH = window.location.pathname.startsWith("/portfolio/") || window.location.pathname === "/portfolio"
  ? "/portfolio"
  : "";

const $ = (id) => document.getElementById(id);
const fmtMoney = (v) => Number(v || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
const fmtPct = (v) => `${(Number(v || 0) * 100).toFixed(2)}%`;
const fmtNum = (v, d = 2) => Number(v || 0).toFixed(d);

const STATIC_NAMES = {
  VOO: "标普500指数基金",
  "512890.SH": "红利低波基金",
  "510300.SH": "沪深300基金",
  "518880.SH": "黄金基金",
  "000300.SH": "沪深300指数",
  "204001": "1天国债逆回购",
  "204002": "2天国债逆回购",
  "204003": "3天国债逆回购",
  "204004": "4天国债逆回购",
  "204007": "7天国债逆回购",
  "204014": "14天国债逆回购",
  "204028": "28天国债逆回购",
  "204091": "91天国债逆回购",
  "204182": "182天国债逆回购",
  "USD/CNY": "美元兑人民币汇率",
  REPO: "国债逆回购",
};

const SHORT_NAMES = {
  VOO: "标普500",
  "512890.SH": "红利",
  "510300.SH": "沪深300",
  "518880.SH": "黄金",
  REPO: "逆回购",
};

const DATA_KIND_NAMES = {
  price: "行情",
  dividend: "分红",
  adj_factor: "复权因子",
  fx: "汇率",
  repo: "逆回购利率",
};

const SIDE_NAMES = { BUY: "买入", SELL: "卖出" };
const REASON_NAMES = { rebalance: "再平衡", liquidity_shortfall: "补足现金" };
const CURRENCY_NAMES = { CNY: "人民币", USD: "美元" };
const SOURCE_NAMES = {
  "sohu:hisHq": "搜狐历史行情",
  "eastmoney:repo_kline": "东方财富逆回购行情",
  "eastmoney:fund_kline": "东方财富基金行情",
  "akshare:bond_buy_back_hist_em": "公开逆回购历史行情",
  "yahoo:CNY=X": "雅虎财经汇率",
  "stooq:usdcny": "公开汇率行情",
  "frankfurter:USD-CNY": "欧洲公开汇率",
  "tushare:fund_daily": "专业基金日线",
  "tushare:index_daily": "专业指数日线",
  "tushare:fund_div": "专业基金分红",
  "tushare:fund_adj": "专业复权因子",
};

function assetName(symbol) {
  const configured = config?.assets?.find((asset) => asset.symbol === symbol);
  const repo = config?.repo_options?.find((option) => option.symbol === symbol);
  if (repo) return repo.name;
  return STATIC_NAMES[symbol] || configured?.name || symbol;
}

function formatSource(value) {
  return String(value || "")
    .split(",")
    .filter(Boolean)
    .map((source) => {
      if (SOURCE_NAMES[source]) return SOURCE_NAMES[source];
      if (source.startsWith("datasrc:")) return "本地真实数据缓存";
      if (source.startsWith("yahoo:")) return "雅虎财经行情";
      if (source.startsWith("stooq:")) return "公开市场行情";
      if (source.startsWith("sohu:")) return "搜狐历史行情";
      if (source.startsWith("tushare:")) return "专业金融数据";
      return "公开数据源";
    })
    .join("、");
}

function describeMissing(item) {
  const [kind, symbol] = String(item || "").split(":");
  if (kind === "fx_rates") return "美元兑人民币汇率";
  if (kind === "repo_rates") return "国债逆回购利率";
  const kindName = {
    prices: "行情",
    dividends: "分红",
    adj_factors: "复权因子",
  }[kind] || "数据";
  return `${assetName(symbol)}${kindName}`;
}

function humanizeError(text) {
  const raw = String(text || "");
  const autoMissingPrefix = "自动补足数据后仍缺少：";
  if (raw.startsWith(autoMissingPrefix)) {
    const items = raw.slice(autoMissingPrefix.length).split("、").filter(Boolean);
    return `${autoMissingPrefix}${items.map(describeMissing).join("、")}`;
  }
  return raw
    .replace("missing fx_rates or repo_rates; run data sync first", "缺少汇率或逆回购利率，请重新运行回测以自动补足")
    .replace("database contains generated/mock data in the requested range; purge and resync real/public data first", "所选区间仍有生成数据，请清理后重新同步真实或公开数据")
    .replaceAll("prices:", "行情：")
    .replaceAll("repo_rates:", "逆回购利率：")
    .replaceAll("fx_rates:", "汇率：");
}

async function api(path, options = {}) {
  const response = await fetch(`${APP_BASE_PATH}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function setMessage(text, isError = false) {
  $("message").textContent = text || "";
  $("message").className = isError ? "message error" : "message";
}

function readConfig() {
  const next = structuredClone(config);
  next.initial_capital_cny = Number($("initialCapital").value);
  next.start_date = $("startDate").value;
  next.end_date = $("endDate").value;
  next.rebalance_frequency = $("rebalanceFrequency").value;
  next.rebalance_band = Number($("rebalanceBand").value);
  next.monthly_spend_cny = Number($("monthlySpend").value);
  next.repo_symbol = $("repoSymbol").value;
  next.assets = next.assets.map((asset) => ({
    ...asset,
    enabled: $(`enabled_${asset.key}`).checked,
    target_weight: Number($(`weight_${asset.key}`).value),
  }));
  next.fees.cn_etf.commission_rate = Number($("cnCommission").value);
  next.fees.ibkr_us_etf.plan = $("ibkrPlan").value;
  next.fees.fx.bank_out_spread_bps = Number($("fxOutBps").value);
  next.fees.fx.bank_in_spread_bps = Number($("fxInBps").value);
  next.fees.tax.us_dividend_withholding_rate = Number($("usDividendTax").value);
  return next;
}

function updateRepoWeight() {
  const enabledWeight = config.assets.reduce((sum, asset) => {
    const enabledEl = $(`enabled_${asset.key}`);
    const weightEl = $(`weight_${asset.key}`);
    return sum + ((enabledEl?.checked ?? asset.enabled) ? Number(weightEl?.value ?? asset.target_weight) : 0);
  }, 0);
  const repoWeight = Math.max(1 - enabledWeight, 0);
  $("repoWeight").textContent = fmtPct(repoWeight);
  $("repoWeight").style.color = enabledWeight > 1 ? "#b42318" : "";
}

function renderControls() {
  $("initialCapital").value = config.initial_capital_cny;
  $("startDate").value = config.start_date;
  $("endDate").value = config.end_date;
  $("rebalanceFrequency").value = config.rebalance_frequency;
  $("rebalanceBand").value = config.rebalance_band;
  $("bandValue").textContent = fmtPct(config.rebalance_band);
  $("monthlySpend").value = config.monthly_spend_cny;
  $("repoSymbol").innerHTML = (config.repo_options || []).map((option) => `<option value="${option.symbol}">${option.name}</option>`).join("");
  $("repoSymbol").value = config.repo_symbol;
  $("cnCommission").value = config.fees.cn_etf.commission_rate;
  $("ibkrPlan").value = config.fees.ibkr_us_etf.plan;
  $("fxOutBps").value = config.fees.fx.bank_out_spread_bps;
  $("fxInBps").value = config.fees.fx.bank_in_spread_bps;
  $("usDividendTax").value = config.fees.tax.us_dividend_withholding_rate;

  const host = $("assetControls");
  host.innerHTML = "";
  for (const asset of config.assets) {
    const row = document.createElement("div");
    row.className = "asset-control";
    row.innerHTML = `
      <input id="enabled_${asset.key}" type="checkbox" ${asset.enabled ? "checked" : ""} />
      <input id="weight_${asset.key}" type="range" min="0" max="0.8" step="0.01" value="${asset.target_weight}" />
      <strong id="weight_label_${asset.key}">${fmtPct(asset.target_weight)}</strong>
      <div class="asset-name">${assetName(asset.symbol)}</div>
    `;
    host.appendChild(row);
    row.querySelector(`#enabled_${asset.key}`).addEventListener("change", updateRepoWeight);
    row.querySelector(`#weight_${asset.key}`).addEventListener("input", (event) => {
      row.querySelector(`#weight_label_${asset.key}`).textContent = fmtPct(event.target.value);
      updateRepoWeight();
    });
  }
  $("rebalanceBand").addEventListener("input", () => {
    $("bandValue").textContent = fmtPct($("rebalanceBand").value);
  });
  updateRepoWeight();
}

function renderStatus(rows) {
  const displayRows = rows.map((row) => ({
    数据类型: DATA_KIND_NAMES[row.kind] || row.kind,
    标的名称: assetName(row.symbol),
    开始日期: row.start_date,
    结束日期: row.end_date,
    记录数: Number(row.rows || 0).toLocaleString("zh-CN"),
    数据来源: formatSource(row.sources),
  }));
  renderTable("statusTable", ["数据类型", "标的名称", "开始日期", "结束日期", "记录数", "数据来源"], displayRows);
}

function summarizeDataQuality(rows) {
  const total = rows.length;
  const fixture = rows.filter((row) => String(row.sources || "").includes("fixture:")).length;
  return { total, fixture, real: total - fixture };
}

function renderSummary(summary) {
  const items = [
    ["期末总资产", `￥${fmtMoney(summary.final_asset_cny)}`],
    ["累计收益", fmtPct(summary.total_return)],
    ["年化收益", fmtPct(summary.annualized_return)],
    ["最大回撤", fmtPct(summary.max_drawdown)],
    ["总手续费", `￥${fmtMoney(summary.total_fees_cny)}`],
    ["总消费", `￥${fmtMoney(summary.total_spend_cny)}`],
    ["对比组合期末资产", `￥${fmtMoney(summary.comparison_final_asset_cny)}`],
    ["浮盈浮亏", `￥${fmtMoney(summary.final_unrealized_pnl_cny)}`],
    ["再平衡次数", fmtNum(summary.rebalance_count, 0)],
    ["交易次数", fmtNum(summary.trade_count, 0)],
    ["分红预扣税", `￥${fmtMoney(summary.withheld_tax_cny)}`],
  ];
  $("summaryGrid").innerHTML = items.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
}

function daysBetween(start, end) {
  const startTime = new Date(`${start}T00:00:00`).getTime();
  const endTime = new Date(`${end}T00:00:00`).getTime();
  return Math.max((endTime - startTime) / 86400000, 0);
}

function stdDev(values) {
  if (!values.length) return 0;
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const variance = values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length;
  return Math.sqrt(variance);
}

function computeSeriesMetrics(rows) {
  let strategyNav = 1;
  let strategyPeak = 1;
  let previousTotal = null;
  let benchmarkBase = null;
  let latestBenchmark = null;
  return rows.map((row) => {
    const payload = row.payload || {};
    const total = Number(row.total_asset_cny || 0);
    const flow = Number(row.flow_cny || 0);
    const dailyReturn = previousTotal && previousTotal !== 0 ? (total - previousTotal - flow) / previousTotal : 0;
    strategyNav *= 1 + dailyReturn;
    strategyPeak = Math.max(strategyPeak, strategyNav);
    const drawdown = strategyPeak ? strategyNav / strategyPeak - 1 : 0;
    const rawBenchmark = payload.benchmark_value;
    if (rawBenchmark != null && Number(rawBenchmark) > 0) {
      latestBenchmark = Number(rawBenchmark);
      if (benchmarkBase == null) benchmarkBase = latestBenchmark;
    }
    previousTotal = total;
    return {
      ...row,
      payload,
      daily_return: dailyReturn,
      cumulative_return: strategyNav - 1,
      drawdown,
      benchmark_return: benchmarkBase && latestBenchmark ? latestBenchmark / benchmarkBase - 1 : 0,
    };
  });
}

function deriveSummary(summary, series) {
  if (!series.length) return summary;
  const last = series.at(-1);
  const totalReturn = last.cumulative_return || 0;
  const years = Math.max(daysBetween(series[0].trade_date, last.trade_date) / 365.25, 1 / 365.25);
  return {
    ...summary,
    final_asset_cny: last.total_asset_cny,
    total_return: totalReturn,
    annualized_return: (1 + totalReturn) ** (1 / years) - 1,
    max_drawdown: Math.min(...series.map((row) => row.drawdown ?? 0)),
    volatility: stdDev(series.slice(1).map((row) => row.daily_return || 0)) * Math.sqrt(252),
    comparison_final_asset_cny: last.payload?.comparison?.total_asset_cny ?? summary.comparison_final_asset_cny,
  };
}

function ensureChart(id) {
  if (!charts[id]) charts[id] = echarts.init($(id));
  return charts[id];
}

function renderCharts(series) {
  if (!series.length) return;
  if (!window.echarts) {
    renderFallbackCharts(series);
    return;
  }
  const dates = series.map((row) => row.trade_date);
  ensureChart("assetChart").setOption({
    title: { text: "总资产", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", scale: true },
    series: [{ type: "line", name: "总资产", data: series.map((row) => row.total_asset_cny), smooth: true, symbol: "none" }],
  });
  ensureChart("comparisonChart").setOption({
    title: { text: "总资产对比", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => `￥${fmtMoney(v)}` },
    legend: { top: 4, right: 10 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", scale: true },
    series: [
      { type: "line", name: "当前策略", data: series.map((row) => row.total_asset_cny), smooth: true, symbol: "none" },
      {
        type: "line",
        name: "沪深300基金加黄金基金加国债逆回购",
        data: series.map((row) => row.payload.comparison?.total_asset_cny ?? null),
        smooth: true,
        symbol: "none",
      },
    ],
  });
  ensureChart("returnChart").setOption({
    title: { text: "收益率对比沪深300", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    legend: { top: 4, right: 10 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: [
      { type: "line", name: "策略", data: series.map((row) => row.cumulative_return), smooth: true, symbol: "none" },
      { type: "line", name: "沪深300", data: series.map((row) => row.benchmark_return), smooth: true, symbol: "none" },
    ],
  });
  ensureChart("drawdownChart").setOption({
    title: { text: "回撤", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: [{ type: "line", areaStyle: {}, name: "回撤", data: series.map((row) => row.drawdown), symbol: "none" }],
  });

  const symbols = Object.keys(series.at(-1)?.payload?.values || {});
  ensureChart("weightChart").setOption({
    title: { text: "资产权重", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    legend: { top: 4, right: 10 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", max: 1, axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: symbols.map((symbol) => ({
      type: "line",
      stack: "weights",
      areaStyle: {},
      name: assetName(symbol),
      data: series.map((row) => row.payload.weights[symbol] || 0),
      symbol: "none",
    })),
  });
}

function renderFallbackCharts(series) {
  const makePointSeries = (values) => values.map((value, index) => ({ x: index, y: Number(value || 0) }));
  drawFallbackChart("assetChart", "总资产", [{ name: "总资产", color: "#1f7a5a", points: makePointSeries(series.map((row) => row.total_asset_cny)) }], false);
  drawFallbackChart("comparisonChart", "总资产对比", [
    { name: "当前策略", color: "#1f7a5a", points: makePointSeries(series.map((row) => row.total_asset_cny)) },
    { name: "沪深300基金加黄金基金加国债逆回购", color: "#2f5aa8", points: makePointSeries(series.map((row) => row.payload.comparison?.total_asset_cny)) },
  ], false);
  drawFallbackChart("returnChart", "收益率对比沪深300", [
    { name: "策略", color: "#1f7a5a", points: makePointSeries(series.map((row) => row.cumulative_return)) },
    { name: "沪深300", color: "#2f5aa8", points: makePointSeries(series.map((row) => row.benchmark_return)) },
  ], true);
  drawFallbackChart("drawdownChart", "回撤", [{ name: "回撤", color: "#b42318", points: makePointSeries(series.map((row) => row.drawdown)) }], true);
  const symbols = Object.keys(series.at(-1)?.payload?.weights || {});
  const colors = ["#1f7a5a", "#2f5aa8", "#b45f06", "#7a3db8", "#667085"];
  drawFallbackChart("weightChart", "资产权重", symbols.map((symbol, index) => ({
    name: assetName(symbol),
    color: colors[index % colors.length],
    points: makePointSeries(series.map((row) => row.payload.weights[symbol] || 0)),
  })), true, 0, 1);
}

function drawFallbackChart(id, title, lineSeries, percentAxis, forcedMin = null, forcedMax = null) {
  const host = $(id);
  const width = Math.max(host.clientWidth || 520, 320);
  const height = Math.max(host.clientHeight || 300, 240);
  const pad = { top: 34, right: 18, bottom: 30, left: 54 };
  const allY = lineSeries.flatMap((line) => line.points.map((point) => point.y));
  let minY = forcedMin ?? Math.min(...allY);
  let maxY = forcedMax ?? Math.max(...allY);
  if (minY === maxY) {
    minY -= 1;
    maxY += 1;
  }
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const xMax = Math.max(...lineSeries.flatMap((line) => line.points.map((point) => point.x)), 1);
  const sx = (x) => pad.left + (x / xMax) * innerW;
  const sy = (y) => pad.top + (1 - (y - minY) / (maxY - minY)) * innerH;
  const yLabel = (value) => percentAxis ? fmtPct(value) : fmtMoney(value);
  const paths = lineSeries.map((line) => {
    const d = line.points.map((point, index) => `${index ? "L" : "M"}${sx(point.x).toFixed(1)},${sy(point.y).toFixed(1)}`).join(" ");
    return `<path d="${d}" fill="none" stroke="${line.color}" stroke-width="2" vector-effect="non-scaling-stroke" />`;
  }).join("");
  const legend = lineSeries.map((line, index) => {
    const x = pad.left + index * 94;
    return `<g transform="translate(${x},18)"><rect width="10" height="10" fill="${line.color}"/><text x="15" y="10" font-size="11" fill="#667085">${line.name}</text></g>`;
  }).join("");
  host.innerHTML = `
    <svg width="100%" height="100%" viewBox="0 0 ${width} ${height}" role="img" aria-label="${title}">
      <text x="8" y="18" font-size="14" font-weight="600" fill="#1d2733">${title}</text>
      ${legend}
      <line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}" stroke="#d9dee7"/>
      <line x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" stroke="#d9dee7"/>
      <text x="4" y="${pad.top + 4}" font-size="11" fill="#667085">${yLabel(maxY)}</text>
      <text x="4" y="${height - pad.bottom}" font-size="11" fill="#667085">${yLabel(minY)}</text>
      ${paths}
    </svg>
  `;
}

function renderTable(id, columns, rows) {
  const table = $(id);
  if (!rows.length) {
    table.innerHTML = "<tbody><tr><td>暂无数据</td></tr></tbody>";
    return;
  }
  table.innerHTML = `
    <thead><tr>${columns.map((col) => `<th>${col}</th>`).join("")}</tr></thead>
    <tbody>
      ${rows.map((row) => `<tr>${columns.map((col) => `<td>${formatCell(row[col])}</td>`).join("")}</tr>`).join("")}
    </tbody>
  `;
}

function formatCell(value) {
  if (value && typeof value === "object" && value.kind === "performance") {
    return formatPerformanceCell(value);
  }
  if (typeof value === "number") {
    const formatted = Math.abs(value) < 1 && value !== 0 ? fmtPct(value) : fmtNum(value, 2);
    return value < 0 ? `<span class="negative">${formatted}</span>` : formatted;
  }
  return value ?? "";
}

function formatPerformanceCell(value) {
  const profit = value.profit;
  const rate = value.rate;
  const profitText = profit === "" || profit == null ? "-" : `￥${fmtMoney(profit)}`;
  const rateText = rate === "" || rate == null ? "-" : fmtPct(rate);
  const className = Number(profit || 0) < 0 || Number(rate || 0) < 0 ? "negative" : "";
  return `<span class="${className}">${profitText} / ${rateText}</span>`;
}

function rebalanceDisplayRows(rows) {
  const symbols = [];
  for (const row of rows) {
    for (const symbol of Object.keys(row.payload?.asset_performance || {})) {
      if (!symbols.includes(symbol)) symbols.push(symbol);
    }
  }
  const orderedSymbols = [...config.assets.map((asset) => asset.symbol), "REPO"].filter((symbol) => symbols.includes(symbol));
  for (const symbol of symbols) {
    if (!orderedSymbols.includes(symbol)) orderedSymbols.push(symbol);
  }
  const baseColumns = ["日期", "区间", "当回撤", "前资产", "后资产", "成交", "费"];
  const assetColumns = orderedSymbols.map((symbol) => SHORT_NAMES[symbol] || assetName(symbol));
  const displayRows = rows.map((row) => {
    const item = {
      日期: row.rebalance_date,
      区间: row.period_return,
      当回撤: row.payload?.period_max_drawdown ?? 0,
      前资产: row.total_asset_before,
      后资产: row.total_asset_after,
      成交: row.turnover_cny,
      费: row.fee_cny,
    };
    for (const symbol of orderedSymbols) {
      const perf = row.payload?.asset_performance?.[symbol] || {};
      item[SHORT_NAMES[symbol] || assetName(symbol)] = {
        kind: "performance",
        profit: perf.profit_cny ?? "",
        rate: perf.return ?? "",
      };
    }
    return item;
  });
  return { columns: [...baseColumns, ...assetColumns], rows: displayRows };
}

async function loadStatus() {
  const data = await api("/api/data/status");
  renderStatus(data.status || []);
}

async function runBacktest() {
  const button = $("runBtn");
  button.disabled = true;
  setMessage("正在检查数据并运行回测...");
  try {
    const result = await api("/api/backtest/run", { method: "POST", body: JSON.stringify({ config: readConfig() }) });
    currentRunId = result.run_id;
    if (result.status) renderStatus(result.status);
    const [series, rebalance, trades] = await Promise.all([
      api(`/api/backtest/${currentRunId}/series`),
      api(`/api/backtest/${currentRunId}/rebalance`),
      api(`/api/backtest/${currentRunId}/trades`),
    ]);
    const computedSeries = computeSeriesMetrics(series.series || []);
    renderSummary(deriveSummary(result.summary, computedSeries));
    renderCharts(computedSeries);
    const rebalanceTable = rebalanceDisplayRows(rebalance.rebalance || []);
    renderTable("rebalanceTable", rebalanceTable.columns, rebalanceTable.rows);
    renderTable(
      "tradesTable",
      ["交易日期", "标的名称", "方向", "份额", "价格", "成交额", "费用", "币种", "原因"],
      (trades.trades || []).map((row) => ({
        交易日期: row.trade_date,
        标的名称: assetName(row.symbol),
        方向: SIDE_NAMES[row.side] || row.side,
        份额: row.quantity,
        价格: row.price,
        成交额: row.gross_amount,
        费用: row.fee,
        币种: CURRENCY_NAMES[row.currency] || row.currency,
        原因: REASON_NAMES[row.reason] || row.reason,
      })),
    );
    if (result.cache?.hit) {
      setMessage("参数一致，已直接读取历史回测结果");
    } else if (result.data_sync?.triggered) {
      const quality = summarizeDataQuality(result.status || []);
      setMessage(`数据已自动补足，回测完成：${quality.real} 项真实/公开源`);
    } else {
      setMessage("数据充足，回测完成");
    }
  } catch (error) {
    setMessage(humanizeError(error.message), true);
  } finally {
    button.disabled = false;
  }
}

async function init() {
  config = await api("/api/default-config");
  renderControls();
  await loadStatus();
  $("runBtn").addEventListener("click", runBacktest);
  window.addEventListener("resize", () => Object.values(charts).forEach((chart) => chart.resize()));
}

init().catch((error) => setMessage(error.message, true));
