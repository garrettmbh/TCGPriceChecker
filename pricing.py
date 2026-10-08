"""
pricing.py

Currency conversion, NY sales tax, and recency-weighted sales averages for
the results page. Kept separate from tcg_scraper.py, which only talks to
TCGPlayer — this module knows nothing about TCGPlayer's API shapes.
"""
import time
import math
from datetime import datetime, timezone

import requests

NY_SALES_TAX_RATE = 1.0875  # New York State tax, applied to the CAD total

# Frankfurter (api.frankfurter.dev) is a free, keyless, ECB-backed FX API.
FX_URL = "https://api.frankfurter.dev/v1/latest"
FX_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours — no need to hit the FX API every page load
FX_FALLBACK_USD_TO_CAD = 1.37  # only used if the live fetch fails AND no cached rate exists yet

_fx_cache = {"rate": None, "fetched_at": 0.0}


def get_usd_to_cad_rate():
    """
    Returns a float: how many CAD one USD buys right now. Cached for
    FX_CACHE_TTL_SECONDS. Falls back to the last known rate (even if stale)
    if a refresh fails, or to FX_FALLBACK_USD_TO_CAD if there's never been
    a successful fetch — a currency-rate hiccup shouldn't take the whole
    page down.
    """
    now = time.time()
    if _fx_cache["rate"] is not None and (now - _fx_cache["fetched_at"]) < FX_CACHE_TTL_SECONDS:
        return _fx_cache["rate"]
    try:
        resp = requests.get(FX_URL, params={"base": "USD", "symbols": "CAD"}, timeout=10)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["CAD"])
        _fx_cache["rate"] = rate
        _fx_cache["fetched_at"] = now
        return rate
    except Exception:
        return _fx_cache["rate"] if _fx_cache["rate"] is not None else FX_FALLBACK_USD_TO_CAD


def usd_to_totals(usd_amount, rate=None):
    """
    Convert one USD amount into the three figures the UI shows:
      usd   - as given (listing price + shipping, or a sale's price + shipping)
      cad   - usd * live USD->CAD rate
      total - cad * NY_SALES_TAX_RATE
    """
    if rate is None:
        rate = get_usd_to_cad_rate()
    cad = usd_amount * rate
    total = cad * NY_SALES_TAX_RATE
    return {"usd": round(usd_amount, 2), "cad": round(cad, 2), "total": round(total, 2)}


def days_since(date_str):
    """
    Parse a TCGPlayer-style date string (e.g. '2026-09-01T00:00:00Z') and
    return whole days between then and now (UTC). Returns None if the
    string is missing or unparsable, rather than raising.
    """
    if not date_str:
        return None
    try:
        cleaned = date_str.replace("Z", "+00:00")
        sale_dt = datetime.fromisoformat(cleaned)
        if sale_dt.tzinfo is None:
            sale_dt = sale_dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - sale_dt
        return max(0, delta.days)
    except (ValueError, TypeError):
        return None


# def weighted_average(totals_with_days):
#     """
#     totals_with_days: list of (total, days_ago) tuples; days_ago may be None.

#     Weight = 1 / (days_ago + 1): a sale from today gets weight 1, one from
#     9 days ago gets weight 0.1, and so on — more recent sales count more.
#     This exact curve is a judgment call (the request said "reduce weight
#     each day" but not by how much); swap it out if it doesn't match
#     expectations once you see it against real sales.

#     A sale with no parseable date is treated as "today" (weight 1) rather
#     than dropped, so a date-parsing gap doesn't silently remove data.

#     Returns None for an empty list.
#     """
#     if not totals_with_days:
#         return None
#     weight_sum = 0.0
#     weighted_sum = 0.0
#     for total, days_ago in totals_with_days:
#         weight = 1.0 / ((days_ago or 0) + 1)
#         weight_sum += weight
#         weighted_sum += weight * total
#     return weighted_sum / weight_sum if weight_sum else None


def weighted_average(totals_with_days):
    if not totals_with_days:
        return None

    DECAY_DAYS = 90.0
    WEIGHT_AT_DECAY = 0.15

    weight_sum = 0.0
    weighted_sum = 0.0

    for total, days_ago in totals_with_days:
        days_ago = max(days_ago or 0, 0)

        # A sale 90 days old has 15% of the weight
        # of a sale from today.
        weight = WEIGHT_AT_DECAY ** (days_ago / DECAY_DAYS)

        weight_sum += weight
        weighted_sum += weight * total

    return weighted_sum / weight_sum if weight_sum else None


def simple_average(totals):
    return sum(totals) / len(totals) if totals else None


def enrich_listings(listings, rate=None):
    """
    Mutates the {condition: {...} or None} dict from tcg_scraper.
    get_current_listings, adding usd/cad/total to each present entry
    (converted from the existing "lowest_total" USD figure). Returns the
    same dict for convenience.
    """
    if rate is None:
        rate = get_usd_to_cad_rate()
    for entry in listings.values():
        if not entry:
            continue
        conv = usd_to_totals(entry["lowest_total"], rate)
        entry["usd"] = conv["usd"]
        entry["cad"] = conv["cad"]
        entry["total"] = conv["total"]
    return listings


def enrich_sales(sales, rate=None):
    """
    Mutates the {condition: [sale, ...]} dict from tcg_scraper.get_latest_sales.

    For each sale, the original USD price + shipping is preserved in
    "usd_total". The presentation fields are then calculated from that
    immutable source value:

      usd   - original USD price + shipping
      cad   - USD converted to CAD
      total - CAD total including NY sales tax

    Also computes, per condition, the weighted and simple averages of the
    CAD+tax Total figure, and attaches each sale's diff from the weighted
    average.

    Returns (sales, stats) where stats is
    {condition: {"weighted_avg": float, "simple_avg": float} or None}.
    """
    if rate is None:
        rate = get_usd_to_cad_rate()

    stats = {}

    for condition, sale_list in sales.items():
        totals_with_days = []

        for sale in sale_list:
            # Always use the original USD value.
            #
            # The fallback to "total" keeps this compatible with any older
            # cached/raw sale objects that don't yet have "usd_total".
            usd_total = sale.get("usd_total", sale["total"])

            # Preserve the raw USD source value permanently.
            sale["usd_total"] = round(usd_total, 2)

            conv = usd_to_totals(usd_total, rate)

            sale["usd"] = conv["usd"]
            sale["cad"] = conv["cad"]
            sale["total"] = conv["total"]
            sale["days_ago"] = days_since(sale.get("date"))

            totals_with_days.append(
                (sale["total"], sale["days_ago"])
            )

        if totals_with_days:
            w_avg = weighted_average(totals_with_days)
            s_avg = simple_average([t for t, _ in totals_with_days])

            stats[condition] = {
                "weighted_avg": round(w_avg, 2),
                "simple_avg": round(s_avg, 2),
            }

            for sale in sale_list:
                sale["diff"] = round(
                    w_avg - sale["total"],
                    2,
                )
        else:
            stats[condition] = None

    return sales, stats


def add_listing_diffs(listings, sales_stats):
    """
    Adds the difference between each condition's lowest current listing
    and its weighted recent-sales average.

    diff = lowest_current_listing_total - weighted_avg

    Positive diff means the current listing is above the weighted average.
    Negative diff means the current listing is below the weighted average.
    """
    for condition, listing in listings.items():
        if not listing:
            continue

        # Always initialize the key so Jinja never encounters
        # a listing dictionary without "diff".
        listing["diff"] = None

        stats = sales_stats.get(condition)

        if not stats:
            continue

        listing["diff"] = round(
            listing["total"] - stats["weighted_avg"],
            2,
        )

    return listings
