"""
FastAPI app - the real control panel. GET / serves a live dashboard that
handles everything: first-run setup (Discord + Tastytrade credentials),
live risk tuning, and monitoring - all over the same origin, no CORS needed
since it's one process on your machine talking to itself.
"""
from __future__ import annotations
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import settings, update_risk_settings, update_credentials, is_configured, is_vault_active
from app.db import get_recent_trades
from app.risk_engine import evaluate
from app.runtime_state import is_paused, set_paused, uptime_seconds, get_discord_error
from app.signal_parser import parse_signal
from app.tastytrade_client import tastytrade_client

app = FastAPI(title="Discord -> Tastytrade Signal Bot")

DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"


class SignalIn(BaseModel):
    text: str


class SettingsIn(BaseModel):
    dry_run: bool | None = None
    max_slippage_pct: float | None = None
    default_contracts: int | None = None
    max_contracts_hard_cap: int | None = None
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    size_tag_map: dict | None = None
    sizing_mode: str | None = None
    budget_usd: float | None = None
    entry_order_type: str | None = None
    stop_order_type: str | None = None


class CredentialsIn(BaseModel):
    discord_user_token: str | None = None
    discord_signal_channel_ids: list[int] | None = None
    tt_env: str | None = None
    tt_client_secret: str | None = None
    tt_refresh_token: str | None = None
    tt_account_number: str | None = None
    anthropic_api_key: str | None = None


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_PATH.read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {"status": "ok", "dry_run": settings.dry_run}


@app.get("/api/status")
async def api_status():
    return {
        "configured": is_configured(),
        "secure_storage_active": is_vault_active(),
        "dry_run": settings.dry_run,
        "paused": is_paused(),
        "stream_connected": tastytrade_client.is_stream_connected(),
        "tastytrade_error": tastytrade_client.get_last_error(),
        "discord_error": get_discord_error(),
        "uptime_seconds": uptime_seconds(),
        "channels": settings.discord_signal_channel_ids,
        "tt_env": "live" if "cert" not in settings.tt_base_url else "sandbox",
        "has_discord_token": bool(settings.discord_user_token),
        "has_tt_credentials": bool(settings.tt_client_secret and settings.tt_refresh_token and settings.tt_account_number),
        "has_anthropic_key": bool(settings.anthropic_api_key),
        "tt_account_number": settings.tt_account_number,
        "risk": {
            "max_slippage_pct": settings.max_slippage_pct,
            "default_contracts": settings.default_contracts,
            "max_contracts_hard_cap": settings.max_contracts_hard_cap,
            "take_profit_pct": settings.take_profit_pct,
            "stop_loss_pct": settings.stop_loss_pct,
            "size_tag_map": settings.size_tag_map,
            "sizing_mode": settings.sizing_mode,
            "budget_usd": settings.budget_usd,
            "entry_order_type": settings.entry_order_type,
            "stop_order_type": settings.stop_order_type,
        },
    }


@app.post("/api/pause")
async def api_pause():
    set_paused(True)
    return {"paused": True}


@app.post("/api/resume")
async def api_resume():
    set_paused(False)
    return {"paused": False}


@app.get("/api/trades")
async def api_trades(limit: int = 50):
    return {"trades": get_recent_trades(limit)}


@app.post("/api/settings")
async def api_settings(payload: SettingsIn):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    update_risk_settings(data)
    return {"status": "updated", "applied": data}


@app.post("/api/credentials")
async def api_credentials(payload: CredentialsIn):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    update_credentials(data)
    return {"status": "saved - restart required to connect with new credentials"}


@app.post("/api/restart")
async def api_restart():
    """
    Restarts the whole process in place so it picks up .env changes.

    On POSIX (Linux/Mac), os.execv genuinely replaces the process image -
    verified working, including a clean re-read of .env by the new process.

    On Windows, os.execv is unreliable: it doesn't truly replace the process
    the way POSIX exec does, and can leave the listening socket held by the
    old process while the "new" one tries to bind the same port, causing it
    to fail silently. Instead, spawn a detached watcher process that waits
    for this process to fully exit and release the port, then launches a
    fresh one - and exit this process immediately via os._exit so the port
    frees up right away rather than waiting on any cleanup.

    Only reconstructs the original invocation correctly if you started the
    app with `python run.py` as documented, from the project's root
    directory - a different launch method needs its own restart handling.
    """
    def _restart():
        time.sleep(1.0)  # give the HTTP response time to reach the browser first
        if platform.system() == "Windows":
            watcher_cmd = [
                sys.executable, "-c",
                "import time,subprocess,sys; time.sleep(2); subprocess.run(sys.argv[1:])",
                sys.executable,
            ] + sys.argv
            subprocess.Popen(
                watcher_cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_restart, daemon=True).start()
    return {"status": "restarting"}


@app.post("/api/shutdown")
async def api_shutdown():
    """
    Cleanly stops the whole process - a deliberate, user-initiated stop
    (unlike restart, this doesn't relaunch). This is a hard exit rather
    than an attempt at graceful cross-thread async cleanup: the Discord
    listener runs its own asyncio event loop in a separate thread from
    this one, which makes coordinating a fully graceful shutdown across
    both non-trivial and fragile to get right reliably. Since this is a
    deliberate stop rather than a crash, and orders are fire-and-forget
    once submitted to the broker (not something this process needs to stay
    alive to track), a hard exit is an acceptable tradeoff here.
    """
    def _shutdown():
        time.sleep(0.8)  # give the HTTP response time to reach the browser first
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()
    return {"status": "shutting down"}


@app.post("/api/test-broker-latency")
async def api_test_broker_latency():
    """Safe latency check - a real, read-only, authenticated call to Tastytrade
    (account balances), never an order. Use this alongside Dry Run testing to
    estimate the one leg dry-run can't measure for real: the final broker
    network round-trip after the slippage decision is already made."""
    try:
        ms = await tastytrade_client.measure_broker_latency_ms()
        return {"latency_ms": ms}
    except Exception as e:
        return {"error": str(e)}


@app.post("/test-parse")
async def test_parse(payload: SignalIn):
    signal = parse_signal(payload.text)
    return {"parsed": signal.__dict__ if signal else None}


@app.post("/test-signal")
async def test_signal(payload: SignalIn):
    signal = parse_signal(payload.text)
    if signal is None:
        return {"error": "could not parse signal"}
    live_price = await tastytrade_client.get_live_price(
        signal.symbol, signal.expiration, signal.option_type, signal.strike
    )
    decision = evaluate(signal, live_price)
    return {"parsed": signal.__dict__, "live_price": live_price, "decision": decision.__dict__}


@app.post("/process")
async def process(payload: SignalIn):
    """Manual test endpoint - runs a signal through the same pipeline a real
    Discord message would."""
    from app.discord_selfbot import process_signal_text
    # No real Discord message context here since this is a manual test
    # endpoint - channel_id is just for the activity log, not routing.
    channel_id = settings.discord_signal_channel_ids[0] if settings.discord_signal_channel_ids else 0
    await process_signal_text(payload.text, channel_id)
    return {"status": "processed - check logs / trades.db"}
