"""
ebay.py

Lowest current eBay prices per card condition, from eBay's Browse API — free
with a developer key (the same API the eBay scanner project uses).

What eBay's APIs do and don't offer (checked October 2026):

  * Browse API ........ ACTIVE listings only. Free; default quota is 5,000
                        calls/day per app.
  * Sold items ........ NOT available for free. The Finding API's
                        findCompletedItems was shut down in February 2025, and
                        the Marketplace Insights API (sold items, 90 days) is
                        limited to partners eBay approves. So this module
                        returns no recent sales; see SOLD_UNAVAILABLE_MESSAGE.
  * Card condition .... since late 2023, search results only say "Ungraded" or
                        "Graded". The real Near Mint / Lightly Played / ... is
                        a "Card Condition" aspect on each listing, which search
                        can FILTER by but doesn't return per item. So instead
                        of one search that we'd have to classify afterwards,
                        we run one search per Card Condition value (usually 4),
                        each sorted by price, and take the cheapest of each.
                        If eBay doesn't offer that filter for a query, we fall
                        back to a single price-sorted search and guess the
                        condition from listing titles (flagged as approximate).

Configuration (environment variables):
    EBAY_CLIENT_ID       your Production App ID (Client ID)
    EBAY_CLIENT_SECRET   your Production Cert ID (Client Secret)
    EBAY_MARKETPLACE_ID  optional, default EBAY_US
    EBAY_DELIVERY_COUNTRY optional, default CA; where eBay quotes shipping to
    EBAY_DELIVERY_ZIP    optional; eBay ignored a Canadian postal code in testing,
                         so leave it unset
"""

import base64
import copy
import logging
import os
import re
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import requests

import pricing
import tcg_scraper

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_SITE = "https://www.ebay.ca"  # every link we build or rewrite points here
CCG_CATEGORY_ID = "183454"  # Toys & Hobbies > Collectible Card Games > CCG Individual Cards

# Row labels, in display order.
CONDITION_ORDER = [
    "Near Mint",
    "Lightly Played",
    "Moderately Played",
    "Heavily Played / Damaged",
    "Not Specified",
]

CACHE_TTL_SECONDS = 10 * 60   # a lookup costs several API calls; be gentle on the daily quota
MAX_SHIPPING_LOOKUPS = 4     # per condition: getItem calls to price 'calculated' shipping
PER_CONDITION_LIMIT = 50      # cheapest N per condition (some get dropped by the language filter)
FALLBACK_LIMIT = 200          # for the single-query fallback

SOLD_UNAVAILABLE_MESSAGE = (
    "eBay sold-item data isn't available through eBay's free API. The old "
    "completed-items search was shut down in 2025, and the replacement is "
    "restricted to eBay-approved partners."
)

GRADED_CONDITION_ID = "2750"  # eBay's "Graded" condition for trading cards (ungraded is 4000)

# Fan-made / non-official cards. Matched against the listing title only.
EXCLUDED_TITLE_PATTERN = re.compile(r"\b(fan[\s-]?made|proxy|proxies|custom\w*)\b", re.I)

JAPAN_TAX_RATE = 1.0     # seller in Japan: no tax
DEFAULT_TAX_RATE = 1.13  # seller anywhere else: 13%

# Printing -> title checks. eBay's keyword search can match "1st edition" in
# places other than the title (item specifics, description), so the title is
# checked too.
FIRST_EDITION_TITLE = re.compile(r"\b(1st|first)[\s-]*ed", re.I)
REVERSE_TITLE = re.compile(r"\b(reverse|rev)\b", re.I)


def tax_rate_for_listing(listing):
    """JP seller -> 0% tax; any other (or unknown) seller location -> 13%."""
    if (listing.get("seller_country") or "").upper() == "JP":
        return JAPAN_TAX_RATE
    return DEFAULT_TAX_RATE


def _printing_matches_title(title, printing):
    """
    1st Edition printings need '1st ed...' in the title; Reverse needs
    'reverse'. Any other printing drops titles that say 1st edition.
    No printing selected = no check.
    """
    names = [n.lower() for n in tcg_scraper._printing_names(printing)]
    if not names:
        return True
    if any(n.startswith("1st edition") for n in names):
        return bool(FIRST_EDITION_TITLE.search(title))
    if any(n == "reverse holofoil" for n in names):
        return bool(REVERSE_TITLE.search(title)) and not FIRST_EDITION_TITLE.search(title)
    return not FIRST_EDITION_TITLE.search(title)


class EbayError(Exception):
    pass


class EbayNotConfigured(EbayError):
    pass


# --- configuration ----------------------------------------------------------

def _client_id():
    return os.environ.get("EBAY_CLIENT_ID", "").strip()


def _client_secret():
    return os.environ.get("EBAY_CLIENT_SECRET", "").strip()


def _marketplace():
    return os.environ.get("EBAY_MARKETPLACE_ID", "").strip() or "EBAY_US"


def _delivery_zip():
    return os.environ.get("EBAY_DELIVERY_ZIP", "").strip()


def _delivery_country():
    return os.environ.get("EBAY_DELIVERY_COUNTRY", "").strip() or "CA"


def is_configured():
    return bool(_client_id() and _client_secret())


# --- OAuth (application token) ----------------------------------------------

_token = {"value": None, "expires_at": 0.0}


def _get_token(force=False):
    now = time.time()
    if not force and _token["value"] and now < _token["expires_at"] - 60:
        return _token["value"]
    if not is_configured():
        raise EbayNotConfigured("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set.")
    basic = base64.b64encode(f"{_client_id()}:{_client_secret()}".encode()).decode()
    try:
        resp = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials", "scope": OAUTH_SCOPE},
            timeout=15,
        )
    except requests.RequestException as e:
        raise EbayError(f"could not reach eBay to get a token ({e})")
    if resp.status_code != 200:
        raise EbayError(f"eBay rejected the API credentials ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    _token["value"] = data["access_token"]
    _token["expires_at"] = now + int(data.get("expires_in", 7200))
    return _token["value"]


def _api_get(url, params):
    """GET with the bearer token; refreshes the token once if eBay says it expired."""
    resp = None
    for attempt in (0, 1):
        headers = {
            "Authorization": f"Bearer {_get_token(force=bool(attempt))}",
            "X-EBAY-C-MARKETPLACE-ID": _marketplace(),
            "Accept": "application/json",
        }
        ctx = f"contextualLocation=country={_delivery_country()}"
        if _delivery_zip():
            ctx += f",zip={_delivery_zip()}"
        headers["X-EBAY-C-ENDUSERCTX"] = ctx
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as e:
            raise EbayError(f"could not reach eBay ({e})")
        if resp.status_code == 401 and attempt == 0:
            continue
        break
    if resp.status_code != 200:
        message = resp.text[:200]
        try:
            message = resp.json()["errors"][0]["message"]
        except Exception:
            pass
        raise EbayError(f"eBay search failed ({resp.status_code}): {message}")
    return resp.json()


# --- query -------------------------------------------------------------------

def _split_number(number):
    return [p.strip() for p in str(number or "").split("/") if p.strip()]


def _unpadded_number(number):
    """'014/064' -> '14/64' (eBay titles rarely zero-pad); 'GG36/GG70' is left alone."""
    return "/".join(str(int(p)) if p.isdigit() else p for p in _split_number(number))


def _padded_number(number):
    """The number exactly as TCGPlayer reports it ('069/084'), only tidied of spaces."""
    return "/".join(_split_number(number))


def _normalize_number(number, language=tcg_scraper.DEFAULT_LANGUAGE):
    """
    English: '014/064' -> '14/64' (eBay titles rarely zero-pad).
    Japanese: left as TCGPlayer gives it ('069/084'), because Japanese
    listings on eBay usually keep the leading zeroes.
    """
    if tcg_scraper.resolve_language(language) == "jp":
        return _padded_number(number)
    return _unpadded_number(number)


_DASHES = "-\u2013\u2014"
_DASH_AT_WORD_EDGE = re.compile(
    rf"(?:(?<=\s)|^)[{_DASHES}]+|[{_DASHES}]+(?=\s|$)"
)


def _clean_text(text):
    """
    Strip characters eBay treats as search operators. Dashes are only removed
    when they touch whitespace (or the start/end of the text), so 'Ho-Oh' keeps
    its hyphen while ' \u2014 ' and a leading '-word' (an eBay exclusion) go.
    """
    base = re.sub(r"[()\[\]{},#:;\"]+", " ", text or "")
    base = _DASH_AT_WORD_EDGE.sub(" ", base)
    return " ".join(base.split())


def _edition_term(printing):
    """Extra search word for the selected printing: 'reverse' / '1st edition'."""
    names = [n.lower() for n in tcg_scraper._printing_names(printing)]
    if any(n.startswith("1st edition") for n in names):
        return "1st edition"
    if any(n == "reverse holofoil" for n in names):
        return "reverse"
    return ""


def build_query(name, number=None, language=tcg_scraper.DEFAULT_LANGUAGE, set_name=None,
                printing=None):
    """
    The eBay search text: card name + card number. If the card has no number,
    card name + set name instead. Brackets and dashes are removed because eBay
    treats parentheses and commas in a query as search operators.

    Japanese cards use the number exactly as TCGPlayer gives it (e.g.
    'Rocket's Suicune ex 069/084'), with no OR alternative. English cards use
    the unpadded form.
    """
    base = _clean_text(name)
    num = _normalize_number(number, language)
    if num:
        if num.lower() not in base.lower():
            base = f"{base} {num}".strip()
    else:
        set_text = _clean_text(set_name)
        if set_text and set_text.lower() not in base.lower():
            base = f"{base} {set_text}".strip()
    term = _edition_term(printing)
    if term and term not in base.lower():
        base = f"{base} {term}"
    return base


def _language_name(language):
    """eBay's "Language" item-specific value for the UI's language code."""
    return tcg_scraper.LANGUAGE_SETTINGS[tcg_scraper.resolve_language(language)]["ebay_language"]


def _language_aspect(language):
    """The aspect_filter clause limiting a search to one language."""
    return f"Language:{{{_escape_aspect(_language_name(language))}}}"


def search_url(query, language=tcg_scraper.DEFAULT_LANGUAGE):
    """The same search on ebay.com (cheapest first), for a quick visual check."""
    from urllib.parse import urlencode
    # eBay's website writes aspect filters double-encoded (see sold_search_url).
    return (f"{EBAY_SITE}/sch/i.html?" + urlencode(
        {"_nkw": query, "_sacat": CCG_CATEGORY_ID, "_sop": "15", "LH_BIN": "1"})
        + f"&Language={quote(quote(_language_name(language), safe=''), safe='')}")


# --- conditions ----------------------------------------------------------------

def condition_bucket(value):
    """Map one of eBay's Card Condition values to a row label."""
    v = (value or "").lower()
    if "near mint" in v or v.strip() in ("nm", "mint"):
        return "Near Mint"
    if "lightly" in v or "excellent" in v:
        return "Lightly Played"
    if "moderately" in v or "very good" in v:
        return "Moderately Played"
    if "heavily" in v or "poor" in v or "damaged" in v:
        return "Heavily Played / Damaged"
    return "Not Specified"


# eBay's Card Condition values, used for the sold-listing links when we haven't
# just seen eBay's own values (see _lookup). These are the wording eBay shows
# in its Card Condition filter.
DEFAULT_CONDITION_VALUES = {
    "Near Mint": ["Near Mint or Better"],
    "Lightly Played": ["Lightly Played (Excellent)"],
    "Moderately Played": ["Moderately Played (Very Good)"],
    "Heavily Played / Damaged": ["Heavily Played (Poor)"],
    "Not Specified": ["Not Specified"],
}


SOLD_SORT_PRICE = "15"   # eBay _sop: price + shipping, lowest first
SOLD_SORT_RECENT = "13"  # eBay _sop: ended recently


def sold_search_url(query, condition=None, values=None, language=tcg_scraper.DEFAULT_LANGUAGE,
                    sort=SOLD_SORT_RECENT):
    """
    A link to eBay's own sold-listings page: Sold items + Completed items,
    ordered by `sort` (SOLD_SORT_PRICE or SOLD_SORT_RECENT). With a condition
    row label, the Card Condition filter is set; with condition=None there is
    no condition filter at all.
    eBay's website writes aspect filters double-encoded (a space is %2520), and
    separates several chosen values with an encoded pipe (%7C).
    """
    double = lambda text: quote(quote(text, safe=""), safe="")
    base = urlencode({"_nkw": query, "_sacat": CCG_CATEGORY_ID,
                      "LH_Sold": "1", "LH_Complete": "1", "_sop": sort})
    url = f"{EBAY_SITE}/sch/i.html?{base}"
    if condition is not None:
        chosen = values or DEFAULT_CONDITION_VALUES[condition]
        url += f"&Card%2520Condition={'%7C'.join(double(v) for v in chosen)}"
    return url + f"&Language={double(_language_name(language))}"


def sold_search_urls(query, condition_values=None, language=tcg_scraper.DEFAULT_LANGUAGE):
    """
    {"all": {"price": url, "recent": url},
     "conditions": {row label: {"price": url, "recent": url}}}
    "price" = lowest price + shipping first (the main links); "recent" = ended
    recently first. "all" has no condition filter. condition_values = eBay's own
    Card Condition wording per row, when a lookup has just seen it.
    """
    condition_values = condition_values or {}

    def pair(label=None):
        vals = condition_values.get(label) if label else None
        return {
            "price": sold_search_url(query, label, vals, language, SOLD_SORT_PRICE),
            "recent": sold_search_url(query, label, vals, language, SOLD_SORT_RECENT),
        }

    return {"all": pair(), "conditions": {label: pair(label) for label in CONDITION_ORDER}}


# Fallback only: guess a condition from the listing title. When a title names
# several conditions ("NM-LP") the WORSE one wins, to avoid flattering the price.
_TITLE_PATTERNS = [
    ("Heavily Played / Damaged", re.compile(r"\b(hp|heavily played|damaged|dmg|poor)\b", re.I)),
    ("Moderately Played", re.compile(r"\b(mp|moderately played|very good|vg)\b", re.I)),
    ("Lightly Played", re.compile(r"\b(lp|lightly played|light play)\b", re.I)),
    ("Near Mint", re.compile(r"\b(nm|near mint|mint)\b", re.I)),
]


def _classify_title(title):
    for label, pattern in _TITLE_PATTERNS:
        if pattern.search(title or ""):
            return label
    return "Not Specified"


# --- parsing listings ------------------------------------------------------------

def _to_usd(value, currency):
    """Prices come back in the listing's currency; USD and CAD are handled."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if currency == "USD":
        return amount
    if currency == "CAD":
        return amount / pricing.get_usd_to_cad_rate()
    return None


def _money_usd(obj):
    """
    A {"value", "currency", "convertedFromValue", "convertedFromCurrency"} amount
    as USD. When eBay converted the amount from USD (e.g. to CAD for a Canadian
    buyer), the original USD figure is used, so nothing is converted twice.
    """
    if not obj:
        return None
    if (obj.get("currency") or "").upper() != "USD" \
            and (obj.get("convertedFromCurrency") or "").upper() == "USD":
        return _to_usd(obj.get("convertedFromValue"), "USD")
    return _to_usd(obj.get("value"), obj.get("currency"))


def _to_ebay_ca(url):
    """The Browse API returns ebay.com item links; point them at ebay.ca."""
    return re.sub(r"^https?://(www\.)?ebay\.com", EBAY_SITE, url, flags=re.I) if url else url


def _item_to_listing(item, language=tcg_scraper.DEFAULT_LANGUAGE, printing=None):
    """One item summary -> the same shape the TCGPlayer listings use, or None to skip it."""
    if str(item.get("conditionId")) == GRADED_CONDITION_ID:
        return None  # graded slabs aren't comparable to raw-card prices
    title = item.get("title") or ""
    if EXCLUDED_TITLE_PATTERN.search(title):
        return None  # fan-made / proxy / custom cards aren't official
    if not _printing_matches_title(title, printing):
        return None  # wrong printing (e.g. not a 1st Edition card)
    # Same language filter as TCGPlayer: drop anything that names another language
    # (for a Japanese search, Japanese is the wanted language and isn't dropped).
    pattern = tcg_scraper.foreign_language_pattern(language)
    if pattern.search(f"{title} {item.get('shortDescription') or ''}"):
        return None
    price_obj = item.get("price") or {}
    price = _to_usd(price_obj.get("value"), price_obj.get("currency"))
    if price is None:
        return None
    shipping = None
    for option in item.get("shippingOptions") or []:
        amount = _money_usd(option.get("shippingCost"))
        if amount is not None and (shipping is None or amount < shipping):
            shipping = amount
    shipping_known = shipping is not None
    shipping = shipping if shipping_known else 0.0
    return {
        "lowest_total": round(price + shipping, 2),
        "price": round(price, 2),
        "shipping": round(shipping, 2),
        "shipping_known": shipping_known,
        "seller": (item.get("seller") or {}).get("username", "unknown"),
        "title": title,
        "url": _to_ebay_ca(item.get("itemWebUrl")),
        "seller_country": ((item.get("itemLocation") or {}).get("country") or "").upper(),
        "item_id": item.get("itemId"),
    }


_shipping_cache = {}  # item id -> {"ts": float, "usd": float or None}


def _item_shipping_usd(item_id):
    """
    Shipping to the delivery country (USD) for one item, from the single-item
    endpoint. Search results leave "calculated" shipping blank, but this
    endpoint prices it. None if eBay gives no cost. Cached like the searches.
    """
    if not item_id:
        return None
    hit = _shipping_cache.get(item_id)
    if hit and (time.time() - hit["ts"]) < CACHE_TTL_SECONDS:
        return hit["usd"]
    try:
        data = _api_get(ITEM_URL + item_id, {})
    except EbayError as e:
        log.warning("eBay item lookup for %s failed: %s", item_id, e)
        return None  # not cached, so a later lookup can retry
    costs = [c for c in (_money_usd(o.get("shippingCost")) for o in data.get("shippingOptions") or [])
             if c is not None]
    usd = min(costs) if costs else None
    if len(_shipping_cache) > 2000:
        _shipping_cache.clear()
    _shipping_cache[item_id] = {"ts": time.time(), "usd": usd}
    return usd


def _pick_cheapest(listings):
    """
    The cheapest listing (price + shipping) from one condition's candidates.
    A listing whose shipping search didn't return is priced with $0 shipping at
    first, i.e. a lower bound. The cheapest such listing is looked up with
    getItem (up to MAX_SHIPPING_LOOKUPS), re-ranked with its real shipping, and
    repeated until the front-runner has known shipping. Listings whose shipping
    can't be found only win when no listing has a known cost.
    """
    pool = sorted(listings, key=lambda l: l["lowest_total"])
    unpriced, lookups = [], 0
    while pool and not pool[0]["shipping_known"] and lookups < MAX_SHIPPING_LOOKUPS:
        top = pool.pop(0)
        lookups += 1
        cost = _item_shipping_usd(top.get("item_id"))
        if cost is None:
            unpriced.append(top)
            continue
        top["shipping"] = round(cost, 2)
        top["shipping_known"] = True
        top["lowest_total"] = round(top["price"] + cost, 2)
        pool.append(top)
        pool.sort(key=lambda l: l["lowest_total"])
    known = [l for l in pool if l["shipping_known"]]
    if known:
        return known[0]
    rest = unpriced + pool
    return min(rest, key=lambda l: l["lowest_total"]) if rest else None


def _finalize(pools):
    return {label: _pick_cheapest(items) for label, items in pools.items()}


# --- searching ---------------------------------------------------------------------

def _search(params, limit):
    params = dict(params, limit=str(limit))
    return _api_get(SEARCH_URL, params).get("itemSummaries") or []


def _card_condition_values(base):
    """Ask eBay which Card Condition values exist for this query: [{value, href}, ...]."""
    data = _api_get(SEARCH_URL, dict(base, fieldgroups="ASPECT_REFINEMENTS", limit="1"))
    for dist in (data.get("refinement") or {}).get("aspectDistributions") or []:
        if "card condition" in (dist.get("localizedAspectName") or "").lower():
            return [
                {"value": v["localizedAspectValue"], "href": v.get("refinementHref")}
                for v in dist.get("aspectValueDistributions") or []
                if v.get("localizedAspectValue")
            ]
    return []


def _escape_aspect(value):
    return re.sub(r"([\\|,{}])", r"\\\1", value)


def _search_condition_value(base, entry, language=tcg_scraper.DEFAULT_LANGUAGE):
    """The cheapest listings with one Card Condition value."""
    params = None
    href = entry.get("href")
    if href:
        parts = urlsplit(href)
        # Only follow links back to eBay's own search endpoint (never send our token elsewhere).
        if parts.scheme == "https" and parts.netloc == "api.ebay.com" and parts.path.endswith("/item_summary/search"):
            params = dict(parse_qsl(parts.query))
            params.pop("fieldgroups", None)
            params.pop("offset", None)
            params["sort"] = "price"
    if not params or "aspect_filter" not in params:
        params = dict(base)
        params["aspect_filter"] = (
            f"categoryId:{CCG_CATEGORY_ID},Card Condition:{{{_escape_aspect(entry['value'])}}},"
            f"{_language_aspect(language)}"
        )
    return _search(params, PER_CONDITION_LIMIT)


def _lookup(query, language=tcg_scraper.DEFAULT_LANGUAGE, printing=None):
    base = {
        "q": query,
        "category_ids": CCG_CATEGORY_ID,
        "filter": f"buyingOptions:{{FIXED_PRICE|BEST_OFFER}},deliveryCountry:{_delivery_country()}",
        # Limit to the chosen language. The refinement links eBay returns for
        # each Card Condition (see _card_condition_values) carry this filter
        # forward, so every per-condition search inherits it.
        "aspect_filter": f"categoryId:{CCG_CATEGORY_ID},{_language_aspect(language)}",
        "sort": "price",
    }
    pools = {label: [] for label in CONDITION_ORDER}

    try:
        values = _card_condition_values(base)
    except EbayError as e:
        log.warning("eBay Card Condition lookup failed (%s); falling back to title matching", e)
        values = []

    condition_values = {}
    if values:
        succeeded = 0
        for entry in values:
            condition_values.setdefault(condition_bucket(entry["value"]), []).append(entry["value"])
            try:
                items = _search_condition_value(base, entry, language)
            except EbayError as e:
                log.warning("eBay search for Card Condition %r failed: %s", entry["value"], e)
                continue
            succeeded += 1
            bucket = condition_bucket(entry["value"])
            for item in items:
                listing = _item_to_listing(item, language, printing)
                if listing:
                    pools[bucket].append(listing)
        if succeeded:
            return {"by_condition": _finalize(pools), "approximate": False, "query": query,
                    "condition_values": condition_values}

    # Fallback: one price-sorted search, conditions guessed from titles.
    for item in _search(base, FALLBACK_LIMIT):
        listing = _item_to_listing(item, language, printing)
        if listing:
            pools[_classify_title(listing["title"])].append(listing)
    return {"by_condition": _finalize(pools), "approximate": True, "query": query, "condition_values": {}}


_cache = {}  # (marketplace, query, language) -> {"ts": float, "data": dict}


def get_lowest_listings(query, language=tcg_scraper.DEFAULT_LANGUAGE, printing=None):
    """
    Returns {"by_condition": {label: listing-or-None}, "approximate": bool, "query": str,
             "condition_values": {label: [eBay's Card Condition wording]}}.
    Raises EbayNotConfigured / EbayError. Cached for CACHE_TTL_SECONDS.
    """
    if not is_configured():
        raise EbayNotConfigured("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set.")
    language = tcg_scraper.resolve_language(language)
    key = (_marketplace(), query, language, printing)
    hit = _cache.get(key)
    if hit and (time.time() - hit["ts"]) < CACHE_TTL_SECONDS:
        return copy.deepcopy(hit["data"])
    data = _lookup(query, language, printing)
    _cache[key] = {"ts": time.time(), "data": copy.deepcopy(data)}
    return data
