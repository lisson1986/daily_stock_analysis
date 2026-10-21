"""Read-only real-holdings research overlay.

Never import this module from the paper execution or checkpoint paths.
"""
from __future__ import annotations

from pathlib import Path

import yaml


DEFAULT_SNAPSHOT = Path("config/holdings-research-2026-10-21.yaml")


def load_research_holdings(path=DEFAULT_SNAPSHOT):
    """Return research priorities, without mutating paper strategy or account state."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if data.get("mode") != "research_only" or data.get("schema_version") != 1:
        raise ValueError("Unsupported holdings research snapshot")
    holdings = data.get("holdings", [])
    codes = [str(row["code"]) for row in holdings]
    watch = [str(row["code"]) for row in data.get("watch_only", [])]
    if len(codes) != len(set(codes)) or set(codes) & set(watch):
        raise ValueError("Duplicate or conflicting research symbols")
    if any(len(code) != 6 or not code.isdigit() for code in codes + watch):
        raise ValueError("Invalid stock code")
    return {
        "snapshot_date": data["snapshot_date"],
        "research_only": True,
        "priority_codes": codes,
        "watch_only_codes": watch,
        "holdings": holdings,
        "account": data["account"],
        "limitations": data.get("limitations", []),
    }


def evaluate_coverage(packets, snapshot):
    """Never count a limited-history analysis as a full technical analysis."""
    indexed = {str(packet["code"]): packet for packet in packets}
    results = []
    for code in snapshot["priority_codes"]:
        packet = indexed.get(code, {})
        status = packet.get("status", "missing")
        if status not in ("complete", "limited"):
            status = "unavailable" if packet else "missing"
        results.append({"code": code, "status": status,
                        "has_analysis": bool(packet.get("analysis")),
                        "history_quality": packet.get("history_quality", "unknown")})
    return {
        "requested": len(results),
        "analyzed": sum(r["has_analysis"] for r in results),
        "full": sum(r["status"] == "complete" and r["has_analysis"] for r in results),
        "limited": sum(r["status"] == "limited" and r["has_analysis"] for r in results),
        "items": results,
    }
