from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from app.db import db_session, insert_many
from app.main import backtest_archive_entries
from app.services.backtest_engine import (
    BACKTEST_ENGINE_VERSION, PortfolioState, Position, _buy_position,
    _portfolio_value, adjusted_price_series, configured_share_splits,
    load_dividend_events, load_price_map, run_backtest,
)
from app.services.data_sync import sync_all, SyncWarning
from tests.helpers import build_synced_db


class ShareScaleRegressionTests(unittest.TestCase):
    def fixture(self):
        path, config = build_synced_db("2021-10-21", "2022-01-07")
        config.update(monthly_spend_cny=0, rebalance_band=0, rebalance_to_target=True, dip_buy_enabled=False)
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == "512890.SH"
            asset["target_weight"] = 0.2 if asset["enabled"] else 0
        config["fees"]["cn_etf"]["commission_rate"] = 0
        config["fees"]["repo"]["investor_commission_rate"] = 0
        for option in config["repo_options"]:
            option["commission_rate"] = 0
        with db_session(path) as conn:
            conn.execute("UPDATE repo_rates SET open_rate=0,close_rate=0")
            conn.execute("UPDATE prices SET open=2,close=2 WHERE symbol='512890.SH' AND trade_date<'2021-10-25'")
            conn.execute("UPDATE prices SET open=1,close=1 WHERE symbol='512890.SH' AND trade_date>='2021-10-25'")
            conn.execute("UPDATE prices SET open=1.2 WHERE symbol='512890.SH' AND trade_date='2021-10-25'")
            conn.execute("UPDATE prices SET open=1.5352269334,close=1.5352269334,source='csindex:index_perf:splice_scale_0.001' WHERE symbol='512890.SH' AND trade_date='2021-10-22'")
        return path, config

    def test_close_open_and_dividend_share_one_scale_and_reject_post_listing_proxy(self):
        path, config = self.fixture()
        scales = {}
        with db_session(path) as conn:
            splits = configured_share_splits(config["assets"])
            closes = load_price_map(conn, ["512890.SH"], config["start_date"], config["end_date"], share_splits=splits, share_scale_maps=scales)
            opens = load_price_map(conn, ["512890.SH"], config["start_date"], config["end_date"], "open", splits)
            conn.execute("INSERT INTO fund_dividends VALUES('512890.SH',NULL,NULL,'2022-01-05','2022-01-06',0.03,'CNY','test:dividend')")
            dividends, _ = load_dividend_events(conn, ["512890.SH"], config["start_date"], config["end_date"], scales)
        self.assertNotIn("2021-10-22", opens["512890.SH"])
        self.assertNotIn("2021-10-22", closes["512890.SH"])
        self.assertEqual(opens["512890.SH"]["2021-10-25"], 2.4)
        self.assertEqual(closes["512890.SH"]["2021-10-25"], 2)
        self.assertEqual(scales["512890.SH"]["2022-01-05"], 2)
        self.assertEqual(dividends["2022-01-05"][0]["normalized_share_scale"], 2)

    def test_split_preserves_assets_and_continuous_year_matches_new_position(self):
        path, config = self.fixture()
        with db_session(path) as conn:
            long = run_backtest(conn, config, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)
            independent = run_backtest(conn, {**config, "start_date": "2022-01-01"}, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)
            daily = conn.execute("SELECT total_asset_cny,daily_return FROM portfolio_daily WHERE run_id=?", (long["run_id"],)).fetchall()
            self.assertTrue(all(abs(r["total_asset_cny"] - 1_000_000) < 1e-7 for r in daily))
            self.assertTrue(all(abs(r["daily_return"]) < 1e-12 for r in daily))
            self.assertEqual(long["summary"]["final_asset_cny"], independent["summary"]["final_asset_cny"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades WHERE run_id=? AND trade_date>='2022-01-01'", (long["run_id"],)).fetchone()[0], 0)
            conn.execute("INSERT INTO fund_dividends VALUES('512890.SH',NULL,NULL,'2022-01-05','2022-01-06',0.03,'CNY','test:dividend')")
            with patch("app.services.backtest_engine.get_cached_backtest_run", return_value=None):
                paid = run_backtest(conn, config, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)
            self.assertAlmostEqual(paid["summary"]["total_dividend_cny"], 6000)
            self.assertAlmostEqual(paid["summary"]["final_asset_cny"], 1_006_000)

    def test_verified_split_is_independent_of_large_market_gap(self):
        rows = [{"trade_date": "2021-10-21", "price": 2, "adj_factor": None},
                {"trade_date": "2021-10-25", "price": 0.8, "adj_factor": None}]
        self.assertEqual(adjusted_price_series(rows, {"2021-10-25": 2})["2021-10-25"], 1.6)

    def test_real_board_lots_trade_records_and_asset_conservation(self):
        _, config = self.fixture()
        position = Position("512890.SH", "CN", "CNY", "cn_etf", share_scale=2)
        state = PortfolioState(cash_cny=101, positions={position.symbol: position})
        trades = []
        from datetime import date
        _buy_position(state, position, date(2022, 1, 4), 101, 2, {}, config["fees"], trades, True, "rebalance")
        self.assertEqual(position.quantity, 50)
        self.assertEqual(trades[0]["quantity"], 100)
        self.assertEqual(trades[0]["price"], 1)
        self.assertEqual(trades[0]["gross_amount"], 100)
        self.assertEqual(_portfolio_value(state, {position.symbol: 2}, {})[0], 101)

    def test_pre_listing_proxy_is_retained(self):
        path, config = self.fixture()
        with db_session(path) as conn:
            conn.execute("INSERT INTO prices(symbol,trade_date,open,close,currency,source) VALUES('512890.SH','2018-12-28',0.6,0.7,'CNY','csindex:index_perf:splice_scale_0.001')")
            maps = load_price_map(conn, ["512890.SH"], "2018-12-01", "2019-01-01", "open")
        self.assertEqual(maps["512890.SH"]["2018-12-28"], 0.6)

    def test_sync_never_fills_post_listing_etf_gap_with_index(self):
        path, config = self.fixture()
        asset = next(a for a in config["assets"] if a["enabled"])
        with db_session(path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='512890.SH' AND trade_date='2022-01-05'")
            with patch("app.services.data_sync.fetch_cn_fund_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_datasrc_market_prices", side_effect=SyncWarning("offline")), \
                 patch("app.services.data_sync.fetch_sohu_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_eastmoney_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_price_fallback_rows") as fallback:
                sync_all(conn, "test-token", config["start_date"], config["end_date"], [asset], missing_items=["prices:512890.SH"])
            fallback.assert_not_called()
            self.assertIsNone(conn.execute("SELECT close FROM prices WHERE symbol='512890.SH' AND trade_date='2022-01-05'").fetchone())

    def test_old_results_stay_in_history_but_leave_current_ranking(self):
        path, config = self.fixture()
        with db_session(path) as conn:
            for version, score in [(BACKTEST_ENGINE_VERSION - 1, 99), (BACKTEST_ENGINE_VERSION, 80)]:
                conn.execute("INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)", (str(version), "2026-09-30", json.dumps(config), json.dumps({"engine_version": version, "ranking_eligible": True, "ranking_score": score})))
            history = backtest_archive_entries(conn, 20)
            ranking = backtest_archive_entries(conn, 20, leaderboard=True)
        self.assertEqual(len(history), 2)
        self.assertEqual([r["run_id"] for r in ranking], [str(BACKTEST_ENGINE_VERSION)])
        self.assertTrue(next(r for r in history if r["run_id"] == str(BACKTEST_ENGINE_VERSION - 1))["summary"]["requires_recalculation"])

    def test_price_format_executes_as_number_while_returns_remain_percentages(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the JavaScript formatting regression")
        source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
        helpers = "\n".join(re.findall(r"^const fmt(?:Num|Pct) = .*;$", source, re.MULTILINE))
        function = source[source.index("function formatCell("):source.index("function formatPerformanceCell(")]
        script = helpers + '\nfunction escapeHtml(v){return String(v);}\n' + function
        script += '\nconsole.log(JSON.stringify([formatCell({kind:"number",raw:0.809,decimals:4}),formatCell(0.0397)]));'
        output = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(output.stdout), ["0.8090", "3.97%"])


if __name__ == "__main__":
    unittest.main()
