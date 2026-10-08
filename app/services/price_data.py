"""Source classification shared by ingestion and backtest readers."""

INDEX_PROXY_PRICE_SOURCES = {
    "csindex:index_perf", "tushare:index_daily", "datasrc:index",
    "sohu:index_kline", "eastmoney:index_kline",
}


def is_proxy_price_source(source: str) -> bool:
    return (
        ":splice_scale_" in source
        or ":fixed_scale_" in source
        or any(source == prefix or source.startswith(prefix + ":") for prefix in INDEX_PROXY_PRICE_SOURCES)
        or source.startswith(("eastmoney:fund_nav:", "akshare:fund_open_fund_info_em:"))
    )
