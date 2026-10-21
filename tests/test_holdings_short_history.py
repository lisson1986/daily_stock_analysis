"""Behavioral tests for short-history morning research, without network or orders."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from src.research import morning


def test_four_real_bars_yield_limited_research(monkeypatch):
    dates = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
    history = pd.DataFrame({
        "date": dates, "open": [30, 31, 32, 33], "high": [32, 33, 34, 35],
        "low": [29, 30, 31, 32], "close": [31, 32, 33, 34],
        "volume": [100, 120, 130, 140], "source": ["fixture"] * 4,
    })
    monkeypatch.setattr(morning, "current_session", lambda now: ("2026-10-21", "2026-10-20"))
    monkeypatch.setattr(morning, "raw_history", lambda *args: history)
    monkeypatch.setattr(morning, "collect_news", lambda *args: {"items": [], "available": False})

    import data_provider
    import src.analyzer
    import src.config
    import src.services.screening.daily as daily

    class Manager:
        def get_fundamental_context(self, code):
            return {"available": False}

    class Analyzer:
        def __init__(self, **kwargs):
            pass

        def analyze(self, context, news_context):
            assert context["technical_analysis"]["insufficient_history"] is True
            assert context["data_quality"]["history_bars"] == 4
            return SimpleNamespace(success=True, to_dict=lambda: {"summary": "fixture"})

    monkeypatch.setattr(data_provider, "DataFetcherManager", Manager)
    monkeypatch.setattr(src.analyzer, "GeminiAnalyzer", Analyzer)
    monkeypatch.setattr(src.config, "get_config", lambda: {})
    monkeypatch.setattr(daily, "compute_daily_features", lambda *args: (_ for _ in ()).throw(
        AssertionError("MA20 must not run on four bars")))

    result = morning.morning_holdings({}, datetime(2026, 10, 21, 9, 30,
        tzinfo=ZoneInfo("Asia/Shanghai")), codes=["301660"])
    assert result["coverage"] == 1
    assert result["full_coverage"] == 0
    assert result["limited_coverage"] == 1
    assert result["holdings"][0]["status"] == "limited"


def test_one_bar_is_unavailable(monkeypatch):
    monkeypatch.setattr(morning, "current_session", lambda now: ("2026-10-21", "2026-10-20"))
    monkeypatch.setattr(morning, "raw_history", lambda *args: pd.DataFrame(
        {"date": ["2026-10-20"], "close": [30], "source": ["fixture"]}))
    result = morning.morning_holdings({}, datetime(2026, 10, 21, 9, 30,
        tzinfo=ZoneInfo("Asia/Shanghai")), codes=["301660"])
    assert result["coverage"] == 0
    assert result["holdings"][0]["status"] == "unavailable"
    assert "Insufficient daily history" in result["holdings"][0]["error"]
