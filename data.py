"""MT5 connection and the historical bar store.

This module exists because three MT5 behaviours will silently corrupt a
backtest if you do not know about them. All three were verified against the
live Exness terminal on 2026-08-07, not taken from documentation:

1. **Large requests fail silently.** `copy_rates_from_pos` served 65,000 bars
   and returned `None (-2, 'Terminal: Invalid params')` for 100,000. A 12-month
   M5 range request returns `None` outright. Chunked into 30-day pieces the
   same range yields 70,846 bars. A `None` that is not backed by an error code
   therefore means "no data in this window", not "failure" — and the difference
   matters, because treating one as the other loses a year of history quietly.

2. **Bar 0 is the bar still forming.** Every read starts at position 1 and
   every bar is additionally checked to have closed. Including a forming bar in
   a backtest is look-ahead bias in its purest form: you are using the future
   close of the candle you are deciding on.

3. **Timestamps are server time, not UTC**, handed over as naive epoch seconds.
   The offset is measured against our own clock at connect time and recorded.
   Assuming it is zero shifts every session filter by hours.

The symbol name is resolved at runtime and never hardcoded. On account
472250693 the symbol is `XAUUSD`; `XAUUSDm` does not exist there. On account
472286354 it is the other way round — which is exactly how mt5_beast_bot ended
up logging "symbol XAUUSDm unavailable" for an hour.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import pandas as pd

import logger as logging_setup

log = logging_setup.get("data")

#: MT5 reports success as (1, "Success"). A None result carrying this code is
#: an empty window; a None result carrying anything else is a real failure.
MT5_SUCCESS_CODE = 1

#: A quote older than this is a shut market, not latency. Used both to reject a
#: bogus server-time measurement and to refuse to trade on stale prices.
MAX_TICK_AGE_SECONDS = 300.0

#: Symbols whose trade mode is 0 cannot be traded. Exness carries lookalikes
#: (XAUUSD247) with trading disabled; selecting one fails only at order time.
TRADE_MODE_DISABLED = 0

BAR_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "spread"]


class BrokerError(RuntimeError):
    """Connection is down or in an unknown state. Callers halt rather than
    retry blindly — 'retry until it works' is how duplicate orders happen."""


class SymbolResolutionError(RuntimeError):
    """The gold symbol could not be identified unambiguously. Raised rather
    than guessed: trading the wrong instrument is worse than not starting."""


class WrongAccountError(RuntimeError):
    """The terminal is answering for an account we did not ask for.

    This is not hypothetical. On 2026-08-17 this bot's own terminal exited and
    the MetaTrader5 API silently attached to a different terminal that happened
    to be running — one logged into another bot's account. The daemon read that
    account's equity, 63,504 against our 9,962, for two hours: it recorded a new
    peak equity six times too high, and any position it had opened would have
    been sized against someone else's balance, on someone else's account.

    `path=` is only a hint about which terminal to *launch*. It is not a
    guarantee about which one you end up talking to, so identity has to be
    checked on every read rather than assumed from the connection.
    """


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """The contract, as the broker describes it. Every field is required.

    Nothing here is defaulted. A missing `trade_contract_size` is not something
    to guess at — guessing it is how a position ends up sized 100x wrong.
    """

    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int
    spread_points: float

    @property
    def money_per_price_unit_per_lot(self) -> float:
        """What a 1.00 price move is worth on one lot, in account currency.

        Derived from tick_value/tick_size rather than contract_size so it stays
        correct for instruments where those disagree. For XAUUSD here:
        0.1 / 0.001 = 100.0, matching the 100 oz contract.
        """
        if self.tick_size <= 0:
            raise SymbolResolutionError(f"{self.name} reports tick_size <= 0")
        return self.tick_value / self.tick_size


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    login: int
    server: str
    company: str
    currency: str
    leverage: int
    balance: float
    equity: float
    margin_free: float
    is_demo: bool

    @property
    def kind(self) -> str:
        return "demo" if self.is_demo else "REAL"


class Mt5Session:
    """Owns the connection. Reads only — order sending lives in execution.py."""

    def __init__(
        self,
        *,
        terminal_path: str = "",
        login: int = 0,
        password: str = "",
        server: str = "",
        mt5: Any | None = None,
    ) -> None:
        """
        Args:
            terminal_path: which terminal64.exe to drive. Set this when more
                than one MT5 is installed, otherwise the API picks one for you.
            login/password/server: when all three are supplied the terminal is
                logged in to that account. **This switches the terminal**, so
                leaving them blank (attach to whatever is already there) is the
                polite default when other bots share the machine.
            mt5: the MetaTrader5 module, injected so the whole class is
                testable on a machine with no terminal.
        """
        self._mt5 = mt5
        self._terminal_path = terminal_path
        self._login = login
        self._password = password
        self._server = server
        self._connected = False
        self._server_offset = timedelta(0)
        self._offset_measured = False
        self._time_symbol: str | None = None

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            import MetaTrader5  # noqa: N813 - the package really is named this

            self._mt5 = MetaTrader5
        return self._mt5

    @property
    def switches_account(self) -> bool:
        return bool(self._login and self._password and self._server)

    def _fail(self, what: str) -> BrokerError:
        """Always attach the terminal's own error text. Debugging a broker
        problem without the broker's error code is guesswork."""
        try:
            code, text = self.mt5.last_error()
        except Exception:  # noqa: BLE001 - never mask the original problem
            code, text = ("?", "?")
        return BrokerError(f"{what} (MT5 error {code}: {text})")

    def _last_error_code(self) -> int:
        try:
            return int(self.mt5.last_error()[0])
        except Exception:  # noqa: BLE001 - defensive
            return -1

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> AccountSnapshot:
        """Attach to the terminal. Idempotent."""
        if self._connected:
            return self.account()

        kwargs: dict[str, Any] = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if self.switches_account:
            kwargs.update(login=self._login, password=self._password, server=self._server)
            log.info("connecting and logging in to %s on %s", self._login, self._server)
        else:
            log.info("attaching to the terminal as-is (no account switch)")

        if not self.mt5.initialize(**kwargs):
            raise self._fail("MT5 initialize() failed — is the terminal running?")

        self._connected = True
        self._offset_measured = False   # a new terminal needs a fresh measurement
        try:
            snapshot = self.account()   # also asserts we got the right account
        except Exception:
            self.disconnect()  # never leave a half-open connection behind
            raise
        log.info(
            "connected: %s account %s @ %s, equity %.2f %s, leverage 1:%d",
            snapshot.kind, snapshot.login, snapshot.server,
            snapshot.equity, snapshot.currency, snapshot.leverage,
        )
        return snapshot

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self.mt5.shutdown()
        finally:
            self._connected = False

    def reconnect(self) -> AccountSnapshot:
        """Full teardown and re-handshake.

        Deliberately not a retry loop — the caller decides whether to try
        again, because "retry until it works" is how the same order gets sent
        twice. What this does fix is the case that killed the daemon on
        2026-08-17: the terminal exited, every read failed with "IPC send
        failed", and nothing ever tried to bring it back. Reconnecting relaunches
        the terminal at `path` and re-checks which account answered.
        """
        log.warning("reconnecting to the terminal")
        self.disconnect()
        return self.connect()

    def is_connected(self) -> bool:
        """Ask the terminal, do not trust our own flag. A terminal that was
        killed still leaves our boolean set."""
        if not self._connected:
            return False
        try:
            terminal = self.mt5.terminal_info()
        except Exception:  # noqa: BLE001 - a raising bridge is a dead bridge
            return False
        return bool(terminal is not None and getattr(terminal, "connected", False))

    def _require(self) -> None:
        if not self._connected:
            raise BrokerError("not connected — call connect() first")

    def __enter__(self) -> Mt5Session:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    # -- account ------------------------------------------------------------

    def account(self) -> AccountSnapshot:
        """Read the account, and refuse to hand back one we did not ask for.

        The identity check is on every read, not just at connect. A terminal
        can exit and be replaced by another under a live connection, and the
        API will answer from whichever one it can reach — see WrongAccountError.
        """
        self._require()
        info = self.mt5.account_info()
        if info is None:
            raise self._fail("account_info() returned None")
        if self._login and int(info.login) != self._login:
            raise WrongAccountError(
                f"terminal is answering for account {int(info.login)} "
                f"(equity {float(info.equity):,.2f}) but this bot trades "
                f"{self._login}. Refusing to read or act on it."
            )
        # trade_mode: 0 demo, 1 contest, 2 real. Anything not clearly demo is
        # reported as real — the safe error is over-caution.
        is_demo = int(getattr(info, "trade_mode", 2)) in (0, 1)
        return AccountSnapshot(
            login=int(info.login),
            server=str(info.server),
            company=str(getattr(info, "company", "")),
            currency=str(info.currency),
            leverage=int(getattr(info, "leverage", 0)),
            balance=float(info.balance),
            equity=float(info.equity),
            margin_free=float(getattr(info, "margin_free", 0.0)),
            is_demo=is_demo,
        )

    # -- time ---------------------------------------------------------------

    def algo_trading_allowed(self) -> bool:
        """Whether the terminal will accept an order at all.

        This is the "Algo Trading" toggle in the MT5 window. It defaults to off
        on a terminal that has just been started, and nothing in the API turns
        it on — it is a decision the terminal's operator makes.

        Worth checking *before* building an order rather than discovering it in
        the rejection: on 2026-08-17 both trading terminals came up with it off
        after a restart and every order was refused with retcode 10027. This bot
        retried every fifteen seconds for nineteen hours — 1,485 rejections —
        while its heartbeat cheerfully reported "scanning".
        """
        self._require()
        info = self.mt5.terminal_info()
        return bool(info is not None and getattr(info, "trade_allowed", False))

    def tick_age_seconds(self) -> float:
        """How old the last quote is. Large means the market is shut."""
        if self._time_symbol is None:
            return float("inf")
        tick = self.mt5.symbol_info_tick(self._time_symbol)
        if tick is None or not getattr(tick, "time", 0):
            return float("inf")
        seen = datetime.fromtimestamp(int(tick.time), tz=timezone.utc) - self._server_offset
        return (datetime.now(timezone.utc) - seen).total_seconds()

    def _measure_server_offset(self) -> timedelta | None:
        """How far the server's clock labelling sits from UTC, or None.

        Rounded to the nearest hour: brokers sit on whole-hour offsets, and we
        do not want ordinary tick latency showing up as a fractional timezone.

        **Only a fresh tick may be used.** Over a weekend the last quote is a
        day and a half old, and measuring against it yields an "offset" of -33
        hours — which is not a timezone, it is a stale tick. Observed on
        2026-08-16: every bar timestamp was shifted 33 hours forward, so
        Friday's closing bars were reported as Sunday's, and the clock-drift
        guard blocked trading while blaming the wrong thing.

        Returning None means "cannot tell right now" — the caller keeps
        whatever it already had rather than adopting a wrong answer.
        """
        symbol = self._time_symbol
        if symbol is None:
            return None
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None or not getattr(tick, "time", 0):
            return None
        server = datetime.fromtimestamp(int(tick.time), tz=timezone.utc)
        gap = (server - datetime.now(timezone.utc)).total_seconds()
        hours = round(gap / 3600.0)
        # A real timezone offset is within +/- 14 hours, and what is left over
        # after removing it is tick latency measured in seconds. Anything else
        # is a stale quote wearing a timezone's clothes.
        if abs(hours) > 14 or abs(gap - hours * 3600) > MAX_TICK_AGE_SECONDS:
            log.warning(
                "ignoring server-time measurement: last tick is %.1f hours from "
                "our clock, which is a shut market rather than a timezone",
                gap / 3600,
            )
            return None
        return timedelta(hours=hours)

    def refresh_server_offset(self) -> bool:
        """Adopt a server-time offset only when one can honestly be measured.

        Called at symbol resolution and again on each loop, so an offset that
        could not be taken over the weekend gets picked up as soon as the
        market reopens. Returns whether we now have a measured value.
        """
        measured = self._measure_server_offset()
        if measured is not None:
            if measured != self._server_offset or not self._offset_measured:
                log.info("server clock offset measured: %+.0fh",
                         measured.total_seconds() / 3600)
            self._server_offset = measured
            self._offset_measured = True
        return self._offset_measured

    @property
    def offset_is_measured(self) -> bool:
        return self._offset_measured

    @property
    def server_offset(self) -> timedelta:
        return self._server_offset

    def clock_drift_seconds(self) -> float:
        """Signed difference between the server's clock and ours once the
        timezone offset is removed.

        Reported as zero while the market is shut. A stale quote is not clock
        drift, and conflating them made the guard fire every weekend blaming a
        skew that did not exist. Staleness is a separate condition with its own
        check — see `tick_age_seconds`.
        """
        self._require()
        if self._time_symbol is None or not self._offset_measured:
            return 0.0
        tick = self.mt5.symbol_info_tick(self._time_symbol)
        if tick is None or not getattr(tick, "time", 0):
            raise self._fail(f"no tick for {self._time_symbol}")
        server = datetime.fromtimestamp(int(tick.time), tz=timezone.utc) - self._server_offset
        drift = (server - datetime.now(timezone.utc)).total_seconds()
        return drift if abs(drift) <= MAX_TICK_AGE_SECONDS else 0.0

    def _to_utc(self, epoch_seconds: int) -> datetime:
        return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc) - self._server_offset

    def _to_server_naive(self, moment: datetime) -> datetime:
        """MT5 wants naive server-time datetimes for range reads."""
        return (moment.astimezone(timezone.utc) + self._server_offset).replace(tzinfo=None)

    # -- symbol -------------------------------------------------------------

    def resolve_symbol(self, patterns: Sequence[str]) -> SymbolSpec:
        """Find the gold symbol on *this* account and read its contract spec.

        Matching is conservative on purpose:
          1. an exact case-insensitive name match on a pattern wins outright;
          2. otherwise a candidate must start with a pattern and be tradeable;
          3. if more than one survives, raise and list them.

        Rule 3 is the point. An account carrying both XAUUSD and XAUUSD247
        makes a silent wrong pick entirely plausible.
        """
        self._require()
        symbols = self.mt5.symbols_get()
        if not symbols:
            raise self._fail("symbols_get() returned nothing")

        by_name = {str(s.name): s for s in symbols}
        lowered = {name.lower(): name for name in by_name}

        chosen: str | None = None
        for pattern in patterns:
            if pattern.lower() in lowered:
                chosen = lowered[pattern.lower()]
                break

        if chosen is None:
            candidates = sorted(
                name for name in by_name
                if any(name.lower().startswith(p.lower()) for p in patterns)
                and int(getattr(by_name[name], "trade_mode", TRADE_MODE_DISABLED)) != TRADE_MODE_DISABLED
            )
            if not candidates:
                raise SymbolResolutionError(
                    f"No tradeable symbol matched {list(patterns)} on this account. "
                    f"The terminal exposes {len(by_name)} symbols."
                )
            if len(candidates) > 1:
                raise SymbolResolutionError(
                    f"Ambiguous gold symbol: {list(patterns)} matched {candidates}. "
                    "Refusing to guess — narrow symbol_search_patterns to the exact name."
                )
            chosen = candidates[0]

        # Unselected symbols return no ticks and no history, which reads as
        # "the broker has no data" if you skip this.
        if not self.mt5.symbol_select(chosen, True):
            raise self._fail(f"symbol_select({chosen}) failed")

        self._time_symbol = chosen
        self.refresh_server_offset()
        log.info("resolved symbol %s (server clock %+.0fh vs UTC%s)",
                 chosen, self._server_offset.total_seconds() / 3600,
                 "" if self._offset_measured else ", NOT YET MEASURED — market shut")
        return self.symbol_spec(chosen)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._require()
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise self._fail(f"symbol_info({symbol}) returned None")

        required = {
            "digits": "digits",
            "point": "point",
            "tick_size": "trade_tick_size",
            "tick_value": "trade_tick_value",
            "contract_size": "trade_contract_size",
            "volume_min": "volume_min",
            "volume_max": "volume_max",
            "volume_step": "volume_step",
            "stops_level_points": "trade_stops_level",
            "freeze_level_points": "trade_freeze_level",
        }
        values: dict[str, Any] = {}
        missing: list[str] = []
        for field_name, attr in required.items():
            value = getattr(info, attr, None)
            if value is None:
                missing.append(attr)
            else:
                values[field_name] = value
        if missing:
            raise SymbolResolutionError(
                f"{symbol} is missing required properties: {missing}. "
                "Refusing to trade a contract we cannot fully describe."
            )

        return SymbolSpec(
            name=str(info.name),
            digits=int(values["digits"]),
            point=float(values["point"]),
            tick_size=float(values["tick_size"]),
            tick_value=float(values["tick_value"]),
            contract_size=float(values["contract_size"]),
            volume_min=float(values["volume_min"]),
            volume_max=float(values["volume_max"]),
            volume_step=float(values["volume_step"]),
            stops_level_points=int(values["stops_level_points"]),
            freeze_level_points=int(values["freeze_level_points"]),
            spread_points=float(getattr(info, "spread", 0.0)),
        )

    def live_spread_points(self, symbol: str) -> float:
        """Spread right now, from the tick.

        Note the tick object has **no** `.spread` attribute — verified: its
        fields are ask, bid, last, time, time_msc, volume, volume_real, flags.
        Reading `symbol_info_tick(...).spread` raises AttributeError. Ask minus
        bid is what an order would actually pay, and `symbol_info().spread` is
        the cached fallback.
        """
        self._require()
        tick = self.mt5.symbol_info_tick(symbol)
        info = self.mt5.symbol_info(symbol)
        if tick is None or info is None:
            raise self._fail(f"no tick/info for {symbol}")
        point = float(getattr(info, "point", 0.0))
        if point <= 0:
            raise SymbolResolutionError(f"{symbol} reports a non-positive point size")
        return (float(tick.ask) - float(tick.bid)) / point

    def tick(self, symbol: str) -> tuple[float, float]:
        """(bid, ask) right now."""
        self._require()
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            raise self._fail(f"symbol_info_tick({symbol}) returned None")
        return float(t.bid), float(t.ask)

    # -- bars ---------------------------------------------------------------

    def _timeframe_constant(self, timeframe: Any) -> int:
        attr = timeframe.mt5_constant_name
        try:
            return getattr(self.mt5, attr)
        except AttributeError as exc:  # pragma: no cover - defensive
            raise BrokerError(f"MT5 module has no {attr}") from exc

    def recent_bars(self, symbol: str, timeframe: Any, count: int) -> pd.DataFrame:
        """The last `count` CLOSED bars, oldest first.

        `start_pos=1` skips index 0, the bar still forming.
        """
        self._require()
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        rows = self.mt5.copy_rates_from_pos(
            symbol, self._timeframe_constant(timeframe), 1, count
        )
        if rows is None:
            if self._last_error_code() != MT5_SUCCESS_CODE:
                raise self._fail(f"copy_rates_from_pos({symbol}, {timeframe.value}, {count})")
            return _empty_frame()
        return self._to_frame(rows, timeframe)

    def range_chunks(
        self, symbol: str, timeframe: Any, start: datetime, end: datetime, chunk_days: int
    ) -> Iterator[pd.DataFrame]:
        """Walk a date range in pieces the terminal will actually serve.

        A single request for a year of M5 comes back None with 'Invalid params',
        which is indistinguishable from 'no data' unless you already know the
        limit exists. Hence chunking, and hence the error-code check.
        """
        self._require()
        constant = self._timeframe_constant(timeframe)
        step = timedelta(days=chunk_days)
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + step, end)
            rows = self.mt5.copy_rates_range(
                symbol, constant, self._to_server_naive(cursor), self._to_server_naive(chunk_end)
            )
            if rows is None:
                if self._last_error_code() != MT5_SUCCESS_CODE:
                    raise self._fail(
                        f"copy_rates_range({symbol}, {timeframe.value}, "
                        f"{cursor:%Y-%m-%d} -> {chunk_end:%Y-%m-%d})"
                    )
                rows = []
            if len(rows):
                yield self._to_frame(rows, timeframe)
            cursor = chunk_end

    def _to_frame(self, rows: Any, timeframe: Any) -> pd.DataFrame:
        # MT5 hands back a numpy *structured* array. `pd.DataFrame(list(rows))`
        # loses the field names under pandas 3.x and yields integer columns, so
        # from_records is used to keep ('time', 'open', ... ) intact.
        frame = pd.DataFrame.from_records(rows)
        offset = int(self._server_offset.total_seconds())
        frame["timestamp"] = pd.to_datetime(
            frame["time"].astype("int64") - offset, unit="s", utc=True
        )
        frame = frame.rename(columns={"tick_volume": "volume"})
        frame = frame[BAR_COLUMNS]
        # Drop any bar whose period has not fully elapsed. Belt to the
        # start_pos=1 braces, and the only guard that works for range reads.
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeframe.minutes)
        return frame[frame["timestamp"] <= pd.Timestamp(cutoff)]


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS})


@dataclass(frozen=True, slots=True)
class FetchReport:
    """What we actually got, so a short history is visible rather than silent."""

    timeframe: str
    bars: int
    oldest: datetime | None
    newest: datetime | None
    requested_days: int

    @property
    def actual_days(self) -> int:
        if self.oldest is None or self.newest is None:
            return 0
        return (self.newest - self.oldest).days

    def describe(self) -> str:
        if not self.bars:
            return f"{self.timeframe:>4}: NO DATA"
        return (
            f"{self.timeframe:>4}: {self.bars:>6,} bars  "
            f"{self.oldest:%Y-%m-%d} -> {self.newest:%Y-%m-%d}  "
            f"({self.actual_days}/{self.requested_days} days requested)"
        )


class BarStore:
    """Fetches history in chunks and caches it as parquet.

    The cache is keyed on symbol, timeframe and the requested window. Re-running
    a backtest should not re-download a year of M5 every time.
    """

    def __init__(self, session: Mt5Session, cache_dir: Path, chunk_days: int) -> None:
        self._session = session
        self._cache_dir = cache_dir
        self._chunk_days = chunk_days
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str, timeframe: Any, start: datetime, end: datetime) -> Path:
        stem = f"{symbol}_{timeframe.value}_{start:%Y%m%d}_{end:%Y%m%d}.parquet"
        return self._cache_dir / stem

    def history(
        self, symbol: str, timeframe: Any, start: datetime, end: datetime, *, refresh: bool = False
    ) -> tuple[pd.DataFrame, FetchReport]:
        path = self._cache_path(symbol, timeframe, start, end)
        if path.exists() and not refresh:
            frame = pd.read_parquet(path)
            log.info("cache hit %s (%d bars)", path.name, len(frame))
            return frame, _report(timeframe, frame, (end - start).days)

        log.info(
            "fetching %s %s from %s to %s in %d-day chunks",
            symbol, timeframe.value, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", self._chunk_days,
        )
        frames = list(
            self._session.range_chunks(symbol, timeframe, start, end, self._chunk_days)
        )
        if not frames:
            return _empty_frame(), _report(timeframe, _empty_frame(), (end - start).days)

        frame = pd.concat(frames, ignore_index=True)
        # Chunk boundaries can repeat a bar. A duplicate timestamp would be
        # replayed twice by the simulator, so they go at the source.
        before = len(frame)
        frame = frame.drop_duplicates(subset="timestamp", keep="first")
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        if before != len(frame):
            log.debug("dropped %d duplicate bars at chunk boundaries", before - len(frame))

        frame.to_parquet(path, index=False)
        report = _report(timeframe, frame, (end - start).days)
        log.info("%s", report.describe())
        return frame, report


def _report(timeframe: Any, frame: pd.DataFrame, requested_days: int) -> FetchReport:
    if frame.empty:
        return FetchReport(timeframe.value, 0, None, None, requested_days)
    return FetchReport(
        timeframe=timeframe.value,
        bars=len(frame),
        oldest=frame["timestamp"].iloc[0].to_pydatetime(),
        newest=frame["timestamp"].iloc[-1].to_pydatetime(),
        requested_days=requested_days,
    )
