"""Async Telegram alerts.

The brief asked for "asynchronous requests" but specified the `requests`
library, which is synchronous. In an asyncio daemon a blocking HTTP call stops
the event loop, so a slow Telegram response would delay position management —
alerts must never be able to do that. This uses `httpx.AsyncClient`.

The other rule here: **a dead notification channel never blocks trading.**
Every failure is logged and swallowed. A bot that refuses to manage an open
position because Telegram is down has its priorities backwards.
"""
from __future__ import annotations

import asyncio
import html
from typing import Any

import httpx

import logger as logging_setup

log = logging_setup.get("telegram")

API = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client: httpx.AsyncClient | None = None
        if not self.enabled:
            log.warning(
                "Telegram is not configured (token=%s, chat_id=%s) — alerts are "
                "disabled. Trading is unaffected.",
                "set" if token else "unset", "set" if chat_id else "unset",
            )

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def __aenter__(self) -> TelegramNotifier:
        if self.enabled:
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, text: str) -> bool:
        """Best effort. Returns whether it went out; never raises."""
        if not self.enabled or self._client is None:
            return False
        try:
            response = await self._client.post(
                f"{API}/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code != 200:
                log.warning("Telegram rejected the message: %s %s",
                            response.status_code, response.text[:200])
                return False
            return True
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            log.warning("Telegram send failed (%s) — continuing", exc)
            return False

    # -- message templates --------------------------------------------------

    async def trade_opened(self, **kw: Any) -> None:
        await self.send(
            f"🟢 <b>OPENED</b>  {html.escape(str(kw['direction'])).upper()} "
            f"{kw['lots']} {html.escape(str(kw['symbol']))}\n"
            f"<code>timeframe {kw['timeframe']}\n"
            f"entry      {kw['entry']:.3f}\n"
            f"stop       {kw['stop_loss']:.3f}   (-${kw['risk_usd']:.2f})\n"
            f"target     {kw['take_profit']:.3f}   (+${kw['reward_usd']:.2f})\n"
            f"leg        {kw['leg']:.3f}\n"
            f"ticket     {kw['ticket']}</code>\n"
            f"<i>needs {kw['breakeven_winrate_pct']:.0f}% win rate to break even</i>"
        )

    async def trade_closed(self, **kw: Any) -> None:
        icon = "✅" if kw["realised_usd"] > 0 else "❌"
        await self.send(
            f"{icon} <b>CLOSED</b>  {html.escape(str(kw['exit_reason'])).upper()}\n"
            f"<code>ticket   {kw['ticket']}\n"
            f"exit     {kw['exit_price']:.3f}\n"
            f"realised {kw['realised_usd']:+.2f} USD  ({kw['r_multiple']:+.2f} R)\n"
            f"equity   {kw['equity']:,.2f}</code>"
        )

    async def daily_summary(self, summary: str, equity: float) -> None:
        await self.send(f"📊 <b>DAILY</b>\n<code>{html.escape(summary)}\n"
                        f"equity {equity:,.2f}</code>")

    async def system_error(self, what: str) -> None:
        await self.send(f"⚠️ <b>SYSTEM</b>\n<code>{html.escape(what)[:900]}</code>")

    async def halted(self, reason: str) -> None:
        await self.send(f"🛑 <b>HALTED</b>\n<code>{html.escape(reason)[:900]}</code>\n"
                        f"<i>the bot will not open new positions until this clears</i>")


async def discover_chat_id(token: str) -> list[tuple[str, str]]:
    """Read chat IDs from recent updates.

    Saves the owner hunting for it: message the bot once, run
    `python telegram_bot.py`, and the id is printed.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(f"{API}/bot{token}/getUpdates")
        response.raise_for_status()
        found: list[tuple[str, str]] = []
        for update in response.json().get("result", []):
            chat = (update.get("message") or update.get("channel_post") or {}).get("chat")
            if chat:
                label = chat.get("title") or chat.get("username") or chat.get("first_name", "?")
                pair = (str(chat["id"]), str(label))
                if pair not in found:
                    found.append(pair)
        return found


if __name__ == "__main__":
    from config import load_secrets

    secrets = load_secrets()
    if not secrets.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    chats = asyncio.run(discover_chat_id(secrets.telegram_token))
    if not chats:
        raise SystemExit(
            "No chats found. Send your bot a message first, then run this again.\n"
            "Telegram only retains recent updates, so do it in the next few minutes."
        )
    print("Add one of these to .env as TELEGRAM_CHAT_ID:\n")
    for chat_id, label in chats:
        print(f"  TELEGRAM_CHAT_ID={chat_id}    ({label})")
