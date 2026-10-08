"""Serve the real application with deterministic, disposable market data.

No production database or external market data provider is used. The fixture
path and run IDs are written under output/ for Playwright's test process.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import normalize_config
from app.db import add_leaderboard_membership, db_session, init_db, json_dumps
from app.identity import leaderboard_key_id
from app.main import create_server
from app.services.backtest_engine import run_backtest
from tests.helpers import seed_fixture_data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=51331)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifact_dir = root / 'output' / 'playwright'
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='portfolio_e2e_') as temp:
        db_path = Path(temp) / 'fixture.sqlite3'
        init_db(db_path)
        config = normalize_config({
            'start_date': '2019-02-01', 'end_date': '2024-12-31',
            'initial_capital_cny': 1_000_000, 'monthly_spend_cny': 1500,
            'rebalance_frequency': 'monthly', 'rebalance_band': 0,
            'rebalance_to_target': True, 'dip_buy_enabled': False,
            'rebalance_month_analysis_enabled': False,
        })
        for asset in config['assets']:
            asset['enabled'] = asset['symbol'] in {'512890.SH', '518880.SH'}
            asset['target_weight'] = 0.4 if asset['enabled'] else 0
        with db_session(db_path) as conn:
            seed_fixture_data(conn, config, config['start_date'], config['end_date'])
            # High-amplitude, different asset cycles exercise actual rebalances.
            prices = conn.execute("SELECT rowid,open,close,adj_close FROM prices WHERE symbol='512890.SH' ORDER BY trade_date").fetchall()
            conn.executemany(
                'UPDATE prices SET open=?,close=?,adj_close=? WHERE rowid=?',
                [tuple(row[field] * (1 + 0.18 * math.sin(index / 29)) for field in ('open', 'close', 'adj_close')) + (row['rowid'],)
                 for index, row in enumerate(prices)],
            )
            runs = []
            for index in range(2):
                scenario = deepcopy(config)
                if index:
                    scenario.update(start_date='2020-02-01', rebalance_frequency='yearly', rebalance_band=0.25)
                    scenario['fees']['cn_etf']['commission_rate'] = 0.0008
                result = run_backtest(conn, scenario, include_month_analysis=False, include_rolling_analysis=False)
                result['summary']['analysis_status'] = 'completed'
                conn.execute('UPDATE backtest_runs SET summary_json=? WHERE run_id=?', (json_dumps(result['summary']), result['run_id']))
                add_leaderboard_membership(conn, leaderboard_key_id('e2e-test'), result['run_id'])
                runs.append({'run_id': result['run_id'], 'config': scenario})
        (artifact_dir / 'fixture.json').write_text(json.dumps({'runs': runs}), encoding='utf-8')
        server = create_server(port=args.port, db_path=db_path)
        print(f'E2E fixture ready at http://127.0.0.1:{args.port}', flush=True)
        try:
            # Missing fixtures must fail a test instead of fetching live prices.
            with patch('app.main.sync_all', side_effect=AssertionError('E2E must not synchronize external market data')):
                server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
