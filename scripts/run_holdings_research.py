#!/usr/bin/env python3
"""Run independent, read-only research on a dated brokerage holdings snapshot.

Does not open PaperStore, restore/checkpoint state, or generate orders.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.research.holdings_overlay import evaluate_coverage, load_research_holdings
from src.research.morning import morning_holdings
from src.research.runner import json_safe


def build_report(config_path, snapshot_path, now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    snapshot = load_research_holdings(snapshot_path)
    deep = morning_holdings(cfg, now, codes=snapshot["priority_codes"])
    return json_safe({
        "mode": "holdings_research_only",
        "generated_at": now.isoformat(),
        "snapshot_date": snapshot["snapshot_date"],
        "snapshot_is_live": False,
        "research_codes": snapshot["priority_codes"],
        "watch_only_codes": snapshot["watch_only_codes"],
        "coverage": evaluate_coverage(deep.get("holdings", []), snapshot),
        "deep_research": deep,
        "limitations": snapshot["limitations"] + [
            "Historical broker screenshot, not live portfolio data.",
            "No paper or real trading actions; no account state is read or written.",
            "Analysis availability is not equivalent to verified fundamental/news completeness.",
        ],
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/research-system.yaml")
    parser.add_argument("--snapshot", default="config/holdings-research-2026-10-21.yaml")
    parser.add_argument("--output", default="reports/holdings/holdings-latest.json")
    args = parser.parse_args(argv)
    report = build_report(args.config, args.snapshot)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "coverage": report["coverage"],
                      "output": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
