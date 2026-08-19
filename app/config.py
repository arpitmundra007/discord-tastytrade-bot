from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

log = logging.getLogger("config")

load_dotenv(os.getenv("ENV_FILE_PATH", ".env"), override=True)

# --- Secure secret storage via the OS credential vault (Windows Credential
# Manager / macOS Keychain / Linux Secret Service), instead of leaving
# tokens sitting in plaintext in .env. Falls back to .env transparently if
# no OS credential backend is available - the app still works either way,
# just with weaker protection on that machine.
_KEYRING_SERVICE = "discord-tastytrade-bot"
_SECRET_KEYS = ("discord_user_token", "tt_client_secret", "tt_refresh_token", "anthropic_api_key")

try:
    import keyring
    import keyring.errors
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

_keyring_warned = False


def _keyring_get(key: str) -> str | None:
    if not KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(_KEYRING_SERVICE, key)
    except Exception:
        return None


def _keyring_set(key: str, value: str) -> bool:
    """Returns True if actually stored in the OS vault, False if it fell back."""
    global _keyring_warned
    if not KEYRING_AVAILABLE:
        return False
    try:
        keyring.set_password(_KEYRING_SERVICE, key, value)
        return True
    except Exception as e:
        if not _keyring_warned:
            log.warning(
                "No OS credential vault available (%s) - secrets will be stored in .env "
                "instead. Still functional, just less protected on this machine.",
                type(e).__name__,
            )
            _keyring_warned = True
        return False


_SECRET_PLACEHOLDER = "<stored securely - see OS credential manager>"


def _load_secret(env_key: str, keyring_key: str) -> str:
    """Keyring takes priority if present; otherwise falls back to the .env
    value, which also transparently handles migrating an existing plaintext
    .env secret the first time it's re-saved through the dashboard.

    If the vault entry is ever missing (e.g. cleared by a Windows reset) but
    .env still has the placeholder marker from when it WAS in the vault,
    treat that as unset rather than using the placeholder text as if it
    were a real credential - which would otherwise fail confusingly instead
    of clearly showing as "not configured"."""
    from_vault = _keyring_get(keyring_key)
    if from_vault:
        return from_vault
    from_env = os.getenv(env_key, "")
    if from_env == _SECRET_PLACEHOLDER:
        return ""
    return from_env


def _parse_size_map(raw: str) -> dict:
    """'SMALL:1,MEDIUM:3' -> {'SMALL': 1, 'MEDIUM': 3}"""
    out = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        tag, qty = pair.split(":")
        out[tag.strip().upper()] = int(qty.strip())
    return out


@dataclass
class Settings:
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    discord_user_token: str = field(default_factory=lambda: _load_secret("DISCORD_USER_TOKEN", "discord_user_token"))
    discord_signal_channel_ids: list = field(
        default_factory=lambda: [
            int(c.strip()) for c in os.getenv("DISCORD_SIGNAL_CHANNEL_IDS", os.getenv("DISCORD_SIGNAL_CHANNEL_ID", "")).split(",")
            if c.strip()
        ]
    )

    tt_client_secret: str = field(default_factory=lambda: _load_secret("TT_CLIENT_SECRET", "tt_client_secret"))
    tt_refresh_token: str = field(default_factory=lambda: _load_secret("TT_REFRESH_TOKEN", "tt_refresh_token"))
    anthropic_api_key: str = field(default_factory=lambda: _load_secret("ANTHROPIC_API_KEY", "anthropic_api_key"))
    tt_account_number: str = os.getenv("TT_ACCOUNT_NUMBER", "")
    tt_base_url: str = os.getenv("TT_BASE_URL", "https://api.cert.tastyworks.com")

    max_slippage_pct: float = float(os.getenv("MAX_SLIPPAGE_PCT", "10"))

    size_tag_map: dict = field(default_factory=lambda: _parse_size_map(os.getenv("SIZE_TAG_MAP", "SMALL:1")))
    default_contracts: int = int(os.getenv("DEFAULT_CONTRACTS", "1"))
    max_contracts_hard_cap: int = int(os.getenv("MAX_CONTRACTS_HARD_CAP", "2"))
    sizing_mode: str = os.getenv("SIZING_MODE", "tag")  # "tag" (per-signal-tag contract counts) or "budget" (single $ budget per trade)
    budget_usd: float = float(os.getenv("BUDGET_USD", "300"))

    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "50"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "50"))
    entry_order_type: str = os.getenv("ENTRY_ORDER_TYPE", "limit")  # "limit" (price-protected, may not fill) or "market" (guaranteed fill, no price protection)
    stop_order_type: str = os.getenv("STOP_ORDER_TYPE", "stop")  # "stop" (guaranteed exit once triggered, uncertain fill price) or "stop_limit" (bounded fill price, may not fill at all in a gap)

    db_path: str = os.getenv("DB_PATH", "./trades.db")


settings = Settings()

ENV_PATH = os.getenv("ENV_FILE_PATH", ".env")

# Keys the dashboard is allowed to write back to .env. Broker/Discord
# credentials are included so the dashboard can save them, but changes to
# those only take effect after a restart (the session/listener are already
# established); risk fields take effect on the very next signal.
_MANAGED_ENV_KEYS = {
    "dry_run": "DRY_RUN",
    "max_slippage_pct": "MAX_SLIPPAGE_PCT",
    "default_contracts": "DEFAULT_CONTRACTS",
    "max_contracts_hard_cap": "MAX_CONTRACTS_HARD_CAP",
    "take_profit_pct": "TAKE_PROFIT_PCT",
    "stop_loss_pct": "STOP_LOSS_PCT",
    "sizing_mode": "SIZING_MODE",
    "budget_usd": "BUDGET_USD",
    "entry_order_type": "ENTRY_ORDER_TYPE",
    "stop_order_type": "STOP_ORDER_TYPE",
}


def update_risk_settings(data: dict) -> None:
    """Applies risk-related fields to the live settings object immediately,
    then persists them to .env so they survive a restart too."""
    if "dry_run" in data:
        settings.dry_run = bool(data["dry_run"])
    if "max_slippage_pct" in data:
        settings.max_slippage_pct = float(data["max_slippage_pct"])
    if "default_contracts" in data:
        settings.default_contracts = int(data["default_contracts"])
    if "max_contracts_hard_cap" in data:
        settings.max_contracts_hard_cap = int(data["max_contracts_hard_cap"])
    if "take_profit_pct" in data:
        settings.take_profit_pct = float(data["take_profit_pct"])
    if "stop_loss_pct" in data:
        settings.stop_loss_pct = float(data["stop_loss_pct"])
    if "sizing_mode" in data:
        settings.sizing_mode = str(data["sizing_mode"])
    if "budget_usd" in data:
        settings.budget_usd = float(data["budget_usd"])
    if "entry_order_type" in data:
        settings.entry_order_type = str(data["entry_order_type"])
    if "stop_order_type" in data:
        settings.stop_order_type = str(data["stop_order_type"])
    if "size_tag_map" in data:
        settings.size_tag_map = {str(k).upper(): int(v) for k, v in data["size_tag_map"].items()}
        _write_env_line("SIZE_TAG_MAP", ",".join(f"{k}:{v}" for k, v in settings.size_tag_map.items()))

    for field_name, env_key in _MANAGED_ENV_KEYS.items():
        if field_name in data:
            _write_env_line(env_key, str(getattr(settings, field_name)).lower() if isinstance(getattr(settings, field_name), bool) else str(getattr(settings, field_name)))


def update_credentials(data: dict) -> None:
    """Writes Discord/Tastytrade connection fields to .env. These require a
    process restart to take effect (the listener and broker session are
    already established at startup), unlike update_risk_settings.

    Every value is stripped of leading/trailing whitespace before saving -
    a stray space or newline from pasting (easy to do, invisible once saved)
    otherwise produces a token that LOOKS right but fails auth with a
    cryptic error and no visible clue why.
    """
    if "discord_user_token" in data and data["discord_user_token"].strip():
        settings.discord_user_token = data["discord_user_token"].strip()
        _save_secret("DISCORD_USER_TOKEN", "discord_user_token", settings.discord_user_token)
    if "discord_signal_channel_ids" in data:
        ids = data["discord_signal_channel_ids"]
        settings.discord_signal_channel_ids = ids
        _write_env_line("DISCORD_SIGNAL_CHANNEL_IDS", ",".join(str(i) for i in ids))
    if "tt_env" in data:
        base_url = "https://api.tastyworks.com" if data["tt_env"] == "live" else "https://api.cert.tastyworks.com"
        settings.tt_base_url = base_url
        _write_env_line("TT_BASE_URL", base_url)
    if "tt_client_secret" in data and data["tt_client_secret"].strip():
        settings.tt_client_secret = data["tt_client_secret"].strip()
        _save_secret("TT_CLIENT_SECRET", "tt_client_secret", settings.tt_client_secret)
    if "tt_refresh_token" in data and data["tt_refresh_token"].strip():
        settings.tt_refresh_token = data["tt_refresh_token"].strip()
        _save_secret("TT_REFRESH_TOKEN", "tt_refresh_token", settings.tt_refresh_token)
    if "anthropic_api_key" in data and data["anthropic_api_key"].strip():
        settings.anthropic_api_key = data["anthropic_api_key"].strip()
        _save_secret("ANTHROPIC_API_KEY", "anthropic_api_key", settings.anthropic_api_key)
    if "tt_account_number" in data and data["tt_account_number"].strip():
        settings.tt_account_number = data["tt_account_number"].strip()
        _write_env_line("TT_ACCOUNT_NUMBER", settings.tt_account_number)


def _save_secret(env_key: str, keyring_key: str, value: str) -> None:
    """Stores a secret in the OS credential vault if available, and leaves
    only a placeholder (never the raw value) in .env - so opening .env in a
    text editor, accidentally committing it, or copying the project folder
    doesn't expose the secret even though it's sitting right there. Falls
    back to writing the plaintext value directly if no vault is available on
    this machine, so the app still works, just less protected."""
    if _keyring_set(keyring_key, value):
        _write_env_line(env_key, _SECRET_PLACEHOLDER)
    else:
        _write_env_line(env_key, value)


_vault_active_cache: bool | None = None


def is_vault_active() -> bool:
    """Cached after first call - whether the OS credential vault is actually
    working on this machine (not just importable). Used by the dashboard to
    show whether saved secrets are encrypted or falling back to plaintext."""
    global _vault_active_cache
    if _vault_active_cache is None:
        test_key = "_vault_status_check"
        _vault_active_cache = bool(
            KEYRING_AVAILABLE and _keyring_set(test_key, "test") and _keyring_get(test_key) == "test"
        )
    return _vault_active_cache


def is_configured() -> bool:
    """Whether enough is filled in to actually connect - used by the dashboard
    to decide whether to land on Setup or Live."""
    return bool(
        settings.discord_user_token
        and settings.discord_signal_channel_ids
        and settings.tt_client_secret
        and settings.tt_refresh_token
        and settings.tt_account_number
    )


def _write_env_line(key: str, value: str) -> None:
    """Replaces KEY=... in .env if present, otherwise appends it. Creates .env if it doesn't exist yet."""
    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, "a", encoding="utf-8").close()
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    prefix = f"{key}="
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{prefix}{value}\n"
            found = True
            break
    if not found:
        lines.append(f"{prefix}{value}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)
