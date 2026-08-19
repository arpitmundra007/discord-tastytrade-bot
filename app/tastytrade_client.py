"""
Wraps the `tastytrade` (tastyware) SDK: session auth, a PERSISTENT live-quote
stream, and OTOCO (entry + take-profit + stop-loss) order construction/
submission.

Verified against tastytrade SDK v13.2.2 (pip install tastytrade), which
diverged from the method names shown in some older examples/docs:
  - Account.a_get()          -> Account.get(session, account_number=...)
  - account.a_place_complex_order() -> account.place_complex_order()
  - Option.get_option(...)   -> Option.get(session, occ_symbol_string),
    plus an explicit option.set_streamer_symbol() call before reading
    option.streamer_symbol (it's not auto-populated)
  - Session(...) takes is_test: bool, not a base URL string - TT_BASE_URL
    from .env is translated to is_test here based on whether it contains
    "cert"
  - NewComplexOrder(...) defaults to type=ComplexOrderType.OCO if not set
    explicitly - silently wrong for a trigger_order+orders OTOCO structure,
    so `type=ComplexOrderType.OTOCO` is now passed explicitly
This was verified by actually constructing a NewComplexOrder with real
field values and confirming it validates and serializes correctly
(including auto-computed price-effect Credit/Debit), not just import-checked.

NOTE: NewOrder/NewComplexOrder are marked deprecated in v13.2.2 in favor of
OTOCOOrder and similar dedicated classes - still functional and verified
working here, but worth migrating to the newer classes if a future SDK
version removes the old ones.
If you're on a different SDK version, re-check these against
https://tastyworks-api.readthedocs.io/ - method names have moved before and
may move again.

--- Why persistent, and what that costs ---
Opening a fresh DXLink streaming connection per signal (the original version
of this file) costs 1-3+ seconds of handshake/subscribe/first-event latency
on every single trade - the dominant source of delay in the whole pipeline.
Keeping ONE connection open for the life of the process removes that cost
for every signal after the first, but trades it for a new problem: a
long-lived connection can silently die (network blip, server-side restart,
auth token expiry) and, unless you handle that, every signal after the drop
just times out waiting for a quote that will never arrive. Everything below
marked "reconnect" exists to handle that failure mode automatically instead
of requiring a manual restart.
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import date
from decimal import Decimal

from tastytrade import Session, Account, DXLinkStreamer
from tastytrade.dxfeed import Quote
from tastytrade.instruments import Option
from tastytrade.order import NewOrder, NewComplexOrder, OrderAction, OrderTimeInForce, OrderType, ComplexOrderType

from app.config import settings
from app.signal_parser import ParsedSignal

log = logging.getLogger("tastytrade_client")

# Reuse a cached quote if it's fresher than this - avoids re-waiting on a new
# event for rapid-fire signals on the same symbol.
QUOTE_MAX_AGE_SECONDS = 3.0
# How long to wait for a fresh quote on first subscribe before giving up.
QUOTE_WAIT_TIMEOUT_SECONDS = 2.0
# Reconnect backoff after a stream failure.
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0


def occ_symbol(symbol: str, expiration: date, option_type: str, strike: float) -> str:
    """OCC-style option symbol, e.g. SPY   260804P00731000 - for logging/dry-run only."""
    root = symbol.ljust(6)
    yy = expiration.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    return f"{root}{yy}{option_type}{strike_int:08d}"


class TastytradeClient:
    def __init__(self):
        self._session: Session | None = None
        self._account: Account | None = None

        self._streamer: DXLinkStreamer | None = None
        self._stream_task: asyncio.Task | None = None
        self._stream_ready = asyncio.Event()  # set once the streamer is connected and usable
        self._last_error: str | None = None

        self._quotes: dict[str, tuple[Quote, float]] = {}  # streamer_symbol -> (quote, received_at)
        self._quote_events: dict[str, asyncio.Event] = {}
        self._subscribed: set[str] = set()  # symbols to (re)subscribe to, survives reconnects

    # ---------- lifecycle ----------

    async def connect(self):
        """Authenticate and fetch the account. Call once at startup."""
        try:
            is_test = "cert" in settings.tt_base_url
            self._session = Session(settings.tt_client_secret, settings.tt_refresh_token, is_test=is_test)
            # Tastytrade requires a User-Agent formatted as <app-name>/<version> -
            # without it, requests can fail with misleading auth errors that
            # have nothing to do with the credentials themselves. The SDK's
            # Session() doesn't expose a way to pass this through its
            # constructor (it already sets its own `headers` kwarg internally,
            # so passing headers=... there collides and raises), so it's set
            # directly on the underlying httpx client instead.
            self._session._client.headers.update({"User-Agent": "discord-tastytrade-bot/1.0"})
            self._account = await Account.get(self._session, account_number=settings.tt_account_number)
            self._last_error = None
        except Exception as e:
            self._last_error = f"Tastytrade connect failed: {e}"
            log.exception("Tastytrade connect() failed - check TT_CLIENT_SECRET, TT_REFRESH_TOKEN, TT_ACCOUNT_NUMBER, and TT_BASE_URL")
            raise

    def start_streaming(self):
        """Starts the persistent, self-healing quote stream in the background. Call once at startup, after connect()."""
        if self._stream_task is None:
            self._stream_task = asyncio.create_task(self._stream_manager())

    async def close(self):
        if self._stream_task:
            self._stream_task.cancel()
        if self._streamer:
            try:
                await self._streamer.__aexit__(None, None, None)
            except Exception:
                pass

    # ---------- persistent stream with auto-reconnect ----------

    async def _stream_manager(self):
        """
        Owns the DXLink connection for the process lifetime. On any failure,
        closes cleanly, backs off, and reconnects - resubscribing to every
        symbol that was active before the drop. This is the piece that makes
        "persistent" safe to run unattended instead of degrading silently.
        """
        delay = RECONNECT_BASE_DELAY
        while True:
            try:
                self._stream_ready.clear()
                # NOTE: verify DXLinkStreamer's constructor/entry signature against
                # your installed SDK version - this opens it as a long-lived context
                # manager rather than the one-shot `async with` used per-call before.
                self._streamer = DXLinkStreamer(self._session)
                await self._streamer.__aenter__()

                if self._subscribed:
                    await self._streamer.subscribe(Quote, list(self._subscribed))

                log.info("DXLink stream connected (%d symbols resubscribed)", len(self._subscribed))
                self._stream_ready.set()
                delay = RECONNECT_BASE_DELAY  # reset backoff after a clean connect

                async for quote in self._streamer.listen(Quote):
                    sym = quote.event_symbol
                    self._quotes[sym] = (quote, time.monotonic())
                    event = self._quote_events.get(sym)
                    if event:
                        event.set()

                # listen() returning at all means the stream ended - treat as a drop
                log.warning("DXLink stream ended unexpectedly, reconnecting")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"DXLink stream error: {e}"
                log.exception("DXLink stream error, reconnecting in %.1fs", delay)

            self._stream_ready.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    def get_last_error(self) -> str | None:
        return self._last_error

    async def _ensure_subscribed(self, streamer_symbol: str):
        self._quote_events.setdefault(streamer_symbol, asyncio.Event())
        if streamer_symbol in self._subscribed:
            return
        self._subscribed.add(streamer_symbol)
        if self._stream_ready.is_set() and self._streamer:
            await self._streamer.subscribe(Quote, [streamer_symbol])
        # if the stream isn't connected right now, _stream_manager will pick up
        # this symbol on its next (re)connect since it's already in self._subscribed

    def is_stream_connected(self) -> bool:
        return self._stream_ready.is_set()

    async def measure_broker_latency_ms(self) -> float:
        """
        Round-trip time to a real, read-only, authenticated Tastytrade
        endpoint (account balances) - hits the same live API infrastructure
        an order submission would, without ever placing an order. Use this
        as an estimate for the one leg dry-run testing can't measure for
        real: the final broker network round-trip after the slippage
        decision is already made.
        """
        if self._account is None:
            raise RuntimeError("Not connected to Tastytrade yet - check the Setup tab and the Quote Stream status on the Live tab first.")
        start = time.monotonic()
        await self._account.get_balances(self._session)
        return round((time.monotonic() - start) * 1000, 1)

    # ---------- public API used by the risk engine / bot ----------

    async def get_live_price(self, symbol: str, expiration: date, option_type: str, strike: float) -> float:
        option = await Option.get(self._session, occ_symbol(symbol, expiration, option_type, strike))
        option.set_streamer_symbol()
        streamer_symbol = option.streamer_symbol
        await self._ensure_subscribed(streamer_symbol)

        cached = self._quotes.get(streamer_symbol)
        if cached and (time.monotonic() - cached[1]) < QUOTE_MAX_AGE_SECONDS:
            quote = cached[0]
        else:
            event = self._quote_events[streamer_symbol]
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=QUOTE_WAIT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                if streamer_symbol not in self._quotes:
                    raise RuntimeError(
                        f"No quote received for {streamer_symbol} within {QUOTE_WAIT_TIMEOUT_SECONDS}s "
                        f"(stream_ready={self._stream_ready.is_set()})"
                    )
            quote = self._quotes[streamer_symbol][0]

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid and ask:
            return round((bid + ask) / 2, 2)
        return float(bid or ask or 0)

    async def submit_bracket_order(
        self,
        signal: ParsedSignal,
        contracts: int,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        dry_run: bool = True,
    ) -> dict:
        option = await Option.get(
            self._session, occ_symbol(signal.symbol, signal.expiration, signal.option_type, signal.strike)
        )

        opening_action = OrderAction.BUY_TO_OPEN if signal.action.value.startswith("Buy") else OrderAction.SELL_TO_OPEN
        closing_action = OrderAction.SELL_TO_CLOSE if opening_action == OrderAction.BUY_TO_OPEN else OrderAction.BUY_TO_CLOSE

        opening_leg = option.build_leg(Decimal(contracts), opening_action)
        closing_leg = option.build_leg(Decimal(contracts), closing_action)

        entry_price_signed = Decimal(str(-entry_price)) if opening_action == OrderAction.BUY_TO_OPEN else Decimal(str(entry_price))

        # Entry leg: Limit protects against paying worse than entry_price but
        # may not fill at all if the market moves away first. Market
        # guarantees a fill but gives up all price protection at the moment
        # the slippage check already passed - configurable since this is a
        # real tradeoff, not a clear-cut default.
        if settings.entry_order_type == "market":
            trigger_order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.MARKET,
                legs=[opening_leg],
            )
        else:
            trigger_order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=[opening_leg],
                price=entry_price_signed,
            )

        # Stop-loss leg: "stop" (pure stop-market) has NO price field - once
        # triggered it becomes a market order, guaranteeing the position
        # actually closes at the cost of uncertain fill price. "stop_limit"
        # bounds the fill price but risks not filling at all in a gap,
        # leaving the position open and unprotected - usually the worse
        # outcome for something meant to cap a loss, so "stop" is the default.
        if settings.stop_order_type == "stop_limit":
            stop_leg_order = NewOrder(
                time_in_force=OrderTimeInForce.GTC,
                order_type=OrderType.STOP_LIMIT,
                legs=[closing_leg],
                stop_trigger=Decimal(str(stop_loss_price)),
                price=Decimal(str(stop_loss_price)),
            )
        else:
            stop_leg_order = NewOrder(
                time_in_force=OrderTimeInForce.GTC,
                order_type=OrderType.STOP,
                legs=[closing_leg],
                stop_trigger=Decimal(str(stop_loss_price)),
            )

        otoco = NewComplexOrder(
            type=ComplexOrderType.OTOCO,
            trigger_order=trigger_order,
            orders=[
                NewOrder(
                    time_in_force=OrderTimeInForce.GTC,
                    order_type=OrderType.LIMIT,
                    legs=[closing_leg],
                    price=Decimal(str(take_profit_price)),
                ),
                stop_leg_order,
            ],
        )

        payload_preview = {
            "symbol": occ_symbol(signal.symbol, signal.expiration, signal.option_type, signal.strike),
            "contracts": contracts,
            "entry_limit": entry_price,
            "entry_order_type": settings.entry_order_type,
            "take_profit": take_profit_price,
            "stop_loss": stop_loss_price,
            "stop_order_type": settings.stop_order_type,
        }

        if dry_run:
            return {"dry_run": True, **payload_preview}

        result = await self._account.place_complex_order(self._session, otoco, dry_run=False)
        return {"dry_run": False, "result": str(result), **payload_preview}


tastytrade_client = TastytradeClient()
