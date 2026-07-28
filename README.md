# 永久投资策略回测

这是 `portfolio_backtest_implementation_plan.md` 的落地实现：Python + SQLite 后端、单页面 JavaScript + ECharts 前端。

## 启动

```powershell
$py = 'C:\Users\yupeng.zhang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py scripts/init_db.py
& $py scripts/sync_data.py --start 2012-01-01 --end 2026-06-23
& $py -m app.main --port 51327
```

打开：

```text
http://127.0.0.1:51327
```

## 数据源

- 国内 ETF、沪深300、基金分红、复权因子：优先 Tushare HTTP API，需要 `TUSHARE_TOKEN` 环境变量。
- US ETF：优先 Stooq 日线公开 CSV。
- 港股通 ETF（如 03195.HK）：优先本地 DataSrc 缓存，失败后使用 Yahoo/Eastmoney 公开行情；分红优先 Yahoo。
- 国债逆回购：优先 AKShare `bond_buy_back_hist_em`，失败后使用东方财富 K 线公开接口；全部失败则返回缺数据。
- 汇率：按启用资产补齐 `USD/CNY`、`HKD/CNY` 等币种对，优先本地 DataSrc，失败后使用 Yahoo、Frankfurter、Stooq 和公开 currency API；全部失败则返回缺数据。
- 数据同步失败不会静默伪装成功，状态表会显示 `source`，同步返回会包含 `warnings` 和 `missing_data`。生产同步不会生成 mock/估算行情。

## 费用口径

页面把费用拆成两类：

- 可调整费用假设：券商佣金、换汇/出入金点差、港股通汇兑点差、美国分红预扣税。
- 固定费率与成本对照：交易所/监管规费、结算费、基金内扣费率等当前按公开规则固化的假设。

03195 与 VOO 的主要默认口径：

| 项目 | 03195 港股通 | VOO 美股 |
| --- | ---: | ---: |
| 基金内扣费用 | 0.79%/年 | 0.03%/年 |
| 基金费用差 | 03195 每年多约 0.76% | - |
| 官方交易规费 | 0.0085%/边 | SEC 卖出 0.00206% |
| 结算/交收费 | 0.0042%/边 | FINRA TAF 0.000195 美元/股，上限 9.79 美元 |
| 印花税 | ETF 暂按 0 | 0 |
| 持仓组合费 | 港股通 0.008%/年，按日折算 | 0 |
| 分红税 | 多体现在基金净值/跟踪差 | 默认按最差预期 30%，可在页面调低 |

注意：ETF 基金内扣费用已经体现在基金净值或历史价格中，回测不额外重复扣除；这里只作为成本对照展示。03195 的管理费、受托人费等属于 0.79% 经常性开支内部结构，不与 0.79% 重复相加。券商佣金、换汇点差、出入金和盘口价差因账户与下单时点不同，页面保留为可调整假设。

主要参考：恒生投资 03195 产品资料、Vanguard VOO 产品资料、HKEX 交易规费、SEC Section 31、FINRA TAF、IBKR 美股佣金。

## 测试

```powershell
& $py scripts/run_tests.py
```

测试脚本使用标准库 `unittest` 和 `trace` 统计 `app/` 下业务代码覆盖率，低于 90% 会失败。
