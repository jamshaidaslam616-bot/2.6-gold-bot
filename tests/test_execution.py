"""Order-construction tests.

These need no terminal: the point is that `OrderRequest` cannot be built into
an unsafe shape in the first place. "Forbidden by the type" beats "checked
before sending", because the check can be forgotten at a call site and the
constructor cannot.
"""
from __future__ import annotations

import pytest

from engine import Direction
from execution import NakedOrderRefused, OrderRequest

BUY = dict(symbol="XAUUSD", direction=Direction.BULLISH, lots=0.05,
           entry=4000.0, stop_loss=3990.0, take_profit=4020.0, magic=2600015)
SELL = dict(symbol="XAUUSD", direction=Direction.BEARISH, lots=0.05,
            entry=4000.0, stop_loss=4010.0, take_profit=3980.0, magic=2600015)


def test_valid_orders_construct() -> None:
    assert OrderRequest(**BUY).is_pending
    assert OrderRequest(**SELL).is_pending


def test_market_order_has_no_entry() -> None:
    assert not OrderRequest(**{**BUY, "entry": None}).is_pending


@pytest.mark.parametrize("missing", ["stop_loss", "take_profit"])
def test_naked_order_is_refused(missing: str) -> None:
    """The rule the brief called 'completely forbidden'. Enforced here rather
    than at the send site, so no call path can skip it."""
    with pytest.raises(NakedOrderRefused):
        OrderRequest(**{**BUY, missing: 0.0})
    with pytest.raises(NakedOrderRefused):
        OrderRequest(**{**BUY, missing: None})


def test_buy_with_stop_above_entry_is_refused() -> None:
    """An inverted bracket is not a stop, it is an instant exit."""
    with pytest.raises(NakedOrderRefused, match="below the entry"):
        OrderRequest(**{**BUY, "stop_loss": 4010.0})


def test_buy_with_target_below_entry_is_refused() -> None:
    with pytest.raises(NakedOrderRefused, match="above the entry"):
        OrderRequest(**{**BUY, "take_profit": 3990.0})


def test_sell_with_stop_below_entry_is_refused() -> None:
    with pytest.raises(NakedOrderRefused, match="above the entry"):
        OrderRequest(**{**SELL, "stop_loss": 3990.0})


def test_sell_with_target_above_entry_is_refused() -> None:
    with pytest.raises(NakedOrderRefused, match="below the entry"):
        OrderRequest(**{**SELL, "take_profit": 4010.0})


def test_non_positive_volume_is_refused() -> None:
    with pytest.raises(ValueError, match="lots must be positive"):
        OrderRequest(**{**BUY, "lots": 0.0})


def test_comment_is_truncated_not_rejected() -> None:
    """MT5 silently truncates past 31 chars; we do it explicitly so the value
    in the journal matches the value at the broker."""
    from config import Timeframe

    request = OrderRequest(**{**BUY, "comment": "x" * 80})
    assert len(request.comment) == 80          # stored intact
    # Magic encodes the timeframe's minutes, so a magic on the account says
    # which timeframe opened the position. They must never collide.
    assert Timeframe.M15.magic == 2600015
    assert len({tf.magic for tf in Timeframe}) == len(list(Timeframe))
