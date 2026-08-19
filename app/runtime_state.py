"""
Process-wide runtime state that the dashboard can flip live, without a
restart - currently just the pause/kill switch. Kept separate from Settings
because this is transient (resets on restart) rather than configuration.
"""
from __future__ import annotations
import time

_paused = False
_started_at = time.monotonic()
_discord_error: str | None = None


def is_paused() -> bool:
    return _paused


def set_paused(value: bool):
    global _paused
    _paused = value


def uptime_seconds() -> float:
    return round(time.monotonic() - _started_at, 1)


def get_discord_error() -> str | None:
    return _discord_error


def set_discord_error(value: str | None):
    global _discord_error
    _discord_error = value
