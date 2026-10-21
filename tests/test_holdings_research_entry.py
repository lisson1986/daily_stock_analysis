"""Behavioral tests for the independent holdings report entrypoint."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts import run_holdings_research as entry


ROOT = Path(__file__).resolve().parents[1]


def test_build_report_is_research_only(monkeypatch):
    captured = {}

    def fake_morning(cfg, now, codes=None):
        captured["codes"] = list(codes)
        return {"holdings": [
            {"code": "301660", "status": "unavailable", "error": "short history"},
            {"code": "301297", "status": "complete", "analysis": {"summary": "mock"}},
        ], "complete": False}

    monkeypatch.setattr(entry, "morning_holdings", fake_morning)
    result = entry.build_report(
        ROOT / "config/research-system.yaml",
        ROOT / "config/holdings-research-2026-10-21.yaml",
        datetime(2026, 10, 21, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert captured["codes"] == ["301660", "301297"]
    assert result["watch_only_codes"] == ["300136"]
    assert result["snapshot_is_live"] is False
    assert result["coverage"]["requested"] == 2
    assert result["coverage"]["analyzed"] == 1
    assert result["coverage"]["full"] == 1


def test_cli_writes_only_requested_report(monkeypatch, tmp_path):
    monkeypatch.setattr(entry, "build_report", lambda *args, **kwargs: {
        "mode": "holdings_research_only", "coverage": {"requested": 2, "analyzed": 0}
    })
    target = tmp_path / "holdings.json"
    assert entry.main(["--output", str(target)]) == 0
    assert target.exists()
    assert not (tmp_path / "paper_research.db").exists()
