"""
tcg_scraper.py

Scrapes TCGPlayer's internal JSON APIs (the same ones tcgplayer.com's own
React frontend calls) to look up a Pokemon card, its current lowest
price+shipping per condition, and recent sales per condition.

IMPORTANT: These are unofficial, reverse-engineered endpoints. TCGPlayer
does not publish a public API for this data (their official Catalog/Pricing
API requires a partner key and doesn't include shipping-inclusive lowest
listings or sales history the way this does). That means:
  - Endpoints/payload shapes can change without notice.
  - Heavy/frequent use may get your IP rate-limited or blocked.
  - This is intended for light personal use, not high-volume querying.

If something breaks, the fastest way to fix it is:
  1. Open tcgplayer.com in a browser, go to a card's page, open DevTools
     Network tab, and search for the term.
  2. Look for XHR requests to mp-search-api.tcgplayer.com or
     mpapi.tcgplayer.com and diff the request/response shape against
     what's below.
"""

import json
import logging
import os
import re
import requests
import time
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

SEARCH_URL = "https://mp-search-api.tcgplayer.com/v1/search/request"
LISTINGS_URL = "https://mp-search-api.tcgplayer.com/v1/product/{product_id}/listings"
LATEST_SALES_URL = "https://mpapi.tcgplayer.com/v2/product/{product_id}/latestsales"
PRODUCT_PAGE_URL = "https://www.tcgplayer.com/product/{product_id}"
SET_NAMES_URL = "https://mpapi.tcgplayer.com/v2/Catalog/SetNames"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
    "Accept": "application/json",
}

# Standard TCGPlayer condition tiers for singles, ordered best to worst.
CONDITION_ORDER = [
    "Near Mint",
    "Lightly Played",
    "Moderately Played",
    "Heavily Played",
    "Damaged",
]


class TCGScraperError(Exception):
    pass


def _optional_number(value):
    """
    Like _unwrap_number, but returns None when there's no usable value
    (missing, null, non-numeric, or <= 0) instead of 0.0. Used for fields
    where "no data" must stay distinguishable from a real number, e.g. a
    card with no market price should sort last rather than as $0.
    """
    if value is None or value == "":
        return None
    n = _unwrap_number(value)
    return n if n > 0 else None


def _unwrap_number(value):
    """
    TCGPlayer's listings API inconsistently represents numbers: sometimes
    as a plain number (54.99), sometimes as an object like
    {"source": "60.0", "parsedValue": 60} (seen on round-number listings).
    This normalizes either form to a plain float.
    """
    if isinstance(value, dict):
        return float(value.get("parsedValue", 0) or 0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# Sellers sometimes list a non-English printing (Japanese/Korean/Chinese)
# under the English language filter, mentioning the real language only in
# a free-text listing comment/caption (e.g. "Japanese Suicune"). Word
# boundaries (\b) keep short codes like "jp"/"kr"/"cn" from matching inside
# unrelated words, though they can still false-positive on a seller name or
# unrelated note that happens to contain one of these tokens — tune the
# list below if that turns out to be a problem in practice.
FOREIGN_LANGUAGE_PATTERN = re.compile(
    r"\b(japanese|japan|jpn|jap|jp|korean|korea|kor|kr|chinese|china|chn|cn|prc|spanish|spain|italian)\b",
    re.IGNORECASE,
)

# Confirmed from a real listings response: a seller's custom caption lives
# at listing["customData"]["title"] and/or listing["customData"]["description"]
# (that's how "Japanese Suicune" showed up for a mislabeled-as-English listing).
# Keeping a couple of generic fallback key names too, in case a different
# product/listing type surfaces free text somewhere else.
_CAPTION_KEY_HINTS = ("comment", "caption", "note")


def _listing_caption_text(listing):
    """Pull together any free-text fields on a listing that might reveal
    the actual (possibly mislabeled) language/edition, so we can check
    them for language mentions."""
    parts = []

    custom_data = listing.get("customData")
    if isinstance(custom_data, dict):
        for key in ("title", "description"):
            value = custom_data.get(key)
            if isinstance(value, str) and value:
                parts.append(value)

    # Fallback: any other top-level field whose key name looks caption-like.
    for key, value in listing.items():
        if isinstance(value, str) and any(hint in key.lower() for hint in _CAPTION_KEY_HINTS):
            parts.append(value)

    return " ".join(parts)


def _is_foreign_language_listing(listing):
    text = _listing_caption_text(listing)
    if not text:
        return False
    return bool(FOREIGN_LANGUAGE_PATTERN.search(text))


def _session():
    """
    Create a session and prime it with cookies from a normal page visit.
    TCGPlayer's search/listings/sales APIs appear to expect at least a
    session cookie (load-balancer affinity, visitor key, etc.) set by
    visiting the site itself — calling the JSON endpoints cold, with no
    cookies at all, is a plausible source of 400s.
    """
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://www.tcgplayer.com/", timeout=10)
    except requests.RequestException:
        pass  # if priming fails, fall through and try the API call anyway
    # Force the US market/currency regardless of what the priming request's
    # geo-IP lookup would otherwise default to (e.g. CA for a Canadian IP).
    s.cookies.set("setting", "CD=US&M=1", domain=".tcgplayer.com")
    # Optional: browse as a logged-in (dummy) account, which TCGPlayer may
    # show more sales/listings to. Set the TCG_AUTH_TICKET environment
    # variable to that account's "TCGAuthTicket_Production" cookie value
    # (see README). Never hardcode it in this file.
    auth_ticket = os.environ.get("TCG_AUTH_TICKET", "").strip()
    if auth_ticket:
        s.cookies.set("TCGAuthTicket_Production", auth_ticket, domain=".tcgplayer.com")
    return s


def build_query(name, set_name=None, card_number=None, holo_type=None):
    """Combine the description fields into one search string."""
    parts = [name]
    if set_name:
        parts.append(set_name)
    if card_number:
        parts.append(str(card_number))
    if holo_type:
        parts.append(holo_type)
    return " ".join(p for p in parts if p)


SEARCH_PAGE_SIZE = 24   # what TCGPlayer's own search page requests
SEARCH_MAX_PAGES = 3    # up to 72 candidates per query


def _search_once(session, query):
    """Run one search query (paged) and return candidate dicts."""
    candidates, seen_ids = [], set()
    for page in range(SEARCH_MAX_PAGES):
        payload = {
            "algorithm": "sales_exp_fields_experiment",
            "from": page * SEARCH_PAGE_SIZE,
            "size": SEARCH_PAGE_SIZE,
            "filters": {
                # Pokemon *cards* only (no sealed product, accessories, etc.)
                "term": {"productLineName": ["pokemon"], "productTypeName": ["Cards"]},
                "range": {},
                "match": {},
            },
            "listingSearch": {
                "context": {"cart": {"packages": {}}},
                "filters": {
                    "term": {"sellerStatus": "Live", "channelId": 0, "language": ["English"]},
                    "range": {"quantity": {"gte": 1}},
                    "exclude": {"channelExclusion": 0},
                },
            },
            "context": {"cart": {"packages": {}}, "shippingCountry": "US"},
            "settings": {"useFuzzySearch": True, "didYouMean": {}},
            "sort": {"field": "market-price", "order": "desc"},
        }
        params = {"q": query, "isList": "false", "mpfev": "5580"}
        resp = session.post(SEARCH_URL, params=params, json=payload, timeout=15)
        if resp.status_code != 200:
            raise TCGScraperError(
                f"Search request failed ({resp.status_code}). "
                f"TCGPlayer's search endpoint or payload shape may have changed. "
                f"Response body: {resp.text[:800]}"
            )
        data = resp.json()
        try:
            block = data["results"][0]
            results = block["results"]
        except (KeyError, IndexError, TypeError):
            raise TCGScraperError(
                "Unexpected search response shape from TCGPlayer — "
                "the API contract likely changed. Raw keys: " + str(list(data.keys()))
            )

        for r in results:
            # productId can arrive as an int, a float (480498.0), or a
            # {"parsedValue": ...} object; downstream URLs need a plain int.
            product_id = int(_unwrap_number(r.get("productId")))
            if not product_id or product_id in seen_ids:
                continue
            seen_ids.add(product_id)
            candidates.append({
                "product_id": product_id,
                "name": r.get("productName", ""),
                "set_name": r.get("setName", ""),
                "number": (r.get("customAttributes") or {}).get("number", ""),
                "rarity": r.get("rarityName", ""),
                # For the picker page's sort dropdown.
                "market_price": _optional_number(r.get("marketPrice")),
                "set_id": (lambda n: int(n) if n else None)(_optional_number(r.get("setId"))),
                "image_url": f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_200w.jpg",
                "url": PRODUCT_PAGE_URL.format(product_id=product_id),
            })

        total = _unwrap_number(block.get("totalResults", 0))
        if len(results) < SEARCH_PAGE_SIZE or (total and (page + 1) * SEARCH_PAGE_SIZE >= total):
            break
    return candidates


def _norm_number(value):
    """'004/102' -> '4', '4' -> '4', 'TG05/TG30' -> 'tg05'."""
    n = str(value or "").split("/")[0].strip().lower()
    return str(int(n)) if n.isdigit() else n


def _filter_candidates(candidates, set_name, card_number):
    """
    Narrow results locally by card number and set name. Matching happens
    here rather than in the search text because number formats vary
    ("004/102" vs "4/102"). If a filter would remove everything (e.g. the
    field isn't reported in that format), it's skipped rather than emptying
    the list.
    """
    out = candidates
    if card_number:
        want = _norm_number(card_number)
        narrowed = [c for c in out if _norm_number(c["number"]) == want]
        if narrowed:
            out = narrowed
    if set_name:
        want = set_name.strip().lower()
        narrowed = [
            c for c in out
            if c["set_name"] and (want in c["set_name"].lower() or c["set_name"].lower() in want)
        ]
        if narrowed:
            out = narrowed
    return out


# --- Set release dates (for "Set order" sorting) ---------------------------
#
# The search response only gives a numeric setId, which isn't chronological.
# TCGPlayer's SetNames catalog lists every Pokemon set with a releaseDate, so
# we look dates up there. The set list only changes about monthly, so it's
# cached in memory instead of being fetched on every search.

SET_CACHE_TTL_SECONDS = 24 * 60 * 60        # refresh the set list daily
SET_RETRY_AFTER_FAILURE_SECONDS = 5 * 60    # don't hammer TCGPlayer if it's failing
# Which field in a SetNames row equals the search result's setId isn't
# something I could confirm without a real response, so we try these and keep
# whichever one actually overlaps the setIds in the current search results.
_SET_ID_KEYS = ("setNameId", "setId", "id")

_set_cache = {"rows": None, "fetched_at": 0.0, "last_attempt": 0.0}


def _parse_release_date(value):
    """Best-effort conversion of a releaseDate value to a datetime.date."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        ts = ts / 1000.0 if ts > 1e11 else ts  # milliseconds -> seconds
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)  # 2001-09-21 / 2001-09-21T00:00:00Z
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _find_set_rows(obj, depth=0):
    """Locate the list of set dicts (the ones with a releaseDate) anywhere in
    the response, whatever it's wrapped in ({"results": [...]}, a bare list...)."""
    if depth > 4:
        return None
    if isinstance(obj, list):
        if any(isinstance(x, dict) and "releaseDate" in x for x in obj[:10]):
            return obj
        for x in obj:
            found = _find_set_rows(x, depth + 1)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (list, dict)):
                found = _find_set_rows(v, depth + 1)
                if found:
                    return found
    return None


def _load_sets():
    """Cached [{"ids": {key: int}, "date": date|None}, ...] from SetNames.
    On failure, keeps serving the last good (possibly stale) list; returns []
    if there's never been one. Never raises — sorting is a nicety and must
    not break a search."""
    now = time.time()
    rows = _set_cache["rows"]
    if rows is not None and (now - _set_cache["fetched_at"]) < SET_CACHE_TTL_SECONDS:
        return rows
    if rows is None and (now - _set_cache["last_attempt"]) < SET_RETRY_AFTER_FAILURE_SECONDS \
            and _set_cache["last_attempt"]:
        return []
    _set_cache["last_attempt"] = now
    try:
        headers = {k: v for k, v in HEADERS.items() if k != "Content-Type"}
        resp = requests.get(SET_NAMES_URL, params={"categoryId": 3, "mpfev": 5616},
                            headers=headers, timeout=10)
        resp.raise_for_status()
        raw = _find_set_rows(resp.json())
        if not raw:
            raise ValueError("no set list with releaseDate found in response")
        parsed = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            ids = {}
            for key in _SET_ID_KEYS:
                n = _optional_number(item.get(key))
                if n:
                    ids[key] = int(n)
            parsed.append({"ids": ids, "date": _parse_release_date(item.get("releaseDate"))})
        _set_cache.update(rows=parsed, fetched_at=now)
        return parsed
    except Exception as e:  # network, JSON, shape — all just mean "no dates this time"
        log.warning("Could not load set release dates (%s); %s", e,
                    "using stale list" if rows is not None else "set order will fall back to setId")
        return rows or []


def annotate_release_dates(candidates):
    """Adds set_release (ISO date string) and set_release_ord (sortable int)
    to each candidate whose set is found; leaves them None otherwise."""
    for c in candidates:
        c["set_release"] = None
        c["set_release_ord"] = None
    wanted = {c["set_id"] for c in candidates if c.get("set_id")}
    if not wanted:
        return
    rows = _load_sets()
    if not rows:
        return
    best_key, best_hits = None, 0
    for key in _SET_ID_KEYS:
        have = {r["ids"][key] for r in rows if key in r["ids"]}
        if len(wanted & have) > best_hits:
            best_key, best_hits = key, len(wanted & have)
    if best_key is None:
        log.warning("No SetNames id field matched the search results' setIds %s; "
                    "sample row ids: %s", sorted(wanted)[:5], [r["ids"] for r in rows[:3]])
        return
    dates = {r["ids"][best_key]: r["date"] for r in rows if best_key in r["ids"] and r["date"]}
    for c in candidates:
        d = dates.get(c.get("set_id"))
        if d:
            c["set_release"] = d.isoformat()
            c["set_release_ord"] = d.toordinal()


def find_candidates(name, set_name=None, card_number=None, holo_type=None):
    """
    Search from most to least specific until something matches:
      1. name + set + number + holo type   (the full description)
      2. name + set
      3. name only
    Results are always narrowed locally by set/number (see _filter_candidates).
    Returns (candidates, note); note explains when a broader search was used.
    """
    s = _session()
    queries = []
    for q in (
        build_query(name, set_name, card_number, holo_type),
        build_query(name, set_name),
        build_query(name),
    ):
        if q not in queries:
            queries.append(q)

    for i, q in enumerate(queries):
        found = _filter_candidates(_search_once(s, q), set_name, card_number)
        if found:
            note = None if i == 0 else (
                f'No results for "{queries[0]}", so these are results for "{q}".'
            )
            annotate_release_dates(found)
            return found, note
    return [], None


def search_products(name, set_name=None, card_number=None, holo_type=None):
    """Candidates only (see find_candidates for the explanatory note)."""
    return find_candidates(name, set_name, card_number, holo_type)[0]


# From TCGPlayer's categoryfilters endpoint (Pokemon, categoryId=3). Static
# enough to hardcode; names are what listings report in their "printing"
# field, ids are what the sales endpoint's "variants" filter takes.
PRINTING_VARIANT_IDS = {
    "Normal": 10,
    "Holofoil": 11,
    "Reverse Holofoil": 77,
    "1st Edition": 78,
    "1st Edition Holofoil": 79,
    "Unlimited": 122,
    "Unlimited Holofoil": 123,
}

EDITION_CHOICES = [
    ("", "Any printing"),
    ("standard", "Standard (no edition)"),
    ("reverse", "Reverse"),
    ("unlimited", "Unlimited"),
    ("1st", "1st Edition"),
]


def resolve_printing(edition, holofoil=True):
    """
    Turn the UI's edition picklist + Holofoil checkbox into TCGPlayer's exact
    printing name, or None for "no filter".
      Reverse            -> "Reverse Holofoil" (reverse is always holo)
      Unlimited          -> "Unlimited Holofoil" / "Unlimited"
      1st Edition        -> "1st Edition Holofoil" / "1st Edition"
      Standard           -> "Holofoil" / "Normal"  (modern cards, no edition)
    """
    if not edition:
        return None
    if edition == "reverse":
        return "Reverse Holofoil"
    base = {"unlimited": "Unlimited", "1st": "1st Edition"}.get(edition)
    if base:
        return f"{base} Holofoil" if holofoil else base
    if edition == "standard":
        return "Holofoil" if holofoil else "Normal"
    return None


def _matches_printing(listing_printing, printing_filter):
    """Exact (case-insensitive) match. Substring matching no longer works
    now that "Holofoil" alone is a real printing distinct from
    "Unlimited Holofoil"."""
    if not printing_filter:
        return True
    if not listing_printing:
        return False
    return printing_filter.strip().lower() == listing_printing.strip().lower()


LISTINGS_PAGE_SIZE = 50
# Listings come back cheapest-first, so deeper pages are only needed when the
# cheapest 50 don't include some condition/printing. Cap the paging so a
# lookup on a hugely popular card can't turn into dozens of requests.
LISTINGS_MAX_PAGES = 6


# Printings a product has (e.g. ["Normal", "Reverse Holofoil"]), remembered so
# the results page's Printing dropdown can list every option even while one
# printing is selected. Printings per product almost never change, so this is
# only a few hours' cache, mainly to avoid re-learning it on every switch.
PRINTINGS_CACHE_TTL_SECONDS = 6 * 60 * 60
_printings_cache = {}  # product_id -> {"printings": [...], "ts": float}


def _cached_printings(product_id):
    entry = _printings_cache.get(product_id)
    if entry and (time.time() - entry["ts"]) < PRINTINGS_CACHE_TTL_SECONDS:
        return entry["printings"]
    return None


def _store_printings(product_id, printings):
    if printings:
        _printings_cache[product_id] = {"printings": sorted(set(printings)), "ts": time.time()}


def get_current_listings(product_id, printing_filter=None):
    """
    Fetch current live listings for a product, grouped by condition, with
    the lowest total (price + shipping) per condition.

    Listings are fetched cheapest-first and paged until every condition has a
    match (after language/printing filtering), the results run out, or
    LISTINGS_MAX_PAGES is hit. Without paging, a condition or printing whose
    listings are all pricier than the cheapest 50 would wrongly show as
    "No live listings".

    Some products (mainly older sets) mix multiple printings under one
    product page. Pass printing_filter as an exact TCGPlayer printing name
    (e.g. "1st Edition Holofoil", "Reverse Holofoil", "Normal"); None
    considers all printings together.

    Returns a dict:
      {
        "by_condition": {condition: {...} or None, ...},
        "available_printings": [str, ...],   # every printing the product has,
                                             # even when printing_filter is set
      }
    """
    product_id = int(float(product_id))
    url = LISTINGS_URL.format(product_id=product_id)
    base_payload = {
        "filters": {
            "term": {"sellerStatus": "Live", "channelId": 0},
            "range": {"quantity": {"gte": 1}},
            "exclude": {"channelExclusion": 0},
        },
        "from": 0,
        "size": LISTINGS_PAGE_SIZE,
        "sort": {"field": "price+shipping", "order": "asc"},
        "context": {"cart": {"packages": {}}, "shippingCountry": "US"},
    }
    s = _session()

    def fetch_page(page, with_printing):
        payload = json.loads(json.dumps(base_payload))
        payload["from"] = page * LISTINGS_PAGE_SIZE
        if with_printing:
            # Server-side filter. Confirmed against the request TCGPlayer's
            # own product page sends when a printing is selected:
            # filters.term.printing = ["<exact printing name>"]. If it's ever
            # rejected we fall back to unfiltered requests plus the
            # client-side check below.
            payload["filters"]["term"]["printing"] = [printing_filter]
        return s.post(url, json=payload, timeout=15)

    server_filtered = bool(printing_filter)
    resp = fetch_page(0, server_filtered)
    if resp.status_code != 200 and server_filtered:
        server_filtered = False
        resp = fetch_page(0, False)

    lowest_by_condition = {c: None for c in CONDITION_ORDER}
    available_printings = set()
    page = 0
    while True:
        if resp.status_code != 200:
            raise TCGScraperError(
                f"Listings request failed ({resp.status_code}) for product {product_id}. "
                f"Response body: {resp.text[:800]}"
            )
        data = resp.json()
        try:
            block = data["results"][0]
            results = block["results"]
        except (KeyError, IndexError, TypeError):
            raise TCGScraperError("Unexpected listings response shape from TCGPlayer.")

        # Informational: printings present (from the aggregation when
        # available, plus what we actually saw).
        for agg in (block.get("aggregations") or {}).get("printing", []) or []:
            if agg.get("value"):
                available_printings.add(agg["value"])
        for listing in results:
            if listing.get("printing"):
                available_printings.add(listing["printing"])

        for listing in results:
            condition = listing.get("condition")
            if condition not in lowest_by_condition:
                continue
            if not _matches_printing(listing.get("printing"), printing_filter):
                continue
            if _is_foreign_language_listing(listing):
                continue
            price = _unwrap_number(listing.get("price", 0))
            shipping = _unwrap_number(listing.get("shippingPrice", 0))
            total = price + shipping
            current = lowest_by_condition[condition]
            if current is None or total < current["lowest_total"]:
                lowest_by_condition[condition] = {
                    "lowest_total": round(total, 2),
                    "price": round(price, 2),
                    "shipping": round(shipping, 2),
                    "seller": listing.get("sellerName", "unknown"),
                }

        page += 1
        total_results = _unwrap_number(block.get("totalResults", 0))
        done = (
            len(results) < LISTINGS_PAGE_SIZE
            or all(v is not None for v in lowest_by_condition.values())
            or page >= LISTINGS_MAX_PAGES
            or (total_results and page * LISTINGS_PAGE_SIZE >= total_results)
        )
        if done:
            break
        resp = fetch_page(page, server_filtered)

    def discover_all_printings():
        """One unfiltered request, just to learn which printings exist."""
        try:
            r = fetch_page(0, False)
            if r.status_code != 200:
                return set()
            blk = r.json()["results"][0]
            found = set()
            for agg in (blk.get("aggregations") or {}).get("printing", []) or []:
                if agg.get("value"):
                    found.add(agg["value"])
            for listing in blk.get("results", []) or []:
                if listing.get("printing"):
                    found.add(listing["printing"])
            return found
        except Exception:
            return set()  # the dropdown is a nicety; never fail the lookup over it

    if not printing_filter or not server_filtered:
        # We saw the product unfiltered (or filtered it ourselves), so this is
        # the complete list.
        _store_printings(product_id, available_printings)
    else:
        # The server-side filter can narrow TCGPlayer's printing breakdown to
        # just the selected printing, leaving nothing to switch to. Use the
        # remembered full list, or learn it with one extra request.
        full = _cached_printings(product_id)
        if full is None:
            full = discover_all_printings() if len(available_printings) <= 1 else set(available_printings)
            _store_printings(product_id, full)
        available_printings |= set(full or ())

    return {
        "by_condition": lowest_by_condition,
        "available_printings": sorted(available_printings),
    }


def get_latest_sales(product_id, printing_filter=None, limit_per_condition=5):
    """
    Fetch recent completed sales for a product, grouped by condition.

    printing_filter works the same as in get_current_listings. Note: I
    haven't seen a real latestsales response to confirm the exact field
    name TCGPlayer uses for printing on a sale record — this checks a
    couple of likely field names ("printing", "variant"). If sales aren't
    actually being filtered, that's the first thing to check against a
    real response.

    Returns: {condition: [ {date, price, shipping, total}, ... ]}
    """
    product_id = int(float(product_id))
    url = LATEST_SALES_URL.format(product_id=product_id)
    payload = {
        "conditions": [],
        "variants": [],
        "languages": [],
        "listingType": "All",
    }
    s = _session()
    resp = None
    variant_id = PRINTING_VARIANT_IDS.get(printing_filter) if printing_filter else None
    if variant_id:
        # Server-side filter by variant id; fall back to unfiltered if rejected.
        filtered_payload = dict(payload, variants=[variant_id])
        resp = s.post(url, json=filtered_payload, timeout=15)
        if resp.status_code != 200:
            resp = None
    server_filtered = resp is not None
    if resp is None:
        resp = s.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        raise TCGScraperError(
            f"Latest sales request failed ({resp.status_code}) for product {product_id}. "
            f"Response body: {resp.text[:800]}"
        )

    data = resp.json()
    sales = data.get("data", [])

    by_condition = {c: [] for c in CONDITION_ORDER}
    for sale in sales:
        condition = sale.get("condition")
        if condition not in by_condition:
            continue
        if len(by_condition[condition]) >= limit_per_condition:
            continue
        # Only filter client-side if the server didn't already, and only if
        # the sale record actually reports a printing (unconfirmed field).
        sale_printing = sale.get("printing") or sale.get("variant")
        if not server_filtered and sale_printing and not _matches_printing(sale_printing, printing_filter):
            continue
        # Using the same unwrap helper here defensively — the sales endpoint
        # hasn't been confirmed against a real response, but the listings
        # endpoint's plain-vs-wrapped-number inconsistency makes it worth
        # guarding against here too.
        price = _unwrap_number(sale.get("purchasePrice", 0))
        shipping = _unwrap_number(sale.get("shippingPrice", 0))
        usd_total = round(price + shipping, 2)
        by_condition[condition].append({
            "date": sale.get("orderDate", ""),
            "price": round(price, 2),
            "shipping": round(shipping, 2),
            "usd_total": usd_total,
            "total": usd_total,
        })
    return by_condition


def lookup_card(name, set_name=None, card_number=None, holo_type=None, printing_filter=None):
    """
    High-level helper: search, and if there's a single clear match, fetch
    full details. Returns either:
      {"status": "ambiguous", "candidates": [...]}
      {"status": "not_found"}
      {"status": "ok", "product": {...}, "listings": {...},
       "available_printings": [...], "sales": {...}}
    """
    candidates, note = find_candidates(name, set_name, card_number, holo_type)
    if not candidates:
        return {"status": "not_found"}
    if len(candidates) > 1:
        return {"status": "ambiguous", "candidates": candidates, "note": note}
    result = get_card_details(candidates[0], printing_filter=printing_filter)
    result["note"] = note
    return result


def get_card_details(candidate, printing_filter=None):
    """Given one candidate dict (from search_products), fetch listings + sales."""
    product_id = candidate["product_id"]
    listings_result = get_current_listings(product_id, printing_filter=printing_filter)
    sales = get_latest_sales(product_id, printing_filter=printing_filter)
    return {
        "status": "ok",
        "product": candidate,
        "listings": listings_result["by_condition"],
        "available_printings": listings_result["available_printings"],
        "sales": sales,
    }
