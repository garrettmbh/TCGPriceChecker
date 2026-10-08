# Pokémon Card Price Lookup

Flask app: enter a card name (+ optional set, number, holo type), see the
card image, the current lowest price+shipping per condition, and recent
sales per condition — all pulled live from TCGPlayer.

## Setup

```bash
cd tcg-price-lookup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5075` in your browser.

## Using it from your phone (same wifi)

The app already binds to `0.0.0.0`, so it's reachable on your LAN:

1. On your Mac: System Settings → Wi-Fi → Details (or `ipconfig getifaddr en0`
   in Terminal) to get your Mac's local IP, e.g. `192.168.1.42`.
2. On your phone (same wifi network), visit `http://192.168.1.42:5075`.

If it doesn't load, macOS's firewall may be blocking incoming connections —
System Settings → Network → Firewall → allow Python, or temporarily disable
it to test.

## How it works

`tcg_scraper.py` calls three of TCGPlayer's internal JSON endpoints (the
same ones tcgplayer.com's own frontend uses — there's no public API for
shipping-inclusive lowest listings or sales history):

- `mp-search-api.tcgplayer.com/v1/search/request` — find the product ID for
  a name/set/number/holo-type combination
- `mp-search-api.tcgplayer.com/v1/product/{id}/listings` — current live
  listings, sorted by price+shipping, grouped into the lowest per condition
- `mpapi.tcgplayer.com/v2/product/{id}/latestsales` — recent completed sales
  per condition

## ⚠️ This will probably need a little debugging on your end

I built this from documented knowledge of TCGPlayer's endpoint shapes, but
I could not actually run it against tcgplayer.com — my dev sandbox's
network is locked down to a small allowlist that doesn't include
tcgplayer.com. So there's a real chance the exact request/response shape
has drifted since my knowledge was current, or differs in some detail I
didn't anticipate.

If a search or lookup fails, the error message will tell you which request
failed. To fix it:

1. Open tcgplayer.com in Chrome, search for the same card, open DevTools →
   Network → XHR.
2. Find the request to the matching endpoint (`search/request`, `listings`,
   or `latestsales`).
3. Compare its request payload and response JSON shape to what's in
   `tcg_scraper.py`, and adjust the field names/paths accordingly. The
   functions are small and isolated specifically so this is a quick patch,
   not a rewrite.

## Optional: use a logged-in (dummy) TCGPlayer account

TCGPlayer can show more sales history to logged-in users. Rather than putting
a username/password in the script, the scraper reads an auth cookie from an
environment variable (unset = anonymous, the default):

1. In a browser, log in to TCGPlayer with the **dummy account** (not your real one).
2. DevTools -> Application (Chrome) / Storage (Firefox) -> Cookies ->
   `https://www.tcgplayer.com` -> copy the value of `TCGAuthTicket_Production`.
3. Before starting the app: `export TCG_AUTH_TICKET='<that value>'` then `python app.py`.

Treat that value like a password: keep it out of the script, git, and chats.
It expires periodically, so re-copy it if results stop looking "logged in".

## Login / logout

Set these environment variables to put a login page in front of the whole
site (recommended for any public deployment, since the app makes requests to
TCGPlayer through your dummy account's cookie). Leave `APP_USERNAME` and
`APP_PASSWORD` unset and every page stays open — the default for local runs.

- `APP_USERNAME` / `APP_PASSWORD` — the login to require
- `APP_SECRET_KEY` — any long random string (e.g. `openssl rand -hex 32`).
  Signs the login session cookie. Without it, a random key is generated each
  time the app starts, which logs everyone out on every redeploy/restart.
- `SESSION_COOKIE_SECURE` — defaults to on (HTTPS only). Set to `0` only for
  local `http://` testing with auth enabled, since browsers won't store a
  `Secure` cookie over plain HTTP.

Login uses a signed session cookie (not HTTP Basic Auth, which mobile
browsers forget quickly), lasting 30 days — change `SESSION_LIFETIME_DAYS` in
`app.py` to adjust. A "Log out" link appears on every page once logged in,
and `/logout` works directly too. Cookies are per browser/device, so expect
one login on each.

## A few things worth knowing

- **Terms of Service**: TCGPlayer's ToS restricts automated scraping of
  their site. This is fine for light personal use (checking prices before
  you buy/sell), but don't run it at high frequency or resell/republish the
  data — that's the kind of use they actively police.
- **Currency**: listings are requested with `shippingCountryTag: "CA"` since
  you're in Canada — TCGPlayer will return CAD-converted pricing where
  applicable. Drop/change that in `tcg_scraper.py` if you want USD instead.
- **Rate limiting**: there's a 5-minute in-memory cache per product in
  `app.py` so repeat lookups of the same card don't keep hitting TCGPlayer.
- **Ambiguous searches**: if your search matches multiple printings (e.g.
  different sets with the same name), you'll get a picker page instead of
  results.

## Possible next steps

- Add a "watch this card and alert me below $X" feature using the same
  polling pattern as your eBay scanner.
- Deploy it somewhere always-on (a Raspberry Pi, or a small VPS) instead of
  running it only when your Mac is on.
- Swap the in-memory cache for SQLite if you want price history over time,
  not just a live snapshot.
