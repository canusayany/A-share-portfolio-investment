let config = null;
let currentRunId = null;
const charts = {};
let activeChartId = "assetChart";
const APP_BASE_PATH = window.location.pathname.startsWith("/portfolio/") || window.location.pathname === "/portfolio"
  ? "/portfolio"
  : "";
const SP500_GROUP = "sp500";
const SP500_CONTROL_KEY = "sp500_group";

const $ = (id) => document.getElementById(id);
const fmtMoney = (v) => Number(v || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
const fmtPct = (v) => `${(Number(v || 0) * 100).toFixed(2)}%`;
const fmtRate = (v, d = 4) => `${(Number(v || 0) * 100).toFixed(d)}%`;
const fmtNum = (v, d = 2) => Number(v || 0).toFixed(d);

const STATIC_NAMES = {
  VOO: "标普500指数基金",
  "03195.HK": "港股通标普500ETF",
  "513500.SH": "标普500ETF博时",
  "512890.SH": "红利低波基金",
  "510300.SH": "沪深300基金",
  "160706": "嘉实沪深300ETF联接(LOF)A",
  "518880.SH": "黄金基金",
  "Au99.99": "上海金交所 Au99.99",
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
  "03195.HK": "港股通标普",
  "513500.SH": "A股标普",
  "512890.SH": "红利",
  "510300.SH": "沪深300",
  "160706": "嘉实300",
  "518880.SH": "黄金",
  "Au99.99": "AU99.99",
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
const CURRENCY_NAMES = { CNY: "人民币", USD: "美元", HKD: "港币" };
const CHART_COLORS = {
  accent: "#087a55",
  blue: "#3478d4",
  danger: "#d3423f",
  violet: "#7257b5",
  amber: "#b97918",
  muted: "#718087",
  line: "#dce4e8",
  text: "#3c4d54",
};
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
  "eastmoney:hk_kline": "东方财富港股行情",
  "stooq:hk": "Stooq 港股行情",
  "public:dividend_unavailable_empty": "公开分红源不可用，按无分红覆盖",
  "yahoo:3195.HK": "雅虎港股行情",
  "yahoo:HKDCNY=X": "雅虎港币汇率",
};

const SP500_ROUTE_DETAILS = {
  us_sp500: [
    ["市场", "美股"],
    ["币种", "USD/CNY"],
    ["费用", "IBKR/分红税"],
  ],
  hk_sp500_connect: [
    ["市场", "港股通"],
    ["币种", "HKD/CNY"],
    ["费用", "佣金/交易费/结算费/组合费"],
  ],
  cn_sp500_etf: [
    ["市场", "A股场内"],
    ["币种", "CNY"],
    ["费用", "场内ETF佣金/管理托管"],
  ],
};

function isSp500Asset(asset) {
  return asset.exclusive_group === SP500_GROUP || ["us_sp500", "hk_sp500_connect"].includes(asset.key);
}

function sp500Assets() {
  return (config?.assets || []).filter(isSp500Asset);
}

function selectedSp500Asset() {
  const assets = sp500Assets();
  return assets.find((asset) => asset.enabled) || assets[0];
}

function selectedSp500Weight() {
  const selected = selectedSp500Asset();
  return Number(selected?.target_weight || 0);
}

function sp500RouteDetails(key) {
  return SP500_ROUTE_DETAILS[key] || [];
}

function assetBySymbol(symbol) {
  return config?.assets?.find((asset) => asset.symbol === symbol);
}

function fallbackBySymbol(symbol) {
  return config?.assets?.find((asset) => asset.price_fallback?.symbol === symbol)?.price_fallback;
}

function assetName(symbol) {
  const configured = assetBySymbol(symbol);
  const fallback = fallbackBySymbol(symbol);
  const repo = config?.repo_options?.find((option) => option.symbol === symbol);
  if (repo) return repo.name;
  return STATIC_NAMES[symbol] || fallback?.name || configured?.name || symbol;
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
  const { attempts, retry = false, retryDelayMs = 500, ...fetchOptions } = options;
  const method = (fetchOptions.method || "GET").toUpperCase();
  const retryable = method === "GET" || retry;
  const maxAttempts = attempts || (retryable ? (method === "GET" ? 5 : 3) : 1);
  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const response = await fetch(`${APP_BASE_PATH}${path}`, {
        ...fetchOptions,
        headers: { "Content-Type": "application/json", ...(fetchOptions.headers || {}) },
        cache: method === "GET" ? "no-store" : fetchOptions.cache,
      });
      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch {
          throw new Error(`接口返回内容不是 JSON：${response.status} ${response.statusText}`);
        }
      }
      if (!response.ok) {
        const message = data.error || response.statusText || `HTTP ${response.status}`;
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      lastError = error;
      const retryStatus = [408, 429, 502, 503, 504].includes(error.status);
      if (!retryable || attempt >= maxAttempts || (!retryStatus && error.status)) break;
      await sleep(Math.min(retryDelayMs * attempt * attempt, 5000));
    }
  }
  if (isNetworkError(lastError)) {
    throw new Error(`网络请求失败，已自动重试 ${maxAttempts - 1} 次仍未成功，请稍后再试`);
  }
  throw lastError;
}

function isNetworkError(error) {
  if (!error || error.status) return false;
  return error.name === "TypeError" || /Failed to fetch|NetworkError|Load failed/i.test(error.message || "");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function createClientRequestId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function setMessage(text, isError = false) {
  const message = $("message");
  if (message) {
    message.textContent = text || "";
    message.className = isError ? "message error" : "message";
  }
  const resultStatus = $("resultStatusText");
  if (resultStatus) {
    resultStatus.textContent = text || "尚未运行回测";
    resultStatus.classList.toggle("error", isError);
  }
}

function feeInputNumber(id, fallback) {
  return Number($(id)?.value ?? fallback ?? 0);
}

function ibkrPlanLabel(plan) {
  return {
    pro_fixed: "固定费率",
    pro_tiered: "阶梯费率",
    lite: "免佣类型",
  }[plan] || plan;
}

function renderFeeSummary() {
  const host = $("feeSummary");
  if (!host || !config) return;
  const voo = assetBySymbol("VOO") || {};
  const hk = assetBySymbol("03195.HK") || {};
  const hkFee = config.fees.hk_connect_etf;
  const ibkr = config.fees.ibkr_us_etf;
  const cnCommission = feeInputNumber("cnCommission", config.fees.cn_etf.commission_rate);
  const hkCommission = feeInputNumber("hkCommission", hkFee.broker_commission_rate);
  const hkFxBps = feeInputNumber("hkFxBps", hkFee.fx_spread_bps);
  const hkPortfolio = feeInputNumber("hkPortfolioFee", hkFee.portfolio_fee_annual_rate);
  const usDividendTax = feeInputNumber("usDividendTax", config.fees.tax.us_dividend_withholding_rate);
  const officialHkPerSide = hkFee.trading_fee_rate + hkFee.transaction_levy_rate + hkFee.afrc_transaction_levy_rate;
  const fundGap = Number(hk.expense_ratio || 0) - Number(voo.expense_ratio || 0);
  const rows = [
    ["基金内扣", "03195", fmtRate(hk.expense_ratio, 2), "已在净值/价格中体现"],
    ["基金内扣", "VOO", fmtRate(voo.expense_ratio, 2), "已在净值/价格中体现"],
    ["基金内扣差", "03195-VOO", fmtRate(fundGap, 2), "03195 长期拖累更高"],
    ["03195 交易", "官方规费", `${fmtRate(officialHkPerSide, 4)}/边`, "交易费+交易征费+会财局征费"],
    ["03195 交易", "股份交收费", `${fmtRate(hkFee.stock_settlement_fee_rate, 4)}/边`, "当前按保守现行口径"],
    ["03195 交易", "ETF印花税", fmtRate(hkFee.stamp_duty_rate, 2), "暂按 0"],
    ["03195 持仓", "港股通组合费", `${fmtRate(hkPortfolio, 3)}/年`, "按日折算"],
    ["03195 假设", "券商佣金", `${fmtRate(hkCommission, 3)}/边`, "用户可调"],
    ["03195 假设", "汇兑点差", `${hkFxBps.toFixed(0)} bp`, "用户可调"],
    ["VOO 交易", "IBKR佣金", ibkrPlanLabel($("ibkrPlan")?.value || ibkr.plan), "用户可调"],
    ["VOO 卖出", "SEC费", `${fmtRate(ibkr.sec_transaction_fee_rate, 4)}`, "仅卖出"],
    ["VOO 卖出", "FINRA TAF", `$${fmtNum(ibkr.finra_taf_per_share_usd, 6)}/股`, `上限 $${fmtNum(ibkr.finra_taf_cap_usd, 2)}`],
    ["VOO 税务", "分红预扣税", fmtRate(usDividendTax, 0), "最差预期可用 30%"],
    ["境内 ETF", "佣金", `${fmtRate(cnCommission, 3)}/边`, "用户可调"],
  ];
  host.innerHTML = `
    <div class="fee-cards">
      ${rows.map(([group, item, value, note]) => `
        <div class="fee-card">
          <span>${group}</span>
          <strong>${item}</strong>
          <b>${value}</b>
          <small>${note}</small>
        </div>
      `).join("")}
    </div>
    <div class="fee-note">ETF基金内扣费用已反映在历史价格或净值中，回测不额外重复扣除。</div>
  `;
}

function readConfig() {
  const next = structuredClone(config);
  const sp500SelectedKey = $("sp500Type")?.value;
  const sp500Enabled = $("enabled_sp500_group")?.checked ?? false;
  const sp500Weight = Number($("weight_sp500_group")?.value ?? 0);
  next.initial_capital_cny = Number($("initialCapital").value);
  next.start_date = $("startDate").value;
  next.end_date = $("endDate").value;
  next.rebalance_frequency = $("rebalanceFrequency").value;
  next.rebalance_band = Number($("rebalanceBand").value);
  next.monthly_spend_cny = Number($("monthlySpend").value);
  next.repo_target_mode = $("repoTargetMode").value;
  next.repo_fixed_target_cny = Number($("repoFixedTarget").value);
  next.repo_fixed_target_ratio = Number($("repoFixedRatio").value);
  next.repo_symbol = $("repoSymbol").value;
  next.assets = next.assets.map((asset) => {
    if (isSp500Asset(asset)) {
      const selected = asset.key === sp500SelectedKey;
      return {
        ...asset,
        enabled: sp500Enabled && selected,
        target_weight: selected ? sp500Weight : 0,
      };
    }
    return {
      ...asset,
      enabled: $(`enabled_${asset.key}`).checked,
      target_weight: Number($(`weight_${asset.key}`).value),
    };
  });
  next.fees.cn_etf.commission_rate = Number($("cnCommission").value);
  next.fees.ibkr_us_etf.plan = $("ibkrPlan").value;
  next.fees.fx.bank_out_spread_bps = Number($("fxOutBps").value);
  next.fees.fx.bank_in_spread_bps = Number($("fxInBps").value);
  next.fees.hk_connect_etf.broker_commission_rate = Number($("hkCommission").value);
  next.fees.hk_connect_etf.fx_spread_bps = Number($("hkFxBps").value);
  next.fees.hk_connect_etf.portfolio_fee_annual_rate = Number($("hkPortfolioFee").value);
  next.fees.tax.us_dividend_withholding_rate = Number($("usDividendTax").value);
  return next;
}

function repoModeLabel(mode) {
  return mode === "fixed_bucket" ? "固定消费池" : "按剩余权重";
}

function currentRepoMode() {
  return $("repoTargetMode")?.value || config.repo_target_mode || "residual_weight";
}

function currentAssetControls() {
  const controls = [];
  if ($(`enabled_${SP500_CONTROL_KEY}`)) {
    controls.push({
      key: SP500_CONTROL_KEY,
      enabled: $(`enabled_${SP500_CONTROL_KEY}`).checked,
      weight: Number($(`weight_${SP500_CONTROL_KEY}`).value || 0),
    });
  }
  for (const asset of config.assets.filter((item) => !isSp500Asset(item))) {
    controls.push({
      key: asset.key,
      enabled: $(`enabled_${asset.key}`)?.checked ?? Boolean(asset.enabled),
      weight: Number($(`weight_${asset.key}`)?.value ?? asset.target_weight ?? 0),
    });
  }
  return controls;
}

function setAssetWeightDisplay(key, weight, mode, enabled, effectiveWeight) {
  const label = $(`weight_label_${key}`);
  const effective = $(`effective_${key}`);
  if (!effective) return;
  if (mode === "fixed_bucket") {
    if (label) label.textContent = enabled ? fmtPct(effectiveWeight) : "0.00%";
    effective.hidden = false;
    effective.textContent = enabled ? `输入 ${fmtPct(weight)}` : "未启用";
  } else {
    if (label) label.textContent = fmtPct(weight);
    effective.hidden = true;
    effective.textContent = "";
  }
}

function currentRepoPlan(mode, enabledWeight) {
  if (mode === "fixed_bucket") {
    const initialCapital = Math.max(Number($("initialCapital")?.value || config.initial_capital_cny || 0), 0);
    const fixedAmount = Math.max(Number($("repoFixedTarget")?.value || 0), 0);
    const fixedRatio = Math.min(Math.max(Number($("repoFixedRatio")?.value || 0), 0), 1);
    const repoTargetValue = initialCapital > 0 ? Math.min(fixedAmount + initialCapital * fixedRatio, initialCapital) : 0;
    const repoWeight = initialCapital > 0 ? repoTargetValue / initialCapital : 1;
    return {
      mode,
      enabledWeight,
      repoTargetValue,
      repoWeight,
      remainingWeight: Math.max(1 - repoWeight, 0),
      warning: repoWeight >= 1 || enabledWeight <= 0,
    };
  }
  const repoWeight = Math.max(1 - enabledWeight, 0);
  return {
    mode,
    enabledWeight,
    repoTargetValue: null,
    repoWeight,
    remainingWeight: Math.max(1 - repoWeight, 0),
    warning: enabledWeight > 1,
  };
}

function renderControlSummary(plan) {
  const host = $("controlSummary");
  if (!host) return;
  const start = $("startDate")?.value || config.start_date;
  const end = $("endDate")?.value || config.end_date;
  const initialCapital = Number($("initialCapital")?.value || config.initial_capital_cny || 0);
  const monthlySpend = Number($("monthlySpend")?.value || config.monthly_spend_cny || 0);
  const frequency = $("rebalanceFrequency")?.value === "semiannual" ? "每半年" : "每年";
  const repoValue = plan.mode === "fixed_bucket"
    ? `￥${fmtMoney(plan.repoTargetValue)} / ${fmtPct(plan.repoWeight)}`
    : fmtPct(plan.repoWeight);
  const items = [
    ["区间", `${start} 至 ${end}`],
    ["初始资金", `￥${fmtMoney(initialCapital)}`],
    ["再平衡", frequency],
    ["模式", repoModeLabel(plan.mode)],
    ["国债", repoValue],
    ["月消费", `￥${fmtMoney(monthlySpend)}`],
  ];
  host.innerHTML = items.map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("");
}

function renderAllocationSummary(plan) {
  const host = $("allocationSummary");
  if (!host) return;
  const actualAssetWeight = plan.mode === "fixed_bucket" && plan.enabledWeight > 0 ? plan.remainingWeight : plan.enabledWeight;
  const rows = [
    ["输入合计", fmtPct(plan.enabledWeight)],
    ["实际资产", fmtPct(actualAssetWeight)],
    ["国债目标", plan.mode === "fixed_bucket" ? `￥${fmtMoney(plan.repoTargetValue)} / ${fmtPct(plan.repoWeight)}` : fmtPct(plan.repoWeight)],
  ];
  host.innerHTML = rows.map(([label, value]) => `
    <div class="allocation-item ${plan.warning ? "warning" : ""}">
      <span>${label}</span>
      <strong>${value}</strong>
    </div>
  `).join("");
}

function syncRepoModeTabs() {
  const mode = currentRepoMode();
  document.querySelectorAll("[data-repo-mode]").forEach((button) => {
    const active = button.dataset.repoMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function selectRepoMode(mode) {
  $("repoTargetMode").value = mode;
  updateRepoWeight();
}

function parseInputDate(value) {
  const parts = String(value || "").split("-").map(Number);
  if (parts.length !== 3 || parts.some((item) => !Number.isFinite(item))) return null;
  return new Date(parts[0], parts[1] - 1, parts[2]);
}

function formatInputDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function applyDatePreset(value) {
  const endValue = $("endDate").value || config.end_date;
  const end = parseInputDate(endValue) || new Date();
  if (value === "all") {
    $("startDate").value = config.start_date;
    $("endDate").value = config.end_date;
  } else {
    const start = new Date(end.getTime());
    start.setFullYear(start.getFullYear() - Number(value));
    $("startDate").value = formatInputDate(start);
    $("endDate").value = formatInputDate(end);
  }
  document.querySelectorAll("[data-date-preset]").forEach((button) => {
    button.classList.toggle("active", button.dataset.datePreset === value);
  });
  updateRepoWeight();
}

function updateRepoWeight() {
  if ($("enabled_sp500_group")) {
    updateSp500Route();
  }
  const mode = currentRepoMode();
  const controls = currentAssetControls();
  const enabledWeight = controls.reduce((sum, item) => sum + (item.enabled ? item.weight : 0), 0);
  if ($("assetWeightTitle")) $("assetWeightTitle").textContent = mode === "fixed_bucket" ? "资产分配比例" : "资产权重";
  const fixedControls = $("repoFixedControls");
  if (fixedControls) fixedControls.hidden = mode !== "fixed_bucket";
  if ($("repoFixedRatioValue")) $("repoFixedRatioValue").textContent = fmtPct(Number($("repoFixedRatio")?.value || 0));
  const plan = currentRepoPlan(mode, enabledWeight);
  syncRepoModeTabs();
  renderControlSummary(plan);
  renderAllocationSummary(plan);
  if (mode === "fixed_bucket") {
    controls.forEach((item) => {
      const effectiveWeight = item.enabled && enabledWeight > 0 ? (item.weight / enabledWeight) * plan.remainingWeight : 0;
      setAssetWeightDisplay(item.key, item.weight, mode, item.enabled, effectiveWeight);
    });
    $("repoWeight").textContent = `目标 ${fmtPct(plan.repoWeight)}（￥${fmtMoney(plan.repoTargetValue)}），剩余 ${fmtPct(plan.remainingWeight)} 按比例分配`;
    $("repoWeight").style.color = plan.warning ? "#b42318" : "";
    return;
  }
  controls.forEach((item) => setAssetWeightDisplay(item.key, item.weight, mode, item.enabled, item.enabled ? item.weight : 0));
  $("repoWeight").textContent = fmtPct(plan.repoWeight);
  $("repoWeight").style.color = plan.warning ? "#b42318" : "";
}

function updateSp500Route() {
  const route = $("sp500Route");
  const selectedKey = $("sp500Type")?.value;
  if (!route || !selectedKey) return;
  route.innerHTML = sp500RouteDetails(selectedKey)
    .map(([label, value]) => `<span><em>${label}</em>${value}</span>`)
    .join("");
}

function renderControls() {
  $("initialCapital").value = config.initial_capital_cny;
  $("startDate").value = config.start_date;
  $("endDate").value = config.end_date;
  $("rebalanceFrequency").value = config.rebalance_frequency;
  $("rebalanceBand").value = config.rebalance_band;
  $("bandValue").textContent = fmtPct(config.rebalance_band);
  $("monthlySpend").value = config.monthly_spend_cny;
  $("repoTargetMode").value = config.repo_target_mode || "residual_weight";
  $("repoFixedTarget").value = config.repo_fixed_target_cny ?? 360000;
  $("repoFixedRatio").value = config.repo_fixed_target_ratio ?? 0;
  $("repoFixedRatioValue").textContent = fmtPct(config.repo_fixed_target_ratio ?? 0);
  $("repoSymbol").innerHTML = (config.repo_options || []).map((option) => `<option value="${option.symbol}">${option.name}</option>`).join("");
  $("repoSymbol").value = config.repo_symbol;
  $("cnCommission").value = config.fees.cn_etf.commission_rate;
  $("ibkrPlan").value = config.fees.ibkr_us_etf.plan;
  $("fxOutBps").value = config.fees.fx.bank_out_spread_bps;
  $("fxInBps").value = config.fees.fx.bank_in_spread_bps;
  $("hkCommission").value = config.fees.hk_connect_etf.broker_commission_rate;
  $("hkFxBps").value = config.fees.hk_connect_etf.fx_spread_bps;
  $("hkPortfolioFee").value = config.fees.hk_connect_etf.portfolio_fee_annual_rate;
  $("usDividendTax").value = config.fees.tax.us_dividend_withholding_rate;
  ["cnCommission", "fxOutBps", "fxInBps", "hkCommission", "hkFxBps", "hkPortfolioFee", "usDividendTax"].forEach((id) => {
    $(id).addEventListener("input", renderFeeSummary);
  });
  $("ibkrPlan").addEventListener("change", renderFeeSummary);
  ["initialCapital", "startDate", "endDate", "monthlySpend", "rebalanceFrequency", "repoTargetMode", "repoFixedTarget", "repoFixedRatio"].forEach((id) => {
    $(id).addEventListener(id === "rebalanceFrequency" || id === "repoTargetMode" ? "change" : "input", updateRepoWeight);
  });
  document.querySelectorAll("[data-repo-mode]").forEach((button) => {
    button.addEventListener("click", () => selectRepoMode(button.dataset.repoMode));
  });
  document.querySelectorAll("[data-date-preset]").forEach((button) => {
    button.addEventListener("click", () => applyDatePreset(button.dataset.datePreset));
  });
  ["startDate", "endDate"].forEach((id) => {
    $(id).addEventListener("input", () => {
      document.querySelectorAll("[data-date-preset]").forEach((button) => button.classList.remove("active"));
    });
  });

  const host = $("assetControls");
  host.innerHTML = "";
  renderSp500Control(host);
  for (const asset of config.assets.filter((item) => !isSp500Asset(item))) {
    const row = document.createElement("div");
    row.className = "asset-control";
    row.innerHTML = `
      <input id="enabled_${asset.key}" type="checkbox" aria-label="启用${assetName(asset.symbol)}" ${asset.enabled ? "checked" : ""} />
      <input id="weight_${asset.key}" type="range" min="0" max="0.8" step="0.01" value="${asset.target_weight}" aria-label="${assetName(asset.symbol)}目标权重" />
      <strong id="weight_label_${asset.key}">${fmtPct(asset.target_weight)}</strong>
      <div class="asset-name">
        <span class="asset-title">${assetName(asset.symbol)}</span>
        <span id="effective_${asset.key}" class="asset-effective" hidden></span>
      </div>
    `;
    host.appendChild(row);
    row.querySelector(`#enabled_${asset.key}`).addEventListener("change", updateRepoWeight);
    row.querySelector(`#weight_${asset.key}`).addEventListener("input", (event) => {
      updateRepoWeight();
    });
  }
  $("rebalanceBand").addEventListener("input", () => {
    $("bandValue").textContent = fmtPct($("rebalanceBand").value);
    updateRepoWeight();
  });
  updateRepoWeight();
  renderFeeSummary();
}

function renderSp500Control(host) {
  const assets = sp500Assets();
  if (!assets.length) return;
  const selected = selectedSp500Asset();
  const enabled = Boolean(selected?.enabled);
  const weight = selectedSp500Weight();
  const row = document.createElement("div");
  row.className = "asset-control asset-control-group";
  row.innerHTML = `
    <input id="enabled_${SP500_CONTROL_KEY}" type="checkbox" aria-label="启用标普500" ${enabled ? "checked" : ""} />
    <input id="weight_${SP500_CONTROL_KEY}" type="range" min="0" max="0.8" step="0.01" value="${weight}" aria-label="标普500目标权重" />
    <strong id="weight_label_${SP500_CONTROL_KEY}">${fmtPct(weight)}</strong>
    <div class="asset-name">
      <span class="asset-title">标普500</span>
      <span id="effective_${SP500_CONTROL_KEY}" class="asset-effective" hidden></span>
    </div>
    <label class="asset-type">
      类型
      <select id="sp500Type">
        ${assets.map((asset) => `<option value="${asset.key}" ${asset.key === selected?.key ? "selected" : ""}>${asset.choice_label || assetName(asset.symbol)}</option>`).join("")}
      </select>
    </label>
    <div id="sp500Route" class="asset-route"></div>
  `;
  host.appendChild(row);
  row.querySelector(`#enabled_${SP500_CONTROL_KEY}`).addEventListener("change", updateRepoWeight);
  row.querySelector(`#weight_${SP500_CONTROL_KEY}`).addEventListener("input", (event) => {
    updateRepoWeight();
  });
  row.querySelector("#sp500Type").addEventListener("change", updateRepoWeight);
  updateSp500Route();
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
  updateDataStatus(rows);
}

function updateDataStatus(rows) {
  const statusText = $("dataStatusText");
  const mobileStatus = $("mobileDataStatus");
  const statusDot = $("dataStatusDot");
  const validDates = rows.map((row) => row.end_date).filter(Boolean).sort();
  const latestDate = validDates.at(-1);
  const text = rows.length
    ? `${rows.length} 项数据已就绪${latestDate ? ` · 最新 ${latestDate}` : ""}`
    : "暂无可用数据";
  if (statusText) statusText.textContent = text;
  if (mobileStatus) mobileStatus.textContent = rows.length ? `数据最新 ${latestDate || "已就绪"}` : "暂无可用数据";
  if (statusDot) {
    statusDot.classList.remove("is-loading", "is-error");
    statusDot.classList.toggle("is-error", !rows.length);
  }
}

function summarizeDataQuality(rows) {
  const total = rows.length;
  const fixture = rows.filter((row) => String(row.sources || "").includes("fixture:")).length;
  return { total, fixture, real: total - fixture };
}

function metricMarkup(item) {
  return `<div class="metric ${item.tone ? `is-${item.tone}` : ""}"><span>${item.label}</span><strong>${item.value}</strong></div>`;
}

function renderSummaryGroups(primary, secondary) {
  $("summaryGrid").innerHTML = `
    <div class="metric-group metric-group-primary">${primary.map(metricMarkup).join("")}</div>
    <div class="metric-group metric-group-secondary">${secondary.map(metricMarkup).join("")}</div>
  `;
}

function renderInitialSummary() {
  renderSummaryGroups(
    ["期末总资产", "累计收益", "年化收益", "最大回撤"].map((label) => ({ label, value: "--" })),
    ["总手续费", "总消费", "浮盈浮亏", "对比期末资产", "再平衡次数", "交易次数", "分红预扣税"]
      .map((label) => ({ label, value: "--" })),
  );
}

function renderSummary(summary) {
  const positiveTone = (value) => Number(value || 0) >= 0 ? "positive" : "negative";
  const primary = [
    { label: "期末总资产", value: `￥${fmtMoney(summary.final_asset_cny)}` },
    { label: "累计收益", value: fmtPct(summary.total_return), tone: positiveTone(summary.total_return) },
    { label: "年化收益", value: fmtPct(summary.annualized_return), tone: positiveTone(summary.annualized_return) },
    { label: "最大回撤", value: fmtPct(summary.max_drawdown), tone: "negative" },
  ];
  const secondary = [
    { label: "总手续费", value: `￥${fmtMoney(summary.total_fees_cny)}` },
    { label: "总消费", value: `￥${fmtMoney(summary.total_spend_cny)}` },
    { label: "浮盈浮亏", value: `￥${fmtMoney(summary.final_unrealized_pnl_cny)}`, tone: positiveTone(summary.final_unrealized_pnl_cny) },
    { label: "对比期末资产", value: `￥${fmtMoney(summary.comparison_final_asset_cny)}` },
    { label: "再平衡次数", value: fmtNum(summary.rebalance_count, 0) },
    { label: "交易次数", value: fmtNum(summary.trade_count, 0) },
    { label: "分红预扣税", value: `￥${fmtMoney(summary.withheld_tax_cny)}` },
  ];
  renderSummaryGroups(primary, secondary);
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
  return rows.map((row) => ({
    ...row,
    payload: row.payload || {},
    daily_return: Number(row.daily_return || 0),
    cumulative_return: Number(row.cumulative_return || 0),
    drawdown: Number(row.drawdown || 0),
    benchmark_return: Number(row.benchmark_return || 0),
  }));
}

function deriveSummary(summary, series) {
  if (!series.length) return summary;
  const last = series.at(-1);
  return {
    ...summary,
    final_asset_cny: summary.final_asset_cny ?? last.total_asset_cny,
    total_return: summary.total_return ?? last.cumulative_return,
    max_drawdown: summary.max_drawdown ?? Math.min(...series.map((row) => row.drawdown ?? 0)),
    comparison_final_asset_cny: last.payload?.comparison?.total_asset_cny ?? summary.comparison_final_asset_cny,
  };
}

function ensureChart(id) {
  if (!charts[id]) charts[id] = echarts.init($(id));
  return charts[id];
}

function resizeCharts() {
  Object.values(charts).forEach((chart) => chart.resize());
}

let chartResizeTimer = null;

function queueChartResize() {
  window.clearTimeout(chartResizeTimer);
  chartResizeTimer = window.setTimeout(resizeCharts, 80);
}

function selectChart(chartId) {
  activeChartId = chartId;
  document.querySelectorAll("[data-chart-tab]").forEach((button) => {
    const active = button.dataset.chartTab === chartId;
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".chart-view").forEach((view) => {
    view.hidden = view.querySelector(".chart")?.id !== chartId;
  });
  window.requestAnimationFrame(() => charts[chartId]?.resize());
}

function selectRecordPanel(panelId) {
  document.querySelectorAll("[data-record-tab]").forEach((button) => {
    const active = button.dataset.recordTab === panelId;
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".record-panel").forEach((panel) => {
    panel.hidden = panel.id !== panelId;
  });
}

function lineZoomOption() {
  return {
    animationDuration: 450,
    grid: { left: 66, right: 26, top: 58, bottom: 62 },
    dataZoom: [
      { type: "inside", xAxisIndex: 0, filterMode: "none", zoomOnMouseWheel: true, moveOnMouseMove: true },
      {
        type: "slider",
        xAxisIndex: 0,
        filterMode: "none",
        height: 16,
        bottom: 15,
        borderColor: CHART_COLORS.line,
        backgroundColor: "#f7f9fa",
        fillerColor: "rgba(8, 122, 85, 0.10)",
        handleStyle: { color: "#ffffff", borderColor: CHART_COLORS.accent },
        moveHandleStyle: { color: CHART_COLORS.accent },
        textStyle: { color: CHART_COLORS.muted, fontSize: 10 },
      },
    ],
  };
}

function polishCharts() {
  Object.values(charts).forEach((chart) => chart.setOption({
    textStyle: { color: CHART_COLORS.text, fontFamily: '"Segoe UI", "Microsoft YaHei UI", sans-serif' },
    tooltip: {
      backgroundColor: "rgba(20, 35, 41, 0.94)",
      borderWidth: 0,
      padding: [9, 11],
      textStyle: { color: "#ffffff", fontSize: 11 },
    },
    xAxis: {
      axisLine: { lineStyle: { color: CHART_COLORS.line } },
      axisTick: { show: false },
      axisLabel: { color: CHART_COLORS.muted, fontSize: 10 },
    },
    yAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: CHART_COLORS.muted, fontSize: 10 },
      splitLine: { lineStyle: { color: "#e9eef0", type: "dashed" } },
    },
  }));
}

function renderCharts(series) {
  if (!series.length) return;
  $("analysisEmpty").hidden = true;
  if (!window.echarts) {
    renderFallbackCharts(series);
    return;
  }
  const dates = series.map((row) => row.trade_date);
  ensureChart("assetChart").setOption({
    ...lineZoomOption(),
    title: { text: "总资产", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", scale: true },
    series: [{ type: "line", name: "总资产", data: series.map((row) => row.total_asset_cny), smooth: true, symbol: "none", lineStyle: { color: CHART_COLORS.accent, width: 2.4 }, itemStyle: { color: CHART_COLORS.accent } }],
  });
  ensureChart("comparisonChart").setOption({
    ...lineZoomOption(),
    title: { text: "总资产对比", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => `￥${fmtMoney(v)}` },
    legend: { top: 4, right: 10 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", scale: true },
    series: [
      { type: "line", name: "当前策略", data: series.map((row) => row.total_asset_cny), smooth: true, symbol: "none", lineStyle: { color: CHART_COLORS.accent, width: 2.3 }, itemStyle: { color: CHART_COLORS.accent } },
      {
        type: "line",
        name: "沪深300基金加黄金基金加国债逆回购",
        data: series.map((row) => row.payload.comparison?.total_asset_cny ?? null),
        smooth: true,
        symbol: "none",
        lineStyle: { color: CHART_COLORS.blue, width: 1.8, type: "dashed" },
        itemStyle: { color: CHART_COLORS.blue },
      },
    ],
  });
  ensureChart("returnChart").setOption({
    ...lineZoomOption(),
    title: { text: "收益率对比沪深300", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    legend: { top: 4, right: 10 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: [
      { type: "line", name: "策略", data: series.map((row) => row.cumulative_return), smooth: true, symbol: "none", lineStyle: { color: CHART_COLORS.accent, width: 2.3 }, itemStyle: { color: CHART_COLORS.accent } },
      { type: "line", name: "沪深300", data: series.map((row) => row.benchmark_return), smooth: true, symbol: "none", lineStyle: { color: CHART_COLORS.blue, width: 1.8, type: "dashed" }, itemStyle: { color: CHART_COLORS.blue } },
    ],
  });
  ensureChart("dailyReturnChart").setOption({
    ...lineZoomOption(),
    title: { text: "单日收益", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(1)}%` } },
    series: [{ type: "line", name: "单日收益", data: series.map((row) => row.daily_return), smooth: false, symbol: "none", lineStyle: { color: CHART_COLORS.violet, width: 1.4 }, itemStyle: { color: CHART_COLORS.violet } }],
  });
  ensureChart("drawdownChart").setOption({
    ...lineZoomOption(),
    title: { text: "回撤", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: [{ type: "line", areaStyle: { color: "rgba(211, 66, 63, 0.12)" }, name: "回撤", data: series.map((row) => row.drawdown), symbol: "none", lineStyle: { color: CHART_COLORS.danger, width: 1.8 }, itemStyle: { color: CHART_COLORS.danger } }],
  });

  const symbols = Object.keys(series.at(-1)?.payload?.values || {});
  const weightColors = [CHART_COLORS.accent, CHART_COLORS.blue, CHART_COLORS.amber, CHART_COLORS.violet, "#5c8f99", "#9d6c52", "#7e8d50"];
  ensureChart("weightChart").setOption({
    ...lineZoomOption(),
    title: { text: "资产权重", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    legend: { top: 4, right: 10 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", max: 1, axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: symbols.map((symbol, index) => ({
      type: "line",
      stack: "weights",
      areaStyle: {},
      name: assetName(symbol),
      data: series.map((row) => row.payload.weights[symbol] || 0),
      symbol: "none",
      lineStyle: { width: 1.2, color: weightColors[index % weightColors.length] },
      itemStyle: { color: weightColors[index % weightColors.length] },
    })),
  });
  polishCharts();
  selectChart(activeChartId);
  queueChartResize();
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
  drawFallbackChart("dailyReturnChart", "单日收益", [{ name: "单日收益", color: "#7a3db8", points: makePointSeries(series.map((row) => row.daily_return)) }], true);
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
    table.innerHTML = "<tbody><tr><td class=\"table-empty\">暂无数据</td></tr></tbody>";
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

function rebalanceActionLabel(payload = {}) {
  if (payload.rebalance_action === "trade" || payload.rebalanced === true) return "已调仓";
  if (payload.rebalance_reason === "within_band") return "未偏离";
  return "已记录";
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
  const baseColumns = ["日期", "动作", "区间", "当回撤", "前资产", "后资产", "成交", "费"];
  const assetColumns = orderedSymbols.map((symbol) => SHORT_NAMES[symbol] || assetName(symbol));
  const displayRows = rows.map((row) => {
    const item = {
      日期: row.rebalance_date,
      动作: rebalanceActionLabel(row.payload),
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

async function waitForBacktestJob(jobId) {
  let pollCount = 0;
  let transientFailures = 0;
  while (true) {
    let job = null;
    try {
      job = await api(`/api/backtest/jobs/${jobId}`, { attempts: 5, retryDelayMs: 700 });
      transientFailures = 0;
    } catch (error) {
      transientFailures += 1;
      if (!isNetworkError(error) || transientFailures > 4) throw error;
      setMessage("网络短暂波动，正在继续等待回测结果...");
      await sleep(Math.min(1500 * transientFailures, 6000));
      continue;
    }
    if (job.status === "completed") return job.result;
    if (job.status === "failed") throw new Error(job.error || job.message || "回测失败");
    if (job.status === "cancelled") throw new Error(job.error || job.message || "回测任务已取消");
    setMessage(job.message || (job.status === "running" ? "正在运行回测..." : "回测任务排队中..."));
    pollCount += 1;
    await sleep(Math.min(1200 + pollCount * 200, 5000));
  }
}

async function loadBacktestResultSections(runId) {
  setMessage("回测完成，正在加载净值曲线...");
  const series = await api(`/api/backtest/${runId}/series`, { attempts: 6, retryDelayMs: 700 });
  setMessage("正在加载再平衡记录...");
  const rebalance = await api(`/api/backtest/${runId}/rebalance`, { attempts: 5, retryDelayMs: 700 });
  setMessage("正在加载交易流水...");
  const trades = await api(`/api/backtest/${runId}/trades`, { attempts: 5, retryDelayMs: 700 });
  return { series, rebalance, trades };
}

async function runBacktest() {
  const button = $("runBtn");
  const buttonLabel = button.querySelector("span");
  button.disabled = true;
  button.classList.add("is-running");
  if (buttonLabel) buttonLabel.textContent = "正在回测";
  if (window.matchMedia("(max-width: 900px)").matches) setParameterPanel(false);
  setMessage("正在提交回测任务...");
  try {
    const job = await api("/api/backtest/start", {
      method: "POST",
      body: JSON.stringify({ config: readConfig(), client_request_id: createClientRequestId() }),
      retry: true,
      attempts: 3,
      retryDelayMs: 700,
    });
    setMessage(job.message || "回测任务已进入队列");
    const result = await waitForBacktestJob(job.job_id);
    currentRunId = result.run_id;
    if (result.status) renderStatus(result.status);
    const { series, rebalance, trades } = await loadBacktestResultSections(currentRunId);
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
      setMessage(quality.real > 0 ? `数据已自动补足，回测完成：${quality.real} 项真实/公开源` : "数据已自动补足，回测完成");
    } else {
      setMessage("数据充足，回测完成");
    }
  } catch (error) {
    setMessage(humanizeError(error.message), true);
  } finally {
    button.disabled = false;
    button.classList.remove("is-running");
    if (buttonLabel) buttonLabel.textContent = "运行回测";
  }
}

function setParameterPanel(open) {
  document.body.classList.toggle("parameters-open", open);
  [$("parameterToggle"), $("mobileParameterToggle")].filter(Boolean).forEach((button) => {
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
  if (open) window.requestAnimationFrame(() => $("closeParameterPanel")?.focus());
}

function setupTabs(selector, dataKey, selectPanel) {
  const buttons = [...document.querySelectorAll(selector)];
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => selectPanel(button.dataset[dataKey]));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      let nextIndex = index;
      if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = buttons.length - 1;
      buttons[nextIndex].click();
      buttons[nextIndex].focus();
    });
  });
}

function setupUiInteractions() {
  [$("parameterToggle"), $("mobileParameterToggle")].filter(Boolean).forEach((button) => {
    button.addEventListener("click", () => setParameterPanel(true));
  });
  $("closeParameterPanel")?.addEventListener("click", () => setParameterPanel(false));
  $("parameterBackdrop")?.addEventListener("click", () => setParameterPanel(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("parameters-open")) setParameterPanel(false);
  });
  setupTabs("[data-chart-tab]", "chartTab", selectChart);
  setupTabs("[data-record-tab]", "recordTab", selectRecordPanel);
  selectChart(activeChartId);
  selectRecordPanel("statusPanel");
}

async function init() {
  setupUiInteractions();
  renderInitialSummary();
  renderTable("rebalanceTable", [], []);
  renderTable("tradesTable", [], []);
  config = await api("/api/default-config");
  renderControls();
  $("runBtn").addEventListener("click", runBacktest);
  window.addEventListener("resize", queueChartResize);
  setMessage("准备就绪，可以运行回测");
  try {
    await loadStatus();
  } catch (error) {
    const statusDot = $("dataStatusDot");
    statusDot?.classList.remove("is-loading");
    statusDot?.classList.add("is-error");
    if ($("dataStatusText")) $("dataStatusText").textContent = "数据状态检查失败";
    if ($("mobileDataStatus")) $("mobileDataStatus").textContent = "数据状态异常";
    setMessage(humanizeError(error.message), true);
  }
}

init().catch((error) => setMessage(error.message, true));
