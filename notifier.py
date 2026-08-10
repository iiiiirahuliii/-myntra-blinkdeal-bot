"""
notifier.py - Telegram delivery helper.

Sends a message via the Telegram Bot API with timeouts, retry/backoff for
transient failures, and logging. Kept independent of check.py so it can be
reused (e.g. by a future /status command or multi-product version) without
dragging in the scraping logic.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class NotifierError(RuntimeError):
    """Raised when a Telegram message could not be delivered after all retries."""


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = 15,
    max_retries: int = 3,
    backoff_seconds: float = 2,
) -> None:
    """Send `text` to `chat_id` via the Telegram Bot API.

    Retries on network errors and non-200 responses using exponential
    backoff. Raises NotifierError if every attempt fails.
    """
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code == 200:
                logger.info("Telegram message sent successfully.")
                return
            logger.warning(
                "Telegram API returned status %s on attempt %s/%s: %s",
                response.status_code,
                attempt,
                max_retries,
                response.text[:300],
            )
            last_error = RuntimeError(
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        except requests.RequestException as exc:
            logger.warning(
                "Telegram request failed on attempt %s/%s: %s",
                attempt,
                max_retries,
                exc,
            )
            last_error = exc

        if attempt < max_retries:
            sleep_for = backoff_seconds * (2 ** (attempt - 1))
            time.sleep(sleep_for)

    raise NotifierError(
        f"Failed to send Telegram message after {max_retries} attempts"
    ) from last_error
