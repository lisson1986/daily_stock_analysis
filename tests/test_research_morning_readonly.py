"""Safety and coverage regression tests for read-only morning research."""
import ast
from pathlib import Path
from unittest.mock import patch

from src.research import morning


def test_morning_has_three_explicit_holdings():
    assert morning.HOLDINGS == ('300136', '301297', '301660')


def test_morning_does_not_import_paper_settlement():
    source = Path('src/research/morning.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not {'apply', 'save_packet', 'checkpoint', 'restore'} & names


def test_morning_reports_no_session_without_model_or_market_calls():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    with patch.object(morning, 'current_session', return_value=(None, None)):
        result = morning.morning_holdings({}, datetime(2026, 10, 18, 8, tzinfo=ZoneInfo('Asia/Shanghai')))
    assert result['complete'] is False
    assert result['holdings'] == []
    assert result.get('coverage', 0) == 0


def test_morning_script_never_settles_or_saves_packet():
    source = Path('scripts/run_research.py').read_text(encoding='utf-8')
    branch = source.split("elif args.mode == 'morning':", 1)[1].split("elif args.mode == 'catch-up':", 1)[0]
    assert 'store.apply(' not in branch
    assert 'store.save_packet(' not in branch
    assert 'close_research(' not in branch


def test_morning_workflow_does_not_publish_state():
    source = Path('.github/workflows/03-research-system.yml').read_text(encoding='utf-8')
    assert "if: steps.mode.outputs.value != 'morning'" in source
    assert "steps.mode.outputs.value != 'initialize' && steps.mode.outputs.value != 'morning'" in source
    assert "steps.mode.outputs.value == 'morning' || steps.publish.outcome == 'success'" in source
