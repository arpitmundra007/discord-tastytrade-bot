"""
Self-bot mode: reads signal channels using YOUR OWN Discord account (via its
personal token) instead of an admin-invited bot application. This works on
channels you're just a member of, without needing anyone's permission to
invite a bot - which is the whole appeal, and also exactly what Discord's
own policy prohibits:

    "Automating normal user accounts (self-bots) outside of the OAuth2/bot
    API is forbidden, and can result in account termination if found."
    - https://support.discord.com/hc/en-us/articles/115002192352

That prohibition is unconditional in Discord's own wording - it is not
limited to spam or abusive behavior, and reading messages only does not
create an exception to it. Running this means accepting that your Discord
account (all of it - every server you're in, not just the trading one) is
at risk of termination if Discord's systems flag this account as automated.
This file exists because you asked for it after that tradeoff was made
explicit - it isn't a recommendation.

Depends on `discord.py-self`, NOT `discord.py` - the two packages occupy the
same `discord` import namespace and cannot be installed in the same
environment. See requirements.txt and the README's self-bot section.

Uses your Discord USER token (not a bot token) - see the README for how to
find it. Never use a browser extension or third-party "token grabber" tool
to get it; those are a common vector for actual account-stealing malware.
Retrieve it manually via your browser's own developer tools only.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

import discord

from app.config import settings
from app.db import log_trade
from app.llm_parser import parse_signal_with_llm, to_parsed_signal
from app.risk_engine import evaluate
from app.runtime_state import is_paused, set_discord_error
from app.signal_parser import parse_signal
from app.tastytrade_client import tastytrade_client

log = logging.getLogger("signal_selfbot")

client = discord.Client()


def _message_text(message: "discord.Message") -> str:
    parts = [message.content or ""]
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for f in embed.fields:
            if f.value:
                parts.append(f.value)
    return "\n".join(p for p in parts if p)


async def _try_llm_fallback(raw_text: str, channel_id: int):
    """
    Called only when the regex parser can't make sense of a message. Tries
    the LLM extractor; if it succeeds but the signal isn't something this
    app can execute yet (wrong instrument type, a stop-loss update with no
    position tracking, low confidence), logs it clearly to the dashboard's
    activity feed instead of either failing silently or guessing at a trade.
    """
    llm_sig = await parse_signal_with_llm(raw_text)
    if llm_sig is None:
        log.info("Unparseable message in channel %s (regex and LLM both failed, or no API key set): %r", channel_id, raw_text[:200])
        return None

    log.info("LLM-parsed signal from channel %s: %s (%s)", channel_id, llm_sig, llm_sig.reasoning)
    gap = llm_sig.get_execution_gap()
    if gap:
        log.info("LLM-parsed signal not actioned: %s", gap)
        log_trade(raw_text, vars(llm_sig), approved=False, reason=f"LLM-parsed but not executable: {gap}", order_payload=None)
        return None

    parsed = to_parsed_signal(llm_sig)
    if parsed is None:
        # Defensive - get_execution_gap() should have already caught anything
        # that would land here, but never silently drop a signal without a
        # visible reason if it somehow does.
        log.warning("LLM signal passed execution-gap check but conversion still failed: %s", llm_sig)
        log_trade(raw_text, vars(llm_sig), approved=False, reason="LLM-parsed signal failed conversion to a tradable order", order_payload=None)
        return None

    return parsed


async def process_signal_text(raw_text: str, channel_id: int, posted_at: "datetime | None" = None):
    """
    posted_at: Discord's own server timestamp for when the message was
    actually posted (message.created_at), not just when our handler started
    running. Passed through from on_message so latency measurements reflect
    true end-to-end time, including Discord's own gateway delivery delay -
    not just this app's internal processing time. Falls back to "now" for
    calls with no real Discord message behind them (e.g. the /process
    manual test endpoint).
    """
    t_received = datetime.now(timezone.utc)
    posted_at = posted_at or t_received

    if is_paused():
        log.info("Trading paused via dashboard kill switch - ignoring signal from channel %s", channel_id)
        return

    signal = parse_signal(raw_text)
    t_parsed = datetime.now(timezone.utc)
    used_llm = False
    if signal is None:
        signal = await _try_llm_fallback(raw_text, channel_id)
        t_parsed = datetime.now(timezone.utc)
        used_llm = True
        if signal is None:
            return

    log.info("Parsed signal from channel %s: %s", channel_id, signal)

    live_price = await tastytrade_client.get_live_price(
        signal.symbol, signal.expiration, signal.option_type, signal.strike
    )
    t_quoted = datetime.now(timezone.utc)

    decision = evaluate(signal, live_price)
    t_evaluated = datetime.now(timezone.utc)

    timing = {
        "posted_to_received_ms": round((t_received - posted_at).total_seconds() * 1000),
        "parse_ms": round((t_parsed - t_received).total_seconds() * 1000),
        "used_llm_fallback": used_llm,
        "quote_fetch_ms": round((t_quoted - t_parsed).total_seconds() * 1000),
        "risk_eval_ms": round((t_evaluated - t_quoted).total_seconds() * 1000),
    }

    if not decision.approved:
        timing["total_ms"] = round((datetime.now(timezone.utc) - posted_at).total_seconds() * 1000)
        log.warning("Signal rejected: %s | timing=%s", decision.reason, timing)
        log_trade(raw_text, signal.__dict__, approved=False, reason=decision.reason, order_payload={"timing": timing})
        return

    order_result = await tastytrade_client.submit_bracket_order(
        signal,
        contracts=decision.contracts,
        entry_price=decision.entry_limit_price,
        take_profit_price=decision.take_profit_price,
        stop_loss_price=decision.stop_loss_price,
        dry_run=settings.dry_run,
    )
    t_ordered = datetime.now(timezone.utc)
    timing["order_submit_ms"] = round((t_ordered - t_evaluated).total_seconds() * 1000)
    timing["total_ms"] = round((t_ordered - posted_at).total_seconds() * 1000)

    log.info("Order result: %s | timing=%s", order_result, timing)
    order_result = {**order_result, "timing": timing}
    log_trade(raw_text, signal.__dict__, approved=True, reason=decision.reason, order_payload=order_result)


@client.event
async def on_ready():
    log.warning(
        "SELF-BOT MODE ACTIVE - logged in as %s using a personal account token. "
        "This account is at risk of termination per Discord's own ToS regardless "
        "of read-only usage. Monitoring channel IDs: %s",
        client.user, settings.discord_signal_channel_ids,
    )
    try:
        await tastytrade_client.connect()
        tastytrade_client.start_streaming()
    except Exception:
        log.error(
            "Tastytrade connection failed on startup - Discord listener is still running, "
            "but no orders will work until this is fixed and the app is restarted. "
            "Check TT_CLIENT_SECRET / TT_REFRESH_TOKEN / TT_ACCOUNT_NUMBER / TT_BASE_URL."
        )


@client.event
async def on_message(message: "discord.Message"):
    if message.channel.id not in settings.discord_signal_channel_ids:
        return
    try:
        await process_signal_text(_message_text(message), message.channel.id, posted_at=message.created_at)
    except Exception:
        log.exception("Failed to process message %s", message.id)


def run():
    logging.basicConfig(level=logging.INFO)
    if not settings.discord_user_token:
        set_discord_error("DISCORD_USER_TOKEN is not set - see README's self-bot section")
        log.error("DISCORD_USER_TOKEN is not set - see README's self-bot section")
        return
    try:
        # No "Bot " prefix - this logs in as the user account itself.
        client.run(settings.discord_user_token)
    except Exception as e:
        set_discord_error(f"Discord login failed: {e}")
        log.error("Discord client.run() failed: %s", e)
