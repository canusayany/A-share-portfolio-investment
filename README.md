# 跨市场 ETF + 国债逆回购组合回测

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
- 国债逆回购：优先 AKShare `bond_buy_back_hist_em`，失败后使用东方财富 K 线公开接口；全部失败则返回缺数据。
- 汇率：优先 Stooq `USDCNY` 日线，失败后使用 Yahoo `CNY=X`；全部失败则返回缺数据。
- 数据同步失败不会静默伪装成功，状态表会显示 `source`，同步返回会包含 `warnings` 和 `missing_data`。生产同步不会生成 mock/估算行情。

## 测试

```powershell
& $py scripts/run_tests.py
```

测试脚本使用标准库 `unittest` 和 `trace` 统计 `app/` 下业务代码覆盖率，低于 90% 会失败。
