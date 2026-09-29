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

## Deploying to Northflank (free, always-on)

This includes a Dockerfile, so Northflank can build and run it directly —
no code changes needed beyond what's already here.

1. Push this project to a GitHub repo (Northflank builds from a repo, not a
   local folder).
2. In Northflank: **Projects -> Create Project**, then inside it
   **Services -> Create Service -> Combined service**, and connect the repo.
3. Under **Build options**, choose **Dockerfile**, path `/Dockerfile`.
4. Under **Networking**, add a port: **8000**, protocol **HTTP**, public
   enabled. (8000 is what the Dockerfile's gunicorn binds to — if you change
   one, change the other to match.)
5. Under **Environment / runtime variables**, add:
   - `TCG_AUTH_TICKET` — the dummy account's cookie value (see above)
   - `APP_USERNAME` / `APP_PASSWORD` — pick any login you want; this puts a
     password prompt on the whole site, which matters once it's a public
     URL making requests through your dummy account's cookie
6. Create the service. Northflank builds the image and gives you an HTTPS
   URL when it's done — that's the site.
7. Pick the free **Sandbox** plan when prompted, if it isn't the default.

Since Sandbox is a capped free tier, not a guarantee, check Northflank's
current pricing page before relying on it long-term — free tiers do
sometimes get pared back.

Every push to the connected branch redeploys automatically. Rotate
`TCG_AUTH_TICKET` here the same way you would locally, by re-copying the
cookie and updating the variable's value — no redeploy needed for that
alone, though Northflank may restart the service to apply it.

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
