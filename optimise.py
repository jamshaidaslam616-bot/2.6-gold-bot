"""Take-profit search, with an out-of-sample check attached.

The owner asked for the target to be set "so that it stays profitable". That is
a reasonable thing to want and a dangerous thing to do naively: test enough
variants against one stretch of history and one of them will look good by
chance alone. Eleven candidates on a coin-flip strategy will still produce a
"winner", and it will be the one that fits 2022-2024's noise best.

So this tool never reports a single number. Every candidate is measured on two
disjoint periods:

    IN-SAMPLE       the earlier stretch — where the choice is made
    OUT-OF-SAMPLE   the later stretch  — which the choice never saw

A setting that leads in-sample and collapses out-of-sample was fitted to noise.
A setting that holds up in both has at least survived one honest test. Neither
outcome is proof, but the second is evidence and the first is a warning.

Results are reported in **R multiples** rather than dollars. R is per-trade and
size-independent, so it compares cleanly across periods where equity — and
therefore lot size — differs.

Note: only RISK_MULTIPLE at 2.0 is the owner's specification. Every other row
is a deviation from it, and is marked.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

import logger as logging_setup
from backtester import Backtester, IntrabarResolver, load_market
from config import (COSTS, LIVE_TIMEFRAMES, PATHS, RISK, STRATEGY, CostConfig,
                    TakeProfitMode, Timeframe)

log = logging_setup.get("optimise")


@dataclass(frozen=True, slots=True)
class Candidate:
    label: str
    mode: TakeProfitMode
    rr: float = 2.0
    extension: float = 0.0

    @property
    def is_spec(self) -> bool:
        return self.mode is TakeProfitMode.RISK_MULTIPLE and self.rr == 2.0

    def apply(self):
        return replace(
            STRATEGY,
            take_profit_mode=self.mode,
            min_risk_reward=self.rr,
            peak_extension=self.extension,
        )


CANDIDATES = [
    Candidate("peak (0.6R)", TakeProfitMode.PEAK),
    Candidate("peak +25%", TakeProfitMode.PEAK_EXTENDED, extension=0.25),
    Candidate("peak +50%", TakeProfitMode.PEAK_EXTENDED, extension=0.50),
    Candidate("rr 0.75", TakeProfitMode.RISK_MULTIPLE, rr=0.75),
    Candidate("rr 1.00", TakeProfitMode.RISK_MULTIPLE, rr=1.00),
    Candidate("rr 1.25", TakeProfitMode.RISK_MULTIPLE, rr=1.25),
    Candidate("rr 1.50", TakeProfitMode.RISK_MULTIPLE, rr=1.50),
    Candidate("rr 2.00 (SPEC)", TakeProfitMode.RISK_MULTIPLE, rr=2.00),
    Candidate("rr 2.50", TakeProfitMode.RISK_MULTIPLE, rr=2.50),
    Candidate("rr 3.00", TakeProfitMode.RISK_MULTIPLE, rr=3.00),
]


@dataclass
class Split:
    label: str
    n: int
    mean_r: float
    t_stat: float
    win_rate: float
    profit_factor: float

    @property
    def positive(self) -> bool:
        return self.mean_r > 0


def summarise(trades: pd.DataFrame, label: str) -> Split:
    if trades.empty:
        return Split(label, 0, 0.0, 0.0, 0.0, 0.0)
    r = trades["r"].to_numpy()
    net = trades["net"].to_numpy()
    wins, losses = net[net > 0].sum(), abs(net[net < 0].sum())
    se = r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else float("inf")
    return Split(
        label=label,
        n=len(r),
        mean_r=float(r.mean()),
        t_stat=float(r.mean() / se) if se else 0.0,
        win_rate=float((net > 0).mean() * 100),
        profit_factor=float(wins / losses) if losses else float("inf"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Search the take profit, honestly")
    parser.add_argument("--months", type=int, default=48)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--commission", type=float, default=5.50,
                        help="per lot per side. Zero account 5.50, mini 0.00")
    parser.add_argument("--timeframes", default="M15",
                        help="comma separated, e.g. M15,H1. M5 is slow over 4 years")
    parser.add_argument("--split", type=float, default=0.6,
                        help="fraction of the period used in-sample")
    parser.add_argument("--spec-only", action="store_true",
                        help="test only the owner's 1:2 spec — use this when the "
                             "question is which TIMEFRAME has an edge, so the "
                             "answer is not contaminated by also fitting the target")
    args = parser.parse_args()

    candidates = ([c for c in CANDIDATES if c.is_spec] if args.spec_only else CANDIDATES)

    PATHS.ensure()
    logging_setup.setup(PATHS.logs, filename="optimise.log")

    wanted = [Timeframe(t.strip()) for t in args.timeframes.split(",")]
    spec, frames, account = load_market(args.months, symbol_override=args.symbol,
                                        timeframes=wanted)
    costs = CostConfig(commission_per_lot_per_side=args.commission,
                       slippage_points=COSTS.slippage_points)
    intrabar = IntrabarResolver(frames[Timeframe.M5]) if Timeframe.M5 in frames else None

    print(f"\naccount {account}   symbol {spec.name}   "
          f"commission ${args.commission}/lot/side")

    for timeframe in wanted:
        if timeframe not in frames:
            print(f"\n{timeframe.value}: no data from this broker — skipped")
            continue
        bars = frames[timeframe]
        boundary = bars["timestamp"].iloc[int(len(bars) * args.split)]
        print(f"\n{'=' * 96}")
        print(f"{timeframe.value}  —  {len(bars):,} bars, "
              f"median spread {bars['spread'].median():.0f} points")
        print(f"  IN-SAMPLE      {bars['timestamp'].iloc[0]:%Y-%m-%d} -> {boundary:%Y-%m-%d}")
        print(f"  OUT-OF-SAMPLE  {boundary:%Y-%m-%d} -> {bars['timestamp'].iloc[-1]:%Y-%m-%d}")
        print("=" * 96)
        print(f"{'take profit':<16}{'IS n':>6}{'IS meanR':>10}{'IS t':>7}{'IS PF':>7}"
              f"   |{'OOS n':>7}{'OOS meanR':>11}{'OOS t':>8}{'OOS PF':>8}{'OOS win%':>10}")
        print("-" * 96)

        rows = []
        for candidate in candidates:
            engine_cfg = candidate.apply()
            # Halts off: a 10% drawdown mid-period would truncate the sample and
            # make candidates incomparable. The winner is re-run with halts on.
            bt = Backtester(spec, strategy_cfg=engine_cfg, costs=costs,
                            enforce_halts=False)
            result = bt.run(
                bars, timeframe, starting_equity=10_000.0,
                intrabar=intrabar if timeframe.minutes > Timeframe.M5.minutes else None,
            )
            if not result.trades:
                print(f"{candidate.label:<16}  no trades")
                continue

            trades = pd.DataFrame([
                {"closed": t.closed_at, "r": t.r_multiple, "net": t.net_usd}
                for t in result.trades
            ])
            trades["closed"] = pd.to_datetime(trades["closed"], utc=True)
            is_ = summarise(trades[trades["closed"] <= boundary], "IS")
            oos = summarise(trades[trades["closed"] > boundary], "OOS")
            rows.append((candidate, is_, oos))

            mark = "  <- SPEC" if candidate.is_spec else ""
            print(f"{candidate.label:<16}{is_.n:>6}{is_.mean_r:>+10.3f}{is_.t_stat:>7.2f}"
                  f"{is_.profit_factor:>7.2f}   |{oos.n:>7}{oos.mean_r:>+11.3f}"
                  f"{oos.t_stat:>8.2f}{oos.profit_factor:>8.2f}{oos.win_rate:>9.1f}%{mark}")

        if not rows:
            continue

        best = max(rows, key=lambda x: x[1].mean_r)
        candidate, is_, oos = best
        print("-" * 96)
        print(f"Best IN-SAMPLE: {candidate.label}  (mean R {is_.mean_r:+.3f}, t {is_.t_stat:.2f})")
        print(f"  it then did   {oos.mean_r:+.3f} R out-of-sample over {oos.n} trades "
              f"(t {oos.t_stat:.2f}, PF {oos.profit_factor:.2f})")
        if oos.mean_r <= 0:
            print("  VERDICT: collapsed out-of-sample. The in-sample lead was noise.")
        elif oos.t_stat < 2:
            print("  VERDICT: still positive out-of-sample, but not statistically "
                  "separable from zero. Suggestive, not evidence.")
        else:
            print("  VERDICT: holds up out-of-sample at t > 2. Worth taking seriously.")

        survivors = [c.label for c, i, o in rows if i.mean_r > 0 and o.mean_r > 0]
        print(f"  positive in BOTH periods: {len(survivors)}/{len(rows)} candidates"
              f"{'  ' + ', '.join(survivors) if survivors else ''}")
        print("  If most candidates are positive in both, the target barely matters and")
        print("  the edge is elsewhere. If exactly one is, that one is probably luck.")


if __name__ == "__main__":
    main()
