from datetime import date, datetime
import json
import sqlite3
import unittest
from zoneinfo import ZoneInfo

from app.db import SCHEMA, insert_many
from app.services.data_sync import (
    mark_sync_coverage, missing_coverage_ranges, parse_eastmoney_net_worth_trend,
    price_anomaly_ranges, valid_hk_ohlc,
)
from app.services.fund_income import missing_money_dates, parse_money_income, verified_voo_dividends


def timestamp(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


class MarketInputRepairs(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)

    def test_nav_distribution_uses_published_daily_total_return(self):
        nav = [
            {"x": timestamp("2007-02-01"), "y": 1.99, "equityReturn": 0.4},
            {"x": timestamp("2007-02-02"), "y": 1.006, "equityReturn": -3.2161},
        ]
        rows = parse_eastmoney_net_worth_trend(
            "var Data_netWorthTrend = " + json.dumps(nav) + ";", "160706", "510300.SH",
            "2007-02-01", "2007-02-02", "CNY",
        )
        self.assertAlmostEqual(rows[1]["close"] / rows[0]["close"], 0.967839)
        self.assertIsNone(rows[1]["open"])

    def test_holiday_income_must_exist_and_must_match_issuer_units(self):
        values = {"2024-01-05": 0.5157, "2024-01-07": 0.9896, "2024-01-08": 0.5674}
        official = '<input id="fundCode" value="511990">百份收益<tbody id="navList">' + "".join(
            f"<tr><td>{day}</td><td>1%</td><td>{amount}</td></tr>" for day, amount in values.items()
        ) + "</tbody>"
        def archive(multiplier):
            data = [[timestamp(day), amount * multiplier] for day, amount in values.items()]
            return 'var fS_code = "511990";var Data_millionCopiesIncome = ' + json.dumps(data) + ";"
        rows = parse_money_income(official, archive(1), "2024-01-05", "2024-01-08")
        insert_many(self.conn, "money_fund_income", rows)
        self.assertFalse(missing_money_dates(self.conn, "2024-01-05", "2024-01-08"))
        self.conn.execute("DELETE FROM money_fund_income WHERE income_date='2024-01-07'")
        self.assertEqual(missing_money_dates(self.conn, "2024-01-05", "2024-01-08"), {"2024-01-07"})
        with self.assertRaises(ValueError):
            parse_money_income(official, archive(100), "2024-01-05", "2024-01-08")

    def test_unverified_voo_coverage_does_not_extend_new_interval(self):
        mark_sync_coverage(self.conn, "dividends", "VOO", "2010-09-07", "2026-10-20", "digrin:html:dividend")
        self.assertTrue(missing_coverage_ranges(self.conn, "dividends", "VOO", "2026-06-26", "2026-06-26"))
        mark_sync_coverage(self.conn, "dividends", "VOO", "2026-06-26", "2026-06-26", "vanguard:digrin_paid_history")
        self.assertEqual(missing_coverage_ranges(self.conn, "dividends", "VOO", "2026-06-25", "2026-06-26"),
                         [("2026-06-25", "2026-06-25")])

    def test_voo_rejects_truncated_history_and_uses_real_payment_date(self):
        issuer = ('{"ticker":"VOO","fundId":"0968","asOfDate":"01/03/2011","incomeCapitalGains":'
                  '[{"type":"Dividend","share":"$0.526","recordDate":"12/29/2010","payableDate":"12/31/2010"}]}')
        archive = [
            {"symbol":"VOO", "currency":"USD", "ex_date":"2010-09-24", "pay_date":"2010-09-30", "div_cash":0.558},
            {"symbol":"VOO", "currency":"USD", "ex_date":"2010-12-27", "pay_date":"2010-12-31", "div_cash":0.526},
        ]
        rows = verified_voo_dividends(issuer, archive, "2010-12-27", "2010-12-31")
        self.assertEqual([(r["ex_date"], r["record_date"], r["pay_date"]) for r in rows],
                         [("2010-12-27", "2010-12-29", "2010-12-31")])
        with self.assertRaisesRegex(ValueError, "2010 Q3"):
            verified_voo_dividends(issuer, archive[1:], "2010-12-27", "2010-12-31")

    def test_inconsistent_open_is_flagged_in_the_cache(self):
        row = dict(symbol="03195.HK", trade_date="2025-04-25", open=8.36, high=8.62, low=8.59,
                   close=8.6, source="eastmoney:hk_kline", currency="HKD")
        insert_many(self.conn, "prices", [row])
        self.assertFalse(valid_hk_ohlc(row))
        self.assertEqual(price_anomaly_ranges(self.conn, row["symbol"], row["trade_date"], row["trade_date"]),
                         [(row["trade_date"], row["trade_date"])])
        self.assertTrue(valid_hk_ohlc({**row, "low": 8.36}))
