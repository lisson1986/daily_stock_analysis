#!/usr/bin/env python3
"""Opt-in research lifecycle: explicit init, restore, close, replay and checkpoint."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from src.research.paper import PaperStore  # noqa: E402
from src.research.runner import (close_research, current_session, macro_research,
                                 render_report, weekly_review)  # noqa: E402
from src.research.sources import calendar_sessions, execution_bar, raw_history, verify_security  # noqa: E402
from src.research.state import assert_private_state_repository, checkpoint, restore  # noqa: E402


def catch_up(store, cfg, now):
    """Settle missed sessions with pre-existing signals; never manufacture old AI signals."""
    last = store.state()['last_session']
    if last is None:
        return {'settled': 0}
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    end = local.date() if local.hour >= 16 else local.date() - timedelta(days=1)
    days = calendar_sessions(last, end.isoformat())
    count = 0
    for prev, day in zip(days, days[1:]):
        # Current session gets its normal research packet in close mode.
        if day == local.date().isoformat():
            break
        state = store.state()
        bars = {}
        for code in set(state['positions']) | {o['code'] for o in state['orders']}:
            verify_security(code, day)
            hist = raw_history(code, (datetime.fromisoformat(day) - timedelta(days=180)).date().isoformat(), day)
            bars[code] = execution_bar(code, hist, day)
        store.apply(day, prev, bars, None, {'catch_up': True, 'coverage': 0, 'daily_valid_count': 0,
                                          'limitations': ['Settlement only; no retrospective AI selection.']})
        count += 1
    return {'settled': count}


def rule_backtest(packet, cfg, store):
    """Historical price rule baseline, requiring caller-supplied point-in-time inputs."""
    if packet.get('schema_version') != 1 or packet.get('point_in_time_universe') is not True:
        raise ValueError('Historical backtest requires declared point-in-time universe, not current survivors')
    if not packet.get('source') or not packet.get('fee_assumption'):
        raise ValueError('Historical source and dated fee assumption required')
    histories = {}
    previous = packet['previous_session']
    for item in packet['sessions']:
        day, bars = item['session'], item['bars']
        if item['previous_session'] != previous:
            raise ValueError('Non-contiguous historical exchange sessions')
        candidate = None
        ranked = []
        # The rule reads only past/current closing prices; trades execute the next session.
        for code, bar in bars.items():
            histories.setdefault(code, []).append(float(bar['close']))
            prices = histories[code]
            if len(prices) >= 20 and bar.get('buyable') and prices[-1] > sum(prices[-20:]) / 20:
                ranked.append({'code': code, 'close': prices[-1], 'score': prices[-1] / prices[-20] - 1})
        if ranked:
            candidate = max(ranked, key=lambda r: (r['score'], r['code']))
        store.apply(day, previous, bars, candidate, {'mode': 'historical_price_rule_baseline',
                                                   'source': packet['source'],
                                                   'limitations': ['PIT universe declared by data supplier, not independently certified.',
                                                                   'Not an AI/news historical backtest.']})
        previous = day
    return {'review': store.review(), 'replay': store.replay(), 'mode': 'historical_price_rule_baseline'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['initialize', 'restore', 'checkpoint', 'validate-state-repo',
                                        'morning', 'close', 'catch-up', 'weekly', 'replay', 'backtest'])
    parser.add_argument('--config', default=os.getenv('RESEARCH_CONFIG_PATH', 'config/research-system.yaml'))
    parser.add_argument('--paper-db', default=os.getenv('RESEARCH_PAPER_DB', 'data/paper_research.db'))
    parser.add_argument('--state-dir', default=os.getenv('RESEARCH_STATE_DIR', 'private-state/checkpoint'))
    parser.add_argument('--reports-dir', default='reports/research')
    parser.add_argument('--fixture', help='Offline packet {session,previous_session,bars,candidate,research}')
    parser.add_argument('--input', help='Historical PIT price-rule dataset (backtest only)')
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(Path(args.config).read_text(encoding='utf-8'))
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    db_path = Path(os.getenv('DATABASE_PATH', './data/stock_analysis.db'))
    files = {'paper_research.db': Path(args.paper_db), 'stock_analysis.db': db_path}
    if args.mode == 'validate-state-repo':
        assert_private_state_repository(os.getenv('RESEARCH_STATE_REPO', ''), os.getenv('RESEARCH_STATE_TOKEN', ''))
        print('Private state repository verified')
        return 0
    if args.mode == 'restore':
        restore(args.state_dir, files)
        print('State restored with verified checksums')
        return 0
    if args.mode == 'checkpoint':
        checkpoint(files, args.state_dir)
        print('Consistent checkpoint prepared')
        return 0
    if args.mode == 'initialize':
        if Path(args.paper_db).exists() or (Path(args.state_dir) / 'manifest.json').exists():
            raise ValueError('Already initialized; initialization cannot reset a saved account')
        store = PaperStore(args.paper_db, cfg, initialize=True)
        from src.storage import DatabaseManager
        DatabaseManager.get_instance()
        report = {'initialized': True, 'state': store.state(), 'config_hash': store.config_hash}
        checkpoint(files, args.state_dir)
        render_report({'initialization': report}, Path(args.reports_dir) / 'initialization.md')
        print('Paper account explicitly initialized; no orders generated')
        return 0
    store = PaperStore(args.paper_db, cfg)
    if args.mode == 'close':
        if args.fixture:
            packet = json.loads(Path(args.fixture).read_text(encoding='utf-8'))
            report = store.apply(packet['session'], packet['previous_session'], packet['bars'],
                                 packet.get('candidate'), packet.get('research', {}))
        else:
            catch_up(store, cfg, now)
            report = close_research(cfg, store, now)
    elif args.mode == 'morning':
        session, _ = current_session(now)
        report = {'session': session, 'macro': macro_research(cfg, now) if session else [],
                  'existing_orders': store.state()['orders'], 'mode': 'information_only',
                  'limitations': ['Morning information never rewrites frozen paper orders.']}
        if session:
            report = store.save_packet(f'morning-{session}', report)
    elif args.mode == 'catch-up':
        report = catch_up(store, cfg, now)
    elif args.mode == 'weekly':
        report = weekly_review(store)
        report = store.save_packet(f'weekly-{now.date().isoformat()}', report)
    elif args.mode == 'replay':
        report = {'review': store.review(), 'replay': store.replay()}
    elif args.mode == 'backtest':
        if not args.input or store.state()['last_session']:
            raise ValueError('Backtest needs --input and a new isolated initialized paper database')
        report = rule_backtest(json.loads(Path(args.input).read_text(encoding='utf-8')), cfg, store)
    else:
        raise ValueError('Unsupported mode')
    report = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
    label = report.get('session') or now.date().isoformat()
    render_report(report, Path(args.reports_dir) / f'{args.mode}-{label}.md')
    print(json.dumps({'mode': args.mode, 'session': report.get('session'), 'completed': True}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
