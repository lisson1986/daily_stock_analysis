"""Deterministic next-session paper execution and transactional event archive.

Separate paper tables avoid changing the existing user portfolio contracts.
Every session, fills, frozen signal and NAV are committed in one transaction.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from datetime import date
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def validate_config(cfg):
    if cfg.get('schema_version') != 1 or not cfg.get('strategy_version'):
        raise ValueError('Unsupported research configuration')
    for key in ('initial_cash', 'commission_min'):
        if not math.isfinite(float(cfg[key])) or cfg[key] <= 0:
            raise ValueError(f'Invalid {key}')
    for key in ('position_fraction', 'exposure_fraction', 'risk_fraction', 'stop_fraction',
                'take_profit_fraction', 'trail_activate_fraction', 'trail_fraction',
                'entry_gap_fraction', 'volume_fraction', 'pause_drawdown_fraction',
                'exit_drawdown_fraction'):
        if not 0 < cfg[key] < 1:
            raise ValueError(f'Invalid {key}')
    for key in ('slippage_bps', 'commission_fraction', 'sell_tax_fraction', 'transfer_fee_fraction'):
        if not math.isfinite(float(cfg[key])) or cfg[key] < 0:
            raise ValueError(f'Invalid {key}')
    for key in ('holding_sessions', 'max_positions', 'research_count', 'deep_analysis_count', 'news_days'):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] < 1:
            raise ValueError(f'Invalid {key}')
    if cfg['deep_analysis_count'] > cfg['research_count']:
        raise ValueError('Deep analysis exceeds research pool')
    if len(cfg['watchlist']) > cfg['deep_analysis_count'] or not all(eligible(c) for c in cfg['watchlist']):
        raise ValueError('Watchlist must fit deep analysis and contain eligible stock codes')
    if cfg['pause_drawdown_fraction'] >= cfg['exit_drawdown_fraction']:
        raise ValueError('Pause threshold must precede exit threshold')


def eligible(code):
    return len(code) == 6 and code.isdigit() and code.startswith(('000', '001', '002', '003',
                                                               '300', '301', '600', '601', '603', '605'))


def validate_bar(bar, session):
    if bar.get('date') != session or bar.get('adjustment') != 'none':
        raise ValueError('Execution requires same-session unadjusted bars')
    vals = [float(bar[k]) for k in ('open', 'high', 'low', 'close', 'volume')]
    if not all(math.isfinite(v) and v >= 0 for v in vals) or min(vals[:4]) <= 0:
        raise ValueError('Invalid OHLCV')
    if bar['low'] > min(bar['open'], bar['close']) or bar['high'] < max(bar['open'], bar['close']):
        raise ValueError('Invalid OHLC range')
    # Live adapter deliberately refuses corporate-action windows until manual reconciliation.
    if bar.get('corporate_action'):
        raise ValueError('Corporate action requires reconciliation before settlement')


def fees(notional, side, cfg):
    commission = max(cfg['commission_min'], notional * cfg['commission_fraction'])
    transfer = notional * cfg['transfer_fee_fraction']
    tax = notional * cfg['sell_tax_fraction'] if side == 'sell' else 0
    return round(commission + transfer + tax, 2)


def settle(state, session, previous_session, bars, cfg):
    """Settle a COMPLETE exchange session; caller supplies verified session continuity."""
    state = copy.deepcopy(state)
    if state['last_session'] and state['last_session'] != previous_session:
        raise ValueError('Missing session: catch up before settling current session')
    if state['last_session'] and session <= state['last_session']:
        raise ValueError('Sessions must increase')
    required = set(state['positions']) | {o['code'] for o in state['orders']}
    for code in required:
        if code not in bars:
            raise ValueError(f'Missing execution/valuation bar: {code}')
        validate_bar(bars[code], session)
    fills, notices = [], []
    slip = cfg['slippage_bps'] / 10000
    equity = state['cash'] + sum(p['quantity'] * p['mark'] for p in state['positions'].values())
    # Opening orders use only prior settled risk/cash, never today's close or later sale proceeds.
    drawdown = state['drawdown']
    for order in state['orders']:
        code, bar = order['code'], bars[order['code']]
        reason = None
        if order['signal_date'] != previous_session:
            reason = 'expired_signal'
        elif state.get('liquidate') or drawdown >= cfg['pause_drawdown_fraction']:
            reason = 'drawdown_pause'
        elif code in state['positions'] or len(state['positions']) >= cfg['max_positions']:
            reason = 'position_limit'
        elif not bar.get('buyable', False) or bar['open'] * (1 + slip) > order['limit']:
            reason = 'untradeable_or_entry_limit'
        if reason:
            notices.append({'code': code, 'reason': reason})
            continue
        price = round(bar['open'] * (1 + slip), 2)
        if price > order['limit']:
            notices.append({'code': code, 'reason': 'rounded_price_exceeds_limit'})
            continue
        invested = sum(p['quantity'] * p['mark'] for p in state['positions'].values())
        budget = min(equity * cfg['position_fraction'],
                     equity * cfg['risk_fraction'] / cfg['stop_fraction'],
                     equity * cfg['exposure_fraction'] - invested, state['cash'])
        quantity = max(0, int(min(budget / price, bar['volume'] * cfg['volume_fraction']) // 100) * 100)
        while quantity and quantity * price + fees(quantity * price, 'buy', cfg) > budget:
            quantity -= 100
        if not quantity:
            notices.append({'code': code, 'reason': 'cash_or_volume_limit'})
            continue
        cost = fees(quantity * price, 'buy', cfg)
        basis = round(quantity * price + cost, 2)
        state['cash'] = round(state['cash'] - basis, 2)
        state['positions'][code] = {'quantity': quantity, 'entry': price, 'basis': basis,
                                    'entry_date': session, 'age': 1, 'mark': bar['close'],
                                    'highest_close': bar['close'],
                                    'stop': price * (1 - cfg['stop_fraction']),
                                    'target': price * (1 + cfg['take_profit_fraction'])}
        fills.append({'code': code, 'side': 'buy', 'quantity': quantity, 'price': price,
                      'fees': cost, 'pnl': None, 'reason': 'frozen_signal'})
    for code, pos in list(state['positions'].items()):
        bar = bars[code]
        if bar.get('valuation_stale'):
            notices.append({'code': code, 'reason': 'suspended_stale_valuation',
                            'price_date': bar.get('price_date')})
        if pos['entry_date'] == session:
            continue  # T+1: new buys cannot exit on their entry session.
        pos['age'] += 1
        reason, price = None, None
        if pos.get('pending_exit'):
            reason, price = 'pending_' + pos['pending_exit'], bar['open']
        elif state.get('liquidate'):
            reason, price = 'portfolio_drawdown', bar['open']
        elif bar['open'] <= pos['stop']:
            reason, price = 'gap_stop', bar['open']
        elif bar['low'] <= pos['stop']:
            reason, price = 'stop', pos['stop']
        elif bar['open'] >= pos['target']:
            reason, price = 'take_profit', pos['target']
        elif bar['high'] >= pos['target']:
            reason, price = 'take_profit', pos['target']
        elif pos['age'] >= cfg['holding_sessions']:
            reason, price = 'time_exit', bar['close']
        if reason and not bar.get('sellable', False):
            notices.append({'code': code, 'reason': 'sell_blocked', 'trigger': reason})
            if reason != 'take_profit':
                pos['pending_exit'] = reason.removeprefix('pending_')
        elif reason:
            capacity = int(bar['volume'] * cfg['volume_fraction'] // 100) * 100
            quantity = min(pos['quantity'], capacity)
            if quantity:
                # A profit limit order cannot fill below its limit.
                price = round(price if reason == 'take_profit' else price * (1 - slip), 2)
                cost = fees(quantity * price, 'sell', cfg)
                proceeds = round(quantity * price - cost, 2)
                basis = pos['basis'] * quantity / pos['quantity']
                pnl = round(proceeds - basis, 2)
                state['cash'] = round(state['cash'] + proceeds, 2)
                state['realized'] = round(state['realized'] + pnl, 2)
                pos['basis'] -= basis
                pos['quantity'] -= quantity
                fills.append({'code': code, 'side': 'sell', 'quantity': quantity, 'price': price,
                              'fees': cost, 'pnl': pnl, 'reason': reason})
                if pos['quantity'] == 0:
                    del state['positions'][code]
            if not quantity or code in state['positions']:
                notices.append({'code': code, 'reason': 'volume_limited_exit'})
                if reason != 'take_profit':
                    pos['pending_exit'] = reason.removeprefix('pending_')
        if code in state['positions']:
            pos['mark'] = bar['close']
            pos['highest_close'] = max(pos['highest_close'], bar['close'])
            if pos['highest_close'] >= pos['entry'] * (1 + cfg['trail_activate_fraction']):
                pos['stop'] = max(pos['stop'], pos['highest_close'] * (1 - cfg['trail_fraction']))
    state['orders'] = []
    state['last_session'] = session
    state['equity'] = round(state['cash'] + sum(p['quantity'] * p['mark']
                                             for p in state['positions'].values()), 2)
    state['high_water'] = max(state['high_water'], state['equity'])
    state['drawdown'] = max(0, 1 - state['equity'] / state['high_water'])
    state['liquidate'] = state['drawdown'] >= cfg['exit_drawdown_fraction']
    state['paused'] = state['drawdown'] >= cfg['pause_drawdown_fraction']
    state['total_fees'] += sum(f['fees'] for f in fills)
    if state['cash'] < 0:
        raise ValueError('Cash reconciliation failed')
    return state, fills, notices


def initial_state(cfg):
    return {'cash': cfg['initial_cash'], 'equity': cfg['initial_cash'], 'high_water': cfg['initial_cash'],
            'positions': {}, 'orders': [], 'realized': 0, 'total_fees': 0,
            'last_session': None, 'drawdown': 0, 'paused': False, 'liquidate': False}


class PaperStore:
    """SQLite transaction boundary, immutable session inputs and reproducible replay."""
    def __init__(self, path, cfg, initialize=False):
        validate_config(cfg)
        self.cfg = cfg
        self.config_hash = digest(cfg)
        path = Path(path)
        if not path.exists() and not initialize:
            raise RuntimeError('State missing. Explicit initialization required; refusing reset.')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK(id=1),
              config_hash TEXT NOT NULL, config TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_sessions (session TEXT PRIMARY KEY,
              previous_session TEXT NOT NULL, inputs TEXT NOT NULL, inputs_hash TEXT NOT NULL,
              state TEXT NOT NULL, fills TEXT NOT NULL, notices TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_archive (session TEXT PRIMARY KEY, report TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_packets (packet_key TEXT PRIMARY KEY, report TEXT NOT NULL);
        ''')
        row = self.db.execute('SELECT config_hash FROM paper_account WHERE id=1').fetchone()
        if row is None:
            if not initialize:
                raise RuntimeError('Uninitialized paper account')
            with self.db:
                self.db.execute('INSERT INTO paper_account VALUES(1,?,?,?)',
                                (self.config_hash, json.dumps(cfg), json.dumps(initial_state(cfg))))
        elif row[0] != self.config_hash:
            raise ValueError('Configuration changed: create a new strategy/account version')

    def state(self):
        return json.loads(self.db.execute('SELECT state FROM paper_account WHERE id=1').fetchone()[0])

    def report(self, session):
        row = self.db.execute('SELECT report FROM research_archive WHERE session=?', (session,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_packet(self, key, report):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO research_packets VALUES(?,?)',
                            (key, json.dumps(report, ensure_ascii=False)))
        return json.loads(self.db.execute('SELECT report FROM research_packets WHERE packet_key=?',
                                         (key,)).fetchone()[0])

    def apply(self, session, previous_session, bars, candidate, research):
        date.fromisoformat(session)
        date.fromisoformat(previous_session)
        if session <= previous_session:
            raise ValueError('Previous session must precede current session')
        if candidate and not eligible(candidate['code']):
            raise ValueError('Ineligible paper symbol')
        if candidate and (not math.isfinite(float(candidate['close'])) or candidate['close'] <= 0):
            raise ValueError('Invalid signal reference price')
        # Entire input packet is immutable. An already completed session is never traded again.
        existing = self.report(session)
        if existing is not None:
            return existing
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            existing = self.report(session)
            if existing is not None:
                return existing
            state, fills, notices = settle(self.state(), session, previous_session, bars, self.cfg)
            if candidate and not state['paused'] and candidate['code'] not in state['positions']:
                state['orders'] = [{'code': candidate['code'], 'signal_date': session,
                                    'limit': round(candidate['close'] * (1 + self.cfg['entry_gap_fraction']), 2)}]
            inputs = {'bars': bars, 'candidate': candidate, 'research': research}
            report = {'session': session, 'strategy_version': self.cfg['strategy_version'],
                      'config_hash': self.config_hash, 'state': state, 'fills': fills,
                      'notices': notices, 'research': research, 'inputs_hash': digest(inputs)}
            self.db.execute('INSERT INTO paper_sessions VALUES(?,?,?,?,?,?,?)',
                            (session, previous_session, json.dumps(inputs, ensure_ascii=False),
                             digest(inputs), json.dumps(state), json.dumps(fills), json.dumps(notices)))
            self.db.execute('INSERT INTO research_archive VALUES(?,?)',
                            (session, json.dumps(report, ensure_ascii=False)))
            self.db.execute('UPDATE paper_account SET state=? WHERE id=1', (json.dumps(state),))
        return report

    def review(self):
        rows = self.db.execute('SELECT session,state,fills,notices FROM paper_sessions ORDER BY session').fetchall()
        states = [json.loads(r[1]) for r in rows]
        sells = [f for r in rows for f in json.loads(r[2]) if f['side'] == 'sell']
        wins = [f['pnl'] for f in sells if f['pnl'] > 0]
        losses = [f['pnl'] for f in sells if f['pnl'] < 0]
        return {'sessions': len(rows), 'equity': self.state()['equity'],
                'net_return_pct': (self.state()['equity'] / self.cfg['initial_cash'] - 1) * 100,
                'max_drawdown_pct': max([s['drawdown'] for s in states] or [0]) * 100,
                'sell_fills': len(sells), 'winning_sell_fill_pct': len(wins) / len(sells) * 100 if sells else None,
                'profit_factor': sum(wins) / abs(sum(losses)) if losses else None,
                'total_fees': self.state()['total_fees'],
                'blocked_events': sum(len(json.loads(r[3])) for r in rows),
                'limitations': ['Sell fill statistics include partial exits; not independent round trips.',
                                'Daily-bar conservative simulation; no real orders.',
                                'Fee rates are explicit simulation assumptions.']}

    def replay(self):
        state = initial_state(self.cfg)
        count = 0
        for session, prev, text, inputs_hash, expected in self.db.execute(
                'SELECT session,previous_session,inputs,inputs_hash,state FROM paper_sessions ORDER BY session'):
            inputs = json.loads(text)
            if digest(inputs) != inputs_hash:
                raise ValueError(f'Input archive checksum divergence at {session}')
            state, _, _ = settle(state, session, prev, inputs['bars'], self.cfg)
            candidate = inputs['candidate']
            if candidate and not state['paused'] and candidate['code'] not in state['positions']:
                state['orders'] = [{'code': candidate['code'], 'signal_date': session,
                                    'limit': round(candidate['close'] * (1 + self.cfg['entry_gap_fraction']), 2)}]
            if digest(state) != digest(json.loads(expected)):
                raise ValueError(f'Replay divergence at {session}')
            count += 1
        if digest(state) != digest(self.state()):
            raise ValueError('Live account state differs from frozen-session replay')
        return {'verified_sessions': count, 'state_hash': digest(state), 'mode': 'frozen_signal_replay'}
