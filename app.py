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
APP_USERNAME/APP_PASSWORD add a login prompt.
"""

import os
import secrets
import copy

from flask import Flask, render_template, request, Response
import pricing
import tcg_scraper

app = Flask(__name__)

# Optional HTTP Basic Auth. Set APP_USERNAME and APP_PASSWORD (e.g. in
# Northflank's runtime variables) to require a login for every page — this
# app makes outbound requests to TCGPlayer using your dummy account's
# cookie, so a public URL should not be left open to anyone who finds it.
# Unset locally, this is a no-op.
_APP_USERNAME = os.environ.get("APP_USERNAME", "")
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.before_request
def _require_login():
    if not _APP_USERNAME or not _APP_PASSWORD:
        return None  # auth not configured — behave as before
    auth = request.authorization
    valid = (
        auth
        and secrets.compare_digest(auth.username, _APP_USERNAME)
        and secrets.compare_digest(auth.password, _APP_PASSWORD)
    )
    if not valid:
        return Response(
            "Login required.", 401,
            {"WWW-Authenticate": 'Basic realm="Card Price Lookup"'},
        )
    return None

# Very small in-memory cache so re-loading a result page (or two people
# looking up the same card) doesn't hammer TCGPlayer. Keyed by product_id.
_details_cache = {}
CACHE_TTL_SECONDS = 300

import time


def _get_cached_details(candidate, printing_filter=None):
    cache_key = (candidate["product_id"], printing_filter)
    now = time.time()

    cached = _details_cache.get(cache_key)

    if cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
        # Never give the presentation layer the object stored in the cache.
        # pricing.enrich_sales() mutates the returned data.
        return copy.deepcopy(cached["data"])

    data = tcg_scraper.get_card_details(
        candidate,
        printing_filter=printing_filter,
    )

    # Store an untouched copy of the raw TCGPlayer data.
    _details_cache[cache_key] = {
        "ts": now,
        "data": copy.deepcopy(data),
    }

    # Return a separate object that the pricing layer can safely mutate.
    return copy.deepcopy(data)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", edition="", holofoil=True,
                           edition_choices=tcg_scraper.EDITION_CHOICES)


@app.route("/search", methods=["GET"])
def search():
    name = request.args.get("name", "").strip()
    set_name = request.args.get("set_name", "").strip() or None
    card_number = request.args.get("card_number", "").strip() or None
    holo_type = request.args.get("holo_type", "").strip() or None
    edition = request.args.get("edition", "").strip()
    # Hidden "0" + checkbox "1" share the name; unchecked sends only the "0".
    # Absent entirely (e.g. hand-typed URL) defaults to checked.
    holo_values = request.args.getlist("holofoil")
    holofoil = (holo_values[-1] == "1") if holo_values else True
    printing = tcg_scraper.resolve_printing(edition, holofoil)
    form_state = dict(name=name, set_name=set_name, card_number=card_number,
                      holo_type=holo_type, edition=edition, holofoil=holofoil,
                      edition_choices=tcg_scraper.EDITION_CHOICES)

    if not name:
        return render_template("index.html", error="Enter at least a card name.",
                               **dict(form_state, name=""))

    try:
        result = tcg_scraper.lookup_card(name, set_name, card_number, holo_type,
                                          printing_filter=printing)
    except tcg_scraper.TCGScraperError as e:
        return render_template("index.html", error=str(e), **form_state)

    if result["status"] == "not_found":
        return render_template(
            "index.html",
            error="No matching card found. Try loosening the search (fewer fields).",
            **form_state,
        )

    if result["status"] == "ambiguous":
        return render_template(
            "disambiguate.html",
            candidates=result["candidates"],
            search_note=result.get("note"),
            name=name, set_name=set_name, card_number=card_number,
            holo_type=holo_type, printing=printing,
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
    )


@app.route("/card/<int:product_id>", methods=["GET"])
def card_detail(product_id):
    """Used when the user picks one option off the disambiguation page."""
    name = request.args.get("name", "")
    set_name = request.args.get("set_name", "")
    number = request.args.get("card_number", "")
    printing = request.args.get("printing", "").strip() or None
    candidate = {
        "product_id": product_id,
        "name": name,
        "set_name": set_name,
        "number": number,
        "image_url": f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_200w.jpg",
        "url": f"https://www.tcgplayer.com/product/{product_id}",
    }
    try:
        result = _get_cached_details(candidate, printing)
    except tcg_scraper.TCGScraperError as e:
        return render_template("index.html", error=str(e), edition="", holofoil=True,
                               edition_choices=tcg_scraper.EDITION_CHOICES)

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
        condition_order=tcg_scraper.CONDITION_ORDER,
    )


if __name__ == "__main__":
    # Local development only. In the Docker/Northflank deployment, gunicorn
    # runs the app instead (see Dockerfile) — this block never executes there.
    # host="0.0.0.0" makes it reachable from your phone on the same LAN.
    app.run(host="0.0.0.0", port=5075, debug=True)
