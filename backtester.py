"""Historical simulation of the 2.6 strategy.

The simulator is an independent *runner*, not an independent implementation.
Every trading decision comes from `engine.py` and every lot size from
`risk.py` — the same code the live daemon calls. A backtest that reimplements
the rules measures a strategy that will never ship.

--------------------------------------------------------------------------
Where backtests lie, and what is done about it here
--------------------------------------------------------------------------

**Look-ahead.** Handled in `engine.py`: a fractal is invisible until
`confirmed_at`. The simulator never touches `swings` directly, only through
`engine.visible_swings()`.

**Intrabar sequence.** When one bar contains both the stop and the target,
OHLC alone cannot say which came first. Guessing in our favour is how a losing
strategy backtests beautifully. Two defences:

  * for M15 and H1, the bar is replayed using the M5 bars inside it, so the
    order is *observed* rather than assumed;
  * where no finer data exists (M5 itself), the tie is resolved **against us** —
    stop first, always.

**Costs.** Spread is taken per bar from the data's own `spread` column, not
averaged. Commission is $5.50 per lot per side, measured from a real fill on
this broker rather than taken from a forum. Slippage is applied against us at
both ends.

**Survivorship of the risk limits.** The daily-loss halt and the drawdown kill
switch are enforced during the replay. If the strategy would have tripped the
10% ceiling in March, the simulation stops trading in March — because that is
what the live bot would have done. A backtest that trades through a halt it
would really have hit is reporting a system nobody is running.
"""
from __future__ import annotations

import argparse
import math
import csv
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import logger as logging_setup
from config import (
    BACKTEST_MONTHS,
    COSTS,
    FETCH_CHUNK_DAYS,
    LIVE_TIMEFRAMES,
    PATHS,
    RISK,
    MANAGEMENT,
    STRATEGY,
    CostConfig,
    ManagementConfig,
    RiskConfig,
    StrategyConfig,
    Timeframe,
)
from data import BarStore, Mt5Session, SymbolSpec
from engine import Direction, Setup, SetupState, TwoSixEngine
from risk import RiskManager, size_position

log = logging_setup.get("backtest")


@dataclass(frozen=True, slots=True)
class Trade:
    timeframe: str
    direction: str
    opened_at: datetime
    closed_at: datetime
    entry: float
    stop_loss: float
    take_profit: float
    exit_price: float
    exit_reason: str          # "tp" | "sl" | "end_of_data"
    lots: float
    leg: float
    gross_usd: float
    spread_usd: float
    slippage_usd: float
    commission_usd: float
    net_usd: float
    equity_after: float
    r_multiple: float

    @property
    def won(self) -> bool:
        return self.net_usd > 0


@dataclass
class BacktestResult:
    timeframe: str
    symbol: str
    bars: int
    period_start: datetime | None
    period_end: datetime | None
    starting_equity: float
    trades: list[Trade] = field(default_factory=list)
    setups_created: int = 0
    setups_filled: int = 0
    setups_expired: int = 0
    setups_invalidated: int = 0
    setups_superseded: int = 0
    skipped_by_risk: dict[str, int] = field(default_factory=dict)
    halted_at: datetime | None = None
    halt_reason: str = ""

    # -- metrics ------------------------------------------------------------

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def losses(self) -> int:
        return self.total_trades - self.wins

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades * 100 if self.trades else 0.0

    @property
    def gross_profit(self) -> float:
        return sum(t.net_usd for t in self.trades if t.net_usd > 0)

    @property
    def gross_loss(self) -> float:
        return abs(sum(t.net_usd for t in self.trades if t.net_usd < 0))

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss else float("inf")

    @property
    def net_pnl(self) -> float:
        return sum(t.net_usd for t in self.trades)

    @property
    def final_equity(self) -> float:
        return self.trades[-1].equity_after if self.trades else self.starting_equity

    @property
    def net_return_pct(self) -> float:
        return (self.final_equity - self.starting_equity) / self.starting_equity * 100

    @property
    def total_costs(self) -> float:
        return sum(t.spread_usd + t.slippage_usd + t.commission_usd for t in self.trades)

    @property
    def expectancy_usd(self) -> float:
        return self.net_pnl / self.total_trades if self.trades else 0.0

    @property
    def avg_r(self) -> float:
        return float(np.mean([t.r_multiple for t in self.trades])) if self.trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        """Peak-to-trough on the closed-trade equity curve."""
        if not self.trades:
            return 0.0
        curve = np.array([self.starting_equity] + [t.equity_after for t in self.trades])
        peaks = np.maximum.accumulate(curve)
        return float(np.max((peaks - curve) / peaks) * 100)

    @property
    def breakeven_win_rate(self) -> float:
        """What win rate this trade geometry needs just to break even.

        Comparing it against `win_rate` is the whole verdict in one line: a
        strategy winning 55% on a 1:2 needs only 33%, and one winning 80% on a
        near target may still be underwater.
        """
        if not self.trades:
            return 0.0
        avg_win = self.gross_profit / self.wins if self.wins else 0.0
        avg_loss = self.gross_loss / self.losses if self.losses else 0.0
        if avg_win + avg_loss == 0:
            return 0.0
        return avg_loss / (avg_win + avg_loss) * 100

    def describe(self) -> str:
        if not self.trades:
            return (
                f"\n{'=' * 68}\n{self.timeframe} {self.symbol} — NO TRADES\n{'=' * 68}\n"
                f"  bars {self.bars:,}  setups created {self.setups_created}  "
                f"expired {self.setups_expired}  invalidated {self.setups_invalidated}  "
                f"superseded {self.setups_superseded}\n"
                f"  skipped by risk: {self.skipped_by_risk or 'none'}\n"
            )
        verdict = "PROFITABLE" if self.net_pnl > 0 else "LOSS-MAKING"
        lines = [
            "",
            "=" * 68,
            f"{self.timeframe}  {self.symbol}  —  {verdict}",
            "=" * 68,
            f"  period            {self.period_start:%Y-%m-%d} -> {self.period_end:%Y-%m-%d}"
            f"  ({self.bars:,} bars)",
            f"  starting equity   ${self.starting_equity:,.2f}",
            f"  final equity      ${self.final_equity:,.2f}",
            "",
            f"  Total Trades      {self.total_trades}",
            f"  Win Rate          {self.win_rate:.1f}%   ({self.wins}W / {self.losses}L)",
            f"  Break-even needed {self.breakeven_win_rate:.1f}%   "
            f"<- {'CLEARS IT' if self.win_rate >= self.breakeven_win_rate else 'FALLS SHORT'}",
            f"  Profit Factor     {self.profit_factor:.2f}",
            f"  Max Drawdown      {self.max_drawdown_pct:.2f}%",
            f"  Net Return        {self.net_return_pct:+.2f}%   (${self.net_pnl:+,.2f})",
            f"  Expectancy/trade  ${self.expectancy_usd:+.2f}   ({self.avg_r:+.3f} R)",
            "",
            f"  Costs paid        ${self.total_costs:,.2f}  "
            f"({self.total_costs / abs(self.net_pnl) * 100:.0f}% of |net P&L|)"
            if self.net_pnl else f"  Costs paid        ${self.total_costs:,.2f}",
            f"  Setups            {self.setups_created} created, {self.setups_filled} filled, "
            f"{self.setups_expired} expired, {self.setups_invalidated} invalidated, "
            f"{self.setups_superseded} superseded",
            f"  Skipped by risk   {self.skipped_by_risk or 'none'}",
        ]
        if self.halted_at:
            lines += ["", f"  *** HALTED {self.halted_at:%Y-%m-%d}: {self.halt_reason} ***"]
        return "\n".join(lines) + "\n"


class IntrabarResolver:
    """Replays a parent bar using the M5 bars inside it.

    Without this, a bar that contains both the stop and the target is a coin
    flip the simulator gets to call. With it, the order is read off the data.
    """

    def __init__(self, m5: pd.DataFrame) -> None:
        # Epoch nanoseconds rather than Timestamps: the series is tz-aware, and
        # numpy's searchsorted has no representation for a timezone, so it
        # silently falls back to comparing tz-aware against tz-naive and raises.
        self._ts = m5["timestamp"].astype("int64").to_numpy()
        self._high = m5["high"].to_numpy()
        self._low = m5["low"].to_numpy()

    def steps(self, start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[float, float]]:
        """(high, low) of each M5 bar in [start, end), oldest first."""
        lo = int(np.searchsorted(self._ts, start.value, side="left"))
        hi = int(np.searchsorted(self._ts, end.value, side="left"))
        return [(float(self._high[i]), float(self._low[i])) for i in range(lo, hi)]


@dataclass
class _OpenTrade:
    setup: Setup
    lots: float
    entry_price: float
    opened_at: datetime
    opened_index: int
    spread_price_at_entry: float
    stop: float                  # moves when management is on; else the setup's
    best_price: float            # most favourable price seen, for trailing
    remaining_lots: float
    realised_partial: float = 0.0
    partial_taken: bool = False

    @property
    def risk(self) -> float:
        return abs(self.entry_price - self.setup.stop_loss)


class Backtester:
    def __init__(
        self,
        spec: SymbolSpec,
        strategy_cfg: StrategyConfig = STRATEGY,
        risk_cfg: RiskConfig = RISK,
        costs: CostConfig = COSTS,
        management: ManagementConfig = MANAGEMENT,
        *,
        enforce_halts: bool = True,
    ) -> None:
        self.spec = spec
        self.engine = TwoSixEngine(strategy_cfg)
        self.risk_cfg = risk_cfg
        self.costs = costs
        self.management = management
        self.enforce_halts = enforce_halts

    def _spread_price(self, spreads: np.ndarray, index: int) -> float:
        """This bar's spread in price units."""
        points = (
            float(spreads[index]) if self.costs.use_bar_spread
            else self.costs.fallback_spread_points
        )
        if points <= 0:
            points = self.costs.fallback_spread_points
        return points * self.spec.point

    # -- exit detection -----------------------------------------------------

    def _take_partial(self, trade: _OpenTrade, level: float) -> None:
        """Close part of the position at `level`, if the broker would allow it.

        At $10,000 with 0.5% risk the sizes are 0.01-0.02 lots, and half of
        0.01 is below the 0.01 minimum — so on this account a partial is
        usually not executable at all. Rather than pretend otherwise, the
        partial is simply skipped when the remainder would be untradeable.
        """
        step, minimum = self.spec.volume_step, self.spec.volume_min
        want = trade.remaining_lots * self.management.partial_fraction
        lots = math.floor(want / step + 1e-9) * step
        if lots < minimum or trade.remaining_lots - lots < minimum:
            trade.partial_taken = True      # decided, and the answer was "cannot"
            return
        sign = 1.0 if trade.setup.direction is Direction.BULLISH else -1.0
        gross = (level - trade.entry_price) * sign * lots * self.spec.money_per_price_unit_per_lot
        trade.realised_partial += gross - 2 * self.costs.commission_per_lot_per_side * lots
        trade.remaining_lots = round(trade.remaining_lots - lots, 8)
        trade.partial_taken = True

    def _manage_and_exit(
        self,
        trade: _OpenTrade,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_index: int,
        steps: list[tuple[float, float]] | None,
    ) -> tuple[str, float] | None:
        """Walk the bar, apply position management, and report any exit.

        With `steps` the sub-bars are walked in order and the first level
        touched wins — the honest answer. Without them the stop is checked
        first, which is the answer that cannot flatter us.

        The stop is always tested against its value at the *start* of the step,
        before that step's own high is allowed to tighten it. Otherwise a bar
        that ran up and back down could trail the stop using its own high and
        then be stopped out by its own low — the simulator inventing an exit
        that no real stop order could have produced.
        """
        cfg = self.management
        bullish = trade.setup.direction is Direction.BULLISH
        risk = trade.risk
        sequence = steps if steps else [(bar_high, bar_low)]

        for high, low in sequence:
            stop_at_step_start = trade.stop
            if bullish and low <= stop_at_step_start:
                return self._stop_reason(trade, stop_at_step_start), stop_at_step_start
            if not bullish and high >= stop_at_step_start:
                return self._stop_reason(trade, stop_at_step_start), stop_at_step_start

            favourable = (high - trade.entry_price) if bullish else (trade.entry_price - low)
            r_now = favourable / risk if risk else 0.0

            if cfg.partial_at_r and not trade.partial_taken and r_now >= cfg.partial_at_r:
                level = trade.entry_price + cfg.partial_at_r * risk * (1 if bullish else -1)
                self._take_partial(trade, level)

            if cfg.breakeven_at_r and r_now >= cfg.breakeven_at_r:
                trade.stop = (max(trade.stop, trade.entry_price) if bullish
                              else min(trade.stop, trade.entry_price))

            if cfg.trails and r_now >= cfg.trail_from_r:
                trade.best_price = max(trade.best_price, high) if bullish else min(trade.best_price, low)
                offset = cfg.trail_distance_r * risk
                trailed = trade.best_price - offset if bullish else trade.best_price + offset
                trade.stop = max(trade.stop, trailed) if bullish else min(trade.stop, trailed)

            if bullish and high >= trade.setup.take_profit:
                return "tp", trade.setup.take_profit
            if not bullish and low <= trade.setup.take_profit:
                return "tp", trade.setup.take_profit

        if cfg.max_hold_bars and (bar_index - trade.opened_index) >= cfg.max_hold_bars:
            return "time", bar_close
        return None

    @staticmethod
    def _stop_reason(trade: _OpenTrade, stop: float) -> str:
        """Distinguish the original structural stop from a moved one, so the
        report can say whether management helped or merely got in the way."""
        if abs(stop - trade.setup.stop_loss) < 1e-9:
            return "sl"
        return "breakeven" if abs(stop - trade.entry_price) < 1e-9 else "trail"

    # -- the replay ---------------------------------------------------------

    def run(
        self,
        bars: pd.DataFrame,
        timeframe: Timeframe,
        *,
        starting_equity: float = 10_000.0,
        intrabar: IntrabarResolver | None = None,
    ) -> BacktestResult:
        result = BacktestResult(
            timeframe=timeframe.value,
            symbol=self.spec.name,
            bars=len(bars),
            period_start=bars["timestamp"].iloc[0].to_pydatetime() if len(bars) else None,
            period_end=bars["timestamp"].iloc[-1].to_pydatetime() if len(bars) else None,
            starting_equity=starting_equity,
        )
        if len(bars) < 100:
            return result

        ts = bars["timestamp"].to_numpy()
        highs = bars["high"].to_numpy()
        lows = bars["low"].to_numpy()
        closes = bars["close"].to_numpy()
        spreads = bars["spread"].to_numpy()
        times = [pd.Timestamp(t).to_pydatetime() for t in ts]
        bar_span = timedelta(minutes=timeframe.minutes)

        structure = self.engine.analyse(highs, lows, closes, times)
        # What counts as an abnormal spread is a property of this instrument,
        # measured from its own history rather than assumed.
        spread_limit = max(
            float(np.percentile(spreads, self.risk_cfg.max_spread_percentile)),
            float(np.median(spreads)) * self.risk_cfg.max_spread_median_multiple,
        )
        log.info(
            "%s: %d bars, %d swings, %d breaks of structure, "
            "median spread %.0f, p%.0f cutoff %.0f points",
            timeframe.value, len(bars), len(structure.swings), len(structure.breaks),
            float(np.median(spreads)), self.risk_cfg.max_spread_percentile, spread_limit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            risk = RiskManager(
                self.risk_cfg, self.costs,
                Path(tmp) / "state.json", Path(tmp) / "KILL_SWITCH",
            )
            equity = starting_equity
            risk.state.peak_equity = equity
            risk.state.day_start_equity = equity
            risk.state.day = times[0].date().isoformat()

            pending: Setup | None = None
            # A list, so `max_concurrent_positions` can be tested rather than
            # assumed. One order rests at a time either way — that is how the
            # live daemon works — so concurrency builds up one fill at a time.
            open_trades: list[_OpenTrade] = []
            room = self.risk_cfg.max_concurrent_positions

            for t in range(len(bars)):
                bar_high, bar_low = float(highs[t]), float(lows[t])
                now = times[t]

                if self.enforce_halts and risk.state.roll_day(now.date().isoformat(), equity):
                    pass  # counters reset; drawdown ceiling deliberately does not

                # 1. open positions are managed before anything new is considered
                if open_trades:
                    steps = intrabar.steps(
                        pd.Timestamp(ts[t]), pd.Timestamp(ts[t]) + bar_span
                    ) if intrabar else None
                    survivors: list[_OpenTrade] = []
                    for held in open_trades:
                        hit = self._manage_and_exit(
                            held, bar_high, bar_low, float(closes[t]), t, steps
                        )
                        if hit is None:
                            survivors.append(held)
                            continue
                        reason, price = hit
                        trade, equity = self._close(
                            held, price, reason, now, equity, timeframe.value
                        )
                        result.trades.append(trade)
                        risk.record_closed_trade(trade.net_usd)
                        risk.observe_equity(equity, now)   # bar time, not wall clock
                        if self.enforce_halts and risk.kill_switch_engaged() \
                                and result.halted_at is None:
                            result.halted_at = now
                            result.halt_reason = risk.state.halted_reason
                    open_trades = survivors

                # 2. a pending setup is resolved on this bar
                if pending is not None:
                    state = self.engine.update_setup(
                        pending, bar_index=t, bar_high=bar_high,
                        bar_low=bar_low, structure=structure,
                        spread=self._spread_price(spreads, t),
                    )
                    if state is SetupState.FILLED:
                        opened = self._try_open(
                            pending, t, now, spreads, spread_limit, equity, risk, result,
                            open_positions=len(open_trades),
                        )
                        if opened is not None:
                            open_trades.append(opened)
                            result.setups_filled += 1
                        pending = None
                        continue
                    if state is SetupState.INVALIDATED:
                        result.setups_invalidated += 1
                        pending = None
                    elif state is SetupState.EXPIRED:
                        result.setups_expired += 1
                        pending = None
                    elif state is SetupState.SUPERSEDED:
                        result.setups_superseded += 1
                        pending = None

                # 3. only now, at the close of bar t, may a new setup be formed.
                #    It can therefore not act before bar t+1.
                if pending is None and len(open_trades) < room:
                    setup = self.engine.signal_at(
                        structure,
                        as_of_index=t,
                        reference_price=float(closes[t]),
                        min_stop_distance=self.spec.stops_level_points * self.spec.point,
                    )
                    if setup is not None:
                        pending = setup
                        result.setups_created += 1

            # anything still open when the data runs out is marked to market
            for held in open_trades:
                trade, equity = self._close(
                    held, float(closes[-1]), "end_of_data", times[-1],
                    equity, timeframe.value,
                )
                result.trades.append(trade)

        return result

    # -- open / close -------------------------------------------------------

    def _try_open(
        self,
        setup: Setup,
        index: int,
        when: datetime,
        spreads: np.ndarray,
        spread_limit: float,
        equity: float,
        risk: RiskManager,
        result: BacktestResult,
        open_positions: int = 0,
    ) -> _OpenTrade | None:
        spread_price = self._spread_price(spreads, index)
        spread_points = spread_price / self.spec.point

        if self.enforce_halts:
            decision = risk.can_open(
                is_demo=True,
                open_positions=open_positions,
                spread_points=spread_points,
                clock_drift_seconds=0.0,
                equity=equity,
                margin_free=equity,   # backtest does not model margin utilisation
                spread_limit_points=spread_limit,
            )
            if not decision.allowed:
                key = decision.reason.split("(")[0].strip()[:48]
                result.skipped_by_risk[key] = result.skipped_by_risk.get(key, 0) + 1
                return None

        sizing = size_position(
            equity=equity, spec=self.spec, stop_distance=setup.risk_distance,
            risk_cfg=self.risk_cfg, costs=self.costs,
        )
        if not sizing.ok:
            key = "size below broker minimum"
            result.skipped_by_risk[key] = result.skipped_by_risk.get(key, 0) + 1
            return None

        return _OpenTrade(
            setup=setup,
            lots=sizing.lots,
            entry_price=setup.entry,
            opened_at=when,
            opened_index=index,
            spread_price_at_entry=spread_price,
            stop=setup.stop_loss,
            best_price=setup.entry,
            remaining_lots=sizing.lots,
        )

    def _close(
        self,
        trade: _OpenTrade,
        exit_price: float,
        reason: str,
        when: datetime,
        equity: float,
        timeframe: str,
    ) -> tuple[Trade, float]:
        setup = trade.setup
        mppu = self.spec.money_per_price_unit_per_lot
        sign = 1.0 if setup.direction is Direction.BULLISH else -1.0

        # Only the lots still open are closed here; anything taken as a partial
        # was already realised at its own price and is carried in
        # `realised_partial`.
        closing_lots = trade.remaining_lots
        gross = (exit_price - trade.entry_price) * sign * closing_lots * mppu
        # The spread is NOT subtracted here — it is already embedded in `gross`.
        # The fill trigger required the bid to travel a further `spread` before
        # the order could execute, and the entry is recorded at the ask we paid.
        # Deducting it again would charge the spread twice. It is still recorded
        # so the report can show what the spread actually cost.
        spread_usd = trade.spread_price_at_entry * trade.lots * mppu
        slippage_usd = 2 * self.costs.slippage_points * self.spec.point * trade.lots * mppu
        commission_usd = 2 * self.costs.commission_per_lot_per_side * closing_lots
        net = gross + trade.realised_partial - slippage_usd - commission_usd

        # R is measured against the risk originally taken on the full position,
        # so a partial that reduces exposure shows up as a smaller R rather than
        # being flattered by a shrinking denominator.
        risk_usd = setup.risk_distance * trade.lots * mppu
        equity_after = equity + net
        return (
            Trade(
                timeframe=timeframe,
                direction=setup.direction.value,
                opened_at=trade.opened_at,
                closed_at=when,
                entry=round(trade.entry_price, self.spec.digits),
                stop_loss=round(setup.stop_loss, self.spec.digits),
                take_profit=round(setup.take_profit, self.spec.digits),
                exit_price=round(exit_price, self.spec.digits),
                exit_reason=reason,
                lots=trade.lots,
                leg=round(setup.impulse.range, self.spec.digits),
                gross_usd=round(gross, 2),
                spread_usd=round(spread_usd, 2),
                slippage_usd=round(slippage_usd, 2),
                commission_usd=round(commission_usd, 2),
                net_usd=round(net, 2),
                equity_after=round(equity_after, 2),
                r_multiple=round(net / risk_usd, 3) if risk_usd else 0.0,
            ),
            equity_after,
        )


def export_csv(results: list[BacktestResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "timeframe", "direction", "opened_at", "closed_at", "entry", "stop_loss",
            "take_profit", "exit_price", "exit_reason", "lots", "leg", "gross_usd",
            "spread_usd", "slippage_usd", "commission_usd", "net_usd", "r_multiple",
            "equity_after",
        ])
        for result in results:
            for t in result.trades:
                writer.writerow([
                    result.timeframe, t.direction, t.opened_at.isoformat(),
                    t.closed_at.isoformat(), t.entry, t.stop_loss, t.take_profit,
                    t.exit_price, t.exit_reason, t.lots, t.leg, t.gross_usd,
                    t.spread_usd, t.slippage_usd, t.commission_usd, t.net_usd,
                    t.r_multiple, t.equity_after,
                ])
    log.info("wrote %d trades to %s", sum(r.total_trades for r in results), path)


def load_market(
    months: int,
    *,
    refresh: bool = False,
    symbol_override: str | None = None,
    timeframes: Sequence[Timeframe] | None = None,
) -> tuple[SymbolSpec, dict[Timeframe, pd.DataFrame], str]:
    """Contract spec plus bars for all three timeframes.

    `symbol_override` exists because a terminal is logged into one account at a
    time, and the two demo accounts here carry different gold symbols. Rather
    than switching the terminal — which would knock another bot off its own
    symbol — a symbol that is not on the current account is served from the
    parquet cache, and the contract spec is borrowed from the symbol that *is*
    resolvable. That borrowing is safe only because the two contracts were
    verified identical (3 digits, 0.001 point/tick, 0.1 tick value, 100 oz,
    0.01 min/step); they differ in spread and commission, which are cost
    parameters rather than contract geometry. It is logged loudly regardless.
    """
    session = Mt5Session()   # attach only; never switches the terminal's account
    with session:
        account = session.account()
        live_spec = session.resolve_symbol(STRATEGY.symbol_search_patterns)
        spec = live_spec
        if symbol_override and symbol_override != live_spec.name:
            probe = session.mt5.symbol_info(symbol_override)
            if probe is None:
                log.warning(
                    "%s is not on account %s (which carries %s). Serving it from "
                    "the parquet cache and borrowing %s's contract spec — verified "
                    "identical geometry, different costs.",
                    symbol_override, account.login, live_spec.name, live_spec.name,
                )
            spec = replace(live_spec, name=symbol_override)

        store = BarStore(session, PATHS.cache, FETCH_CHUNK_DAYS)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=months * 30)
        wanted = list(timeframes or LIVE_TIMEFRAMES)
        if Timeframe.M5 not in wanted:
            wanted.append(Timeframe.M5)   # always needed for intrabar resolution
        frames: dict[Timeframe, pd.DataFrame] = {}
        for tf in wanted:
            frame, report = store.history(spec.name, tf, start, end, refresh=refresh)
            if frame.empty:
                log.warning("no %s %s data available — skipping that timeframe",
                            spec.name, tf.value)
                continue
            frames[tf] = frame
            log.info("%s", report.describe())
        if not frames:
            raise SystemExit(
                f"No {spec.name} data at all. Run once while the terminal is on the "
                f"account that carries {spec.name}."
            )

    return spec, frames, f"{account.login} ({account.kind})"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the 2.6 gold strategy")
    parser.add_argument("--months", type=int, default=BACKTEST_MONTHS)
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--refresh", action="store_true", help="ignore the parquet cache")
    parser.add_argument("--symbol", default=None,
                        help="gold symbol to test. If it is not on the current "
                             "account it is served from the parquet cache")
    parser.add_argument("--timeframes", default=None,
                        help="comma separated, e.g. M15,H1,H4. Default: the live set")
    parser.add_argument("--no-halts", action="store_true",
                        help="do not enforce the daily/drawdown halts (shows raw edge)")
    parser.add_argument("--no-costs", action="store_true",
                        help="zero spread, slippage and commission (shows cost sensitivity)")
    parser.add_argument(
        "--commission", type=float, default=None,
        help="commission per lot per side. Account-type dependent and NOT "
             "interchangeable: verified on this broker, a Zero account (XAUUSD) "
             "charges $5.50/side with a near-zero spread, while a mini account "
             "(XAUUSDm) charges $0.00 and prices the cost into a 160-500 point "
             "spread. Applying the Zero figure to mini data double-counts.",
    )
    args = parser.parse_args()

    PATHS.ensure()
    logging_setup.setup(PATHS.logs, filename="backtest.log")

    if args.no_costs:
        costs = CostConfig(commission_per_lot_per_side=0.0, slippage_points=0.0,
                           use_bar_spread=False, fallback_spread_points=0.0)
    elif args.commission is not None:
        costs = CostConfig(commission_per_lot_per_side=args.commission)
    else:
        costs = COSTS

    wanted = ([Timeframe(t.strip()) for t in args.timeframes.split(",")]
              if args.timeframes else list(LIVE_TIMEFRAMES))
    spec, frames, account = load_market(
        args.months, refresh=args.refresh, symbol_override=args.symbol, timeframes=wanted
    )
    intrabar = IntrabarResolver(frames[Timeframe.M5]) if Timeframe.M5 in frames else None

    print(f"\naccount {account}   symbol {spec.name}")
    print(f"costs: commission ${costs.commission_per_lot_per_side}/lot/side, "
          f"slippage {costs.slippage_points} points, "
          f"spread {'per-bar' if costs.use_bar_spread else costs.fallback_spread_points}")
    print(f"halts: {'enforced' if not args.no_halts else 'DISABLED'}")

    backtester = Backtester(spec, costs=costs, enforce_halts=not args.no_halts)
    results = []
    for tf in wanted:
        if tf not in frames:
            continue
        # M5 and finer have no smaller series to resolve their own bars with, so
        # they fall back to the pessimistic stop-first rule.
        resolver = intrabar if tf.minutes > Timeframe.M5.minutes else None
        result = backtester.run(
            frames[tf], tf, starting_equity=args.equity, intrabar=resolver
        )
        results.append(result)
        print(result.describe())

    suffix = ("_nocost" if args.no_costs else "") + ("_nohalt" if args.no_halts else "")
    export_csv(results, PATHS.reports / f"backtest{suffix}.csv")

    combined = sum(r.net_pnl for r in results)
    print("=" * 68)
    print(f"COMBINED across all three timeframes: ${combined:+,.2f} "
          f"({sum(r.total_trades for r in results)} trades)")
    print("=" * 68)


if __name__ == "__main__":
    main()
