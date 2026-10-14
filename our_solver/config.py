"""Bot configuration from environment variables (VPS deploy)."""
from __future__ import annotations

import os
from pathlib import Path

_BASE = Path(__file__).resolve().parent


def _read_token() -> str:
    """
    Resolve bot token in this order:
    1. TELEGRAM_BOT_TOKEN env
    2. BOT_TOKEN env
    3. bot_token.txt next to main.py (single line)
    """
    for key in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    token_file = _BASE / "bot_token.txt"
    if token_file.is_file():
        try:
            line = token_file.read_text(encoding="utf-8").strip().splitlines()
            if line:
                return line[0].strip()
        except Exception:
            pass
    return ""


# Required
BOT_TOKEN = _read_token()


def _read_api_creds() -> tuple:
    """Telegram MTProto API id/hash (for large file download via Pyrogram)."""
    api_id = (
        os.environ.get("TELEGRAM_API_ID")
        or os.environ.get("TG_API_ID")
        or os.environ.get("API_ID")
        or ""
    ).strip()
    api_hash = (
        os.environ.get("TELEGRAM_API_HASH")
        or os.environ.get("TG_API_HASH")
        or os.environ.get("API_HASH")
        or ""
    ).strip()
    # Optional files next to main.py (Goku-friendly)
    id_file = _BASE / "api_id.txt"
    hash_file = _BASE / "api_hash.txt"
    if not api_id and id_file.is_file():
        try:
            api_id = id_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except Exception:
            pass
    if not api_hash and hash_file.is_file():
        try:
            api_hash = hash_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except Exception:
            pass
    return api_id, api_hash


TG_API_ID, TG_API_HASH = _read_api_creds()

# Admins: comma-separated Telegram user IDs
ADMIN_ALLOWLIST_RAW = os.environ.get("ADMIN_ALLOWLIST", "6738343341").strip()
ADMIN_ALLOWLIST = {
    int(s.strip())
    for s in ADMIN_ALLOWLIST_RAW.split(",")
    if s.strip().isdigit()
}
ADMIN_FORWARD_CHAT_ID = int(
    os.environ.get(
        "ADMIN_FORWARD_CHAT_ID",
        str(next(iter(ADMIN_ALLOWLIST), 0)),
    )
    or 0
)

# Bot mode: "open" = anyone can use | "manual" = admins + free-listed only
BOT_MODE = os.environ.get("BOT_MODE", "open").strip().lower()
if BOT_MODE not in ("open", "manual"):
    BOT_MODE = "open"

# Checking limits — VPS defaults (raise/lower via env as needed)
# Cookie mode: high concurrency + pooled HTTP (see utils/http.py)
CHECK_CONCURRENCY = int(os.environ.get("CHECK_CONCURRENCY", "80"))
PER_CHECK_TIMEOUT_MS = int(os.environ.get("PER_CHECK_TIMEOUT_MS", "12000"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "999"))
MAX_CONCURRENT_USERS = int(os.environ.get("MAX_CONCURRENT_USERS", "32"))
RDP_CONCURRENCY = int(os.environ.get("RDP_CONCURRENCY", "16"))
# Hotmail / Mail Mode workers (async tasks):
# 0 = auto
#   proxyless: HOTMAIL_PROXYLESS_THREADS (default 10) — efficient multi-acc on one IP
#   proxy:     min(16, max(4, proxy_count))
HOTMAIL_CONCURRENCY = int(os.environ.get("HOTMAIL_CONCURRENCY", "0"))
# Proxyless Wave1: high fan-out + fail-fast (see checker login_only timeouts)
# Proxyless: msa_fresh MSA login (keep moderate to avoid RL)
HOTMAIL_PROXYLESS_THREADS = int(os.environ.get("HOTMAIL_PROXYLESS_THREADS", "6"))
HOTMAIL_PROXY_MAX_THREADS = int(os.environ.get("HOTMAIL_PROXY_MAX_THREADS", "24"))
# Soft cap per Start press (was wrongly 500 — dropped real lists)
HOTMAIL_MAX_BATCH = int(os.environ.get("HOTMAIL_MAX_BATCH", "10000"))
# Lightning Hotmail OAuth APIs (Outlook Lite client is dead — Office native works)
HOTMAIL_OAUTH_CLIENT = os.environ.get(
    "HOTMAIL_OAUTH_CLIENT", "d3590ed6-52b3-4102-aeff-aad2292ab01c"
).strip()
HOTMAIL_OAUTH_REDIRECT = os.environ.get(
    "HOTMAIL_OAUTH_REDIRECT",
    "https://login.microsoftonline.com/common/oauth2/nativeclient",
).strip()
HOTMAIL_IDP_URL = os.environ.get(
    "HOTMAIL_IDP_URL",
    "https://odc.officeapps.live.com/odc/emailhrd/getidp?hm=1&emailAddress={email}",
).strip()
JOBS_PER_USER = 2

# Netflix detail worker (same as original) — override on VPS without code edit
NETFLIX_DETAIL_WORKER = os.environ.get(
    "NETFLIX_DETAIL_WORKER",
    "https://netflix-detail-api.gyashllzx.workers.dev",
).rstrip("/")

# Per-site API bases (env-overridable for Workers / mirrors)
CLAUDE_API_BASE = os.environ.get("CLAUDE_API_BASE", "https://claude.ai").rstrip("/")
CHATGPT_API_BASE = os.environ.get("CHATGPT_API_BASE", "https://chatgpt.com").rstrip("/")
CURSOR_API_BASE = os.environ.get("CURSOR_API_BASE", "https://cursor.com").rstrip("/")
GROK_API_BASE = os.environ.get("GROK_API_BASE", "https://grok.com").rstrip("/")
PRIMEVIDEO_BASE = os.environ.get(
    "PRIMEVIDEO_BASE", "https://www.primevideo.com"
).rstrip("/")

# Parallel URL downloads when user pastes several log links (Logs Mode)
GRAB_URL_WORKERS = max(1, min(8, int(os.environ.get("GRAB_URL_WORKERS", "6"))))

# MTT proxy for Logs (/grab) + ULP URL downloads (empty = direct).
# Formats: http://host:port  ·  socks5://host:port  ·  http://user:pass@host:port
# socks5 is sent to curl as socks5h (DNS through the proxy).
MTT_PROXY = (
    os.environ.get("MTT_PROXY")
    or os.environ.get("GRAB_PROXY")
    or ""
).strip()

CREDIT = "<i>VortexCheck  ·  v5.2</i>"

# Timezone used for greetings ("Good morning") and any user-facing clock text.
# Any IANA name works (Asia/Dhaka, Europe/London, …). Falls back to UTC.
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "UTC").strip() or "UTC"

# State file for VIP/free/mode persistence on disk
STATE_FILE = os.environ.get("STATE_FILE", str(_BASE / "bot_state.json"))

# /grab limits — download streams to disk.
# URL / multi-part combined size: default 5 GB (not a single TG file).
# Single Telegram document is still capped by TG_MTPROTO_MAX_BYTES (~4 GB Premium).
GRAB_MAX_BYTES = int(os.environ.get("GRAB_MAX_BYTES", str(5 * 1024 * 1024 * 1024)))  # 5GB
GRAB_TIMEOUT_SEC = int(os.environ.get("GRAB_TIMEOUT_SEC", "7200"))  # 2h for multi‑GB
# Legacy aria2 knobs — /grab URL downloads use curl only (these are unused).
GRAB_ARIA2_CONNECTIONS = os.environ.get("GRAB_ARIA2_CONNECTIONS", "").strip()
GRAB_ARIA2_SAFE = os.environ.get("GRAB_ARIA2_SAFE", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
