let config = null;
let defaultConfigSnapshot = null;
let currentRunId = null;
let currentSummary = null;
let currentRunConfig = null;
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
let identityState = null;
let identityGateRequired = false;
let identityGateResolver = null;
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
let assetComovementData = null;
let assetComovementRunId = null;
let assetComovementLoadingRunId = null;
let assetComovementRequestVersion = 0;
let assetComovementWindow = "all";
const strategyDiagnosticsCache = new Map();
let strategyDiagnosticsData = null;
let strategyDiagnosticsRunId = null;
let strategyDiagnosticsLoadingKey = null;
let strategyDiagnosticsRequestVersion = 0;
let strategyDiagnosticsWindow = "all";
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
let toastTimer = null;
let resultRequestVersion = 0;
let runInProgress = false;
let exportRunId = null;
let drawerReturnFocus = null;
let identityReturnFocus = null;
let pendingReplayRunId = null;
let currentChartSeries = [];
let currentRebalanceRecords = [];
let currentTradeRecords = [];
let selectedTradeDate = null;
let eventFocusVersion = 0;
let identityVersion = 0;
let draftLoadVersion = 0;
let favoritesOnly = false;
let recentArchiveRequestVersion = 0;
let metadataEditingRunId = null;
let metadataReturnFocus = null;
let commonComparison = null;
let commonComparisonVersion = 0;
const researchTasks = new Map();
const PERCENT_CONTROL_IDS = ["rebalanceBand", "repoFixedRatio", "dipBuyDrawdown", "dipBuyAssetCapRatio"];


function loadChartLibrary() {
  if (window.echarts) return Promise.resolve(window.echarts);
  if (chartLibraryPromise) return chartLibraryPromise;
  chartLibraryPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `${APP_BASE_PATH}/static/echarts.min.js?v=5.6.0`;
    script.async = true;
    script.onload = () => resolve(window.echarts);
    script.onerror = () => {
      chartLibraryPromise = null;
      script.remove();
      reject(new Error("图表组件加载失败"));
    };
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

const MOBILE_LAYOUT_QUERY = "(max-width: 1100px)";

function isMobileLayout() {
  return window.matchMedia(MOBILE_LAYOUT_QUERY).matches;
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
  if (kind === "calendar") return `${{ CN: "A股", HK: "港股", US: "美股" }[symbol] || symbol}交易日历`;
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
        error.payload = data;
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

function updateIdentityButtons() {
  const hint = identityState?.key_hint || "";
  [$('identityKeyButton'), $('mobileIdentityKeyButton')].filter(Boolean).forEach((button) => {
    button.title = hint ? `身份 Key 已设置（指纹 ${hint}）` : "设置身份 Key";
  });
}

function resetLeaderboardForIdentity() {
  identityVersion += 1;
  draftLoadVersion += 1;
  recentArchiveRequestVersion += 1;
  commonComparisonVersion += 1;
  commonComparison = null;
  researchTasks.clear();
  runHistory = [];
  comparisonRunId = null;
  recentArchiveLoaded = false;
  metadataEditingRunId = null;
  $("metadataDialog")?.close();
  favoritesOnly = false;
  if ($("historyFavoritesOnly")) $("historyFavoritesOnly").checked = false;
  renderRunHistory(); renderResearch();
  refreshRecentArchive().catch((error) => console.warn("无法刷新新身份的记录", error));
  leaderboardRequestVersion += 1;
  leaderboardHistory = [];
  leaderboardArchiveLoaded = false;
  leaderboardArchiveLoading = false;
  leaderboardAvailableYears = [];
  leaderboardPeriodMetadata = null;
  leaderboardPeriodSelection = "";
  if ($("leaderboardTab")) $("leaderboardTab").textContent = "全局榜单";
  renderLeaderboard([]);
  if (activeArchiveView === "leaderboard") refreshLeaderboardArchiveSafely();
}

function closeIdentityGate() {
  const gate = $("identityGate");
  if (gate) gate.hidden = true;
  document.body.classList.remove("identity-locked");
  document.querySelector(".app-shell")?.removeAttribute("inert");
  document.querySelector(".mobile-app-bar")?.removeAttribute("inert");
  syncDrawerAccessibility();
  identityReturnFocus?.focus();
  const resolve = identityGateResolver;
  identityGateResolver = null;
  identityGateRequired = false;
  if (resolve) resolve();
}

function showIdentityGate(required = false) {
  identityReturnFocus = document.activeElement;
  identityGateRequired = required;
  const gate = $("identityGate");
  const form = $("identityForm");
  const input = $("identityKeyInput");
  const error = $("identityError");
  if (form) form.reset();
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
  if ($("identityCancel")) $("identityCancel").hidden = required;
  if (gate) gate.hidden = false;
  document.body.classList.add("identity-locked");
  document.querySelector(".app-shell")?.setAttribute("inert", "");
  document.querySelector(".mobile-app-bar")?.setAttribute("inert", "");
  window.requestAnimationFrame(() => input?.focus());
  return new Promise((resolve) => {
    identityGateResolver = resolve;
  });
}

async function saveIdentityKey(event) {
  event.preventDefault();
  const input = $("identityKeyInput");
  const error = $("identityError");
  const submit = $("identitySubmit");
  const key = String(input?.value ?? "");
  if (!key.length) {
    input?.setCustomValidity("请输入至少 1 个字符");
    input?.reportValidity();
    return;
  }
  input?.setCustomValidity("");
  if (submit) {
    submit.disabled = true;
    submit.textContent = "正在保存";
  }
  try {
    const hadIdentity = Boolean(identityState?.configured);
    identityState = await api("/api/identity", {
      method: "POST",
      body: JSON.stringify({ key }),
    });
    updateIdentityButtons();
    closeIdentityGate();
    if (hadIdentity) resetLeaderboardForIdentity();
  } catch (saveError) {
    if (error) {
      error.textContent = humanizeError(saveError.message);
      error.hidden = false;
    }
    input?.focus();
  } finally {
    if (submit) {
      submit.disabled = false;
      submit.textContent = "保存并进入";
    }
  }
}

async function ensureIdentity() {
  identityState = await api("/api/identity");
  updateIdentityButtons();
  if (!identityState.configured) await showIdentityGate(true);
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
  [$("message"), $("workspaceMessage")].filter(Boolean).forEach((message) => {
    message.textContent = text || "";
    message.classList.toggle("error", isError);
  });
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
  next.monthly_spend_annual_growth = Number($("monthlySpendAnnualGrowth")?.value || 0) / 100;
  next.repo_target_mode = $("repoTargetMode").value;
  next.repo_fixed_target_cny = Number($("repoFixedTarget").value);
  next.repo_fixed_target_ratio = Number($("repoFixedRatio").value);
  next.repo_symbol = $("repoSymbol").value;
  next.dip_buy_enabled = next.rebalance_frequency === "yearly" && $("dipBuyEnabled").checked;
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

function configFingerprint(value) {
  if (!value) return "";
  // Compare editable values only; catalogue descriptions and server metadata
  // may change without changing a strategy.
  const scalarKeys = ["initial_capital_cny", "start_date", "end_date", "rebalance_frequency",
    "annual_rebalance_month", "rolling_window_years", "rebalance_month_analysis_enabled",
    "rebalance_band", "rebalance_to_target", "monthly_spend_cny", "monthly_spend_annual_growth", "repo_target_mode",
    "repo_fixed_target_cny", "repo_fixed_target_ratio", "repo_symbol", "dip_buy_enabled",
    "dip_buy_drawdown", "dip_buy_total_parts", "dip_buy_level_mode", "dip_buy_cost_basis_mode",
    "dip_buy_recovery_sell_enabled", "dip_buy_asset_cap_enabled", "dip_buy_asset_cap_ratio",
    "dip_buy_blackout_enabled", "dip_buy_blackout_months"];
  const normalized = Object.fromEntries(scalarKeys.map((key) => [key, value[key]]));
  normalized.monthly_spend_annual_growth = Number(value.monthly_spend_annual_growth || 0);
  if (value.rebalance_frequency !== "yearly") {
    normalized.dip_buy_enabled = false;
    normalized.rebalance_month_analysis_enabled = false;
  }
  normalized.assets = (value.assets || []).map(({ key, enabled, target_weight }) => ({ key, enabled, target_weight }));
  normalized.fees = value.fees;
  const stable = (item) => Array.isArray(item) ? item.map(stable)
    : item && typeof item === "object" ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, stable(item[key])])) : item;
  return JSON.stringify(stable(normalized));
}

function updateResultContext() {
  if (!config) return;
  const dirty = Boolean(currentRunConfig && configFingerprint(readConfig()) !== configFingerprint(currentRunConfig));
  if ($("draftNotice")) $("draftNotice").hidden = !dirty;
  if ($("resultState")) $("resultState").textContent = runInProgress ? "正在回测" : !currentRunId ? "等待运行" : dirty ? "参数待应用" : "已保存结果";
  if ($("resultConfigSummary")) {
    const cfg = currentRunConfig;
    $("resultConfigSummary").textContent = cfg
      ? `${currentSummary?.start_date || cfg.start_date} 至 ${currentSummary?.end_date || cfg.end_date} · ${REBALANCE_FREQUENCY_NAMES[cfg.rebalance_frequency] || cfg.rebalance_frequency}检查 · 容忍带 ${fmtPct(cfg.rebalance_band)} · ${cfg.rebalance_to_target ? "超带后恢复目标" : "超带后调回带内"}`
      : "设置资金与资产配置，运行后在这里查看结果。";
  }
  if ($("openCsvExport")) $("openCsvExport").disabled = !currentRunId;
  renderResearch();
}

function updateRebalanceExplanation() {
  const host = $("rebalanceExplanation");
  if (!host) return;
  const summary = currentSummary;
  host.hidden = !currentRunId;
  host.textContent = summary?.rebalance_check_count != null
    ? `周期检查 ${summary.rebalance_check_count} 次 · 实际调仓 ${summary.rebalance_trade_count} 次 · 带内无需调仓 ${summary.rebalance_within_band_count ?? 0} 次 · 成交条件不足 ${summary.rebalance_constrained_count ?? 0} 次。首次建仓单独计算；带内无需调仓记录已隐藏。`
    : currentRunId ? "此历史版本未区分周期检查与实际成交，请重新运行以查看调仓原因。" : "运行后将分别显示周期检查与实际调仓次数。";
}

function setRunBusy(busy) {
  runInProgress = busy;
  ["runBtn", "mobileRunBtn", "workspaceRunBtn"].forEach((id) => {
    const button = $(id);
    if (!button) return;
    button.disabled = busy || !config;
    button.classList.toggle("is-running", busy);
    const label = button.querySelector("span") || button;
    label.textContent = busy ? "正在回测" : "运行回测";
  });
  updateResultContext();
}

function focusInvalidControl(input, message) {
  if (isMobileLayout()) setParameterPanel(true);
  let parent = input?.parentElement;
  while (parent && parent !== $("parameterPanel")) {
    if (parent.tagName === "DETAILS") parent.open = true;
    parent = parent.parentElement;
  }
  setMessage(message || input?.validationMessage || "请检查参数", true);
  window.requestAnimationFrame(() => { input?.focus(); input?.reportValidity(); });
  return false;
}

function validateControls() {
  for (const id of PERCENT_CONTROL_IDS) {
    const input = $(`${id}Percent`);
    if (input && !input.disabled && !($("repoFixedControls")?.hidden && id === "repoFixedRatio") && !validPercentInput(input)) return focusInvalidControl(input);
  }
  for (const input of document.querySelectorAll('#parameterPanel input[type="number"], #parameterPanel input[type="date"]')) {
    input.setCustomValidity("");
    if (input.disabled) continue;
    if (input.closest("#repoFixedControls") && $("repoFixedControls")?.hidden) continue;
    const asset = input.closest(".asset-control");
    if (asset && !asset.querySelector('input[type="checkbox"]')?.checked) continue;
    if (input.type === "number" && (!input.value || !Number.isFinite(Number(input.value)))) {
      input.setCustomValidity("请输入数值；不启用的金额请填 0");
    }
    if (input.type === "number" && input.value && ((input.min !== "" && Number(input.value) < Number(input.min)) || (input.max !== "" && Number(input.value) > Number(input.max)))) input.setCustomValidity("数值超出允许范围");
    if (!input.checkValidity()) return focusInvalidControl(input);
  }
  const next = readConfig();
  if (next.start_date > next.end_date) return focusInvalidControl($("endDate"), "结束日期不能早于开始日期");
  const total = next.assets.reduce((sum, asset) => sum + (asset.enabled ? asset.target_weight : 0), 0);
  if (next.repo_target_mode === "residual_weight" && total > 1 + 1e-8) {
    return focusInvalidControl($("assetControls")?.querySelector('input[type="number"]'), `资产权重合计 ${fmtPct(total)}，不能超过 100%`);
  }
  return true;
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
    const repoTargetValue = enabledWeight <= 0 ? initialCapital : initialCapital > 0 ? Math.min(fixedAmount + initialCapital * fixedRatio, initialCapital) : 0;
    const repoWeight = initialCapital > 0 ? repoTargetValue / initialCapital : 1;
    return {
      mode,
      enabledWeight,
      repoTargetValue,
      repoWeight,
      remainingWeight: Math.max(1 - repoWeight, 0),
      warning: false,
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
    $("startDate").value = (defaultConfigSnapshot || config).start_date;
    $("endDate").value = (defaultConfigSnapshot || config).end_date;
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
  PERCENT_CONTROL_IDS.forEach((id) => { if ($(`${id}Percent`)) $(`${id}Percent`).disabled = Boolean($(id)?.disabled); });
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
  const frequency = REBALANCE_FREQUENCY_NAMES[$("rebalanceFrequency")?.value] || "每年";
  if ($("rebalanceRuleHint")) $("rebalanceRuleHint").textContent = `${frequency}检查权重；只有超出 ${fmtPct($("rebalanceBand").value)} 相对容忍带才调仓。首次建仓按目标配置。修改后需重新运行；频率提高不保证每期都有成交。`;
  updateResultContext();
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
    const normalized = Math.min(Math.max(Number(percent.value || 0), 0), 100) / 100;
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
  if ($("monthlySpendAnnualGrowth")) $("monthlySpendAnnualGrowth").value = Number(config.monthly_spend_annual_growth || 0) * 100;
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
  ["initialCapital", "startDate", "endDate", "monthlySpend", "monthlySpendAnnualGrowth", "rebalanceFrequency", "annualRebalanceMonth", "rollingWindowYears", "rebalanceMonthAnalysisEnabled", "rebalanceToTarget", "repoTargetMode", "repoFixedTarget", "repoFixedRatio", "dipBuyEnabled", "dipBuyDrawdown", "dipBuyTotalParts", "dipBuyLevelMode", "dipBuyCostBasisMode", "dipBuyRecoverySellEnabled", "dipBuyAssetCapEnabled", "dipBuyAssetCapRatio", "dipBuyBlackoutEnabled", "dipBuyBlackoutMonths"].forEach((id) => {
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
      <input id="weight_${asset.key}" type="range" min="0" max="1" step="any" value="${asset.target_weight}" aria-label="${assetName(asset.symbol)}目标权重" />
      <label class="asset-percent"><input id="weight_percent_${asset.key}" type="number" min="0" max="100" step="any" value="${Number(asset.target_weight || 0) * 100}" /><span>%</span></label>
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
  PERCENT_CONTROL_IDS.forEach((id) => bindPercentControl(id, listenerOptions));
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
    <input id="weight_${BROAD_ETF_CONTROL_KEY}" type="range" min="0" max="1" step="any" value="${weight}" aria-label="宽基ETF目标权重" />
    <label class="asset-percent"><input id="weight_percent_${BROAD_ETF_CONTROL_KEY}" type="number" min="0" max="100" step="any" value="${Number(weight || 0) * 100}" /><span>%</span></label>
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
    <input id="weight_${SP500_CONTROL_KEY}" type="range" min="0" max="1" step="any" value="${weight}" aria-label="标普500目标权重" />
    <label class="asset-percent"><input id="weight_percent_${SP500_CONTROL_KEY}" type="number" min="0" max="100" step="any" value="${Number(weight || 0) * 100}" /><span>%</span></label>
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
  updateDataStatus(rows);
}

function updateDataStatus(rows) {
  const statusText = $("dataStatusText");
  const mobileStatus = $("mobileDataStatus");
  const statusDot = $("dataStatusDot");
  const validDates = rows.map((row) => row.end_date).filter(Boolean).sort();
  const latestDate = validDates.at(-1);
  const text = rows.length
    ? `${rows.length} 项已缓存${latestDate ? ` · 最晚日期 ${latestDate}` : ""} · 运行时核验所需区间`
    : "暂无可用数据";
  if (statusText) statusText.textContent = text;
  if (mobileStatus) mobileStatus.textContent = rows.length ? `缓存至 ${latestDate || "未知日期"}` : "暂无可用数据";
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
    <details class="summary-more"><summary>费用、现金流与交易统计</summary><div class="metric-group metric-group-secondary">${secondary.map(metricMarkup).join("")}</div></details>
    ${currentSummary?.requires_recalculation ? '<p class="message error">历史结果使用旧版计算逻辑，请重新运行。</p>' : ""}
    ${noteList.length ? '<details class="summary-methodology"><summary>收益口径与数据来源</summary>' : ""}
    ${noteList.map((note) => `<div class="summary-note" role="note">
      <span class="summary-note-icon" aria-hidden="true">i</span>
      <span>${escapeHtml(note.text)}</span>
      ${note.href ? `<a href="${escapeHtml(note.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(note.linkText || "查看依据")}</a>` : ""}
    </div>`).join("")}${noteList.length ? "</details>" : ""}
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

function captureReturnMarkup(value) {
  if (value == null || !Number.isFinite(Number(value))) return '<strong class="is-unavailable">—</strong>';
  const number = Number(value);
  const tone = number > 0 ? "gain" : number < 0 ? "loss" : "flat";
  const amount = Math.abs(number) * 100;
  const text = amount > 0 && amount < 0.0001 ? "<0.0001" : amount.toFixed(amount > 0 && amount < 0.01 ? 4 : 2);
  return `<strong class="is-${tone}">${number > 0 ? "+" : number < 0 ? "−" : ""}${text}%</strong>`;
}

function captureScenarioMarkup(side, summary) {
  const strategy = summary[`${side}_market_strategy_monthly_return`];
  const benchmark = summary[`${side}_market_benchmark_monthly_return`];
  const months = Number(summary[`${side}_market_months`] || 0);
  const available = months > 0 && strategy != null && benchmark != null && Number.isFinite(Number(strategy)) && Number.isFinite(Number(benchmark));
  const rising = side === "up";
  let conclusion = months > 0 ? "该组收益明细暂不可用" : "暂无可比较的月度区间";
  if (available) {
    const s = Number(strategy), b = Number(benchmark);
    if (Math.abs(s) <= 1e-12) conclusion = rising ? "大盘涨，组合基本持平" : "大盘跌，组合基本持平";
    else if (rising) conclusion = s < 0 ? "大盘涨，组合反而下跌" : Math.abs(s - b) <= 1e-12 ? "上涨幅度与大盘一致" : s > b ? "上涨幅度超过大盘" : "组合跟涨，幅度小于大盘";
    else conclusion = s > 0 ? "大盘跌，组合仍上涨" : Math.abs(s - b) <= 1e-12 ? "跌幅与大盘一致" : s > b ? "组合跌得比大盘少" : "组合跌得比大盘多";
  }
  return `<section class="capture-item" aria-label="沪深300${rising ? "上涨" : "下跌"}时">
    <div class="capture-scenario-heading"><h3>沪深300${rising ? "上涨" : "下跌"}时</h3><span>${months} 个区间</span></div>
    <p class="capture-conclusion">${conclusion}</p>
    <div class="capture-returns"><div><span>组合月均收益</span>${captureReturnMarkup(available ? strategy : null)}</div><div><span>沪深300月均收益</span>${captureReturnMarkup(available ? benchmark : null)}</div></div>
    ${months > 0 && months < 12 ? '<small class="capture-sample-note">样本少于 12 个区间，仅供参考</small>' : ""}
  </section>`;
}

function captureUpDaysMarkup(summary) {
  const total = summary.common_observation_days;
  const strategy = summary.strategy_up_days;
  const benchmark = summary.benchmark_up_days;
  const available = Number.isInteger(total) && total > 0 && [strategy, benchmark].every((count) => Number.isInteger(count) && count >= 0 && count <= total);
  const row = (label, count, className) => `<div class="capture-up-days-row ${className}"><span>${label}</span><strong>${available ? `${count} 天` : "—"}</strong><span class="up-days-ratio">${available ? fmtPct(count / total) : "—"}</span><div class="up-days-track" aria-hidden="true"><i style="width:${available ? (count / total * 100).toFixed(2) : 0}%"></i></div></div>`;
  const difference = available ? strategy - benchmark : 0;
  const comparison = difference > 0 ? `策略多 ${difference} 天上涨` : difference < 0 ? `策略少 ${Math.abs(difference)} 天上涨` : "两者上涨天数相同";
  return `<section class="capture-up-days" aria-label="总上涨天数对比"><div class="capture-scenario-heading"><h3>总上涨天数</h3><span>${available ? `共同统计 ${total} 个交易日` : "暂无共同交易日数据"}</span></div>${row("策略", strategy, "is-strategy")}${row("沪深300", benchmark, "is-benchmark")}<p>${available ? `${comparison} · 右侧为上涨天数占比` : "需要至少两个相邻且有有效行情的交易日"}</p></section>`;
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
      <div class="capture-card-heading"><div><span>大盘涨跌时，组合表现如何？</span><small>比较上涨天数，再看大盘涨跌月份中的月均收益（几何平均）</small></div></div>
      ${captureUpDaysMarkup(summary)}
      <div class="capture-grid">
        ${captureScenarioMarkup("up", summary)}
        ${captureScenarioMarkup("down", summary)}
      </div>
      <details class="capture-methodology"><summary>查看捕获率与计算说明</summary>
        <dl><div><dt>上涨捕获率</dt><dd>${summary.upside_capture_ratio == null ? "—" : fmtPct(summary.upside_capture_ratio)}</dd></div><div><dt>下跌捕获率</dt><dd>${summary.downside_capture_ratio == null ? "—" : fmtPct(summary.downside_capture_ratio)}</dd></div></dl>
        <p>月均收益：将同组各月按复利合并，再折算为平均每月收益。月份可能不连续，各月表现也可能不同，这不是整个回测的累计收益。</p>
        <p>捕获率 = 同组组合年化收益 ÷ 同组沪深300年化收益。100% 表示两者同组年化收益相同；不是组合赚了 100%。</p>
        <p>上涨捕获率为负，表示大盘上涨的这些区间内组合总体亏损；下跌捕获率为负，表示大盘下跌时组合总体盈利。下跌捕获率接近 0，表示组合在这些区间内接近持平。</p>
        <p>按月末分段，首月只作为起点；末月截至回测结束日，可能不足整月。沪深300持平的区间不计入两组。</p>
        <p>上涨天数按相邻有效交易日统计，首日及行情缺失的区间不计，两者使用相同样本。策略日收益剔除外部入金和消费，包含费用影响；天数占比不等于收益率。</p>
      </details>
    </article>`;
}

function renderInitialSummary() {
  currentSummary = null;
  renderSummaryGroups(
    ["期末总资产", "累计收益率", "年化收益率", "最大回撤"].map((label) => ({ label, value: "--" })),
    ["原始本金折算年化", "原始本金累计盈亏", "总手续费", "实际到账现金分红", "总消费", "浮盈浮亏", "对比期末资产", "实际调仓次数", "交易次数", "分红预扣税"]
      .map((label) => ({ label, value: "--" })),
  );
  renderRiskInsights();
  updateRebalanceExplanation();
  updateResultContext();
}

function renderSummary(summary) {
  currentSummary = summary;
  const positiveTone = (value) => Number(value || 0) >= 0 ? "positive" : "negative";
  const optionalPct = (value) => value == null || !Number.isFinite(Number(value)) ? "—" : fmtPct(value);
  const originalProfitValue = summary.net_profit_cny == null
    ? "—"
    : `￥${fmtMoney(summary.net_profit_cny)} / ${optionalPct(summary.original_capital_return)}`;
  const hasDividendLowVol = Boolean((currentRunConfig || config)?.assets?.some(
    (asset) => asset.symbol === "512890.SH" && asset.enabled && Number(asset.target_weight || 0) > 0,
  ));
  const primary = [
    { label: "期末总资产", value: `￥${fmtMoney(summary.final_asset_cny)}` },
    { label: "累计收益率", value: fmtPct(summary.total_return), tone: positiveTone(summary.total_return) },
    { label: "年化收益率", value: fmtPct(summary.annualized_return), tone: positiveTone(summary.annualized_return) },
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
    { label: "实际调仓次数", value: summary.rebalance_trade_count == null ? "—" : fmtNum(summary.rebalance_trade_count, 0) },
    { label: "交易次数", value: fmtNum(summary.trade_count, 0) },
    { label: "分红预扣税", value: `￥${fmtMoney(summary.withheld_tax_cny)}` },
  ];
  const dividendNote = hasDividendLowVol ? {
    text: "512890 说明：现金分红只统计 ETF 向持有人实际派发的现金。官方年报确认该 ETF 在 2023—2025 年未实施利润分配；成分股股息留在基金内，并已反映在净值和价格中。007466 等联接基金的分红不属于 512890。",
    href: "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-31/512890_20260331_5BME.pdf",
    linkText: "查看上交所年报",
  } : null;
  const returnBasisNote = {
    text: "收益口径：现金流调整收益先用（当日总资产－昨日总资产－当日外部净流入）÷昨日总资产计算每日收益，再复利与年化；不是始终拿原始本金作分母。原始本金折算口径把消费等外部净流出加回期末资产后，再对原始本金计算，未按每笔现金流发生时间加权。再平衡表的当年总资产取收益年度决策日总资产；当年盈亏为当年资产变动剔除外部收支，并分别用上年度总资产和原始资金作分母展示当年收益。",
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
  const recalculationNote = summary.requires_recalculation ? {
    text: "此历史结果使用旧版计算逻辑，尚未重算，已退出当前排行榜。请重新运行相同参数。",
  } : null;
  renderSummaryGroups(primary, secondary, [recalculationNote, returnBasisNote, treasuryNote, dividendNote]);
  if (Number(summary.total_spend_shortfall_cny) > 0) {
    const warning = document.createElement("p");
    warning.className = "message error";
    warning.textContent = `消费资金不足：计划 ￥${fmtMoney(summary.total_planned_spend_cny)}，实际提取 ￥${fmtMoney(summary.total_spend_cny)}，缺口 ￥${fmtMoney(summary.total_spend_shortfall_cny)}（${summary.spend_shortfall_count} 次，首次 ${summary.first_spend_shortfall_date}）。`;
    $("summaryGrid").appendChild(warning);
  }
  renderRiskInsights(summary);
  updateRebalanceExplanation();
  updateResultContext();
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

  return {
    ...summary,
    final_asset_cny: summary.final_asset_cny ?? last.total_asset_cny,
    total_return: summary.total_return ?? last.cumulative_return,
    max_drawdown: summary.max_drawdown ?? Math.min(...series.map((row) => row.drawdown ?? 0)),
    comparison_final_asset_cny: last.payload?.comparison?.total_asset_cny ?? summary.comparison_final_asset_cny,
    worst_year: summary.worst_year ?? calendarRisk.worst_year,
    worst_half_year: summary.worst_half_year ?? calendarRisk.worst_half_year,
    drawdown_recovery: summary.drawdown_recovery ?? recovery,
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
  document.querySelector(".analysis-panel")?.classList.toggle("is-diagnostics-active", chartId === "strategyDiagnosticsChart");
  document.querySelectorAll("[data-chart-tab]").forEach((button) => {
    const active = button.dataset.chartTab === chartId;
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".chart-view").forEach((view) => {
    view.hidden = view.querySelector(".chart")?.id !== chartId;
  });
  if (chartId === "dailyPnlChart") loadDailyPnlChart().catch(() => {});
  if (chartId === "strategyDiagnosticsChart") loadStrategyDiagnostics().catch(() => {});
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

function showToast(text, isError = false) {
  const toast = $("toast");
  if (!toast) {
    setMessage(text, isError);
    return;
  }
  if (toastTimer) window.clearTimeout(toastTimer);
  toast.textContent = text || "";
  toast.className = isError ? "toast error" : "toast";
  toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    toast.hidden = true;
    toastTimer = null;
  }, 5000);
}

function resetStrategyDiagnostics() {
  strategyDiagnosticsRequestVersion += 1;
  strategyDiagnosticsCache.clear();
  strategyDiagnosticsData = null;
  strategyDiagnosticsRunId = null;
  strategyDiagnosticsLoadingKey = null;
  strategyDiagnosticsWindow = "all";
  const windowSelect = $("strategyDiagnosticsWindow");
  if (windowSelect) windowSelect.value = "all";
  delete pendingChartOptions.strategyDiagnosticsChart;
  charts.strategyDiagnosticsChart?.clear();
  const content = $("strategyDiagnosticsContent");
  if (content) content.hidden = true;
  const loading = $("strategyDiagnosticsLoading");
  if (loading) {
    loading.hidden = false;
    loading.textContent = "运行回测后查看策略诊断";
  }
  const range = $("strategyDiagnosticsRange");
  if (range) range.textContent = "运行回测后生成真实反事实诊断";
}

function diagnosticDeltaMarkup(value, kind = "percent") {
  if (value == null || !Number.isFinite(Number(value))) return '<span class="diagnostic-delta is-neutral">—</span>';
  const number = Number(value);
  const text = kind === "ratio" ? `${number > 0 ? "+" : ""}${number.toFixed(2)}` : fmtSignedPct(number);
  const tone = number > 1e-12 ? "good" : number < -1e-12 ? "bad" : "neutral";
  return `<span class="diagnostic-delta is-${tone}">${escapeHtml(text)}</span>`;
}

function diagnosticWeightText(weights = {}) {
  return Object.entries(weights)
    .filter(([, weight]) => Number(weight || 0) > 1e-12)
    .map(([symbol, weight]) => `${SHORT_NAMES[symbol] || assetName(symbol)} ${fmtPct(weight)}`)
    .join(" · ");
}

function renderStrategyOptimizationChart(data) {
  const host = $("strategyDiagnosticsChart");
  if (!host) return;
  const candidates = data.optimization_candidates || [];
  const recommendedId = data.recommendation?.candidate_id;
  if (!candidates.length) {
    host.innerHTML = '<div class="strategy-diagnostics-loading">没有可比较的权重候选</div>';
    return;
  }
  if (!window.echarts) {
    host.innerHTML = `<div class="optimization-fallback">${candidates
      .slice()
      .sort((a, b) => Number(b.annual_return_drawdown_ratio || 0) - Number(a.annual_return_drawdown_ratio || 0))
      .slice(0, 6)
      .map((row) => `<div class="${row.id === recommendedId ? "is-recommended" : ""}"><strong>${escapeHtml(row.label)}</strong><span>年化 ${fmtPct(row.annualized_return)} · 回撤 ${fmtPct(row.max_drawdown)} · 比值 ${fmtRatio(row.annual_return_drawdown_ratio)}</span></div>`)
      .join("")}</div>`;
    return;
  }
  const points = candidates.map((row) => {
    const recommended = row.id === recommendedId;
    return {
      value: [Math.abs(Number(row.max_drawdown || 0)) * 100, Number(row.annualized_return || 0) * 100],
      name: row.label,
      row,
      symbolSize: row.current || recommended ? 16 : 9,
      itemStyle: { color: row.current ? "#087a55" : recommended ? "#b97918" : "#90a0a6", opacity: row.current || recommended ? 1 : 0.72 },
      label: { show: row.current || recommended, formatter: row.current ? "当前" : "建议复核", position: "top", color: row.current ? "#087a55" : "#8a5c12", fontWeight: 700 },
    };
  });
  queueChartOption("strategyDiagnosticsChart", {
    animationDuration: 420,
    grid: { left: 54, right: 24, top: 34, bottom: 52 },
    tooltip: {
      trigger: "item",
      formatter: (params) => {
        const row = params.data.row;
        return [
          `<div style="font-weight:700;margin-bottom:6px">${escapeHtml(row.label)}</div>`,
          `<div>年化收益 <strong>${fmtPct(row.annualized_return)}</strong></div>`,
          `<div>最大回撤 <strong>${fmtPct(row.max_drawdown)}</strong></div>`,
          `<div>收益/回撤 <strong>${fmtRatio(row.annual_return_drawdown_ratio)}</strong></div>`,
          `<div style="max-width:360px;margin-top:6px;color:#cbd5d8">${escapeHtml(diagnosticWeightText(row.weights))}</div>`,
        ].join("");
      },
    },
    xAxis: { type: "value", name: "最大回撤绝对值（越左越稳）", nameLocation: "middle", nameGap: 32, axisLabel: { formatter: "{value}%" }, splitLine: { lineStyle: { color: "#edf1f2" } } },
    yAxis: { type: "value", name: "年化收益", axisLabel: { formatter: "{value}%" }, splitLine: { lineStyle: { color: "#edf1f2" } } },
    series: [{ type: "scatter", data: points, emphasis: { focus: "self", scale: 1.2 } }],
  });
  if (activeChartId === "strategyDiagnosticsChart") {
    applyChartOption("strategyDiagnosticsChart");
    charts.strategyDiagnosticsChart?.resize();
  }
}

function renderStrategyDiagnostics() {
  const data = strategyDiagnosticsData;
  if (!data?.available) return;
  const base = data.base || {};
  const loading = $("strategyDiagnosticsLoading");
  if (loading) loading.hidden = true;
  const content = $("strategyDiagnosticsContent");
  if (content) content.hidden = false;
  if ($("diagnosticAnnualized")) $("diagnosticAnnualized").textContent = fmtPct(base.annualized_return);
  if ($("diagnosticDrawdown")) $("diagnosticDrawdown").textContent = fmtPct(base.max_drawdown);
  if ($("diagnosticCalmar")) $("diagnosticCalmar").textContent = fmtRatio(base.annual_return_drawdown_ratio);
  if ($("diagnosticPositiveYears")) $("diagnosticPositiveYears").textContent = `${Number(base.positive_year_count || 0)} / ${Number(base.complete_year_count || 0)}`;
  if ($("strategyDiagnosticsRange")) {
    $("strategyDiagnosticsRange").textContent = `${data.window.start_date} 至 ${data.window.end_date} · ${data.window.label} · ${data.optimization_candidates.length}组真实权重回测`;
  }

  const effects = data.asset_effects || [];
  const redundant = effects.filter((row) => row.conclusion === "可能冗余");
  const core = effects.filter((row) => row.conclusion === "核心贡献");
  const verdictTitle = redundant.length
    ? `发现 ${redundant.length} 个可能冗余的标的，需要优先复核`
    : core.length
      ? `组合有效，且有 ${core.length} 个标的同时改善收益与回撤`
      : "组合有价值，但标的作用存在明确的收益与保护取舍";
  if ($("strategyVerdictTitle")) $("strategyVerdictTitle").textContent = verdictTitle;
  if ($("strategyVerdictText")) {
    $("strategyVerdictText").textContent = "判断来自删除标的后的完整重跑，而不是只看相关性。回撤变化为正表示删除后回撤改善，为负表示删除后风险变差。";
  }
  if ($("strategyRecommendation")) {
    $("strategyRecommendation").innerHTML = `<span>本窗口建议</span>${escapeHtml(data.recommendation?.text || "保持当前权重")}<small>${escapeHtml(diagnosticWeightText(data.recommendation?.weights || {}))}</small>`;
  }
  if ($("strategyAssetEffects")) {
    $("strategyAssetEffects").innerHTML = effects.map((row) => `
      <tr title="${escapeHtml(row.explanation)}">
        <td><strong>${escapeHtml(row.removed_name)}</strong><small>${escapeHtml(row.removed_symbol)} · 原权重 ${fmtPct(row.removed_weight)}</small></td>
        <td>${diagnosticDeltaMarkup(row.annualized_return_delta)}</td>
        <td>${diagnosticDeltaMarkup(row.max_drawdown_delta)}</td>
        <td>${diagnosticDeltaMarkup(row.annual_return_drawdown_ratio_delta, "ratio")}</td>
        <td><span class="diagnostic-conclusion is-${row.conclusion === "可能冗余" ? "bad" : row.conclusion === "核心贡献" ? "good" : "neutral"}">${escapeHtml(row.conclusion)}</span></td>
      </tr>`).join("");
  }

  const stress = data.stress_protection || {};
  if ($("stressProtectionCaption")) {
    $("stressProtectionCaption").textContent = `${stress.comparable_periods || 0}个月中有 ${stress.stress_periods || 0}个红利下跌月；保护率表示该月标的总收益大于0。`;
  }
  if ($("stressProtectionGrid")) {
    $("stressProtectionGrid").innerHTML = (stress.assets || []).map((asset) => {
      const isRisk = asset.role === "risk_asset";
      const rate = asset.stress_positive_rate;
      return `<article class="stress-protection-card ${isRisk ? "is-risk" : ""}">
        <span>${escapeHtml(asset.label)}</span>
        <strong>${isRisk ? `${stress.stress_periods || 0}个下跌月` : rate == null ? "—" : fmtPct(rate)}</strong>
        <small>${isRisk ? "压力基准" : `压力期平均 ${fmtSignedPct(asset.stress_average_return)}`}</small>
        <em>最差10%月份为正 ${asset.worst_decile_positive_rate == null ? "—" : fmtPct(asset.worst_decile_positive_rate)}</em>
      </article>`;
    }).join("");
  }
  renderStrategyOptimizationChart(data);
}

async function loadStrategyDiagnostics() {
  const runId = currentRunId;
  if (!runId) {
    const loading = $("strategyDiagnosticsLoading");
    if (loading) {
      loading.hidden = false;
      loading.textContent = "运行回测后查看策略诊断";
    }
    return;
  }
  const cacheKey = `${runId}:${strategyDiagnosticsWindow}`;
  if (strategyDiagnosticsCache.has(cacheKey)) {
    strategyDiagnosticsRunId = runId;
    strategyDiagnosticsData = strategyDiagnosticsCache.get(cacheKey);
    renderStrategyDiagnostics();
    return;
  }
  if (strategyDiagnosticsLoadingKey === cacheKey) return;
  const requestVersion = ++strategyDiagnosticsRequestVersion;
  strategyDiagnosticsLoadingKey = cacheKey;
  const loading = $("strategyDiagnosticsLoading");
  if (loading) {
    loading.hidden = false;
    loading.textContent = "正在运行“删掉一个标的”和5%调权对照，请稍候…";
  }
  const content = $("strategyDiagnosticsContent");
  if (content) content.hidden = true;
  try {
    const response = await api(`/api/backtest/${encodeURIComponent(runId)}/strategy-diagnostics?window=${encodeURIComponent(strategyDiagnosticsWindow)}`, {
      attempts: 1,
      requestTimeoutMs: 180000,
    });
    if (requestVersion !== strategyDiagnosticsRequestVersion || currentRunId !== runId) return;
    strategyDiagnosticsCache.set(cacheKey, response.strategy_diagnostics);
    strategyDiagnosticsRunId = runId;
    strategyDiagnosticsData = response.strategy_diagnostics;
    renderStrategyDiagnostics();
  } catch (error) {
    if (requestVersion === strategyDiagnosticsRequestVersion && currentRunId === runId && loading) {
      loading.hidden = false;
      loading.textContent = `策略诊断失败：${humanizeError(error.message)}`;
    }
  } finally {
    if (requestVersion === strategyDiagnosticsRequestVersion) strategyDiagnosticsLoadingKey = null;
  }
}

async function openCsvExportDialog() {
  if (!currentRunId) {
    showToast("请先运行或回放一条回测结果，再导出CSV", true);
    return;
  }
  const dialog = $("csvExportDialog");
  const runId = currentRunId;
  exportRunId = null;
  let exportConfig = null;
  try {
    const entry = await api(`/api/backtest/${encodeURIComponent(runId)}`, { attempts: 1 });
    if (currentRunId !== runId) return;
    currentRunConfig = JSON.parse(JSON.stringify(entry.config));
    currentSummary = entry.summary;
    exportConfig = currentRunConfig;
  } catch (loadError) {
    showToast(`读取当前回测失败：${humanizeError(loadError.message)}`, true);
    return;
  }
  if (!exportConfig) {
    showToast("当前回测配置尚未加载，请重新打开该回测后再导出CSV", true);
    return;
  }
  const start = exportConfig.start_date || currentSummary?.start_date || "";
  const end = exportConfig.end_date || currentSummary?.end_date || "";
  if ($("csvExportStart")) {
    $("csvExportStart").min = exportConfig.start_date || start;
    $("csvExportStart").max = exportConfig.end_date || end;
    $("csvExportStart").value = start;
  }
  if ($("csvExportEnd")) {
    $("csvExportEnd").min = exportConfig.start_date || start;
    $("csvExportEnd").max = exportConfig.end_date || end;
    $("csvExportEnd").value = end;
  }
  const assets = (exportConfig.assets || []).filter((asset) => asset.enabled && Number(asset.target_weight || 0) > 0);
  const repoOption = (exportConfig.repo_options || []).find((option) => (
    option.symbol === exportConfig.repo_symbol && (option.instrument_type || "repo") === "repo"
  ));
  const exportOptions = [
    ...assets.map((asset) => ({
      symbol: asset.symbol,
      name: asset.choice_label || asset.name || asset.symbol,
      detail: asset.symbol,
    })),
    {
      symbol: "REPO",
      name: "现金部分（含逆回购及应收分红）",
      detail: "逐日市值、盈亏、收益与回撤",
    },
    ...(repoOption ? [{
      symbol: repoOption.symbol,
      name: repoOption.name || repoOption.symbol,
      detail: `${repoOption.symbol} · 逆回购利率`,
    }] : []),
  ];
  if ($("csvExportAssets")) {
    $("csvExportAssets").innerHTML = exportOptions.map((option) => `<label><input type="checkbox" value="${escapeHtml(option.symbol)}" checked /><span><strong>${escapeHtml(option.name)}</strong><small>${escapeHtml(option.detail)}</small></span></label>`).join("");
  }
  if ($("csvExportError")) $("csvExportError").textContent = "";
  exportRunId = runId;
  [$("csvExportStart"), $("csvExportEnd")].forEach((input) => input?.setCustomValidity(""));
  if (typeof dialog?.showModal === "function") dialog.showModal();
  else dialog?.setAttribute("open", "");
}

async function downloadCsvExport() {
  const runId = exportRunId;
  if (!runId || runId !== currentRunId) {
    showToast("当前结果已切换，请重新打开导出窗口", true);
    return;
  }
  const startInput = $("csvExportStart");
  const endInput = $("csvExportEnd");
  const error = $("csvExportError");
  const startDate = String(startInput?.value || "");
  const endDate = String(endInput?.value || "");
  const symbols = [...document.querySelectorAll('#csvExportAssets input[type="checkbox"]:checked')].map((input) => input.value);
  const datesValid = Boolean(startDate && endDate && startDate <= endDate
    && (!startInput.min || startDate >= startInput.min) && (!endInput.max || endDate <= endInput.max));
  [startInput, endInput].filter(Boolean).forEach((input) => input.setCustomValidity(datesValid ? "" : "请选择有效的开始和结束时间"));
  if (!datesValid) {
    (startDate ? endInput : startInput)?.reportValidity();
    return;
  }
  if (!symbols.length) {
    if (error) error.textContent = "请至少选择一个标的";
    showToast("导出失败：请至少选择一个标的", true);
    return;
  }
  const params = new URLSearchParams({ start_date: startDate, end_date: endDate, symbols: symbols.join(",") });
  const button = $("downloadCsv");
  if (error) error.textContent = "";
  if (button) {
    button.disabled = true;
    button.textContent = "正在导出";
  }
  try {
    const response = await fetch(
      `${APP_BASE_PATH}/api/backtest/${encodeURIComponent(runId)}/export.csv?${params}`,
      { method: "GET", cache: "no-store", credentials: "same-origin", headers: { Accept: "text/csv" } },
    );
    if (!response.ok) {
      const responseText = await response.text();
      let message = response.statusText || `HTTP ${response.status}`;
      try {
        message = JSON.parse(responseText).error || message;
      } catch {
        if (responseText.trim()) message = responseText.trim();
      }
      const responseError = new Error(message);
      responseError.status = response.status;
      throw responseError;
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `permanent-investment-${startDate.replaceAll("-", "")}-${endDate.replaceAll("-", "")}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    $("csvExportDialog")?.close();
    showToast(`CSV已下载：${startDate} 至 ${endDate}`);
  } catch (downloadError) {
    const message = `导出失败：${humanizeError(downloadError.message)}`;
    if (error) error.textContent = message;
    showToast(message, true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "导出CSV";
    }
  }
}

const ASSET_COMOVEMENT_CATEGORIES = [
  { key: "same_up", label: "同涨", color: "#d96b45", className: "is-same-up" },
  { key: "same_down", label: "同跌", color: "#5278a8", className: "is-same-down" },
  { key: "hedge_positive", label: "对冲为正", color: "#1f7a5a", className: "is-hedge-positive" },
  { key: "hedge_negative", label: "对冲为负", color: "#b58a32", className: "is-hedge-negative" },
  { key: "unclassified", label: "未分类", color: "#b4bec2", className: "is-unclassified" },
];

function resetAssetComovementChart() {
  assetComovementRequestVersion += 1;
  assetComovementData = null;
  assetComovementRunId = null;
  assetComovementLoadingRunId = null;
  assetComovementWindow = "all";
  const windowSelect = $("assetComovementWindow");
  if (windowSelect) windowSelect.value = "all";
  delete pendingChartOptions.assetComovementChart;
  charts.assetComovementChart?.clear();
  const chartHost = $("assetComovementChart");
  if (chartHost && !window.echarts) chartHost.innerHTML = "";
  const summary = $("assetComovementSummary");
  if (summary) summary.innerHTML = "";
  const range = $("assetComovementRange");
  if (range) range.textContent = "运行回测后统计完整交易日";
  const empty = $("assetComovementEmpty");
  if (empty) empty.hidden = true;
}

function showAssetComovementEmpty(message) {
  charts.assetComovementChart?.clear();
  const summary = $("assetComovementSummary");
  if (summary) summary.innerHTML = "";
  const empty = $("assetComovementEmpty");
  if (!empty) return;
  empty.textContent = message;
  empty.hidden = false;
}

function renderAssetComovementFallback(items, total) {
  const host = $("assetComovementChart");
  if (!host) return;
  const segments = items
    .filter((item) => item.count > 0)
    .map((item) => (
      `<span style="width:${(item.count / total * 100).toFixed(4)}%;background:${item.color}" title="${escapeHtml(item.label)} ${item.count} 天"></span>`
    )).join("");
  host.innerHTML = `<div class="asset-comovement-fallback" role="img" aria-label="三资产联动天数分布">${segments}</div>`;
}

function renderAssetComovementChart() {
  const data = assetComovementData;
  const selected = data?.windows?.[assetComovementWindow] || data?.windows?.all;
  if (!data?.available || !selected?.comparable_days) {
    showAssetComovementEmpty(data?.message || "所选区间没有三项均可比的交易日");
    return;
  }
  const empty = $("assetComovementEmpty");
  if (empty) empty.hidden = true;
  const counts = selected.counts || {};
  const percentages = selected.percentages || {};
  const requestedItems = ASSET_COMOVEMENT_CATEGORIES.slice(0, 4).map((item) => ({
    ...item,
    count: Number(counts[item.key] || 0),
    percentage: Number(percentages[item.key] || 0),
  }));
  const chartItems = ASSET_COMOVEMENT_CATEGORIES.map((item) => ({
    ...item,
    count: Number(counts[item.key] || 0),
    percentage: Number(percentages[item.key] || 0),
  }));
  const summary = $("assetComovementSummary");
  if (summary) {
    summary.innerHTML = requestedItems.map((item) => `
      <article class="asset-comovement-card ${item.className}">
        <span>${escapeHtml(item.label)}</span>
        <strong>${item.count.toLocaleString("zh-CN")}<small>天</small></strong>
        <em>${fmtPct(item.percentage)}</em>
      </article>
    `).join("");
  }
  const range = $("assetComovementRange");
  if (range) {
    range.textContent = `${selected.start_date} 至 ${selected.end_date} · 可比 ${selected.comparable_days.toLocaleString("zh-CN")} 天`;
  }
  const note = $("assetComovementNote");
  if (note) {
    const unclassified = Number(counts.unclassified || 0);
    note.textContent = `对冲 ${selected.hedge_days} 天，同向 ${selected.same_direction_days} 天${unclassified ? `，未分类 ${unclassified} 天` : ""}。对冲日按三项等权平均日总收益区分正负；分红、上市前代理和ETF替换沿用回测口径。`;
  }
  if (!window.echarts) {
    renderAssetComovementFallback(chartItems, selected.comparable_days);
    return;
  }
  queueChartOption("assetComovementChart", {
    animationDuration: 420,
    grid: { left: 8, right: 8, top: 50, bottom: 14 },
    tooltip: {
      trigger: "item",
      formatter: (params) => `${escapeHtml(params.seriesName)}<br><strong>${Number(params.value).toLocaleString("zh-CN")} 天</strong> · ${fmtPct(Number(params.value) / selected.comparable_days)}`,
    },
    legend: { top: 5, left: "center", itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 11 } },
    xAxis: { type: "value", max: selected.comparable_days, show: false },
    yAxis: { type: "category", data: ["交易日"], show: false },
    series: chartItems.map((item, index) => ({
      type: "bar",
      name: item.label,
      stack: "days",
      data: [item.count],
      barWidth: 34,
      itemStyle: {
        color: item.color,
        borderRadius: index === 0 ? [7, 0, 0, 7] : index === chartItems.length - 1 ? [0, 7, 7, 0] : 0,
      },
      label: {
        show: item.percentage >= 0.08,
        position: "inside",
        formatter: `${item.count}天`,
        color: "#fff",
        fontSize: 10,
        fontWeight: 700,
      },
    })),
  });
  if (activeChartId === "assetComovementChart") {
    applyChartOption("assetComovementChart");
    charts.assetComovementChart?.resize();
  }
}

async function loadAssetComovementChart() {
  const runId = currentRunId;
  if (!runId) {
    showAssetComovementEmpty("运行回测后查看三资产联动统计");
    return;
  }
  if (assetComovementRunId === runId && assetComovementData) {
    renderAssetComovementChart();
    return;
  }
  if (assetComovementLoadingRunId === runId) return;
  const requestVersion = ++assetComovementRequestVersion;
  assetComovementLoadingRunId = runId;
  showAssetComovementEmpty("正在统计全部共同交易日…");
  try {
    const response = await api(`/api/backtest/${encodeURIComponent(runId)}/asset-comovement`, { attempts: 4, retryDelayMs: 500 });
    if (requestVersion !== assetComovementRequestVersion || currentRunId !== runId) return;
    assetComovementRunId = runId;
    assetComovementData = response.asset_comovement;
    renderAssetComovementChart();
  } catch (error) {
    if (requestVersion === assetComovementRequestVersion && currentRunId === runId) {
      showAssetComovementEmpty(`资产联动统计加载失败：${humanizeError(error.message)}`);
    }
  } finally {
    if (requestVersion === assetComovementRequestVersion) assetComovementLoadingRunId = null;
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
  if (option?.tooltip) option.tooltip = { confine: true, ...option.tooltip };
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
  currentChartSeries = series;
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
    table.innerHTML = `<tbody><tr><td class="table-empty">${escapeHtml(options.emptyMessage || "暂无数据")}</td></tr></tbody>`;
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
  table.querySelectorAll("[data-rebalance-date]").forEach((button) => button.addEventListener("click", () => selectRebalanceEvent(button.dataset.rebalanceDate)));
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
  if (value && typeof value === "object" && value.kind === "rebalance-positive") return rebalancePositiveMarkup(value);
  if (value && typeof value === "object" && value.kind === "event-date") return `<button type="button" class="event-date-button" data-rebalance-date="${escapeHtml(value.raw)}" aria-label="查看 ${escapeHtml(value.raw)} 调仓详情并定位图表">${escapeHtml(value.raw)}</button>`;
  if (value && typeof value === "object" && value.kind === "money") return value.raw == null ? "—" : escapeHtml(`￥${fmtMoney(value.raw)}`);
  if (value && typeof value === "object" && value.kind === "number") {
    return escapeHtml(fmtNum(value.raw, value.decimals ?? 2));
  }
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
    const sign = profit > 0 ? "+" : profit < 0 ? "−" : "";
    const amount = Math.abs(profit);
    const full = `${sign}￥${fmtMoney(amount)}`;
    const compact = amount >= 1e8 ? `${sign}￥${(amount / 1e8).toFixed(2)}亿` : amount >= 1e4 ? `${sign}￥${(amount / 1e4).toFixed(2)}万` : full;
    const period = value.yearLabel ? `${value.yearLabel}年${value.asOfDate ? ` · 截至${String(value.asOfDate).slice(5)}` : ""}` : "";
    const title = `${full}${value.asOfDate ? `；截至 ${value.asOfDate}` : ""}${value.yearStartTotal == null ? "" : `；年初资产 ￥${fmtMoney(value.yearStartTotal)}`}${value.externalFlow == null ? "" : `；期间外部净流入 ￥${fmtMoney(value.externalFlow)}`}`;
    return `<span class="table-year-profit is-${tone}" title="${escapeHtml(title)}"><strong><span class="visually-hidden">${escapeHtml(full)}</span><span class="profit-full" aria-hidden="true">${escapeHtml(full)}</span><span class="profit-compact" aria-hidden="true">${escapeHtml(compact)}</span></strong>${period ? `<small class="rebalance-year-context">${escapeHtml(period)}</small>` : ""}</span>`;
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
  const title = value.profitBasis === "position_change_excluding_trade_principal_including_net_distributions_and_fees"
    ? "盈亏剔除买卖本金，包含净分红及费用；百分比为盈亏除以期初持仓金额，首次建仓年用初始配置金额，不是该标的的时间加权净值收益。"
    : "";
  return `<span class="${className}" title="${escapeHtml(title)}">${profitText} / ${rateText}</span>`;
}

function rebalanceActionLabel(payload = {}) {
  if (payload.event_type === "initial_allocation") return payload.rebalanced ? "首次建仓" : "建仓未成交";
  if (payload.event_type === "treasury_activation") return "现金标的启用";
  if (payload.rebalance_action === "trade" || payload.rebalanced === true) return "已调仓";
  if (payload.rebalance_reason === "within_band") return "带内，无需调仓";
  if (payload.rebalance_reason === "trade_constraints") return "超带，成交条件不足";
  return "历史记录，未区分成交";
}

function rebalanceAssetColumnName(symbol) {
  return SHORT_NAMES[symbol] || assetName(symbol);
}

function rebalanceCashEquivalentSymbols() {
  const symbols = new Set(["REPO"]);
  const resultConfig = currentRunConfig || config;
  for (const option of resultConfig.repo_options || []) {
    if (["repo", "money_fund"].includes(option.instrument_type) && option.symbol) {
      symbols.add(option.symbol);
    }
  }
  if (resultConfig.repo_symbol) symbols.add(resultConfig.repo_symbol);
  return symbols;
}

function rebalanceDisplayRows(rows) {
  const visibleRows = rows.filter((row) => rebalanceActionLabel(row.payload) !== "带内，无需调仓");
  const cashEquivalentSymbols = rebalanceCashEquivalentSymbols();
  const symbols = [];
  for (const row of visibleRows) {
    for (const symbol of Object.keys(row.payload?.asset_performance || {})) {
      if (cashEquivalentSymbols.has(symbol)) continue;
      if (!symbols.includes(symbol)) symbols.push(symbol);
    }
  }
  const orderedSymbols = (currentRunConfig || config).assets.map((asset) => asset.symbol).filter((symbol) => symbols.includes(symbol));
  for (const symbol of symbols) {
    if (!orderedSymbols.includes(symbol)) orderedSymbols.push(symbol);
  }
  const baseColumns = ["执行日", "当年盈亏", "当年最大回撤", "当年收益（按上年度总资产）", "当年总资产", "当年收益（按原始资金）", "检查结果", "成交金额", "成交笔数", "当年手续费", "收益年度", "决策日"];
  const assetColumns = orderedSymbols.map(rebalanceAssetColumnName);
  const displayRows = visibleRows.map((row) => {
    const annualTotal = row.payload?.decision_total_asset_cny ?? row.total_asset_before;
    const item = {
      执行日: { kind: "event-date", raw: row.rebalance_date },
      检查结果: rebalanceActionLabel(row.payload),
      成交笔数: row.payload?.executed_trade_count == null ? "—" : { kind: "number", raw: row.payload.executed_trade_count, decimals: 0 },
      成交金额: row.turnover_cny == null ? "—" : `￥${fmtMoney(row.turnover_cny)}`,
      决策日: row.payload?.decision_date || row.rebalance_date,
      收益年度: row.payload?.year_label ? `${row.payload.year_label}年` : `${String(row.payload?.decision_date || row.rebalance_date).slice(0, 4)}年`,
      当年总资产: annualTotal == null ? "—" : `￥${fmtMoney(annualTotal)}`,
      "当年收益（按上年度总资产）": { kind: "metric", raw: row.payload?.year_profit_on_year_start, format: "percent", tone: annualReturnTone(row.payload?.year_profit_on_year_start) },
      "当年收益（按原始资金）": { kind: "metric", raw: row.payload?.year_profit_on_original_capital, format: "percent", tone: annualReturnTone(row.payload?.year_profit_on_original_capital) },
      当年盈亏: {
        kind: "year-profit",
        profit: row.payload?.year_profit_cny,
        raw: row.payload?.year_profit_cny,
        yearLabel: row.payload?.year_label || String(row.payload?.decision_date || row.rebalance_date).slice(0, 4),
        asOfDate: row.payload?.decision_date || row.rebalance_date,
        yearStartTotal: row.payload?.year_start_total_cny,
        externalFlow: row.payload?.year_external_flow_cny,
      },
      当年最大回撤: { kind: "metric", raw: row.payload?.year_max_drawdown, format: "percent", tone: drawdownTone(row.payload?.year_max_drawdown) },
      当年手续费: { kind: "money", raw: row.payload?.year_fee_cny },
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
        profitBasis: row.payload?.asset_profit_basis,
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

async function waitForBacktestJob(jobId, onProgress = setMessage) {
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
      onProgress("网络短暂波动，正在继续等待回测结果...");
      await sleep(Math.min(1500 * transientFailures, 6000));
      continue;
    }
    if (job.status === "completed") return job.result;
    if (job.status === "failed") throw new Error(job.error || job.message || "回测失败");
    if (job.status === "cancelled") throw new Error(job.error || job.message || "回测任务已取消");
    onProgress(job.message || (job.status === "running" ? "正在运行回测..." : "回测任务排队中..."));
    pollCount += 1;
    await sleep(Math.min(650 + pollCount * 100, 1500));
  }
}

async function loadBacktestResultSections(runId, onSeries, prefetchedChart = null) {
  const seriesPromise = prefetchedChart
    ? Promise.resolve({ chart: prefetchedChart })
    : api(`/api/backtest/${runId}/chart-series`, { attempts: 6, retryDelayMs: 700 });
  const rebalancePromise = api(`/api/backtest/${runId}/rebalance`, { attempts: 5, retryDelayMs: 700 });
  const tradesPromise = api(`/api/backtest/${runId}/trades`, { attempts: 5, retryDelayMs: 700 });
  // Attach handlers to all parallel requests immediately. A failed records
  // request must not become an unhandled rejection while charts are loading.
  const [seriesData, rebalance, trades] = await Promise.all([seriesPromise, rebalancePromise, tradesPromise]);
  const series = computeSeriesMetrics(expandChartSeries(seriesData));
  await onSeries?.(series);
  return { series, rebalance, trades };
}

function setAnalysisMessage(text = "", retry = false) {
  if ($("analysisMessage")) $("analysisMessage").textContent = text;
  if ($("retryAnalysisBtn")) $("retryAnalysisBtn").hidden = !retry;
}

async function retryBacktestAnalysis() {
  const runId = currentRunId;
  if (!runId) return;
  if ($("retryAnalysisBtn")) $("retryAnalysisBtn").disabled = true;
  try {
    await api(`/api/backtest/${encodeURIComponent(runId)}/analysis`, { method: "POST", body: "{}" });
    if (currentRunId !== runId) return;
    setAnalysisMessage("扩展分析已重新开始，主体结果仍可查看。");
    const { rebalance, trades } = await loadBacktestResultSections(runId);
    if (currentRunId !== runId) return;
    watchBacktestAnalysis(runId, rebalance, trades);
  } catch (error) {
    if (currentRunId === runId) setAnalysisMessage(`重试失败：${humanizeError(error.message)}`, true);
  } finally {
    if ($("retryAnalysisBtn")) $("retryAnalysisBtn").disabled = false;
  }
}

async function watchBacktestAnalysis(runId, rebalance, trades) {
  const watchId = ++activeAnalysisWatch;
  let failures = 0;
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await sleep(1000);
    if (watchId !== activeAnalysisWatch || currentRunId !== runId) return;
    try {
      const entry = await api(`/api/backtest/${encodeURIComponent(runId)}`, { attempts: 2, retryDelayMs: 500 });
      if (watchId !== activeAnalysisWatch || currentRunId !== runId) return;
      failures = 0;
      const status = entry.summary?.analysis_status || "completed";
      if (status === "completed" || status === "not_required") {
        renderSummary(entry.summary);
        renderBacktestRecords(entry.summary, rebalance, trades);
        scheduleArchiveRefresh({ includeLeaderboard: activeArchiveView === "leaderboard" });
        setMessage("主体结果、滚动窗口和月份对比均已完成");
        setAnalysisMessage("扩展分析已完成。");
        return;
      }
      if (status === "failed") {
        renderBacktestRecords(entry.summary, rebalance, trades);
        setAnalysisMessage(`扩展分析失败：${humanizeError(entry.summary?.analysis_error || "未知错误")}`, true);
        setMessage(`主体结果已显示；扩展分析失败：${humanizeError(entry.summary?.analysis_error || "未知错误")}`, true);
        return;
      }
    } catch (error) {
      if (watchId !== activeAnalysisWatch || currentRunId !== runId) return;
      failures += 1;
      if (failures >= 5) {
        console.warn("无法继续获取后台分析进度", error);
        setAnalysisMessage("暂时无法获取分析进度，可以重新连接并继续。", true);
        return;
      }
    }
  }
  if (watchId === activeAnalysisWatch && currentRunId === runId) setAnalysisMessage("扩展分析仍在后台运行，可以重新连接查看进度。", true);
}

function renderBacktestRecords(summary, rebalance, trades) {
  currentRebalanceRecords = rebalance.rebalance || [];
  currentTradeRecords = trades.trades || [];
  const rollingPeriods = summary?.rolling_periods || [];
  const analysisConfig = currentRunConfig || config;
  const analysisEnabled = analysisConfig?.rebalance_frequency === "yearly" && analysisConfig?.rebalance_month_analysis_enabled;
  const analysisPending = ["pending", "running"].includes(summary?.analysis_status);
  const analysisFailed = summary?.analysis_status === "failed";
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
      emptyMessage: analysisPending ? "滚动窗口正在计算，完成后自动更新。" : analysisFailed ? "扩展分析失败，可点击上方按钮重试。" : "回测区间不足一个完整滚动窗口，可缩短窗口年数或延长回测区间。",
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
      emptyMessage: !analysisEnabled ? "月份对比未开启。选择每年检查，并在高级设置中开启 1–12 月对比后重新运行。" : analysisPending ? "月份对比正在后台计算。" : analysisFailed ? "扩展分析失败，可点击上方按钮重试。" : "当前区间没有可比较的年度月份。",
      sortableColumns: ["再平衡月份", "年盈利率", "最大回撤", "年盈利/回撤比"],
      defaultSort: { column: "年盈利/回撤比", direction: "desc" },
      sortDirections: { 再平衡月份: "asc", 最大回撤: "desc" },
    },
  );
  $("recordTabMonths").textContent = `月份对比（${monthScenarios.length}）`;
  if (["pending", "running"].includes(summary?.analysis_status)) {
    $("recordTabRolling").textContent = "滚动窗口（后台计算中）";
    $("recordTabMonths").textContent = analysisEnabled ? "月份对比（后台计算中）" : "月份对比（未开启）";
    setAnalysisMessage("滚动窗口与已启用的月份对比正在后台计算。");
  } else if (summary?.analysis_status === "failed") {
    setAnalysisMessage(`扩展分析失败：${humanizeError(summary.analysis_error || "未知错误")}`, true);
  } else {
    setAnalysisMessage(rollingPeriods.length ? "扩展分析已完成。" : "当前区间不足一个完整滚动窗口。");
    if (!analysisEnabled && !monthScenarios.length) $("recordTabMonths").textContent = "月份对比（未开启）";
  }

  const rebalanceTable = rebalanceDisplayRows(rebalance.rebalance || []);
  renderTable("rebalanceTable", rebalanceTable.columns, rebalanceTable.rows, { pageSize: 200, newestFirst: true });
  $("recordTabRebalance").textContent = `再平衡记录（${rebalanceTable.rows.length}）`;
  renderTradeRecords();
}

function historyTitle(entry) {
  if (entry.metadata?.name) return String(entry.metadata.name);
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
  const haystack = `${historyTitle(entry)} ${historyParams(entry)} ${entry.metadata?.note || ""} ${(entry.config?.assets || []).map((a) => a.name || a.symbol).join(" ")}`.toLocaleLowerCase("zh-CN");
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
    .filter((entry) => (!favoritesOnly || entry.metadata?.favorite) && archiveEntryMatches(entry))
    .map((entry, index) => ({ entry, index }))
    .sort((left, right) => archiveSortValue(right.entry, mode) - archiveSortValue(left.entry, mode) || left.index - right.index)
    .map((item) => item.entry);
}

function comparisonEntries() {
  const current = currentHistoryEntry() || (currentRunId && currentRunConfig ? { run_id: currentRunId, config: currentRunConfig, summary: currentSummary || {} } : null);
  const compared = archiveEntries().find((entry) => entryRunId(entry) === comparisonRunId);
  return [current, compared];
}

function renderHistoryComparison() {
  const host = $("historyComparison");
  const [current, compared] = comparisonEntries();
  if (!host || !current || !compared || currentRunId === comparisonRunId) { if (host) host.hidden = true; return; }
  host.hidden = false;
  const key = `${entryRunId(current)}:${entryRunId(compared)}`;
  const common = commonComparison?.key === key ? commonComparison : null;
  const left = common?.entries?.[0] || current, right = common?.entries?.[1] || compared;
  const dates = (entry) => [entry.summary?.start_date || entry.config?.start_date, entry.summary?.end_date || entry.config?.end_date];
  const a = dates(left), b = dates(right), samePeriod = a[0] === b[0] && a[1] === b[1];
  const overlap = commonDateRange(current, compared);
  const s1 = left.summary || {}, s2 = right.summary || {};
  const optionalPct = (value) => value == null || !Number.isFinite(Number(value)) ? "—" : fmtPct(value);
  const moneyCell = (v) => v == null ? "—" : `￥${fmtMoney(v)}`;
  const count = (v) => v == null ? "—（旧结果未记录）" : String(v);
  const rows = [["方案", historyTitle(current), historyTitle(compared)], ["实际区间", a.join(" 至 "), b.join(" 至 ")], ["年化收益", optionalPct(s1.annualized_return), optionalPct(s2.annualized_return)], ["累计收益", optionalPct(s1.total_return), optionalPct(s2.total_return)], ["最大回撤", optionalPct(s1.max_drawdown), optionalPct(s2.max_drawdown)], ["期末资产", moneyCell(s1.final_asset_cny), moneyCell(s2.final_asset_cny)], ["总费用", moneyCell(s1.total_fees_cny), moneyCell(s2.total_fees_cny)], ["实际调仓次数", count(s1.rebalance_trade_count), count(s2.rebalance_trade_count)], ["交易笔数", count(s1.trade_count), count(s2.trade_count)]];
  host.innerHTML = `<strong>${common?.entries ? "按共同区间重跑的结果" : "当前结果与所选方案对比"}</strong><p class="comparison-warning">${samePeriod ? "实际区间一致。请同时查看以下参数差异。" : "区间不同，全程指标不能直接横向比较。"}</p><div class="comparison-grid">${rows.flatMap((row) => row.map((cell) => `<span>${escapeHtml(cell)}</span>`)).join("")}</div><details class="comparison-differences"><summary>查看参数差异</summary>${configDifferenceMarkup(current.config, compared.config)}</details><p>${escapeHtml(common?.message || (overlap ? `共同请求区间：${overlap.start_date} 至 ${overlap.end_date}。重新计算会保存两份回测，不修改编辑草稿。` : "两份方案没有重叠区间，不能进行同期比较。"))}</p><button type="button" class="button button-secondary" data-compare-common ${!overlap || common?.busy ? "disabled" : ""}>${common?.busy ? "同期回测中…" : "按共同区间重新计算两组"}</button>`;
  host.querySelector("[data-compare-common]")?.addEventListener("click", runCommonComparison);
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
      ${historyMetadataMarkup(entry)}<div class="history-item-metrics">${historyMetricMarkup("年盈利率", summary.annualized_return, "percent", annualReturnTone(summary.annualized_return))}${historyMetricMarkup("年盈利/回撤比", ratio, "ratio", ratioTone(ratio))}${historyMetricMarkup("最大回撤", summary.max_drawdown, "percent", drawdownTone(summary.max_drawdown))}</div>
      ${historyActionsMarkup(entry, "history", compareLabel)}
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
  bindHistoryMetadataActions(host);
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
      ${historyMetadataMarkup(entry)}<div class="leaderboard-metrics">${metricMarkup}</div>
      <div class="leaderboard-score">${scoreMarkup}</div>
      ${historyActionsMarkup(entry, "leaderboard", compareLabel)}
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
  bindHistoryMetadataActions(host);
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
  const requestVersion = ++recentArchiveRequestVersion;
  const onlyFavorites = favoritesOnly;
  const history = await api(`/api/backtest/history${onlyFavorites ? "?favorites=1" : ""}`);
  if (requestVersion !== recentArchiveRequestVersion || onlyFavorites !== favoritesOnly) return;
  const recentRecords = onlyFavorites ? (history.records || []) : (history.records || []).slice(0, MAX_RUN_HISTORY);
  runHistory = recentRecords;
  recentArchiveLoaded = true;
  const historyRecentMeta = $("historyRecentMeta");
  if (historyRecentMeta) historyRecentMeta.textContent = onlyFavorites ? `全部收藏 ${recentRecords.length} 组（包含较早记录）` : `数据库最近 ${recentRecords.length} / ${MAX_RUN_HISTORY} 组`;
  if ($("historyRecentTab")) $("historyRecentTab").textContent = `${onlyFavorites ? "收藏方案" : "最近回测"} ${recentRecords.length}`;
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
  if (runInProgress || pendingReplayRunId) { showToast("请等待当前回测或回放完成后再删除记录"); return; }
  if (!runId || !window.confirm("删除这组回测及其榜单记录？此操作不可恢复。")) return;
  try {
    await api(`/api/backtest/${encodeURIComponent(runId)}`, { method: "DELETE", retry: true });
    if (currentRunId === runId) {
      resultRequestVersion += 1;
      activeAnalysisWatch += 1;
      currentRunId = null;
      currentSummary = null;
      currentRunConfig = null;
      currentChartSeries = []; currentRebalanceRecords = []; currentTradeRecords = []; selectedTradeDate = null;
      if ($("rebalanceEventDetail")) $("rebalanceEventDetail").hidden = true;
      renderTradeRecords(); renderResearch();
      resetDailyPnlChart();
      resetStrategyDiagnostics();
      resetAssetComovementChart();
      exportRunId = null;
      $("csvExportDialog")?.close();
      Object.keys(pendingChartOptions).forEach((key) => delete pendingChartOptions[key]);
      Object.values(charts).forEach((chart) => chart.clear());
      document.querySelectorAll(".chart-view .chart").forEach((host) => {
        if (!charts[host.id]) host.innerHTML = "";
      });
      renderInitialSummary();
      ["rollingTable", "monthsTable", "rebalanceTable", "tradesTable"].forEach((id) => renderTable(id, [], []));
      [["recordTabRolling", "滚动窗口"], ["recordTabMonths", "月份对比"], ["recordTabRebalance", "调仓记录"], ["recordTabTrades", "交易流水"]]
        .forEach(([id, label]) => { $(id).textContent = label; });
      setAnalysisMessage();
    }
    if (comparisonRunId === runId) comparisonRunId = null;
    await refreshBacktestArchiveSafely({ includeLeaderboard: true });
    setMessage("回测记录已从数据库删除");
  } catch (error) {
    setMessage(`删除失败：${humanizeError(error.message)}`, true);
  }
}

async function replayHistoryRun(runId) {
  if (runInProgress) { showToast("请等待当前回测完成后再回放记录"); return; }
  const requestVersion = ++resultRequestVersion;
  pendingReplayRunId = runId;
  activeAnalysisWatch += 1;
  setMessage("正在回放已保存的回测结果...");
  try {
    const chartReady = loadChartLibrary().catch((error) => console.warn(error));
    const entry = await api(`/api/backtest/${encodeURIComponent(runId)}`);
    if (requestVersion !== resultRequestVersion) return;
    const { series, rebalance, trades } = await loadBacktestResultSections(entry.run_id);
    await chartReady;
    if (requestVersion !== resultRequestVersion) return;
    currentRunConfig = JSON.parse(JSON.stringify(entry.config));
    selectedTradeDate = null;
    if ($("rebalanceEventDetail")) $("rebalanceEventDetail").hidden = true;
    resetDailyPnlChart();
    resetStrategyDiagnostics();
    resetAssetComovementChart();
    currentRunId = entry.run_id;
    renderSummary(deriveSummary(entry.summary, series));
    renderCharts(series);
    renderBacktestRecords(entry.summary, rebalance, trades);
    if (activeChartId === "strategyDiagnosticsChart") loadStrategyDiagnostics().catch(() => {});
    scheduleArchiveRefresh({ includeLeaderboard: activeArchiveView === "leaderboard" });
    setHistoryPanel(false);
    selectRecordPanel("rebalancePanel");
    if (["pending", "running"].includes(entry.summary?.analysis_status)) {
      setMessage("已显示主体结果；滚动窗口与月份对比正在后台补齐");
      watchBacktestAnalysis(currentRunId, rebalance, trades);
    } else {
      setMessage("已回放保存的回测结果");
    }
  } catch (error) {
    if (requestVersion === resultRequestVersion) setMessage(`回放失败：${humanizeError(error.message)}`, true);
  } finally {
    if (requestVersion === resultRequestVersion) pendingReplayRunId = null;
  }
}

async function runBacktest() {
  if (runInProgress || !config || !validateControls()) return;
  draftLoadVersion += 1;
  const requestVersion = ++resultRequestVersion;
  pendingReplayRunId = null;
  activeAnalysisWatch += 1;
  setRunBusy(true);
  if (isMobileLayout()) setParameterPanel(false);
  setMessage("正在提交回测任务...");
  try {
    const submittedFullConfig = readConfig();
    const submittedConfig = compactConfigForRequest(submittedFullConfig);
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
    const { series, rebalance, trades } = await loadBacktestResultSections(result.run_id, null, result.chart || null);
    await chartReady;
    if (requestVersion !== resultRequestVersion) return;
    resetDailyPnlChart();
    resetStrategyDiagnostics();
    resetAssetComovementChart();
    currentRunId = result.run_id;
    selectedTradeDate = null;
    if ($("rebalanceEventDetail")) $("rebalanceEventDetail").hidden = true;
    currentRunConfig = JSON.parse(JSON.stringify(submittedFullConfig));
    if (result.status) renderStatus(result.status);
    const finalSummary = deriveSummary(result.summary, series);
    renderSummary(finalSummary);
    renderCharts(series);
    renderBacktestRecords(finalSummary, rebalance, trades);
    selectRecordPanel("rebalancePanel");
    if (activeChartId === "strategyDiagnosticsChart") loadStrategyDiagnostics().catch(() => {});
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
    if (requestVersion === resultRequestVersion) setMessage(humanizeError(error.message), true);
  } finally {
    setRunBusy(false);
  }
}

function setParameterPanel(open, invoker = null) {
  const wasOpen = document.body.classList.contains("parameters-open");
  if (open) setHistoryPanel(false);
  if (open && !document.body.classList.contains("parameters-open")) drawerReturnFocus = invoker || $(isMobileLayout() ? "mobileParameterToggle" : "parameterToggle") || document.activeElement;
  document.body.classList.toggle("parameters-open", open);
  [$("parameterToggle"), $("mobileParameterToggle")].filter(Boolean).forEach((button) => {
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
  syncDrawerAccessibility();
  if (open) window.requestAnimationFrame(() => $("closeParameterPanel")?.focus());
  else if (wasOpen) drawerReturnFocus?.focus();
}

function setHistoryPanel(open, invoker = null) {
  const wasOpen = isMobileLayout() ? document.body.classList.contains("history-open") : !document.body.classList.contains("history-collapsed");
  if (open) drawerReturnFocus = invoker || $(isMobileLayout() ? "mobileHistoryToggle" : "historyToggle") || document.activeElement;
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
  syncDrawerAccessibility();
  if (expanded) window.requestAnimationFrame(() => $("closeHistoryPanel")?.focus());
  else if (wasOpen) drawerReturnFocus?.focus();
}

function syncDrawerAccessibility() {
  const mobile = isMobileLayout();
  const parametersOpen = mobile && document.body.classList.contains("parameters-open");
  const historyOpen = mobile ? document.body.classList.contains("history-open") : !document.body.classList.contains("history-collapsed");
  $("parameterPanel")?.toggleAttribute("inert", historyOpen || (mobile && !parametersOpen));
  $("historyPanel")?.toggleAttribute("inert", !historyOpen);
  document.querySelector("main")?.toggleAttribute("inert", parametersOpen || historyOpen);
  if (!document.body.classList.contains("identity-locked")) {
    document.querySelector(".mobile-app-bar")?.toggleAttribute("inert", mobile && (parametersOpen || historyOpen));
  }
  [$("parameterToggle"), $("mobileParameterToggle")].filter(Boolean).forEach((button) => button.setAttribute("aria-expanded", String(parametersOpen)));
  [$("historyToggle"), $("mobileHistoryToggle")].filter(Boolean).forEach((button) => button.setAttribute("aria-expanded", String(historyOpen)));
}

function trapOverlayFocus(event) {
  if (event.key !== "Tab") return;
  const overlay = !$("identityGate")?.hidden ? $("identityGate")
    : isMobileLayout() && document.body.classList.contains("parameters-open") ? $("parameterPanel")
      : (isMobileLayout() ? document.body.classList.contains("history-open") : !document.body.classList.contains("history-collapsed")) ? $("historyPanel") : null;
  if (!overlay) return;
  const items = [...overlay.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, a[href], [tabindex="0"]')]
    .filter((item) => item.tabIndex >= 0 && item.getClientRects().length && !item.closest("[hidden], [inert]"));
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (event.shiftKey && (document.activeElement === first || !overlay.contains(document.activeElement))) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !overlay.contains(document.activeElement))) {
    event.preventDefault(); first.focus();
  }
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
  setupResearchInteractions();
  window.addEventListener("resize", syncDrawerAccessibility);
  setRunBusy(false);
  $("parameterPanel")?.addEventListener("input", () => { if (config) updateResultContext(); });
  $("parameterPanel")?.addEventListener("change", () => { if (config) updateResultContext(); });
  $("identityKeyInput")?.addEventListener("input", (event) => event.target.setCustomValidity(""));
  ["leaderboardStartDate", "leaderboardEndDate", "csvExportStart", "csvExportEnd"].forEach((id) => {
    $(id)?.addEventListener("input", (event) => event.target.setCustomValidity(""));
  });
  $("retryAnalysisBtn")?.addEventListener("click", retryBacktestAnalysis);
  document.addEventListener("keydown", trapOverlayFocus);
  $("identityForm")?.addEventListener("submit", saveIdentityKey);
  [$('identityKeyButton'), $('mobileIdentityKeyButton')].filter(Boolean).forEach((button) => {
    button.addEventListener("click", () => showIdentityGate(false));
  });
  $("identityCancel")?.addEventListener("click", () => {
    if (!identityGateRequired) closeIdentityGate();
  });
  [$("parameterToggle"), $("mobileParameterToggle")].filter(Boolean).forEach((button) => {
    button.addEventListener("click", () => setParameterPanel(true, button));
  });
  $("closeParameterPanel")?.addEventListener("click", () => setParameterPanel(false));
  $("parameterBackdrop")?.addEventListener("click", () => setParameterPanel(false));
  $("historyToggle")?.addEventListener("click", (event) => setHistoryPanel(document.body.classList.contains("history-collapsed"), event.currentTarget));
  $("mobileHistoryToggle")?.addEventListener("click", (event) => setHistoryPanel(true, event.currentTarget));
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
    if (event.key !== "Escape" || event.target?.closest?.("dialog[open]")) return;
    if (!$("identityGate")?.hidden) {
      if (!identityGateRequired) closeIdentityGate();
      return;
    }
    if (document.body.classList.contains("parameters-open")) setParameterPanel(false);
    if (document.body.classList.contains("history-open") || !document.body.classList.contains("history-collapsed")) setHistoryPanel(false);
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
  $("assetComovementWindow")?.addEventListener("change", (event) => {
    assetComovementWindow = event.target.value || "all";
    renderAssetComovementChart();
  });
  $("strategyDiagnosticsWindow")?.addEventListener("change", (event) => {
    strategyDiagnosticsWindow = event.target.value || "all";
    loadStrategyDiagnostics().catch(() => {});
  });
  $("assetComovementDetails")?.addEventListener("toggle", (event) => {
    if (!event.target.open) return;
    loadAssetComovementChart().then(() => charts.assetComovementChart?.resize()).catch(() => {});
  });
  $("openCsvExport")?.addEventListener("click", openCsvExportDialog);
  $("downloadCsv")?.addEventListener("click", downloadCsvExport);
  setupTabs("[data-record-tab]", "recordTab", selectRecordPanel);
  selectChart(activeChartId);
  selectRecordPanel("rebalancePanel");
  selectArchiveView(activeArchiveView);
  syncDrawerAccessibility();
}


// Percent editors retain invalid text for correction; only valid values update the strategy.
function validPercentInput(input) {
  const value = Number(input.value);
  const valid = input.value.trim() !== "" && Number.isFinite(value) && value >= Number(input.min) && value <= Number(input.max);
  input.setCustomValidity(valid ? "" : `请输入 ${input.min} 至 ${input.max} 之间的百分比`);
  input.setAttribute("aria-invalid", valid ? "false" : "true");
  return valid;
}

function bindPercentControl(id, options = {}) {
  const range = $(id), input = $(`${id}Percent`);
  if (!range || !input) return;
  input.value = String(Math.round(Number(range.value) * 1e8) / 1e6);
  input.setCustomValidity("");
  input.disabled = range.disabled;
  range.addEventListener("input", () => {
    input.value = String(Math.round(Number(range.value) * 1e8) / 1e6);
    input.setCustomValidity(""); input.setAttribute("aria-invalid", "false");
    if (id === "rebalanceBand") $("bandValue").textContent = fmtPct(range.value);
    updateRepoWeight();
  }, options);
  input.addEventListener("input", () => {
    if (!validPercentInput(input)) return;
    range.value = String(Number(input.value) / 100);
    if (id === "rebalanceBand") $("bandValue").textContent = fmtPct(range.value);
    updateRepoWeight();
  }, options);
}

function renderTradeRecords() {
  const rows = selectedTradeDate ? currentTradeRecords.filter((row) => row.trade_date === selectedTradeDate) : currentTradeRecords;
  renderTable("tradesTable", ["交易日期", "标的名称", "方向", "份额", "价格", "成交额", "费用", "币种", "原因"], rows.map((row) => ({
    交易日期: row.trade_date, 标的名称: tradeAssetName(row.symbol), 方向: SIDE_NAMES[row.side] || row.side,
    份额: row.quantity, 价格: { kind: "number", raw: row.price, decimals: 4 }, 成交额: { kind: "number", raw: row.gross_amount, decimals: 2 },
    费用: { kind: "number", raw: row.fee, decimals: 2 }, 币种: CURRENCY_NAMES[row.currency] || row.currency, 原因: REASON_NAMES[row.reason] || row.reason,
  })), { pageSize: 300, newestFirst: true });
  if ($("recordTabTrades")) $("recordTabTrades").textContent = `交易流水（${rows.length}${selectedTradeDate ? ` / ${currentTradeRecords.length}` : ""}）`;
  if ($("tradeDateFilter")) $("tradeDateFilter").hidden = !selectedTradeDate;
  if ($("tradeDateFilterLabel")) $("tradeDateFilterLabel").textContent = selectedTradeDate ? `仅显示 ${selectedTradeDate}：${rows.length} 笔` : "";
}

function eventWeightRows(event) {
  const before = event.payload?.before_weights, after = event.payload?.after_weights;
  if (!before || !after) return null;
  return [...new Set([...Object.keys(before), ...Object.keys(after)])].map((symbol) => ({ symbol, before: Number(before[symbol] || 0), after: Number(after[symbol] || 0) })).filter((row) => Math.abs(row.before) > 1e-10 || Math.abs(row.after) > 1e-10);
}

function rebalanceEventReason(payload = {}) {
  if (payload.event_type === "initial_allocation") return "首次配置建仓";
  if (payload.event_type === "treasury_activation") return "现金管理标的开始可用";
  const frequency = REBALANCE_FREQUENCY_NAMES[payload.rebalance_frequency] || REBALANCE_FREQUENCY_NAMES[currentRunConfig?.rebalance_frequency];
  const threshold = payload.threshold_exceeded;
  if (frequency && threshold === true) return `${frequency}检查，权重超出${payload.rebalance_band == null ? "" : fmtPct(payload.rebalance_band)}相对容忍带${payload.rebalance_reason === "trade_constraints" ? "，但成交条件不足" : ""}`;
  const labels = { scheduled_open: "到达检查周期，按下一交易日开盘执行", outside_band: "资产权重超出容忍带", band_breach: "资产权重超出容忍带", trade_constraints: "触发规则，但成交条件不足", initial_allocation: "首次配置建仓", initial: "首次配置建仓", within_band: "权重处于容忍带内" };
  return labels[payload.rebalance_reason] || REASON_NAMES[payload.rebalance_reason] || payload.rebalance_reason || "旧记录未保存详细触发原因";
}

function selectRebalanceEvent(date) {
  const events = currentRebalanceRecords.filter((event) => event.rebalance_date === date && rebalanceActionLabel(event.payload) !== "带内，无需调仓");
  if (!events.length) return;
  selectedTradeDate = date;
  renderTradeRecords();
  const host = $("rebalanceEventDetail");
  const trades = currentTradeRecords.filter((row) => row.trade_date === date);
  // Trade amounts retain their original currencies; the event turnover and fees are CNY.
  const amounts = new Map();
  trades.forEach((trade) => {
    const currency = trade.currency || "CNY";
    const item = amounts.get(currency) || { buy: 0, sell: 0 };
    const side = String(trade.side).toLowerCase();
    if (side === "buy") item.buy += Number(trade.gross_amount || 0);
    if (side === "sell") item.sell += Number(trade.gross_amount || 0);
    amounts.set(currency, item);
  });
  const amountText = [...amounts].map(([currency, v]) => `${CURRENCY_NAMES[currency] || currency}：买入 ${fmtMoney(v.buy)} / 卖出 ${fmtMoney(v.sell)}`).join("；") || "当日没有可展示的逐笔成交";
  host.hidden = false;
  host.innerHTML = `<div class="event-detail-heading"><h3>${escapeHtml(date)} 调仓详情</h3><button type="button" class="button button-secondary" data-clear-event>清除事件与日期筛选</button></div><p>${escapeHtml(amountText)}</p>${events.map((event) => {
    const weights = eventWeightRows(event);
    return `<div class="event-detail-entry"><p><strong>${escapeHtml(rebalanceActionLabel(event.payload))}</strong> · 原因：${escapeHtml(rebalanceEventReason(event.payload))} · 决策日 ${escapeHtml(event.payload?.decision_date || date)}</p><p>成交金额 ￥${fmtMoney(event.turnover_cny)} · 手续费 ￥${fmtMoney(event.fee_cny)}</p>${weights ? `<details open><summary>成交前后权重</summary><div class="event-weight-grid"><span>标的</span><span>成交前</span><span>成交后</span>${weights.flatMap((row) => [assetName(row.symbol), fmtPct(row.before), fmtPct(row.after)]).map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div><small>现金与逆回购部分包含闲置现金、逆回购净值与应收股息，货币基金单独列出；按各时点组合总资产计算。</small></details>` : '<p class="field-help">旧记录未保存成交前后权重，重新运行该方案后可查看。此处不以收盘权重代替成交时点权重。</p>'}</div>`;
  }).join("")}<button type="button" class="button button-secondary" data-show-event-trades>查看当天 ${trades.length} 笔交易</button><p id="eventChartPosition" class="field-help"></p>`;
  host.querySelector("[data-clear-event]")?.addEventListener("click", clearRebalanceEvent);
  host.querySelector("[data-show-event-trades]")?.addEventListener("click", () => { selectRecordPanel("tradesPanel"); $("tradesPanel")?.scrollIntoView({ block: "start", behavior: "smooth" }); });
  const timedCharts = ["assetChart", "returnChart", "dailyReturnChart", "drawdownChart", "weightChart", "comparisonChart"];
  const chartId = timedCharts.includes(activeChartId) ? activeChartId : "assetChart";
  selectChart(chartId);
  focusRebalanceChart(date, chartId);
  host.scrollIntoView?.({ block: "start", behavior: "smooth" });
  host.focus?.({ preventScroll: true });
}


async function focusRebalanceChart(date, chartId) {
  const version = ++eventFocusVersion, runId = currentRunId, resultVersion = resultRequestVersion;
  const isCurrent = () => version === eventFocusVersion && runId === currentRunId && resultVersion === resultRequestVersion && selectedTradeDate === date;
  if ($("eventChartPosition")) $("eventChartPosition").textContent = "正在定位调仓执行日…";
  try {
    if (!currentChartSeries.some((row) => row.trade_date === date)) {
      const response = await api(`/api/backtest/${encodeURIComponent(runId)}/chart-series?focus_date=${encodeURIComponent(date)}`);
      if (!isCurrent()) return;
      const series = computeSeriesMetrics(expandChartSeries(response));
      if (!series.some((row) => row.trade_date === date)) throw new Error("返回图表仍未包含该执行日，请重试");
      renderCharts(series);
    }
    if (!window.echarts) {
      await loadChartLibrary();
      if (!isCurrent()) return;
      renderCharts(currentChartSeries);
    }
    if (!isCurrent()) return;
    window.requestAnimationFrame(() => {
      if (!isCurrent()) return;
      applyChartOption(chartId);
      const dates = currentChartSeries.map((row) => row.trade_date), index = dates.indexOf(date), chart = charts[chartId];
      if (!chart || index < 0) { showEventChartRetry(date, chartId, "图表尚未就绪，可重试精确定位。"); return; }
      chart.dispatchAction({ type: "dataZoom", startValue: dates[Math.max(0, index - 15)], endValue: dates[Math.min(dates.length - 1, index + 15)] });
      chart.setOption({ series: [{ markLine: { symbol: "none", label: { formatter: date }, data: [{ xAxis: date }], lineStyle: { color: "#a96b26", width: 2 } } }] });
      chart.dispatchAction({ type: "showTip", seriesIndex: 0, dataIndex: index });
      if ($("eventChartPosition")) $("eventChartPosition").textContent = `图表已精确定位 ${date}；已筛选同一执行日的交易。`;
    });
  } catch (error) { if (isCurrent()) showEventChartRetry(date, chartId, `精确日期定位失败：${humanizeError(error.message)}。交易详情和日期筛选仍保留。`); }
}

function showEventChartRetry(date, chartId, message) {
  const host = $("eventChartPosition");
  if (!host) return;
  host.innerHTML = `${escapeHtml(message)} <button type="button" class="button button-secondary" data-retry-event-focus>重试定位当天</button>`;
  host.querySelector("[data-retry-event-focus]")?.addEventListener("click", () => focusRebalanceChart(date, chartId));
}

function clearRebalanceEvent() {
  eventFocusVersion += 1;
  selectedTradeDate = null;
  if ($("rebalanceEventDetail")) $("rebalanceEventDetail").hidden = true;
  Object.values(charts).forEach((chart) => { chart.setOption?.({ series: [{ markLine: { data: [] } }] }); chart.dispatchAction?.({ type: "hideTip" }); });
  renderTradeRecords();
}

function historyMetadataMarkup(entry) {
  const metadata = entry.metadata || {};
  return `${metadata.favorite ? '<span class="favorite-badge">已收藏</span>' : ""}${metadata.note ? `<p class="history-note">${escapeHtml(metadata.note)}</p>` : ""}`;
}

function historyMetadataActions(entry) {
  const id = escapeHtml(entryRunId(entry));
  return `<button type="button" data-history-metadata="${id}">编辑名称备注</button><button type="button" data-history-favorite="${id}" aria-pressed="${Boolean(entry.metadata?.favorite)}">${entry.metadata?.favorite ? "取消收藏" : "收藏方案"}</button><button type="button" data-history-copy="${id}">载入参数</button>`;
}

function historyActionsMarkup(entry, source, compareLabel) {
  const id = escapeHtml(entryRunId(entry));
  const panelId = `${source}-actions-${id}`;
  return `<div class="history-item-actions">
    <button type="button" class="history-view-result" data-${source}-replay="${id}">查看结果</button>
    <button type="button" data-${source}-compare="${id}" aria-pressed="${compareLabel === "取消对比"}">${compareLabel}</button>
    <button type="button" class="history-more-toggle" aria-expanded="false" aria-controls="${panelId}">更多</button>
    <div id="${panelId}" class="history-item-secondary" hidden>
      ${historyMetadataActions(entry)}<button type="button" class="danger" data-${source}-delete="${id}">删除记录</button>
    </div>
  </div>`;
}

function bindHistoryMetadataActions(host) {
  host.querySelectorAll(".history-item-actions").forEach((actions) => {
    const toggle = actions.querySelector(".history-more-toggle");
    const panel = actions.querySelector(".history-item-secondary");
    toggle.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      toggle.setAttribute("aria-expanded", String(!panel.hidden));
    });
    actions.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || panel.hidden) return;
      event.preventDefault(); event.stopPropagation();
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      toggle.focus();
    });
  });
  host.querySelectorAll("[data-history-metadata]").forEach((button) => button.addEventListener("click", () => openMetadataEditor(button.dataset.historyMetadata, button)));
  host.querySelectorAll("[data-history-favorite]").forEach((button) => button.addEventListener("click", async () => {
    const id = button.dataset.historyFavorite, entry = archiveEntries().find((item) => entryRunId(item) === id);
    button.disabled = true;
    try { await updateHistoryMetadata(id, { favorite: !entry?.metadata?.favorite }); }
    catch (error) { setMessage(`收藏失败：${humanizeError(error.message)}`, true); button.disabled = false; }
  }));
  host.querySelectorAll("[data-history-copy]").forEach((button) => button.addEventListener("click", () => copySavedRunToDraft(button.dataset.historyCopy)));
}

async function updateHistoryMetadata(runId, changes) {
  const identityAtRequest = identityVersion;
  const result = await api(`/api/backtest/${encodeURIComponent(runId)}/metadata`, { method: "POST", body: JSON.stringify(changes) });
  if (identityAtRequest !== identityVersion) return null;
  [runHistory, leaderboardHistory].forEach((entries) => entries.forEach((entry) => { if (entryRunId(entry) === runId) entry.metadata = result.metadata; }));
  renderRunHistory(); renderLeaderboard(leaderboardHistory);
  return result.metadata;
}

function openMetadataEditor(runId, returnFocus) {
  const entry = archiveEntries().find((item) => entryRunId(item) === runId);
  if (!entry) return;
  metadataEditingRunId = runId;
  metadataReturnFocus = returnFocus || document.activeElement;
  $("metadataName").value = entry.metadata?.name || "";
  $("metadataNote").value = entry.metadata?.note || "";
  $("metadataFavorite").checked = Boolean(entry.metadata?.favorite);
  $("metadataError").textContent = "";
  $("saveMetadata").disabled = false;
  $("metadataDialog").showModal();
  $("metadataName").focus();
}

function closeMetadataEditor() {
  $("metadataDialog")?.close();
  metadataEditingRunId = null;
  // The edited card may have been re-rendered; keep keyboard focus in the archive.
  if (metadataReturnFocus?.isConnected) metadataReturnFocus.focus();
  else $("historySearch")?.focus();
}

async function saveMetadata(event) {
  event?.preventDefault();
  const runId = metadataEditingRunId;
  if (!runId) return;
  const changes = { name: $("metadataName").value.trim(), note: $("metadataNote").value.trim(), favorite: $("metadataFavorite").checked };
  if (changes.name.length > 80 || changes.note.length > 1000) { $("metadataError").textContent = "名称最多 80 字，备注最多 1000 字。"; return; }
  $("saveMetadata").disabled = true;
  try {
    await updateHistoryMetadata(runId, changes);
    if (metadataEditingRunId === runId) { closeMetadataEditor(); showToast("方案信息已保存"); }
  } catch (error) {
    if (metadataEditingRunId === runId) $("metadataError").textContent = `保存失败：${humanizeError(error.message)}`;
  } finally { if (metadataEditingRunId === runId) $("saveMetadata").disabled = false; }
}

async function copySavedRunToDraft(runId) {
  if (runInProgress) { showToast("请等待当前回测完成后再替换草稿"); return; }
  if (!window.confirm("将这份已保存方案载入编辑区？当前未运行的参数修改将被替换，显示结果保持不变。")) return;
  const version = ++draftLoadVersion, identityAtRequest = identityVersion;
  try {
    const entry = await api(`/api/backtest/${encodeURIComponent(runId)}`);
    if (version !== draftLoadVersion || identityAtRequest !== identityVersion || runInProgress) return;
    config = structuredClone(entry.config);
    // Old saved configs can lack the public cash-option catalogue.
    config.repo_options ||= defaultConfigSnapshot?.repo_options || [];
    renderControls(); setHistoryPanel(false); setParameterPanel(true);
    setMessage("已将保存参数载入编辑区。点击运行回测生成当前版本结果，再开始研究。");
  } catch (error) { if (version === draftLoadVersion && identityAtRequest === identityVersion) setMessage(`载入参数失败：${humanizeError(error.message)}`, true); }
}

function commonDateRange(a, b) {
  const start = [a.config?.start_date, b.config?.start_date].filter(Boolean).sort().at(-1);
  const end = [a.config?.end_date, b.config?.end_date].filter(Boolean).sort()[0];
  return start && end && start < end ? { start_date: start, end_date: end } : null;
}

function comparisonNumber(value) {
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 8 });
}

// Keep fee comparisons at the leaf level so a single changed rate stays readable.
const FEE_DIFFERENCE_FIELDS = {
  "cn_etf.commission_rate": ["境内基金佣金率", "percent", "% / 边"],
  "cn_etf.min_commission_cny": ["境内基金最低佣金", "number", "元 / 笔"],
  "cn_etf.exchange_handling_rate": ["境内基金交易经手费率", "percent", "% / 边"],
  "cn_etf.include_exchange_in_commission": ["佣金是否包含交易经手费", "boolean"],
  "cn_etf.stamp_tax_rate": ["境内基金印花税率", "percent", "%"],
  "cn_etf.transfer_fee_rate": ["境内基金过户费率", "percent", "%"],
  "repo.investor_commission_rate": ["逆回购佣金率", "percent", "%"],
  "repo.fee_cap_cny": ["逆回购固定费用上限", "cap", "元"],
  "repo.lot_size_cny": ["逆回购最小交易金额", "number", "元"],
  "ibkr_us_etf.plan": ["美国券商费率类型", "plan"],
  "ibkr_us_etf.fixed_per_share_usd": ["美股固定佣金（每股）", "number", "美元 / 股"],
  "ibkr_us_etf.fixed_min_usd": ["美股固定最低佣金", "number", "美元 / 笔"],
  "ibkr_us_etf.fixed_max_trade_pct": ["美股固定佣金占成交额上限", "percent", "%"],
  "ibkr_us_etf.tiered_per_share_usd": ["美股阶梯佣金（每股）", "number", "美元 / 股"],
  "ibkr_us_etf.tiered_min_usd": ["美股阶梯最低佣金", "number", "美元 / 笔"],
  "ibkr_us_etf.lite_commission_usd": ["美股免佣类型的佣金", "number", "美元 / 笔"],
  "ibkr_us_etf.sec_transaction_fee_rate": ["美股卖出 SEC 交易费率", "percent", "%"],
  "ibkr_us_etf.finra_taf_per_share_usd": ["美股卖出 FINRA 活动费（每股）", "number", "美元 / 股"],
  "ibkr_us_etf.finra_taf_cap_usd": ["美股卖出 FINRA 活动费上限", "number", "美元 / 笔"],
  "fx.bank_out_spread_bps": ["出金购汇点差", "number", "基点"],
  "fx.bank_in_spread_bps": ["入金结汇点差", "number", "基点"],
  "fx.outbound_wire_fee_cny": ["出境汇款费", "number", "元 / 笔"],
  "fx.inbound_wire_fee_cny": ["入境汇款费", "number", "元 / 笔"],
  "fx.ibkr_auto_fx_markup": ["美国券商自动换汇加价", "percent", "%"],
  "fx.use_ibkr_auto_fx": ["使用美国券商自动换汇", "boolean"],
  "hk_connect_etf.broker_commission_rate": ["港股通佣金率", "percent", "% / 边"],
  "hk_connect_etf.min_broker_commission_hkd": ["港股通最低佣金", "number", "港元 / 笔"],
  "hk_connect_etf.trading_fee_rate": ["港股通交易费率", "percent", "% / 边"],
  "hk_connect_etf.transaction_levy_rate": ["港股通交易征费率", "percent", "% / 边"],
  "hk_connect_etf.afrc_transaction_levy_rate": ["港股通会财局征费率", "percent", "% / 边"],
  "hk_connect_etf.stock_settlement_fee_rate": ["港股通股份交收费率", "percent", "% / 边"],
  "hk_connect_etf.min_stock_settlement_fee_hkd": ["港股通最低股份交收费", "number", "港元 / 笔"],
  "hk_connect_etf.max_stock_settlement_fee_hkd": ["港股通股份交收费上限", "number", "港元 / 笔"],
  "hk_connect_etf.stamp_duty_rate": ["港股通印花税率", "percent", "%"],
  "hk_connect_etf.portfolio_fee_annual_rate": ["港股通组合费年率", "percent", "% / 年"],
  "hk_connect_etf.fx_spread_bps": ["港股通汇兑点差", "number", "基点"],
  "hk_connect_etf.lot_size": ["港股通每手股数", "number", "股 / 手"],
  "tax.cn_fund_dividend_tax_rate": ["境内基金分红税率", "percent", "%"],
  "tax.us_dividend_withholding_rate": ["美国分红预扣税率", "percent", "%"],
  "tax.hk_dividend_withholding_rate": ["港股分红预扣税率", "percent", "%"],
  "tax.us_capital_gain_tax_rate": ["美国资本利得税率", "percent", "%"],
};

function feeDifferenceRows(left = {}, right = {}) {
  const flatten = (object, prefix = "", result = {}) => {
    for (const [key, value] of Object.entries(object || {})) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (value && typeof value === "object") flatten(value, path, result);
      else result[path] = value;
    }
    return result;
  };
  const a = flatten(left), b = flatten(right);
  const format = (value, type, unit = "") => {
    if (value == null) return "未保存";
    if (type === "plan") return ibkrPlanLabel(value);
    if (type === "boolean" || typeof value === "boolean") return value ? "是" : "否";
    if (type === "cap" && Number(value) === 0) return "不设固定金额上限";
    if (type === "percent") return `${comparisonNumber(Number(value) * 100)}${unit}`;
    if (["number", "cap"].includes(type)) return `${comparisonNumber(value)} ${unit}`;
    return String(value);
  };
  return [...new Set([...Object.keys(a), ...Object.keys(b)])].filter((key) => a[key] !== b[key]).map((key) => {
    const [label, type, unit] = FEE_DIFFERENCE_FIELDS[key] || [`其他费用 · ${key}`, "text", ""];
    return [label, format(a[key], type, unit), format(b[key], type, unit)];
  });
}

function configDifferenceMarkup(left = {}, right = {}) {
  const labels = { rolling_window_years: "滚动窗口年数", rebalance_month_analysis_enabled: "年度月份研究", allow_fractional_us_shares: "允许美股碎股", initial_capital_cny: "初始资金", start_date: "请求开始日期", end_date: "请求结束日期", rebalance_frequency: "检查频率", annual_rebalance_month: "年度月份", rebalance_band: "相对容忍带", rebalance_to_target: "恢复目标权重", monthly_spend_cny: "月消费", monthly_spend_annual_growth: "消费年增幅", repo_target_mode: "现金目标模式", repo_fixed_target_cny: "固定现金金额", repo_fixed_target_ratio: "固定现金比例", repo_symbol: "现金品种", dip_buy_enabled: "补仓开启", dip_buy_drawdown: "补仓下跌阈值", dip_buy_total_parts: "补仓份数", dip_buy_level_mode: "补仓档位方式", dip_buy_cost_basis_mode: "补仓成本基准", dip_buy_recovery_sell_enabled: "回本卖出", dip_buy_asset_cap_enabled: "单资产额度上限", dip_buy_asset_cap_ratio: "补仓上限比例", dip_buy_blackout_enabled: "补仓静默期", dip_buy_blackout_months: "静默月数" };
  const value = (key, item) => {
    if (item == null) return "未保存";
    if (typeof item === "boolean") return item ? "是" : "否";
    if (key === "rebalance_frequency") return REBALANCE_FREQUENCY_NAMES[item] || item;
    if (key === "repo_target_mode") return ["fixed_bucket", "residual_weight"].includes(item) ? repoModeLabel(item) : item;
    if (key === "dip_buy_level_mode") return { fixed: "每档补 1 份", multiplier: "第 N 档补 N 份" }[item] || item;
    if (key === "dip_buy_cost_basis_mode") return { current_average: "目前持仓成本", initial: "最初成本" }[item] || item;
    if (key === "repo_symbol") {
      const options = [...(left.repo_options || []), ...(right.repo_options || []), ...(defaultConfigSnapshot?.repo_options || []), ...(config?.repo_options || [])];
      const option = options.find((entry) => entry.symbol === item);
      return option ? `${option.name}（${item}）` : assetName(item);
    }
    if (/ratio|band|drawdown|annual_growth/.test(key)) return fmtPct(item);
    if (["initial_capital_cny", "monthly_spend_cny", "repo_fixed_target_cny"].includes(key)) return `${comparisonNumber(item)} 元`;
    if (key === "rolling_window_years") return `${item} 年`;
    if (key === "annual_rebalance_month") return `${item} 月`;
    if (key === "dip_buy_blackout_months") return `${item} 个月`;
    if (key === "dip_buy_total_parts") return `${item} 份`;
    return String(item);
  };
  const rows = Object.entries(labels).filter(([key]) => (left[key] ?? (key === "monthly_spend_annual_growth" ? 0 : null)) !== (right[key] ?? (key === "monthly_spend_annual_growth" ? 0 : null))).map(([key, label]) => [label, value(key, left[key]), value(key, right[key])]);
  const assets = (cfg) => (cfg.assets || []).filter((a) => a.enabled).map((a) => `${a.name || a.symbol || a.key} ${fmtPct(a.target_weight)}`).join(" / ") || "仅现金";
  if (assets(left) !== assets(right)) rows.push(["资产配置", assets(left), assets(right)]);
  rows.push(...feeDifferenceRows(left.fees, right.fees));
  return rows.length ? `<div class="comparison-grid">${rows.flatMap((row) => row.map((item) => `<span>${escapeHtml(item)}</span>`)).join("")}</div>` : '<p>两份记录没有主策略参数差异。</p>';
}

async function runCommonComparison() {
  const [left, right] = comparisonEntries();
  if (!left || !right || currentRunId === comparisonRunId) return;
  const range = commonDateRange(left, right);
  if (!range) return;
  const key = `${entryRunId(left)}:${entryRunId(right)}`;
  if (commonComparison?.busy) { showToast("同期比较正在执行，请等待完成"); return; }
  const version = ++commonComparisonVersion, identityAtRequest = identityVersion;
  commonComparison = { key, busy: true, message: "正在读取两份保存参数…" }; renderHistoryComparison();
  try {
    const entries = [];
    for (const [index, original] of [left, right].entries()) {
      const saved = await api(`/api/backtest/${encodeURIComponent(entryRunId(original))}`);
      if (version !== commonComparisonVersion || identityAtRequest !== identityVersion) return;
      const fixedConfig = { ...saved.config, ...range, rebalance_month_analysis_enabled: false };
      commonComparison.message = `正在计算第 ${index + 1} / 2 组：${range.start_date} 至 ${range.end_date}`; renderHistoryComparison();
      const job = await api("/api/backtest/start", { method: "POST", body: JSON.stringify({ config: compactConfigForRequest(fixedConfig), client_request_id: createClientRequestId() }), retry: true });
      if (version !== commonComparisonVersion || identityAtRequest !== identityVersion) return;
      const result = await waitForBacktestJob(job.job_id, (message) => {
        if (version !== commonComparisonVersion || identityAtRequest !== identityVersion || commonComparison?.key !== key) return;
        commonComparison.message = `第 ${index + 1} / 2 组 · ${message}`;
        renderHistoryComparison();
      });
      if (version !== commonComparisonVersion || identityAtRequest !== identityVersion) return;
      entries.push({ run_id: result.run_id, config: fixedConfig, summary: result.summary, metadata: original.metadata });
    }
    if (version !== commonComparisonVersion) return;
    commonComparison = { key, busy: false, entries, message: "两组同期回测已完成并保存。实际交易区间见表格；编辑草稿与当前图表保持不变。" };
    scheduleArchiveRefresh({ includeLeaderboard: false });
  } catch (error) {
    if (version === commonComparisonVersion) commonComparison = { key, busy: false, message: `同期计算未完成：${humanizeError(error.message)}。可重试；已完成的回测仍保留在历史中。` };
  }
  renderHistoryComparison();
}

const RESEARCH_UI = {
  rebalance_grid: { prefix: "rebalanceResearch", start: "startRebalanceResearch", cancel: "cancelRebalanceResearch", resume: "resumeRebalanceResearch", name: "调仓研究" },
  withdrawal_stress: { prefix: "withdrawalResearch", start: "startWithdrawalResearch", cancel: "cancelWithdrawalResearch", resume: "resumeWithdrawalResearch", name: "消费研究" },
};
function researchKey(runId, kind) { return `${runId}:${kind}`; }
function researchActive(task) { return ["starting", "queued", "running"].includes(task?.status); }
function parseResearchNumbers(text, label, { min = 0, max = Infinity, integer = false, scale = 1 } = {}) {
  const tokens = String(text || "").trim().split(/[,，;；\s]+/).filter(Boolean);
  if (!tokens.length) throw new Error(`请填写${label}`);
  const values = tokens.map((token) => Number(token));
  if (values.some((value) => !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value)))) throw new Error(`${label}须为 ${min} 至 ${Number.isFinite(max) ? max : "有效上限"} 之间${integer ? "的整数" : "的数字"}`);
  return [...new Set(values)].map((value) => value * scale);
}
function buildResearchRequest(kind, runId = currentRunId) {
  if (!runId) throw new Error("请先运行或查看一份已保存的结果");
  let request, count;
  if (kind === "rebalance_grid") {
    const frequencies = [...document.querySelectorAll('#rebalanceResearchFrequencies input:checked')].map((input) => input.value);
    if (!frequencies.length) throw new Error("请至少选择一种检查频率");
    const bands = parseResearchNumbers($("rebalanceResearchBands").value, "容忍带", { max: 100, scale: .01 });
    count = frequencies.length * bands.length;
    request = { run_id: runId, kind, frequencies, bands };
  } else {
    const monthly_spends = parseResearchNumbers($("withdrawalResearchSpends").value, "月消费金额", { max: 1e9 });
    const annual_growth_rates = parseResearchNumbers($("withdrawalResearchGrowthRates").value, "消费年增幅", { max: 50, scale: .01 });
    const start_years = parseResearchNumbers($("withdrawalResearchStartYears").value, "开始年份", { min: 1900, max: 2100, integer: true });
    count = monthly_spends.length * annual_growth_rates.length * start_years.length;
    request = { run_id: runId, kind, monthly_spends, annual_growth_rates, start_years };
  }
  if (count > 24) throw new Error(`共 ${count} 个场景，超过 24 个；请减少选项`);
  return { request, count };
}

function updateResearchCounts() {
  for (const [kind, ui] of Object.entries(RESEARCH_UI)) {
    if (!$(ui.prefix + "Count")) continue;
    try { const { count } = buildResearchRequest(kind, currentRunId || "preview"); $(ui.prefix + "Count").textContent = `${count} 个场景 / 最多 24 个`; }
    catch (error) { $(ui.prefix + "Count").textContent = error.message; }
  }
}

function rebalancePositiveValue(row) {
  return { kind: "rebalance-positive", raw: row.rebalance_evaluated_count > 0 ? row.rebalance_positive_ratio : null,
    count: row.rebalance_evaluated_count, positive: row.rebalance_positive_count, negative: row.rebalance_negative_count, flat: row.rebalance_flat_count };
}

function rebalancePositiveMarkup(value) {
  if (value.count == null) return '<span class="research-win-rate is-muted">—<small>尚未记录</small></span>';
  if (!value.count) return '<span class="research-win-rate is-muted">—<small>无已完成调仓周期</small></span>';
  if (value.raw == null || !Number.isFinite(Number(value.raw))) return '<span class="research-win-rate is-muted">—<small>明细不可用</small></span>';
  return `<span class="research-win-rate"><strong>${fmtPct(value.raw)}</strong><small>盈利 ${Number(value.positive)} · 亏损 ${Number(value.negative)} · 持平 ${Number(value.flat)}<br>共 ${Number(value.count)} 个周期</small></span>`;
}

function renderResearch() {
  const baseline = $("researchBaseline");
  if (!baseline) return;
  baseline.textContent = currentRunId && currentRunConfig ? `基准：已保存结果 ${currentRunId.slice(0, 8)} · ${currentRunConfig.start_date} 至 ${currentRunConfig.end_date} · ${REBALANCE_FREQUENCY_NAMES[currentRunConfig.rebalance_frequency] || currentRunConfig.rebalance_frequency}检查 · 当前编辑区的修改不参与本次研究。` : "先运行或查看一份已保存结果，再开始研究。";
  if (currentRunConfig && $("withdrawalResearchStartYears") && !$("withdrawalResearchStartYears").value) $("withdrawalResearchStartYears").value = String(currentRunConfig.start_date || "").slice(0, 4);
  for (const [kind, ui] of Object.entries(RESEARCH_UI)) {
    const task = researchTasks.get(researchKey(currentRunId, kind));
    const active = researchActive(task);
    if ($(ui.start)) $(ui.start).disabled = !currentRunId || active;
    if ($(ui.cancel)) { $(ui.cancel).hidden = !task?.job_id || !active; $(ui.cancel).disabled = Boolean(task?.cancelling); }
    if ($(ui.resume)) $(ui.resume).hidden = !task?.requestError || !task?.job_id || !active;
    const status = $(ui.prefix + "Status");
    if (status) {
      const labels = { starting: "正在提交", queued: "排队中", running: "计算中", completed: "已完成", failed: "失败", cancelled: "已取消", idle: "等待可用" };
      status.textContent = task ? `${labels[task.status] || task.status} · ${task.completed || 0} / ${task.total || 0} 个场景${task.requestError ? ` · ${task.requestError}` : task.error ? ` · ${humanizeError(task.error)}` : ""}${task.rows?.length ? ` · 成功 ${task.rows.filter((row) => row.status === "success").length} / 失败 ${task.rows.filter((row) => row.status === "failed").length}` : ""}` : "当前保存方案尚无研究结果。选择场景后运行。";
    }
    const progress = $(ui.prefix + "Progress");
    if (progress) { progress.hidden = !task; progress.max = Math.max(task?.total || 1, 1); progress.value = task?.completed || 0; }
    if ($(ui.prefix + "Rebase")) $(ui.prefix + "Rebase").hidden = !task?.needsRebase;
    if ($(ui.prefix + "Existing")) $(ui.prefix + "Existing").hidden = !task?.existingRunId;
    const metric = (raw) => ({ kind: "metric", raw, format: "percent" });
    const money = (raw) => ({ kind: "money", raw });
    const rows = (task?.rows || []).map((row, index) => ({
      场景: kind === "rebalance_grid" ? `${REBALANCE_FREQUENCY_NAMES[row.inputs?.rebalance_frequency] || "—"} · ${fmtPct(row.inputs?.rebalance_band)}` : `${fmtMoney(row.inputs?.monthly_spend_cny)}元/月 · ${fmtPct(row.inputs?.monthly_spend_annual_growth)}年增 · ${row.inputs?.start_year}起`, 状态: row.status === "success" ? "完成" : "失败", 检查频率: REBALANCE_FREQUENCY_NAMES[row.inputs?.rebalance_frequency] || "—", 容忍带: metric(row.inputs?.rebalance_band), 月消费: money(row.inputs?.monthly_spend_cny), 消费年增幅: metric(row.inputs?.monthly_spend_annual_growth), 开始年份: row.inputs?.start_year == null ? "—" : { kind: "number", raw: row.inputs.start_year, decimals: 0 },
      盈利调仓占比: rebalancePositiveValue(row),
      实际区间: row.start_date && row.end_date ? `${row.start_date} 至 ${row.end_date}` : "—", 年化收益: metric(row.annualized_return), 最大回撤: metric(row.max_drawdown), 总费用: money(row.total_fees_cny), 实际调仓次数: row.rebalance_trade_count == null ? "—" : { kind: "number", raw: row.rebalance_trade_count, decimals: 0 }, 期末资产: money(row.final_asset_cny), 计划提取: money(row.total_planned_spend_cny), 实际提取: money(row.total_spend_cny), 提取缺口: money(row.total_spend_shortfall_cny), 首次不足: row.first_spend_shortfall_date || (row.status === "success" ? "未发生" : "—"), 不足次数: row.spend_shortfall_count == null ? "—" : { kind: "number", raw: row.spend_shortfall_count, decimals: 0 }, 补仓适用性: row.dip_buy_applicability_note || (row.dip_buy_active ? "启用" : "未启用"), 说明: row.error || "",
    }));
    renderResearchSupplement(kind, task);
    const columns = kind === "rebalance_grid" ? ["场景", "盈利调仓占比", "年化收益", "最大回撤", "总费用", "实际调仓次数", "期末资产", "实际区间", "状态", "补仓适用性", "说明"] : ["场景", "首次不足", "期末资产", "提取缺口", "不足次数", "计划提取", "实际提取", "年化收益", "最大回撤", "总费用", "实际区间", "状态", "说明"];
    if ($(ui.prefix + "Table")) renderTable(ui.prefix + "Table", rows.length ? columns : [], rows, { pageSize: 24, sortableColumns: columns });
  }
  updateResearchCounts();
}


function renderResearchSupplement(kind, task) {
  const ui = RESEARCH_UI[kind], method = $(ui.prefix + "Methodology"), cards = $(ui.prefix + "Cards");
  if (method) {
    const fields = kind === "rebalance_grid" ? ["fixed_conditions", "rebalance_positive_ratio", "annual_only_rules"] : ["fixed_conditions", "withdrawal_growth", "start_dates", "returns"];
    const notes = typeof task?.methodology === "string" ? [task.methodology] : fields.map((key) => task?.methodology?.[key]).filter(Boolean);
    method.innerHTML = notes.length ? `<details><summary>计算方法与适用范围</summary>${notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("")}</details>` : "";
  }
  if (!cards) return;
  const pct = (value) => value == null ? "—" : fmtPct(value), money = (value) => value == null ? "—" : `￥${fmtMoney(value)}`;
  cards.innerHTML = (task?.rows || []).map((row) => {
    const input = row.inputs || {};
    const title = kind === "rebalance_grid" ? `${REBALANCE_FREQUENCY_NAMES[input.rebalance_frequency] || "—"} · 容忍带 ${pct(input.rebalance_band)}` : `每月 ${fmtMoney(input.monthly_spend_cny)} 元 · 年增 ${pct(input.monthly_spend_annual_growth)} · ${input.start_year} 起`;
    if (row.status !== "success") return `<article class="research-card"><h4>${escapeHtml(title)}</h4><p class="negative">此场景失败：${escapeHtml(row.error || "未知错误")}</p></article>`;
    const metrics = kind === "rebalance_grid" ? [["年化收益",pct(row.annualized_return)],["最大回撤",pct(row.max_drawdown)],["总费用",money(row.total_fees_cny)],["实际调仓",`${row.rebalance_trade_count ?? "—"} 次`]] : [["首次不足",row.first_spend_shortfall_date || "未发生"],["期末资产",money(row.final_asset_cny)],["提取缺口",money(row.total_spend_shortfall_cny)],["不足次数",`${row.spend_shortfall_count ?? "—"} 次`]];
    return `<article class="research-card"><h4>${escapeHtml(title)}</h4>${kind === "rebalance_grid" ? `<div class="research-card-win"><span>盈利调仓占比</span>${rebalancePositiveMarkup(rebalancePositiveValue(row))}</div>` : ""}<div class="research-card-metrics">${metrics.map(([label,value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div><small>${escapeHtml(row.start_date || "—")} 至 ${escapeHtml(row.end_date || "—")}</small></article>`;
  }).join("");
}

async function startResearch(kind) {
  const runId = currentRunId;
  const ui = RESEARCH_UI[kind];
  if (researchActive(researchTasks.get(researchKey(runId, kind)))) return;
  let built;
  try { built = buildResearchRequest(kind, runId); }
  catch (error) { $(ui.prefix + "Status").textContent = error.message; return; }
  const task = { identityVersion, run_id: runId, kind, status: "starting", total: built.count, completed: 0, rows: [], request: built.request, baseline: structuredClone(currentRunConfig) };
  researchTasks.set(researchKey(runId, kind), task); renderResearch();
  try {
    const job = await api("/api/research/start", { method: "POST", body: JSON.stringify(built.request) });
    if (task.identityVersion !== identityVersion) return;
    Object.assign(task, job);
    if (currentRunId === runId) renderResearch();
    await pollResearchTask(task);
  } catch (error) {
    if (task.identityVersion !== identityVersion) return;
    if (error.status === 429 && error.payload?.job_id) {
      try {
        const existing = await api(`/api/research/jobs/${encodeURIComponent(error.payload.job_id)}`);
        if (task.identityVersion !== identityVersion) return;
        if (["cancelled", "completed", "failed"].includes(existing.status)) {
          task.status = "idle"; task.error = "上一计算正在退出，请稍后再次点击运行。";
        } else if (existing.run_id === runId && existing.kind === kind) {
          Object.assign(task, existing); task.requestError = "已恢复这份基准的进行中任务";
          renderResearch(); await pollResearchTask(task); return;
        } else {
          const prior = { ...existing, identityVersion };
          researchTasks.set(researchKey(existing.run_id, existing.kind), prior);
          task.status = "idle"; task.error = "已有其他研究正在计算，可查看对应保存方案的进度或取消后再运行。"; task.existingRunId = existing.run_id;
          pollResearchTask(prior);
        }
        if (currentRunId === runId) renderResearch();
        return;
      } catch (lookupError) { task.requestError = `已有任务状态暂不可读取：${humanizeError(lookupError.message)}`; }
    }
    task.status = "failed"; task.needsRebase = error.status === 409;
    task.error = task.needsRebase ? "请先用已保存基准参数重新运行回测，再开展研究。可将基准载入编辑区后点击运行。" : humanizeError(error.message);
    if (currentRunId === runId) renderResearch();
  }
}

async function pollResearchTask(task) {
  if (task.polling || !task.job_id) return;
  task.polling = true; task.requestError = "";
  try {
    while (researchActive(task) && researchTasks.get(researchKey(task.run_id, task.kind)) === task) {
      await sleep(900);
      if ((task.identityVersion != null && task.identityVersion !== identityVersion) || researchTasks.get(researchKey(task.run_id, task.kind)) !== task) break;
      try {
        const response = await api(`/api/research/jobs/${encodeURIComponent(task.job_id)}`, { attempts: 2 });
        // A cancel response is authoritative; a GET started before cancellation cannot revive it.
        if (task.status === "cancelled" || (task.identityVersion != null && task.identityVersion !== identityVersion) || researchTasks.get(researchKey(task.run_id, task.kind)) !== task) break;
        Object.assign(task, response);
      } catch (error) { task.requestError = `进度读取失败：${humanizeError(error.message)}；可重试读取或取消。`; break; }
      if (currentRunId === task.run_id) renderResearch();
    }
  } finally { task.polling = false; if (currentRunId === task.run_id) renderResearch(); }
}

async function cancelResearch(kind) {
  const task = researchTasks.get(researchKey(currentRunId, kind));
  if (!task?.job_id || !researchActive(task) || task.cancelling) return;
  task.cancelling = true; renderResearch();
  try { const response = await api(`/api/research/jobs/${encodeURIComponent(task.job_id)}/cancel`, { method: "POST" }); if (task.identityVersion != null && task.identityVersion !== identityVersion) return; Object.assign(task, response); task.requestError = ""; }
  catch (error) { task.requestError = `取消失败：${humanizeError(error.message)}；请重试。`; }
  finally { task.cancelling = false; if (currentRunId === task.run_id) renderResearch(); }
}

function setupResearchInteractions() {
  $("openResearchSection")?.addEventListener("click", () => { if ($("researchSection")) $("researchSection").open = true; });
  for (const [kind, ui] of Object.entries(RESEARCH_UI)) {
    $(ui.start)?.addEventListener("click", () => startResearch(kind));
    $(ui.cancel)?.addEventListener("click", () => cancelResearch(kind));
    $(ui.resume)?.addEventListener("click", () => { const task = researchTasks.get(researchKey(currentRunId, kind)); if (task) pollResearchTask(task); });
    $(ui.prefix + "Rebase")?.addEventListener("click", () => copySavedRunToDraft(currentRunId));
    $(ui.prefix + "Existing")?.addEventListener("click", async () => { const task = researchTasks.get(researchKey(currentRunId, kind)); if (!task?.existingRunId) return; await replayHistoryRun(task.existingRunId); if ($("researchSection")) { $("researchSection").open = true; $("researchSection").scrollIntoView?.({ block: "start" }); } });
  }
  $("researchSection")?.addEventListener("input", updateResearchCounts);
  $("clearTradeDateFilter")?.addEventListener("click", () => { selectedTradeDate = null; renderTradeRecords(); });
  $("historyFavoritesOnly")?.addEventListener("change", async (event) => {
    favoritesOnly = event.target.checked;
    renderRunHistory(); renderLeaderboard(leaderboardHistory);
    try { await refreshRecentArchive(); } catch (error) { setMessage(`收藏列表读取失败：${humanizeError(error.message)}`, true); }
  });
  $("metadataForm")?.addEventListener("submit", saveMetadata);
  $("closeMetadata")?.addEventListener("click", closeMetadataEditor);
  $("cancelMetadata")?.addEventListener("click", closeMetadataEditor);
  $("metadataDialog")?.addEventListener("cancel", (event) => { event.preventDefault(); closeMetadataEditor(); });
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
  await ensureIdentity();
  renderRunHistory();
  renderInitialSummary();
  renderTable("rollingTable", [], []);
  renderTable("monthsTable", [], []);
  renderTable("rebalanceTable", [], []);
  renderTable("tradesTable", [], []);
  config = await api("/api/default-config");
  defaultConfigSnapshot = JSON.parse(JSON.stringify(config));
  renderControls();
  ["runBtn", "mobileRunBtn", "workspaceRunBtn"].forEach((id) => $(id)?.addEventListener("click", runBacktest));
  setRunBusy(false);
  $("runBtn").addEventListener("pointerenter", () => loadChartLibrary().catch(() => {}), { once: true });
  window.addEventListener("resize", queueChartResize);
  syncDrawerAccessibility();
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
