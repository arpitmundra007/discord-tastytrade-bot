from __future__ import annotations
"""
LLM-based fallback parser: catches signals the regex parser (signal_parser.py)
can't make sense of - typos, abbreviations, different traders' formatting
styles, and instrument types the regex was never built for (futures, etc).

IMPORTANT SCOPE BOUNDARY: this module only EXTRACTS structured meaning from
text. It does not decide whether to trade - that's still entirely the risk
engine's job, gated by whether the extracted instrument_type/action is one
this app actually knows how to execute. As of this module's creation, that's
options only. A perfectly-extracted futures signal is still not tradable
here, because tastytrade_client.py has no futures order-construction path
yet - see get_execution_gap() below, which is what callers should check
before ever handing a parsed signal to the risk engine.
"""
import logging
import re

from anthropic import AsyncAnthropic

from app.config import settings
from app.signal_parser import Action, ParsedSignal, _next_trading_day_offset

log = logging.getLogger("llm_parser")

MODEL = "claude-sonnet-4-5"

EXTRACTION_TOOL = {
    "name": "extract_trading_signal",
    "description": "Extract structured trading signal fields from a trader's Discord message, however it's formatted or abbreviated.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_type": {
                "type": "string",
                "enum": ["option", "future", "equity", "unknown"],
                "description": "What kind of instrument this signal refers to.",
            },
            "action": {
                "type": "string",
                "enum": [
                    "buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close",
                    "update_stop_loss", "update_take_profit", "unknown",
                ],
                "description": "Long entries are buy_to_open. Short entries (e.g. 'short', 'shrt', 'sell short') are sell_to_open. A message that ONLY updates a stop-loss or take-profit for an already-open position (no new entry) uses update_stop_loss/update_take_profit.",
            },
            "symbol": {
                "type": ["string", "null"],
                "description": "The ticker/root symbol, e.g. SPY, MNQ. Null if the message doesn't name one (e.g. a bare stop-loss update).",
            },
            "quantity": {"type": ["number", "null"]},
            "reference_price": {
                "type": ["number", "null"],
                "description": "The entry/signal price if given (e.g. the '@ 30122' or the option premium '$1.7').",
            },
            "strike": {"type": ["number", "null"], "description": "Options only."},
            "option_type": {"type": ["string", "null"], "enum": ["call", "put", None]},
            "expiration_hint": {
                "type": ["string", "null"],
                "description": "Raw expiration text if present, e.g. '0DTE' - options only, do not try to resolve to a date.",
            },
            "stop_loss": {"type": ["number", "null"]},
            "take_profit": {"type": ["number", "null"]},
            "risk_tag": {
                "type": ["string", "null"],
                "description": "Any size/risk label present, e.g. 'HIGH RISK', 'LOTTO', 'SMALL'.",
            },
            "confidence": {
                "type": "number",
                "description": "0-1, your confidence that this extraction is correct and complete enough to act on.",
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence on how you interpreted it - especially any abbreviation/typo you resolved.",
            },
        },
        "required": ["instrument_type", "action", "confidence", "reasoning"],
    },
}

SYSTEM_PROMPT = """You extract structured trading signal data from short, often terse or \
abbreviated Discord messages posted by options/futures traders. These messages use heavy \
shorthand, inconsistent formatting (bullets, dots, pipes), and frequent typos - e.g. "SHRT" \
means "short", "SL" means stop-loss, "TP" means take-profit, "MNQ" is a futures symbol, "0DTE" \
means zero-days-to-expiration. Always call extract_trading_signal with your best interpretation. \
If a message only updates a stop-loss/take-profit with no new position details, set action \
accordingly and leave entry fields null - do not guess a symbol or quantity that isn't stated."""


class LLMParsedSignal:
    def __init__(self, data: dict, raw_text: str):
        self.instrument_type = data.get("instrument_type", "unknown")
        self.action = data.get("action", "unknown")
        self.symbol = data.get("symbol")
        self.quantity = data.get("quantity")
        self.reference_price = data.get("reference_price")
        self.strike = data.get("strike")
        self.option_type = data.get("option_type")
        self.expiration_hint = data.get("expiration_hint")
        self.stop_loss = data.get("stop_loss")
        self.take_profit = data.get("take_profit")
        self.risk_tag = data.get("risk_tag")
        self.confidence = float(data.get("confidence", 0))
        self.reasoning = data.get("reasoning", "")
        self.raw_text = raw_text

    def __repr__(self):
        return (
            f"LLMParsedSignal(instrument_type={self.instrument_type!r}, action={self.action!r}, "
            f"symbol={self.symbol!r}, qty={self.quantity!r}, confidence={self.confidence:.2f})"
        )

    def get_execution_gap(self) -> str | None:
        """
        Returns a human-readable reason this signal CANNOT be executed by
        this app yet, or None if it's clear to hand to the risk engine.
        This is the actual safety boundary - checked before any parsed
        signal reaches order-placement code.
        """
        if self.confidence < 0.6:
            return f"Low extraction confidence ({self.confidence:.0%}) - not acting on this without a clearer signal."
        if self.action in ("update_stop_loss", "update_take_profit"):
            return "This looks like a stop-loss/take-profit update for an already-open position - this app doesn't yet track open positions across messages, so it can't safely match this to a trade."
        if self.action == "unknown":
            return "Couldn't determine buy/sell/short intent from this message."
        if self.instrument_type == "future":
            return "Futures execution isn't implemented yet - this app only places options orders currently."
        if self.instrument_type == "equity":
            return "Equity execution isn't implemented yet - this app only places options orders currently."
        if self.instrument_type == "unknown":
            return "Couldn't determine instrument type from this message."
        if self.instrument_type == "option" and self.action in ("buy_to_open", "sell_to_open") and (
            self.strike is None or self.option_type is None or self.symbol is None
        ):
            return "Missing strike, option type, or symbol - not enough to build an options order."
        if self.instrument_type == "option" and self.action in ("buy_to_open", "sell_to_open"):
            if _parse_dte(self.expiration_hint) is None:
                return f"Couldn't resolve an expiration from {self.expiration_hint!r} - not enough to build an options order."
        return None


def _parse_dte(expiration_hint: str | None) -> int | None:
    """'0DTE' -> 0, '1 DTE' -> 1, anything else -> None (unresolvable)."""
    if not expiration_hint:
        return None
    m = re.search(r"(\d+)\s*DTE", expiration_hint, re.IGNORECASE)
    return int(m.group(1)) if m else None


def to_parsed_signal(llm_sig: "LLMParsedSignal", today=None) -> ParsedSignal | None:
    """
    Converts a validated (get_execution_gap() is None) LLM signal into the
    exact same ParsedSignal type signal_parser.py produces, so it flows
    through identical risk-engine and order-placement code - not a parallel,
    separately-tested path. Returns None if conversion still isn't possible
    (defensive - get_execution_gap should have already caught this).
    """
    from datetime import date as date_cls

    action_map = {
        "buy_to_open": Action.BUY_TO_OPEN,
        "sell_to_open": Action.SELL_TO_OPEN,
        "buy_to_close": Action.BUY_TO_CLOSE,
        "sell_to_close": Action.SELL_TO_CLOSE,
    }
    action = action_map.get(llm_sig.action)
    dte = _parse_dte(llm_sig.expiration_hint)
    if (
        action is None
        or llm_sig.symbol is None
        or llm_sig.strike is None
        or llm_sig.option_type is None
        or llm_sig.reference_price is None
        or dte is None
    ):
        return None

    today = today or date_cls.today()
    expiration = today if dte == 0 else _next_trading_day_offset(today, dte)

    return ParsedSignal(
        action=action,
        symbol=llm_sig.symbol.upper(),
        strike=float(llm_sig.strike),
        option_type=llm_sig.option_type[0].upper(),  # "call"/"put" -> "C"/"P"
        expiration=expiration,
        signal_price=float(llm_sig.reference_price),
        size_tag=(llm_sig.risk_tag or "").upper(),
        raw_text=llm_sig.raw_text,
    )


async def parse_signal_with_llm(raw_text: str) -> LLMParsedSignal | None:
    if not settings.anthropic_api_key:
        log.debug("No ANTHROPIC_API_KEY configured - skipping LLM fallback parse")
        return None

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": raw_text}],
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "extract_trading_signal"},
        )
    except Exception as e:
        log.warning("LLM parse request failed: %s", e)
        return None

    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_trading_signal":
            try:
                return LLMParsedSignal(block.input, raw_text)
            except Exception as e:
                log.warning("LLM returned unparseable tool input: %s", e)
                return None

    log.warning("LLM response had no tool_use block")
    return None
