"""
Flask app: search a Pokemon card by name/set/number/holo type, show its
image, current lowest price+shipping per condition, and recent sales.

Run locally:
    pip install -r requirements.txt
    python app.py

Then open http://127.0.0.1:5075 on your Mac, or http://<your-mac's-lan-ip>:5075
from your phone (same wifi network) — see README for finding your LAN IP.

For a public deployment (e.g. Northflank), see Dockerfile and README.md —
gunicorn runs this app there instead of the __main__ block below, and
APP_USERNAME/APP_PASSWORD add a login page.
"""

import os
import re
import secrets
import copy
from datetime import timedelta

from flask import Flask, render_template, request, redirect, url_for, session
import ebay
import pricing
import tcg_scraper

app = Flask(__name__)

# Session-based login. Set APP_USERNAME and APP_PASSWORD (e.g. in
# Northflank's runtime variables) to require a login for every page — this
# app makes outbound requests to TCGPlayer using your dummy account's
# cookie, so a public URL should not be left open to anyone who finds it.
# Unset locally (both empty), this is a no-op and every route is open.
#
# This replaced HTTP Basic Auth, which mobile browsers (Safari especially)
# tend to forget far sooner than desktop ones, forcing repeated logins.
# A session cookie, by contrast, persists for SESSION_LIFETIME_DAYS below.
_APP_USERNAME = os.environ.get("APP_USERNAME", "")
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_LIFETIME_DAYS = 30

# Signs the session cookie. If APP_SECRET_KEY isn't set, a random key is
# generated at startup instead — the app still works, but every restart
# (including a redeploy) invalidates existing sessions, logging everyone
# out. Set APP_SECRET_KEY in Northflank to avoid that; see README.
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=SESSION_LIFETIME_DAYS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Northflank serves this over HTTPS, so the cookie should require it there.
# For local http:// testing with auth enabled, set SESSION_COOKIE_SECURE=0.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"


def _auth_enabled():
    return bool(_APP_USERNAME and _APP_PASSWORD)


@app.context_processor
def _inject_auth_state():
    # Lets every template show/hide a "Log out" link without passing this
    # through each individual render_template() call.
    return {"logged_in": _auth_enabled() and bool(session.get("authenticated"))}


@app.before_request
def _require_login():
    if not _auth_enabled():
        return None  # auth not configured — every route is open
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("authenticated"):
        dest = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=dest))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _auth_enabled():
        return redirect(url_for("index"))
    error = None
    next_url = request.values.get("next", "") or url_for("index")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("index")  # only ever redirect within this site
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (
            secrets.compare_digest(username, _APP_USERNAME)
            and secrets.compare_digest(password, _APP_PASSWORD)
        ):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(next_url)
        error = "Incorrect username or password."
    return render_template("login.html", error=error, next=next_url)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login") if _auth_enabled() else url_for("index"))


# Very small in-memory cache so re-loading a result page (or two people
# looking up the same card) doesn't hammer TCGPlayer. Keyed by product_id.
_details_cache = {}
CACHE_TTL_SECONDS = 300

import time


def _get_cached_details(candidate, printing_filter=None, language="en"):
    cache_key = (candidate["product_id"], printing_filter, language)
    now = time.time()

    cached = _details_cache.get(cache_key)

    if cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
        # Never give the presentation layer the object stored in the cache.
        # pricing.enrich_sales() mutates the returned data.
        return copy.deepcopy(cached["data"])

    data = tcg_scraper.get_card_details(
        candidate,
        printing_filter=printing_filter,
        language=language,
    )

    # Store an untouched copy of the raw TCGPlayer data.
    _details_cache[cache_key] = {
        "ts": now,
        "data": copy.deepcopy(data),
    }

    # Return a separate object that the pricing layer can safely mutate.
    return copy.deepcopy(data)


_PRINTING_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '&.()/-]{0,39}$")


def _parse_known_printings(raw):
    """
    Printings the previous page already knew this card has, carried along in
    the Printing dropdown's form so switching never shrinks the dropdown to
    just the selected printing. Only used to fill the dropdown, so it's
    sanitized rather than trusted.
    """
    names = []
    for part in (raw or "").split("|"):
        part = part.strip()
        if _PRINTING_NAME_RE.match(part) and part not in names:
            names.append(part)
        if len(names) >= 12:
            break
    return names


def _with_market_totals(candidates):
    """
    Adds market_total to each picker candidate: TCGPlayer's market price (USD)
    converted to CAD and scaled by the NY sales tax, the same "Total" the
    results page shows. The original USD figure stays in market_price.
    """
    rate = pricing.get_usd_to_cad_rate()
    out = []
    for c in candidates:
        c = dict(c)
        usd = c.get("market_price")
        c["market_total"] = pricing.usd_to_totals(usd, rate)["total"] if usd else None
        out.append(c)
    return out


def _candidate_from_args(product_id):
    return {
        "product_id": product_id,
        "name": request.args.get("name", ""),
        "set_name": request.args.get("set_name", ""),
        "number": request.args.get("card_number", ""),
        "image_url": f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_200w.jpg",
        "url": f"https://www.tcgplayer.com/product/{product_id}",
    }


def _ebay_shell(product, language):
    """Template variables for the eBay tab's initial 'loading' state."""
    query = ebay.build_query(product["name"], product["number"], language, product.get("set_name"))
    return dict(
        ebay_panel_url=url_for("card_ebay_panel", product_id=product["product_id"],
                               name=product["name"], set_name=product.get("set_name") or "",
                               card_number=product["number"], language=language),
        ebay_query_text=query,
        ebay_search_url=ebay.search_url(query, language),
        ebay_condition_order=ebay.CONDITION_ORDER,
        sold_message=ebay.SOLD_UNAVAILABLE_MESSAGE,
        sold_links=ebay.sold_search_urls(query, None, language),
    )


@app.route("/card/<int:product_id>/ebay", methods=["GET"])
def card_ebay_panel(product_id):
    """HTML fragment for the eBay tab. The results page fetches this in the
    background as soon as it loads."""
    candidate = _candidate_from_args(product_id)
    language = tcg_scraper.resolve_language(request.args.get("language", ""))
    shell = _ebay_shell(candidate, language)
    query = shell["ebay_query_text"]

    listings = {label: None for label in ebay.CONDITION_ORDER}
    ebay_error, approximate, condition_values = None, False, {}
    try:
        data = ebay.get_lowest_listings(query, language)
        listings, approximate = data["by_condition"], data["approximate"]
        condition_values = data.get("condition_values") or {}
    except ebay.EbayNotConfigured:
        ebay_error = ("eBay isn't set up yet. Add the EBAY_CLIENT_ID and "
                      "EBAY_CLIENT_SECRET environment variables (see the README).")
    except ebay.EbayError as e:
        ebay_error = f"eBay lookup failed: {e}"

    rate = pricing.get_usd_to_cad_rate()
    listings = pricing.enrich_listings(listings, rate)

    shell["sold_links"] = ebay.sold_search_urls(query, condition_values, language)
    return render_template(
        "_ebay_panel.html", loading=False, listings=listings, fx_rate=rate,
        ebay_error=ebay_error, ebay_approximate=approximate, language=language, **shell)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", edition="", holofoil=True, language="en",
                           edition_choices=tcg_scraper.EDITION_CHOICES,
                           language_choices=tcg_scraper.LANGUAGE_CHOICES)


@app.route("/search", methods=["GET"])
def search():
    name = request.args.get("pokemon_card_name", "").strip()
    set_name = request.args.get("set_name", "").strip() or None
    card_number = request.args.get("pokemon_card_number", "").strip() or None
    edition = request.args.get("edition", "").strip()
    # Hidden "0" + checkbox "1" share the name; unchecked sends only the "0".
    # Absent entirely (e.g. hand-typed URL) defaults to checked.
    holo_values = request.args.getlist("holofoil")
    holofoil = (holo_values[-1] == "1") if holo_values else True
    printing = tcg_scraper.resolve_printing(edition, holofoil)
    language = tcg_scraper.resolve_language(request.args.get("language", ""))
    form_state = dict(name=name, set_name=set_name, card_number=card_number,
                      edition=edition, holofoil=holofoil, language=language,
                      edition_choices=tcg_scraper.EDITION_CHOICES,
                      language_choices=tcg_scraper.LANGUAGE_CHOICES)

    if not (name or set_name or card_number):
        return render_template("index.html", error="Enter a card name, set name, or card number.",
                               **form_state)

    try:
        result = tcg_scraper.lookup_card(name, set_name, card_number,
                                          printing_filter=printing, language=language)
    except tcg_scraper.TCGScraperError as e:
        return render_template("index.html", error=str(e), **form_state)

    if result["status"] == "not_found":
        hidden = result.get("hidden_by_printing", 0)
        if hidden and printing:
            label = tcg_scraper.printing_label(printing)
            error = (f"{hidden} card{'s' if hidden != 1 else ''} matched your search, but "
                     f"none have a {label} printing. Try another printing, or Any printing.")
        else:
            error = "No matching card found. Try loosening the search (fewer fields)."
        return render_template("index.html", error=error, **form_state)

    if result["status"] == "ambiguous":
        return render_template(
            "disambiguate.html",
            candidates=_with_market_totals(result["candidates"]),
            search_note=result.get("note"),
            name=name, set_name=set_name, card_number=card_number,
            printing=printing, language=language,
        )

    rate = pricing.get_usd_to_cad_rate()
    listings = pricing.enrich_listings(result["listings"], rate)
    sales, sales_stats = pricing.enrich_sales(result["sales"], rate)
    listings = pricing.add_listing_diffs(listings, sales_stats)

    return render_template(
        "results.html",
        product=result["product"],
        listings=listings,
        sales=sales,
        sales_stats=sales_stats,
        fx_rate=rate,
        available_printings=result["available_printings"],
        printing_filter=printing,
        search_note=result.get("note"),
        condition_order=tcg_scraper.CONDITION_ORDER,
        language=language,
        **_ebay_shell(result["product"], language),
    )


@app.route("/card/<int:product_id>", methods=["GET"])
def card_detail(product_id):
    """Used when the user picks one option off the disambiguation page."""
    printing = request.args.get("printing", "").strip() or None
    known_printings = _parse_known_printings(request.args.get("printings"))
    language = tcg_scraper.resolve_language(request.args.get("language", ""))
    source = "ebay" if request.args.get("source") == "ebay" else "tcgplayer"
    candidate = _candidate_from_args(product_id)

    try:
        result = _get_cached_details(candidate, printing, language)
    except tcg_scraper.TCGScraperError as e:
        return render_template("index.html", error=str(e), edition="", holofoil=True,
                               language=language,
                               edition_choices=tcg_scraper.EDITION_CHOICES,
                               language_choices=tcg_scraper.LANGUAGE_CHOICES)

    rate = pricing.get_usd_to_cad_rate()
    listings = pricing.enrich_listings(result["listings"], rate)
    sales, sales_stats = pricing.enrich_sales(result["sales"], rate)
    listings = pricing.add_listing_diffs(listings, sales_stats)
    # The dropdown must list every printing the card has, even when TCGPlayer
    # narrows its own list to the selected one: add back what the page we came
    # from already knew.
    available_printings = sorted(set(result["available_printings"]) | set(known_printings))

    return render_template(
        "results.html",
        product=result["product"],
        listings=listings,
        sales=sales,
        sales_stats=sales_stats,
        fx_rate=rate,
        available_printings=available_printings,
        printing_filter=printing,
        condition_order=tcg_scraper.CONDITION_ORDER,
        language=language,
        initial_tab=source,
        **_ebay_shell(result["product"], language),
    )


if __name__ == "__main__":
    # Local development only. In the Docker/Northflank deployment, gunicorn
    # runs the app instead (see Dockerfile) — this block never executes there.
    # host="0.0.0.0" makes it reachable from your phone on the same LAN.
    app.run(host="0.0.0.0", port=5075, debug=True)
