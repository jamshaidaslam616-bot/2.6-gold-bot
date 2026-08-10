"""Configuration for the 2.6 Gold bot.

Two rules this module exists to enforce:

* **Credentials live in .env, never in source.** `.env` is gitignored, and
  nothing here ever logs or prints a password. The original brief put them in
  config.py; that puts a demo password one `git add` away from being public.
* **Live trading needs two independent unlocks**, both owner-set. There is no
  argument, flag or environment variable that skips `assert_live_unlocked()`.

Every risk number below is the account owner's, agreed and unchanged:
0.5% per trade, 3% daily loss, 10% max drawdown, one position at a time.
They are not tuned to improve a backtest.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


class Timeframe(str, Enum):
    """The three timeframes the bot watches.

    Each carries its own magic number so a position can always be attributed to
    the timeframe that opened it. Note this is *attribution*, not independence:
    the owner chose one position at a time globally, so `risk.py` gates entries
    across all three. See `TIMEFRAME_PRIORITY`.
    """

    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        return {"M1": 1, "M5": 5, "M15": 15, "M30": 30,
                "H1": 60, "H4": 240, "D1": 1440}[self.value]

    @property
    def magic(self) -> int:
        # 2600 + minutes, so a magic number says which timeframe opened it.
        return 2600000 + self.minutes

    @property
    def mt5_constant_name(self) -> str:
        """Resolved by name against the MetaTrader5 module rather than
        hardcoded, so tests can substitute a stand-in module."""
        return f"TIMEFRAME_{self.value}"


#: Which timeframes the live daemon actually watches. Kept narrow on purpose —
#: every timeframe added is another chance for a coin flip to look like a
#: signal. Widen only on out-of-sample evidence, never on a backtest total.
#:
#: M15 alone, decided 2026-08-10. Of the seven timeframes tested over four
#: years on XAUUSD, it was the only one positive in BOTH the in-sample and
#: out-of-sample halves. That is a weak reason and it is the best one available:
#: no timeframe reached |t| > 2, and with seven candidates there was an ~87%
#: chance that at least one would look good by luck alone. M15 is the least bad
#: choice, not a proven one.
LIVE_TIMEFRAMES: tuple[Timeframe, ...] = (Timeframe.M15,)

#: Highest timeframe wins when two fire on the same loop. Higher-timeframe
#: structure survives noise that shakes out a 5-minute swing.
TIMEFRAME_PRIORITY: tuple[Timeframe, ...] = tuple(
    sorted(LIVE_TIMEFRAMES, key=lambda tf: -tf.minutes)
)

#: All magic numbers this bot may ever touch. Anything else on the account
#: belongs to someone else and is never read as ours, modified, or closed.
OUR_MAGICS: frozenset[int] = frozenset(tf.magic for tf in Timeframe)


@dataclass(frozen=True, slots=True)
class Secrets:
    mt5_terminal_path: str
    mt5_login: int
    mt5_password: str
    mt5_server: str
    telegram_token: str
    telegram_chat_id: str
    live_trading_enabled: bool
    live_confirmation_phrase: str

    @property
    def has_mt5_credentials(self) -> bool:
        return bool(self.mt5_login and self.mt5_password and self.mt5_server)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        """Redacted on purpose. A Secrets object can end up in a traceback."""
        return (
            f"Secrets(login={self.mt5_login or '<unset>'}, "
            f"server={self.mt5_server or '<unset>'}, password=<redacted>, "
            f"telegram={'set' if self.has_telegram else 'unset'})"
        )


def load_secrets() -> Secrets:
    login_raw = _env("MT5_LOGIN")
    return Secrets(
        mt5_terminal_path=_env("MT5_TERMINAL_PATH"),
        mt5_login=int(login_raw) if login_raw.isdigit() else 0,
        mt5_password=_env("MT5_PASSWORD"),
        mt5_server=_env("MT5_SERVER"),
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        live_trading_enabled=_env("LIVE_TRADING_ENABLED").lower() == "true",
        live_confirmation_phrase=_env("LIVE_CONFIRMATION_PHRASE"),
    )


class TakeProfitMode(str, Enum):
    """How the target is placed.

    Worth knowing what the spec's own geometry costs. With entry at the 38.46%
    retracement and the stop on the Origin, risk is 0.6154 x Range, so a 1:2
    target lands 0.846 x Range *beyond* the Peak — every winner needs a large
    new extreme, which is why the win rate sits near 34%.
    """

    RISK_MULTIPLE = "rr"          # entry +/- min_risk_reward * risk  (the spec)
    PEAK = "peak"                 # the prior extreme: price returns to where it turned
    PEAK_EXTENDED = "peak_extended"   # the Peak plus a fraction of the range


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """The 2.6 rules.

    `retracement_divisor` is the 2.6 itself. Entry sits `leg / 2.6` away from
    the impulse's *origin*, which is 38.46% of the leg from the origin and
    therefore a **61.5% retracement** measured the conventional way, from the
    impulse's end. Both readings describe the same price; the owner confirmed
    the 61.5% one on 2026-08-07.
    """

    retracement_divisor: float = 2.6

    # Fractal detection. `swing_right_bars` is also the confirmation lag: a
    # swing at bar i cannot be known until bar i + swing_right_bars. Raising it
    # gives cleaner structure and slower signals.
    swing_left_bars: int = 3
    swing_right_bars: int = 3

    # The spec puts the stop exactly on the Origin wick, so this defaults to
    # zero. It is left configurable because a stop sitting precisely on the
    # extreme is the most-hunted price on the chart, but widening it is a
    # deviation from the rules and should be a deliberate one.
    sl_buffer_leg_fraction: float = 0.0

    # A Break of Structure counts only when a candle *body* closes beyond the
    # previous swing level. A wick poking through is not a break — that
    # distinction is the whole point of the rule.
    bos_requires_body_close: bool = True

    # Where the target goes. The spec says "kam az kam 1:2", which is
    # RISK_MULTIPLE at 2.0 — that is the default and the only spec-compliant
    # setting. The other modes exist because the owner asked for the target to
    # be tuned; they are deviations and are labelled as such in reports.
    take_profit_mode: TakeProfitMode = TakeProfitMode.RISK_MULTIPLE
    min_risk_reward: float = 2.0

    #: PEAK_EXTENDED only: how far past the Peak to aim, as a fraction of the
    #: impulse range. 0.0 targets the Peak itself.
    peak_extension: float = 0.0

    # A pending entry is abandoned after this many bars of its own timeframe.
    # A stale limit order sitting through a regime change is a live grenade.
    setup_ttl_bars: int = 24

    # Minimum impulse size in *price units* (dollars, for gold). Legs smaller
    # than this are noise: with an M15 ATR around $8, a sub-$2 leg produces an
    # entry-to-stop distance the spread alone can swallow.
    min_leg_price: float = 2.0

    symbol_search_patterns: tuple[str, ...] = ("XAUUSD", "XAUUSDm", "XAUUSD.", "GOLD")


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Owner-set. Not tuned, not raised to make a backtest look better."""

    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    max_concurrent_positions: int = 1
    max_trades_per_day: int = 10

    # Spread filter. The point is to skip abnormally wide conditions — news,
    # rollover, thin liquidity — so the threshold has to be relative to the
    # instrument, not an absolute point count.
    #
    # An absolute number cannot work across account types. Measured on this
    # broker: XAUUSD on a Zero account has a *median spread of zero* (you pay
    # commission instead), while XAUUSDm on a mini account runs 160-500 points.
    # A single figure either blocks every mini trade or lets every Zero one
    # through. So the live limit is this percentile of the instrument's own
    # recent spread distribution, and `max_spread_points` is only a backstop
    # against nonsense.
    # A percentile alone is not enough. On a Zero account the spread is almost
    # a constant 50 points, so p95 lands *on* the normal value and every tick
    # gets refused — the bot sits idle forever while logging "spread 50 exceeds
    # 50". Observed live on 2026-08-10. The median multiple gives the filter
    # room to breathe on instruments whose spread barely varies; whichever is
    # more generous wins, and the absolute ceiling still catches nonsense.
    max_spread_percentile: float = 99.0
    max_spread_median_multiple: float = 2.0
    max_spread_points: float = 600.0

    # Refuse to trade if the terminal's clock has drifted from ours. A skewed
    # clock corrupts every session and bar-age check silently.
    max_clock_drift_seconds: float = 30.0

    margin_headroom_pct: float = 50.0


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Measured, not assumed.

    `commission_per_lot_per_side` comes from a real fill on this broker on
    2026-08-07: a 0.01-lot XAUUSD round trip was charged $0.11, all of it on
    the entry deal. That is $11.00 per lot round turn, i.e. $5.50 per side.

    `slippage_points` exists because fills are not exact. The same trade's take
    profit was set at 4263.732 and filled at 4264.152 — 0.42 in our favour that
    time. A backtest that assumes exact fills quietly flatters itself, so the
    simulator applies this against us in both directions.
    """

    commission_per_lot_per_side: float = 5.50

    #: Applied against us at both entry and exit. One observation is not a
    #: distribution, so rather than trusting this number the backtester reports
    #: results at several cost levels and lets the sensitivity speak.
    slippage_points: float = 20.0

    #: Use each bar's own recorded spread rather than a single average. Gold's
    #: spread at 03:00 UTC and during NFP are different instruments.
    use_bar_spread: bool = True
    fallback_spread_points: float = 50.0


@dataclass(frozen=True, slots=True)
class Paths:
    root: Path = ROOT
    logs: Path = field(default=ROOT / "logs")
    cache: Path = field(default=ROOT / "cache")
    runtime: Path = field(default=ROOT / "runtime")
    reports: Path = field(default=ROOT / "reports")

    @property
    def kill_switch(self) -> Path:
        return self.runtime / "KILL_SWITCH"

    @property
    def journal_csv(self) -> Path:
        return self.runtime / "journal.csv"

    def ensure(self) -> None:
        for directory in (self.logs, self.cache, self.runtime, self.reports):
            directory.mkdir(parents=True, exist_ok=True)


STRATEGY = StrategyConfig()
RISK = RiskConfig()
COSTS = CostConfig()
PATHS = Paths()

BACKTEST_MONTHS = 12
#: The terminal returns None with "Invalid params" for very large requests —
#: verified on 2026-08-07: 65,000 bars served, 100,000 refused. Range reads are
#: chunked well inside that, which is also why a 12-month M5 pull must be
#: chunked rather than asked for in one call.
FETCH_CHUNK_DAYS = 30


class LiveTradingLocked(RuntimeError):
    """Raised when live trading is attempted without both owner unlocks."""


def assert_live_unlocked(secrets: Secrets) -> None:
    """The only door to live trading. Both unlocks are the owner's to set.

    I do not set either of these, and nothing in this codebase writes to .env.
    """
    if not secrets.live_trading_enabled:
        raise LiveTradingLocked(
            "LIVE_TRADING_ENABLED is not true. Live trading is locked."
        )
    if len(secrets.live_confirmation_phrase) < 8:
        raise LiveTradingLocked(
            "LIVE_CONFIRMATION_PHRASE is unset or too short. Live trading is locked."
        )
