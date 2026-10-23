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
import unicodedata
from urllib.parse import urlencode, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

SEARCH_URL = "https://mp-search-api.tcgplayer.com/v1/search/request"
LISTINGS_URL = "https://mp-search-api.tcgplayer.com/v1/product/{product_id}/listings"
LATEST_SALES_URL = "https://mpapi.tcgplayer.com/v2/product/{product_id}/latestsales"
PRODUCT_PAGE_URL = "https://www.tcgplayer.com/product/{product_id}"
def condition_url(product_id, condition, printing_filter=None):
    """Product page pre-filtered to one condition and, if given, the printing(s):
    ...?Printing=Unlimited+Holofoil&Condition=Lightly+Played"""
    params = []
    names = _printing_names(printing_filter)
    if names:
        params.append(("Printing", "|".join(names)))
    params.append(("Condition", condition))
    return (PRODUCT_PAGE_URL.format(product_id=product_id) + "?"
            + urlencode(params, quote_via=quote_plus))


def product_url(product_id, printing_filter=None):
    """Product page, filtered to the selected printing(s) when there is one."""
    base = PRODUCT_PAGE_URL.format(product_id=product_id)
    names = _printing_names(printing_filter)
    if not names:
        return base
    return base + "?" + urlencode({"Printing": "|".join(names)}, quote_via=quote_plus)


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

# --- Language (English / Japanese) -------------------------------------------
#
# The UI's language toggle picks which TCGPlayer product line is searched and
# which language eBay results are limited to.
#   product_line     TCGPlayer's productLineName filter value (URL-style name)
#   listing_language TCGPlayer's per-listing "language" filter value
#   set_category_id  TCGPlayer categoryId used to fetch set release dates
#                    (Pokemon = 3; Pokemon Japan = 85 -- unverified, see README;
#                    if wrong, "Set order" sorting falls back to setId)
#   ebay_language    eBay's "Language" item-specific value
LANGUAGE_CHOICES = [("en", "English"), ("jp", "Japanese")]
LANGUAGE_SETTINGS = {
    "en": {"product_line": "pokemon", "listing_language": "English",
           "set_category_id": 3, "ebay_language": "English"},
    "jp": {"product_line": "pokemon-japan", "listing_language": "Japanese",
           "set_category_id": 85, "ebay_language": "Japanese"},
}
DEFAULT_LANGUAGE = "en"


def resolve_language(value):
    """Anything unrecognised (or missing) means English."""
    return value if value in LANGUAGE_SETTINGS else DEFAULT_LANGUAGE


# Same idea as FOREIGN_LANGUAGE_PATTERN, but for Japanese searches: Japanese
# is now the wanted language, so it's left out and everything else is dropped.
FOREIGN_LANGUAGE_PATTERN_JP = re.compile(
    r"\b(korean|korea|kor|kr|chinese|china|chn|cn|prc|spanish|spain|italian)\b",
    re.IGNORECASE,
)


def foreign_language_pattern(language=DEFAULT_LANGUAGE):
    """The regex of languages to exclude when `language` is the wanted one."""
    if resolve_language(language) == "jp":
        return FOREIGN_LANGUAGE_PATTERN_JP
    return FOREIGN_LANGUAGE_PATTERN


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


def _is_foreign_language_listing(listing, language=DEFAULT_LANGUAGE):
    text = _listing_caption_text(listing)
    if not text:
        return False
    return bool(foreign_language_pattern(language).search(text))


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


def build_query(name, set_name=None, card_number=None):
    """Combine the description fields into one search string."""
    parts = [name]
    if set_name:
        parts.append(set_name)
    if card_number:
        parts.append(str(card_number))
    return " ".join(p for p in parts if p)


SEARCH_PAGE_SIZE = 24   # what TCGPlayer's own search page requests
SEARCH_MAX_PAGES = 8          # name given: up to 72 candidates per query
SEARCH_MAX_PAGES_BROWSE = 10  # no name (e.g. a whole set): up to 240


def _search_once(session, query, language=DEFAULT_LANGUAGE, max_pages=SEARCH_MAX_PAGES):
    """Run one search query (paged) and return candidate dicts."""
    lang = LANGUAGE_SETTINGS[resolve_language(language)]
    candidates, seen_ids = [], set()
    for page in range(max_pages):
        payload = {
            "algorithm": "sales_exp_fields_experiment",
            "from": page * SEARCH_PAGE_SIZE,
            "size": SEARCH_PAGE_SIZE,
            "filters": {
                # Pokemon (or Pokemon Japan) *cards* only (no sealed product,
                # accessories, etc.)
                "term": {"productLineName": [lang["product_line"]], "productTypeName": ["Cards"]},
                "range": {},
                "match": {},
            },
            "listingSearch": {
                "context": {"cart": {"packages": {}}},
                "filters": {
                    "term": {"sellerStatus": "Live", "channelId": 0, "language": [lang["listing_language"]]},
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


def _name_tokens(text):
    """Lowercase, accent-free word set: 'Pokémon Farfetch\'d-GX' -> {'pokemon','farfetchd','gx'}."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"['\u2019`]", "", text)          # farfetch'd -> farfetchd
    return set(re.sub(r"[^a-z0-9]+", " ", text).split())


def _name_matches(product_name, wanted_name):
    """True if every word of the searched card name appears in the product's name."""
    want = _name_tokens(wanted_name)
    return bool(want) and want <= _name_tokens(product_name)


def _filter_candidates(candidates, set_name, card_number, name=None):
    """
    Narrow results locally by card number and set name. Matching happens
    here rather than in the search text because number formats vary
    ("004/102" vs "4/102"). If a filter would remove everything (e.g. the
    field isn't reported in that format), it's skipped rather than emptying
    the list.
    """
    out = candidates
    if name:
        # Strict on purpose (no fallback): TCGPlayer's fuzzy search matches the
        # text against every field, so keep only cards whose NAME matches.
        out = [c for c in out if _name_matches(c["name"], name)]
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

_set_caches = {}  # language -> {"rows", "fetched_at", "last_attempt"}


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


def _load_sets(language=DEFAULT_LANGUAGE):
    """Cached [{"ids": {key: int}, "date": date|None}, ...] from SetNames.
    On failure, keeps serving the last good (possibly stale) list; returns []
    if there's never been one. Never raises — sorting is a nicety and must
    not break a search. Cached separately per language (English and Japanese
    sets live in different TCGPlayer categories)."""
    language = resolve_language(language)
    _set_cache = _set_caches.setdefault(
        language, {"rows": None, "fetched_at": 0.0, "last_attempt": 0.0})
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
        resp = requests.get(SET_NAMES_URL, params={"categoryId": LANGUAGE_SETTINGS[language]["set_category_id"],
                                    "mpfev": 5616},
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


def annotate_release_dates(candidates, language=DEFAULT_LANGUAGE):
    """Adds set_release (ISO date string) and set_release_ord (sortable int)
    to each candidate whose set is found; leaves them None otherwise."""
    for c in candidates:
        c["set_release"] = None
        c["set_release_ord"] = None
    wanted = {c["set_id"] for c in candidates if c.get("set_id")}
    if not wanted:
        return
    rows = _load_sets(language)
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


# --- Hiding cards that don't have the chosen printing ------------------------
#
# TCGPlayer's search results don't say which printings a card has, so when a
# printing is chosen we ask each candidate's listings. That's one request per
# candidate (cached for hours, run a few at a time, with an overall time cap).
# If a card's printings can't be determined, it is KEPT rather than hidden.

PRINTING_CHECK_WORKERS = 6
PRINTING_CHECK_TIMEOUT_SECONDS = 20


def get_product_printings(product_id, session=None):
    """
    The printings a product currently has live listings for, e.g.
    {"Normal", "Reverse Holofoil"}. Cached. Raises TCGScraperError when it
    can't be determined reliably (the caller then keeps the card).
    """
    product_id = int(float(product_id))
    cached = _cached_printings(product_id)
    if cached is not None:
        return set(cached)
    payload = {
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
    s = session or _session()
    resp = s.post(LISTINGS_URL.format(product_id=product_id), json=payload, timeout=15)
    if resp.status_code != 200:
        raise TCGScraperError(f"Printings check failed ({resp.status_code}) for product {product_id}.")
    try:
        block = resp.json()["results"][0]
        rows = block.get("results") or []
    except (KeyError, IndexError, TypeError, ValueError):
        raise TCGScraperError(f"Unexpected listings response for product {product_id}.")
    facet = {a["value"] for a in (block.get("aggregations") or {}).get("printing", []) or [] if a.get("value")}
    seen = {r["printing"] for r in rows if r.get("printing")}
    if not facet and len(rows) >= LISTINGS_PAGE_SIZE:
        # No printing breakdown and a full page of results: a printing that only
        # appears beyond the cheapest listings can't be ruled out.
        raise TCGScraperError(f"Can't rule out printings for product {product_id}.")
    found = facet | seen
    _store_printings(product_id, found, allow_empty=True)
    return found


def filter_candidates_by_printing(candidates, printing_filter, session=None):
    """
    Keep candidates that have `printing_filter` (an exact printing name).
    Returns (kept, hidden_count, unchecked_count); unchecked ones are kept.
    """
    if not printing_filter or not candidates:
        return list(candidates), 0, 0
    session = session or _session()  # one session (one cookie-priming request) for every check

    known = {}  # product_id -> set of printings; missing = couldn't be checked
    pending = []
    for c in candidates:
        cached = _cached_printings(c["product_id"])
        if cached is not None:
            known[c["product_id"]] = set(cached)
        else:
            pending.append(c["product_id"])

    if pending:
        pool = ThreadPoolExecutor(max_workers=PRINTING_CHECK_WORKERS)
        futures = {pool.submit(get_product_printings, pid, session): pid for pid in pending}
        try:
            for fut in as_completed(futures, timeout=PRINTING_CHECK_TIMEOUT_SECONDS):
                try:
                    known[futures[fut]] = fut.result()
                except Exception as e:
                    log.warning("Printing check for product %s failed: %s", futures[fut], e)
        except FuturesTimeout:
            log.warning("Printing checks timed out; keeping the cards not yet checked")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    kept, hidden, unchecked = [], 0, 0
    for c in candidates:
        printings = known.get(c["product_id"])
        if printings is None:
            kept.append(c)
            unchecked += 1
        elif any(_matches_printing(p, printing_filter) for p in printings):
            kept.append(c)
        else:
            hidden += 1
    return kept, hidden, unchecked


def find_candidates(name, set_name=None, card_number=None, printing_filter=None, report=None,
                    language=DEFAULT_LANGUAGE):
    """
    Search from most to least specific until something matches:
      1. name + set + number   (the full description)
      2. name + set
      3. name only
    Results are always narrowed locally by set/number (see _filter_candidates).
    If printing_filter is given (an exact TCGPlayer printing name such as
    "Reverse Holofoil"), cards without that printing are left out. If `report`
    is a dict, it's filled in with {"hidden": n} so a caller can explain an
    empty result. Returns (candidates, note); note explains broader searches
    and hidden cards.
    """
    s = _session()
    queries = []
    for q in (
        build_query(name, set_name, card_number),
        build_query(name, set_name),
        build_query(name),
    ):
        if q and q not in queries:
            queries.append(q)

    pages = SEARCH_MAX_PAGES if name else SEARCH_MAX_PAGES_BROWSE
    label = printing_label(printing_filter)
    emptied_by_printing = False  # an earlier, more specific search found cards, but none had the printing
    for i, q in enumerate(queries):
        found = _filter_candidates(_search_once(s, q, language, max_pages=pages), set_name, card_number, name)
        if not found:
            continue
        notes = []
        if printing_filter:
            found, hidden, unchecked = filter_candidates_by_printing(found, printing_filter, session=s)
            if report is not None:
                report["hidden"] = max(report.get("hidden", 0), hidden)
            if not found:
                emptied_by_printing = True
                continue  # every card lacked the printing; a broader query may find others
        else:
            hidden = unchecked = 0
        if i and emptied_by_printing:
            notes.append(f'None of the cards for "{queries[0]}" have a {label} printing, '
                         f'so these are results for "{q}".')
        elif i:
            notes.append(f'No results for "{queries[0]}", so these are results for "{q}".')
        if printing_filter:
            if hidden:
                notes.append(f"Showing only cards with a {label} printing ({hidden} hidden).")
            if unchecked:
                notes.append(f"Couldn't check the printings of {unchecked}, so they're included.")
        annotate_release_dates(found, language)
        return found, (" ".join(notes) or None)
    return [], None


def search_products(name, set_name=None, card_number=None):
    """Candidates only (see find_candidates for the explanatory note)."""
    return find_candidates(name, set_name, card_number)[0]


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
    ("standard", "Normal"),
    ("reverse", "Reverse"),
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
    if edition == "1st":
        return "1st Edition Holofoil" if holofoil else "1st Edition"
    if edition in ("standard", "unlimited"):  # "unlimited" kept so old URLs still work
        return "Holofoil|Unlimited Holofoil" if holofoil else "Normal|Unlimited"
    return None


def _printing_names(printing_filter):
    """A printing filter can name several printings, joined by '|'."""
    return [p.strip() for p in (printing_filter or "").split("|") if p.strip()]


def printing_label(printing_filter):
    return " or ".join(_printing_names(printing_filter))


def _matches_printing(listing_printing, printing_filter):
    """Exact (case-insensitive) match against any of the filter's printing
    names. Substring matching doesn't work because "Holofoil" alone is a real
    printing distinct from "Unlimited Holofoil"."""
    if not printing_filter:
        return True
    if not listing_printing:
        return False
    have = listing_printing.strip().lower()
    return any(have == n.lower() for n in _printing_names(printing_filter))


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


PRINTINGS_EMPTY_TTL_SECONDS = 30 * 60  # "no live listings" can change soon; re-check sooner


def _cached_printings(product_id):
    entry = _printings_cache.get(product_id)
    if entry:
        ttl = PRINTINGS_CACHE_TTL_SECONDS if entry["printings"] else PRINTINGS_EMPTY_TTL_SECONDS
        if (time.time() - entry["ts"]) < ttl:
            return entry["printings"]
    return None


def _store_printings(product_id, printings, allow_empty=False):
    if printings or allow_empty:
        _printings_cache[product_id] = {"printings": sorted(set(printings)), "ts": time.time()}


def get_current_listings(product_id, printing_filter=None, language=DEFAULT_LANGUAGE):
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
            payload["filters"]["term"]["printing"] = _printing_names(printing_filter)
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
            if _is_foreign_language_listing(listing, language):
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
                    "url": condition_url(product_id, condition, printing_filter),
                    "title": f"View {condition} listings on TCGPlayer",
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
                log.warning("Printings lookup for product %s got HTTP %s", product_id, r.status_code)
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
        except Exception as e:
            log.warning("Printings lookup for product %s failed: %s", product_id, e)
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
    variant_ids = [PRINTING_VARIANT_IDS[n] for n in _printing_names(printing_filter)
                   if n in PRINTING_VARIANT_IDS]
    if variant_ids:
        # Server-side filter by variant ids; fall back to unfiltered if rejected.
        filtered_payload = dict(payload, variants=variant_ids)
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


def lookup_card(name, set_name=None, card_number=None, printing_filter=None,
                language=DEFAULT_LANGUAGE):
    """
    High-level helper: search, and if there's a single clear match, fetch
    full details. Returns either:
      {"status": "ambiguous", "candidates": [...]}
      {"status": "not_found", "hidden_by_printing": int}  # cards found but lacking the printing
      {"status": "ok", "product": {...}, "listings": {...},
       "available_printings": [...], "sales": {...}}
    """
    report = {}
    candidates, note = find_candidates(name, set_name, card_number,
                                       printing_filter=printing_filter, report=report,
                                       language=language)
    if not candidates:
        return {"status": "not_found", "hidden_by_printing": report.get("hidden", 0)}
    if len(candidates) > 1:
        return {"status": "ambiguous", "candidates": candidates, "note": note}
    result = get_card_details(candidates[0], printing_filter=printing_filter,
                              language=language)
    result["note"] = note
    return result


def get_card_details(candidate, printing_filter=None, language=DEFAULT_LANGUAGE):
    """Given one candidate dict (from search_products), fetch listings + sales."""
    product_id = candidate["product_id"]
    listings_result = get_current_listings(product_id, printing_filter=printing_filter,
                                           language=language)
    sales = get_latest_sales(product_id, printing_filter=printing_filter)
    return {
        "status": "ok",
        "product": candidate,
        "listings": listings_result["by_condition"],
        "available_printings": listings_result["available_printings"],
        "sales": sales,
    }
