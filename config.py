"""
config.py - Centralized configuration for the Myntra BLINKDEAL bot.

Everything here is read from environment variables, with sensible defaults,
so the same code runs unchanged locally (export the vars yourself, or use a
.env file you source) and inside GitHub Actions (where TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID come from repository Secrets).
"""

import os


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Required environment variable '{name}' is not set.")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --- Telegram ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require("TELEGRAM_CHAT_ID")

# --- Target product ----------------------------------------------------------
# Defaults to the product from the handoff doc. Override with the PRODUCT_URL
# repo/environment variable if you point this at a different item later.
PRODUCT_URL = _optional(
    "PRODUCT_URL",
    "https://www.myntra.com/mailers/jewellery/kalyan-jewellers/"
    "kalyan-jewellers-24k-(999)-purity-shreeram-gold-coin-1-gms/31416910/buy",
)
COUPON_CODE = _optional("COUPON_CODE", "BLINKDEAL")

# --- HTTP behaviour -----------------------------------------------------------
REQUEST_TIMEOUT_SECONDS = float(_optional("REQUEST_TIMEOUT_SECONDS", "15"))
MAX_RETRIES = int(_optional("MAX_RETRIES", "3"))
RETRY_BACKOFF_SECONDS = float(_optional("RETRY_BACKOFF_SECONDS", "2"))

# Ordinary browser identification headers. This is just normal HTTP client
# practice (every request needs a User-Agent) -- not an attempt to defeat
# bot detection, which this project deliberately does not try to do.
USER_AGENT = _optional(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# --- State persistence --------------------------------------------------------
STATE_FILE_PATH = _optional("STATE_FILE_PATH", "state.json")

# If this many consecutive runs in a row fail to get a usable page, send a
# single heads-up Telegram message so a human notices (instead of silently
# never alerting again). Default ~1 hour at a 5-minute cadence.
BLOCK_ALERT_THRESHOLD = int(_optional("BLOCK_ALERT_THRESHOLD", "12"))

# --- Logging -------------------------------------------------------------------
LOG_LEVEL = _optional("LOG_LEVEL", "INFO")
