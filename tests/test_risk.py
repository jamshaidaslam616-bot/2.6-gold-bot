"""Risk tests.

Sizing is checked against hand-computed numbers rather than against the
implementation restated in the test — a test that recomputes `risk/cost` proves
only that the same expression was typed twice.
"""
from __future__ import annotations

import json

import pytest

from config import CostConfig, RiskConfig
from data import SymbolSpec
from risk import RiskManager, RiskState, size_position

# XAUUSD as this broker actually reports it (read from the live terminal).
GOLD = SymbolSpec(
    name="XAUUSD", digits=3, point=0.001, tick_size=0.001, tick_value=0.1,
    contract_size=100.0, volume_min=0.01, volume_max=200.0, volume_step=0.01,
    stops_level_points=0, freeze_level_points=0, spread_points=50.0,
)


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig()


@pytest.fixture
def costs() -> CostConfig:
    return CostConfig()


@pytest.fixture
def manager(tmp_path, risk_cfg, costs) -> RiskManager:
    return RiskManager(risk_cfg, costs, tmp_path / "state.json", tmp_path / "KILL_SWITCH")


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def test_sizing_matches_hand_computed_lots(risk_cfg, costs) -> None:
    """equity 10,000, 0.5% = $50 budget. Stop $10 -> $1000/lot of price risk,
    plus $11.00/lot commission round turn = $1011/lot. 50/1011 = 0.04946 lots,
    which rounds DOWN to 0.04."""
    result = size_position(
        equity=10_000.0, spec=GOLD, stop_distance=10.0, risk_cfg=risk_cfg, costs=costs
    )
    assert result.ok
    assert result.lots == pytest.approx(0.04)
    assert result.risk_usd == pytest.approx(0.04 * 1011.0)
    assert result.risk_pct < risk_cfg.risk_per_trade_pct   # under, never over


def test_sizing_never_exceeds_the_limit(risk_cfg, costs) -> None:
    """The property that matters, swept across plausible stops."""
    for stop in [0.5, 1, 2, 3.7, 5, 8, 12.5, 20, 50, 100]:
        result = size_position(
            equity=10_000.0, spec=GOLD, stop_distance=stop, risk_cfg=risk_cfg, costs=costs
        )
        if result.ok:
            assert result.risk_pct <= risk_cfg.risk_per_trade_pct + 1e-9, (
                f"stop {stop} sized to {result.risk_pct:.4f}%, over the limit"
            )


def test_rejects_rather_than_rounding_up_to_broker_minimum(risk_cfg, costs) -> None:
    """A tiny account with a wide stop cannot trade 0.01 lots inside 0.5%.

    Rounding up here is the quiet way a 0.5% limit becomes something else, and
    it happens precisely when the stop is widest.
    """
    result = size_position(
        equity=200.0, spec=GOLD, stop_distance=50.0, risk_cfg=risk_cfg, costs=costs
    )
    assert not result.ok
    assert "minimum" in result.rejected
    assert result.lots == 0


def test_commission_comes_out_of_the_risk_budget(risk_cfg) -> None:
    """Same trade, commission on vs off. With commission the size must be
    smaller — if it is not, commission is being charged on top of the limit
    rather than inside it."""
    free = CostConfig(commission_per_lot_per_side=0.0)
    charged = CostConfig(commission_per_lot_per_side=5.50)
    a = size_position(equity=100_000.0, spec=GOLD, stop_distance=5.0,
                      risk_cfg=risk_cfg, costs=free)
    b = size_position(equity=100_000.0, spec=GOLD, stop_distance=5.0,
                      risk_cfg=risk_cfg, costs=charged)
    assert a.lots > b.lots


def test_rejects_non_positive_inputs(risk_cfg, costs) -> None:
    assert not size_position(equity=0, spec=GOLD, stop_distance=5,
                             risk_cfg=risk_cfg, costs=costs).ok
    assert not size_position(equity=10_000, spec=GOLD, stop_distance=0,
                             risk_cfg=risk_cfg, costs=costs).ok


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


HEALTHY = dict(
    is_demo=True, open_positions=0, spread_points=50.0,
    clock_drift_seconds=1.0, equity=10_000.0, margin_free=9_900.0,
)


def test_healthy_account_is_allowed(manager: RiskManager) -> None:
    assert manager.can_open(**HEALTHY).allowed


def test_real_account_is_refused(manager: RiskManager) -> None:
    decision = manager.can_open(**{**HEALTHY, "is_demo": False})
    assert not decision.allowed and "demo" in decision.reason


def test_one_position_at_a_time(manager: RiskManager) -> None:
    """The owner chose a single global position across all three timeframes."""
    decision = manager.can_open(**{**HEALTHY, "open_positions": 1})
    assert not decision.allowed and "already open" in decision.reason


def test_wide_spread_is_refused_against_the_instrument_limit(manager: RiskManager) -> None:
    """The caller measures what is normal for this instrument and passes it in."""
    decision = manager.can_open(**{**HEALTHY, "spread_points": 200.0,
                                   "spread_limit_points": 60.0})
    assert not decision.allowed and "spread" in decision.reason

    ok = manager.can_open(**{**HEALTHY, "spread_points": 200.0,
                             "spread_limit_points": 280.0})
    assert ok.allowed, "a mini account's normal 200-point spread must be tradeable"


def test_absolute_backstop_still_applies(manager: RiskManager, risk_cfg) -> None:
    """A nonsense quote is refused even if the measured limit would allow it."""
    decision = manager.can_open(**{**HEALTHY, "spread_points": 5000.0,
                                   "spread_limit_points": 9999.0})
    assert not decision.allowed and "spread" in decision.reason


def test_clock_drift_is_refused(manager: RiskManager) -> None:
    decision = manager.can_open(**{**HEALTHY, "clock_drift_seconds": -120.0})
    assert not decision.allowed and "drift" in decision.reason


def test_thin_margin_is_refused(manager: RiskManager) -> None:
    decision = manager.can_open(**{**HEALTHY, "margin_free": 100.0})
    assert not decision.allowed and "margin" in decision.reason


# ---------------------------------------------------------------------------
# Halts
# ---------------------------------------------------------------------------


def test_daily_loss_limit_halts_trading(manager: RiskManager) -> None:
    manager.observe_equity(10_000.0)          # sets day_start_equity and peak
    manager.record_closed_trade(-150.0)
    assert manager.can_open(**HEALTHY).allowed, "1.5% down is inside the 3% limit"

    manager.record_closed_trade(-160.0)       # cumulative -310 > 3% of 10,000
    decision = manager.can_open(**HEALTHY)
    assert not decision.allowed and "daily loss" in decision.reason


def test_drawdown_engages_the_kill_switch(manager: RiskManager, tmp_path) -> None:
    manager.observe_equity(10_000.0)
    manager.observe_equity(8_900.0)           # 11% off the peak, past the 10% ceiling
    assert manager.kill_switch_engaged()
    assert (tmp_path / "KILL_SWITCH").exists()
    assert not manager.can_open(**HEALTHY).allowed


def test_drawdown_counts_open_losses_not_only_closed_ones(manager: RiskManager) -> None:
    """Equity, not balance. A position running 20% against us must register
    before it closes, otherwise the ceiling only fires after the damage."""
    manager.observe_equity(10_000.0)
    manager.observe_equity(8_500.0)           # unrealised
    assert manager.kill_switch_engaged()


def test_kill_switch_does_not_clear_itself(manager: RiskManager) -> None:
    manager.observe_equity(10_000.0)
    manager.observe_equity(8_000.0)
    assert manager.kill_switch_engaged()
    manager.observe_equity(12_000.0)          # full recovery and then some
    assert manager.kill_switch_engaged(), "a limit that resets itself is not a limit"


def test_max_trades_per_day(manager: RiskManager, risk_cfg) -> None:
    manager.observe_equity(10_000.0)
    for _ in range(risk_cfg.max_trades_per_day):
        manager.record_closed_trade(0.01)
    decision = manager.can_open(**HEALTHY)
    assert not decision.allowed and "trades today" in decision.reason


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_state_survives_a_restart(tmp_path, risk_cfg, costs) -> None:
    """A crash-loop must not launder the day's losses."""
    state_path, kill_path = tmp_path / "state.json", tmp_path / "KILL_SWITCH"
    first = RiskManager(risk_cfg, costs, state_path, kill_path)
    first.observe_equity(10_000.0)
    first.record_closed_trade(-310.0)
    assert not first.can_open(**HEALTHY).allowed

    second = RiskManager(risk_cfg, costs, state_path, kill_path)   # "restart"
    decision = second.can_open(**HEALTHY)
    assert not decision.allowed and "daily loss" in decision.reason


def test_corrupt_state_file_refuses_to_start(tmp_path, risk_cfg, costs) -> None:
    """Silently starting fresh would erase a halt."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        RiskManager(risk_cfg, costs, state_path, tmp_path / "KILL_SWITCH")


def test_new_day_clears_daily_halt_but_not_drawdown(manager: RiskManager) -> None:
    manager.observe_equity(10_000.0)
    manager.record_closed_trade(-400.0)
    assert manager.state.halted_reason.startswith("daily loss")

    manager.state.roll_day("2099-01-01", 9_600.0)
    assert manager.state.halted_reason == ""
    assert manager.state.trades_today == 0
    assert manager.state.peak_equity == 10_000.0, "peak equity must not reset"


def test_day_roll_archives_the_previous_day() -> None:
    state = RiskState(day="2026-08-06", realised_today=-12.5, trades_today=3)
    state.roll_day("2026-08-07", 10_000.0)
    assert state.history[-1] == {"day": "2026-08-06", "realised": -12.5, "trades": 3}
    assert state.realised_today == 0.0


def test_state_file_is_valid_json(manager: RiskManager, tmp_path) -> None:
    manager.observe_equity(10_000.0)
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["peak_equity"] == 10_000.0
