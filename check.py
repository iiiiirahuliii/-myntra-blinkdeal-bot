"""
check.py - Entry point for the Myntra BLINKDEAL monitor.

Run on a schedule by GitHub Actions (see .github/workflows/monitor.yml).
Each run starts in a brand-new, empty container, so state.json is the only
thing that remembers whether BLINKDEAL was already active last time we
checked. See README.md -> "How state persists across runs" for how that
file survives between runs despite the container being thrown away each time.

IMPORTANT - read this before relying on it:
This script does not try to defeat Myntra's bot detection (no proxy
rotation, no browser-fingerprint spoofing, no CAPTCHA solving). It sends an
ordinary HTTP request with standard browser-style headers. If Myntra's
anti-bot system blocks the request outright, this script will detect that
it didn't get a usable page, log it, and (after BLOCK_ALERT_THRESHOLD
consecutive occurrences) send you a single Telegram heads-up rather than
fail silently forever. See README.md -> "Known limitation" for details and
what to check if that happens.
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
    """Fetch the product page with retries/timeouts. Returns None if every attempt fails."""
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
    }

    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return requests.get(
                url, headers=headers, timeout=config.REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                "Fetch attempt %s/%s failed: %s", attempt, config.MAX_RETRIES, exc
            )
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    logger.error("All fetch attempts failed: %s", last_exc)
    return None


def looks_like_block_page(html: str) -> bool:
    """Heuristic check for 'this isn't the real product page' responses
    (anti-bot block pages, maintenance pages, CAPTCHA walls, etc.)."""
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
#
# Myntra (like most React/SSR storefronts) embeds page data as JSON inside
# <script> tags rather than as plain text in the HTML. The exact variable
# name can change without notice and can differ between page templates, so
# this tries several known patterns rather than relying on just one.
#
# NOTE: I could not verify these patterns against a live, unblocked response
# (see module docstring / README "Known limitation") -- they're informed
# guesses based on common Myntra/React conventions, not confirmed against
# the real page. Verify and adjust using your own browser (View Source /
# DevTools) the first time you deploy this -- see README "Verifying the
# parser yourself".

JSON_BLOB_PATTERNS = [
    re.compile(r"window\.__myx\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.DOTALL),
]

# When walking parsed JSON, only check string values that live under a key
# that looks coupon/offer-related. This avoids false positives from the
# coupon code appearing somewhere unrelated (e.g. a list of "coupons you
# don't qualify for", a comment, analytics payload, etc.).
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
            logger.info(
                "Coupon '%s' found inside embedded JSON (under a coupon/offer/"
                "deal/promo key).",
                coupon_code,
            )
            return True
    return False


def detect_coupon_in_visible_offer_blocks(html: str, coupon_code: str) -> bool:
    """Lower-confidence fallback used only if no embedded JSON was found at
    all: look inside HTML elements that look like offer/coupon UI (by class
    name), instead of searching the whole page's text indiscriminately."""
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
            logger.info(
                "Coupon '%s' found in an offer/coupon-styled HTML element "
                "(fallback method -- no embedded JSON was found on this page).",
                coupon_code,
            )
            return True
    return False


def coupon_is_active(html: str, coupon_code: str) -> bool:
    blobs = find_json_blobs(html)
    if blobs:
        return detect_coupon_in_json(blobs, coupon_code)

    logger.warning(
        "No recognizable embedded JSON found on the page; falling back to "
        "scanning offer/coupon-styled HTML elements. This is lower-confidence "
        "-- see README 'Known limitation' if this keeps happening, since it "
        "usually means the page didn't render normally."
    )
    return detect_coupon_in_visible_offer_blocks(html, coupon_code)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
        send_telegram_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, "🧪 Test notification from BLINKDEAL Bot! Everything is working!")
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
        logger.warning(
            "Could not get a usable page (unreadable check #%s in a row).",
            state["consecutive_unreadable_checks"],
        )

        if (
            state["consecutive_unreadable_checks"] >= config.BLOCK_ALERT_THRESHOLD
            and not state["block_alert_sent"]
        ):
            try:
                send_telegram_message(
                    config.TELEGRAM_BOT_TOKEN,
                    config.TELEGRAM_CHAT_ID,
                    "BLINKDEAL bot heads-up: the last "
                    f"{state['consecutive_unreadable_checks']} checks in a row "
                    "couldn't read the Myntra page (likely blocked, or the page "
                    "changed). Check the GitHub Actions logs.",
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                    max_retries=config.MAX_RETRIES,
                    backoff_seconds=config.RETRY_BACKOFF_SECONDS,
                )
                state["block_alert_sent"] = True
            except NotifierError as exc:
                logger.error("Could not send block heads-up alert: %s", exc)

        save_state(state)
        return 0

    # We got back something that looks like a real page.
    state["consecutive_unreadable_checks"] = 0
    state["block_alert_sent"] = False

    try:
        active_now = coupon_is_active(response.text, config.COUPON_CODE)
    except Exception:
        # Never let a parsing bug crash the whole run / corrupt state.
        logger.exception("Unexpected error while parsing the page.")
        save_state(state)
        return 1

    was_active = state["blinkdeal_active"]

    if active_now and not was_active:
        logger.info("BLINKDEAL just appeared. Sending alert.")
        try:
            send_telegram_message(
                config.TELEGRAM_BOT_TOKEN,
                config.TELEGRAM_CHAT_ID,
                f"BLINKDEAL is live!\n{config.PRODUCT_URL}",
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                max_retries=config.MAX_RETRIES,
                backoff_seconds=config.RETRY_BACKOFF_SECONDS,
            )
        except NotifierError as exc:
            logger.error("Could not send BLINKDEAL alert: %s", exc)
            # Don't flip blinkdeal_active to True if the alert failed to
            # send -- the next run will see active_now again and retry,
            # instead of silently going quiet for the rest of the deal.
            save_state(state)
            return 1
        state["last_alert_sent_utc"] = utc_now_iso()
        state["blinkdeal_active"] = True
    elif active_now and was_active:
        logger.info("BLINKDEAL still active; alert already sent earlier, skipping.")
    elif not active_now and was_active:
        logger.info("BLINKDEAL no longer detected; resetting state.")
        state["blinkdeal_active"] = False
    else:
        logger.info("BLINKDEAL not active.")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
