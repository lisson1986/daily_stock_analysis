"""Risk-path tests for the public paper entry points and consistent state recovery."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from src.research.paper import PaperStore, initial_state, settle
from src.research.state import checkpoint, restore


@pytest.fixture
def cfg():
    return yaml.safe_load((Path(__file__).resolve().parents[2] / 'config/research-system.yaml').read_text())


def bar(day, **overrides):
    result = {'date': day, 'open': 10, 'high': 10.2, 'low': 9.9, 'close': 10,
              'volume': 1000000, 'adjustment': 'none', 'buyable': True, 'sellable': True}
    result.update(overrides)
    return result


def bought(cfg):
    state = initial_state(cfg)
    state['last_session'] = '2026-10-08'
    state['orders'] = [{'code': '300136', 'signal_date': '2026-10-08', 'limit': 10.2}]
    return settle(state, '2026-10-09', '2026-10-08', {'300136': bar('2026-10-09')}, cfg)[0]


def test_t_plus_one_and_stop(cfg):
    state = initial_state(cfg)
    state['orders'] = [{'code': '300136', 'signal_date': '2026-10-08', 'limit': 10.2}]
    # Both exits touched on entry day: no same-day sale.
    state, fills, _ = settle(state, '2026-10-09', '2026-10-08',
                             {'300136': bar('2026-10-09', low=9, high=12)}, cfg)
    assert [f['side'] for f in fills] == ['buy']
    assert state['cash'] >= 0
    # Both touched next session: stop wins.
    state, fills, _ = settle(state, '2026-10-12', '2026-10-09',
                             {'300136': bar('2026-10-12', low=9, high=12)}, cfg)
    assert fills[0]['reason'] == 'stop'
    assert not state['positions']


def test_gap_stop_uses_open_not_stop(cfg):
    state = bought(cfg)
    _, fills, _ = settle(state, '2026-10-12', '2026-10-09',
                         {'300136': bar('2026-10-12', open=8, close=8, high=8.2, low=7.8)}, cfg)
    assert fills[0]['price'] == 7.99


def test_blocked_exit_and_holding_sessions(cfg):
    state = bought(cfg)
    for prev, day in [('2026-10-09', '2026-10-12'), ('2026-10-12', '2026-10-13'),
                      ('2026-10-13', '2026-10-14')]:
        state, fills, _ = settle(state, day, prev, {'300136': bar(day)}, cfg)
        assert not fills
    state, fills, notices = settle(state, '2026-10-15', '2026-10-14',
                                   {'300136': bar('2026-10-15', sellable=False)}, cfg)
    assert not fills and notices[0]['reason'] == 'sell_blocked'
    assert state['positions']['300136']['age'] == 5
    state, fills, _ = settle(state, '2026-10-16', '2026-10-15', {'300136': bar('2026-10-16')}, cfg)
    assert fills[0]['reason'] == 'pending_time_exit'


def test_blocked_stop_remains_an_exit_after_rebound(cfg):
    state = bought(cfg)
    state, fills, _ = settle(state, '2026-10-12', '2026-10-09',
                             {'300136': bar('2026-10-12', low=9, sellable=False)}, cfg)
    assert not fills and state['positions']['300136']['pending_exit'] == 'stop'
    _, fills, _ = settle(state, '2026-10-13', '2026-10-12', {'300136': bar('2026-10-13')}, cfg)
    assert fills[0]['reason'] == 'pending_stop'


@pytest.mark.parametrize('overrides', [{'buyable': False}, {'open': 11, 'high': 11, 'close': 11}])
def test_entry_limit_and_untradeable(cfg, overrides):
    state = initial_state(cfg)
    state['orders'] = [{'code': '300136', 'signal_date': '2026-10-08', 'limit': 10.2}]
    state, fills, notices = settle(state, '2026-10-09', '2026-10-08',
                                   {'300136': bar('2026-10-09', **overrides)}, cfg)
    assert not fills and not state['orders'] and notices


def test_no_hindsight_funding_from_intraday_sales(cfg):
    state = bought(cfg)
    state['cash'] = 0
    state['orders'] = [{'code': '301297', 'signal_date': '2026-10-09', 'limit': 10.2}]
    state, fills, _ = settle(state, '2026-10-12', '2026-10-09',
                             {'300136': bar('2026-10-12', low=9), '301297': bar('2026-10-12')}, cfg)
    assert all(f['side'] == 'sell' for f in fills)
    assert '301297' not in state['positions']


def test_no_close_drawdown_hindsight_for_open_buy(cfg):
    state = initial_state(cfg)
    state['orders'] = [{'code': '300136', 'signal_date': '2026-10-08', 'limit': 10.2}]
    state, fills, _ = settle(state, '2026-10-09', '2026-10-08',
                             {'300136': bar('2026-10-09', close=5, low=5)}, cfg)
    assert fills[0]['side'] == 'buy' and state['paused']


def test_missing_or_adjusted_bars_do_not_mutate(cfg):
    state = bought(cfg)
    before = copy.deepcopy(state)
    with pytest.raises(ValueError, match='Missing'):
        settle(state, '2026-10-12', '2026-10-09', {}, cfg)
    with pytest.raises(ValueError, match='unadjusted'):
        settle(state, '2026-10-12', '2026-10-09', {'300136': bar('2026-10-12', adjustment='qfq')}, cfg)
    assert state == before


def test_session_continuity_and_corporate_actions(cfg):
    state = bought(cfg)
    with pytest.raises(ValueError, match='Missing session'):
        settle(state, '2026-10-13', '2026-10-12', {}, cfg)
    with pytest.raises(ValueError, match='Corporate action'):
        settle(state, '2026-10-12', '2026-10-09',
               {'300136': bar('2026-10-12', corporate_action=True)}, cfg)


def test_trailing_stop_only_next_session(cfg):
    state = bought(cfg)
    state, fills, _ = settle(state, '2026-10-12', '2026-10-09',
                             {'300136': bar('2026-10-12', close=10.7, high=10.8, low=9.9)}, cfg)
    assert not fills
    assert state['positions']['300136']['stop'] == pytest.approx(10.7 * .96)
    state, fills, _ = settle(state, '2026-10-13', '2026-10-12', {'300136': bar('2026-10-13')}, cfg)
    assert fills[0]['reason'] == 'gap_stop'


def test_partial_exit_conserves_basis(cfg):
    state = bought(cfg)
    prior = state['positions']['300136']['quantity']
    state, fills, notices = settle(state, '2026-10-12', '2026-10-09',
                                   {'300136': bar('2026-10-12', low=9, volume=100000)}, cfg)
    assert fills[0]['quantity'] == 1000
    assert state['positions']['300136']['quantity'] == prior - 1000
    assert notices


def test_immutable_idempotent_session_and_replay(cfg, tmp_path):
    store = PaperStore(tmp_path / 'paper.db', cfg, initialize=True)
    first = store.apply('2026-10-08', '2026-09-30', {}, {'code': '300136', 'close': 10}, {})
    # Re-running with a changed candidate returns the frozen report, never a new order.
    assert store.apply('2026-10-08', '2026-09-30', {}, {'code': '301297', 'close': 20}, {}) == first
    store.apply('2026-10-09', '2026-10-08', {'300136': bar('2026-10-09')}, None, {})
    assert store.replay()['verified_sessions'] == 2
    assert store.db.execute('SELECT count(*) FROM paper_sessions').fetchone()[0] == 2


def test_failed_transaction_does_not_publish_session(cfg, tmp_path):
    store = PaperStore(tmp_path / 'paper.db', cfg, initialize=True)
    store.apply('2026-10-08', '2026-09-30', {}, {'code': '300136', 'close': 10}, {})
    before = store.state()
    with pytest.raises(ValueError):
        store.apply('2026-10-09', '2026-10-08', {}, None, {})
    assert store.state() == before and store.report('2026-10-09') is None


def test_restore_wal_and_corruption_fail_closed(cfg, tmp_path):
    path = tmp_path / 'live.db'
    store = PaperStore(path, cfg, initialize=True)
    store.apply('2026-10-08', '2026-09-30', {}, None, {})
    checkpoint({'paper.db': path}, tmp_path / 'backup')
    restored = tmp_path / 'restored.db'
    restore(tmp_path / 'backup', {'paper.db': restored})
    assert PaperStore(restored, cfg).state() == store.state()
    manifest = tmp_path / 'backup/manifest.json'
    data = json.loads(manifest.read_text())
    data['files']['paper.db']['sha256'] = 'bad'
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='checksum'):
        restore(tmp_path / 'backup', {'paper.db': restored})
    assert PaperStore(restored, cfg).state() == store.state()


def test_missing_state_and_config_change_refuse_reset(cfg, tmp_path):
    with pytest.raises(RuntimeError, match='refusing reset'):
        PaperStore(tmp_path / 'missing.db', cfg)
    PaperStore(tmp_path / 'paper.db', cfg, initialize=True)
    changed = {**cfg, 'holding_sessions': 10}
    with pytest.raises(ValueError, match='Configuration changed'):
        PaperStore(tmp_path / 'paper.db', changed)


def test_backups_are_actual_sqlite(cfg, tmp_path):
    store = PaperStore(tmp_path / 'p.db', cfg, initialize=True)
    checkpoint({'p.db': tmp_path / 'p.db'}, tmp_path / 'b')
    restore(tmp_path / 'b', {'p.db': tmp_path / 'out.db'})
    with sqlite3.connect(tmp_path / 'out.db') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert store.review()['sessions'] == 0
