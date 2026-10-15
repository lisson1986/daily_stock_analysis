#!/usr/bin/env python3
"""Independent, conservative A-share reverse-T paper ledger; no broker orders.

CSV input: session,open,high,low,close,volume,signal
signal is one of sell, buyback, hold. Signals MUST be independently frozen before
the session. Orders are simulated at session open, never using the same day's high/low
to infer intraday sequencing. No same-day buy/sell of newly purchased shares.
"""
import argparse
import csv
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

D = lambda x: Decimal(str(x))
CENT = D("0.01")


def money(x):
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def fees(notional, side, commission=D("0.0003"), minimum=D("5"),
         transfer=D("0.00001"), sell_tax=D("0.0005")):
    return money(max(minimum, notional * commission) + notional * transfer +
                 (notional * sell_tax if side == "sell" else D(0)))


def simulate(rows, initial_shares=1500, initial_cost=D("37.911"), lot=300,
             max_out=600, slip_bps=10, initial_cash=D("0")):
    """Reverse T only: existing shares sold first, repurchased later.

    No implicit borrowing or fictitious additional cash. The initial cost is a
    reference basis, not an assertion of brokerage realized P&L. Cash from
    simulated sales may be used to buy back; never buy more than previously sold.
    """
    shares = initial_shares
    out = 0
    cash = D(initial_cash)
    slip = D(slip_bps) / D(10000)
    records = []
    highwater = None
    max_dd = D(0)
    prev_date = ""
    for r in rows:
        date = r["session"]
        if date <= prev_date:
            raise ValueError("Sessions must be strictly increasing")
        prev_date = date
        o, h, l, c, vol = [D(r[k]) for k in ("open", "high", "low", "close", "volume")]
        if min(o, h, l, c) <= 0 or vol < 0 or l > min(o, c) or h < max(o, c):
            raise ValueError("Invalid daily OHLCV")
        signal = r.get("signal", "hold").strip().lower()
        if signal not in ("sell", "buyback", "hold"):
            raise ValueError("Unknown frozen signal")
        side, qty, px, cost = None, 0, None, D(0)
        if signal == "sell" and shares >= lot and out + lot <= max_out:
            side, qty = "sell", lot
            px = money(o * (1 - slip))
            proceeds = px * qty
            cost = fees(proceeds, side)
            cash += proceeds - cost
            shares -= qty
            out += qty
        elif signal == "buyback" and out >= lot:
            px_try = money(o * (1 + slip))
            total = px_try * lot + fees(px_try * lot, "buy")
            if cash >= total:
                side, qty, px = "buy", lot, px_try
                cost = fees(px * qty, side)
                cash -= px * qty + cost
                shares += qty
                out -= qty
        nav = money(cash + shares * c)
        benchmark = money(initial_cash + initial_shares * c)
        if highwater is None or nav > highwater:
            highwater = nav
        dd = (highwater - nav) / highwater if highwater > 0 else D(0)
        max_dd = max(max_dd, dd)
        records.append({"session": date, "signal": signal, "side": side,
                        "quantity": qty, "fill_price": str(px) if px else None,
                        "fees": str(cost), "shares": shares, "unrecovered_shares": out,
                        "cash": str(money(cash)), "nav": str(nav),
                        "buy_hold_nav": str(benchmark),
                        "relative_nav": str(money(nav - benchmark)),
                        "drawdown_pct": str(money(dd * 100))})
    return {"assumptions": {"initial_shares": initial_shares,
                            "initial_reference_cost": str(initial_cost),
                            "lot": lot, "max_out": max_out,
                            "slippage_bps": slip_bps,
                            "initial_cash": str(initial_cash),
                            "method": "prior-frozen signals; next session open; reverse T only",
                            "caveat": "OHLCV cannot verify intraday order-book fills or sequencing"},
            "trades": records,
            "summary": {"sessions": len(records), "fills": sum(bool(x["side"]) for x in records),
                        "final_shares": shares, "unrecovered_shares": out,
                        "final_nav": records[-1]["nav"] if records else None,
                        "final_relative_nav": records[-1]["relative_nav"] if records else None,
                        "max_drawdown_pct": str(money(max_dd * 100))}}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="Frozen-signal daily CSV")
    p.add_argument("--output", required=True, help="JSON report output")
    p.add_argument("--initial-shares", type=int, default=1500)
    p.add_argument("--initial-cost", type=Decimal, default=D("37.911"))
    p.add_argument("--lot", type=int, default=300)
    p.add_argument("--max-out", type=int, default=600)
    p.add_argument("--slippage-bps", type=int, default=10)
    args = p.parse_args()
    if not (args.initial_shares > 0 and args.lot > 0 and
            args.initial_shares % 100 == args.lot % 100 == args.max_out % 100 == 0 and
            0 < args.max_out <= args.initial_shares and args.slippage_bps >= 0):
        p.error("Invalid A-share lot, inventory, or slippage")
    with open(args.input, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    result = simulate(rows, args.initial_shares, args.initial_cost,
                      args.lot, args.max_out, args.slippage_bps)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
