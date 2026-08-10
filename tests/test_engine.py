"""Engine tests against the owner's 2.6 specification.

Three groups:

* **look-ahead** — the group that has to exist. Look-ahead makes a backtest
  look *better*, so nothing in the output reveals it. It must be proven absent.
* **BOS** — specifically that a wick through a level is not a break. That single
  rule is what separates this strategy from one that fires on every sweep.
* **the 2.6 arithmetic** — checked against hand-computed prices, not against the
  implementation restated in the test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from config import StrategyConfig
from engine import (
    BreakOfStructure,
    Direction,
    Impulse,
    MarketStructure,
    SetupState,
    Swing,
    SwingKind,
    TwoSixEngine,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def timestamps(n: int) -> list[datetime]:
    return [BASE_TIME + timedelta(minutes=15 * i) for i in range(n)]


@pytest.fixture
def engine() -> TwoSixEngine:
    return TwoSixEngine(StrategyConfig())


# ---------------------------------------------------------------------------
# A hand-built bullish scenario with a deliberate wick-only sweep at bar 14
# ---------------------------------------------------------------------------
#
#   bar  3  swing HIGH 4050
#   bar 10  swing LOW  4000        <- the Origin
#   bar 14  high 4060 (through 4050) but CLOSE 4040  -> must NOT be a BOS
#   bar 15  close 4055 (body through 4050)           -> IS the BOS
#   bar 19  swing HIGH 4100        <- the Peak
#
HIGHS = [4020, 4030, 4040, 4050, 4045, 4035, 4025, 4020, 4015, 4012,
         4010, 4014, 4018, 4022, 4060, 4058, 4070, 4080, 4090, 4100,
         4095, 4085, 4075, 4070, 4065, 4060, 4055, 4050, 4045, 4040]
LOWS = [4010, 4020, 4030, 4040, 4035, 4025, 4015, 4010, 4005, 4002,
        4000, 4004, 4008, 4012, 4030, 4048, 4060, 4070, 4080, 4090,
        4085, 4075, 4065, 4060, 4055, 4050, 4045, 4040, 4035, 4030]
CLOSES = [4015, 4028, 4038, 4045, 4038, 4028, 4018, 4012, 4008, 4005,
          4003, 4010, 4014, 4018, 4040, 4055, 4068, 4078, 4088, 4098,
          4090, 4080, 4070, 4065, 4060, 4055, 4050, 4045, 4040, 4035]

ORIGIN_PRICE = 4000.0
PEAK_PRICE = 4100.0
RANGE = 100.0
RESULT = RANGE / 2.6                      # 38.4615
EXPECTED_ENTRY = PEAK_PRICE - RESULT      # 4061.5385
EXPECTED_RISK = RANGE - RESULT            # 61.5385


@pytest.fixture
def structure(engine: TwoSixEngine) -> MarketStructure:
    return engine.analyse(HIGHS, LOWS, CLOSES, timestamps(len(HIGHS)))


def random_series(n: int = 400, seed: int = 7):
    """A gold-shaped random walk, real enough to produce plenty of structure."""
    rng = np.random.default_rng(seed)
    closes = 4000 + np.cumsum(rng.normal(0, 1.5, n))
    wick = np.abs(rng.normal(0, 0.8, n)) + 0.1
    return closes + wick, closes - wick, closes, timestamps(n)


def swing(index: int, price: float, kind: SwingKind, right: int = 3) -> Swing:
    return Swing(index, index + right, BASE_TIME + timedelta(minutes=15 * index), price, kind)


def bos(index: int, direction: Direction, broken: Swing, close: float) -> BreakOfStructure:
    """A standalone break — it starts its own run."""
    return BreakOfStructure(index, direction, broken.price, broken, close,
                            wave_start_index=index, wave_lookback_bar=broken.index)


def impulse(origin: float, origin_i: int, peak: float, peak_i: int,
            break_event: BreakOfStructure) -> Impulse:
    return Impulse(origin_price=origin, origin_index=origin_i,
                   peak_price=peak, peak_index=peak_i,
                   bos=break_event, confirmed_at=peak_i + 3)


# ---------------------------------------------------------------------------
# A trending scenario: two bullish breaks in one wave.
#
#   bar  3  swing HIGH 4050
#   bar 10  swing LOW  4000   <- the TRUE Origin, lowest low of the whole wave
#   bar 14  close 4052 through 4050            -> BOS 1, the wave starts here
#   bar 17  swing HIGH 4080
#   bar 21  swing LOW  4070   <- a shallow pullback, NOT the Origin
#   bar 24  close 4086 through 4080            -> BOS 2, continuation
#   bar 27  swing HIGH 4100   <- the FINAL Peak
#
TREND_HIGHS = [4020, 4030, 4040, 4050, 4045, 4035, 4025, 4020, 4015, 4012, 4010,
               4014, 4020, 4035, 4055, 4062, 4072, 4080, 4078, 4076, 4074, 4072,
               4076, 4082, 4088, 4092, 4096, 4100, 4098, 4094, 4090, 4086, 4082, 4078]
TREND_LOWS = [4010, 4020, 4030, 4040, 4035, 4025, 4015, 4008, 4005, 4002, 4000,
              4005, 4012, 4028, 4048, 4056, 4066, 4074, 4073, 4072, 4071, 4070,
              4072, 4078, 4084, 4088, 4092, 4096, 4094, 4090, 4086, 4082, 4078, 4074]
TREND_CLOSES = [4015, 4028, 4038, 4045, 4038, 4028, 4018, 4010, 4008, 4005, 4003,
                4010, 4018, 4032, 4052, 4060, 4070, 4077, 4075, 4074, 4072, 4071,
                4074, 4080, 4086, 4090, 4094, 4098, 4096, 4092, 4088, 4084, 4080, 4076]


@pytest.fixture
def trend(engine: TwoSixEngine) -> MarketStructure:
    return engine.analyse(TREND_HIGHS, TREND_LOWS, TREND_CLOSES,
                          timestamps(len(TREND_HIGHS)))


# ---------------------------------------------------------------------------
# Look-ahead
# ---------------------------------------------------------------------------


def test_swing_confirmation_lags_by_right_bars(engine: TwoSixEngine) -> None:
    highs, lows, closes, ts = random_series()
    for s in engine.find_swings(highs, lows, ts):
        assert s.confirmed_at == s.index + engine.config.swing_right_bars


def test_swing_is_invisible_until_confirmed(engine: TwoSixEngine) -> None:
    highs, lows, closes, ts = random_series()
    swings = engine.find_swings(highs, lows, ts)
    target = swings[len(swings) // 2]
    assert target not in engine.visible_swings(swings, target.index)
    assert target not in engine.visible_swings(swings, target.confirmed_at - 1)
    assert target in engine.visible_swings(swings, target.confirmed_at)


def test_truncation_invariance(engine: TwoSixEngine) -> None:
    """The proof: what the engine sees at bar `t` computed from the whole
    series must equal what it sees computed from a series that *ends* at `t`.
    Any difference is the engine reading bars that had not happened yet."""
    highs, lows, closes, ts = random_series(n=400)
    full = engine.analyse(highs, lows, closes, ts)

    for t in range(40, 400, 13):
        cut = engine.analyse(highs[: t + 1], lows[: t + 1], closes[: t + 1], ts[: t + 1])
        assert engine.visible_swings(full.swings, t) == engine.visible_swings(cut.swings, t)
        assert [b for b in full.breaks if b.index <= t] == list(cut.breaks), (
            f"structure at bar {t} differs depending on whether future bars exist"
        )


def test_impulse_is_identical_on_a_truncated_series(engine: TwoSixEngine) -> None:
    """Truncation invariance applied to the *wave*, not just the pivots.

    Origin and Peak are now raw wick extremes scanned over spans of bars, which
    is exactly the kind of code that quietly reads past `t`. The wave resolved
    at bar `t` from the full series must equal the wave resolved at bar `t`
    from a series that ends there.
    """
    highs, lows, closes, ts = random_series(n=400)
    full = engine.analyse(highs, lows, closes, ts)

    for t in range(40, 400, 11):
        cut = engine.analyse(highs[: t + 1], lows[: t + 1], closes[: t + 1], ts[: t + 1])
        a = engine.latest_impulse(full, t)
        b = engine.latest_impulse(cut, t)
        if a is None or b is None:
            assert a is None and b is None, f"disagree on whether a wave exists at bar {t}"
            continue
        assert (a.origin_price, a.origin_index) == (b.origin_price, b.origin_index)
        assert (a.peak_price, a.peak_index) == (b.peak_price, b.peak_index)
        assert a.range == b.range


def test_impulse_never_uses_unconfirmed_structure(engine: TwoSixEngine) -> None:
    highs, lows, closes, ts = random_series()
    built = engine.analyse(highs, lows, closes, ts)
    for t in range(20, 400, 7):
        imp = engine.latest_impulse(built, t)
        if imp is None:
            continue
        assert imp.confirmed_at <= t
        assert imp.bos.index <= t
        assert imp.peak_index <= t
        assert imp.origin_index <= t


# ---------------------------------------------------------------------------
# Step 1 — Break of Structure
# ---------------------------------------------------------------------------


def test_the_scenario_has_the_swings_it_claims(engine: TwoSixEngine, structure) -> None:
    """Guard on the fixture itself, so a later failure means the engine broke
    rather than the hand-built data drifting."""
    highs = {s.index: s.price for s in structure.swings if s.kind is SwingKind.HIGH}
    lows = {s.index: s.price for s in structure.swings if s.kind is SwingKind.LOW}
    assert highs[3] == 4050.0
    assert lows[10] == ORIGIN_PRICE
    assert highs[19] == PEAK_PRICE


def test_wick_through_the_level_is_not_a_break(structure: MarketStructure) -> None:
    """Bar 14's high is 4060, above the 4050 swing high, but it closes at 4040.

    This is the rule the whole strategy turns on. Using the wick would fire on
    every liquidity sweep — which is the exact move a BOS is meant to ignore.
    """
    assert not any(b.index == 14 for b in structure.breaks)


def test_body_close_through_the_level_is_a_break(structure: MarketStructure) -> None:
    """Bar 15 closes at 4055, body clear of 4050."""
    bos = next(b for b in structure.breaks if b.direction is Direction.BULLISH)
    assert bos.index == 15
    assert bos.broken_level == 4050.0
    assert bos.close == 4055.0


def test_wick_mode_would_have_fired_at_the_sweep(engine: TwoSixEngine) -> None:
    """Same data with body-close confirmation switched off breaks at bar 14 —
    confirming the fixture really does discriminate between the two rules."""
    loose = TwoSixEngine(StrategyConfig(bos_requires_body_close=False))
    built = loose.analyse(HIGHS, LOWS, CLOSES, timestamps(len(HIGHS)))
    assert any(b.index == 14 for b in built.breaks)


def test_a_level_breaks_only_once(structure: MarketStructure) -> None:
    """Later closes beyond the same level are continuation, not new structure."""
    levels = [b.broken_level for b in structure.breaks if b.direction is Direction.BULLISH]
    assert len(levels) == len(set(levels))


# ---------------------------------------------------------------------------
# Step 2 — the impulse
# ---------------------------------------------------------------------------


def test_impulse_waits_for_the_peak_to_confirm(engine, structure) -> None:
    """After the BOS the wave runs until a new swing confirms. Before that
    there is no Peak, so no level can be computed and none is offered."""
    assert engine.latest_impulse(structure, 15) is None    # BOS bar itself
    assert engine.latest_impulse(structure, 21) is None    # peak at 19, confirms at 22

    imp = engine.latest_impulse(structure, 22)
    assert imp is not None
    assert imp.origin_price == ORIGIN_PRICE
    assert imp.peak_price == PEAK_PRICE
    assert imp.range == RANGE
    assert imp.direction is Direction.BULLISH


def test_origin_precedes_the_break_which_precedes_the_peak(engine, structure) -> None:
    imp = engine.latest_impulse(structure, 22)
    assert imp is not None
    assert imp.origin_index < imp.bos.index < imp.peak_index


# ---------------------------------------------------------------------------
# The wave must not shrink on a continuation break
# ---------------------------------------------------------------------------


def test_continuation_break_does_not_reanchor_the_origin(engine, trend) -> None:
    """The regression test for a real bug.

    Two bullish breaks, one wave. The engine used to anchor the Origin to the
    last pivot before the *most recent* break — here the shallow 4070 pullback
    — turning a 100-point wave into a 30-point one. Measured on 12 months of
    M15 that hit 49% of setups, and 15% ended up measuring under half the real
    move; the worst was 11.8x too small. Every level derived from a wrong range
    is wrong: entry, stop and target together.
    """
    breaks = [b for b in trend.breaks if b.direction is Direction.BULLISH]
    assert [b.index for b in breaks] == [14, 24], "fixture must produce two bullish breaks"
    assert breaks[1].wave_start_index == 14, "the second break continues the first's wave"

    imp = engine.latest_impulse(trend, 30)
    assert imp is not None
    assert imp.origin_price == 4000.0, "Origin is the wave's lowest low, not the 4070 dip"
    assert imp.peak_price == 4100.0, "Peak is the FINAL high, not the first one at 4080"
    assert imp.range == 100.0
    assert imp.bos.index == 14, "the wave belongs to the break that started the run"


def test_the_final_peak_wins_over_an_earlier_one(engine, trend) -> None:
    """Bar 17's 4080 pivot was a pause inside the move, not its end."""
    imp = engine.latest_impulse(trend, 30)
    assert imp is not None
    assert imp.peak_index == 27


def test_levels_follow_the_full_wave(engine, trend) -> None:
    setup = engine.signal_at(trend, as_of_index=30, reference_price=4090.0)
    assert setup is not None
    assert setup.entry == pytest.approx(4100.0 - 100.0 / 2.6)   # 4061.54
    assert setup.stop_loss == pytest.approx(4000.0)
    # The bug would have produced entry 4088.46 off a 30-point range.
    assert setup.entry < 4070.0


# ---------------------------------------------------------------------------
# Step 3 — the 2.6 level
# ---------------------------------------------------------------------------


def test_bullish_entry_is_peak_minus_result(engine, structure) -> None:
    """entry = Peak - Range/2.6, i.e. a 38.46% retracement.

    Not Origin + Range/2.6 — that is the 61.5% level, a different price, and
    confusing the two inverts every trade the bot takes.
    """
    setup = engine.signal_at(structure, as_of_index=22, reference_price=4070.0)
    assert setup is not None
    assert setup.entry == pytest.approx(EXPECTED_ENTRY)
    retraced = (PEAK_PRICE - setup.entry) / RANGE
    assert retraced == pytest.approx(1 / 2.6)


def test_bearish_entry_is_the_low_plus_result(engine: TwoSixEngine) -> None:
    """Mirror image: the impulse ran down, so Peak is the swing low and the
    entry sits Result *above* it."""
    down = bos(15, Direction.BEARISH, swing(5, 4020.0, SwingKind.LOW), 4015.0)
    setup = engine.build_setup(
        impulse(4100.0, 10, 4000.0, 20, down), as_of_index=23, reference_price=4030.0
    )
    assert setup is not None
    assert setup.direction is Direction.BEARISH
    assert setup.entry == pytest.approx(4000.0 + RESULT)
    assert setup.stop_loss == pytest.approx(4100.0)


def test_stop_sits_exactly_on_the_origin(engine, structure) -> None:
    """The spec puts it on the absolute wick, with no buffer."""
    setup = engine.signal_at(structure, as_of_index=22, reference_price=4070.0)
    assert setup is not None
    assert setup.stop_loss == pytest.approx(ORIGIN_PRICE)
    assert setup.risk_distance == pytest.approx(EXPECTED_RISK)


def test_describe_renders(engine, structure) -> None:
    """`describe()` is only called on the path that actually places an order,
    so nothing else in the suite exercises it.

    That gap cost a live run: after Impulse changed from holding Swing objects
    to holding prices, describe() still read `impulse.origin.price`. Every test
    passed, the daemon ran fine while standing aside, and it crashed on the
    first setup it found — five times, into the kill switch. A formatter that
    only runs when money moves has to be tested like one.
    """
    setup = engine.signal_at(structure, as_of_index=22, reference_price=4070.0)
    assert setup is not None
    text = setup.describe()
    for fragment in ("bullish", "entry=", "sl=", "tp=", "rr=1:", "range=", "origin", "peak"):
        assert fragment in text, f"{fragment!r} missing from {text!r}"
    assert f"{ORIGIN_PRICE:.3f}" in text
    assert f"{PEAK_PRICE:.3f}" in text


def test_target_is_at_least_one_to_two(engine, structure) -> None:
    setup = engine.signal_at(structure, as_of_index=22, reference_price=4070.0)
    assert setup is not None
    assert setup.risk_reward == pytest.approx(engine.config.min_risk_reward)
    assert setup.take_profit == pytest.approx(EXPECTED_ENTRY + 2 * EXPECTED_RISK)


def test_target_demands_a_large_new_extreme(engine, structure) -> None:
    """A documented consequence: tp lands 0.846 x Range beyond the Peak.

    This is why the win rate is low by construction. If this ratio ever changes
    the geometry has changed and the whole expectation with it.
    """
    setup = engine.signal_at(structure, as_of_index=22, reference_price=4070.0)
    assert setup is not None
    beyond_peak = (setup.take_profit - PEAK_PRICE) / RANGE
    assert beyond_peak == pytest.approx(0.84615, rel=1e-4)


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_rejects_when_price_already_passed_the_entry(engine, structure) -> None:
    assert engine.signal_at(structure, 22, reference_price=EXPECTED_ENTRY - 0.01) is None
    assert engine.signal_at(structure, 22, reference_price=EXPECTED_ENTRY + 0.01) is not None


def test_rejects_range_below_minimum(engine: TwoSixEngine) -> None:
    up = bos(15, Direction.BULLISH, swing(5, 4000.2, SwingKind.HIGH), 4000.3)
    assert engine.build_setup(impulse(4000.0, 10, 4000.5, 20, up), as_of_index=23,
                              reference_price=4000.4) is None


def test_respects_broker_minimum_stop_distance(engine, structure) -> None:
    assert engine.signal_at(structure, 22, reference_price=4070.0,
                            min_stop_distance=1000.0) is None


# ---------------------------------------------------------------------------
# Step 5 — lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def waiting(engine, structure):
    setup = engine.signal_at(structure, as_of_index=22, reference_price=4070.0)
    assert setup is not None
    return setup, structure


def test_fills_when_price_touches_the_level(engine, waiting) -> None:
    setup, built = waiting
    state = engine.update_setup(setup, bar_index=24, bar_high=4070.0,
                                bar_low=setup.entry - 0.001, structure=built)
    assert state is SetupState.FILLED


def test_spread_shifts_the_fill_trigger(engine, waiting) -> None:
    """A buy limit fills on the ask; bars are bid. Touching the level on the
    bid is not yet a fill."""
    setup, built = waiting
    touching = engine.update_setup(setup, bar_index=24, bar_high=4070.0,
                                   bar_low=setup.entry, structure=built, spread=0.05)
    assert touching is SetupState.WAITING

    deeper = engine.update_setup(setup, bar_index=24, bar_high=4070.0,
                                 bar_low=setup.entry - 0.05, structure=built, spread=0.05)
    assert deeper is SetupState.FILLED


def test_breaking_the_origin_invalidates(engine, waiting) -> None:
    """The spec's reset rule: price takes out the Origin without ever touching
    the 2.6 level, so the setup is dead and we wait for a fresh BOS."""
    setup, built = waiting
    state = engine.update_setup(setup, bar_index=24, bar_high=4070.0,
                                bar_low=ORIGIN_PRICE - 0.01, structure=built)
    assert state is SetupState.INVALIDATED


def test_engulfing_bar_is_scored_against_us(engine, waiting) -> None:
    """A bar covering both the entry and the Origin resolves as invalidated.
    OHLC cannot say which came first, and the simulator does not get to pick
    the reading that flatters it."""
    setup, built = waiting
    state = engine.update_setup(setup, bar_index=24, bar_high=4070.0,
                                bar_low=ORIGIN_PRICE - 1.0, structure=built)
    assert state is SetupState.INVALIDATED


def test_expires_after_ttl(engine, waiting) -> None:
    setup, built = waiting
    state = engine.update_setup(setup, bar_index=setup.expires_index,
                                bar_high=4070.0, bar_low=4065.0, structure=built)
    assert state is SetupState.EXPIRED


def test_a_new_break_supersedes(engine, waiting) -> None:
    setup, built = waiting
    newer = bos(26, Direction.BEARISH, swing(10, 4000.0, SwingKind.LOW), 3999.0)
    with_new = MarketStructure(built.swings, built.breaks + (newer,),
                               built.highs, built.lows)

    before = engine.update_setup(setup, bar_index=25, bar_high=4070.0,
                                 bar_low=4065.0, structure=with_new)
    assert before is SetupState.WAITING

    after = engine.update_setup(setup, bar_index=26, bar_high=4070.0,
                                bar_low=4065.0, structure=with_new)
    assert after is SetupState.SUPERSEDED
