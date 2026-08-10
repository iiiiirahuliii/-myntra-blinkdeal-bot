"""
check.py - Entry point for the Myntra BLINKDEAL monitor with verification reports.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import cloudscraper
from bs4 import BeautifulSoup

import config
from notifier import NotifierError, send_telegram_message

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("check")


# ---------------------------------------------------------------------------
# State (persisted to STATE_FILE_PATH between runs)
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
# Fetching
# ---------------------------------------------------------------------------

def fetch_product_page(url: str) -> Optional[requests.Response]:
    """Fetch the product page using cloudscraper to bypass anti-bot challenges."""
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
    }

    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )

    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return scraper.get(
                url, headers=headers, timeout=config.REQUEST_TIMEOUT_SECONDS
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Fetch attempt %s/%s failed: %s", attempt, config.MAX_RETRIES, exc
            )
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    logger.error("All fetch attempts failed: %s", last_exc)
    return None


def looks_like_block_page(html: str) -> bool:
    """Heuristic check for anti-bot block pages, CAPTCHAs, etc."""
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
# Parsing / coupon detection
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
# Main Execution Block
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

    # 1. Determine the status message for the verification alert
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
            status_msg = f"✅ Page Loaded! Coupon '{config.COUPON_CODE}' Active: {active_now}"
        except Exception:
            logger.exception("Unexpected error while parsing the page.")
            status_msg = "❌ Error parsing product page HTML/JSON."
            active_now = False

    # 2. Send the Detailed Test / Verification Message to Telegram
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

    # 3. Handle block threshold alerts & deal state transitions
    if page_unusable:
        if (
            state["consecutive_unreadable_checks"] >= config.BLOCK_ALERT_THRESHOLD
            and not state["block_alert_sent"]
        ):
            try:
                send_telegram_message(
                    config.TELEGRAM_BOT_TOKEN,
                    config.TELEGRAM_CHAT_ID,
                    "⚠️ BLINKDEAL bot heads-up: Multiple consecutive checks failed to read Myntra.",
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                    max_retries=config.MAX_RETRIES,
                    backoff_seconds=config.RETRY_BACKOFF_SECONDS,
                )
                state["block_alert_sent"] = True
            except NotifierError as exc:
                logger.error("Could not send block alert: %s", exc)

        save_state(state)
        return 0

    was_active = state["blinkdeal_active"]

    if active_now and not was_active:
        logger.info("BLINKDEAL just appeared! Sending primary deal alert.")
        try:
            send_telegram_message(
                config.TELEGRAM_BOT_TOKEN,
                config.TELEGRAM_CHAT_ID,
                f"🎉 BLINKDEAL IS LIVE!\n{config.PRODUCT_URL}",
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                max_retries=config.MAX_RETRIES,
                backoff_seconds=config.RETRY_BACKOFF_SECONDS,
            )
        except NotifierError as exc:
            logger.error("Could not send BLINKDEAL alert: %s", exc)
            save_state(state)
            return 1
        state["last_alert_sent_utc"] = utc_now_iso()
        state["blinkdeal_active"] = True
    elif active_now and was_active:
        logger.info("BLINKDEAL still active; alert already sent earlier.")
    elif not active_now and was_active:
        logger.info("BLINKDEAL no longer detected; resetting state.")
        state["blinkdeal_active"] = False
    else:
        logger.info("BLINKDEAL not active.")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
