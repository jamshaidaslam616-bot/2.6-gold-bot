"""Live daemon — the 2.6 strategy against a running MT5 terminal.

Loop shape, and why it is in this order:

  1. read equity and let the risk layer see it (drawdown counts unrealised P&L)
  2. reconcile against the broker — did anything close while we were asleep?
  3. if a position is open, do nothing else. It is bracketed at the broker, so
     it is managed even if this process dies.
  4. otherwise resolve any resting order: still valid, or superseded/expired?
  5. only then look for a new setup, highest timeframe first

Step 2 before step 5 is the important one. Local memory is a hint; the broker
is the truth. A daemon that trusts its own state after a restart will happily
open a second position on top of one it has forgotten about.

The owner chose **one position at a time across all three timeframes**, so the
per-timeframe magic numbers are for attribution, not independence. When two
timeframes signal on the same cycle, H1 beats M15 beats M5 — higher-timeframe
structure survives noise that shakes out a five-minute swing.
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import logger as logging_setup
from config import (
    COSTS,
    MANAGEMENT,
    PATHS,
    RISK,
    STRATEGY,
    TIMEFRAME_PRIORITY,
    Timeframe,
    load_secrets,
)
from data import MAX_TICK_AGE_SECONDS, BrokerError, Mt5Session, SymbolSpec
from engine import Direction, Setup, SetupState, TwoSixEngine
from execution import Executor, OrderRequest
from journal import Journal, JournalRow
from risk import RiskManager, size_position
from telegram_bot import TelegramNotifier

log = logging_setup.get("main")

LOOP_SECONDS = 15
BARS_FOR_STRUCTURE = 400
#: How often to log an "alive and idle" line when nothing is happening.
HEARTBEAT_SECONDS = 300
MAX_CONSECUTIVE_ERRORS = 5


class Daemon:
    def __init__(self) -> None:
        PATHS.ensure()
        # Configure logging before anything else can want to log. Without this
        # the root logger has no handlers and every log line in the daemon is
        # silently discarded — the bot runs fine and leaves no record of what
        # it did, which is the worst of both worlds when a trade goes wrong.
        logging_setup.setup(PATHS.logs, filename="bot.log")
        self.secrets = load_secrets()
        self.engine = TwoSixEngine(STRATEGY)
        self.journal = Journal(PATHS.journal_csv)
        self.risk = RiskManager(
            RISK, COSTS, PATHS.runtime / "risk_state.json", PATHS.kill_switch
        )
        self.session: Mt5Session | None = None
        self.executor: Executor | None = None
        self.spec: SymbolSpec | None = None
        self.notifier: TelegramNotifier | None = None

        self._pending: tuple[Timeframe, Setup, int] | None = None
        self._open_ticket: int | None = None
        self._open_setup: tuple[Timeframe, Setup] | None = None
        self._pending_raw: dict | None = None
        self._errors = 0
        self._last_heartbeat = datetime.min.replace(tzinfo=timezone.utc)
        self._stop = asyncio.Event()
        self._last_summary_day = ""

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if not self.secrets.has_mt5_credentials:
            raise SystemExit(
                "MT5_LOGIN / MT5_PASSWORD / MT5_SERVER are not set in .env.\n"
                "This daemon drives its OWN terminal on its OWN account — see README."
            )
        if not self.secrets.mt5_terminal_path:
            log.warning(
                "MT5_TERMINAL_PATH is unset. With more than one MT5 installed the "
                "API picks one for you, and logging in will switch whichever terminal "
                "it picks — which is how the other bot on this machine lost its symbol "
                "for an hour. Set it."
            )

        self.session = Mt5Session(
            terminal_path=self.secrets.mt5_terminal_path,
            login=self.secrets.mt5_login,
            password=self.secrets.mt5_password,
            server=self.secrets.mt5_server,
        )
        account = await asyncio.to_thread(self.session.connect)
        self.spec = await asyncio.to_thread(
            self.session.resolve_symbol, STRATEGY.symbol_search_patterns
        )
        self.executor = Executor(self.session, self.spec, self.secrets)

        log.info(
            "2.6 gold bot up — %s account %s, symbol %s, risk %.2f%%/trade, "
            "one position at a time",
            account.kind, account.login, self.spec.name, RISK.risk_per_trade_pct,
        )
        await self.notifier.send(
            f"🤖 <b>2.6 bot started</b>\n<code>account  {account.login} ({account.kind})\n"
            f"symbol   {self.spec.name}\nequity   {account.equity:,.2f}\n"
            f"risk     {RISK.risk_per_trade_pct}%/trade, 1 position max</code>"
        )
        await self._adopt_broker_state()

    async def _adopt_broker_state(self) -> None:
        """Take ownership of whatever we left behind last time.

        Without this a restart loses the thread: an order resting at the broker
        is invisible to the daemon, so when it fills nothing journals it, and
        when it later closes `record_close` finds no open row and drops the
        trade from the record entirely. The position is still bracketed and
        still safe — but it goes unrecorded, and an unrecorded trade cannot be
        counted, which defeats the whole point of running this on demo.

        `runtime/pending.json` carries the structure fields the broker does not
        know (the impulse range and its swings) so an adopted fill is journalled
        as completely as a fresh one.
        """
        assert self.executor
        positions = await asyncio.to_thread(self.executor.open_positions)
        orders = await asyncio.to_thread(self.executor.pending_orders)
        saved = self._load_pending()

        if positions:
            position = positions[0]
            self._open_ticket = position.ticket
            timeframe = Timeframe.from_magic(position.magic)
            log.info("adopted open position %s (%s, opened %s)",
                     position.ticket, timeframe.value if timeframe else "?",
                     f"{position.opened_at:%Y-%m-%d %H:%M}")
            if not self._journal_has(position.ticket):
                # It filled while we were down. Journal it from what the broker
                # knows; the structure columns stay blank rather than invented.
                await self._record_fill_from_broker(position, saved)

        if orders:
            order = orders[0]
            if saved and saved.get("ticket") == order.ticket:
                log.info("adopted resting order %s from saved state", order.ticket)
                self._pending_raw = saved
            else:
                log.warning(
                    "order %s is resting at the broker but we have no record of "
                    "its setup — cancelling rather than managing it blind",
                    order.ticket,
                )
                await asyncio.to_thread(self.executor.cancel, order.ticket)
                self._clear_pending()
        elif saved:
            self._clear_pending()   # the order is gone; the note is stale

    def _journal_has(self, ticket: int) -> bool:
        return any(r.get("ticket") == str(ticket) for r in self.journal.read())

    def _pending_path(self) -> Path:
        return PATHS.runtime / "pending.json"

    def _save_pending(self, timeframe: Timeframe, setup: Setup, ticket: int) -> None:
        self._pending_path().write_text(json.dumps({
            "timeframe": timeframe.value,
            "ticket": ticket,
            "direction": setup.direction.value,
            "entry": setup.entry,
            "stop_loss": setup.stop_loss,
            "take_profit": setup.take_profit,
            "leg": setup.impulse.range,
            "swing_high": setup.impulse.swing_high,
            "swing_low": setup.impulse.swing_low,
        }, indent=2), encoding="utf-8")

    def _load_pending(self) -> dict | None:
        path = self._pending_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt note is not worth dying for
            log.warning("pending.json is unreadable — ignoring it")
            return None

    def _clear_pending(self) -> None:
        self._pending_path().unlink(missing_ok=True)

    async def stop(self) -> None:
        if self.executor is not None:
            cancelled = await asyncio.to_thread(self.executor.cancel_all_ours)
            if cancelled:
                log.info("cancelled %d resting order(s) on the way out", cancelled)
        if self.session is not None:
            await asyncio.to_thread(self.session.disconnect)
        log.info("stopped. %s", self.journal.summary())

    # -- the loop -----------------------------------------------------------

    async def run(self) -> None:
        async with TelegramNotifier(
            self.secrets.telegram_token, self.secrets.telegram_chat_id
        ) as notifier:
            self.notifier = notifier
            await self.start()
            try:
                while not self._stop.is_set():
                    try:
                        await self.cycle()
                        self._errors = 0
                    except BrokerError as exc:
                        await self._handle_error(f"broker: {exc}")
                    except Exception as exc:  # noqa: BLE001 - the daemon must not die quietly
                        log.exception("unhandled error in cycle")
                        await self._handle_error(f"{type(exc).__name__}: {exc}")
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=LOOP_SECONDS)
                    except asyncio.TimeoutError:
                        pass
            finally:
                await self.stop()

    async def _handle_error(self, what: str) -> None:
        self._errors += 1
        log.error("cycle error %d/%d: %s", self._errors, MAX_CONSECUTIVE_ERRORS, what)
        await self.notifier.system_error(f"{what} ({self._errors}/{MAX_CONSECUTIVE_ERRORS})")
        if self._errors >= MAX_CONSECUTIVE_ERRORS:
            # Deliberately not an infinite retry. A bridge that keeps failing is
            # a bridge in an unknown state, and orders must not be sent into one.
            self.risk.engage_kill_switch(f"{self._errors} consecutive errors: {what}")
            await self.notifier.halted(f"{self._errors} consecutive errors — halting")
            self._stop.set()

    async def cycle(self) -> None:
        assert self.session and self.executor and self.spec

        account = await asyncio.to_thread(self.session.account)
        self.risk.observe_equity(account.equity)
        await self._daily_summary_if_due(account.equity)

        positions = await asyncio.to_thread(self.executor.open_positions)

        # 2. reconcile: did our position close while we were away?
        if self._open_ticket and not any(p.ticket == self._open_ticket for p in positions):
            await self._record_close(self._open_ticket, account.equity)
            self._open_ticket = None
            self._open_setup = None

        # 2b. did a resting order fill? The journal is written HERE, not when
        #     the order was placed — an order that expires unfilled is not a
        #     trade, and journalling it at placement left a phantom row that
        #     never closed.
        if self._pending is not None:
            timeframe, setup, ticket = self._pending
            filled = next((p for p in positions if p.ticket == ticket), None)
            if filled is not None:
                await self._record_fill(timeframe, setup, filled, account.equity)
                self._pending = None
        elif self._pending_raw is not None:
            # An order adopted from a previous run: we know its ticket and its
            # structure from disk, but not the Setup object.
            ticket = int(self._pending_raw["ticket"])
            filled = next((p for p in positions if p.ticket == ticket), None)
            if filled is not None:
                await self._record_fill_from_broker(filled, self._pending_raw)
                self._pending_raw = None
            elif not any(o.ticket == ticket for o in
                         await asyncio.to_thread(self.executor.pending_orders)):
                log.info("adopted order %s is gone without filling", ticket)
                self._pending_raw = None
                self._clear_pending()

        # 3. a bracketed position needs nothing from us, except the one thing
        #    the broker cannot do: close it when it has been open too long.
        if positions:
            self._open_ticket = positions[0].ticket
            await self._close_if_stale(positions[0], account.equity)
            return

        # 4. resolve any resting order
        if self._pending is not None:
            if await self._resolve_pending(positions):
                return
        elif self._pending_raw is not None:
            # Adopted from a previous run. There is no Setup object to
            # re-evaluate, so supersede/invalidate cannot be checked — the
            # broker-side expiry handles it instead. What matters here is that
            # we do NOT fall through and try to place another order: the
            # idempotency check would refuse it, but only after the daemon had
            # rebuilt the whole signal and asked the broker, every 15 seconds,
            # for as long as the order rested.
            await self._heartbeat(account.equity, "adopted order resting")
            return

        # 5. look for a new setup.
        #    A shut market first: the quote stops updating at the weekend, and
        #    both the server-time offset and everything derived from it become
        #    unmeasurable. Re-measuring here means the offset is picked up as
        #    soon as the market reopens rather than staying wrong all week.
        await asyncio.to_thread(self.session.refresh_server_offset)
        tick_age = await asyncio.to_thread(self.session.tick_age_seconds)
        if tick_age > MAX_TICK_AGE_SECONDS:
            await self._heartbeat(
                account.equity,
                f"market shut — last quote {tick_age / 3600:.1f}h old",
            )
            return

        decision = self.risk.can_open(
            is_demo=account.is_demo,
            open_positions=len(positions),
            spread_points=await asyncio.to_thread(
                self.session.live_spread_points, self.spec.name
            ),
            clock_drift_seconds=await asyncio.to_thread(self.session.clock_drift_seconds),
            equity=account.equity,
            margin_free=account.margin_free,
            spread_limit_points=await self._spread_limit(),
        )
        if not decision.allowed:
            log.debug("no entry: %s", decision.reason)
            await self._heartbeat(account.equity, decision.reason)
            return

        await self._look_for_setup(account.equity)
        await self._heartbeat(account.equity, "scanning")

    async def _close_if_stale(self, position, equity: float) -> None:
        """Close a position that has outlived its holding limit.

        A pending order expires at the broker; an open position does not, and
        the first live one sat for 63 hours blocking every other setup. The
        broker has no mechanism for this, so the daemon has to do it — which
        also means a dead daemon leaves the position open on its brackets
        rather than closing it, and that is the right way round.
        """
        limit = MANAGEMENT.max_hold_bars
        if not limit:
            return
        # From the magic, not from memory: after a restart there is no memory,
        # and defaulting to a timeframe would apply the wrong limit silently.
        timeframe = Timeframe.from_magic(position.magic) or Timeframe.M15

        # Counted in CLOSED BARS, not wall-clock minutes, because that is what
        # the backtest counts. Over a weekend the clock runs and the market does
        # not; using minutes would force-close Friday-evening positions into a
        # shut market and make live behaviour diverge from the simulation.
        bars = await asyncio.to_thread(
            self.session.recent_bars, self.spec.name, timeframe, limit + 8
        )
        if bars.empty:
            return
        elapsed = int((bars["timestamp"] > pd.Timestamp(position.opened_at)).sum())
        if elapsed < limit:
            return

        age_hours = (datetime.now(timezone.utc) - position.opened_at).total_seconds() / 3600
        log.info(
            "position %s has been open %d bars (%.1fh), past the %d-bar limit — closing",
            position.ticket, elapsed, age_hours, limit,
        )
        result = await asyncio.to_thread(self.executor.close, position.ticket)
        if not result.ok:
            log.warning("could not close %s: %s", position.ticket, result.describe())
            await self.notifier.system_error(f"stale close failed: {result.describe()}")
            return
        await self.notifier.send(
            f"⏱ <b>TIME EXIT</b>\n<code>ticket {position.ticket}\n"
            f"open {elapsed} bars ({age_hours:.1f}h), limit {limit}\n"
            f"floating was {position.floating_pnl:+.2f}</code>"
        )

    async def _journal_fill(self, timeframe: Timeframe, position, equity: float,
                            leg: float, swing_high: float, swing_low: float) -> None:
        """The one place a fill is written. Stop and target are read back from
        the broker rather than from our own request, so the journal records what
        is actually protecting the position."""
        assert self.spec and self.session
        self._open_ticket = position.ticket

        mppu = self.spec.money_per_price_unit_per_lot
        risk_usd = abs(position.entry - position.stop_loss) * position.lots * mppu
        reward_usd = abs(position.take_profit - position.entry) * position.lots * mppu
        breakeven = risk_usd / (risk_usd + reward_usd) * 100 if (risk_usd + reward_usd) else 0.0
        spread = await asyncio.to_thread(self.session.live_spread_points, self.spec.name)

        self.journal.record_open(
            JournalRow(
                opened_utc=position.opened_at.isoformat(timespec="seconds"),
                timeframe=timeframe.value,
                magic=position.magic,
                account=self.session.account().login,
                symbol=self.spec.name,
                direction=position.direction.value,
                ticket=position.ticket,
                lots=position.lots,
                entry=round(position.entry, self.spec.digits),
                stop_loss=round(position.stop_loss, self.spec.digits),
                take_profit=round(position.take_profit, self.spec.digits),
                risk_usd=round(risk_usd, 2),
                reward_usd=round(reward_usd, 2),
                breakeven_winrate_pct=round(breakeven, 1),
                leg=round(leg, self.spec.digits),
                swing_high=round(swing_high, self.spec.digits),
                swing_low=round(swing_low, self.spec.digits),
                spread_points_at_entry=round(spread, 1),
                equity_at_entry=round(equity, 2),
            )
        )
        log.info("FILLED %s %s %.2f lots at %.3f  sl %.3f  tp %.3f",
                 position.ticket, position.direction.value, position.lots,
                 position.entry, position.stop_loss, position.take_profit)
        await self.notifier.trade_opened(
            direction=position.direction.value, lots=position.lots, symbol=self.spec.name,
            timeframe=timeframe.value, entry=position.entry, stop_loss=position.stop_loss,
            take_profit=position.take_profit, risk_usd=risk_usd, reward_usd=reward_usd,
            leg=leg, ticket=position.ticket, breakeven_winrate_pct=breakeven,
        )

    async def _record_fill(self, timeframe: Timeframe, setup: Setup, position,
                           equity: float) -> None:
        self._open_setup = (timeframe, setup)
        await self._journal_fill(timeframe, position, equity, setup.impulse.range,
                                 setup.impulse.swing_high, setup.impulse.swing_low)
        self._clear_pending()

    async def _record_fill_from_broker(self, position, saved: dict | None) -> None:
        """Journal a fill we were not running to see.

        The broker knows the price, size, stop and target. It does not know the
        impulse that produced them, so those columns come from the saved note
        if there is one and are left at zero if not — blank is honest, invented
        numbers are not.
        """
        assert self.session
        timeframe = Timeframe.from_magic(position.magic) or Timeframe.M15
        equity = self.session.account().equity
        if saved and saved.get("ticket") == position.ticket:
            await self._journal_fill(timeframe, position, equity, float(saved["leg"]),
                                     float(saved["swing_high"]), float(saved["swing_low"]))
        else:
            log.warning("journalling %s without its structure — no saved setup",
                        position.ticket)
            await self._journal_fill(timeframe, position, equity, 0.0, 0.0, 0.0)
        self._clear_pending()

    async def _heartbeat(self, equity: float, note: str) -> None:
        """Say something at a fixed interval even when nothing is happening.

        A daemon that only logs when it acts is indistinguishable from a dead
        one. The first live run sat silent for 37 minutes and then died; the
        silence looked exactly like healthy standing-aside. This makes 'alive
        and idle' and 'stopped' different in the log file, which is where
        anyone will look first.
        """
        now = datetime.now(timezone.utc)
        if (now - self._last_heartbeat).total_seconds() < HEARTBEAT_SECONDS:
            return
        self._last_heartbeat = now
        log.info(
            "heartbeat | equity %.2f | today %+.2f in %d trades | pending %s | %s",
            equity, self.risk.state.realised_today, self.risk.state.trades_today,
            self._pending[2] if self._pending else "none", note,
        )

    async def _spread_limit(self) -> float:
        """What counts as an abnormally wide spread on *this* instrument.

        Measured from its own recent history, because an absolute point count
        cannot span account types: XAUUSD on a Zero account has a median spread
        of zero, XAUUSDm on a mini account runs 160-500 points.
        """
        assert self.session and self.spec
        bars = await asyncio.to_thread(
            self.session.recent_bars, self.spec.name, Timeframe.M5, 500
        )
        if bars.empty:
            return RISK.max_spread_points
        return max(
            float(bars["spread"].quantile(RISK.max_spread_percentile / 100.0)),
            float(bars["spread"].median()) * RISK.max_spread_median_multiple,
        )

    # -- pending order management ------------------------------------------

    async def _resolve_pending(self, positions: list) -> bool:
        """True if the pending order still stands and we should do nothing else."""
        assert self.executor and self.session and self.spec
        timeframe, setup, ticket = self._pending

        live = await asyncio.to_thread(self.executor.pending_orders)
        if not any(o.ticket == ticket for o in live):
            # Gone: either filled (a position would exist) or expired at the broker.
            self._pending = None
            return False

        bars = await asyncio.to_thread(
            self.session.recent_bars, self.spec.name, timeframe, BARS_FOR_STRUCTURE
        )
        if bars.empty:
            return True
        structure = self.engine.analyse(
            bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy(),
            [t.to_pydatetime() for t in bars["timestamp"]],
        )
        last = len(bars) - 1
        state = self.engine.update_setup(
            setup, bar_index=last, bar_high=float(bars["high"].iloc[-1]),
            bar_low=float(bars["low"].iloc[-1]), structure=structure,
        )
        if state in (SetupState.SUPERSEDED, SetupState.INVALIDATED, SetupState.EXPIRED):
            result = await asyncio.to_thread(self.executor.cancel, ticket)
            log.info("cancelling resting order %s (%s): %s", ticket, state.value,
                     result.describe())
            self._pending = None
            return False
        return True

    # -- entry --------------------------------------------------------------

    async def _look_for_setup(self, equity: float) -> None:
        assert self.session and self.executor and self.spec

        for timeframe in TIMEFRAME_PRIORITY:
            bars = await asyncio.to_thread(
                self.session.recent_bars, self.spec.name, timeframe, BARS_FOR_STRUCTURE
            )
            if len(bars) < 100:
                continue
            structure = self.engine.analyse(
                bars["high"].to_numpy(), bars["low"].to_numpy(),
                bars["close"].to_numpy(),
                [t.to_pydatetime() for t in bars["timestamp"]],
            )
            setup = self.engine.signal_at(
                structure,
                as_of_index=len(bars) - 1,
                reference_price=float(bars["close"].iloc[-1]),
                min_stop_distance=self.spec.stops_level_points * self.spec.point,
            )
            if setup is None:
                continue

            sizing = size_position(
                equity=equity, spec=self.spec, stop_distance=setup.risk_distance,
                risk_cfg=RISK, costs=COSTS,
            )
            if not sizing.ok:
                log.info("%s setup found but %s", timeframe.value, sizing.describe())
                continue

            await self._place(timeframe, setup, sizing, equity)
            return

    async def _place(self, timeframe: Timeframe, setup: Setup, sizing, equity: float) -> None:
        assert self.executor and self.spec and self.session

        expiry = datetime.now(timezone.utc) + timedelta(
            minutes=timeframe.minutes * STRATEGY.setup_ttl_bars
        )
        request = OrderRequest(
            symbol=self.spec.name,
            direction=setup.direction,
            lots=sizing.lots,
            entry=setup.entry,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            magic=timeframe.magic,
            comment=f"gold2.6 {timeframe.value}",
            expiry=expiry,
        )
        log.info("%s %s | sizing %s", timeframe.value, setup.describe(), sizing.describe())
        result = await asyncio.to_thread(self.executor.place, request)
        if not result.ok:
            log.warning("order not placed: %s", result.describe())
            await self.notifier.system_error(f"order not placed: {result.describe()}")
            return

        # Nothing is journalled or announced here. A resting order is not a
        # trade — the first one placed live expired six hours later without
        # ever filling, and journalling at placement left a row that could
        # never be closed. The journal is written when the fill is observed.
        self._pending = (timeframe, setup, result.ticket or 0)
        self._save_pending(timeframe, setup, result.ticket or 0)
        log.info("order %s resting at %.3f, expires %s",
                 result.ticket, setup.entry, f"{expiry:%H:%M}")
        await self.notifier.send(
            f"📌 <b>ORDER PLACED</b>  {setup.direction.value.upper()} "
            f"{sizing.lots} {self.spec.name}\n"
            f"<code>waiting at {setup.entry:.3f}\n"
            f"stop  {setup.stop_loss:.3f}\ntarget {setup.take_profit:.3f}\n"
            f"expires {expiry:%H:%M} UTC</code>\n"
            f"<i>not filled yet — this is a resting order</i>"
        )

    # -- exit ---------------------------------------------------------------

    async def _record_close(self, ticket: int, equity: float) -> None:
        assert self.executor
        realised = await asyncio.to_thread(self.executor.realised_pnl, ticket)
        reason = await asyncio.to_thread(self.executor.exit_reason, ticket)
        self.journal.record_close(
            ticket, exit_price=0.0, exit_reason=reason, realised_usd=realised
        )
        self.risk.record_closed_trade(realised)
        log.info("closed %s: %s %+.2f USD", ticket, reason, realised)

        rows = [r for r in self.journal.read() if r.get("ticket") == str(ticket)]
        r_multiple = float(rows[-1].get("r_multiple") or 0) if rows else 0.0
        await self.notifier.trade_closed(
            ticket=ticket, exit_price=0.0, exit_reason=reason,
            realised_usd=realised, r_multiple=r_multiple, equity=equity,
        )
        if self.risk.state.halted_reason:
            await self.notifier.halted(self.risk.state.halted_reason)

    async def _daily_summary_if_due(self, equity: float) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_summary_day and self._last_summary_day != today:
            await self.notifier.daily_summary(self.journal.summary(), equity)
        self._last_summary_day = today

    def request_stop(self) -> None:
        log.info("shutdown requested")
        self._stop.set()


async def amain() -> None:
    daemon = Daemon()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            # Windows does not support add_signal_handler for SIGTERM; KeyboardInterrupt
            # is caught below instead.
            pass
    await daemon.run()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(0)
