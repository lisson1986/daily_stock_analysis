"""Regression checks for read-only limited-history and delayed close safety."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_research_modules_parse():
    for path in ("src/research/morning.py", "scripts/run_research.py"):
        ast.parse(_source(path), filename=path)


def test_morning_short_history_does_not_fake_indicators():
    source = _source("src/research/morning.py")
    assert "len(history) < 2" in source
    assert "limited_history = len(history) < 20" in source
    assert "'insufficient_history': True" in source
    assert "'status'] = 'limited' if limited_history else 'complete'" in source
    assert "'full_coverage'" in source
    assert "'limited_coverage'" in source


def test_delayed_close_guard_precedes_catchup():
    source = _source("scripts/run_research.py")
    start = source.index("if args.mode == 'close':")
    end = source.index("elif args.mode == 'morning':", start)
    section = source[start:end]
    assert section.index("if local.hour < 16:") < section.index("catch_up(store, cfg, now)")
    assert "'skipped': 'before_completed_session'" in section
    assert "'No catch-up, AI orders, or settlement executed.'" in section
