"""Trade journal.

Separate from `logger.py` on purpose. The log is prose for a human reading a
post-mortem; the journal is structured rows for arithmetic. Mixing them gives
you a file that is bad at both.

Every row carries `breakeven_winrate_pct`. That single column is what turns
"the bot won 8 of its last 10" into a statement about whether it is making
money, which is a different question and usually has a different answer.
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import logger as logging_setup

log = logging_setup.get("journal")


@dataclass
class JournalRow:
    opened_utc: str
    timeframe: str
    magic: int
    account: int
    symbol: str
    direction: str
    ticket: int
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    risk_usd: float
    reward_usd: float
    breakeven_winrate_pct: float
    leg: float
    swing_high: float
    swing_low: float
    spread_points_at_entry: float
    equity_at_entry: float
    # Filled in when the position closes.
    closed_utc: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    realised_usd: float = 0.0
    r_multiple: float = 0.0


class Journal:
    """Append-on-open, update-on-close, backed by a CSV."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def _fieldnames(self) -> list[str]:
        return [f.name for f in fields(JournalRow)]

    def read(self) -> list[dict[str, str]]:
        if not self._path.exists():
            return []
        with self._path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _write_all(self, rows: list[dict[str, str]]) -> None:
        with self._path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self._fieldnames())
            writer.writeheader()
            writer.writerows(rows)

    def record_open(self, row: JournalRow) -> None:
        rows = self.read()
        rows.append({k: str(v) for k, v in asdict(row).items()})
        self._write_all(rows)
        log.info("journalled open: ticket %s %s %s lots %.2f",
                 row.ticket, row.direction, row.symbol, row.lots)

    def record_close(
        self,
        ticket: int,
        *,
        exit_price: float,
        exit_reason: str,
        realised_usd: float,
    ) -> None:
        rows = self.read()
        found = False
        for row in rows:
            if row.get("ticket") == str(ticket) and not row.get("closed_utc"):
                risk = float(row.get("risk_usd") or 0)
                row["closed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                row["exit_price"] = str(exit_price)
                row["exit_reason"] = exit_reason
                row["realised_usd"] = f"{realised_usd:.2f}"
                row["r_multiple"] = f"{realised_usd / risk:.3f}" if risk else "0"
                found = True
                break
        if not found:
            log.warning("no open journal row for ticket %s — close not recorded", ticket)
            return
        self._write_all(rows)
        log.info("journalled close: ticket %s %s %+.2f USD", ticket, exit_reason, realised_usd)

    def summary(self) -> str:
        """The running verdict, in the form that actually settles the question."""
        rows = [r for r in self.read() if r.get("realised_usd")]
        if not rows:
            return "journal: no closed trades yet"
        pnls = [float(r["realised_usd"]) for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        losses = len(pnls) - wins
        avg_win = gross_win / wins if wins else 0.0
        avg_loss = gross_loss / losses if losses else 0.0
        breakeven = avg_loss / (avg_win + avg_loss) * 100 if (avg_win + avg_loss) else 0.0
        return (
            f"journal: {len(pnls)} closed  "
            f"win rate {wins / len(pnls) * 100:.1f}%  "
            f"needs {breakeven:.1f}% to break even  "
            f"PF {gross_win / gross_loss if gross_loss else float('inf'):.2f}  "
            f"net {sum(pnls):+.2f} USD"
        )
