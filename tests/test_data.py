"""Server-time measurement tests.

These exist because of one live failure. Over a weekend the last quote is a day
and a half old, and measuring the server's timezone against it produced an
"offset" of minus thirty-three hours. Every bar timestamp then shifted forward
by that much — Friday's closing bars were reported as Sunday's — and the
clock-drift guard blocked trading while blaming a skew that did not exist.

A stale quote and a timezone offset look identical if you only subtract two
numbers. Telling them apart is the whole job here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from data import MAX_TICK_AGE_SECONDS, Mt5Session


class FakeTick:
    def __init__(self, moment: datetime) -> None:
        self.time = int(moment.timestamp())
        self.bid = 4000.0
        self.ask = 4000.05


class FakeMt5:
    """Just enough of the module for the offset logic."""

    def __init__(self, tick_at: datetime) -> None:
        self._tick = FakeTick(tick_at)

    def symbol_info_tick(self, _symbol: str) -> FakeTick:
        return self._tick


def session_with_tick_at(moment: datetime) -> Mt5Session:
    session = Mt5Session(mt5=FakeMt5(moment))
    session._connected = True          # noqa: SLF001 - test seam
    session._time_symbol = "XAUUSD"    # noqa: SLF001
    return session


def test_fresh_tick_at_utc_measures_no_offset() -> None:
    session = session_with_tick_at(datetime.now(timezone.utc))
    assert session.refresh_server_offset() is True
    assert session.server_offset == timedelta(0)


def test_fresh_tick_measures_a_real_timezone() -> None:
    """A broker three hours ahead, quoting now."""
    session = session_with_tick_at(datetime.now(timezone.utc) + timedelta(hours=3))
    assert session.refresh_server_offset() is True
    assert session.server_offset == timedelta(hours=3)


def test_weekend_stale_tick_is_refused() -> None:
    """The live failure: a 33-hour-old quote is not a 33-hour timezone."""
    session = session_with_tick_at(datetime.now(timezone.utc) - timedelta(hours=33))
    assert session.refresh_server_offset() is False
    assert session.server_offset == timedelta(0), "must not adopt a bogus offset"
    assert session.offset_is_measured is False


@pytest.mark.parametrize("hours", [-40, -33, -20, 20, 33, 40])
def test_no_broker_sits_more_than_fourteen_hours_from_utc(hours: int) -> None:
    session = session_with_tick_at(datetime.now(timezone.utc) + timedelta(hours=hours))
    assert session.refresh_server_offset() is False


def test_a_good_offset_is_not_lost_when_the_market_shuts() -> None:
    """Measured on Friday, still correct on Sunday. Re-measuring must not
    overwrite it with the weekend's nonsense."""
    session = session_with_tick_at(datetime.now(timezone.utc) + timedelta(hours=2))
    assert session.refresh_server_offset() is True
    assert session.server_offset == timedelta(hours=2)

    session._mt5 = FakeMt5(datetime.now(timezone.utc) - timedelta(hours=33))  # noqa: SLF001
    assert session.refresh_server_offset() is True, "keeps the value it already had"
    assert session.server_offset == timedelta(hours=2)


def test_drift_reads_zero_while_the_market_is_shut() -> None:
    """Staleness is not drift. Reporting it as drift made the guard fire every
    weekend for the wrong reason."""
    session = session_with_tick_at(datetime.now(timezone.utc) - timedelta(hours=33))
    session.refresh_server_offset()
    assert session.clock_drift_seconds() == 0.0


def test_tick_age_reports_the_staleness_instead() -> None:
    session = session_with_tick_at(datetime.now(timezone.utc) - timedelta(hours=33))
    session.refresh_server_offset()
    age = session.tick_age_seconds()
    assert age > MAX_TICK_AGE_SECONDS
    assert 33 * 3600 - 60 < age < 33 * 3600 + 60


def test_a_genuinely_skewed_clock_still_shows_as_drift() -> None:
    """The guard must keep working for what it was built for."""
    session = session_with_tick_at(datetime.now(timezone.utc))
    session.refresh_server_offset()
    session._mt5 = FakeMt5(datetime.now(timezone.utc) + timedelta(seconds=90))  # noqa: SLF001
    drift = session.clock_drift_seconds()
    assert 60 < drift < 120


# ---------------------------------------------------------------------------
# Account identity
# ---------------------------------------------------------------------------


class FakeAccount:
    def __init__(self, login: int, equity: float) -> None:
        self.login = login
        self.equity = equity
        self.balance = equity
        self.server = "Exness-MT5Trial16"
        self.company = "Exness"
        self.currency = "USD"
        self.leverage = 2000
        self.margin_free = equity
        self.trade_mode = 0


class FakeMt5WithAccount(FakeMt5):
    def __init__(self, tick_at: datetime, login: int, equity: float) -> None:
        super().__init__(tick_at)
        self._account = FakeAccount(login, equity)

    def account_info(self) -> FakeAccount:
        return self._account


def test_reading_another_bots_account_is_refused() -> None:
    """The 2026-08-17 failure.

    This bot's terminal exited and the API attached to a different one that was
    running another bot's account. Equity read 63,504 instead of 9,962 for two
    hours, which became a new recorded peak and would have sized any position
    against someone else's balance.
    """
    from data import WrongAccountError

    session = Mt5Session(login=472501540, password="x", server="s",
                         mt5=FakeMt5WithAccount(datetime.now(timezone.utc),
                                                login=472286354, equity=63504.27))
    session._connected = True          # noqa: SLF001
    with pytest.raises(WrongAccountError, match="472286354"):
        session.account()


def test_the_right_account_reads_normally() -> None:
    session = Mt5Session(login=472501540, password="x", server="s",
                         mt5=FakeMt5WithAccount(datetime.now(timezone.utc),
                                                login=472501540, equity=9962.89))
    session._connected = True          # noqa: SLF001
    assert session.account().equity == pytest.approx(9962.89)


def test_no_expected_login_means_no_check() -> None:
    """Read-only tools attach without credentials and take what they find."""
    session = Mt5Session(mt5=FakeMt5WithAccount(datetime.now(timezone.utc),
                                                login=1, equity=5.0))
    session._connected = True          # noqa: SLF001
    assert session.account().login == 1
