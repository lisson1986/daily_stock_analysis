"""Regression tests for independent raw Sina daily-bar fallback."""
from unittest.mock import patch

import pandas as pd
import pytest

from src.research.sources import _sina_raw_history, raw_history


def bars():
    return pd.DataFrame([
        {'date': '2026-10-14', 'open': 10.0, 'high': 11.0, 'low': 9.0, 'close': 10.5, 'volume': 100000},
        {'date': '2026-10-15', 'open': 10.5, 'high': 12.0, 'low': 10.0, 'close': 11.0, 'volume': 120000},
    ])


def test_sina_fallback_normalizes_raw_shares():
    with patch('akshare.stock_zh_a_daily', return_value=bars()):
        result = _sina_raw_history('301297', '2026-10-14', '2026-10-15')
    assert list(result['date']) == ['2026-10-14', '2026-10-15']
    assert result.iloc[-1]['volume'] == 120000
    assert result.iloc[-1]['source'] == 'sina_raw_day'
    assert result.iloc[-1]['adjustment'] == 'none'


def test_sina_rejects_duplicate_dates():
    frame = pd.concat([bars(), bars().iloc[[-1]]], ignore_index=True)
    with patch('akshare.stock_zh_a_daily', return_value=frame):
        with pytest.raises(ValueError, match='duplicate'):
            _sina_raw_history('301660', '2026-10-14', '2026-10-15')


def test_raw_history_uses_sina_only_after_two_failures():
    with patch('src.research.sources._tencent_raw_history', side_effect=ConnectionError('tencent')), \
         patch('akshare.stock_zh_a_hist', side_effect=ConnectionError('eastmoney')), \
         patch('src.research.sources._sina_raw_history', return_value=bars()) as sina:
        result = raw_history('301297', '2026-10-14', '2026-10-15')
    assert len(result) == 2
    sina.assert_called_once_with('301297', '2026-10-14', '2026-10-15')


def test_all_sources_fail_closed():
    with patch('src.research.sources._tencent_raw_history', side_effect=ConnectionError('tencent')), \
         patch('akshare.stock_zh_a_hist', side_effect=ConnectionError('eastmoney')), \
         patch('src.research.sources._sina_raw_history', side_effect=ConnectionError('sina')):
        with pytest.raises(ValueError, match='tencent_raw:.*akshare_raw:.*sina_raw:'):
            raw_history('301297', '2026-10-14', '2026-10-15')
