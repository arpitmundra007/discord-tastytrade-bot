"""
Parses signals in this format (one per Discord message/embed):

    Buy To Open
    LOTTO SIZE / SMALL
    SPY 731P  0DTE $1.7

Extend the regex / add new branches here as you see more signal formats
(verticals, explicit expiry dates, etc). Keep this module's only job as
"raw text in -> structured ParsedSignal out (or None if unparseable)".
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum


class Action(str, Enum):
    BUY_TO_OPEN = "Buy to Open"
    SELL_TO_CLOSE = "Sell to Close"
    SELL_TO_OPEN = "Sell to Open"
    BUY_TO_CLOSE = "Buy to Close"


_ACTION_MAP = {
    "buy to open": Action.BUY_TO_OPEN,
    "sell to close": Action.SELL_TO_CLOSE,
    "sell to open": Action.SELL_TO_OPEN,
    "buy to close": Action.BUY_TO_CLOSE,
}

# SYMBOL STRIKE[C|P]  NDTE $PRICE   (extra spaces tolerated)
_LEG_RE = re.compile(
    r"([A-Z]{1,6})\s+(\d+(?:\.\d+)?)\s*([CP])\s+(\d+)\s*DTE\s+\$?\s*([\d.]+)",
    re.IGNORECASE,
)


@dataclass
class ParsedSignal:
    action: Action
    symbol: str
    strike: float
    option_type: str  # "C" or "P"
    expiration: date
    signal_price: float
    size_tag: str  # e.g. "SMALL", raw last token from the size line
    raw_text: str


def _next_trading_day_offset(start: date, dte: int) -> date:
    """
    Naive calendar-day walk skipping weekends. Does NOT account for market
    holidays - fine for an MVP, but swap in a real trading-calendar lib
    (e.g. `pandas_market_calendars`) before relying on this for anything
    that spans a holiday.
    """
    d = start
    remaining = dte
    while remaining > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            remaining -= 1
    # if dte == 0, just make sure "today" is a weekday; if not, don't
    # silently trade - let this bubble up as an unparseable/invalid signal
    return d


def parse_signal(raw_text: str, today: date | None = None) -> ParsedSignal | None:
    today = today or date.today()
    lines = [ln.strip() for ln in raw_text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    action = _ACTION_MAP.get(lines[0].strip().lower())
    if action is None:
        return None

    size_line = lines[1]
    size_tag = size_line.split("/")[-1].strip().upper() if "/" in size_line else size_line.strip().upper()

    leg_match = _LEG_RE.search(lines[2])
    if not leg_match:
        return None

    symbol, strike_str, opt_type, dte_str, price_str = leg_match.groups()
    dte = int(dte_str)
    expiration = today if dte == 0 else _next_trading_day_offset(today, dte)

    return ParsedSignal(
        action=action,
        symbol=symbol.upper(),
        strike=float(strike_str),
        option_type=opt_type.upper(),
        expiration=expiration,
        signal_price=float(price_str),
        size_tag=size_tag,
        raw_text=raw_text,
    )
