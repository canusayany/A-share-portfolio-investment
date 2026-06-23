# 跨市场 ETF + 国债逆回购组合回测系统实施方案

> 目标：实现一个单页面回测系统，用 100 万 RMB 初始资金，按可配置比例配置 IBKR 标普 500 ETF、512890、510300、518880 和国债逆回购；支持年度/半年度再平衡、月度 5000 元消费、基金启停、成立日前资金自动进入逆回购、收益/回撤/浮盈浮亏/资产曲线/收益率曲线/沪深 300 对比等指标。
>
> 说明：Tushare token 不写入代码和文档，使用环境变量 `TUSHARE_TOKEN`。本文只定义工程和计算口径，不构成投资建议或税务建议。

## 1. 默认组合与业务规则

### 1.1 默认资产

所有账户的净值统一换算成 CNY 计价。

| 账户 | 默认标的 | 默认权重 | 交易市场 | 默认启用 | 成立/可用日期 | 说明 |
|---|---:|---:|---|---|---:|---|
| IBKR 标普 500 | `VOO`，可改 `SPY`/`IVV` | 20% | US / IBKR | 是 | `VOO` 2010-09-07 | 默认选择低费率 `VOO`；若用户明确指定其他 ETF，仅替换 symbol 和费率元数据 |
| 红利低波 ETF | `512890.SH` | 8% | 上交所 | 是 | 2018-12-19 | 成立前目标资金进入国债逆回购 |
| 沪深 300 ETF | `510300.SH` | 12% | 上交所 | 是 | 2012-05-04 | 成立前目标资金进入国债逆回购 |
| 黄金 ETF | `518880.SH` | 10% | 上交所 | 是 | 2013-07-18 | 成立前目标资金进入国债逆回购 |
| 国债逆回购 | 默认 `204001` / GC001 | 剩余 50% | 上交所 | 固定启用 | 2012 起可用性由数据源校验 | 作为现金管理账户、月度消费来源、未启用/未成立基金的资金池 |

权重规则：

1. 用户可用滑块调整 4 个风险资产目标权重；国债逆回购权重自动等于 `100% - enabled_asset_targets_sum`。
2. 若某资产未启用，则其目标权重转入国债逆回购。
3. 若某资产在当前回测日期尚未成立或价格数据不可用，则其目标权重临时转入国债逆回购；下一个再平衡日再判断是否买入。
4. 系统应阻止风险资产权重合计超过 100%；UI 上可自动归一化或给出校验错误，建议 MVP 采用“超过 100% 禁止运行”。

### 1.2 回测区间

默认：

- 初始资金：`1,000,000 CNY`
- 开始日期：`2012-01-01`，前端滑动可调
- 结束日期：运行当天，默认 `2026-06-23`
- 再平衡频率：`yearly` 或 `semiannual`
- 再平衡容忍带：`±2 percentage points`
- 月度消费：每月 `5,000 CNY`

再平衡日期建议：

- 年度：每年第一个中国交易日。
- 半年度：每年 1 月和 7 月第一个中国交易日。
- 如果该日期美股不开市，US ETF 使用最近一个已知收盘价估值，实际交易顺延到下一个美股交易日；MVP 可先按共同估值日成交，后续加跨市场结算延迟。

### 1.3 月度消费规则

1. 每月第一个中国交易日，从国债逆回购/现金账户扣除 `monthly_spend_cny`。
2. 若当日可用现金不足，但有即将到期逆回购，先等待到期结算；仍不足则记录 `liquidity_shortfall`。
3. MVP 建议默认启用“强制流动性补足”：从超配最多的启用资产卖出补足消费现金，再扣款。
4. 收益率计算把消费视为外部现金流，不把消费扣款当成投资亏损。

### 1.4 国债逆回购滚动规则

MVP 采用 GC001 日度滚动：

1. 每个中国交易日处理完分红、到期、消费、再平衡后，将可用现金按 1000 元整数倍买入 GC001。
2. 逆回购利息在到期交收日入账。
3. 长假前实际占款天数应按交易所规则计算，而不是简单等于 1 天。
4. 若后续需要降低操作频率，可增加 `repo_tenor` 参数，支持 `204001/204002/204003/204004/204007/204014/204028/204091/204182`。

## 2. 数据源可行性结论

### 2.1 Tushare 5000 积分档是否够用

结论：国内 ETF 日线、基金复权因子、基金分红、沪深 300 指数日线在 5000 积分档内可做；美股日线不在积分权限内，需要单独开通或用公开数据源补足；国债逆回购历史建议用 AKShare / 东方财富公开数据补足。

依据：

- Tushare 权限表显示 5000 以上常规数据无总量上限、每分钟频次较高；同时港美股日线属于单独权限，不纳入积分权限。
- `fund_daily` 文档说明 ETF 日线可按代码和日期循环获取历史，总量不限制，5000 积分档可调。
- `fund_adj` 可获取基金复权因子，用于校验除息除权后的总收益口径。
- `index_daily` 可获取沪深 300 指数日线，用作对比基准。

### 2.2 数据源清单

| 数据 | 主数据源 | 备用/校验源 | 字段 | 单位 | 备注 |
|---|---|---|---|---|---|
| ETF 日线价格 | Tushare `fund_daily` | 东方财富 / AKShare | `open/high/low/close/pre_close/vol/amount` | 价格 CNY；成交量通常为手 | 使用未复权收盘价做真实持仓估值 |
| ETF 复权因子 | Tushare `fund_adj` | 通过分红事件自算 | `adj_factor` | 倍数 | 用于校验现金分红模拟后的总收益 |
| ETF 分红 | Tushare `fund_div` | 基金公告 / 东方财富 | `ex_date/pay_date/div_cash` | `div_cash` 为每份派息 CNY | 除息日形成应收，派息日入现金 |
| 基金成立日/费率 | Tushare `fund_basic` + 基金公司/东方财富 | 基金公告 | `found_date/management_fee/custodian_fee` | 年化百分比 | 管理费/托管费已体现在净值/价格，不重复扣 |
| 沪深 300 指数 | Tushare `index_daily`, code `000300.SH` | 中证指数官网 / 东方财富 | `close` | 点 | 作为同期基准，价格指数不含分红 |
| 国债逆回购历史 | AKShare `bond_buy_back_hist_em(symbol="204001")` | 东方财富行情 | `收盘` | 年化收益率，百分比 | Tushare 5000 档不覆盖该需求的稳定历史接口 |
| CNY/USD 汇率 | AKShare 中国货币网/人民币中间价接口 | FRED/ECB/yfinance `CNY=X` | CNY per USD | `RMB/USD` | 估值用中间价；实际汇兑用可配置银行点差 |
| US ETF 日线 | `yfinance` 或 Stooq CSV | Nasdaq / Alpha Vantage | `open/high/low/close/adj_close/dividends/splits` | USD | Tushare 美股日线需单独权限，MVP 用公开数据 |
| US ETF 费率 | Vanguard/发行商官网 | ETFDB/Morningstar | `expense_ratio` | 年化百分比 | 使用实际 ETF 价格时不重复扣管理费 |
| IBKR 佣金/汇兑 | IBKR 官网 | 用户实际账单 | commission / FX markup | USD 或百分比 | 必须做成配置项 |

### 2.3 数据不足时的处理

1. 国内 ETF 缺 `fund_div`：仍可用未复权价回测价格收益，但分红现金流不完整；应在数据状态页标红。
2. 国内 ETF 缺 `fund_adj`：不影响真实持仓现金分红模式，但少一个总收益校验。
3. US ETF 缺分红：可退化使用 `adj_close` 做总收益估值，但无法精确模拟现金分红和预扣税；应明确标记 `us_dividend_mode=adjusted_close_fallback`。
4. 汇率缺日线：用前值填充，最长可填充 10 个自然日；超过则停止回测。
5. 国债逆回购缺历史：优先用 AKShare / 东方财富公开数据补足；若仍缺失，则标记缺数据并阻止完整回测，不能用自生成模拟数据替代。

## 3. 费用、税费与分红口径

### 3.1 国内 ETF 交易费用

国内 ETF 在交易所二级市场买卖，通常无印花税。交易所对基金竞价交易收取经手费，券商端向用户收取佣金，具体佣金以用户券商合同/交割单为准。

建议配置：

```yaml
cn_etf_fee:
  commission_rate: 0.00025         # 默认万2.5，按用户券商修改
  min_commission_cny: 0.0          # 很多 ETF 佣金无5元最低，按实际修改
  exchange_handling_rate: 0.00004  # 上交所 ETF 经手费 0.004%，若佣金已包含则设为0
  include_exchange_in_commission: true
  stamp_tax_rate: 0.0
  transfer_fee_rate: 0.0
```

公式：

设：

- `A`：成交金额，单位 CNY
- `c_cn`：券商 ETF 佣金率，例如万 2.5 = `0.00025`
- `h_cn`：交易所经手费率，0.004% = `0.00004`
- `m_cn`：单笔最低佣金，单位 CNY
- `I_h`：经手费是否另计，另计为 1，已含在佣金为 0

换算：

- 万 2.5 = `2.5 / 10000 = 0.00025`，即 0.025%。
- 万 0.5 = `0.5 / 10000 = 0.00005`，即 0.005%。
- 0.004% = `0.004 / 100 = 0.00004`。

单笔费用：

```text
fee_cn_etf = max(A * c_cn, m_cn) + I_h * A * h_cn
```

如果用户券商佣金为“全包佣金”，`I_h=0`。

### 3.2 国内 ETF 管理费/托管费

当前元数据默认：

| 标的 | 管理费 | 托管费 | 合计 | 处理方式 |
|---|---:|---:|---:|---|
| 512890.SH | 0.50%/年 | 0.10%/年 | 0.60%/年 | 已在基金净值中每日计提，不再从账户现金扣 |
| 510300.SH | 0.15%/年 | 0.05%/年 | 0.20%/年 | 已在基金净值中每日计提，不再从账户现金扣 |
| 518880.SH | 0.50%/年 | 0.10%/年 | 0.60%/年 | 已在基金净值中每日计提，不再从账户现金扣 |

公式上不要二次扣费：

```text
fund_price_return_already_net_of_opex = true
extra_management_fee = 0
```

这些费率只用于展示和数据质量检查。如果未来改用指数而不是 ETF 价格模拟基金，则才需要按日扣：

```text
daily_opex_factor = 1 - annual_opex_rate / day_count
```

### 3.3 国内 ETF 分红、除息除权

采用“真实现金流模式”：

1. 持仓估值使用未复权收盘价 `close`。
2. `fund_div.div_cash` 按每份基金派息金额处理。
3. 除息日 `ex_date` 形成应收股利，避免资产曲线在除息日人为跳水。
4. 派息日 `pay_date` 从应收转入国债逆回购现金账户。

设：

- `Q_i(t_ex^- )`：除息日前持有份额，单位份
- `d_i`：每份派息，单位 CNY/份，来自 `fund_div.div_cash`
- `tax_cn_fund_div`：国内公募基金分红税率，个人账户默认 0，保留配置

```text
dividend_receivable_i = Q_i(t_ex^- ) * d_i * (1 - tax_cn_fund_div)
cash_repo_on_pay_date += dividend_receivable_i
```

校验口径：

用 `fund_adj` 生成的总收益序列与“未复权价格 + 现金分红再投入/不再投入”序列对比。两者不会完全相同，因为 `fund_adj` 常按分红再投口径；差异超过阈值时输出数据质量警告。

### 3.4 IBKR 标普 500 ETF 费用

默认用 `VOO`：

- 发行商：Vanguard
- 费率：0.03%/年
- 成立日：2010-09-07
- 费用已体现在 ETF 净值/价格，不重复扣管理费

交易佣金配置：

```yaml
ibkr_us_etf_fee:
  plan: "pro_fixed"      # lite | pro_fixed | pro_tiered
  fixed_per_share_usd: 0.005
  fixed_min_usd: 1.0
  fixed_max_trade_pct: 0.01
  tiered_per_share_usd: 0.0035
  tiered_min_usd: 0.35
  lite_commission_usd: 0.0
```

公式：

设：

- `N`：成交股数
- `A_usd`：成交金额，单位 USD

IBKR Lite：

```text
fee_us_trade = 0
```

IBKR Pro Fixed：

```text
fee_us_trade = min(max(N * 0.005, 1.00), A_usd * 0.01)
```

IBKR Pro Tiered MVP：

```text
fee_us_trade = max(N * tiered_per_share_usd, tiered_min_usd) + pass_through_fees
```

`pass_through_fees` 先设为 0，后续可按 IBKR 报表精确导入。

### 3.5 IBKR 汇兑、汇出入金与换汇

汇率统一定义：

- `R_t`：估值日 USD/CNY，中间价，单位 CNY per USD。
- CNY 买 USD 时，实际买入价高于中间价：`R_buy = R_t * (1 + s_out)`
- USD 换回 CNY 时，实际卖出价低于中间价：`R_sell = R_t * (1 - s_in)`

配置：

```yaml
fx_fee:
  bank_out_spread_bps: 30        # CNY->USD 银行/通道点差，30 bps = 0.30%
  bank_in_spread_bps: 30         # USD->CNY 点差
  outbound_wire_fee_cny: 150.0   # 汇出固定费，按实际银行修改
  inbound_wire_fee_cny: 0.0
  ibkr_auto_fx_markup: 0.0003    # IBKR 自动换汇 0.03%
  ibkr_manual_fx_rate: 0.00002   # 手动 FX 0.002%，另有最低2 USD
  ibkr_manual_fx_min_usd: 2.0
  use_ibkr_auto_fx: true
```

点差换算：

```text
bps_to_rate = bps / 10000
30 bps = 0.003 = 0.30%
3 bps = 0.0003 = 0.03%
```

CNY 汇出并买入 USD：

设：

- `C_out`：本次从国内账户调往 IBKR 的 CNY 金额
- `f_wire_out`：汇出固定费，CNY
- `s_out`：银行/通道点差
- `s_ib_auto`：IBKR 自动换汇加点，默认 0.03%

若在银行端购汇：

```text
usd_credit = max(C_out - f_wire_out, 0) / (R_t * (1 + s_out))
```

若在 IBKR 自动换汇：

```text
usd_credit = max(C_out - f_wire_out, 0) / (R_t * (1 + s_out + s_ib_auto))
```

USD 换回 CNY：

```text
cny_credit = U_out * R_t * (1 - s_in - s_ib_auto) - f_wire_in
```

估值时不扣清算点差，直接用中间价：

```text
value_us_account_cny = (shares_voo * price_voo_usd + cash_usd + dividend_receivable_usd) * R_t
```

### 3.6 US ETF 分红与预扣税

采用“真实现金流模式”：

1. 价格估值使用未复权 `close`。
2. 分红使用 `dividends` 字段。
3. 分红到账进入 IBKR USD cash。
4. 默认税率按配置处理；若中国税收居民且有效 W-8BEN，默认可设 10%；未提交或不适用条约则常见为 30%。

配置：

```yaml
us_dividend_tax:
  w8ben_valid: true
  withholding_rate: 0.10
```

公式：

设：

- `Q_us`：除息日前持有股数
- `d_us`：每股分红，USD/share
- `tau_us`：美国分红预扣税率

```text
cash_usd += Q_us * d_us * (1 - tau_us)
withheld_tax_usd += Q_us * d_us * tau_us
```

资本利得税：

MVP 默认 `us_capital_gain_tax_rate=0`，仅作为非美国税务居民、未触发特殊规则的估算口径；实际税务情况应以用户身份和税务顾问为准。

### 3.7 国债逆回购收益与手续费

国债逆回购价格通常是年化收益率，单位为百分比。例如 `1.8` 表示年化 1.8%。

设：

- `P_repo`：融出本金，CNY
- `y_repo`：成交年化收益率，百分比，例如 `1.8`
- `D_actual`：实际占款天数，日
- `r_repo_fee`：券商逆回购手续费率
- `cap_repo_fee`：手续费封顶

利息：

```text
interest_repo = P_repo * (y_repo / 100) * D_actual / 365
```

手续费：

```text
fee_repo = min(P_repo * r_repo_fee, cap_repo_fee)
```

净收益：

```text
net_interest_repo = interest_repo - fee_repo
```

默认配置：

```yaml
repo_fee:
  tenor: "204001"
  investor_commission_rate: 0.00001  # 1天期常见 0.001%，即10万元收1元
  fee_cap_cny: 30.0
  lot_size_cny: 1000
```

实际占款天数：

```text
D_actual = maturity_settlement_date - first_settlement_date
```

这里用自然日差。首次交收日含、到期交收日不含。遇周末和长假时，GC001 的实际占款天数可能大于 1。

## 4. 回测核心公式

### 4.1 资产估值

对第 `i` 个 CNY 资产：

```text
MV_i(t) = Q_i(t) * P_i(t)
```

对 US ETF：

```text
MV_us_cny(t) = (Q_us(t) * P_us(t) + cash_usd(t) + recv_usd(t)) * R_t
```

国债逆回购账户：

```text
MV_repo(t) = cash_available_cny(t) + sum(active_repo_principal_lots) + accrued_or_receivable_repo_interest(t)
```

总资产：

```text
V_total(t) = sum(MV_cn_etf_i(t)) + MV_us_cny(t) + MV_repo(t) + dividend_receivable_cny(t)
```

### 4.2 目标权重

设：

- `w_i_base`：用户配置的基础权重
- `enabled_i(t)`：UI 是否启用
- `listed_i(t)`：标的在日期 `t` 是否已成立且价格可用

有效风险资产权重：

```text
w_i_eff(t) = w_i_base * enabled_i(t) * listed_i(t)
```

国债逆回购有效权重：

```text
w_repo_eff(t) = 1 - sum(w_i_eff(t))
```

### 4.3 再平衡触发

设：

- `w_i_now(t) = MV_i(t) / V_total(t)`
- `b = 0.02`

触发条件：

```text
rebalance_needed = any(abs(w_i_now(t) - w_i_eff(t)) > b for all enabled/listed assets and repo)
```

MVP 执行到目标权重：

```text
target_value_i = V_total_before_trade * w_i_eff(t)
trade_value_i = target_value_i - MV_i_before_trade
```

后续可增加“只调回容忍带边界”模式以减少交易费用。

### 4.4 买入/卖出数量

国内 ETF 按 100 份一手：

```text
raw_qty_buy = floor((target_cash / (price * 100)) * 100)
raw_qty_sell = floor((sell_value / (price * 100)) * 100)
```

US ETF 默认支持碎股开关：

```text
if allow_fractional_us_shares:
    qty_us = trade_usd / price_us
else:
    qty_us = floor(trade_usd / price_us)
```

手续费必须从买入预算中扣除，建议用二分法求最大可买数量：

```text
max_qty such that qty * price + fee(qty * price) <= available_cash
```

### 4.5 收益率曲线

消费是外部取现，不应算作投资亏损。

设：

- `F_t`：当天外部现金流，流入为正，消费取现为负
- `V_t`：当天收盘后总资产

日收益率：

```text
r_t = (V_t - V_{t-1} - F_t) / V_{t-1}
```

累计收益率：

```text
TR_t = product(1 + r_k for k <= t) - 1
```

总资产曲线直接画 `V_t`，会反映每月 5000 元消费后的真实账户剩余资产。

### 4.6 每次再平衡周期收益率

设第 `k` 次再平衡前日期为 `t_k`，上次再平衡后日期为 `t_{k-1}`。

```text
period_return_k = product(1 + r_t for t_{k-1} < t <= t_k) - 1
```

报表字段：

- `period_start`
- `period_end`
- `period_return`
- `period_withdrawal_cny`
- `period_fees_cny`
- `asset_before_rebalance`
- `asset_after_rebalance`
- `turnover_cny`

### 4.7 最大回撤

使用流量调整后的累计净值曲线：

```text
nav_t = product(1 + r_k for k <= t)
peak_t = max(nav_s for s <= t)
drawdown_t = nav_t / peak_t - 1
max_drawdown = min(drawdown_t)
```

同时输出资产金额回撤：

```text
asset_drawdown_t = V_t / max(V_s for s <= t) - 1
```

### 4.8 浮盈浮亏

按移动加权成本法：

买入后：

```text
new_cost_basis = old_cost_basis + buy_cash_native + buy_fee_native
new_qty = old_qty + buy_qty
avg_cost = new_cost_basis / new_qty
```

卖出时：

```text
cost_sold = avg_cost * sell_qty
realized_pnl_native = sell_cash_native - sell_fee_native - cost_sold
remaining_cost_basis = old_cost_basis - cost_sold
```

浮盈浮亏：

```text
unrealized_pnl_native = current_qty * current_price - remaining_cost_basis
```

US ETF 额外输出 CNY 折算浮盈浮亏：

```text
unrealized_pnl_cny = unrealized_pnl_usd * R_t + fx_translation_pnl_cny
```

MVP 可先显示 CNY 总浮盈：

```text
unrealized_pnl_cny_simple = market_value_cny - remaining_cost_basis_cny
```

## 5. 系统架构

### 5.1 技术栈

后端：

- Python 3.11+
- FastAPI + Uvicorn
- SQLite
- pandas：数据清洗和入库
- NumPy：回测核心数组计算
- concurrent.futures：并行数据拉取和多场景计算
- tushare：国内金融数据
- akshare：逆回购、汇率等公开数据补足
- yfinance 或 Stooq CSV：US ETF 日线和分红

前端：

- 单页面 HTML
- 原生 JavaScript
- ECharts
- 简洁布局：左侧参数区，右侧图表和表格

### 5.2 目录结构

```text
portfolio-backtest/
  app/
    main.py
    config.py
    db.py
    models.py
    schemas.py
    data_sources/
      base.py
      tushare_source.py
      akshare_source.py
      yfinance_source.py
      stooq_source.py
    services/
      data_sync.py
      calendar.py
      fees.py
      backtest_engine.py
      metrics.py
      reports.py
    static/
      index.html
      app.js
      styles.css
  data/
    backtest.sqlite3
  tests/
    test_fees.py
    test_rebalance.py
    test_repo.py
    test_dividends.py
    test_metrics.py
  scripts/
    init_db.py
    sync_data.py
    run_backtest.py
  .env.example
  requirements.txt
  README.md
```

### 5.3 后端 API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/` | 返回单页面 |
| `GET` | `/api/default-config` | 默认参数、默认费率、默认资产 |
| `GET` | `/api/data/status` | 查看各数据源覆盖区间、缺口、最后同步时间 |
| `POST` | `/api/data/sync` | 拉取/更新行情、分红、汇率、逆回购数据 |
| `POST` | `/api/backtest/run` | 运行回测，返回 `run_id` 和摘要 |
| `GET` | `/api/backtest/{run_id}` | 回测摘要 |
| `GET` | `/api/backtest/{run_id}/series` | 总资产、收益率、回撤、沪深300基准序列 |
| `GET` | `/api/backtest/{run_id}/rebalance` | 每次再平衡记录 |
| `GET` | `/api/backtest/{run_id}/trades` | 交易流水、费用、汇兑记录 |
| `GET` | `/api/backtest/{run_id}/positions` | 每日/期末持仓 |

### 5.4 SQLite 表设计

```sql
CREATE TABLE assets (
  symbol TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  market TEXT NOT NULL,
  currency TEXT NOT NULL,
  inception_date TEXT,
  expense_ratio REAL,
  management_fee REAL,
  custodian_fee REAL,
  source TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE prices (
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL NOT NULL,
  adj_close REAL,
  volume REAL,
  amount REAL,
  currency TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE fund_dividends (
  symbol TEXT NOT NULL,
  ann_date TEXT,
  record_date TEXT,
  ex_date TEXT,
  pay_date TEXT,
  div_cash REAL NOT NULL,
  currency TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, ex_date, pay_date, div_cash)
);

CREATE TABLE adj_factors (
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  adj_factor REAL NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE fx_rates (
  pair TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  rate REAL NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (pair, trade_date)
);

CREATE TABLE repo_rates (
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open_rate REAL,
  close_rate REAL NOT NULL,
  high_rate REAL,
  low_rate REAL,
  volume REAL,
  amount REAL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE trading_calendar (
  market TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  is_open INTEGER NOT NULL,
  prev_trade_date TEXT,
  next_trade_date TEXT,
  PRIMARY KEY (market, trade_date)
);

CREATE TABLE backtest_runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  config_json TEXT NOT NULL,
  summary_json TEXT NOT NULL
);

CREATE TABLE portfolio_daily (
  run_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  total_asset_cny REAL NOT NULL,
  flow_cny REAL NOT NULL,
  daily_return REAL,
  cumulative_return REAL,
  drawdown REAL,
  benchmark_return REAL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, trade_date)
);

CREATE TABLE trades (
  run_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity REAL NOT NULL,
  price REAL NOT NULL,
  gross_amount REAL NOT NULL,
  fee REAL NOT NULL,
  currency TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload_json TEXT
);

CREATE TABLE rebalance_events (
  run_id TEXT NOT NULL,
  rebalance_date TEXT NOT NULL,
  period_return REAL,
  total_asset_before REAL,
  total_asset_after REAL,
  turnover_cny REAL,
  fee_cny REAL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, rebalance_date)
);
```

## 6. 回测引擎流程

### 6.1 数据准备

1. 读取用户配置。
2. 根据 `start_date/end_date` 和资产列表检查数据覆盖。
3. 对每个 symbol 拉取/更新数据：
   - 国内 ETF：`fund_daily`、`fund_div`、`fund_adj`、`fund_basic`
   - 沪深 300：`index_daily(000300.SH)`
   - 国债逆回购：`ak.bond_buy_back_hist_em("204001")`
   - VOO：`yfinance.download("VOO", actions=True)` 或 Stooq CSV
   - 汇率：AKShare 外汇/人民币中间价，备用 `CNY=X`
4. 入库时统一日期格式为 `YYYY-MM-DD`。
5. 缺失数据前值填充只用于估值，不用于交易成交；成交日必须有该市场价格。

### 6.2 日度事件顺序

每天按如下顺序处理：

1. 更新价格、汇率、组合估值。
2. 处理 US / CN ETF 除息形成应收。
3. 处理分红派息到账。
4. 处理逆回购到期，本金和净利息转入可用现金。
5. 若为月度消费日，扣除消费。
6. 若为再平衡日，计算权重偏离并交易。
7. 将剩余可用现金按 1000 元整数倍滚入 GC001。
8. 生成当天 `portfolio_daily`。

### 6.3 并行与 NumPy 优化

数据同步并行：

- 用 `ThreadPoolExecutor` 并行拉取各数据源，因为主要瓶颈是网络 I/O。
- Tushare 调用要加频率限制器，5000 档按 500 次/分钟上限保守设置为 300 次/分钟。

回测计算：

- 将价格、汇率、分红事件预先转成 NumPy 数组。
- 日度估值使用向量化数组：

```python
market_values = quantities_matrix * price_matrix * fx_matrix
total_assets = market_values.sum(axis=1) + cash_series + receivable_series
```

- 再平衡、分红、逆回购这类事件仍用循环处理，但循环只做状态变更。
- 参数扫描时，例如不同再平衡频率、不同权重、不同手续费，用 `ProcessPoolExecutor` 并行跑多个独立 config。
- SQLite 写入集中在主进程，避免多进程同时写库。

## 7. 前端单页面设计

### 7.1 页面布局

左侧参数区：

- 初始资金输入框
- 开始日期滑块/日期选择
- 结束日期
- 再平衡频率：年度 / 半年度
- 容忍带：默认 2%
- 月消费金额：默认 5000
- US ETF symbol：默认 VOO
- 4 个资产的启用开关 + 权重滑块
- 费用高级设置折叠区
- “同步数据”按钮
- “运行回测”按钮

右侧结果区：

- 顶部摘要卡片：
  - 期末总资产
  - 流量调整后累计收益
  - 年化收益
  - 最大回撤
  - 总手续费
  - 总消费
  - 期末浮盈浮亏
- ECharts 图：
  - 总资产折线图
  - 累计收益率折线图，叠加沪深 300
  - 回撤图
  - 资产权重堆叠面积图
- 表格：
  - 每次再平衡收益率
  - 交易流水
  - 数据质量警告

### 7.2 图表口径

资产曲线：

- `x = trade_date`
- `y = total_asset_cny`
- 反映每月消费后的账户剩余资产。

收益率曲线：

- `strategy = cumulative_return`
- `benchmark = hs300_close / hs300_close_start - 1`
- 策略曲线用现金流调整收益率，避免消费造成曲线下跳。

沪深 300 对比：

- 仅作为指数价格基准，不扣 ETF 费用，不含分红。
- 若希望和 510300 可投资基准对比，可另加 `510300 buy-and-hold` 曲线。

## 8. 默认配置样例

```json
{
  "initial_capital_cny": 1000000,
  "start_date": "2012-01-01",
  "end_date": "2026-06-23",
  "rebalance_frequency": "yearly",
  "rebalance_band": 0.02,
  "monthly_spend_cny": 5000,
  "monthly_spend_day": "first_cn_trade_day",
  "repo_symbol": "204001",
  "assets": [
    {
      "key": "us_sp500",
      "symbol": "VOO",
      "name": "Vanguard S&P 500 ETF",
      "target_weight": 0.20,
      "enabled": true,
      "currency": "USD",
      "market": "US"
    },
    {
      "key": "cn_dividend_low_vol",
      "symbol": "512890.SH",
      "target_weight": 0.08,
      "enabled": true,
      "currency": "CNY",
      "market": "CN"
    },
    {
      "key": "cn_hs300_etf",
      "symbol": "510300.SH",
      "target_weight": 0.12,
      "enabled": true,
      "currency": "CNY",
      "market": "CN"
    },
    {
      "key": "cn_gold_etf",
      "symbol": "518880.SH",
      "target_weight": 0.10,
      "enabled": true,
      "currency": "CNY",
      "market": "CN"
    }
  ],
  "fees": {
    "cn_etf": {
      "commission_rate": 0.00025,
      "min_commission_cny": 0,
      "exchange_handling_rate": 0.00004,
      "include_exchange_in_commission": true,
      "stamp_tax_rate": 0,
      "transfer_fee_rate": 0
    },
    "repo": {
      "investor_commission_rate": 0.00001,
      "fee_cap_cny": 30,
      "lot_size_cny": 1000
    },
    "ibkr_us_etf": {
      "plan": "pro_fixed",
      "fixed_per_share_usd": 0.005,
      "fixed_min_usd": 1,
      "fixed_max_trade_pct": 0.01
    },
    "fx": {
      "bank_out_spread_bps": 30,
      "bank_in_spread_bps": 30,
      "outbound_wire_fee_cny": 150,
      "inbound_wire_fee_cny": 0,
      "ibkr_auto_fx_markup": 0.0003,
      "use_ibkr_auto_fx": true
    },
    "tax": {
      "cn_fund_dividend_tax_rate": 0,
      "us_dividend_withholding_rate": 0.10,
      "us_capital_gain_tax_rate": 0
    }
  }
}
```

注意：上面 `cn_etf.commission_rate=0.00025` 表示万 2.5。若实际佣金是万 0.5，应填 `0.00005`。

## 9. 实施步骤

### 阶段 1：工程骨架

1. 创建 FastAPI 项目和静态页面。
2. 建 SQLite schema 和初始化脚本。
3. 加 `.env.example`：

```text
TUSHARE_TOKEN=
DATABASE_URL=sqlite:///data/backtest.sqlite3
```

4. `.gitignore` 排除：

```text
.env
data/*.sqlite3
__pycache__/
.venv/
```

### 阶段 2：数据同步

1. 实现 `DataSource` 抽象：

```python
class DataSource:
    def fetch_prices(self, symbol, start_date, end_date): ...
    def fetch_dividends(self, symbol, start_date, end_date): ...
    def fetch_metadata(self, symbol): ...
```

2. 实现 Tushare 国内 ETF/指数。
3. 实现 AKShare 国债逆回购和 FX。
4. 实现 yfinance/Stooq US ETF。
5. 实现 `data/status` 缺口检查。

验收：

- `512890.SH/510300.SH/518880.SH` 价格覆盖从成立日至结束日。
- `000300.SH` 覆盖从回测开始日至结束日。
- `204001` 逆回购覆盖主要区间。
- `VOO` 覆盖 2012 至结束日。
- 汇率覆盖 2012 至结束日。

### 阶段 3：费用和事件模块

1. `fees.py` 实现：
   - 国内 ETF 佣金
   - IBKR US ETF 佣金
   - FX 汇兑
   - 逆回购手续费
2. `calendar.py` 实现：
   - 中国交易日
   - 美国交易日
   - 月度消费日
   - 再平衡日
   - 逆回购实际占款天数
3. `events.py` 或在 engine 内处理：
   - ETF 分红应收/到账
   - US ETF 分红预扣税
   - 逆回购到期

验收：

- 单笔费用测试覆盖万 2.5、万 0.5、IBKR fixed、FX bps。
- 长假前 GC001 实际占款天数测试通过。
- 分红除息日资产不发生无解释跳变。

### 阶段 4：回测引擎

1. 初始化组合。
2. 按日循环处理事件。
3. 执行初始建仓。
4. 执行年度/半年度再平衡。
5. 执行月度消费。
6. 每日输出账户状态。
7. 计算指标。

验收：

- 关闭所有基金时，100% 进入逆回购。
- 512890 在 2018-12-19 前不买入，资金在逆回购；下一个再平衡日才买入。
- 月度消费不会被计入策略亏损。
- 权重偏离超过 ±2% 时触发再平衡。
- 每次再平衡后权重接近目标，误差仅来自整手/碎股/费用/现金残留。

### 阶段 5：前端

1. 做参数表单和滑块。
2. 做数据状态提示。
3. 接入 `/api/backtest/run`。
4. 用 ECharts 绘制：
   - 总资产
   - 累计收益率 vs 沪深 300
   - 回撤
   - 权重
5. 做再平衡记录表和交易流水表。

验收：

- 页面不需要跳转即可完成配置、运行、查看结果。
- 图表 tooltip 显示日期、策略值、基准值、回撤。
- 数据缺口和降级源在页面清楚显示。

### 阶段 6：校验和文档

1. 单元测试：
   - `test_fees.py`
   - `test_rebalance.py`
   - `test_repo.py`
   - `test_dividends.py`
   - `test_metrics.py`
2. 数据校验：
   - 国内 ETF 现金分红模拟总收益 vs `fund_adj` 总收益差异。
   - VOO 现金分红税前模拟 vs `adj_close` 总收益差异。
   - 逆回购收益抽样手算。
3. README：
   - 安装
   - 配置 token
   - 启动
   - 数据源说明
   - 费用配置说明

## 10. 启动命令建议

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install fastapi uvicorn pandas numpy requests tushare akshare yfinance sqlalchemy pydantic pydantic-settings python-dotenv
Copy-Item .env.example .env
# 手工编辑 .env，填入 TUSHARE_TOKEN，不要提交 .env
python scripts/init_db.py
python scripts/sync_data.py --start 2012-01-01 --end 2026-06-23
uvicorn app.main:app --reload --host 127.0.0.1 --port 8123
```

浏览器打开：

```text
http://127.0.0.1:8123
```

## 11. 风险与需要用户确认的参数

必须配置或确认：

1. IBKR 账户类型：Lite / Pro Fixed / Pro Tiered。
2. 是否允许 US ETF 碎股。
3. 实际国内 ETF 佣金率、是否免 5 元最低、交易所经手费是否包含在佣金里。
4. 实际银行/券商出入金费用和购汇/结汇点差。
5. US 分红预扣税率：W-8BEN 是否有效，默认 10%，否则常见 30%。
6. 国债逆回购使用 GC001 还是 R-001，手续费是否有折扣。
7. 当国债逆回购现金不足以支付月消费时，是强制卖出资产，还是记录现金不足并停止回测。

## 12. 参考来源

- Tushare 积分与频次权限：<https://tushare.pro/document/1?doc_id=290>
- Tushare ETF 日线 `fund_daily`：<https://tushare.pro/wctapi/documents/127.md>
- Tushare 基金复权因子 `fund_adj`：<https://tushare.pro/wctapi/documents/199.md>
- Tushare / 数立方基金分红字段 `fund_div`：<https://datacube.foundersc.com/document/2?doc_id=120>
- Tushare 指数日线 `index_daily`：<https://tushare.pro/wctapi/documents/95.md>
- 上交所收费一览表：<https://www.sse.com.cn/services/tradingservice/charge/ssecharge/>
- 上交所债券通用质押式回购：<https://www.sse.com.cn/assortment/bonds/repo/>
- AKShare 质押式回购历史数据：<https://akshare.akfamily.xyz/data/bond/bond.html>
- IBKR 货币兑换费用：<https://www.interactivebrokers.com/en/pricing/commissions-spot-currencies.php>
- IBKR 美股/ETF 佣金：<https://www.interactivebrokers.com/en/pricing/commissions-home.php>
- Vanguard VOO：<https://investor.vanguard.com/investment-products/etfs/profile/voo>
- IRS 非居民美国来源股息预扣：<https://www.irs.gov/individuals/international-taxpayers/federal-income-tax-withholding-and-reporting-on-other-kinds-of-us-source-income-paid-to-nonresident-aliens>
- 美国-中国税收协定 PDF：<https://www.irs.gov/pub/irs-trty/china.pdf>
- 东方财富基金档案 512890：<https://fundf10.eastmoney.com/jjfl_512890.html>
- 东方财富基金档案 510300：<https://fundf10.eastmoney.com/510300.html>
- 东方财富基金档案 518880：<https://fundf10.eastmoney.com/jjfl_518880.html>
