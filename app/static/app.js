let config = null;
let defaultConfigSnapshot = null;
let currentRunId = null;
let runHistory = [];
let leaderboardHistory = [];
let comparisonRunId = null;
let activeArchiveView = "recent";
let archiveFilter = "";
let recentArchiveLoaded = false;
let leaderboardArchiveLoaded = false;
let leaderboardArchiveLoading = false;
let leaderboardPeriodSelection = "";
let leaderboardPeriodMetadata = null;
let leaderboardAvailableYears = [];
let leaderboardRequestVersion = 0;
let archiveRefreshTimer = null;
let activeAnalysisWatch = 0;
const archiveSortModes = { recent: "newest", leaderboard: "score" };
const tableSortState = {};
const charts = {};
let activeChartId = "assetChart";
const pendingChartOptions = {};
let dailyPnlData = null;
let dailyPnlRunId = null;
let dailyPnlLoadingRunId = null;
let dailyPnlRequestVersion = 0;
let dailyPnlScale = "amount";
const APP_BASE_PATH = (() => {
  const pathname = window.location.pathname.replace(/\/+$/, "") || "/";
  const knownAppPaths = ["/backtest/permanent-investment", "/backtest/cross-market", "/portfolio"];
  return knownAppPaths.find((path) => pathname === path || pathname.startsWith(`${path}/`)) || "";
})();
const SP500_GROUP = "sp500";
const SP500_CONTROL_KEY = "sp500_group";
const BROAD_ETF_GROUP = "cn_broad_etf";
const BROAD_ETF_CONTROL_KEY = "cn_broad_etf_group";
const MAX_RUN_HISTORY = 20;
const MAX_LEADERBOARD_RUNS = 100;
const API_REQUEST_TIMEOUT_MS = 15000;
const API_HEALTH_TIMEOUT_MS = 5000;
let apiRecoveryPromise = null;
let controlsEventController = null;
let chartLibraryPromise = null;

function loadChartLibrary() {
  if (window.echarts) return Promise.resolve(window.echarts);
  if (chartLibraryPromise) return chartLibraryPromise;
  chartLibraryPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `${APP_BASE_PATH}/static/echarts.min.js?v=5.6.0`;
    script.async = true;
    script.onload = () => resolve(window.echarts);
    script.onerror = () => reject(new Error("图表组件加载失败"));
    document.head.appendChild(script);
  });
  return chartLibraryPromise;
}

const $ = (id) => document.getElementById(id);
const fmtMoney = (v) => Number(v || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
const fmtPct = (v) => `${(Number(v || 0) * 100).toFixed(2)}%`;
const fmtRate = (v, d = 4) => `${(Number(v || 0) * 100).toFixed(d)}%`;
const fmtNum = (v, d = 2) => Number(v || 0).toFixed(d);
const fmtRatio = (v) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(2);

function annualReturnDrawdownRatio(summary = {}) {
  const stored = summary.annual_return_drawdown_ratio;
  if (stored != null && Number.isFinite(Number(stored))) return Number(stored);
  const drawdown = Math.abs(Number(summary.max_drawdown || 0));
  return drawdown > 1e-12 ? Number(summary.annualized_return || 0) / drawdown : null;
}

function annualReturnTone(value) {
  return Number(value || 0) >= 0 ? "good" : "bad";
}

function drawdownTone(value) {
  const risk = Math.abs(Number(value || 0));
  if (risk <= 0.10) return "good";
  if (risk <= 0.20) return "warning";
  return "bad";
}

function ratioTone(value) {
  if (value == null || !Number.isFinite(Number(value))) return "muted";
  if (Number(value) >= 1) return "good";
  if (Number(value) >= 0) return "warning";
  return "bad";
}

function metricCell(raw, format, tone) {
  return { kind: "metric", raw: raw == null ? null : Number(raw), format, tone };
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function isMobileLayout() {
  return window.matchMedia("(max-width: 900px)").matches;
}

const STATIC_NAMES = {
  VOO: "标普500指数基金",
  "03195.HK": "港股通标普500ETF",
  "513500.SH": "标普500ETF博时",
  "512890.SH": "红利低波基金",
  "510300.SH": "沪深300基金",
  "159631.SZ": "招商中证A100ETF",
  "510500.SH": "南方中证500ETF",
  "512100.SH": "南方中证1000ETF",
  "160706": "嘉实沪深300ETF联接(LOF)A",
  "518880.SH": "黄金基金",
  "518850.SH": "华夏黄金ETF（518850）",
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
  CBA03101: "中债-5年期国债指数",
  CBA06501: "中债-7-10年期国债指数",
  CBA21801: "30年国债ETF（上市前指数代理）",
  "511090.SH": "鹏扬中债-30年期国债ETF（511090）",
  "CN30Y.YIELD-TR": "30年国债收益率曲线代理",
  "511990.SH": "华宝添益货币ETF（511990）",
  "USD/CNY": "美元兑人民币汇率",
  REPO: "国债逆回购",
};

const SHORT_NAMES = {
  VOO: "标普500",
  "03195.HK": "港股通标普",
  "513500.SH": "A股标普",
  "512890.SH": "红利",
  "510300.SH": "沪深300",
  "159631.SZ": "中证A100",
  "510500.SH": "中证500",
  "512100.SH": "中证1000",
  "160706": "嘉实300",
  "518880.SH": "黄金",
  "518850.SH": "黄金低费率",
  "Au99.99": "AU99.99",
  CBA03101: "5年国债",
  CBA06501: "7-10年国债",
  CBA21801: "30年国债",
  "511090.SH": "30年国债ETF",
  "CN30Y.YIELD-TR": "30年国债代理",
  "511990.SH": "货币基金",
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
const REASON_NAMES = { rebalance: "再平衡", asset_replacement: "指数代理切换ETF", liquidity_shortfall: "补足现金", dip_buy: "逢低补仓", dip_buy_funding: "补仓资金", dip_buy_recovery: "补仓回本卖出" };
const CURRENCY_NAMES = { CNY: "人民币", USD: "美元", HKD: "港币" };
const REBALANCE_FREQUENCY_NAMES = {
  daily: "每日",
  weekly: "每周",
  monthly: "每月",
  quarterly: "每季度",
  semiannual: "每半年",
  yearly: "每年",
};
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
  "csindex:index_perf": "中证指数官方全收益行情",
  "sohu:index_kline": "搜狐指数历史行情",
  "eastmoney:index_kline": "东方财富指数行情",
  "tushare:fund_div": "专业基金分红",
  "tushare:fund_adj": "专业复权因子",
  "chinabond:index_total_return": "中债财富指数",
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

const BROAD_ETF_ROUTE_DETAILS = {
  cn_hs300_etf: [
    ["市场", "A股场内"],
    ["指数", "沪深300"],
    ["费用", "场内ETF佣金/管理托管"],
  ],
  cn_a100_etf: [
    ["市场", "A股场内"],
    ["指数", "中证A100"],
    ["费用", "场内ETF佣金/管理托管"],
  ],
  cn_csi500_etf: [
    ["市场", "A股场内"],
    ["指数", "中证500"],
    ["费用", "场内ETF佣金/管理托管"],
  ],
  cn_csi1000_etf: [
    ["市场", "A股场内"],
    ["指数", "中证1000"],
    ["费用", "场内ETF佣金/管理托管"],
  ],
};

function isSp500Asset(asset) {
  return asset.exclusive_group === SP500_GROUP || ["us_sp500", "hk_sp500_connect"].includes(asset.key);
}

function isBroadEtfAsset(asset) {
  return asset.exclusive_group === BROAD_ETF_GROUP;
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

function broadEtfAssets() {
  return (config?.assets || []).filter(isBroadEtfAsset);
}

function selectedBroadEtfAsset() {
  const assets = broadEtfAssets();
  return assets.find((asset) => asset.enabled) || assets[0];
}

function selectedBroadEtfWeight() {
  const selected = selectedBroadEtfAsset();
  return Number(selected?.target_weight || 0);
}

function broadEtfRouteDetails(key) {
  return BROAD_ETF_ROUTE_DETAILS[key] || [];
}

function assetBySymbol(symbol) {
  for (const asset of config?.assets || []) {
    if (asset.symbol === symbol) return asset;
    const replacement = asset.replacement_assets?.find((item) => item.symbol === symbol);
    if (replacement) return replacement;
  }
  return undefined;
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

function tradeAssetName(symbol) {
  const code = symbol === "REPO" ? config?.repo_symbol : symbol;
  const name = assetName(code || symbol);
  if (!code || !name) return name || "-";
  const normalizedName = String(name).toUpperCase();
  const normalizedCode = String(code).toUpperCase();
  const baseCode = normalizedCode.split(".")[0];
  if (normalizedName.includes(normalizedCode) || (baseCode.length >= 3 && normalizedName.includes(baseCode))) {
    return name;
  }
  return `${name}（${code}）`;
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

class ApiNetworkError extends Error {
  constructor(message, cause, attempts) {
    super(message);
    this.name = "ApiNetworkError";
    this.network = true;
    this.cause = cause;
    this.attempts = attempts;
  }
}

function requestPath(path, attempt) {
  if (attempt <= 1) return `${APP_BASE_PATH}${path}`;
  const separator = path.includes("?") ? "&" : "?";
  return `${APP_BASE_PATH}${path}${separator}_retry=${Date.now()}-${attempt}`;
}

async function waitUntilBrowserOnline(maxWaitMs = 5000) {
  if (navigator.onLine !== false) return;
  await new Promise((resolve) => {
    let timer = null;
    const finish = () => {
      if (timer) window.clearTimeout(timer);
      window.removeEventListener("online", finish);
      resolve();
    };
    timer = window.setTimeout(finish, maxWaitMs);
    window.addEventListener("online", finish, { once: true });
  });
}

async function probeApiHealth() {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), API_HEALTH_TIMEOUT_MS);
  try {
    const response = await fetch(`${APP_BASE_PATH}/api/health?_reconnect=${Date.now()}`, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timer);
  }
}

async function recoverApiConnection() {
  if (apiRecoveryPromise) return apiRecoveryPromise;
  apiRecoveryPromise = (async () => {
    await waitUntilBrowserOnline();
    return probeApiHealth();
  })();
  try {
    return await apiRecoveryPromise;
  } finally {
    apiRecoveryPromise = null;
  }
}

async function api(path, options = {}) {
  const {
    attempts,
    retry = false,
    retryDelayMs = 500,
    requestTimeoutMs = API_REQUEST_TIMEOUT_MS,
    ...fetchOptions
  } = options;
  const method = (fetchOptions.method || "GET").toUpperCase();
  const retryable = method === "GET" || retry;
  const maxAttempts = attempts || (retryable ? (method === "GET" ? 5 : 3) : 1);
  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), requestTimeoutMs);
    try {
      const response = await fetch(requestPath(path, attempt), {
        ...fetchOptions,
        headers: { "Content-Type": "application/json", ...(fetchOptions.headers || {}) },
        cache: method === "GET" ? "no-store" : fetchOptions.cache,
        credentials: fetchOptions.credentials || "same-origin",
        signal: controller.signal,
      });
      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch {
          const error = new Error(`接口返回内容不是 JSON：${response.status} ${response.statusText}`);
          error.status = response.status;
          throw error;
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
      if (isNetworkError(error)) await recoverApiConnection();
      await sleep(Math.min(retryDelayMs * attempt * attempt, 5000));
    } finally {
      window.clearTimeout(timer);
    }
  }
  if (isNetworkError(lastError)) {
    throw new ApiNetworkError(
      `服务器连接中断，已尝试重新连接并自动重试 ${maxAttempts - 1} 次仍未成功，请稍后再试`,
      lastError,
      maxAttempts,
    );
  }
  throw lastError;
}

function isNetworkError(error) {
  if (!error || error.status) return false;
  if (error.network === true || error.name === "AbortError" || error.name === "TypeError") return true;
  if (error.cause && error.cause !== error && isNetworkError(error.cause)) return true;
  return /Failed to fetch|NetworkError|Load failed|fetch.*failed|网络请求失败|服务器连接中断/i.test(error.message || "");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function ensureApiConnection() {
  const health = await api("/api/health", {
    attempts: 5,
    retryDelayMs: 600,
    requestTimeoutMs: API_HEALTH_TIMEOUT_MS,
  });
  if (!health.ok) throw new Error("服务器健康检查未通过");
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
  const dividendLowVol = assetBySymbol("512890.SH") || {};
  const treasury30 = assetBySymbol("CBA21801") || {};
  const hkFee = config.fees.hk_connect_etf;
  const ibkr = config.fees.ibkr_us_etf;
  const cnCommission = feeInputNumber("cnCommission", config.fees.cn_etf.commission_rate);
  const hkCommission = feeInputNumber("hkCommission", hkFee.broker_commission_rate);
  const hkFxBps = feeInputNumber("hkFxBps", hkFee.fx_spread_bps);
  const hkPortfolio = feeInputNumber("hkPortfolioFee", hkFee.portfolio_fee_annual_rate);
  const usDividendTax = feeInputNumber("usDividendTax", config.fees.tax.us_dividend_withholding_rate);
  const officialHkPerSide = hkFee.trading_fee_rate + hkFee.transaction_levy_rate + hkFee.afrc_transaction_levy_rate;
  const fundGap = Number(hk.expense_ratio || 0) - Number(voo.expense_ratio || 0);
  const dividendLowVolProxyExpense = Number(dividendLowVol.price_fallback?.annual_expense_drag_rate || 0);
  const treasury30ProxyExpense = Number(treasury30.proxy_annual_expense_drag_rate || 0);
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
    ["红利低波代理", "基金运作费", `${fmtRate(dividendLowVolProxyExpense, 2)}/年`, "管理+托管+历史指数使用费"],
    ["30年国债代理", "基金运作费估算", `${fmtRate(treasury30ProxyExpense, 2)}/年`, "按511090管理费+托管费逐日扣除"],
    ["30年国债代理", "模拟交易佣金", `${fmtRate(cnCommission, 3)}/边`, "初始建仓用首个收盘，后续用前一已公布收盘"],
    ["511090 实盘段", "基金内扣", `${fmtRate(Number(treasury30.replacement_assets?.[0]?.management_fee || 0) + Number(treasury30.replacement_assets?.[0]?.custodian_fee || 0), 2)}/年`, "已反映在真实ETF价格中，不重复扣除"],
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
    <div class="fee-note">真实ETF的内扣费用已反映在价格或净值中，不再额外重复扣费；512890上市前的H20269全收益指数代理阶段按0.63%/年扣除，30年国债指数代理阶段按0.20%/年扣除，并分别计入模拟交易成本。</div>
  `;
}

function readConfig() {
  const next = structuredClone(config);
  const sp500SelectedKey = $("sp500Type")?.value;
  const sp500Enabled = $("enabled_sp500_group")?.checked ?? false;
  const sp500Weight = Number($("weight_sp500_group")?.value ?? 0);
  const broadEtfSelectedKey = $("broadEtfType")?.value;
  const broadEtfEnabled = $("enabled_cn_broad_etf_group")?.checked ?? false;
  const broadEtfWeight = Number($("weight_cn_broad_etf_group")?.value ?? 0);
  next.initial_capital_cny = Number($("initialCapital").value);
  next.start_date = $("startDate").value || config.start_date;
  next.end_date = $("endDate").value || config.end_date;
  next.rebalance_frequency = $("rebalanceFrequency").value;
  next.annual_rebalance_month = Number($("annualRebalanceMonth").value);
  next.rolling_window_years = Number($("rollingWindowYears").value);
  next.rebalance_month_analysis_enabled = next.rebalance_frequency === "yearly" && $("rebalanceMonthAnalysisEnabled").checked;
  next.rebalance_band = Number($("rebalanceBand").value);
  next.rebalance_to_target = $("rebalanceToTarget").checked;
  next.monthly_spend_cny = Number($("monthlySpend").value);
  next.repo_target_mode = $("repoTargetMode").value;
  next.repo_fixed_target_cny = Number($("repoFixedTarget").value);
  next.repo_fixed_target_ratio = Number($("repoFixedRatio").value);
  next.repo_symbol = $("repoSymbol").value;
  next.dip_buy_enabled = $("dipBuyEnabled").checked;
  next.dip_buy_drawdown = Number($("dipBuyDrawdown").value);
  next.dip_buy_total_parts = Number($("dipBuyTotalParts").value);
  next.dip_buy_level_mode = $("dipBuyLevelMode").value;
  next.dip_buy_cost_basis_mode = $("dipBuyCostBasisMode").value;
  next.dip_buy_recovery_sell_enabled = $("dipBuyRecoverySellEnabled").checked;
  next.dip_buy_asset_cap_enabled = $("dipBuyAssetCapEnabled").checked;
  next.dip_buy_asset_cap_ratio = Number($("dipBuyAssetCapRatio").value);
  next.dip_buy_blackout_enabled = $("dipBuyBlackoutEnabled").checked;
  next.dip_buy_blackout_months = Number($("dipBuyBlackoutMonths").value);
  delete next.dip_buy_parts_per_trigger;
  delete next.dip_buy_cooldown_trading_days;
  next.assets = next.assets.map((asset) => {
    if (isSp500Asset(asset)) {
      const selected = asset.key === sp500SelectedKey;
      return {
        ...asset,
        enabled: sp500Enabled && selected,
        target_weight: selected ? sp500Weight : 0,
      };
    }
    if (isBroadEtfAsset(asset)) {
      const selected = asset.key === broadEtfSelectedKey;
      return {
        ...asset,
        enabled: broadEtfEnabled && selected,
        target_weight: selected ? broadEtfWeight : 0,
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

function compactConfigForRequest(fullConfig) {
  const { repo_options: _repoOptions, ...requestConfig } = fullConfig;
  return {
    ...requestConfig,
    assets: fullConfig.assets.map(({ key, enabled, target_weight }) => ({ key, enabled, target_weight })),
  };
}

function repoModeLabel(mode) {
  return mode === "fixed_bucket" ? "固定消费池" : "按剩余权重";
}

function selectedTreasuryOption() {
  const symbol = $("repoSymbol")?.value || config.repo_symbol;
  return config?.repo_options?.find((option) => option.symbol === symbol);
}

function updateTreasuryHint() {
  const hint = $("treasuryFallbackHint");
  if (!hint) return;
  const option = selectedTreasuryOption();
  hint.textContent = option?.instrument_type === "money_fund"
    ? "现金池按调仓规则持有货币基金；上市前或缺少真实价格时自动使用 1 天国债逆回购补足。"
    : "闲置资金按所选期限滚动投资；临近消费日或调仓日时自动缩短期限。";
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
  if ($(`enabled_${BROAD_ETF_CONTROL_KEY}`)) {
    controls.push({
      key: BROAD_ETF_CONTROL_KEY,
      enabled: $(`enabled_${BROAD_ETF_CONTROL_KEY}`).checked,
      weight: Number($(`weight_${BROAD_ETF_CONTROL_KEY}`).value || 0),
    });
  }
  for (const asset of config.assets.filter((item) => !isSp500Asset(item) && !isBroadEtfAsset(item))) {
    controls.push({
      key: asset.key,
      enabled: $(`enabled_${asset.key}`)?.checked ?? Boolean(asset.enabled),
      weight: Number($(`weight_${asset.key}`)?.value ?? asset.target_weight ?? 0),
    });
  }
  return controls;
}

function setAssetWeightDisplay(key, weight, mode, enabled, effectiveWeight) {
  const effective = $(`effective_${key}`);
  if (!effective) return;
  $(`enabled_${key}`)?.closest(".asset-control")?.classList.toggle("is-disabled", !enabled);
  if (mode === "fixed_bucket") {
    effective.hidden = false;
    effective.textContent = enabled ? `实际配置 ${fmtPct(effectiveWeight)}` : "未启用";
  } else {
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
  const frequency = REBALANCE_FREQUENCY_NAMES[$("rebalanceFrequency")?.value] || "每年";
  const annualMonth = Number($("annualRebalanceMonth")?.value || config.annual_rebalance_month || 1);
  const rollingYears = Number($("rollingWindowYears")?.value || config.rolling_window_years || 3);
  const rebalanceText = $("rebalanceFrequency")?.value === "yearly" ? `${frequency}（${annualMonth}月）` : frequency;
  const repoValue = plan.mode === "fixed_bucket"
    ? `￥${fmtMoney(plan.repoTargetValue)} / ${fmtPct(plan.repoWeight)}`
    : fmtPct(plan.repoWeight);
  const enabledControls = currentAssetControls().filter((item) => item.enabled && item.weight > 0);
  const actualAssetWeight = plan.mode === "fixed_bucket" ? plan.remainingWeight : plan.enabledWeight;
  const controlAsset = (item) => {
    if (item.key === SP500_CONTROL_KEY) return selectedSp500Asset();
    if (item.key === BROAD_ETF_CONTROL_KEY) return selectedBroadEtfAsset();
    return config.assets.find((asset) => asset.key === item.key);
  };
  const allocationText = enabledControls.map((item) => {
    const asset = controlAsset(item);
    const weight = plan.mode === "fixed_bucket" && plan.enabledWeight > 0
      ? item.weight / plan.enabledWeight * plan.remainingWeight
      : item.weight;
    return `${assetName(asset?.symbol || item.key)} ${fmtPct(weight)}`;
  }).join(" · ") || "未启用风险资产";
  const keyItems = [
    ["回测区间", `${start} 至 ${end}`],
    ["资产组合", `${enabledControls.length} 个标的 · 风险资产 ${fmtPct(actualAssetWeight)}`],
    ["再平衡", rebalanceText],
    ["现金池", repoValue],
  ];
  const detailItems = [
    ["标的分配", allocationText],
    ["初始资金", `￥${fmtMoney(initialCapital)}`],
    ["滚动分析", `${rollingYears}年窗口 · 每年滚动`],
    ["容忍带", fmtPct($("rebalanceBand")?.value || config.rebalance_band)],
    ["超带调仓", $("rebalanceToTarget")?.checked ? "恢复到标准权重" : "仅调回容忍带以内"],
    ["现金方式", selectedTreasuryOption()?.name || assetName(config.repo_symbol)],
    ["现金模式", repoModeLabel(plan.mode)],
    ["逢低补仓", $("dipBuyEnabled")?.checked
      ? ($("rebalanceFrequency")?.value === "yearly"
        ? `开启（每 ${fmtPct(Number($("dipBuyDrawdown")?.value || config.dip_buy_drawdown || 0.05))} 一档，${$("dipBuyLevelMode")?.value === "multiplier" ? "第 N 档补 N 份" : "每档补 1 份"}，${$("dipBuyCostBasisMode")?.value === "initial" ? "最初成本" : "目前持仓成本"}${$("dipBuyAssetCapEnabled")?.checked ? `，单标的上限 ${fmtPct(Number($("dipBuyAssetCapRatio")?.value || 0.5))}` : ""}${$("dipBuyRecoverySellEnabled")?.checked ? "，回本卖出补仓份额" : ""}${$("dipBuyBlackoutEnabled")?.checked ? `，再平衡前 ${Number($("dipBuyBlackoutMonths")?.value || 0)} 个月静默` : ""}）`
        : "不生效（仅年度再平衡）")
      : "关闭"],
    ["月消费", `￥${fmtMoney(monthlySpend)}`],
  ];
  host.innerHTML = `
    <div class="control-summary-key">
      ${keyItems.map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}
    </div>
    <details class="control-summary-more">
      <summary><span>查看全部配置</span><small>${detailItems.length} 项</small><i aria-hidden="true"></i></summary>
      <div class="control-summary-details">
        ${detailItems.map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}
      </div>
    </details>`;
}

function renderAllocationSummary(plan) {
  const host = $("allocationSummary");
  if (!host) return;
  const actualAssetWeight = plan.mode === "fixed_bucket" && plan.enabledWeight > 0 ? plan.remainingWeight : plan.enabledWeight;
  const rows = [
    ["输入合计", fmtPct(plan.enabledWeight)],
    ["实际资产", fmtPct(actualAssetWeight)],
    ["现金池目标", plan.mode === "fixed_bucket" ? `￥${fmtMoney(plan.repoTargetValue)} / ${fmtPct(plan.repoWeight)}` : fmtPct(plan.repoWeight)],
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

function syncDipBuyModeTabs() {
  const levelMode = $("dipBuyLevelMode")?.value || "fixed";
  const costMode = $("dipBuyCostBasisMode")?.value || "current_average";
  document.querySelectorAll("[data-dip-level-mode]").forEach((button) => {
    const active = button.dataset.dipLevelMode === levelMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-dip-cost-mode]").forEach((button) => {
    const active = button.dataset.dipCostMode === costMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function selectDipBuyLevelMode(mode) {
  $("dipBuyLevelMode").value = mode;
  updateRepoWeight();
}

function selectDipBuyCostBasisMode(mode) {
  $("dipBuyCostBasisMode").value = mode;
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
  const yearly = $("rebalanceFrequency")?.value === "yearly";
  const dipBuyEnabled = yearly && Boolean($("dipBuyEnabled")?.checked);
  if ($("annualRebalanceMonthField")) $("annualRebalanceMonthField").hidden = !yearly;
  if ($("rebalanceMonthAnalysisField")) $("rebalanceMonthAnalysisField").hidden = !yearly;
  if ($("rebalanceMonthAnalysisEnabled")) $("rebalanceMonthAnalysisEnabled").disabled = !yearly;
  if ($("dipBuyEnabled")) $("dipBuyEnabled").disabled = !yearly;
  ["dipBuyDrawdown", "dipBuyTotalParts", "dipBuyLevelMode", "dipBuyCostBasisMode", "dipBuyRecoverySellEnabled", "dipBuyAssetCapEnabled", "dipBuyAssetCapRatio", "dipBuyBlackoutEnabled"].forEach((id) => {
    if ($(id)) $(id).disabled = !dipBuyEnabled;
  });
  document.querySelectorAll("[data-dip-level-mode], [data-dip-cost-mode]").forEach((button) => {
    button.disabled = !dipBuyEnabled;
  });
  const assetCapEnabled = dipBuyEnabled && Boolean($("dipBuyAssetCapEnabled")?.checked);
  if ($("dipBuyAssetCapRatio")) $("dipBuyAssetCapRatio").disabled = !assetCapEnabled;
  if ($("dipBuyAssetCapField")) $("dipBuyAssetCapField").setAttribute("aria-disabled", assetCapEnabled ? "false" : "true");
  if ($("dipBuyBlackoutMonths")) $("dipBuyBlackoutMonths").disabled = !dipBuyEnabled || !$("dipBuyBlackoutEnabled")?.checked;
  if ($("dipBuySettings")) $("dipBuySettings").hidden = !dipBuyEnabled;
  if ($("dipBuyAvailabilityHint")) {
    $("dipBuyAvailabilityHint").textContent = yearly
      ? "仅年度再平衡生效。现金等价物超过剩余生活费安全垫后，宽基/低波红利/黄金/国债低于成本价达到阈值时，于下一交易日开盘补仓。"
      : "当前再平衡频率不是每年，逢低补仓不会生效。";
  }
  if ($("enabled_sp500_group")) {
    updateSp500Route();
  }
  if ($("enabled_cn_broad_etf_group")) {
    updateBroadEtfRoute();
  }
  const mode = currentRepoMode();
  const controls = currentAssetControls();
  const enabledWeight = controls.reduce((sum, item) => sum + (item.enabled ? item.weight : 0), 0);
  if ($("assetWeightTitle")) $("assetWeightTitle").textContent = mode === "fixed_bucket" ? "资产分配比例" : "资产权重";
  const fixedControls = $("repoFixedControls");
  if (fixedControls) fixedControls.hidden = mode !== "fixed_bucket";
  if ($("repoFixedRatioValue")) $("repoFixedRatioValue").textContent = fmtPct(Number($("repoFixedRatio")?.value || 0));
  if ($("dipBuyDrawdownValue")) $("dipBuyDrawdownValue").textContent = fmtPct(Number($("dipBuyDrawdown")?.value || 0));
  if ($("dipBuyAssetCapRatioValue")) $("dipBuyAssetCapRatioValue").textContent = fmtPct(Number($("dipBuyAssetCapRatio")?.value || 0));
  const plan = currentRepoPlan(mode, enabledWeight);
  updateTreasuryHint();
  syncRepoModeTabs();
  syncDipBuyModeTabs();
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

function updateBroadEtfRoute() {
  const route = $("broadEtfRoute");
  const selectedKey = $("broadEtfType")?.value;
  if (!route || !selectedKey) return;
  route.innerHTML = broadEtfRouteDetails(selectedKey)
    .map(([label, value]) => `<span><em>${label}</em>${value}</span>`)
    .join("");
}

function bindAssetWeightInputs(row, key) {
  const range = row.querySelector(`#weight_${key}`);
  const percent = row.querySelector(`#weight_percent_${key}`);
  if (!range || !percent) return;
  range.addEventListener("input", () => {
    percent.value = String(Math.round(Number(range.value || 0) * 10000) / 100);
    updateRepoWeight();
  });
  percent.addEventListener("input", () => {
    const normalized = Math.min(Math.max(Number(percent.value || 0), 0), 80) / 100;
    range.value = String(normalized);
    updateRepoWeight();
  });
}

function restoreDefaultAllocation() {
  const defaults = defaultConfigSnapshot?.assets || [];
  const sp500Default = defaults.find(isSp500Asset);
  const broadDefault = defaults.find(isBroadEtfAsset);
  if ($("sp500Type") && sp500Default) $("sp500Type").value = sp500Default.key;
  if ($(`enabled_${SP500_CONTROL_KEY}`)) $(`enabled_${SP500_CONTROL_KEY}`).checked = false;
  if ($(`weight_${SP500_CONTROL_KEY}`)) $(`weight_${SP500_CONTROL_KEY}`).value = "0";
  if ($(`weight_percent_${SP500_CONTROL_KEY}`)) $(`weight_percent_${SP500_CONTROL_KEY}`).value = "0";
  if ($("broadEtfType") && broadDefault) $("broadEtfType").value = broadDefault.key;
  if ($(`enabled_${BROAD_ETF_CONTROL_KEY}`)) $(`enabled_${BROAD_ETF_CONTROL_KEY}`).checked = false;
  if ($(`weight_${BROAD_ETF_CONTROL_KEY}`)) $(`weight_${BROAD_ETF_CONTROL_KEY}`).value = "0";
  if ($(`weight_percent_${BROAD_ETF_CONTROL_KEY}`)) $(`weight_percent_${BROAD_ETF_CONTROL_KEY}`).value = "0";
  for (const asset of defaults.filter((item) => !isSp500Asset(item) && !isBroadEtfAsset(item))) {
    const enabled = $(`enabled_${asset.key}`);
    const range = $(`weight_${asset.key}`);
    const percent = $(`weight_percent_${asset.key}`);
    if (enabled) enabled.checked = Boolean(asset.enabled);
    if (range) range.value = String(asset.target_weight || 0);
    if (percent) percent.value = String(Number(asset.target_weight || 0) * 100);
  }
  updateRepoWeight();
  setMessage("已恢复默认稳健组合：低波红利、30年国债、黄金各 25%，现金 25%");
}

function renderControls() {
  controlsEventController?.abort();
  controlsEventController = new AbortController();
  const listenerOptions = { signal: controlsEventController.signal };
  $("initialCapital").value = config.initial_capital_cny;
  $("startDate").value = config.start_date;
  $("endDate").value = config.end_date;
  $("rebalanceFrequency").value = config.rebalance_frequency;
  $("annualRebalanceMonth").value = config.annual_rebalance_month ?? 1;
  $("rollingWindowYears").value = config.rolling_window_years ?? 3;
  $("rebalanceMonthAnalysisEnabled").checked = Boolean(config.rebalance_month_analysis_enabled);
  $("rebalanceBand").value = config.rebalance_band;
  $("bandValue").textContent = fmtPct(config.rebalance_band);
  $("rebalanceToTarget").checked = Boolean(config.rebalance_to_target);
  $("monthlySpend").value = config.monthly_spend_cny;
  $("repoTargetMode").value = config.repo_target_mode || "residual_weight";
  $("repoFixedTarget").value = config.repo_fixed_target_cny ?? 360000;
  $("repoFixedRatio").value = config.repo_fixed_target_ratio ?? 0;
  $("repoFixedRatioValue").textContent = fmtPct(config.repo_fixed_target_ratio ?? 0);
  $("dipBuyEnabled").checked = Boolean(config.dip_buy_enabled);
  $("dipBuyDrawdown").value = config.dip_buy_drawdown ?? 0.05;
  $("dipBuyDrawdownValue").textContent = fmtPct(config.dip_buy_drawdown ?? 0.05);
  $("dipBuyTotalParts").value = config.dip_buy_total_parts ?? 10;
  $("dipBuyLevelMode").value = config.dip_buy_level_mode ?? "fixed";
  $("dipBuyCostBasisMode").value = config.dip_buy_cost_basis_mode ?? "current_average";
  $("dipBuyRecoverySellEnabled").checked = Boolean(config.dip_buy_recovery_sell_enabled);
  $("dipBuyAssetCapEnabled").checked = Boolean(config.dip_buy_asset_cap_enabled);
  $("dipBuyAssetCapRatio").value = config.dip_buy_asset_cap_ratio ?? 0.50;
  $("dipBuyAssetCapRatioValue").textContent = fmtPct(config.dip_buy_asset_cap_ratio ?? 0.50);
  $("dipBuyBlackoutEnabled").checked = config.dip_buy_blackout_enabled ?? true;
  $("dipBuyBlackoutMonths").value = config.dip_buy_blackout_months ?? 1;
  $("repoSymbol").innerHTML = (config.repo_options || []).map((option) => `<option value="${option.symbol}">${option.name}</option>`).join("");
  $("repoSymbol").value = config.repo_symbol;
  $("repoSymbol").addEventListener("change", updateRepoWeight, listenerOptions);
  $("cnCommission").value = config.fees.cn_etf.commission_rate;
  $("ibkrPlan").value = config.fees.ibkr_us_etf.plan;
  $("fxOutBps").value = config.fees.fx.bank_out_spread_bps;
  $("fxInBps").value = config.fees.fx.bank_in_spread_bps;
  $("hkCommission").value = config.fees.hk_connect_etf.broker_commission_rate;
  $("hkFxBps").value = config.fees.hk_connect_etf.fx_spread_bps;
  $("hkPortfolioFee").value = config.fees.hk_connect_etf.portfolio_fee_annual_rate;
  $("usDividendTax").value = config.fees.tax.us_dividend_withholding_rate;
  ["cnCommission", "fxOutBps", "fxInBps", "hkCommission", "hkFxBps", "hkPortfolioFee", "usDividendTax"].forEach((id) => {
    $(id).addEventListener("input", renderFeeSummary, listenerOptions);
  });
  $("ibkrPlan").addEventListener("change", renderFeeSummary, listenerOptions);
  ["initialCapital", "startDate", "endDate", "monthlySpend", "rebalanceFrequency", "annualRebalanceMonth", "rollingWindowYears", "rebalanceMonthAnalysisEnabled", "rebalanceToTarget", "repoTargetMode", "repoFixedTarget", "repoFixedRatio", "dipBuyEnabled", "dipBuyDrawdown", "dipBuyTotalParts", "dipBuyLevelMode", "dipBuyCostBasisMode", "dipBuyRecoverySellEnabled", "dipBuyAssetCapEnabled", "dipBuyAssetCapRatio", "dipBuyBlackoutEnabled", "dipBuyBlackoutMonths"].forEach((id) => {
    $(id).addEventListener(["rebalanceFrequency", "repoTargetMode", "dipBuyLevelMode", "dipBuyCostBasisMode"].includes(id) ? "change" : "input", updateRepoWeight, listenerOptions);
  });
  document.querySelectorAll("[data-repo-mode]").forEach((button) => {
    button.addEventListener("click", () => selectRepoMode(button.dataset.repoMode), listenerOptions);
  });
  document.querySelectorAll("[data-dip-level-mode]").forEach((button) => {
    button.addEventListener("click", () => selectDipBuyLevelMode(button.dataset.dipLevelMode), listenerOptions);
  });
  document.querySelectorAll("[data-dip-cost-mode]").forEach((button) => {
    button.addEventListener("click", () => selectDipBuyCostBasisMode(button.dataset.dipCostMode), listenerOptions);
  });
  document.querySelectorAll("[data-date-preset]").forEach((button) => {
    button.addEventListener("click", () => applyDatePreset(button.dataset.datePreset), listenerOptions);
  });
  ["startDate", "endDate"].forEach((id) => {
    $(id).addEventListener("input", () => {
      document.querySelectorAll("[data-date-preset]").forEach((button) => button.classList.remove("active"));
    }, listenerOptions);
  });
  $("restoreDefaultAllocation")?.addEventListener("click", restoreDefaultAllocation, listenerOptions);

  const host = $("assetControls");
  host.innerHTML = "";
  renderSp500Control(host);
  renderBroadEtfControl(host);
  for (const asset of config.assets.filter((item) => !isSp500Asset(item) && !isBroadEtfAsset(item))) {
    const row = document.createElement("div");
    row.className = "asset-control";
    row.innerHTML = `
      <input id="enabled_${asset.key}" type="checkbox" aria-label="启用${assetName(asset.symbol)}" ${asset.enabled ? "checked" : ""} />
      <input id="weight_${asset.key}" type="range" min="0" max="0.8" step="0.01" value="${asset.target_weight}" aria-label="${assetName(asset.symbol)}目标权重" />
      <label class="asset-percent"><input id="weight_percent_${asset.key}" type="number" min="0" max="80" step="1" value="${Number(asset.target_weight || 0) * 100}" /><span>%</span></label>
      <div class="asset-name">
        <span class="asset-title">${assetName(asset.symbol)}</span>
        <span id="effective_${asset.key}" class="asset-effective" hidden></span>
      </div>
    `;
    host.appendChild(row);
    row.querySelector(`#enabled_${asset.key}`).addEventListener("change", updateRepoWeight);
    bindAssetWeightInputs(row, asset.key);
  }
  $("rebalanceBand").addEventListener("input", () => {
    $("bandValue").textContent = fmtPct($("rebalanceBand").value);
    updateRepoWeight();
  }, listenerOptions);
  updateRepoWeight();
  renderFeeSummary();
}

function renderBroadEtfControl(host) {
  const assets = broadEtfAssets();
  if (!assets.length) return;
  const selected = selectedBroadEtfAsset();
  const enabled = Boolean(selected?.enabled);
  const weight = selectedBroadEtfWeight();
  const row = document.createElement("div");
  row.className = "asset-control asset-control-group";
  row.innerHTML = `
    <input id="enabled_${BROAD_ETF_CONTROL_KEY}" type="checkbox" aria-label="启用宽基ETF" ${enabled ? "checked" : ""} />
    <input id="weight_${BROAD_ETF_CONTROL_KEY}" type="range" min="0" max="0.8" step="0.01" value="${weight}" aria-label="宽基ETF目标权重" />
    <label class="asset-percent"><input id="weight_percent_${BROAD_ETF_CONTROL_KEY}" type="number" min="0" max="80" step="1" value="${Number(weight || 0) * 100}" /><span>%</span></label>
    <div class="asset-name">
      <span class="asset-title">宽基 ETF</span>
      <span id="effective_${BROAD_ETF_CONTROL_KEY}" class="asset-effective" hidden></span>
    </div>
    <label class="asset-type">
      类型
      <select id="broadEtfType">
        ${assets.map((asset) => `<option value="${asset.key}" ${asset.key === selected?.key ? "selected" : ""}>${asset.choice_label || assetName(asset.symbol)}</option>`).join("")}
      </select>
    </label>
    <div id="broadEtfRoute" class="asset-route"></div>
  `;
  host.appendChild(row);
  row.querySelector(`#enabled_${BROAD_ETF_CONTROL_KEY}`).addEventListener("change", updateRepoWeight);
  bindAssetWeightInputs(row, BROAD_ETF_CONTROL_KEY);
  row.querySelector("#broadEtfType").addEventListener("change", updateRepoWeight);
  updateBroadEtfRoute();
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
    <label class="asset-percent"><input id="weight_percent_${SP500_CONTROL_KEY}" type="number" min="0" max="80" step="1" value="${Number(weight || 0) * 100}" /><span>%</span></label>
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
  bindAssetWeightInputs(row, SP500_CONTROL_KEY);
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

function renderSummaryGroups(primary, secondary, notes = null) {
  const noteList = (Array.isArray(notes) ? notes : [notes]).filter(Boolean);
  $("summaryGrid").innerHTML = `
    <div class="metric-group metric-group-primary">${primary.map(metricMarkup).join("")}</div>
    <div class="metric-group metric-group-secondary">${secondary.map(metricMarkup).join("")}</div>
    ${noteList.map((note) => `<div class="summary-note" role="note">
      <span class="summary-note-icon" aria-hidden="true">i</span>
      <span>${escapeHtml(note.text)}</span>
      ${note.href ? `<a href="${escapeHtml(note.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(note.linkText || "查看依据")}</a>` : ""}
    </div>`).join("")}
  `;
}

function riskPeriodMarkup(label, period, periodKind) {
  const available = period && Number.isFinite(Number(period.return));
  const tone = available ? annualReturnTone(period.return) : "muted";
  const coverage = period?.complete ? `完整自然${periodKind}` : "可用区间";
  const detail = available
    ? `${period.period || ""} · ${coverage}`
    : "运行回测后显示";
  return `<article class="risk-stat-card is-${tone}">
    <span>${label}</span>
    <strong>${available ? fmtPct(period.return) : "—"}</strong>
    <small>${escapeHtml(detail)}</small>
  </article>`;
}

function recoveryMarkup(summary = {}) {
  const recovery = summary.drawdown_recovery;
  if (!recovery) {
    return `<article class="risk-stat-card is-muted"><span>回撤恢复时间</span><strong>—</strong><small>运行回测后显示</small></article>`;
  }
  const noMeaningfulDrawdown = Math.abs(Number(summary.max_drawdown || 0)) <= 1e-12;
  if (noMeaningfulDrawdown) {
    return `<article class="risk-stat-card is-good"><span>回撤恢复时间</span><strong>0 天</strong><small>期间未形成明显回撤</small></article>`;
  }
  if (!recovery.recovered) {
    const detail = `低点 ${recovery.trough_date || "—"} 起 · 已持续 ${Number(recovery.ongoing_days || 0)} 天`;
    return `<article class="risk-stat-card is-bad"><span>回撤恢复时间</span><strong>尚未恢复</strong><small>${escapeHtml(detail)}</small></article>`;
  }
  const days = Number(recovery.recovery_days || 0);
  const tone = days <= 365 ? "good" : days <= 730 ? "warning" : "bad";
  const detail = `${recovery.trough_date || "—"} → ${recovery.recovery_date || "—"}`;
  return `<article class="risk-stat-card is-${tone}"><span>回撤恢复时间</span><strong>${days} 天</strong><small>${escapeHtml(detail)}</small></article>`;
}

function captureTone(side, value) {
  if (value == null || !Number.isFinite(Number(value))) return "muted";
  const ratio = Number(value);
  if (side === "up") {
    if (ratio >= 1) return "good";
    if (ratio >= 0.7) return "warning";
    return "bad";
  }
  if (ratio <= 0.5) return "good";
  if (ratio <= 1) return "warning";
  return "bad";
}

function captureBarMarkup(label, side, value, months) {
  const available = value != null && Number.isFinite(Number(value));
  const tone = captureTone(side, value);
  const width = available ? Math.min(Math.abs(Number(value)), 1.5) / 1.5 * 100 : 0;
  const guidance = side === "up" ? "越高代表上涨参与越充分" : "越低代表下跌防守越好";
  return `<div class="capture-item is-${tone}">
    <div class="capture-label"><span>${label}</span><strong>${available ? fmtPct(value) : "—"}</strong></div>
    <div class="capture-track" aria-label="${label} ${available ? fmtPct(value) : "暂无数据"}">
      <i class="capture-fill" style="width:${width.toFixed(2)}%"></i><b class="capture-benchmark" title="沪深300基准 100%"></b>
    </div>
    <small>${available ? `${Number(months || 0)} 个基准${side === "up" ? "上涨" : "下跌"}月 · ${guidance}` : "有效月份不足"}</small>
  </div>`;
}

function renderRiskInsights(summary = {}) {
  const host = $("riskInsightsContent");
  if (!host) return;
  host.innerHTML = `
    <div class="risk-stat-grid">
      ${riskPeriodMarkup("最差年度", summary.worst_year, "年")}
      ${riskPeriodMarkup("最差半年", summary.worst_half_year, "半年")}
      ${recoveryMarkup(summary)}
    </div>
    <article class="capture-card">
      <div class="capture-card-heading"><div><span>行情捕获率</span><small>按沪深300月度涨跌区间计算</small></div><em>基准线 100%</em></div>
      <div class="capture-grid">
        ${captureBarMarkup("上涨行情捕获率", "up", summary.upside_capture_ratio, summary.up_market_months)}
        ${captureBarMarkup("下跌行情捕获率", "down", summary.downside_capture_ratio, summary.down_market_months)}
      </div>
    </article>`;
}

function renderInitialSummary() {
  renderSummaryGroups(
    ["期末总资产", "累计收益（现金流调整）", "年化收益（现金流调整）", "最大回撤"].map((label) => ({ label, value: "--" })),
    ["原始本金折算年化", "原始本金累计盈亏", "总手续费", "实际到账现金分红", "总消费", "浮盈浮亏", "对比期末资产", "再平衡次数", "交易次数", "分红预扣税"]
      .map((label) => ({ label, value: "--" })),
  );
  renderRiskInsights();
}

function renderSummary(summary) {
  const positiveTone = (value) => Number(value || 0) >= 0 ? "positive" : "negative";
  const optionalPct = (value) => value == null || !Number.isFinite(Number(value)) ? "—" : fmtPct(value);
  const originalProfitValue = summary.net_profit_cny == null
    ? "—"
    : `￥${fmtMoney(summary.net_profit_cny)} / ${optionalPct(summary.original_capital_return)}`;
  const hasDividendLowVol = Boolean(config?.assets?.some(
    (asset) => asset.symbol === "512890.SH" && asset.enabled && Number(asset.target_weight || 0) > 0,
  ));
  const primary = [
    { label: "期末总资产", value: `￥${fmtMoney(summary.final_asset_cny)}` },
    { label: "累计收益（现金流调整）", value: fmtPct(summary.total_return), tone: positiveTone(summary.total_return) },
    { label: "年化收益（现金流调整）", value: fmtPct(summary.annualized_return), tone: positiveTone(summary.annualized_return) },
    { label: "最大回撤", value: fmtPct(summary.max_drawdown), tone: "negative" },
  ];
  const secondary = [
    {
      label: "原始本金折算年化",
      value: optionalPct(summary.original_capital_annualized_return),
      tone: summary.original_capital_annualized_return == null ? "" : positiveTone(summary.original_capital_annualized_return),
    },
    {
      label: "原始本金累计盈亏",
      value: originalProfitValue,
      tone: summary.net_profit_cny == null ? "" : positiveTone(summary.net_profit_cny),
    },
    { label: "总手续费", value: `￥${fmtMoney(summary.total_fees_cny)}` },
    { label: "实际到账现金分红", value: `￥${fmtMoney(summary.total_dividend_cny)}` },
    { label: "总消费", value: `￥${fmtMoney(summary.total_spend_cny)}` },
    { label: "浮盈浮亏", value: `￥${fmtMoney(summary.final_unrealized_pnl_cny)}`, tone: positiveTone(summary.final_unrealized_pnl_cny) },
    { label: "对比期末资产", value: `￥${fmtMoney(summary.comparison_final_asset_cny)}` },
    { label: "再平衡次数", value: fmtNum(summary.rebalance_count, 0) },
    { label: "交易次数", value: fmtNum(summary.trade_count, 0) },
    { label: "分红预扣税", value: `￥${fmtMoney(summary.withheld_tax_cny)}` },
  ];
  const dividendNote = hasDividendLowVol ? {
    text: "512890 说明：现金分红只统计 ETF 向持有人实际派发的现金。官方年报确认该 ETF 在 2023—2025 年未实施利润分配；成分股股息留在基金内，并已反映在净值和价格中。007466 等联接基金的分红不属于 512890。",
    href: "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-31/512890_20260331_5BME.pdf",
    linkText: "查看上交所年报",
  } : null;
  const returnBasisNote = {
    text: "收益口径：现金流调整收益先用（当日总资产－昨日总资产－当日外部净流入）÷昨日总资产计算每日收益，再复利与年化；不是始终拿原始本金作分母。原始本金折算口径把消费等外部净流出加回期末资产后，再对原始本金计算，未按每笔现金流发生时间加权。再平衡表的当年盈亏则为当年资产变动剔除外部收支，并同时给出占年初资产、占原始本金的比例。",
  };
  const treasuryCoverage = (summary.instrument_coverage || []).find(
    (item) => item.logical_symbol === "CBA21801",
  );
  let treasuryNote = null;
  if (treasuryCoverage) {
    const actualRatio = fmtPct(treasuryCoverage.tradable_etf_coverage_ratio || 0);
    const actualStart = treasuryCoverage.tradable_etf_start_date || treasuryCoverage.configured_etf_trade_start_date || "2023-06-13";
    const prefix = treasuryCoverage.coverage_mode === "actual_etf_only"
      ? `30年国债全区间均使用511090真实ETF行情（真实ETF覆盖 ${actualRatio}）。`
      : treasuryCoverage.coverage_mode === "proxy_only"
        ? "30年国债本区间尚无511090真实ETF阶段，全部为不可交易指数代理。"
        : `30年国债在 ${actualStart} 前使用不可交易指数代理，自该日起自动切换511090真实ETF（真实ETF覆盖 ${actualRatio}）。`;
    treasuryNote = {
      text: `${prefix}代理期按0.20%/年基金费率及境内ETF佣金估算；初始建仓使用首个可用收盘点位，后续交易只使用前一已公布收盘点位。真实ETF价格已含管理费和托管费，不重复扣除。历史结果只能用于情景与周期检验，不能保证未来趋势。`,
      href: "https://www.sse.com.cn/disclosure/announcement/listing/c/c_20230612_5722454.shtml",
      linkText: "查看511090上市依据",
    };
  }
  renderSummaryGroups(primary, secondary, [returnBasisNote, treasuryNote, dividendNote]);
  renderRiskInsights(summary);
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

function compoundedReturn(values) {
  return values.reduce((growth, value) => growth * (1 + Number(value || 0)), 1) - 1;
}

function deriveWorstCalendarPeriods(series) {
  const years = new Map();
  const halves = new Map();
  series.forEach((row) => {
    const current = parseInputDate(row.trade_date);
    if (!current) return;
    const year = current.getFullYear();
    const half = current.getMonth() < 6 ? 1 : 2;
    const append = (host, key) => {
      if (!host.has(key)) host.set(key, { dates: [], returns: [] });
      host.get(key).dates.push(row.trade_date);
      host.get(key).returns.push(Number(row.daily_return || 0));
    };
    append(years, year);
    append(halves, `${year}-${half}`);
  });
  const yearRows = [...years.entries()].map(([year, group]) => {
    const first = parseInputDate(group.dates[0]);
    const last = parseInputDate(group.dates.at(-1));
    return {
      period: `${year}年`, start_date: group.dates[0], end_date: group.dates.at(-1),
      return: compoundedReturn(group.returns),
      complete: first?.getMonth() === 0 && first.getDate() <= 7 && last?.getMonth() === 11 && last.getDate() >= 24,
    };
  });
  const halfRows = [...halves.entries()].map(([key, group]) => {
    const [year, halfText] = key.split("-");
    const half = Number(halfText);
    const first = parseInputDate(group.dates[0]);
    const last = parseInputDate(group.dates.at(-1));
    const complete = half === 1
      ? first?.getMonth() === 0 && first.getDate() <= 7 && last?.getMonth() === 5 && last.getDate() >= 24
      : first?.getMonth() === 6 && first.getDate() <= 7 && last?.getMonth() === 11 && last.getDate() >= 24;
    return {
      period: `${year}年${half === 1 ? "上" : "下"}半年`, start_date: group.dates[0], end_date: group.dates.at(-1),
      return: compoundedReturn(group.returns), complete,
    };
  });
  const chooseWorst = (rows) => {
    const complete = rows.filter((row) => row.complete);
    return (complete.length ? complete : rows).sort((left, right) => left.return - right.return)[0] || null;
  };
  return { worst_year: chooseWorst(yearRows), worst_half_year: chooseWorst(halfRows) };
}

function deriveDrawdownRecovery(series) {
  if (!series.length) return null;
  let peakNav = 1;
  let peakIndex = 0;
  let troughIndex = 0;
  let troughPeakIndex = 0;
  let troughPeakNav = 1;
  let maxDrawdown = 0;
  const navs = series.map((row) => 1 + Number(row.cumulative_return || 0));
  navs.forEach((nav, index) => {
    if (nav > peakNav) {
      peakNav = nav;
      peakIndex = index;
    }
    const drawdown = peakNav ? nav / peakNav - 1 : 0;
    if (drawdown < maxDrawdown) {
      maxDrawdown = drawdown;
      troughIndex = index;
      troughPeakIndex = peakIndex;
      troughPeakNav = peakNav;
    }
  });
  const recoveryIndex = navs.findIndex((nav, index) => index >= troughIndex && nav >= troughPeakNav * (1 - 1e-12));
  const peakDate = series[troughPeakIndex].trade_date;
  const troughDate = series[troughIndex].trade_date;
  const recoveryDate = recoveryIndex >= 0 ? series[recoveryIndex].trade_date : null;
  const endDate = series.at(-1).trade_date;
  return {
    peak_date: peakDate,
    trough_date: troughDate,
    recovery_date: recoveryDate,
    recovery_days: recoveryDate ? daysBetween(troughDate, recoveryDate) : null,
    underwater_days: daysBetween(peakDate, recoveryDate || endDate),
    ongoing_days: recoveryDate ? 0 : daysBetween(troughDate, endDate),
    recovered: Boolean(recoveryDate),
  };
}

function deriveMarketCapture(series) {
  const monthly = new Map();
  let strategyNav = 1;
  series.forEach((row) => {
    strategyNav *= 1 + Number(row.daily_return || 0);
    monthly.set(String(row.trade_date).slice(0, 7), [strategyNav, 1 + Number(row.benchmark_return || 0)]);
  });
  const strategyUp = [];
  const benchmarkUp = [];
  const strategyDown = [];
  const benchmarkDown = [];
  const endpoints = [...monthly.values()];
  for (let index = 1; index < endpoints.length; index += 1) {
    const previous = endpoints[index - 1];
    const current = endpoints[index];
    const strategyReturn = previous[0] ? current[0] / previous[0] - 1 : 0;
    const benchmarkReturn = previous[1] ? current[1] / previous[1] - 1 : 0;
    if (benchmarkReturn > 1e-12) {
      strategyUp.push(strategyReturn);
      benchmarkUp.push(benchmarkReturn);
    } else if (benchmarkReturn < -1e-12) {
      strategyDown.push(strategyReturn);
      benchmarkDown.push(benchmarkReturn);
    }
  }
  const annualized = (values) => values.length
    ? values.reduce((growth, value) => growth * Math.max(1 + value, 0), 1) ** (12 / values.length) - 1
    : null;
  const capture = (strategy, benchmark) => {
    const strategyReturn = annualized(strategy);
    const benchmarkReturn = annualized(benchmark);
    return strategyReturn == null || benchmarkReturn == null || Math.abs(benchmarkReturn) <= 1e-12
      ? null
      : strategyReturn / benchmarkReturn;
  };
  return {
    upside_capture_ratio: capture(strategyUp, benchmarkUp),
    downside_capture_ratio: capture(strategyDown, benchmarkDown),
    up_market_months: benchmarkUp.length,
    down_market_months: benchmarkDown.length,
  };
}

function expandChartSeries(data) {
  if (Array.isArray(data?.series)) return data.series;
  const chart = data?.chart || {};
  const dates = chart.dates || [];
  const values = chart.values || {};
  const weights = chart.weights || {};
  return dates.map((tradeDate, index) => ({
    trade_date: tradeDate,
    total_asset_cny: chart.total_assets?.[index] ?? null,
    daily_return: chart.daily_returns?.[index] ?? 0,
    cumulative_return: chart.cumulative_returns?.[index] ?? 0,
    drawdown: chart.drawdowns?.[index] ?? 0,
    benchmark_return: chart.benchmark_returns?.[index] ?? 0,
    payload: {
      comparison: { total_asset_cny: chart.comparison_total_assets?.[index] ?? null },
      values: Object.fromEntries(Object.entries(values).map(([symbol, amounts]) => [symbol, amounts[index] ?? 0])),
      weights: Object.fromEntries(Object.entries(weights).map(([symbol, values]) => [symbol, values[index] ?? 0])),
    },
  }));
}

function deriveSummary(summary, series) {
  if (!series.length) return summary;
  const last = series.at(-1);
  const calendarRisk = deriveWorstCalendarPeriods(series);
  const recovery = deriveDrawdownRecovery(series);
  const capture = deriveMarketCapture(series);
  return {
    ...summary,
    final_asset_cny: summary.final_asset_cny ?? last.total_asset_cny,
    total_return: summary.total_return ?? last.cumulative_return,
    max_drawdown: summary.max_drawdown ?? Math.min(...series.map((row) => row.drawdown ?? 0)),
    comparison_final_asset_cny: last.payload?.comparison?.total_asset_cny ?? summary.comparison_final_asset_cny,
    worst_year: summary.worst_year ?? calendarRisk.worst_year,
    worst_half_year: summary.worst_half_year ?? calendarRisk.worst_half_year,
    drawdown_recovery: summary.drawdown_recovery ?? recovery,
    upside_capture_ratio: summary.upside_capture_ratio ?? capture.upside_capture_ratio,
    downside_capture_ratio: summary.downside_capture_ratio ?? capture.downside_capture_ratio,
    up_market_months: summary.up_market_months ?? capture.up_market_months,
    down_market_months: summary.down_market_months ?? capture.down_market_months,
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
  if (chartId === "dailyPnlChart") loadDailyPnlChart().catch(() => {});
  window.requestAnimationFrame(() => {
    applyChartOption(chartId);
    charts[chartId]?.resize();
  });
}

function resetDailyPnlChart() {
  dailyPnlRequestVersion += 1;
  dailyPnlData = null;
  dailyPnlRunId = null;
  dailyPnlLoadingRunId = null;
  delete pendingChartOptions.dailyPnlChart;
  charts.dailyPnlChart?.clear();
  const empty = $("dailyPnlEmpty");
  if (empty) empty.hidden = true;
}

function showDailyPnlEmpty(message) {
  charts.dailyPnlChart?.clear();
  const empty = $("dailyPnlEmpty");
  if (!empty) return;
  empty.textContent = message;
  empty.hidden = false;
}

function dailyPnlAxisBounds(seriesArrays) {
  const finiteValues = seriesArrays.flat().map(Number).filter(Number.isFinite);
  const maxAbs = Math.max(...finiteValues.map((value) => Math.abs(value)), 0);
  if (maxAbs <= 1e-12) return { min: -1, max: 1 };
  const magnitude = 10 ** Math.floor(Math.log10(maxAbs));
  const bound = Math.ceil((maxAbs * 1.08) / magnitude * 2) / 2 * magnitude;
  return { min: -bound, max: bound };
}

function fmtAxisMoney(value) {
  const absolute = Math.abs(Number(value || 0));
  if (absolute >= 100_000_000) return `${(Number(value) / 100_000_000).toFixed(1)}亿`;
  if (absolute >= 10_000) return `${(Number(value) / 10_000).toFixed(1)}万`;
  return fmtMoney(value);
}

function fmtSignedMoney(value) {
  const amount = Number(value || 0);
  return `${amount > 0 ? "+" : amount < 0 ? "-" : ""}￥${fmtMoney(Math.abs(amount))}`;
}

function fmtSignedPct(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const rate = Number(value);
  return `${rate > 0 ? "+" : ""}${(rate * 100).toFixed(2)}%`;
}

const DAILY_PNL_MODES = {
  amount: {
    title: "标的单日盈亏 · 金额",
    assetField: "profits",
    combinedField: "combined_profits",
    portfolioField: "portfolio_profits",
    benchmarkField: "benchmark_profits",
    amount: true,
    basis: "当日市值变化剔除买卖本金，并计入交易费、持仓费和分红。",
  },
  percent: {
    title: "标的单日盈亏 · 收益率",
    assetField: "returns",
    combinedField: "combined_returns",
    portfolioField: "portfolio_returns",
    benchmarkField: "benchmark_returns",
    amount: false,
    basis: "单日收益率＝当日盈亏÷当日投入资本；它不是从峰值计算的回撤。",
  },
  cumulative: {
    title: "标的累计收益 · 每日复利",
    assetField: "cumulative_returns",
    combinedField: "combined_cumulative_returns",
    portfolioField: "portfolio_cumulative_returns",
    benchmarkField: "benchmark_cumulative_returns",
    amount: false,
    basis: "标的按每日流量调整收益连续复利；组合总资产使用回测主序列的累计收益。",
  },
  drawdown: {
    title: "标的回撤 · 距各自历史峰值",
    assetField: "drawdowns",
    combinedField: "combined_drawdowns",
    portfolioField: "portfolio_drawdowns",
    benchmarkField: "benchmark_drawdowns",
    amount: false,
    basis: "回撤＝当日净值÷自身历史峰值－1；组合总资产与主回撤图完全同口径。",
  },
};

function dailyPnlMetric(value, amountMode) {
  return amountMode ? fmtSignedMoney(value) : fmtSignedPct(value);
}

function dailyPnlTooltip(data, colors, mode) {
  return (params) => {
    const points = Array.isArray(params) ? params : [params];
    const index = points.find((point) => Number.isInteger(point?.dataIndex))?.dataIndex;
    if (!Number.isInteger(index)) return "";
    const assetValues = data[mode.assetField] || {};
    const combinedValues = data[mode.combinedField] || [];
    const portfolioValues = data[mode.portfolioField] || [];
    const benchmarkValues = data[mode.benchmarkField] || [];
    const assetLines = data.symbols.map((symbol, symbolIndex) => (
      `<div style="display:grid;grid-template-columns:10px minmax(92px,1fr) auto;align-items:center;gap:7px 11px">`
      + `<i style="width:8px;height:8px;border-radius:50%;background:${colors[symbolIndex % colors.length]}"></i>`
      + `<span>${escapeHtml(data.names[symbol] || symbol)}</span>`
      + `<strong>${dailyPnlMetric(assetValues[symbol]?.[index], mode.amount)}</strong></div>`
    )).join("");
    return [
      `<div style="font-weight:700;margin-bottom:7px">${escapeHtml(data.dates[index])}</div>`,
      assetLines,
      '<div style="border-top:1px solid rgba(255,255,255,.22);margin:7px 0 5px"></div>',
      `<div style="display:flex;justify-content:space-between;gap:26px"><span>所选标的合计</span><strong>${dailyPnlMetric(combinedValues[index], mode.amount)}</strong></div>`,
      `<div style="display:flex;justify-content:space-between;gap:26px"><span>组合总资产（含现金管理）</span><strong>${dailyPnlMetric(portfolioValues[index], mode.amount)}</strong></div>`,
      `<div style="display:flex;justify-content:space-between;gap:26px"><span>沪深300等额参考</span><strong>${dailyPnlMetric(benchmarkValues[index], mode.amount)}</strong></div>`,
      `<div style="max-width:390px;margin-top:7px;padding-top:6px;border-top:1px solid rgba(255,255,255,.16);font-size:11px;opacity:.78">${escapeHtml(mode.basis)}</div>`,
    ].join("");
  };
}

function renderDailyPnlChart() {
  const data = dailyPnlData;
  if (!data?.available) {
    showDailyPnlEmpty(data?.reason || "运行回测后查看逐标的每日盈亏");
    return;
  }
  const empty = $("dailyPnlEmpty");
  if (empty) empty.hidden = true;
  const colors = [CHART_COLORS.accent, CHART_COLORS.amber, CHART_COLORS.violet, "#5c8f99", "#9d6c52", "#7e8d50", "#b35f78"];
  const mode = DAILY_PNL_MODES[dailyPnlScale] || DAILY_PNL_MODES.amount;
  const assetValues = data[mode.assetField] || {};
  const selectedValues = data.symbols.map((symbol) => assetValues[symbol] || []);
  const combinedValues = data[mode.combinedField] || [];
  const portfolioValues = data[mode.portfolioField] || [];
  const benchmarkValues = data[mode.benchmarkField] || [];
  const bounds = dailyPnlAxisBounds([...selectedValues, combinedValues, portfolioValues, benchmarkValues]);
  if (!window.echarts) {
    const toPoints = (values) => values.map((value, index) => ({ x: index, y: Number(value ?? 0) }));
    drawFallbackChart(
      "dailyPnlChart",
      mode.title,
      [
        ...data.symbols.map((symbol, index) => ({ name: data.names[symbol] || symbol, color: colors[index % colors.length], points: toPoints(selectedValues[index]) })),
        { name: "所选标的合计", color: "#6f7f86", points: toPoints(combinedValues) },
        { name: "组合总资产（含现金管理）", color: "#172b35", points: toPoints(portfolioValues) },
        { name: "沪深300等额参考", color: CHART_COLORS.blue, points: toPoints(benchmarkValues) },
      ],
      !mode.amount,
      bounds.min,
      bounds.max,
    );
    return;
  }
  queueChartOption("dailyPnlChart", {
    ...lineZoomOption(),
    grid: { left: 72, right: 26, top: 82, bottom: 62 },
    title: { text: mode.title, left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", formatter: dailyPnlTooltip(data, colors, mode) },
    legend: { type: "scroll", top: 31, left: 8, right: 12, textStyle: { fontSize: 10 } },
    xAxis: { type: "category", data: data.dates },
    yAxis: {
      type: "value",
      min: bounds.min,
      max: bounds.max,
      axisLabel: { formatter: mode.amount ? (value) => fmtAxisMoney(value) : (value) => `${(value * 100).toFixed(1)}%` },
    },
    series: [
      ...data.symbols.map((symbol, index) => ({
        type: "line",
        name: data.names[symbol] || symbol,
        data: selectedValues[index],
        symbol: "none",
        connectNulls: false,
        lineStyle: { color: colors[index % colors.length], width: 1.25, opacity: 0.82 },
        itemStyle: { color: colors[index % colors.length] },
      })),
      {
        type: "line",
        name: "所选标的合计",
        data: combinedValues,
        symbol: "none",
        lineStyle: { color: "#6f7f86", width: 1.8, type: "dashed" },
        itemStyle: { color: "#6f7f86" },
      },
      {
        type: "line",
        name: "组合总资产（含现金管理）",
        data: portfolioValues,
        symbol: "none",
        lineStyle: { color: "#172b35", width: 2.6 },
        itemStyle: { color: "#172b35" },
        markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: "#9eaaaf", width: 1 }, data: [{ yAxis: 0 }] },
      },
      {
        type: "line",
        name: "沪深300等额参考",
        data: benchmarkValues,
        symbol: "none",
        lineStyle: { color: CHART_COLORS.blue, width: 1.8, type: "dashed" },
        itemStyle: { color: CHART_COLORS.blue },
      },
    ],
  });
  if (activeChartId === "dailyPnlChart") {
    applyChartOption("dailyPnlChart");
    charts.dailyPnlChart?.resize();
  }
}

async function loadDailyPnlChart() {
  const runId = currentRunId;
  if (!runId) {
    showDailyPnlEmpty("运行回测后查看逐标的每日盈亏");
    return;
  }
  if (dailyPnlRunId === runId && dailyPnlData) {
    renderDailyPnlChart();
    return;
  }
  if (dailyPnlLoadingRunId === runId) return;
  const requestVersion = ++dailyPnlRequestVersion;
  dailyPnlLoadingRunId = runId;
  showDailyPnlEmpty("正在加载全部交易日的逐标的盈亏…");
  try {
    const response = await api(`/api/backtest/${encodeURIComponent(runId)}/daily-pnl`, { attempts: 4, retryDelayMs: 500 });
    if (requestVersion !== dailyPnlRequestVersion || currentRunId !== runId) return;
    dailyPnlRunId = runId;
    dailyPnlData = response.daily_pnl;
    renderDailyPnlChart();
  } catch (error) {
    if (requestVersion === dailyPnlRequestVersion && currentRunId === runId) {
      showDailyPnlEmpty(`逐日盈亏加载失败：${humanizeError(error.message)}`);
    }
  } finally {
    if (requestVersion === dailyPnlRequestVersion) dailyPnlLoadingRunId = null;
  }
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

function polishChart(chart) {
  chart.setOption({
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
  });
}

function queueChartOption(id, option) {
  pendingChartOptions[id] = option;
}

function applyChartOption(id) {
  const option = pendingChartOptions[id];
  if (!option || !window.echarts) return;
  const chart = ensureChart(id);
  chart.setOption(option, true);
  polishChart(chart);
  delete pendingChartOptions[id];
}

function activeWeightSymbols(series) {
  return [...new Set(series.flatMap((row) => Object.keys(row.payload?.weights || {})))]
    .filter((symbol) => series.some((row) => Math.abs(Number(row.payload?.weights?.[symbol] || 0)) > 1e-8));
}

function portfolioSnapshotTooltip(series, metrics) {
  const symbols = activeWeightSymbols(series);
  return (params) => {
    const points = Array.isArray(params) ? params : [params];
    const dataIndex = points.find((point) => Number.isInteger(point?.dataIndex))?.dataIndex;
    const row = Number.isInteger(dataIndex) ? series[dataIndex] : null;
    if (!row) return "";

    const metricLines = metrics.map(({ label, field }) => (
      `<div style="display:flex;justify-content:space-between;gap:24px"><span>${escapeHtml(label)}</span><strong>${fmtPct(row[field])}</strong></div>`
    )).join("");
    const holdingLines = symbols
      .filter((symbol) => {
        const amount = Number(row.payload?.values?.[symbol] || 0);
        const weight = Number(row.payload?.weights?.[symbol] || 0);
        return Math.abs(amount) >= 0.005 || Math.abs(weight) > 1e-8;
      })
      .map((symbol) => {
        const amount = Number(row.payload?.values?.[symbol] || 0);
        const weight = Number(row.payload?.weights?.[symbol] || 0);
        return `<div style="display:flex;justify-content:space-between;gap:24px"><span>${escapeHtml(assetName(symbol))}</span><strong>￥${fmtMoney(amount)} · ${fmtPct(weight)}</strong></div>`;
      }).join("");

    return [
      `<div style="font-weight:700;margin-bottom:5px">${escapeHtml(row.trade_date)}</div>`,
      metricLines,
      `<div style="display:flex;justify-content:space-between;gap:24px"><span>组合总资产</span><strong>￥${fmtMoney(row.total_asset_cny)}</strong></div>`,
      holdingLines ? '<div style="border-top:1px solid rgba(255,255,255,.22);margin:6px 0 5px;padding-top:5px;color:#d9e5e1">各标的金额 · 组合占比</div>' : "",
      holdingLines,
    ].join("");
  };
}

function renderCharts(series) {
  if (!series.length) return;
  $("analysisEmpty").hidden = true;
  if (!window.echarts) {
    renderFallbackCharts(series);
    return;
  }
  const dates = series.map((row) => row.trade_date);
  queueChartOption("assetChart", {
    ...lineZoomOption(),
    title: { text: "总资产", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", scale: true },
    series: [{ type: "line", name: "总资产", data: series.map((row) => row.total_asset_cny), smooth: true, symbol: "none", lineStyle: { color: CHART_COLORS.accent, width: 2.4 }, itemStyle: { color: CHART_COLORS.accent } }],
  });
  queueChartOption("comparisonChart", {
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
  queueChartOption("returnChart", {
    ...lineZoomOption(),
    title: { text: "收益率对比沪深300", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: "axis",
      formatter: portfolioSnapshotTooltip(series, [
        { label: "策略累计收益", field: "cumulative_return" },
        { label: "沪深300累计收益", field: "benchmark_return" },
      ]),
    },
    legend: { top: 4, right: 10 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: [
      { type: "line", name: "策略", data: series.map((row) => row.cumulative_return), smooth: true, symbol: "none", lineStyle: { color: CHART_COLORS.accent, width: 2.3 }, itemStyle: { color: CHART_COLORS.accent } },
      { type: "line", name: "沪深300", data: series.map((row) => row.benchmark_return), smooth: true, symbol: "none", lineStyle: { color: CHART_COLORS.blue, width: 1.8, type: "dashed" }, itemStyle: { color: CHART_COLORS.blue } },
    ],
  });
  queueChartOption("dailyReturnChart", {
    ...lineZoomOption(),
    title: { text: "单日收益", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtPct(v) },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(1)}%` } },
    series: [{ type: "line", name: "单日收益", data: series.map((row) => row.daily_return), smooth: false, symbol: "none", lineStyle: { color: CHART_COLORS.violet, width: 1.4 }, itemStyle: { color: CHART_COLORS.violet } }],
  });
  queueChartOption("drawdownChart", {
    ...lineZoomOption(),
    title: { text: "回撤", left: 8, top: 4, textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: "axis",
      formatter: portfolioSnapshotTooltip(series, [{ label: "组合回撤", field: "drawdown" }]),
    },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
    series: [{ type: "line", areaStyle: { color: "rgba(211, 66, 63, 0.12)" }, name: "回撤", data: series.map((row) => row.drawdown), symbol: "none", lineStyle: { color: CHART_COLORS.danger, width: 1.8 }, itemStyle: { color: CHART_COLORS.danger } }],
  });

  const symbols = activeWeightSymbols(series);
  const weightColors = [CHART_COLORS.accent, CHART_COLORS.blue, CHART_COLORS.amber, CHART_COLORS.violet, "#5c8f99", "#9d6c52", "#7e8d50"];
  queueChartOption("weightChart", {
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
      data: series.map((row) => row.payload?.weights?.[symbol] || 0),
      symbol: "none",
      lineStyle: { width: 1.2, color: weightColors[index % weightColors.length] },
      itemStyle: { color: weightColors[index % weightColors.length] },
    })),
  });
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
  const symbols = activeWeightSymbols(series);
  const colors = ["#1f7a5a", "#2f5aa8", "#b45f06", "#7a3db8", "#667085"];
  drawFallbackChart("weightChart", "资产权重", symbols.map((symbol, index) => ({
    name: assetName(symbol),
    color: colors[index % colors.length],
    points: makePointSeries(series.map((row) => row.payload?.weights?.[symbol] || 0)),
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

function tableSortValue(value) {
  if (value && typeof value === "object" && "raw" in value) return value.raw;
  if (typeof value === "number") return value;
  return value == null ? null : String(value);
}

function compareTableValues(left, right, direction) {
  const leftEmpty = left == null || left === "" || (typeof left === "number" && !Number.isFinite(left));
  const rightEmpty = right == null || right === "" || (typeof right === "number" && !Number.isFinite(right));
  if (leftEmpty || rightEmpty) {
    if (leftEmpty && rightEmpty) return 0;
    return leftEmpty ? 1 : -1;
  }
  let result = 0;
  if (typeof left === "number" && typeof right === "number") result = left - right;
  else result = String(left).localeCompare(String(right), "zh-CN", { numeric: true });
  return direction === "asc" ? result : -result;
}

function renderTable(id, columns, rows, options = {}) {
  const table = $(id);
  if (!rows.length) {
    table.innerHTML = "<tbody><tr><td class=\"table-empty\">暂无数据</td></tr></tbody>";
    return;
  }
  const pageSize = Number(options.pageSize || rows.length);
  const visibleCount = Math.min(Number(options.visibleCount || pageSize), rows.length);
  const sortableColumns = new Set(options.sortableColumns || []);
  const sortState = tableSortState[id] || options.defaultSort || null;
  let orderedRows = options.newestFirst && !sortState ? [...rows].reverse() : [...rows];
  if (sortState && sortableColumns.has(sortState.column)) {
    orderedRows = orderedRows
      .map((row, index) => ({ row, index }))
      .sort((left, right) => compareTableValues(
        tableSortValue(left.row[sortState.column]),
        tableSortValue(right.row[sortState.column]),
        sortState.direction,
      ) || left.index - right.index)
      .map((item) => item.row);
  }
  const visibleRows = orderedRows.slice(0, visibleCount);
  const remaining = rows.length - visibleCount;
  const headerMarkup = columns.map((col) => {
    const sortable = sortableColumns.has(col);
    const active = sortState?.column === col;
    const direction = active ? sortState.direction : "none";
    const indicator = active ? (direction === "asc" ? "↑" : "↓") : "↕";
    if (!sortable) return `<th>${escapeHtml(col)}</th>`;
    return `<th aria-sort="${active ? (direction === "asc" ? "ascending" : "descending") : "none"}"><button type="button" class="table-sort" data-table-sort="${escapeHtml(col)}"><span>${escapeHtml(col)}</span><i aria-hidden="true">${indicator}</i></button></th>`;
  }).join("");
  table.innerHTML = `
    <thead><tr>${headerMarkup}</tr></thead>
    <tbody>
      ${visibleRows.map((row) => `<tr class="${row.__selected ? "is-selected" : ""}">${columns.map((col) => `<td data-label="${escapeHtml(col)}">${formatCell(row[col])}</td>`).join("")}</tr>`).join("")}
    </tbody>
    ${remaining > 0 ? `<tfoot><tr><td colspan="${columns.length}"><button type="button" class="table-more">再显示 ${Math.min(pageSize, remaining)} 条（剩余 ${remaining} 条）</button></td></tr></tfoot>` : ""}
  `;
  table.querySelectorAll("[data-table-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      const column = button.dataset.tableSort;
      const current = tableSortState[id] || options.defaultSort || {};
      const defaultDirection = options.sortDirections?.[column] || "desc";
      tableSortState[id] = {
        column,
        direction: current.column === column ? (current.direction === "desc" ? "asc" : "desc") : defaultDirection,
      };
      renderTable(id, columns, rows, { ...options, visibleCount: pageSize });
    });
  });
  table.querySelector(".table-more")?.addEventListener("click", () => {
    renderTable(id, columns, rows, { ...options, visibleCount: visibleCount + pageSize });
  });
}

function formatCell(value) {
  if (value && typeof value === "object" && value.kind === "metric") {
    let text = "—";
    if (value.raw != null && Number.isFinite(Number(value.raw))) {
      text = value.format === "ratio" ? fmtRatio(value.raw) : fmtPct(value.raw);
    }
    return `<span class="table-metric is-${escapeHtml(value.tone || "muted")}">${escapeHtml(text)}</span>`;
  }
  if (value && typeof value === "object" && value.kind === "performance") {
    return formatPerformanceCell(value);
  }
  if (value && typeof value === "object" && value.kind === "year-profit") {
    if (value.profit == null || !Number.isFinite(Number(value.profit))) {
      return `<span class="table-year-profit is-muted">重新回测后显示</span>`;
    }
    const profit = Number(value.profit);
    const tone = profit > 1e-9 ? "positive" : profit < -1e-9 ? "negative" : "flat";
    const yearStartRate = value.yearStartRate == null ? "—" : fmtPct(value.yearStartRate);
    const originalRate = value.originalRate == null ? "—" : fmtPct(value.originalRate);
    const title = `年初资产 ￥${fmtMoney(value.yearStartTotal)}；期间外部净流入 ￥${fmtMoney(value.externalFlow)}`;
    return `<span class="table-year-profit is-${tone}" title="${escapeHtml(title)}"><strong>${profit >= 0 ? "+" : "−"}￥${fmtMoney(Math.abs(profit))}</strong><small>年初 ${escapeHtml(yearStartRate)} · 本金 ${escapeHtml(originalRate)}</small></span>`;
  }
  if (typeof value === "number") {
    const formatted = Math.abs(value) < 1 && value !== 0 ? fmtPct(value) : fmtNum(value, 2);
    return value < 0 ? `<span class="negative">${formatted}</span>` : formatted;
  }
  return escapeHtml(value ?? "");
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

function rebalanceAssetColumnName(symbol) {
  return SHORT_NAMES[symbol] || assetName(symbol);
}

function rebalanceCashEquivalentSymbols() {
  const symbols = new Set(["REPO"]);
  for (const option of config.repo_options || []) {
    if (["repo", "money_fund"].includes(option.instrument_type) && option.symbol) {
      symbols.add(option.symbol);
    }
  }
  if (config.repo_symbol) symbols.add(config.repo_symbol);
  return symbols;
}

function rebalanceDisplayRows(rows) {
  const cashEquivalentSymbols = rebalanceCashEquivalentSymbols();
  const symbols = [];
  for (const row of rows) {
    for (const symbol of Object.keys(row.payload?.asset_performance || {})) {
      if (cashEquivalentSymbols.has(symbol)) continue;
      if (!symbols.includes(symbol)) symbols.push(symbol);
    }
  }
  const orderedSymbols = config.assets.map((asset) => asset.symbol).filter((symbol) => symbols.includes(symbol));
  for (const symbol of symbols) {
    if (!orderedSymbols.includes(symbol)) orderedSymbols.push(symbol);
  }
  const baseColumns = ["执行日", "决策日", "收益年度", "当年收益（现金流调整）", "当年盈亏", "当年最大回撤", "当年手续费"];
  const assetColumns = orderedSymbols.map(rebalanceAssetColumnName);
  const displayRows = rows.map((row) => {
    const item = {
      执行日: row.rebalance_date,
      决策日: row.payload?.decision_date || row.rebalance_date,
      收益年度: row.payload?.year_label ? `${row.payload.year_label}年` : `${String(row.payload?.decision_date || row.rebalance_date).slice(0, 4)}年`,
      "当年收益（现金流调整）": row.payload?.year_return ?? row.period_return,
      当年盈亏: {
        kind: "year-profit",
        profit: row.payload?.year_profit_cny,
        yearStartRate: row.payload?.year_profit_on_year_start,
        originalRate: row.payload?.year_profit_on_original_capital,
        yearStartTotal: row.payload?.year_start_total_cny,
        externalFlow: row.payload?.year_external_flow_cny,
      },
      当年最大回撤: row.payload?.year_max_drawdown ?? row.payload?.period_max_drawdown ?? 0,
      当年手续费: row.payload?.year_fee_cny ?? row.fee_cny,
    };
    for (const symbol of orderedSymbols) {
      const periodPerf = row.payload?.asset_performance?.[symbol];
      const annualPerf = row.payload?.year_asset_performance?.[symbol];
      const legacyRepo = symbol === "REPO" && Number(row.payload?.asset_performance_version || 1) < 2;
      const perf = (legacyRepo ? periodPerf || annualPerf : annualPerf || periodPerf) || {};
      item[rebalanceAssetColumnName(symbol)] = {
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
    await sleep(Math.min(650 + pollCount * 100, 1500));
  }
}

async function loadBacktestResultSections(runId, onSeries, prefetchedChart = null) {
  setMessage("计算完成，正在生成图表...");
  const seriesPromise = prefetchedChart
    ? Promise.resolve({ chart: prefetchedChart })
    : api(`/api/backtest/${runId}/chart-series`, { attempts: 6, retryDelayMs: 700 });
  const rebalancePromise = api(`/api/backtest/${runId}/rebalance`, { attempts: 5, retryDelayMs: 700 });
  const tradesPromise = api(`/api/backtest/${runId}/trades`, { attempts: 5, retryDelayMs: 700 });
  const series = computeSeriesMetrics(expandChartSeries(await seriesPromise));
  await onSeries?.(series);
  setMessage("图表已显示，正在加载调仓与交易记录...");
  const [rebalance, trades] = await Promise.all([rebalancePromise, tradesPromise]);
  return { series, rebalance, trades };
}

async function watchBacktestAnalysis(runId, rebalance, trades) {
  const watchId = ++activeAnalysisWatch;
  let failures = 0;
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await sleep(1000);
    if (watchId !== activeAnalysisWatch || currentRunId !== runId) return;
    try {
      const entry = await api(`/api/backtest/${encodeURIComponent(runId)}`, { attempts: 2, retryDelayMs: 500 });
      failures = 0;
      const status = entry.summary?.analysis_status || "completed";
      if (status === "completed" || status === "not_required") {
        renderSummary(entry.summary);
        renderBacktestRecords(entry.summary, rebalance, trades);
        scheduleArchiveRefresh({ includeLeaderboard: activeArchiveView === "leaderboard" });
        setMessage("主体结果、滚动窗口和月份对比均已完成");
        return;
      }
      if (status === "failed") {
        setMessage(`主体结果已显示；扩展分析失败：${humanizeError(entry.summary?.analysis_error || "未知错误")}`, true);
        return;
      }
    } catch (error) {
      failures += 1;
      if (failures >= 5) {
        console.warn("无法继续获取后台分析进度", error);
        return;
      }
    }
  }
}

function renderBacktestRecords(summary, rebalance, trades) {
  const rollingPeriods = summary?.rolling_periods || [];
  renderTable(
    "rollingTable",
    ["回测窗口", "开始日期", "结束日期", "窗口长度", "年盈利率", "最大回撤", "年盈利/回撤比"],
    rollingPeriods.map((row) => {
      const ratio = row.annual_return_drawdown_ratio ?? annualReturnDrawdownRatio(row);
      return {
      回测窗口: row.period || `第 ${row.sequence} 组`,
      开始日期: row.start_date,
      结束日期: row.end_date,
      窗口长度: `${row.window_years}年`,
      年盈利率: metricCell(row.annualized_return, "percent", annualReturnTone(row.annualized_return)),
      最大回撤: metricCell(row.max_drawdown, "percent", drawdownTone(row.max_drawdown)),
      "年盈利/回撤比": metricCell(ratio, "ratio", ratioTone(ratio)),
    };
    }),
    {
      pageSize: 100,
      sortableColumns: ["回测窗口", "开始日期", "结束日期", "年盈利率", "最大回撤", "年盈利/回撤比"],
      defaultSort: { column: "年盈利/回撤比", direction: "desc" },
      sortDirections: { 开始日期: "asc", 结束日期: "asc", 最大回撤: "desc" },
    },
  );
  $("recordTabRolling").textContent = `滚动窗口（${rollingPeriods.length}）`;

  const monthScenarios = summary?.rebalance_month_scenarios || [];
  renderTable(
    "monthsTable",
    ["再平衡月份", "年盈利率", "最大回撤", "年盈利/回撤比", "当前选择"],
    monthScenarios.map((row) => {
      const ratio = row.annual_return_drawdown_ratio ?? annualReturnDrawdownRatio(row);
      return {
      再平衡月份: row.month_name || `${row.month}月`,
      年盈利率: metricCell(row.annualized_return, "percent", annualReturnTone(row.annualized_return)),
      最大回撤: metricCell(row.max_drawdown, "percent", drawdownTone(row.max_drawdown)),
      "年盈利/回撤比": metricCell(ratio, "ratio", ratioTone(ratio)),
      当前选择: row.selected ? "是" : "",
      __selected: Boolean(row.selected),
    };
    }),
    {
      pageSize: 12,
      sortableColumns: ["再平衡月份", "年盈利率", "最大回撤", "年盈利/回撤比"],
      defaultSort: { column: "年盈利/回撤比", direction: "desc" },
      sortDirections: { 再平衡月份: "asc", 最大回撤: "desc" },
    },
  );
  $("recordTabMonths").textContent = `月份对比（${monthScenarios.length}）`;
  if (["pending", "running"].includes(summary?.analysis_status)) {
    $("recordTabRolling").textContent = "滚动窗口（后台计算中）";
    $("recordTabMonths").textContent = "月份对比（后台计算中）";
  }

  const rebalanceTable = rebalanceDisplayRows(rebalance.rebalance || []);
  renderTable("rebalanceTable", rebalanceTable.columns, rebalanceTable.rows, { pageSize: 200, newestFirst: true });
  $("recordTabRebalance").textContent = `再平衡记录（${rebalanceTable.rows.length}）`;
  renderTable(
    "tradesTable",
    ["交易日期", "标的名称", "方向", "份额", "价格", "成交额", "费用", "币种", "原因"],
    (trades.trades || []).map((row) => ({
      交易日期: row.trade_date,
      标的名称: tradeAssetName(row.symbol),
      方向: SIDE_NAMES[row.side] || row.side,
      份额: row.quantity,
      价格: row.price,
      成交额: row.gross_amount,
      费用: row.fee,
      币种: CURRENCY_NAMES[row.currency] || row.currency,
      原因: REASON_NAMES[row.reason] || row.reason,
    })),
    { pageSize: 300, newestFirst: true },
  );
  $("recordTabTrades").textContent = `交易流水（${(trades.trades || []).length}）`;
}

function historyTitle(entry) {
  const assets = (entry.config?.assets || []).filter((asset) => asset.enabled && Number(asset.target_weight) > 0);
  const names = assets.map((asset) => asset.choice_label || SHORT_NAMES[asset.symbol] || asset.name).filter(Boolean);
  return names.slice(0, 2).join(" + ") || "自定义组合";
}

function historyParams(entry) {
  const cfg = entry.config || {};
  const frequency = REBALANCE_FREQUENCY_NAMES[cfg.rebalance_frequency] || cfg.rebalance_frequency || "-";
  const month = cfg.rebalance_frequency === "yearly" ? `（${Number(cfg.annual_rebalance_month || 1)}月）` : "";
  return `${cfg.start_date || "-"} 至 ${cfg.end_date || "-"} · ${frequency}${month}调仓`;
}

function formatHistoryTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "刚刚保存" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function currentHistoryEntry() {
  return archiveEntries().find((entry) => entryRunId(entry) === currentRunId);
}

function archiveEntries() {
  const entriesByRunId = new Map();
  [...runHistory, ...leaderboardHistory].forEach((entry) => {
    const runId = entryRunId(entry);
    if (runId && !entriesByRunId.has(runId)) entriesByRunId.set(runId, entry);
  });
  return [...entriesByRunId.values()];
}

function entryRunId(entry) {
  return entry.runId || entry.run_id;
}

function entryTime(entry) {
  return entry.savedAt || entry.created_at;
}

function historyMetricMarkup(label, value, format, tone) {
  const text = format === "ratio" ? fmtRatio(value) : fmtPct(value);
  return `<span>${escapeHtml(label)}<b class="metric-value is-${escapeHtml(tone)}">${escapeHtml(text)}</b></span>`;
}

function archiveEntryMatches(entry) {
  if (!archiveFilter) return true;
  const haystack = `${historyTitle(entry)} ${historyParams(entry)}`.toLocaleLowerCase("zh-CN");
  return haystack.includes(archiveFilter);
}

function archiveSortValue(entry, mode) {
  const summary = entry.summary || {};
  const metrics = entry.period_metrics || summary;
  if (mode === "annual") return Number(metrics.annualized_return || 0);
  if (mode === "ratio") return annualReturnDrawdownRatio(metrics) ?? Number.NEGATIVE_INFINITY;
  if (mode === "drawdown") return Number(metrics.max_drawdown || 0);
  if (mode === "score") return Number(entry.ranking_score || summary.ranking_score || 0);
  const time = new Date(entryTime(entry)).getTime();
  return Number.isFinite(time) ? time : 0;
}

function filteredArchiveEntries(entries, mode) {
  return entries
    .filter(archiveEntryMatches)
    .map((entry, index) => ({ entry, index }))
    .sort((left, right) => archiveSortValue(right.entry, mode) - archiveSortValue(left.entry, mode) || left.index - right.index)
    .map((item) => item.entry);
}

function renderHistoryComparison() {
  const host = $("historyComparison");
  const periodComparison = activeArchiveView === "leaderboard" && leaderboardPeriodMetadata?.comparable;
  const sourceEntries = periodComparison ? leaderboardHistory : archiveEntries();
  const compared = sourceEntries.find((entry) => entryRunId(entry) === comparisonRunId);
  const current = periodComparison
    ? sourceEntries.find((entry) => entryRunId(entry) === currentRunId)
    : currentHistoryEntry();
  if (!host || !compared || !current || entryRunId(compared) === entryRunId(current)) {
    if (host) host.hidden = true;
    return;
  }
  const summary = current.period_metrics || current.summary || {};
  const baseline = compared.period_metrics || compared.summary || {};
  const annualDelta = Number(summary.annualized_return || 0) - Number(baseline.annualized_return || 0);
  const currentRatio = annualReturnDrawdownRatio(summary);
  const baselineRatio = annualReturnDrawdownRatio(baseline);
  const ratioDelta = currentRatio == null || baselineRatio == null ? null : currentRatio - baselineRatio;
  const drawdownDelta = Number(summary.max_drawdown || 0) - Number(baseline.max_drawdown || 0);
  host.hidden = false;
  host.innerHTML = `<strong>当前结果 vs ${escapeHtml(historyTitle(compared))}</strong><span>${periodComparison ? "同期年化" : "年盈利率"} ${annualDelta >= 0 ? "+" : ""}${fmtPct(annualDelta)} · 年盈利/回撤比 ${ratioDelta == null ? "—" : `${ratioDelta >= 0 ? "+" : ""}${fmtRatio(ratioDelta)}`} · 回撤 ${drawdownDelta >= 0 ? "+" : ""}${fmtPct(drawdownDelta)}</span>`;
}

function renderRunHistory() {
  const host = $("historyList");
  if (!host) return;
  const records = filteredArchiveEntries(runHistory, archiveSortModes.recent);
  if (!records.length) {
    host.innerHTML = `<div class="history-empty">${runHistory.length ? "没有匹配的最近回测。" : "暂无最近回测记录。"}</div>`;
    renderHistoryComparison();
    return;
  }
  host.innerHTML = records.map((entry) => {
    const summary = entry.summary || {};
    const runId = entryRunId(entry);
    const isCurrent = runId === currentRunId;
    const compareLabel = runId === comparisonRunId ? "取消对比" : "对比";
    const ratio = annualReturnDrawdownRatio(summary);
    return `<article class="history-item${isCurrent ? " is-current" : ""}">
      <div class="history-item-header"><strong>${escapeHtml(historyTitle(entry))}${isCurrent ? '<em class="current-badge">当前</em>' : ""}</strong><time>${escapeHtml(formatHistoryTime(entryTime(entry)))}</time></div>
      <div class="history-item-params">${escapeHtml(historyParams(entry))}</div>
      <div class="history-item-metrics">${historyMetricMarkup("年盈利率", summary.annualized_return, "percent", annualReturnTone(summary.annualized_return))}${historyMetricMarkup("年盈利/回撤比", ratio, "ratio", ratioTone(ratio))}${historyMetricMarkup("最大回撤", summary.max_drawdown, "percent", drawdownTone(summary.max_drawdown))}</div>
      <div class="history-item-actions"><button type="button" data-history-compare="${escapeHtml(runId)}">${compareLabel}</button><button type="button" data-history-replay="${escapeHtml(runId)}">查看结果</button><button type="button" class="danger" data-history-delete="${escapeHtml(runId)}">删除</button></div>
    </article>`;
  }).join("");
  host.querySelectorAll("[data-history-compare]").forEach((button) => {
    button.addEventListener("click", () => {
      comparisonRunId = comparisonRunId === button.dataset.historyCompare ? null : button.dataset.historyCompare;
      renderRunHistory();
      renderLeaderboard(leaderboardHistory);
    });
  });
  host.querySelectorAll("[data-history-replay]").forEach((button) => {
    button.addEventListener("click", () => replayHistoryRun(button.dataset.historyReplay));
  });
  host.querySelectorAll("[data-history-delete]").forEach((button) => {
    button.addEventListener("click", () => deleteHistoryRun(button.dataset.historyDelete));
  });
  renderHistoryComparison();
}

function renderLeaderboard(records) {
  const host = $("leaderboardList");
  if (!host) return;
  const displayRecords = filteredArchiveEntries(records, archiveSortModes.leaderboard);
  if (!displayRecords.length) {
    host.innerHTML = `<div class="history-empty">${records.length ? "没有匹配的榜单记录。" : leaderboardPeriodMetadata?.comparable ? "当前区间没有覆盖完整且可比较的策略，请换一个年份或扩大区间。" : "完成回测后会自动进入全局榜单。"}</div>`;
    renderHistoryComparison();
    return;
  }
  host.innerHTML = displayRecords.map((entry) => {
    const summary = entry.summary || {};
    const metrics = entry.period_metrics || summary;
    const isPeriodRanking = Boolean(entry.period_metrics);
    const runId = entryRunId(entry);
    const ratio = annualReturnDrawdownRatio(metrics);
    const isCurrent = runId === currentRunId;
    const compareLabel = runId === comparisonRunId ? "取消对比" : "对比";
    const metricMarkup = isPeriodRanking
      ? `<span>区间收益<b class="metric-value is-${annualReturnTone(metrics.total_return)}">${fmtPct(metrics.total_return)}</b></span><span>区间年化<b class="metric-value is-${annualReturnTone(metrics.annualized_return)}">${fmtPct(metrics.annualized_return)}</b></span><span>最大回撤<b class="metric-value is-${drawdownTone(metrics.max_drawdown)}">${fmtPct(metrics.max_drawdown)}</b></span><span>同期现金超额<b>${fmtPct(metrics.excess_annualized_return)}</b></span><span>正收益月份<b>${fmtPct(metrics.positive_month_ratio)}（${Number(metrics.positive_month_count || 0)}/${Number(metrics.month_count || 0)}）</b></span><span>评价区间<b>${escapeHtml(`${metrics.start_date || "-"} 至 ${metrics.end_date || "-"}`)}</b></span>`
      : `<span>年盈利率<b class="metric-value is-${annualReturnTone(summary.annualized_return)}">${fmtPct(summary.annualized_return)}</b></span><span>年盈利/回撤比<b class="metric-value is-${ratioTone(ratio)}">${fmtRatio(ratio)}</b></span><span>最大回撤<b class="metric-value is-${drawdownTone(summary.max_drawdown)}">${fmtPct(summary.max_drawdown)}</b></span><span>超额年化<b>${fmtPct(summary.excess_annualized_return)}</b></span><span>年度正收益<b>${fmtPct(summary.positive_year_ratio)}（${Number(entry.positive_year_count || summary.positive_year_count || 0)}/${Number(entry.complete_year_count || summary.complete_year_count || 0)}）</b></span><span>回测时间<b>${escapeHtml(`${summary.start_date || entry.config?.start_date || "-"} 至 ${summary.end_date || entry.config?.end_date || "-"}`)}</b></span>`;
    const scoreMarkup = isPeriodRanking
      ? `同期相对评分 ${Number(entry.ranking_score || 0).toFixed(2)} / 100 · 现金基准 ${fmtPct(metrics.repo_annualized_return)} · 样本覆盖 ${fmtPct(metrics.coverage_ratio)}`
      : `综合评分 ${Number(entry.ranking_score || summary.ranking_score || 0).toFixed(2)} / 100 · 逆回购基准 ${fmtPct(summary.repo_annualized_return)}`;
    return `<article class="history-item leaderboard-item${isCurrent ? " is-current" : ""}">
      <div class="history-item-header"><span class="leaderboard-rank">#${Number(entry.rank || 0)}</span><time>${escapeHtml(formatHistoryTime(entryTime(entry)))}</time></div>
      <div class="history-item-params"><strong>${escapeHtml(historyTitle(entry))}</strong><br>${escapeHtml(historyParams(entry))}</div>
      <div class="leaderboard-metrics">${metricMarkup}</div>
      <div class="leaderboard-score">${scoreMarkup}</div>
      <div class="history-item-actions"><button type="button" data-leaderboard-compare="${escapeHtml(runId)}">${compareLabel}</button><button type="button" data-leaderboard-replay="${escapeHtml(runId)}">查看结果</button><button type="button" class="danger" data-leaderboard-delete="${escapeHtml(runId)}">删除</button></div>
    </article>`;
  }).join("");
  host.querySelectorAll("[data-leaderboard-compare]").forEach((button) => {
    button.addEventListener("click", () => {
      comparisonRunId = comparisonRunId === button.dataset.leaderboardCompare ? null : button.dataset.leaderboardCompare;
      renderRunHistory();
      renderLeaderboard(leaderboardHistory);
    });
  });
  host.querySelectorAll("[data-leaderboard-replay]").forEach((button) => {
    button.addEventListener("click", () => replayHistoryRun(button.dataset.leaderboardReplay));
  });
  host.querySelectorAll("[data-leaderboard-delete]").forEach((button) => {
    button.addEventListener("click", () => deleteHistoryRun(button.dataset.leaderboardDelete));
  });
  renderHistoryComparison();
}

function updateArchiveSortControl() {
  const select = $("historySort");
  if (!select) return;
  const options = activeArchiveView === "leaderboard"
    ? [["score", leaderboardPeriodMetadata?.comparable ? "按同期评分" : "按综合评分"], ["annual", leaderboardPeriodMetadata?.comparable ? "按区间年化" : "按年盈利率"], ["ratio", "按盈利回撤比"], ["drawdown", "按最大回撤"]]
    : [["newest", "按最新时间"], ["annual", "按年盈利率"], ["ratio", "按盈利回撤比"], ["drawdown", "按最大回撤"]];
  select.innerHTML = options.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
  select.value = archiveSortModes[activeArchiveView];
}

function leaderboardRequestPath() {
  if (!leaderboardPeriodSelection) return "/api/backtest/leaderboard";
  if (leaderboardPeriodSelection === "all") return "/api/backtest/leaderboard?period=all";
  if (leaderboardPeriodSelection.startsWith("year:")) {
    return `/api/backtest/leaderboard?year=${encodeURIComponent(leaderboardPeriodSelection.slice(5))}`;
  }
  if (leaderboardPeriodSelection === "custom") {
    const startDate = String($("leaderboardStartDate")?.value || "");
    const endDate = String($("leaderboardEndDate")?.value || "");
    return `/api/backtest/leaderboard?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`;
  }
  return "/api/backtest/leaderboard";
}

function syncLeaderboardPeriodControls(payload) {
  leaderboardPeriodMetadata = payload.period || null;
  leaderboardAvailableYears = (payload.available_years || []).map(Number).filter(Number.isFinite);
  const select = $("leaderboardPeriod");
  if (!select) return;
  const options = [
    ...leaderboardAvailableYears.map((year, index) => [`year:${year}`, index === 0 ? `${year}年（最新完整年度）` : `${year}年`]),
    ["all", "各自完整回测期（不可直接横比）"],
    ["custom", "自定义时间区间"],
  ];
  select.innerHTML = options.map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join("");
  if (leaderboardPeriodMetadata?.mode === "year") {
    leaderboardPeriodSelection = `year:${String(leaderboardPeriodMetadata.start_date || "").slice(0, 4)}`;
  } else if (leaderboardPeriodMetadata?.mode === "custom") {
    leaderboardPeriodSelection = "custom";
  } else {
    leaderboardPeriodSelection = "all";
  }
  select.value = leaderboardPeriodSelection;
  const custom = $("leaderboardCustomPeriod");
  if (custom) custom.hidden = leaderboardPeriodSelection !== "custom";
  if (leaderboardPeriodMetadata?.mode === "custom") {
    if ($("leaderboardStartDate")) $("leaderboardStartDate").value = leaderboardPeriodMetadata.start_date || "";
    if ($("leaderboardEndDate")) $("leaderboardEndDate").value = leaderboardPeriodMetadata.end_date || "";
  }
  const meta = $("leaderboardPeriodMeta");
  if (meta) {
    const peerText = leaderboardPeriodMetadata?.comparable ? ` · ${Number(leaderboardPeriodMetadata.peer_count || 0)} 组同期可比` : "";
    meta.textContent = `${leaderboardPeriodMetadata?.label || "榜单"}${peerText} · ${leaderboardPeriodMetadata?.description || ""}`;
  }
}

function selectArchiveView(view) {
  activeArchiveView = view === "leaderboard" ? "leaderboard" : "recent";
  document.querySelectorAll("[data-history-view]").forEach((button) => {
    const active = button.dataset.historyView === activeArchiveView;
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  if ($("historyRecentSection")) $("historyRecentSection").hidden = activeArchiveView !== "recent";
  if ($("leaderboardSection")) $("leaderboardSection").hidden = activeArchiveView !== "leaderboard";
  updateArchiveSortControl();
  renderRunHistory();
  renderLeaderboard(leaderboardHistory);
  if (activeArchiveView === "leaderboard" && !leaderboardArchiveLoaded && !leaderboardArchiveLoading) {
    refreshLeaderboardArchiveSafely();
  }
}

async function refreshRecentArchive() {
  const history = await api("/api/backtest/history");
  const recentRecords = (history.records || []).slice(0, MAX_RUN_HISTORY);
  runHistory = recentRecords;
  recentArchiveLoaded = true;
  const historyRecentMeta = $("historyRecentMeta");
  if (historyRecentMeta) historyRecentMeta.textContent = `数据库最近 ${recentRecords.length} / ${MAX_RUN_HISTORY} 组`;
  if ($("historyRecentTab")) $("historyRecentTab").textContent = `最近回测 ${recentRecords.length}`;
  renderRunHistory();
}

async function refreshLeaderboardArchive() {
  const requestVersion = ++leaderboardRequestVersion;
  leaderboardArchiveLoading = true;
  if ($("leaderboardTab")) $("leaderboardTab").textContent = "全局榜单 加载中";
  try {
    const leaderboard = await api(leaderboardRequestPath());
    if (requestVersion !== leaderboardRequestVersion) return;
    leaderboardHistory = (leaderboard.records || []).slice(0, MAX_LEADERBOARD_RUNS);
    syncLeaderboardPeriodControls(leaderboard);
    updateArchiveSortControl();
    leaderboardArchiveLoaded = true;
  } finally {
    if (requestVersion === leaderboardRequestVersion) leaderboardArchiveLoading = false;
  }
  if (requestVersion !== leaderboardRequestVersion) return;
  if ($("leaderboardTab")) $("leaderboardTab").textContent = `全局榜单 ${leaderboardHistory.length}`;
  renderLeaderboard(leaderboardHistory);
}

async function refreshBacktestArchive({ includeLeaderboard = activeArchiveView === "leaderboard" } = {}) {
  const requests = [refreshRecentArchive()];
  if (includeLeaderboard) requests.push(refreshLeaderboardArchive());
  await Promise.all(requests);
}

async function refreshBacktestArchiveSafely(options = {}) {
  try {
    await refreshBacktestArchive(options);
  } catch (error) {
    console.warn("无法刷新回测归档", error);
  }
}

async function refreshLeaderboardArchiveSafely() {
  try {
    await refreshLeaderboardArchive();
  } catch (error) {
    console.warn("无法刷新全局榜单", error);
    if ($("leaderboardTab")) $("leaderboardTab").textContent = "全局榜单 重试";
  }
}

function scheduleArchiveRefresh(options = {}, delayMs = 80) {
  if (archiveRefreshTimer) window.clearTimeout(archiveRefreshTimer);
  archiveRefreshTimer = window.setTimeout(() => {
    archiveRefreshTimer = null;
    refreshBacktestArchiveSafely(options);
  }, delayMs);
}

async function deleteHistoryRun(runId) {
  if (!runId || !window.confirm("删除这组回测及其榜单记录？此操作不可恢复。")) return;
  try {
    await api(`/api/backtest/${encodeURIComponent(runId)}`, { method: "DELETE", retry: true });
    if (currentRunId === runId) {
      currentRunId = null;
      resetDailyPnlChart();
    }
    if (comparisonRunId === runId) comparisonRunId = null;
    await refreshBacktestArchiveSafely({ includeLeaderboard: true });
    setMessage("回测记录已从数据库删除");
  } catch (error) {
    setMessage(`删除失败：${humanizeError(error.message)}`, true);
  }
}

async function replayHistoryRun(runId) {
  setMessage("正在回放已保存的回测结果...");
  try {
    const chartReady = loadChartLibrary().catch((error) => console.warn(error));
    const entry = await api(`/api/backtest/${encodeURIComponent(runId)}`);
    config = JSON.parse(JSON.stringify(entry.config));
    renderControls();
    resetDailyPnlChart();
    currentRunId = entry.run_id;
    renderSummary(entry.summary);
    const { rebalance, trades } = await loadBacktestResultSections(currentRunId, async (series) => {
      await chartReady;
      renderSummary(deriveSummary(entry.summary, series));
      renderCharts(series);
    });
    renderBacktestRecords(entry.summary, rebalance, trades);
    scheduleArchiveRefresh({ includeLeaderboard: activeArchiveView === "leaderboard" });
    setHistoryPanel(false);
    if (["pending", "running"].includes(entry.summary?.analysis_status)) {
      setMessage("已显示主体结果；滚动窗口与月份对比正在后台补齐");
      watchBacktestAnalysis(currentRunId, rebalance, trades);
    } else {
      setMessage("已回放保存的回测结果");
    }
  } catch (error) {
    setMessage(`回放失败：${humanizeError(error.message)}`, true);
  }
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
    const submittedConfig = compactConfigForRequest(readConfig());
    const chartReady = loadChartLibrary().catch((error) => console.warn(error));
    const job = await api("/api/backtest/start", {
      method: "POST",
      body: JSON.stringify({ config: submittedConfig, client_request_id: createClientRequestId() }),
      retry: true,
      attempts: 5,
      retryDelayMs: 700,
    });
    setMessage(job.message || "回测任务已进入队列");
    const result = await waitForBacktestJob(job.job_id);
    resetDailyPnlChart();
    currentRunId = result.run_id;
    if (result.status) renderStatus(result.status);
    renderSummary(result.summary);
    let finalSummary = result.summary;
    const { rebalance, trades } = await loadBacktestResultSections(currentRunId, async (computedSeries) => {
      await chartReady;
      finalSummary = deriveSummary(result.summary, computedSeries);
      renderSummary(finalSummary);
      renderCharts(computedSeries);
    }, result.chart || null);
    renderBacktestRecords(finalSummary, rebalance, trades);
    scheduleArchiveRefresh({ includeLeaderboard: false });
    const analysisPending = Boolean(result.analysis_pending) || ["pending", "running"].includes(result.summary?.analysis_status);
    if (analysisPending) {
      setMessage("主体结果和图表已显示；滚动窗口与月份对比正在后台补齐");
      watchBacktestAnalysis(currentRunId, rebalance, trades);
    } else if (result.cache?.hit) {
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
  if (open) setHistoryPanel(false);
  document.body.classList.toggle("parameters-open", open);
  [$("parameterToggle"), $("mobileParameterToggle")].filter(Boolean).forEach((button) => {
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
  if (open) window.requestAnimationFrame(() => $("closeParameterPanel")?.focus());
}

function setHistoryPanel(open) {
  if (open) {
    document.body.classList.remove("parameters-open");
    [$('parameterToggle'), $('mobileParameterToggle')].filter(Boolean).forEach((button) => button.setAttribute("aria-expanded", "false"));
  }
  if (isMobileLayout()) {
    document.body.classList.toggle("history-open", open);
  } else {
    document.body.classList.toggle("history-collapsed", !open);
  }
  const expanded = isMobileLayout()
    ? document.body.classList.contains("history-open")
    : !document.body.classList.contains("history-collapsed");
  [$('historyToggle'), $('mobileHistoryToggle')].filter(Boolean).forEach((button) => button.setAttribute("aria-expanded", expanded ? "true" : "false"));
  if (expanded && !recentArchiveLoaded) scheduleArchiveRefresh({ includeLeaderboard: activeArchiveView === "leaderboard" }, 0);
  if (expanded) window.requestAnimationFrame(() => $("closeHistoryPanel")?.focus());
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
  $("historyToggle")?.addEventListener("click", () => setHistoryPanel(document.body.classList.contains("history-collapsed")));
  $("mobileHistoryToggle")?.addEventListener("click", () => setHistoryPanel(true));
  $("closeHistoryPanel")?.addEventListener("click", () => setHistoryPanel(false));
  $("historyBackdrop")?.addEventListener("click", () => setHistoryPanel(false));
  document.querySelectorAll("[data-history-view]").forEach((button) => {
    button.addEventListener("click", () => selectArchiveView(button.dataset.historyView));
  });
  $("historySearch")?.addEventListener("input", (event) => {
    archiveFilter = String(event.target.value || "").trim().toLocaleLowerCase("zh-CN");
    renderRunHistory();
    renderLeaderboard(leaderboardHistory);
  });
  $("historySort")?.addEventListener("change", (event) => {
    archiveSortModes[activeArchiveView] = event.target.value;
    renderRunHistory();
    renderLeaderboard(leaderboardHistory);
  });
  $("leaderboardPeriod")?.addEventListener("change", (event) => {
    leaderboardPeriodSelection = String(event.target.value || "all");
    const custom = $("leaderboardCustomPeriod");
    if (custom) custom.hidden = leaderboardPeriodSelection !== "custom";
    if (leaderboardPeriodSelection === "custom") {
      if ($("leaderboardStartDate") && !$("leaderboardStartDate").value) $("leaderboardStartDate").value = config?.start_date || "";
      if ($("leaderboardEndDate") && !$("leaderboardEndDate").value) $("leaderboardEndDate").value = config?.end_date || "";
      return;
    }
    leaderboardArchiveLoaded = false;
    refreshLeaderboardArchiveSafely();
  });
  $("applyLeaderboardPeriod")?.addEventListener("click", () => {
    const startInput = $("leaderboardStartDate");
    const endInput = $("leaderboardEndDate");
    const startDate = String(startInput?.value || "");
    const endDate = String(endInput?.value || "");
    const valid = Boolean(startDate && endDate && startDate <= endDate);
    [startInput, endInput].filter(Boolean).forEach((input) => input.setCustomValidity(valid ? "" : "请选择有效的开始和结束日期"));
    if (!valid) {
      (startInput?.value ? endInput : startInput)?.reportValidity();
      return;
    }
    leaderboardPeriodSelection = "custom";
    leaderboardArchiveLoaded = false;
    refreshLeaderboardArchiveSafely();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (document.body.classList.contains("parameters-open")) setParameterPanel(false);
    if (document.body.classList.contains("history-open")) setHistoryPanel(false);
  });
  setupTabs("[data-chart-tab]", "chartTab", selectChart);
  document.querySelectorAll("[data-daily-pnl-scale]").forEach((button) => {
    button.addEventListener("click", () => {
      const requestedScale = button.dataset.dailyPnlScale;
      dailyPnlScale = Object.prototype.hasOwnProperty.call(DAILY_PNL_MODES, requestedScale)
        ? requestedScale
        : "amount";
      document.querySelectorAll("[data-daily-pnl-scale]").forEach((option) => {
        option.setAttribute("aria-pressed", option.dataset.dailyPnlScale === dailyPnlScale ? "true" : "false");
      });
      renderDailyPnlChart();
    });
  });
  setupTabs("[data-record-tab]", "recordTab", selectRecordPanel);
  selectChart(activeChartId);
  selectRecordPanel("statusPanel");
  selectArchiveView(activeArchiveView);
}

let backgroundRecoveryTimer = null;

function scheduleBackgroundApiRecovery() {
  if (document.visibilityState === "hidden") return;
  if (backgroundRecoveryTimer) window.clearTimeout(backgroundRecoveryTimer);
  backgroundRecoveryTimer = window.setTimeout(() => {
    backgroundRecoveryTimer = null;
    recoverApiConnection().catch(() => {});
  }, 100);
}

async function init() {
  setupUiInteractions();
  renderRunHistory();
  renderInitialSummary();
  renderTable("rollingTable", [], []);
  renderTable("monthsTable", [], []);
  renderTable("rebalanceTable", [], []);
  renderTable("tradesTable", [], []);
  config = await api("/api/default-config");
  defaultConfigSnapshot = JSON.parse(JSON.stringify(config));
  renderControls();
  $("runBtn").addEventListener("click", runBacktest);
  $("runBtn").addEventListener("pointerenter", () => loadChartLibrary().catch(() => {}), { once: true });
  window.addEventListener("resize", queueChartResize);
  window.addEventListener("online", scheduleBackgroundApiRecovery);
  window.addEventListener("pageshow", scheduleBackgroundApiRecovery);
  document.addEventListener("visibilitychange", scheduleBackgroundApiRecovery);
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
