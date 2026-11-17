"""
price_tracker.py

Every PRICE_CHECK_INTERVAL_HOURS (default 8), fetches the lowest current
TCGPlayer listing per condition for every saved card and stores it in the
price_history table (see saved.py).

Environment variables (all optional):
  PRICE_TRACKER_ENABLED=0       turn the background thread off (e.g. if you run
                                this file as a Northflank cron job instead)
  PRICE_CHECK_INTERVAL_HOURS    default 8
  PRICE_HISTORY_MAX_ROWS        default 1000000 (roughly 100 MB); 0 = never prune

Run one check by hand (ignores the interval):
  python price_tracker.py
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

import saved
import tcg_scraper

log = logging.getLogger(__name__)

INTERVAL_HOURS = float(os.environ.get("PRICE_CHECK_INTERVAL_HOURS", "8"))
MAX_ROWS = int(os.environ.get("PRICE_HISTORY_MAX_ROWS", "1000000"))
DELAY_BETWEEN_CARDS = 2.0   # seconds; be polite to TCGPlayer
WAKE_EVERY = 600            # seconds between "is it time yet?" checks
STARTUP_DELAY = 60          # let the app finish booting first

_started = False


def snapshot_card(card):
    """Fetch and store one card's lowest listings. card needs product_id,
    printing ('' = all printings) and language."""
    printing = card.get("printing") or ""
    language = card.get("language") or "en"
    result = tcg_scraper.get_current_listings(
        card["product_id"], printing_filter=printing or None, language=language)
    rows = []
    for cond in tcg_scraper.CONDITION_ORDER:
        l = result["by_condition"].get(cond)
        rows.append((cond, l["lowest_total"], l["price"], l["shipping"]) if l
                    else (cond, None, None, None))  # no listing: keep the gap in the history
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    saved.record_prices(card["product_id"], printing, language, checked_at, rows)


def run_once():
    cards = saved.tracked_cards()
    log.info("Price check: %d saved card(s)", len(cards))
    for i, card in enumerate(cards):
        try:
            snapshot_card(card)
        except Exception as e:  # one bad card must not stop the rest
            log.warning("Price check failed for product %s: %s", card.get("product_id"), e)
        if i < len(cards) - 1:
            time.sleep(DELAY_BETWEEN_CARDS)
    try:
        saved.prune_history(MAX_ROWS)
    except Exception as e:
        log.warning("Pruning price history failed: %s", e)


def snapshot_async(card):
    """Called when a card is saved, so its prices show up right away."""
    def work():
        try:
            snapshot_card(card)
        except Exception as e:
            log.warning("Snapshot of newly saved card %s failed: %s", card.get("product_id"), e)
    threading.Thread(target=work, daemon=True).start()


def _loop():
    time.sleep(STARTUP_DELAY)
    while True:
        try:
            # claim_run() lets exactly one gunicorn worker (or restart) run per
            # interval. The 10-minute slack keeps a slow run from pushing the
            # next one past the 8-hour mark.
            if saved.claim_run(INTERVAL_HOURS * 3600 - 600):
                run_once()
        except Exception as e:
            log.warning("Price tracker loop error: %s", e)
        time.sleep(WAKE_EVERY)


def start():
    global _started
    if _started or os.environ.get("PRICE_TRACKER_ENABLED", "1") == "0":
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="price-tracker").start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_once()
