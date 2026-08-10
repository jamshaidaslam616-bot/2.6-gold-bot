"""Order routing against a live MT5 terminal.

Four rules are enforced structurally rather than by discipline:

1. **Demo only, until two owner unlocks say otherwise.** `Executor` refuses to
   construct against a non-demo account unless `assert_live_unlocked()` passes,
   and nothing in this codebase writes to `.env`.
2. **No naked orders, ever.** `OrderRequest` will not construct without both a
   stop loss and a take profit. There is no "send it and add the stop after" —
   that gap is where accounts die.
3. **Idempotency before every send.** An ambiguous timeout is not retried
   blindly; the account is re-read first. Double-sending is the single most
   expensive bug class in live trading.
4. **We only ever touch our own magic numbers.** Two other bots share this
   machine. A position that is not ours is not read as ours, not modified, and
   not closed.

The 2.6 entry is a *level*, so the natural instrument is a resting limit order:
it fills at the price the strategy computed or better, which is exactly what
the backtester assumes. A market-on-touch order would fill at whatever is there
and quietly diverge from the simulation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import logger as logging_setup
from config import OUR_MAGICS, Secrets, assert_live_unlocked
from data import BrokerError, Mt5Session, SymbolSpec
from engine import Direction

log = logging_setup.get("execution")


class NakedOrderRefused(ValueError):
    """An order without both brackets. Not recoverable, not configurable."""


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    direction: Direction
    lots: float
    stop_loss: float
    take_profit: float
    magic: int
    entry: float | None = None      # None means market
    comment: str = "gold2.6"
    expiry: datetime | None = None

    def __post_init__(self) -> None:
        if self.lots <= 0:
            raise ValueError(f"lots must be positive, got {self.lots}")
        if not self.stop_loss or self.stop_loss <= 0:
            raise NakedOrderRefused("stop_loss is mandatory on every order")
        if not self.take_profit or self.take_profit <= 0:
            raise NakedOrderRefused("take_profit is mandatory on every order")
        if self.direction is Direction.BULLISH:
            if self.stop_loss >= (self.entry or self.take_profit):
                raise NakedOrderRefused("buy stop_loss must sit below the entry")
            if self.entry is not None and self.take_profit <= self.entry:
                raise NakedOrderRefused("buy take_profit must sit above the entry")
        else:
            if self.stop_loss <= (self.entry or self.take_profit):
                raise NakedOrderRefused("sell stop_loss must sit above the entry")
            if self.entry is not None and self.take_profit >= self.entry:
                raise NakedOrderRefused("sell take_profit must sit below the entry")

    @property
    def is_pending(self) -> bool:
        return self.entry is not None


@dataclass(frozen=True, slots=True)
class OrderResult:
    ok: bool
    retcode: int
    message: str
    ticket: int | None = None
    fill_price: float | None = None
    volume: float = 0.0

    def describe(self) -> str:
        state = "OK" if self.ok else "FAILED"
        return f"{state} retcode={self.retcode} ticket={self.ticket} {self.message}"


@dataclass(frozen=True, slots=True)
class Position:
    ticket: int
    symbol: str
    direction: Direction
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    magic: int
    opened_at: datetime
    floating_pnl: float


@dataclass(frozen=True, slots=True)
class PendingOrder:
    ticket: int
    symbol: str
    direction: Direction
    lots: float
    price: float
    magic: int


class Executor:
    def __init__(self, session: Mt5Session, spec: SymbolSpec, secrets: Secrets) -> None:
        self._session = session
        self._spec = spec
        self._secrets = secrets

        account = session.account()
        if not account.is_demo:
            # Not a warning. Either both unlocks are set by the owner, or this
            # object does not exist.
            assert_live_unlocked(secrets)
            log.critical("LIVE ACCOUNT %s — both owner unlocks present", account.login)
        self._account_login = account.login
        self._filling = self._resolve_filling_mode()

    @property
    def mt5(self) -> Any:
        return self._session.mt5

    def _resolve_filling_mode(self) -> int:
        """Ask the symbol which fillings it accepts rather than assuming.

        `filling_mode` is a bitmask: 1 = FOK allowed, 2 = IOC allowed. Sending
        an unsupported mode is rejected at the server with an unhelpful code.
        """
        info = self.mt5.symbol_info(self._spec.name)
        allowed = int(getattr(info, "filling_mode", 0))
        if allowed & 2:
            return self.mt5.ORDER_FILLING_IOC
        if allowed & 1:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN

    # -- reads --------------------------------------------------------------

    def open_positions(self, magics: frozenset[int] = OUR_MAGICS) -> list[Position]:
        """Positions as the broker reports them — the source of truth after any
        restart. Local state is a hint; this is fact."""
        raw = self.mt5.positions_get() or []
        out: list[Position] = []
        for item in raw:
            magic = int(getattr(item, "magic", 0))
            if magic not in magics:
                continue   # someone else's position
            out.append(
                Position(
                    ticket=int(item.ticket),
                    symbol=str(item.symbol),
                    direction=Direction.BULLISH if int(item.type) == 0 else Direction.BEARISH,
                    lots=float(item.volume),
                    entry=float(item.price_open),
                    stop_loss=float(getattr(item, "sl", 0.0)),
                    take_profit=float(getattr(item, "tp", 0.0)),
                    magic=magic,
                    opened_at=datetime.fromtimestamp(int(item.time), tz=timezone.utc),
                    floating_pnl=float(getattr(item, "profit", 0.0)),
                )
            )
        return out

    def pending_orders(self, magics: frozenset[int] = OUR_MAGICS) -> list[PendingOrder]:
        raw = self.mt5.orders_get() or []
        return [
            PendingOrder(
                ticket=int(o.ticket),
                symbol=str(o.symbol),
                direction=Direction.BULLISH if int(o.type) in (2, 4, 6) else Direction.BEARISH,
                lots=float(o.volume_current),
                price=float(o.price_open),
                magic=int(getattr(o, "magic", 0)),
            )
            for o in raw
            if int(getattr(o, "magic", 0)) in magics
        ]

    def realised_pnl(self, position_id: int, lookback_hours: int = 48) -> float:
        """Net of the closing deal(s): profit + commission + swap.

        Read from the broker's deal history rather than computed from prices —
        commission and swap are not derivable, and on this account commission is
        charged entirely on the entry deal.
        """
        deals = self.mt5.history_deals_get(
            datetime.now(timezone.utc) - timedelta(hours=lookback_hours),
            datetime.now(timezone.utc) + timedelta(hours=2),
        ) or []
        return sum(
            float(d.profit) + float(d.commission) + float(d.swap)
            for d in deals
            if int(getattr(d, "position_id", 0)) == position_id
        )

    def exit_reason(self, position_id: int, lookback_hours: int = 48) -> str:
        deals = self.mt5.history_deals_get(
            datetime.now(timezone.utc) - timedelta(hours=lookback_hours),
            datetime.now(timezone.utc) + timedelta(hours=2),
        ) or []
        for d in deals:
            if int(getattr(d, "position_id", 0)) == position_id and int(d.entry) == 1:
                comment = str(getattr(d, "comment", "")).lower()
                if "tp" in comment:
                    return "tp"
                if "sl" in comment:
                    return "sl"
                return "closed"
        return "unknown"

    # -- writes -------------------------------------------------------------

    def _round(self, price: float) -> float:
        return round(price, self._spec.digits)

    def _order_type(self, request: OrderRequest) -> int:
        if not request.is_pending:
            return (self.mt5.ORDER_TYPE_BUY if request.direction is Direction.BULLISH
                    else self.mt5.ORDER_TYPE_SELL)
        return (self.mt5.ORDER_TYPE_BUY_LIMIT if request.direction is Direction.BULLISH
                else self.mt5.ORDER_TYPE_SELL_LIMIT)

    def _build(self, request: OrderRequest) -> dict[str, Any]:
        if request.is_pending:
            price = request.entry
            action = self.mt5.TRADE_ACTION_PENDING
        else:
            bid, ask = self._session.tick(request.symbol)
            price = ask if request.direction is Direction.BULLISH else bid
            action = self.mt5.TRADE_ACTION_DEAL

        payload: dict[str, Any] = {
            "action": action,
            "symbol": request.symbol,
            "volume": request.lots,
            "type": self._order_type(request),
            "price": self._round(float(price)),
            "sl": self._round(request.stop_loss),
            "tp": self._round(request.take_profit),
            "deviation": 20,
            "magic": request.magic,
            "comment": request.comment[:31],   # MT5 truncates silently past 31
            "type_filling": self._filling,
        }
        if request.is_pending and request.expiry is not None:
            payload["type_time"] = self.mt5.ORDER_TIME_SPECIFIED
            payload["expiration"] = int(request.expiry.timestamp())
        else:
            payload["type_time"] = self.mt5.ORDER_TIME_GTC
        return payload

    def already_working(self, magic: int) -> bool:
        """Idempotency check. Called before every send, and again before any
        retry — never retry a send without re-reading the account first."""
        if any(p.magic == magic for p in self.open_positions()):
            return True
        return any(o.magic == magic for o in self.pending_orders())

    def place(self, request: OrderRequest) -> OrderResult:
        if self.already_working(request.magic):
            return OrderResult(
                False, -1,
                f"an order or position with magic {request.magic} already exists — "
                f"refusing to duplicate",
            )

        payload = self._build(request)

        # Dry run first. The broker validates margin, stops distance and
        # filling mode without executing, which turns a rejected order into a
        # log line instead of a half-open position.
        check = self.mt5.order_check(payload)
        if check is None:
            return OrderResult(False, -1, f"order_check returned None: {self.mt5.last_error()}")
        if int(check.retcode) != 0:
            return OrderResult(False, int(check.retcode),
                               f"rejected at validation: {check.comment}")
        log.info("validated: margin %.2f, free after %.2f", check.margin, check.margin_free)

        result = self.mt5.order_send(payload)
        if result is None:
            return OrderResult(False, -1, f"order_send returned None: {self.mt5.last_error()}")

        retcode = int(result.retcode)
        ok = retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED)
        return OrderResult(
            ok=ok,
            retcode=retcode,
            message=str(result.comment),
            ticket=int(result.order) or None,
            fill_price=float(result.price) or None,
            volume=float(result.volume),
        )

    def close(self, ticket: int) -> OrderResult:
        """Close one of ours at market. Refuses anything that is not ours."""
        position = next((p for p in self.open_positions() if p.ticket == ticket), None)
        if position is None:
            return OrderResult(False, -1, f"position {ticket} is not open, or is not ours")

        bid, ask = self._session.tick(position.symbol)
        payload = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.lots,
            "position": ticket,
            "type": (self.mt5.ORDER_TYPE_SELL if position.direction is Direction.BULLISH
                     else self.mt5.ORDER_TYPE_BUY),
            "price": bid if position.direction is Direction.BULLISH else ask,
            "deviation": 20,
            "magic": position.magic,
            "comment": "gold2.6 close",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling,
        }
        result = self.mt5.order_send(payload)
        if result is None:
            return OrderResult(False, -1, f"close failed: {self.mt5.last_error()}")
        retcode = int(result.retcode)
        return OrderResult(
            ok=retcode == self.mt5.TRADE_RETCODE_DONE,
            retcode=retcode,
            message=str(result.comment),
            ticket=ticket,
            fill_price=float(result.price) or None,
        )

    def cancel(self, ticket: int) -> OrderResult:
        """Remove a resting order. A stale 2.6 level sitting through a regime
        change is a live grenade, so the daemon cancels aggressively."""
        if not any(o.ticket == ticket for o in self.pending_orders()):
            return OrderResult(False, -1, f"pending order {ticket} is not ours or is gone")
        result = self.mt5.order_send(
            {"action": self.mt5.TRADE_ACTION_REMOVE, "order": ticket}
        )
        if result is None:
            return OrderResult(False, -1, f"cancel failed: {self.mt5.last_error()}")
        retcode = int(result.retcode)
        return OrderResult(ok=retcode == self.mt5.TRADE_RETCODE_DONE, retcode=retcode,
                           message=str(result.comment), ticket=ticket)

    def cancel_all_ours(self) -> int:
        cancelled = 0
        for order in self.pending_orders():
            if self.cancel(order.ticket).ok:
                cancelled += 1
        return cancelled
