"""Health and strategy check for a running bot. Read-only — sends nothing.

Two different questions get answered here, and they are worth separating:

  1. **Is it alive?**  process, connection, risk state, kill switch, journal.
  2. **Is it seeing the strategy?**  swings, breaks of structure, the current
     impulse wave, and either the live 2.6 level or the specific reason there
     is no setup.

Question 2 matters because a bot with no trades looks identical whether it is
correctly standing aside or quietly broken. The daemon only logs when it acts,
so silence proves nothing on its own. This prints what the engine can actually
see right now.

It attaches to the terminal WITHOUT credentials, so running it never switches
the account out from under the daemon.

    python status.py
    python status.py --watch 60 --for 480   # every 60s for 8 hours
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (COSTS, LIVE_TIMEFRAMES, PATHS, RISK, STRATEGY,
                    Timeframe, load_secrets)
from data import Mt5Session
from engine import Direction, TwoSixEngine
from journal import Journal
from risk import size_position

BAR_COUNT = 400


def _bot_processes() -> list[int]:
    """PIDs of python processes running this project's main.py.

    Uses PowerShell rather than `wmic`: wmic is removed from current Windows
    builds, and its absence made this report a confident "NOT RUNNING" for a
    bot that was running fine. A health check that lies about the thing it is
    checking is worse than no health check.
    """
    import subprocess
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*gold-2.6-bot*main.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 - never let the probe break the report
        return []
    return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]


def check(session: Mt5Session, engine: TwoSixEngine, spec, journal: Journal) -> None:
    now = datetime.now(timezone.utc)
    print(f"\n{'=' * 78}")
    print(f"  {now:%Y-%m-%d %H:%M:%S} UTC")
    print("=" * 78)

    # ---- 1. alive -------------------------------------------------------
    pids = _bot_processes()
    print(f"\n[1] PROCESS      {'RUNNING  pids ' + str(pids) if pids else '** NOT RUNNING **'}")

    account = session.account()
    print(f"[2] ACCOUNT      {account.login} ({account.kind})  "
          f"balance {account.balance:,.2f}  equity {account.equity:,.2f}")

    raw_positions = session.mt5.positions_get() or []
    ours = [p for p in raw_positions if p.magic in {tf.magic for tf in Timeframe}]
    raw_orders = session.mt5.orders_get() or []
    our_orders = [o for o in raw_orders if getattr(o, "magic", 0) in {tf.magic for tf in Timeframe}]
    print(f"[3] BROKER       {len(ours)} position(s), {len(our_orders)} pending order(s) of ours")
    for p in ours:
        side = "BUY" if p.type == 0 else "SELL"
        print(f"       position {p.ticket}  {side} {p.volume} @ {p.price_open:.3f}  "
              f"sl {p.sl:.3f}  tp {p.tp:.3f}  floating {p.profit:+.2f}")
    for o in our_orders:
        print(f"       pending  {o.ticket}  @ {o.price_open:.3f}  "
              f"sl {o.sl:.3f}  tp {o.tp:.3f}  vol {o.volume_current}")

    state_file = PATHS.runtime / "risk_state.json"
    if state_file.exists():
        st = json.loads(state_file.read_text(encoding="utf-8"))
        halted = st.get("halted_reason") or "no"
        print(f"[4] RISK STATE   day {st['day']}  realised {st['realised_today']:+.2f}  "
              f"trades {st['trades_today']}  peak {st['peak_equity']:,.2f}  halted: {halted}")
    kill = PATHS.kill_switch
    print(f"[5] KILL SWITCH  {'*** ENGAGED *** ' + kill.read_text(encoding='utf-8')[:120] if kill.exists() else 'off'}")
    print(f"[6] JOURNAL      {journal.summary()}")

    # ---- 2. is it seeing the strategy? ----------------------------------
    spread = session.live_spread_points(spec.name)
    m5 = session.recent_bars(spec.name, Timeframe.M5, 500)
    limit = max(
        float(m5["spread"].quantile(RISK.max_spread_percentile / 100)),
        float(m5["spread"].median()) * RISK.max_spread_median_multiple,
    ) if len(m5) else RISK.max_spread_points
    gate = "OK" if round(spread) <= round(limit) else "BLOCKED"
    print(f"[7] SPREAD       {spread:.0f} points, limit {limit:.0f}  -> {gate}   "
          f"clock drift {session.clock_drift_seconds():+.1f}s")

    print(f"\n[8] STRATEGY RECOGNITION")
    for tf in LIVE_TIMEFRAMES:
        bars = session.recent_bars(spec.name, tf, BAR_COUNT)
        if bars.empty:
            print(f"    {tf.value}: NO BARS — that is a problem")
            continue
        st = engine.analyse(
            bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy(),
            [t.to_pydatetime() for t in bars["timestamp"]],
        )
        last = len(bars) - 1
        close = float(bars["close"].iloc[-1])
        print(f"    {tf.value}: {len(bars)} bars to {bars['timestamp'].iloc[-1]:%m-%d %H:%M}, "
              f"close {close:.3f}")
        print(f"          swings {len(st.swings)}   breaks of structure {len(st.breaks)}")
        if st.breaks:
            b = st.breaks[-1]
            print(f"          last BOS: {b.direction.value} at bar {b.index}, "
                  f"body closed {b.close:.3f} through {b.broken_level:.3f}")

        imp = engine.latest_impulse(st, last)
        if imp is None:
            print(f"          impulse : none yet — BOS has not produced a confirmed Peak")
            continue
        result = imp.range / STRATEGY.retracement_divisor
        level = imp.peak_price - result if imp.direction is Direction.BULLISH else imp.peak_price + result
        print(f"          impulse : {imp.direction.value}  "
              f"Origin {imp.origin_price:.3f} -> Peak {imp.peak_price:.3f}  "
              f"Range {imp.range:.3f}")
        print(f"          2.6      : Range/2.6 = {result:.3f}   "
              f"entry level = {level:.3f}   (price is {close:.3f})")

        setup = engine.signal_at(st, as_of_index=last, reference_price=close,
                                 min_stop_distance=spec.stops_level_points * spec.point)
        if setup is None:
            away = close - level
            why = ("price has already passed the level"
                   if (imp.direction is Direction.BULLISH and close <= level)
                   or (imp.direction is Direction.BEARISH and close >= level)
                   else f"range {imp.range:.2f} below the {STRATEGY.min_leg_price} minimum"
                   if imp.range < STRATEGY.min_leg_price else "waiting for price to reach it")
            print(f"          setup   : none — {why}  (price {away:+.3f} from the level)")
        else:
            sizing = size_position(equity=account.equity, spec=spec,
                                   stop_distance=setup.risk_distance,
                                   risk_cfg=RISK, costs=COSTS)
            print(f"          setup   : LIVE  {setup.describe()}")
            print(f"          sizing  : {sizing.describe()}")

    log = PATHS.logs / "bot.log"
    if log.exists():
        lines = [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines()
                 if l.strip()][-4:]
        print(f"\n[9] LOG TAIL")
        for l in lines:
            print(f"    {l[:150]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Health and strategy check")
    parser.add_argument("--watch", type=int, default=0, help="seconds between checks")
    parser.add_argument("--for", dest="minutes", type=int, default=0, help="total minutes")
    args = parser.parse_args()

    secrets = load_secrets()
    # No credentials: attach to whatever the terminal already has, so running
    # this never switches the account the daemon is trading.
    session = Mt5Session(terminal_path=secrets.mt5_terminal_path)
    session.connect()
    spec = session.resolve_symbol(STRATEGY.symbol_search_patterns)
    engine = TwoSixEngine(STRATEGY)
    journal = Journal(PATHS.journal_csv)

    try:
        if not args.watch:
            check(session, engine, spec, journal)
            return
        deadline = time.time() + args.minutes * 60
        while time.time() < deadline:
            check(session, engine, spec, journal)
            time.sleep(args.watch)
    finally:
        session.disconnect()


if __name__ == "__main__":
    main()
