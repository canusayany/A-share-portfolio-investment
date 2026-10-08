from __future__ import annotations

from datetime import date, timedelta
from threading import Thread
import unittest
from urllib import error

from app.db import db_session, json_dumps
from app.main import columnar_chart_payload, create_server
from app.services.backtest_engine import BACKTEST_ENGINE_VERSION
from tests.helpers import temp_db_path
from tests.test_api import http_json


def chart_rows(count=1400):
    days = []
    day = date(2019, 1, 1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return [{
        "trade_date": day, "total_asset_cny": 1_000_000 + index * 13,
        "flow_cny": 0, "daily_return": index / 1_000_000,
        "cumulative_return": index / 10_000, "drawdown": -(index % 31) / 1000,
        "benchmark_return": index / 20_000,
        "payload_json": json_dumps({"values": {"TEST": 100 + index}, "weights": {"TEST": .75}, "comparison": {"total_asset_cny": 900_000 + index}}),
    } for index, day in enumerate(days)]


class ChartFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = chart_rows()
        cls.default_chart = columnar_chart_payload(cls.rows)
        cls.omitted = next(row for row in cls.rows if row["trade_date"] not in cls.default_chart["dates"])
        cls.db_path = temp_db_path()
        cls.server = create_server(port=0, db_path=cls.db_path)
        with db_session(cls.db_path) as conn:
            conn.execute("INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)", (
                "focus-run", "2024-01-01", json_dumps({"start_date": cls.rows[0]["trade_date"], "end_date": cls.rows[-1]["trade_date"]}),
                json_dumps({"engine_version": BACKTEST_ENGINE_VERSION, "analysis_status": "completed"}),
            ))
            conn.executemany("""INSERT INTO portfolio_daily(run_id,trade_date,total_asset_cny,flow_cny,daily_return,cumulative_return,drawdown,benchmark_return,payload_json)
                VALUES('focus-run',:trade_date,:total_asset_cny,:flow_cny,:daily_return,:cumulative_return,:drawdown,:benchmark_return,:payload_json)""", cls.rows)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.url = f"http://{host}:{port}/api/backtest/focus-run/chart-series"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_required_date_is_added_without_changing_other_samples_or_values(self):
        day = self.omitted["trade_date"]
        focused = columnar_chart_payload(self.rows, required_dates={day})
        self.assertEqual(len(self.default_chart["dates"]), 1000)
        self.assertEqual(focused["display_points"], 1001)
        self.assertEqual(focused["source_points"], len(self.rows))
        self.assertEqual(set(focused["dates"]), set(self.default_chart["dates"]) | {day})
        self.assertEqual(focused["dates"], sorted(focused["dates"]))
        index = focused["dates"].index(day)
        for column, source in (("total_assets", "total_asset_cny"), ("daily_returns", "daily_return"), ("cumulative_returns", "cumulative_return"), ("drawdowns", "drawdown"), ("benchmark_returns", "benchmark_return")):
            self.assertEqual(focused[column][index], self.omitted[source])
            self.assertEqual(len(focused[column]), len(focused["dates"]))
        original_index = self.rows.index(self.omitted)
        self.assertEqual(focused["values"]["TEST"][index], 100 + original_index)
        self.assertEqual(focused["weights"]["TEST"][index], .75)
        self.assertEqual(focused["comparison_total_assets"][index], 900_000 + original_index)

    def test_existing_required_date_does_not_duplicate_or_change_default(self):
        self.assertEqual(columnar_chart_payload(self.rows, required_dates=set()), self.default_chart)
        self.assertEqual(columnar_chart_payload(self.rows, required_dates={self.rows[0]["trade_date"]}), self.default_chart)

    def test_api_returns_exact_omitted_trading_day(self):
        default = http_json(self.url)["chart"]
        self.assertEqual(default, self.default_chart)
        day = self.omitted["trade_date"]
        focused = http_json(self.url + "?focus_date=" + day)["chart"]
        self.assertEqual(focused, columnar_chart_payload(self.rows, required_dates={day}))

    def test_invalid_dates_and_multiple_dates_are_client_errors(self):
        for query in ("focus_date=", "focus_date=2020-02-30", "focus_date=20200101", "focus_date=2020-1-01", "focus_date=2020-01-01T00:00:00", "focus_date=2020-01-01&focus_date=2020-01-02", "focus_date=2020-01-01&focus_date="):
            with self.subTest(query=query), self.assertRaises(error.HTTPError) as raised:
                http_json(self.url + "?" + query)
            self.assertEqual(raised.exception.code, 400)

    def test_date_missing_from_this_run_is_not_replaced_by_a_nearby_day(self):
        # A weekend within the interval and a day outside the stored interval.
        for day in ("2019-01-06", "2030-01-01"):
            with self.subTest(day=day), self.assertRaises(error.HTTPError) as raised:
                http_json(self.url + "?focus_date=" + day)
            self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
