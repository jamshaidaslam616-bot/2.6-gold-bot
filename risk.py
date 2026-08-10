"""Risk and safety. Nothing reaches the broker without passing through here.

The original brief had no risk layer at all beyond "1% per trade" — no daily
loss limit, no drawdown ceiling, no kill switch, no cap on how many positions
could be open at once across three timeframes. This module is that missing
layer, built to the account owner's standing numbers:

    0.5% risk per trade      (not the brief's 1% — the owner's limit stands)
    3%   daily loss  -> halt for the rest of the UTC day
    10%  drawdown    -> halt until a human clears the kill switch
    1    position at a time, across all three timeframes

Two design decisions worth stating outright:

**State is persisted.** A bot that restarts and forgets it has already lost 3%
today will happily lose another 3%. `RiskState` is written to disk after every
change and reloaded on startup, so a crash-loop cannot launder the day's losses.

**Under-size, never over-size.** Lot sizes round *down* to the volume step, and
a size that lands below the broker's minimum is a rejected trade rather than a
trade rounded up. Rounding up to `volume_min` is the quiet way a 0.5% limit
becomes 0.9% on small stops, and it happens exactly when the stop is tightest.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

import logger as logging_setup
from config import CostConfig, RiskConfig
from data import SymbolSpec

log = logging_setup.get("risk")


class RiskDecision(NamedTuple):
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Either a tradeable lot size, or a refusal with the arithmetic attached."""

    lots: float
    risk_usd: float
    risk_pct: float
    stop_distance: float
    commission_usd: float
    rejected: str | None = None

    @property
    def ok(self) -> bool:
        return self.rejected is None and self.lots > 0

    def describe(self) -> str:
        if self.rejected:
            return f"SIZE REJECTED: {self.rejected}"
        return (
            f"{self.lots:.2f} lots  stop={self.stop_distance:.3f}  "
            f"risk=${self.risk_usd:.2f} ({self.risk_pct:.3f}%)  "
            f"commission=${self.commission_usd:.2f}"
        )


def size_position(
    *,
    equity: float,
    spec: SymbolSpec,
    stop_distance: float,
    risk_cfg: RiskConfig,
    costs: CostConfig,
) -> SizingResult:
    """Lots such that a stop-out costs `risk_per_trade_pct` of equity, all-in.

    "All-in" is the part that is easy to skip: commission is part of the loss,
    so it comes out of the risk budget rather than being charged on top of it.
    Otherwise a 0.5% limit is really 0.5% plus whatever the broker takes — and
    on this Zero account that is a measured $11.00 per lot round turn.
    """
    if equity <= 0:
        return SizingResult(0, 0, 0, stop_distance, 0, "equity is zero or negative")
    if stop_distance <= 0:
        return SizingResult(0, 0, 0, stop_distance, 0, "stop distance is zero or negative")

    risk_budget = equity * risk_cfg.risk_per_trade_pct / 100.0
    loss_per_lot = stop_distance * spec.money_per_price_unit_per_lot
    commission_per_lot = costs.commission_per_lot_per_side * 2.0
    cost_per_lot = loss_per_lot + commission_per_lot
    if cost_per_lot <= 0:  # pragma: no cover - defensive
        return SizingResult(0, 0, 0, stop_distance, 0, "cost per lot is not positive")

    raw_lots = risk_budget / cost_per_lot

    # Round DOWN to the volume step. The epsilon absorbs binary float error so
    # that an exact multiple is not knocked down a whole step.
    steps = math.floor(raw_lots / spec.volume_step + 1e-9)
    lots = round(steps * spec.volume_step, 8)

    if lots < spec.volume_min:
        return SizingResult(
            0, 0, 0, stop_distance, 0,
            f"{raw_lots:.4f} lots needed but broker minimum is {spec.volume_min} — "
            f"taking the minimum would risk "
            f"{spec.volume_min * cost_per_lot / equity * 100:.3f}% against a "
            f"{risk_cfg.risk_per_trade_pct}% limit, so the trade is skipped",
        )
    if lots > spec.volume_max:
        lots = spec.volume_max

    risk_usd = lots * cost_per_lot
    return SizingResult(
        lots=lots,
        risk_usd=risk_usd,
        risk_pct=risk_usd / equity * 100.0,
        stop_distance=stop_distance,
        commission_usd=lots * commission_per_lot,
    )


@dataclass
class RiskState:
    """Survives restarts on purpose — see the module docstring."""

    day: str = ""                      # UTC date this counter belongs to
    realised_today: float = 0.0
    trades_today: int = 0
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    halted_reason: str = ""
    history: list[dict] = field(default_factory=list)

    def roll_day(self, today: str, equity: float) -> bool:
        """Reset the daily counters when the UTC date changes. Returns True if
        a roll happened. The drawdown ceiling deliberately does *not* reset —
        it is measured from peak equity, not from midnight."""
        if self.day == today:
            return False
        if self.day:
            self.history.append(
                {"day": self.day, "realised": round(self.realised_today, 2),
                 "trades": self.trades_today}
            )
            self.history = self.history[-90:]
        self.day = today
        self.realised_today = 0.0
        self.trades_today = 0
        self.day_start_equity = equity
        if self.halted_reason.startswith("daily loss"):
            self.halted_reason = ""   # a new day clears a daily-loss halt only
        return True


class RiskManager:
    """The gate. `can_open()` is the only sanctioned way in."""

    def __init__(
        self,
        risk_cfg: RiskConfig,
        costs: CostConfig,
        state_path: Path,
        kill_switch_path: Path,
    ) -> None:
        self.cfg = risk_cfg
        self.costs = costs
        self._state_path = state_path
        self._kill_switch = kill_switch_path
        self.state = self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> RiskState:
        if not self._state_path.exists():
            return RiskState()
        try:
            return RiskState(**json.loads(self._state_path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            # A corrupt state file must not silently become a clean slate —
            # that would erase a halt. Fail loudly instead.
            raise RuntimeError(
                f"risk state at {self._state_path} is unreadable ({exc}). "
                "Refusing to start with unknown risk state; inspect or delete it."
            ) from exc

    def save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(asdict(self.state), indent=2), encoding="utf-8")

    # -- kill switch --------------------------------------------------------

    def kill_switch_engaged(self) -> bool:
        return self._kill_switch.exists()

    def engage_kill_switch(self, reason: str) -> None:
        """Halt until a human removes the file. Not auto-clearing is the point:
        a drawdown limit that resets itself is not a limit."""
        self._kill_switch.parent.mkdir(parents=True, exist_ok=True)
        self._kill_switch.write_text(
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n{reason}\n"
            f"\nDelete this file to resume. Understand why it fired first.\n",
            encoding="utf-8",
        )
        log.critical("KILL SWITCH ENGAGED: %s", reason)

    # -- accounting ---------------------------------------------------------

    def observe_equity(self, equity: float, now: datetime | None = None) -> None:
        """Track peak equity and fire the drawdown ceiling.

        Called on every loop, not only after a close, so that an open position
        running against us counts toward drawdown. Waiting for the close would
        let equity fall 20% while the recorded drawdown stayed at 0.

        `now` is injectable because the backtester replays 2025 while the wall
        clock says 2026. Left to `datetime.now()` the day counters would roll
        on every call during a replay, which silently disables the daily loss
        limit — a limit that never fires is worse than no limit, because it
        looks like one.
        """
        moment = now or datetime.now(timezone.utc)
        today = moment.date().isoformat()
        if self.state.roll_day(today, equity):
            log.info("new UTC day %s, daily counters reset (equity %.2f)", today, equity)

        if equity > self.state.peak_equity:
            self.state.peak_equity = equity

        if self.state.peak_equity > 0:
            drawdown = (self.state.peak_equity - equity) / self.state.peak_equity * 100.0
            if drawdown >= self.cfg.max_drawdown_pct and not self.kill_switch_engaged():
                reason = (
                    f"drawdown {drawdown:.2f}% from peak equity "
                    f"{self.state.peak_equity:.2f} (limit {self.cfg.max_drawdown_pct}%)"
                )
                self.state.halted_reason = reason
                self.engage_kill_switch(reason)
        self.save()

    def record_closed_trade(self, net_pnl: float) -> None:
        self.state.realised_today += net_pnl
        self.state.trades_today += 1
        loss_limit = self._daily_loss_limit_usd()
        if loss_limit and -self.state.realised_today >= loss_limit:
            self.state.halted_reason = (
                f"daily loss {self.state.realised_today:.2f} reached the "
                f"{self.cfg.max_daily_loss_pct}% limit (${loss_limit:.2f})"
            )
            log.warning("HALTED FOR THE DAY: %s", self.state.halted_reason)
        self.save()

    def _daily_loss_limit_usd(self) -> float:
        basis = self.state.day_start_equity or self.state.peak_equity
        return basis * self.cfg.max_daily_loss_pct / 100.0 if basis else 0.0

    # -- the gate -----------------------------------------------------------

    def can_open(
        self,
        *,
        is_demo: bool,
        open_positions: int,
        spread_points: float,
        clock_drift_seconds: float,
        equity: float,
        margin_free: float,
        spread_limit_points: float | None = None,
    ) -> RiskDecision:
        """Every reason we might refuse, checked in order of severity.

        Cheap checks last on purpose: if the kill switch is engaged, the answer
        is no regardless of the spread, and the log should say so.
        """
        if not is_demo:
            return RiskDecision(False, "account is not a demo account")
        if self.kill_switch_engaged():
            return RiskDecision(False, f"kill switch engaged ({self._kill_switch})")
        if self.state.halted_reason:
            return RiskDecision(False, self.state.halted_reason)
        if open_positions >= self.cfg.max_concurrent_positions:
            return RiskDecision(
                False,
                f"{open_positions} position(s) already open, limit is "
                f"{self.cfg.max_concurrent_positions}",
            )
        if self.state.trades_today >= self.cfg.max_trades_per_day:
            return RiskDecision(
                False, f"{self.state.trades_today} trades today, limit is "
                       f"{self.cfg.max_trades_per_day}"
            )
        if abs(clock_drift_seconds) > self.cfg.max_clock_drift_seconds:
            return RiskDecision(
                False,
                f"clock drift {clock_drift_seconds:+.1f}s exceeds "
                f"{self.cfg.max_clock_drift_seconds}s — session and bar-age checks "
                f"cannot be trusted",
            )
        # The caller measures what "normal" is for this instrument; the hard
        # ceiling is only there to catch a nonsense quote.
        limit = min(
            spread_limit_points if spread_limit_points is not None
            else self.cfg.max_spread_points,
            self.cfg.max_spread_points,
        )
        # Rounded on both sides. Brokers quote spread in whole points, but the
        # live figure is derived as (ask - bid) / point, which lands on
        # 50.000000000000014 — enough to "exceed" a limit of exactly 50.
        if round(spread_points) > round(limit):
            return RiskDecision(
                False, f"spread {spread_points:.0f} points exceeds {limit:.0f}"
            )
        if equity > 0 and margin_free / equity * 100.0 < self.cfg.margin_headroom_pct:
            return RiskDecision(
                False,
                f"free margin {margin_free:.2f} is under {self.cfg.margin_headroom_pct}% "
                f"of equity {equity:.2f}",
            )
        return RiskDecision(True, "ok")
