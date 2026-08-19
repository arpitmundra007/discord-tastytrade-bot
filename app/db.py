from __future__ import annotations
import json
import sqlite3
from datetime import datetime

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    raw_signal TEXT,
    parsed TEXT,
    approved INTEGER,
    reason TEXT,
    order_payload TEXT
);
"""


def _conn():
    conn = sqlite3.connect(settings.db_path)
    conn.execute(_SCHEMA)
    return conn


def log_trade(raw_signal: str, parsed: dict | None, approved: bool, reason: str, order_payload: dict | None):
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO trade_log (ts, raw_signal, parsed, approved, reason, order_payload) VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.utcnow().isoformat(),
                raw_signal,
                json.dumps(parsed, default=str) if parsed else None,
                int(approved),
                reason,
                json.dumps(order_payload, default=str) if order_payload else None,
            ),
        )
    conn.close()


def get_recent_trades(limit: int = 50):
    conn = _conn()
    conn.row_factory = sqlite3.Row
    with conn:
        rows = conn.execute(
            "SELECT id, ts, raw_signal, parsed, approved, reason, order_payload FROM trade_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "ts": r["ts"],
            "raw_signal": r["raw_signal"],
            "parsed": json.loads(r["parsed"]) if r["parsed"] else None,
            "approved": bool(r["approved"]),
            "reason": r["reason"],
            "order_payload": json.loads(r["order_payload"]) if r["order_payload"] else None,
        })
    return out
