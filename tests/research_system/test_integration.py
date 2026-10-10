"""Offline lifecycle integration: CLI, DSA history bridge, source policy and workflows."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from src.research.paper import PaperStore
from src.research.runner import close_research, current_session
from src.research.sources import accepted_evidence, allow_url, execution_bar
from tests.research_system.test_paper import bar

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfg():
    return yaml.safe_load((ROOT / 'config/research-system.yaml').read_text())


def test_source_domain_spoof_and_unknown_date(cfg):
    assert allow_url('https://www.reuters.com/business/', cfg['source_domains'])
    assert not allow_url('https://reuters.com.evil.example/', cfg['source_domains'])
    assert not allow_url('https://reuters.com@evil.example/', cfg['source_domains'])
    assert not allow_url('http://reuters.com/business/', cfg['source_domains'])
    now = datetime(2026, 10, 9, 18, tzinfo=ZoneInfo('Asia/Shanghai'))
    response = SimpleNamespace(results=[SimpleNamespace(title='A', snippet='B', url='https://reuters.com/a',
                                                        published_date=None)], provider='fixture',
                               success=True, error_message=None)
    assert not accepted_evidence(response, cfg, now)['items'][0]['fresh']
    response.results[0].published_date = '2026-10-09T06:00:00Z'
    assert accepted_evidence(response, cfg, now)['items'][0]['fresh']
    response.results[0].published_date = '2026-10-20'
    assert not accepted_evidence(response, cfg, now)['items'][0]['fresh']


def test_cn_holiday_calendar_and_limit_touch():
    assert current_session(datetime(2026, 10, 1, 18, tzinfo=ZoneInfo('Asia/Shanghai'))) == (None, None)
    assert current_session(datetime(2026, 10, 9, 18, tzinfo=ZoneInfo('Asia/Shanghai'))) == ('2026-10-09', '2026-10-08')
    history = pd.DataFrame([bar(f'2026-09-{n:02d}') for n in range(1, 21)] +
                           [bar('2026-10-09', high=11)])
    assert not execution_bar('600869', history, '2026-10-09')['buyable']
    assert execution_bar('300136', history, '2026-10-09')['buyable']
    history.loc[20, 'low'] = 8
    assert not execution_bar('300136', history, '2026-10-09')['sellable']


def test_cli_restore_fill_replay_checkpoint(cfg, tmp_path):
    env = {**os.environ, 'DATABASE_PATH': str(tmp_path / 'host.db')}
    args = ['--paper-db', str(tmp_path / 'paper.db'), '--state-dir', str(tmp_path / 'checkpoint'),
            '--reports-dir', str(tmp_path / 'reports')]

    def cli(mode, *extra):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/run_research.py'), mode, *args, *extra],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stderr
        return result

    cli('initialize')
    fixture = tmp_path / 'day.json'
    fixture.write_text(json.dumps({'session': '2026-10-08', 'previous_session': '2026-09-30', 'bars': {},
                                   'candidate': {'code': '300136', 'close': 10}, 'research': {'fixture': True}}))
    cli('close', '--fixture', str(fixture))
    fixture.write_text(json.dumps({'session': '2026-10-09', 'previous_session': '2026-10-08',
                                   'bars': {'300136': bar('2026-10-09')}, 'research': {'fixture': True}}))
    cli('close', '--fixture', str(fixture))
    cli('close', '--fixture', str(fixture))
    cli('checkpoint')
    (tmp_path / 'paper.db').unlink()
    for suffix in ('-wal', '-shm'):
        Path(str(tmp_path / 'paper.db') + suffix).unlink(missing_ok=True)
    cli('restore')
    cli('replay')
    report = json.loads((tmp_path / 'reports/replay-2026-10-10.json').read_text()) if (
        tmp_path / 'reports/replay-2026-10-10.json').exists() else json.loads(next(
            (tmp_path / 'reports').glob('replay-*.json')).read_text())
    assert report['replay']['verified_sessions'] == 2
    assert PaperStore(tmp_path / 'paper.db', cfg).state()['positions']['300136']['quantity'] > 0


def test_close_runner_uses_real_dsa_storage_with_mocked_external_io(cfg, tmp_path, monkeypatch):
    from src.analyzer import AnalysisResult, GeminiAnalyzer
    from src.config import Config
    from src.storage import DatabaseManager
    from src.research import runner
    from src.services.screening import snapshot
    from data_provider import DataFetcherManager

    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'host.db'))
    Config.reset_instance()
    DatabaseManager.reset_instance()
    store = PaperStore(tmp_path / 'paper.db', cfg, initialize=True)
    frame = pd.DataFrame([{'code': '300136', 'name': '信维通信', 'price': 10, 'amount': 1e9, 'change_pct': 2},
                          {'code': '301297', 'name': '富乐德', 'price': 10, 'amount': 1e9, 'change_pct': 2}])
    frame.attrs['snapshot_source'] = 'fixture'
    monkeypatch.setattr(snapshot, 'fetch_snapshot_with_fallback', lambda *a, **k: frame)
    dates = pd.bdate_range('2026-07-01', '2026-10-09')
    hist = pd.DataFrame([bar(d.date().isoformat(), close=10 + n / 100,
                            high=11, open=10 + n / 100, low=9.9) for n, d in enumerate(dates)])
    monkeypatch.setattr(runner, 'raw_history', lambda *a: hist)
    monkeypatch.setattr(runner, 'verify_security', lambda *a: {'verified': True})
    monkeypatch.setattr(DataFetcherManager, 'get_fundamental_context', lambda *a: {'fixture': True})
    monkeypatch.setattr(DataFetcherManager, 'get_daily_data', lambda *a, **k: (hist, 'fixture'))
    monkeypatch.setattr(runner, 'collect_news', lambda *a, **k: {
        'available': True, 'items': [{'fresh': True, 'url': 'https://reuters.com/a'}]})
    monkeypatch.setattr(GeminiAnalyzer, 'generate_text', lambda *a, **k: 'Fixture transmission logic')

    def analysis(self, context, **kwargs):
        return AnalysisResult(code=context['code'], name=context['stock_name'], sentiment_score=80,
                              trend_prediction='看多', operation_advice='买入', action='buy',
                              analysis_summary='Fixture only', success=True)

    monkeypatch.setattr(GeminiAnalyzer, 'analyze', analysis)
    report = close_research(cfg, store, datetime(2026, 10, 9, 18, tzinfo=ZoneInfo('Asia/Shanghai')))
    assert report['research']['coverage'] == 2
    assert len(report['state']['orders']) == 1
    assert not report['fills']
    db = DatabaseManager.get_instance()
    assert len(db.get_latest_data('300136', days=5)) == 5
    assert close_research(cfg, store, datetime(2026, 10, 9, 19, tzinfo=ZoneInfo('Asia/Shanghai'))) == report
    DatabaseManager.reset_instance()
    Config.reset_instance()


def test_historical_rule_backtest_isolated_and_reproducible(cfg, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('run_research', ROOT / 'scripts/run_research.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = PaperStore(tmp_path / 'backtest.db', cfg, initialize=True)
    sessions = pd.bdate_range('2026-08-01', periods=30).strftime('%Y-%m-%d').tolist()
    prev = '2026-07-31'
    entries = []
    for n, day in enumerate(sessions):
        price = 10 + n * .01
        entries.append({'session': day, 'previous_session': prev,
                        'bars': {'300136': bar(day, open=price, close=price, high=price + .1, low=price - .1)}})
        prev = day
    packet = {'schema_version': 1, 'point_in_time_universe': True, 'source': 'TEST FIXTURE',
              'fee_assumption': 'explicit test rates', 'previous_session': '2026-07-31', 'sessions': entries}
    result = module.rule_backtest(packet, cfg, store)
    assert result['replay']['verified_sessions'] == 30
    assert result['review']['sell_fills'] > 0
    assert result['mode'] == 'historical_price_rule_baseline'


def test_workflow_opt_in_and_legacy_gate():
    text = (ROOT / '.github/workflows/03-research-system.yml').read_text()
    wf = yaml.safe_load(text)
    assert wf['concurrency']['group'] == 'stock-analysis'
    assert 'RESEARCH_SYSTEM_ENABLED' in wf['jobs']['research']['if']
    assert 'RESEARCH_SYSTEM_ENABLED' in (ROOT / '.github/workflows/00-daily-analysis.yml').read_text()
    assert 'git push --force' not in text
    assert 'NotificationService' not in (ROOT / 'src/research/runner.py').read_text().split('"""')[-1]


def test_confirmed_suspension_carries_stale_valuation(cfg, monkeypatch):
    import akshare
    from src.research.sources import suspension_bar
    from src.research.paper import initial_state, settle
    monkeypatch.setattr(akshare, 'stock_tfp_em', lambda **k: pd.DataFrame([{'代码': '300136'}]))
    hist = pd.DataFrame([bar('2026-10-09')])
    packet = suspension_bar('300136', hist, '2026-10-12')
    assert packet['date'] == '2026-10-12' and packet['price_date'] == '2026-10-09'
    assert packet['volume'] == 0 and not packet['sellable']
    state = initial_state(cfg)
    state['orders'] = [{'code': '300136', 'signal_date': '2026-10-09', 'limit': 10.2}]
    _, fills, notices = settle(state, '2026-10-12', '2026-10-09', {'300136': packet}, cfg)
    assert not fills and notices
    monkeypatch.setattr(akshare, 'stock_tfp_em', lambda **k: pd.DataFrame([{'代码': '301297'}]))
    with pytest.raises(ValueError, match='without confirmed suspension'):
        suspension_bar('300136', hist, '2026-10-12')


def test_raw_fallback_never_adjusts_and_converts_volume(monkeypatch):
    import akshare
    from src.research import sources
    monkeypatch.setattr(sources, '_tencent_raw_history', lambda *a: (_ for _ in ()).throw(ValueError('offline')))

    def raw(**kwargs):
        assert kwargs['adjust'] == ''
        return pd.DataFrame([{'日期': '2026-10-09', '开盘': 10, '收盘': 10, '最高': 10.1,
                              '最低': 9.9, '成交量': 1234}])

    monkeypatch.setattr(akshare, 'stock_zh_a_hist', raw)
    frame = sources.raw_history('300136', '2026-09-01', '2026-10-09')
    assert frame.iloc[0]['volume'] == 123400
    assert frame.iloc[0]['adjustment'] == 'none'
    assert frame.iloc[0]['source'] == 'akshare_raw_day'


def test_state_repository_refuses_public_or_no_write(monkeypatch):
    import requests
    from src.research.state import assert_private_state_repository
    response = SimpleNamespace(status_code=200, json=lambda: {'private': False, 'permissions': {'push': True}})
    monkeypatch.setattr(requests, 'get', lambda *a, **k: response)
    with pytest.raises(ValueError, match='must be private'):
        assert_private_state_repository('owner/state', 'fixture-token')
    response.json = lambda: {'private': True, 'permissions': {'push': False}}
    with pytest.raises(ValueError, match='must be private'):
        assert_private_state_repository('owner/state', 'fixture-token')


def test_archive_tampering_detected(cfg, tmp_path):
    store = PaperStore(tmp_path / 'p.db', cfg, initialize=True)
    store.apply('2026-10-08', '2026-09-30', {}, None, {})
    store.db.execute("UPDATE paper_sessions SET inputs='{}'")
    store.db.commit()
    with pytest.raises(ValueError, match='checksum divergence'):
        store.replay()
