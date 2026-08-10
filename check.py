"""
check.py - Myntra BLINKDEAL monitor using curl_cffi (Chrome TLS Impersonation)
to bypass Akamai / anti-bot blocks on cloud servers.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

import config
from notifier import NotifierError, send_telegram_message

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("check")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

DEFAULT_STATE = {
    "blinkdeal_active": False,
    "last_checked_utc": None,
    "last_alert_sent_utc": None,
    "consecutive_unreadable_checks": 0,
    "block_alert_sent": False,
}


def load_state() -> dict:
    if not os.path.exists(config.STATE_FILE_PATH):
        return dict(DEFAULT_STATE)
    try:
        with open(config.STATE_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read state file (%s); starting fresh.", exc)
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    try:
        with open(config.STATE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as exc:
        logger.error("Could not write state file: %s", exc)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fetching with Chrome TLS Impersonation
# ---------------------------------------------------------------------------

def fetch_product_page(url: str):
    """Fetch product page using curl_cffi to spoof real Chrome 120 TLS fingerprint."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            # Impersonate Chrome 120 browser TLS signature
            response = curl_requests.get(
                url,
                headers=headers,
                impersonate="chrome120",
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            return response
        except Exception as exc:
            last_exc = exc
            logger.warning("Fetch attempt %s/%s failed: %s", attempt, config.MAX_RETRIES, exc)
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    logger.error("All fetch attempts failed: %s", last_exc)
    return None


def looks_like_block_page(html: str) -> bool:
    lowered = html.lower()
    block_markers = (
        "site maintenance",
        "something went wrong",
        "access denied",
        "request blocked",
        "captcha",
        "are you a human",
    )
    return any(marker in lowered for marker in block_markers)


# ---------------------------------------------------------------------------
# Coupon Detection
# ---------------------------------------------------------------------------

JSON_BLOB_PATTERNS = [
    re.compile(r"window\.__myx\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.DOTALL),
]

INTERESTING_KEY_HINT = re.compile(r"coupon|offer|deal|promo", re.IGNORECASE)


def find_json_blobs(html: str) -> list:
    blobs = []
    for pattern in JSON_BLOB_PATTERNS:
        for match in pattern.finditer(html):
            try:
                blobs.append(json.loads(match.group(1)))
            except json.JSONDecodeError:
                continue

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        if tag.string:
            try:
                blobs.append(json.loads(tag.string))
            except json.JSONDecodeError:
                continue
    return blobs


def _walk_for_coupon(node, coupon_code: str, found: list, under_interesting_key: bool = False) -> None:
    if found:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            key_is_interesting = bool(INTERESTING_KEY_HINT.search(str(key)))
            _walk_for_coupon(value, coupon_code, found, under_interesting_key or key_is_interesting)
    elif isinstance(node, list):
        for item in node:
            _walk_for_coupon(item, coupon_code, found, under_interesting_key)
    elif isinstance(node, str):
        if under_interesting_key and coupon_code.upper() in node.upper():
            found.append(node)


def detect_coupon_in_json(blobs: list, coupon_code: str) -> bool:
    for blob in blobs:
        found: list = []
        _walk_for_coupon(blob, coupon_code, found)
        if found:
            logger.info("Coupon '%s' found inside embedded JSON.", coupon_code)
            return True
    return False


def detect_coupon_in_visible_offer_blocks(html: str, coupon_code: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.find_all(
        lambda tag: tag.has_attr("class")
        and any(
            re.search(r"coupon|offer|deal|promo", c, re.IGNORECASE)
            for c in tag.get("class", [])
        )
    )
    for tag in candidates:
        if coupon_code.upper() in tag.get_text(" ", strip=True).upper():
            logger.info("Coupon '%s' found in HTML element.", coupon_code)
            return True
    return False


def coupon_is_active(html: str, coupon_code: str) -> bool:
    blobs = find_json_blobs(html)
    if blobs:
        return detect_coupon_in_json(blobs, coupon_code)

    logger.warning("No embedded JSON found; falling back to HTML scan.")
    return detect_coupon_in_visible_offer_blocks(html, coupon_code)


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    state["last_checked_utc"] = utc_now_iso()

    response = fetch_product_page(config.PRODUCT_URL)
    page_unusable = (
        response is None
        or response.status_code != 200
        or looks_like_block_page(response.text)
    )

    if page_unusable:
        state["consecutive_unreadable_checks"] += 1
        status_code_str = response.status_code if response else 'None'
        status_msg = f"⚠️ Unreadable / Blocked (HTTP Status: {status_code_str})"
        active_now = False
        logger.warning("Could not get a usable page (#%s in a row).", state["consecutive_unreadable_checks"])
    else:
        state["consecutive_unreadable_checks"] = 0
        state["block_alert_sent"] = False
        try:
            active_now = coupon_is_active(response.text, config.COUPON_CODE)
            status_msg = f"✅ Page Loaded Successfully! Coupon '{config.COUPON_CODE}' Active: {active_now}"
        except Exception:
            logger.exception("Unexpected error while parsing the page.")
            status_msg = "❌ Error parsing product page HTML/JSON."
            active_now = False

    # Send verification report to Telegram
    try:
        send_telegram_message(
            config.TELEGRAM_BOT_TOKEN,
            config.TELEGRAM_CHAT_ID,
            f"🔍 BLINKDEAL Check Report:\n"
            f"• Target: {config.PRODUCT_URL}\n"
            f"• Result: {status_msg}\n"
            f"• Time (UTC): {state['last_checked_utc']}",
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            max_retries=config.MAX_RETRIES,
            backoff_seconds=config.RETRY_BACKOFF_SECONDS,
        )
    except NotifierError as exc:
        logger.error("Could not send Telegram status report: %s", exc)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
