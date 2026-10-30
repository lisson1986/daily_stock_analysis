"""Offline regression tests for brokerage research overlay."""
from pathlib import Path

from src.research.holdings_overlay import evaluate_coverage, load_research_holdings


ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_does_not_modify_paper_config():
    snapshot = load_research_holdings(ROOT / "config/holdings-research-2026-10-21.yaml")
    assert snapshot["priority_codes"] == ["301660", "301297"]
    assert snapshot["watch_only_codes"] == ["300136"]
    assert snapshot["research_only"] is True
    assert snapshot["holdings"][0]["shares"] == 1800
    assert snapshot["holdings"][1]["shares"] == 900


def test_limited_history_not_misreported_as_full():
    snapshot = load_research_holdings(ROOT / "config/holdings-research-2026-10-21.yaml")
    packets = [
        {"code": "301660", "status": "limited", "analysis": {"summary": "limited"},
         "history_quality": "limited"},
        {"code": "301297", "status": "complete", "analysis": {"summary": "full"},
         "history_quality": "standard"},
        {"code": "300136", "status": "complete", "analysis": {"summary": "watch"}},
    ]
    result = evaluate_coverage(packets, snapshot)
    assert (result["requested"], result["analyzed"], result["full"], result["limited"]) == (2, 2, 1, 1)
    assert [item["code"] for item in result["items"]] == ["301660", "301297"]


def test_missing_analysis_is_not_success():
    snapshot = load_research_holdings(ROOT / "config/holdings-research-2026-10-21.yaml")
    result = evaluate_coverage([{"code": "301660", "status": "limited"}], snapshot)
    assert result["analyzed"] == 0
    assert result["full"] == 0
    assert result["limited"] == 0
