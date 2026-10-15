#!/usr/bin/env python3
"""Read-only audit of live watchlist and fixed portfolio research coverage."""
import argparse
import json
from pathlib import Path
import yaml

HOLDINGS = {"300136": "信维通信", "301297": "富乐德", "301660": "粤芯半导体"}


def audit(cfg):
    watchlist = set(map(str, cfg.get("watchlist", [])))
    deep = int(cfg.get("deep_analysis_count", 0))
    return {"watchlist": sorted(watchlist), "deep_analysis_count": deep,
            "holdings": [{"code": code, "name": name,
                          "fixed_deep_research": code in watchlist}
                         for code, name in HOLDINGS.items()],
            "missing": sorted(set(HOLDINGS) - watchlist),
            "warning": ("Do not edit watchlist in-place on a persisted paper account: "
                        "PaperStore hashes the entire config and rejects mismatches.")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/research-system.yaml")
    p.add_argument("--strict", action="store_true")
    args = p.parse_args()
    result = audit(yaml.safe_load(Path(args.config).read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.strict and result["missing"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
