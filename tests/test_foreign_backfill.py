from datetime import date
import json
import unittest
from unittest.mock import patch

from app.config import default_config
from app.db import db_session
from app.services.calendar import calendar_missing_years
from app.services.data_sync import (
    fetch_csindex_prices, required_calendar_ranges, required_data_missing, sync_all, sync_trading_calendars,
)
from tests.helpers import build_synced_db
from tests.test_market_calendar import annual_calendar


class ForeignBackfillTests(unittest.TestCase):
    def test_foreign_leading_session_gap_requires_and_receives_targeted_sync(self):
        for symbol, fetcher in (
            ('VOO', 'fetch_yahoo_prices'),
            ('03195.HK', 'fetch_datasrc_market_prices'),
        ):
            with self.subTest(symbol=symbol):
                path, cfg = build_synced_db('2025-03-03', '2025-03-14')
                asset = next(a for a in cfg['assets'] if a['symbol'] == symbol)
                asset['enabled'] = True
                with db_session(path) as conn:
                    self.assertEqual(required_data_missing(conn, cfg['start_date'], cfg['end_date'], [asset]), [])
                    first_row = dict(conn.execute(
                        'SELECT * FROM prices WHERE symbol=? AND trade_date=?',
                        (symbol, '2025-03-03'),
                    ).fetchone())
                    conn.execute('DELETE FROM prices WHERE symbol=? AND trade_date=?', (symbol, '2025-03-03'))
                    missing = required_data_missing(conn, cfg['start_date'], cfg['end_date'], [asset])
                    self.assertEqual(missing, [f'prices:{symbol}'])
                    first_row['source'] = 'test:foreign_leading_session'
                    with patch(f'app.services.data_sync.{fetcher}', return_value=[first_row]) as fetch:
                        result = sync_all(
                            conn, '', cfg['start_date'], cfg['end_date'], [asset], missing_items=missing,
                        )
                    self.assertEqual(fetch.call_count, 1)
                    self.assertEqual(fetch.call_args.args[1:3], ('2025-03-03', '2025-03-03'))
                    self.assertEqual(result['inserted']['prices'], 1)
                    self.assertNotIn(f'prices:{symbol}', result['missing_data'])
                    self.assertEqual(required_data_missing(conn, cfg['start_date'], cfg['end_date'], [asset]), [])
                    restored = conn.execute(
                        'SELECT source FROM prices WHERE symbol=? AND trade_date=?', (symbol, '2025-03-03'),
                    ).fetchone()
                    self.assertEqual(restored['source'], 'test:foreign_leading_session')

    def test_hk_calendar_starts_at_actual_fund_history(self):
        asset = next(a for a in default_config()['assets'] if a['symbol'] == '03195.HK')
        asset['enabled'] = True
        ranges = {market: (first, last) for market, first, last in
                  required_calendar_ranges([asset], '2012-01-01', '2025-12-31')}
        self.assertEqual(ranges['HK'][0], date(2024, 3, 23))
        self.assertEqual(ranges['CN'][0], date(2011, 12, 1))
        self.assertEqual(asset['trade_start_date'], '2024-04-23')
        future = {**asset, 'trade_start_date': '2027-01-01', 'inception_date': '2027-01-01'}
        self.assertEqual([r[0] for r in required_calendar_ranges([future], '2012-01-01', '2025-12-31')], ['CN'])

    def test_corrupt_us_weekend_refreshes_whole_year_including_bad_holiday(self):
        path, cfg = build_synced_db('2025-01-01', '2025-12-31')
        asset = next(a for a in cfg['assets'] if a['symbol'] == 'VOO')
        asset['enabled'] = True
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=1 WHERE market='US' AND trade_date IN ('2025-01-26','2025-06-19')")
            self.assertEqual(calendar_missing_years(conn, 'US', '2025-01-01', '2025-12-31'), [2025])
            with patch('app.services.data_sync.fetch_trading_calendar', return_value=annual_calendar('US', 2025, {'2025-06-19'})) as fetch:
                count, warnings, missing = sync_trading_calendars(conn, 'token', [asset], '2025-01-01', '2025-12-31')
            fetch.assert_called_once_with('token', 'US', 2025)
            self.assertEqual(count, 365)
            self.assertEqual((warnings, missing), ([], []))
            self.assertEqual(calendar_missing_years(conn, 'US', '2025-01-01', '2025-12-31'), [])
            self.assertEqual([r[0] for r in conn.execute("SELECT is_open FROM trading_calendar WHERE market='US' AND trade_date IN ('2025-01-26','2025-06-19')")], [0, 0])

    def test_numeric_csi_code_uses_official_history(self):
        payload = {'code': 200, 'success': True, 'data': [
            {'tradeDate': '20141231', 'close': 1000, 'open': 999},
            {'tradeDate': '20150105', 'close': 1010, 'open': 1001},
        ]}
        with patch('app.services.data_sync.fetch_text', return_value=json.dumps(payload)) as fetch:
            rows = fetch_csindex_prices('930050.CSI', '159593.SZ', '2014-12-31', '2015-01-05', 'CNY')
        self.assertIn('indexCode=930050', fetch.call_args.args[0])
        self.assertEqual([r['trade_date'] for r in rows], ['2014-12-31', '2015-01-05'])
        self.assertTrue(all(r['symbol'] == '159593.SZ' and r['source'] == 'csindex:index_perf' for r in rows))


if __name__ == '__main__':
    unittest.main()
