from __future__ import annotations

from copy import deepcopy
import json
import unittest

from app.db import db_session
from app.services.backtest_engine import rebalance_cycle_metrics, run_backtest
from app.services.research import run_research
from tests.helpers import build_synced_db


class RebalanceCycleMetricTests(unittest.TestCase):
    @staticmethod
    def event(nav, *, kind="scheduled", traded=True):
        return {"event_type": kind, "rebalanced": traded, "execution_nav_after": nav}

    def test_positive_negative_flat_skip_checks_and_cash_activation(self):
        metrics = rebalance_cycle_metrics([
            self.event(1, kind="initial_allocation"),
            self.event(.5, traded=False),
            self.event(1.1),
            self.event(1.2, kind="treasury_activation"),
            self.event(1),
            self.event(.9, traded=False),
            self.event(1 + 1e-14),
        ])
        self.assertEqual(metrics["rebalance_positive_count"], 1)
        self.assertEqual(metrics["rebalance_negative_count"], 1)
        self.assertEqual(metrics["rebalance_flat_count"], 1)
        self.assertEqual(metrics["rebalance_evaluated_count"], 3)
        self.assertAlmostEqual(metrics["rebalance_positive_ratio"], 1 / 3)

    def test_first_executed_rebalance_counts_cycle_from_initial_allocation(self):
        # The portfolio can remain below original capital after opening fees
        # while the completed holding cycle itself is profitable.
        metrics = rebalance_cycle_metrics([self.event(.99, kind="initial_allocation"), self.event(.995)])
        self.assertEqual(metrics["rebalance_evaluated_count"], 1)
        self.assertEqual(metrics["rebalance_positive_ratio"], 1)

    def test_no_trades_or_only_initial_allocation_has_no_completed_cycles(self):
        for events in ([], [self.event(1, kind="initial_allocation")],
                       [self.event(1, kind="initial_allocation"), self.event(1.2, traded=False)],
                       [self.event(1, kind="treasury_activation"), self.event(1.2, traded=False)]):
            with self.subTest(events=events):
                metrics = rebalance_cycle_metrics(events)
                self.assertIsNone(metrics["rebalance_positive_ratio"])
                self.assertEqual(metrics["rebalance_evaluated_count"], 0)


class RealRebalanceCycleTests(unittest.TestCase):
    @staticmethod
    def config(original, **updates):
        config = deepcopy(original)
        config.update(initial_capital_cny=100_000, monthly_spend_cny=0, rebalance_frequency="monthly",
                      rebalance_band=.1, rebalance_to_target=True, dip_buy_enabled=False)
        for asset in config["assets"]:
            asset.update(enabled=asset["symbol"] == "510300.SH", target_weight=.5 if asset["symbol"] == "510300.SH" else 0)
        config["fees"]["cn_etf"].update(commission_rate=0, min_commission_cny=0, exchange_handling_rate=0)
        config["fees"]["repo"]["investor_commission_rate"] = 0
        config.update(updates)
        return config

    @staticmethod
    def flatten(conn):
        conn.execute("UPDATE prices SET open=1,high=1,low=1,close=1,adj_close=1 WHERE symbol='510300.SH'")
        conn.execute("DELETE FROM fund_dividends")
        conn.execute("UPDATE repo_rates SET open_rate=0,close_rate=0,high_rate=0,low_rate=0")

    @staticmethod
    def run_strategy(conn, config):
        result = run_backtest(conn, config, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)
        events = [{**dict(row), "payload": json.loads(row["payload_json"])} for row in conn.execute(
            "SELECT * FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date", (result["run_id"],))]
        return result, events

    def test_within_band_checks_do_not_reset_cycle_and_unfinished_tail_is_excluded(self):
        path, original = build_synced_db("2020-01-02", "2020-07-31")
        with db_session(path) as conn:
            self.flatten(conn)
            conn.execute("UPDATE prices SET open=1.3,close=1.3,high=1.3,low=1.3,adj_close=1.3 WHERE symbol='510300.SH' AND trade_date >= '2020-03-20'")
            conn.execute("UPDATE prices SET open=.8,close=.8,high=.8,low=.8,adj_close=.8 WHERE symbol='510300.SH' AND trade_date >= '2020-06-15'")
            conn.execute("UPDATE prices SET open=2,close=2,high=2,low=2,adj_close=2 WHERE symbol='510300.SH' AND trade_date = '2020-07-31'")
            result, events = self.run_strategy(conn, self.config(original))
        summary = result["summary"]
        self.assertEqual(summary["rebalance_trade_count"], 2)
        self.assertEqual(summary["rebalance_evaluated_count"], 2)
        self.assertGreater(summary["rebalance_within_band_count"], 0)
        self.assertEqual(summary["rebalance_positive_count"], 1)
        self.assertEqual(summary["rebalance_negative_count"], 1)
        self.assertEqual(summary["rebalance_flat_count"], 0)
        self.assertEqual(summary["rebalance_positive_ratio"], .5)
        traded = [event for event in events if event["payload"]["event_type"] == "scheduled" and event["payload"]["rebalanced"]]
        self.assertEqual([event["rebalance_date"] for event in traded], ["2020-04-01", "2020-07-01"])
        # The final price recovery does not become a third completed cycle.
        self.assertGreater(summary["final_asset_cny"], traded[-1]["total_asset_after"])

    def test_actual_opening_gap_is_counted_instead_of_decision_close_return(self):
        path, original = build_synced_db("2020-01-02", "2020-02-03")
        with db_session(path) as conn:
            self.flatten(conn)
            conn.execute("UPDATE prices SET open=1.5,high=1.5 WHERE symbol='510300.SH' AND trade_date='2020-02-03'")
            result, events = self.run_strategy(conn, self.config(original))
            daily = conn.execute("SELECT cumulative_return FROM portfolio_daily WHERE run_id=? AND trade_date='2020-02-03'", (result["run_id"],)).fetchone()
        event = events[-1]
        self.assertEqual(event["rebalance_date"], "2020-02-03")
        self.assertAlmostEqual(event["period_return"], 0)
        self.assertAlmostEqual(event["payload"]["execution_nav_after"], 1.25)
        self.assertNotAlmostEqual(event["payload"]["execution_nav_after"], 1 + daily["cumulative_return"])
        self.assertEqual(result["summary"]["rebalance_positive_count"], 1)
        self.assertEqual(result["summary"]["rebalance_evaluated_count"], 1)

    def test_consumption_does_not_turn_flat_market_cycles_into_losses(self):
        path, original = build_synced_db("2020-01-02", "2020-04-03")
        with db_session(path) as conn:
            self.flatten(conn)
            result, events = self.run_strategy(conn, self.config(original, monthly_spend_cny=2_000, rebalance_band=0))
        summary = result["summary"]
        self.assertEqual(summary["total_spend_cny"], 8_000)
        self.assertAlmostEqual(summary["final_asset_cny"], 92_000)
        self.assertEqual(summary["rebalance_evaluated_count"], 2)
        self.assertEqual(summary["rebalance_flat_count"], 2)
        self.assertEqual(summary["rebalance_negative_count"], 0)
        self.assertEqual(summary["rebalance_positive_ratio"], 0)
        for event in events:
            self.assertAlmostEqual(event["payload"]["execution_nav_after"], 1)

    def test_execution_fees_are_losses_even_when_prices_are_flat(self):
        path, original = build_synced_db("2020-01-02", "2020-04-03")
        config = self.config(original, monthly_spend_cny=2_000, rebalance_band=0)
        config["fees"]["cn_etf"]["commission_rate"] = .001
        with db_session(path) as conn:
            self.flatten(conn)
            result, events = self.run_strategy(conn, config)
        summary = result["summary"]
        self.assertGreater(summary["total_fees_cny"], 0)
        self.assertGreater(summary["rebalance_evaluated_count"], 0)
        self.assertEqual(summary["rebalance_negative_count"], summary["rebalance_evaluated_count"])
        self.assertEqual(summary["rebalance_positive_count"], 0)
        self.assertEqual(summary["rebalance_flat_count"], 0)
        initial_nav = events[0]["payload"]["execution_nav_after"]
        self.assertLess(initial_nav, 1)
        previous_nav = initial_nav
        for event in events[1:]:
            if event["payload"]["rebalanced"]:
                self.assertLess(event["payload"]["execution_nav_after"], previous_nav)
                self.assertGreater(event["fee_cny"], 0)
                previous_nav = event["payload"]["execution_nav_after"]

    def test_research_rows_publish_cycle_counts_and_no_trade_is_not_zero_percent(self):
        path, original = build_synced_db("2020-01-02", "2020-02-03")
        config = self.config(original)
        with db_session(path) as conn:
            self.flatten(conn)
            conn.execute("UPDATE prices SET open=1.5,high=1.5 WHERE symbol='510300.SH' AND trade_date='2020-02-03'")
            result = run_research(conn, config, {"kind": "rebalance_grid", "frequencies": ["monthly", "yearly"], "bands": [.1]})
        monthly, yearly = result["rows"]
        self.assertEqual(monthly["status"], "success", monthly)
        self.assertEqual(monthly["rebalance_positive_ratio"], 1)
        self.assertEqual(monthly["rebalance_positive_count"], 1)
        self.assertEqual(monthly["rebalance_evaluated_count"], 1)
        self.assertEqual(yearly["rebalance_evaluated_count"], 0)
        self.assertIsNone(yearly["rebalance_positive_ratio"])
        self.assertIn("消费现金流", result["methodology"]["rebalance_positive_ratio"])
