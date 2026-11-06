"""
check_sales.py -- run TCGPlayer's latestsales request a few ways and print how
many sales each returns, so you can see what changes the count.

    python3 check_sales.py 662184
    TCG_AUTH_TICKET='<cookie value>' python3 check_sales.py 662184

Run it once without and once with TCG_AUTH_TICKET and compare.
"""
import os
import sys
from collections import Counter

import tcg_scraper as t

product_id = int(sys.argv[1]) if len(sys.argv) > 1 else 662184
url = t.LATEST_SALES_URL.format(product_id=product_id)
ticket = os.environ.get("TCG_AUTH_TICKET", "").strip()
print(f"product {product_id} | TCG_AUTH_TICKET set: {bool(ticket)} (length {len(ticket)})")

s = t._session()
print("auth cookie on session:", any(c.name == "TCGAuthTicket_Production" for c in s.cookies))

base = {"conditions": [], "variants": [], "languages": [], "listingType": "All"}
tests = {
    "no filters": base,
    "variants Holofoil+Unlimited Holofoil (11,123)": dict(base, variants=[11, 123]),
    "variants Holofoil only (11)": dict(base, variants=[11]),
}
for label, payload in tests.items():
    r = s.post(url, json=payload, timeout=15)
    if r.status_code != 200:
        print(f"{label}: HTTP {r.status_code} {r.text[:200]}")
        continue
    body = r.json()
    rows = body.get("data", [])
    by = Counter((x.get("condition"), x.get("language"), x.get("variant")) for x in rows)
    print(f"\n{label}: {len(rows)} sales (totalResults={body.get('totalResults')})")
    for key, n in sorted(by.items(), key=lambda kv: str(kv[0])):
        print("   ", key, n)
