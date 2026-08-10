"""The 2.6 strategy engine — the only place trading decisions are made.

Both the backtester and the live daemon import *this* module. If the simulator
reimplemented the rules, the backtest would be measuring a strategy that never
ships. One implementation means a backtest result is a statement about the code
that will actually trade.

--------------------------------------------------------------------------
The rules, as specified by the owner
--------------------------------------------------------------------------

**Step 1 — Break of Structure.** Price breaks a previous swing high or low, and
the break counts only when a candle **body closes** beyond that level. A wick
poking through is not a break. This is what establishes the direction we are
allowed to trade.

**Step 2 — the impulse wave, measured wick to wick.** After the BOS the move
runs until a new swing forms:

    Origin  = the swing the move started from  (bullish: lowest low WICK)
    Peak    = the swing that ended it          (bullish: highest high WICK)

Wicks, not bodies. The range is the full extent price actually reached.

**Step 3 — the 2.6 level.**

    Range  = |Peak - Origin|
    Result = Range / 2.6

    bullish setup:  entry = Peak   - Result      (subtract from the high)
    bearish setup:  entry = Peak   + Result      (add to the low)

`Peak` is whichever extreme ended the impulse, so both lines say the same
thing: retrace 38.46% back from where the move ran out. Note this is *not* the
same as adding Result to the Origin — that lands on the 61.5% level, a
different price, and getting the two confused inverts every trade.

**Step 4 — brackets.**

    stop   = Origin exactly (the absolute wick)
    target = at least 1:2 on that risk

Worth stating what the geometry implies, because it sets the win rate:

    risk = Range - Result = 0.6154 * Range
    tp   = entry + 2*risk = Peak + 0.846 * Range   (bullish)

The target sits **84.6% of the impulse beyond the Peak**. Every winner needs a
substantial new extreme. A strategy shaped like this wins well under half its
trades by construction, and that is not a defect — but it does mean the average
win has to be large, and costs eat exactly that.

**Step 5 — invalidation.** If price breaks the Origin without having touched
the 2.6 level, the setup is dead and we wait for a fresh BOS.

--------------------------------------------------------------------------
Look-ahead
--------------------------------------------------------------------------

A fractal needs `right` bars after the pivot before anyone can know it is a
pivot. Every `Swing` therefore carries `confirmed_at = index + right`, and
nothing here may consult a swing before that bar. `visible_swings()` is the
only accessor and it enforces this.

Look-ahead is dangerous precisely because it makes a backtest look *better*, so
nothing in the output signals that anything is wrong. It has to be proven
absent — see `test_truncation_invariance`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Sequence

import numpy as np

from config import StrategyConfig, TakeProfitMode


class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


class Direction(str, Enum):
    BULLISH = "bullish"   # BOS to the upside; we buy the retracement
    BEARISH = "bearish"   # BOS to the downside; we sell the retracement

    @property
    def is_buy(self) -> bool:
        return self is Direction.BULLISH


class SetupState(str, Enum):
    WAITING = "waiting"
    FILLED = "filled"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"   # Origin broken before the entry was touched
    SUPERSEDED = "superseded"     # a newer BOS replaced this structure


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    confirmed_at: int
    timestamp: datetime
    price: float
    kind: SwingKind


@dataclass(frozen=True, slots=True)
class BreakOfStructure:
    """A candle whose *body* closed beyond a prior swing level.

    `wave_start_index` and `wave_lookback_bar` are what stop the engine from
    measuring sub-legs. In a sustained trend, price breaks structure again and
    again; those later breaks are *continuation of one wave*, not new waves. So
    every break carries a pointer back to the break that started its
    directional run, and the Origin is measured from there.

    Without this the Origin re-anchors onto whatever shallow pullback low
    preceded the most recent break. Measured on 12 months of M15: 49% of setups
    were built on a continuation break, and 15% ended up measuring less than
    half the real wave — one was 11.8x too small ($10.71 against a $125.91
    move). Every level derived from that range is then wrong.
    """

    index: int
    direction: Direction
    broken_level: float
    broken_swing: Swing
    close: float
    wave_start_index: int      # bar of the break that began this directional run
    wave_lookback_bar: int     # bar of the swing that run-starting break took out


@dataclass(frozen=True, slots=True)
class Impulse:
    """The wave: Origin -> Peak.

    Both are raw **wicks**, per the spec ("sab se nichla point yaani Lowest Low
    Wick"). They are not required to be fractal pivots — the extreme of the
    move is the extreme of the move, whether or not it happens to sit on a
    confirmed swing.
    """

    origin_price: float
    origin_index: int
    peak_price: float
    peak_index: int
    bos: BreakOfStructure       # the break that STARTED this wave
    confirmed_at: int           # when the Final Peak became knowable

    @property
    def direction(self) -> Direction:
        return self.bos.direction

    @property
    def range(self) -> float:
        return abs(self.peak_price - self.origin_price)

    @property
    def swing_high(self) -> float:
        return max(self.origin_price, self.peak_price)

    @property
    def swing_low(self) -> float:
        return min(self.origin_price, self.peak_price)


@dataclass(frozen=True, slots=True)
class MarketStructure:
    """Swings, breaks and the wicks they were derived from.

    The price arrays travel with the structure because Origin and Peak are raw
    wick extremes over a span of bars, not pivot prices — so resolving a wave
    needs the bars themselves, not just the pivots found in them.
    """

    swings: tuple[Swing, ...]
    breaks: tuple[BreakOfStructure, ...]
    highs: np.ndarray
    lows: np.ndarray


@dataclass(frozen=True, slots=True)
class Setup:
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    impulse: Impulse
    created_index: int
    expires_index: int

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def risk_reward(self) -> float:
        return self.reward_distance / self.risk_distance if self.risk_distance else 0.0

    def describe(self) -> str:
        return (
            f"{self.direction.value} entry={self.entry:.3f} sl={self.stop_loss:.3f} "
            f"tp={self.take_profit:.3f} rr=1:{self.risk_reward:.2f} "
            f"range={self.impulse.range:.3f} "
            f"(origin {self.impulse.origin_price:.3f} -> peak {self.impulse.peak_price:.3f})"
        )


class TwoSixEngine:
    """Stateless with respect to the market: every method takes the bars it
    needs, so the backtester can call it 70,000 times and the daemon once a
    minute and get identical answers."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    # -- Step 1 and 2: structure -------------------------------------------

    def find_swings(
        self,
        highs: Sequence[float] | np.ndarray,
        lows: Sequence[float] | np.ndarray,
        timestamps: Sequence[datetime],
    ) -> list[Swing]:
        """Every fractal pivot, each tagged with when it became knowable.

        Wick-based: `highs` and `lows` are the candle extremes, which is what
        the spec's "wick-to-wick" requirement means.

        A pivot is strict on both sides. Plateaus (two equal highs) produce no
        pivot at all — an ambiguous structure should not generate a trade.
        """
        left = self.config.swing_left_bars
        right = self.config.swing_right_bars
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        n = len(highs)
        swings: list[Swing] = []
        if n < left + right + 1:
            return swings

        for i in range(left, n - right):
            window_h = highs[i - left : i + right + 1]
            if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
                swings.append(
                    Swing(i, i + right, timestamps[i], float(highs[i]), SwingKind.HIGH)
                )
                continue
            window_l = lows[i - left : i + right + 1]
            if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
                swings.append(
                    Swing(i, i + right, timestamps[i], float(lows[i]), SwingKind.LOW)
                )
        return swings

    def find_breaks(
        self,
        closes: Sequence[float] | np.ndarray,
        highs: Sequence[float] | np.ndarray,
        lows: Sequence[float] | np.ndarray,
        swings: Sequence[Swing],
    ) -> list[BreakOfStructure]:
        """Breaks of structure, in bar order.

        The confirming price is the candle's **close** — its body — not its
        high or low. A wick through the level is not a break, and using the
        wick would fire on every liquidity sweep, which is the exact move this
        rule exists to ignore.

        Each swing level is broken at most once: the first body close through
        it is the BOS, and later closes beyond the same level are continuation,
        not new structure.
        """
        closes = np.asarray(closes, dtype=float)
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        # When body-close confirmation is switched off the wick decides instead.
        up_probe = closes if self.config.bos_requires_body_close else highs
        down_probe = closes if self.config.bos_requires_body_close else lows

        events: list[BreakOfStructure] = []
        pointer = 0
        recent_high: Swing | None = None
        recent_low: Swing | None = None
        broken_high = -1
        broken_low = -1
        # Track the run: the first break in a direction owns the wave, and
        # every continuation break points back to it.
        run_direction: Direction | None = None
        run_start = -1
        run_lookback = -1

        def _record(index: int, direction: Direction, swing: Swing, close: float) -> None:
            nonlocal run_direction, run_start, run_lookback
            if direction is not run_direction:
                run_direction, run_start, run_lookback = direction, index, swing.index
            events.append(
                BreakOfStructure(index, direction, swing.price, swing, close,
                                 run_start, run_lookback)
            )

        for i in range(len(closes)):
            # Only swings already confirmed at bar i are in play.
            while pointer < len(swings) and swings[pointer].confirmed_at <= i:
                swing = swings[pointer]
                if swing.kind is SwingKind.HIGH:
                    recent_high = swing
                else:
                    recent_low = swing
                pointer += 1

            if (
                recent_high is not None
                and recent_high.index != broken_high
                and float(up_probe[i]) > recent_high.price
            ):
                _record(i, Direction.BULLISH, recent_high, float(closes[i]))
                broken_high = recent_high.index

            if (
                recent_low is not None
                and recent_low.index != broken_low
                and float(down_probe[i]) < recent_low.price
            ):
                _record(i, Direction.BEARISH, recent_low, float(closes[i]))
                broken_low = recent_low.index

        return events

    def analyse(
        self,
        highs: Sequence[float] | np.ndarray,
        lows: Sequence[float] | np.ndarray,
        closes: Sequence[float] | np.ndarray,
        timestamps: Sequence[datetime],
    ) -> MarketStructure:
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        swings = self.find_swings(highs, lows, timestamps)
        breaks = self.find_breaks(closes, highs, lows, swings)
        return MarketStructure(tuple(swings), tuple(breaks), highs, lows)

    @staticmethod
    def visible_swings(swings: Sequence[Swing], as_of_index: int) -> list[Swing]:
        """The swings a decision at `as_of_index` may see. The only sanctioned
        accessor — bypassing it is how a simulator starts reading the future."""
        return [s for s in swings if s.confirmed_at <= as_of_index]

    def latest_impulse(
        self, structure: MarketStructure, as_of_index: int
    ) -> Impulse | None:
        """The most recent tradeable wave visible at `as_of_index`.

        Sequence, per the spec and the owner's clarification: a BOS establishes
        direction, then the move runs **until a new swing confirms** — that
        swing is the Peak. Until it does, the impulse is still in progress and
        there is no level to compute, so this returns None. Waiting is correct;
        an entry priced off an unfinished wave is priced off nothing.
        """
        latest: BreakOfStructure | None = None
        for event in reversed(structure.breaks):
            if event.index <= as_of_index:
                latest = event
                break
        if latest is None:
            return None

        # Continuation breaks belong to the wave that started the run. Anchoring
        # to `latest` instead would re-measure from the most recent shallow
        # pullback and shrink the wave — see BreakOfStructure's docstring.
        bos = next(
            (b for b in structure.breaks if b.index == latest.wave_start_index),
            latest,
        )

        bullish = bos.direction is Direction.BULLISH
        peak_kind = SwingKind.HIGH if bullish else SwingKind.LOW
        visible = self.visible_swings(structure.swings, as_of_index)

        # The Final Peak: of every confirmed pivot since the wave began, the one
        # that reached furthest. "Final" matters — an early pivot that price
        # then exceeded was a pause inside the move, not the end of it.
        candidates = [
            s for s in visible
            if s.kind is peak_kind
            and s.index > bos.index
            and (s.price > bos.broken_level if bullish else s.price < bos.broken_level)
        ]
        if not candidates:
            return None
        peak_swing = max(candidates, key=lambda s: s.price if bullish else -s.price)

        # Take the true extreme wick over the wave rather than the pivot's own
        # price: the highest wick can sit on a bar adjacent to the pivot.
        peak_slice = slice(bos.index, peak_swing.index + 1)
        if bullish:
            peak_price = float(structure.highs[peak_slice].max())
            peak_index = bos.index + int(np.argmax(structure.highs[peak_slice]))
        else:
            peak_price = float(structure.lows[peak_slice].min())
            peak_index = bos.index + int(np.argmin(structure.lows[peak_slice]))

        # The Origin: the lowest low (bullish) of the whole wave, from the swing
        # the run-starting break took out through to the Peak. A raw wick, per
        # the spec — it need not be a fractal pivot.
        origin_slice = slice(min(bos.wave_lookback_bar, bos.index), peak_index + 1)
        if bullish:
            origin_price = float(structure.lows[origin_slice].min())
            origin_index = origin_slice.start + int(np.argmin(structure.lows[origin_slice]))
        else:
            origin_price = float(structure.highs[origin_slice].max())
            origin_index = origin_slice.start + int(np.argmax(structure.highs[origin_slice]))

        return Impulse(
            origin_price=origin_price,
            origin_index=origin_index,
            peak_price=peak_price,
            peak_index=peak_index,
            bos=bos,
            confirmed_at=peak_swing.confirmed_at,
        )

    # -- Steps 3 and 4: the 2.6 level and its brackets ----------------------

    def build_setup(
        self,
        impulse: Impulse,
        *,
        as_of_index: int,
        reference_price: float,
        min_stop_distance: float = 0.0,
    ) -> Setup | None:
        """Turn a wave into a bracketed pending entry, or return None.

        Args:
            reference_price: price when the setup is formed. By the time the
                Peak fractal confirms, price may already be through the 2.6
                level; chasing it is a different trade from the one the rules
                describe, so those are rejected.
            min_stop_distance: the broker's `trade_stops_level` in price units.
                Rejected rather than accommodated — the spec puts the stop on
                the Origin, and a stop moved to satisfy a broker rule is a
                different trade wearing the same name.
        """
        cfg = self.config
        price_range = impulse.range
        if price_range < cfg.min_leg_price:
            return None

        result = price_range / cfg.retracement_divisor
        buffer = price_range * cfg.sl_buffer_leg_fraction   # 0.0 by default

        if impulse.direction is Direction.BULLISH:
            entry = impulse.peak_price - result           # Peak - Result
            stop_loss = impulse.origin_price - buffer     # the Origin wick
            if reference_price <= entry:
                return None
        else:
            entry = impulse.peak_price + result           # the low + Result
            stop_loss = impulse.origin_price + buffer
            if reference_price >= entry:
                return None

        risk = abs(entry - stop_loss)
        if risk <= 0 or risk < min_stop_distance:
            return None

        take_profit = _target(cfg, impulse, entry, risk)
        # A target on the wrong side of the entry is not a target.
        if impulse.direction is Direction.BULLISH and take_profit <= entry:
            return None
        if impulse.direction is Direction.BEARISH and take_profit >= entry:
            return None

        return Setup(
            direction=impulse.direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            impulse=impulse,
            created_index=as_of_index,
            expires_index=as_of_index + cfg.setup_ttl_bars,
        )

    def signal_at(
        self,
        structure: MarketStructure,
        as_of_index: int,
        reference_price: float,
        min_stop_distance: float = 0.0,
    ) -> Setup | None:
        impulse = self.latest_impulse(structure, as_of_index)
        if impulse is None:
            return None
        return self.build_setup(
            impulse,
            as_of_index=as_of_index,
            reference_price=reference_price,
            min_stop_distance=min_stop_distance,
        )

    # -- Step 5: lifecycle --------------------------------------------------

    @staticmethod
    def update_setup(
        setup: Setup,
        *,
        bar_index: int,
        bar_high: float,
        bar_low: float,
        structure: MarketStructure,
        spread: float = 0.0,
    ) -> SetupState:
        """What happened to a waiting setup on this bar.

        Order matters. The Origin is checked before the entry, so a bar that
        engulfs both is scored as "structure broke" rather than "filled, then
        stopped out". We cannot know the intrabar sequence from OHLC alone, and
        a simulator that resolves ties in its own favour is lying.

        `spread` makes the fill trigger honest. A buy limit executes on the
        **ask** while bars are quoted on the bid, so the bid must travel a
        further `spread` before the order can fill. Ignoring that fills marginal
        touches that never happened — and a barely-touched entry that then runs
        is a *winner*, so ignoring it inflates the result.
        """
        if setup.direction is Direction.BULLISH:
            if bar_low <= setup.stop_loss:
                return SetupState.INVALIDATED
            if bar_low <= setup.entry - spread:
                return SetupState.FILLED
        else:
            if bar_high >= setup.stop_loss:
                return SetupState.INVALIDATED
            if bar_high >= setup.entry + spread:
                return SetupState.FILLED

        if _newer_break_exists(structure, setup, bar_index):
            return SetupState.SUPERSEDED
        if bar_index >= setup.expires_index:
            return SetupState.EXPIRED
        return SetupState.WAITING


def _target(cfg: StrategyConfig, impulse: Impulse, entry: float, risk: float) -> float:
    """Place the take profit according to the configured mode.

    RISK_MULTIPLE is the spec ("kam az kam 1:2"). The others are deviations,
    kept here so the target can be tested rather than argued about — but note
    what PEAK implies: reward is Range/2.6 against a risk of Range x 0.6154, so
    the reward:risk is 0.625 and the break-even win rate jumps to about 62%.
    A nearer target is not a free improvement; it just moves where the burden
    sits.
    """
    bullish = impulse.direction is Direction.BULLISH
    if cfg.take_profit_mode is TakeProfitMode.RISK_MULTIPLE:
        return entry + cfg.min_risk_reward * risk if bullish else entry - cfg.min_risk_reward * risk
    if cfg.take_profit_mode is TakeProfitMode.PEAK:
        return impulse.peak_price
    extension = impulse.range * cfg.peak_extension
    return impulse.peak_price + extension if bullish else impulse.peak_price - extension


def _newer_break_exists(
    structure: MarketStructure, setup: Setup, bar_index: int
) -> bool:
    """True once structure has broken again since this setup was formed.

    Either direction counts. An opposite break ends the wave outright; a
    same-direction break means price went *through* the Final Peak instead of
    retracing to our level, so the wave extended and the 2.6 price has moved.
    Both leave a resting order priced off a market that no longer exists.
    """
    for event in structure.breaks:
        if event.index <= setup.created_index:
            continue
        if event.index > bar_index:
            break
        return True
    return False
