"""Scrape Property Finder public listings into a parquet file.

PF embeds the entire search result as JSON in <script id="__NEXT_DATA__">.
We paginate through each search URL and extract one row per listing.

Default scope: studios + 1BHK in Marina, JLT — both for-sale and for-rent.
Adjust SCOPES below or pass --scope to limit.

Run:
    uv run python scripts/scrape_pf.py
    uv run python scripts/scrape_pf.py --scope marina_sale_studio
    uv run python scripts/scrape_pf.py --max-pages 5    # for quick testing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path
from typing import Iterable

import httpx
import polars as pl
from tqdm import tqdm

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

OUT = Path(__file__).resolve().parent.parent / "data" / "raw"

# url-template, intent, area for column tagging
SCOPES: dict[str, dict] = {
    "marina_sale_studio": {
        "url": "https://www.propertyfinder.ae/en/buy/dubai/studio-apartments-for-sale-dubai-marina.html",
        "intent": "sale", "area": "Dubai Marina",
    },
    "marina_sale_1br": {
        "url": "https://www.propertyfinder.ae/en/buy/dubai/1-bedroom-apartments-for-sale-dubai-marina.html",
        "intent": "sale", "area": "Dubai Marina",
    },
    "marina_rent_studio": {
        "url": "https://www.propertyfinder.ae/en/rent/dubai/studio-apartments-for-rent-dubai-marina.html",
        "intent": "rent", "area": "Dubai Marina",
    },
    "marina_rent_1br": {
        "url": "https://www.propertyfinder.ae/en/rent/dubai/1-bedroom-apartments-for-rent-dubai-marina.html",
        "intent": "rent", "area": "Dubai Marina",
    },
    "jlt_sale_studio": {
        "url": "https://www.propertyfinder.ae/en/buy/dubai/studio-apartments-for-sale-jumeirah-lake-towers.html",
        "intent": "sale", "area": "Jumeirah Lake Towers",
    },
    "jlt_sale_1br": {
        "url": "https://www.propertyfinder.ae/en/buy/dubai/1-bedroom-apartments-for-sale-jumeirah-lake-towers.html",
        "intent": "sale", "area": "Jumeirah Lake Towers",
    },
    "jlt_rent_studio": {
        "url": "https://www.propertyfinder.ae/en/rent/dubai/studio-apartments-for-rent-jumeirah-lake-towers.html",
        "intent": "rent", "area": "Jumeirah Lake Towers",
    },
    "jlt_rent_1br": {
        "url": "https://www.propertyfinder.ae/en/rent/dubai/1-bedroom-apartments-for-rent-jumeirah-lake-towers.html",
        "intent": "rent", "area": "Jumeirah Lake Towers",
    },
}


NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def parse_page(html: str) -> tuple[list[dict], dict]:
    m = NEXT_DATA_RE.search(html)
    if not m:
        return [], {}
    data = json.loads(m.group(1))
    sr = data.get("props", {}).get("pageProps", {}).get("searchResult", {})
    return sr.get("listings") or [], sr.get("meta") or {}


def location_part(location: dict, depth: int) -> str | None:
    """Walk PF's location.full_name from least-specific to most-specific."""
    if not location:
        return None
    full = location.get("full_name") or ""
    parts = [p.strip() for p in full.split(",")]
    # parts[0] is most-specific (building), parts[-1] is "Dubai"
    return parts[depth] if depth < len(parts) else None


def listing_to_row(listing: dict, intent: str, area: str) -> dict | None:
    if listing.get("listing_type") != "property":
        return None
    p = listing.get("property") or {}
    if not p:
        return None
    price = p.get("price") or {}
    size = p.get("size") or {}
    location = p.get("location") or {}
    broker = p.get("broker") or {}
    agent = p.get("agent") or {}

    full_name = location.get("full_name") or ""
    parts = [s.strip() for s in full_name.split(",")]
    # heuristics: parts[0] is the most specific (often building/tower),
    # parts[-2] is area, parts[-1] is Dubai.
    building = parts[0] if parts else None

    return {
        "scope_intent": intent,
        "scope_area": area,
        "id": p.get("id"),
        "reference": p.get("reference"),
        "title": p.get("title"),
        "offering_type": p.get("offering_type"),
        "completion_status": p.get("completion_status"),
        "property_type": p.get("property_type"),
        "bedrooms": str(p.get("bedrooms")),
        "bathrooms": p.get("bathrooms"),
        "size_sqft": size.get("value") if size.get("unit") == "sqft" else None,
        "size_unit": size.get("unit"),
        "price_aed": price.get("value"),
        "price_period": price.get("period"),
        "price_per_sqft": p.get("price_per_area"),
        "furnished": p.get("furnished"),
        "is_verified": p.get("is_verified"),
        "is_pf_exclusive": p.get("is_pf_exclusive"),
        "is_smart_ad": p.get("is_smart_ad"),
        "is_premium": p.get("is_premium"),
        "is_new_construction": p.get("is_new_construction"),
        "listed_date": p.get("listed_date"),
        "last_refreshed_at": p.get("last_refreshed_at"),
        "permit_number": p.get("rera") if isinstance(p.get("rera"), str) else p.get("permit_number"),
        "location_full_name": full_name,
        "building": building,
        "lat": (location.get("coordinates") or {}).get("lat"),
        "lon": (location.get("coordinates") or {}).get("lon"),
        "broker_name": broker.get("name"),
        "agent_name": agent.get("name"),
        "share_url": p.get("share_url"),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def scrape_scope(client: httpx.Client, name: str, scope: dict, max_pages: int | None) -> list[dict]:
    base_url = scope["url"]
    sep = "&" if "?" in base_url else "?"
    rows: list[dict] = []

    # First page to learn pagination
    r = client.get(base_url, headers={"User-Agent": UA}, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    listings, meta = parse_page(r.text)
    page_count = meta.get("page_count") or 1
    total_count = meta.get("total_count") or len(listings)
    if max_pages:
        page_count = min(page_count, max_pages)
    print(f"[{name}] total_count={total_count}  page_count={page_count}  per_page={meta.get('per_page')}")

    rows.extend(filter(None, (listing_to_row(l, scope["intent"], scope["area"]) for l in listings)))

    consecutive_404 = 0
    for page in tqdm(range(2, page_count + 1), desc=name):
        url = f"{base_url}{sep}page={page}"
        try:
            r = client.get(url, headers={"User-Agent": UA}, timeout=30.0, follow_redirects=True)
            if r.status_code == 404:
                consecutive_404 += 1
                if consecutive_404 >= 3:
                    print(f"[{name}] stopping at page {page}: 3 consecutive 404s (real page count < {page_count})")
                    break
                continue
            r.raise_for_status()
            consecutive_404 = 0
            listings, _ = parse_page(r.text)
            rows.extend(filter(None, (listing_to_row(l, scope["intent"], scope["area"]) for l in listings)))
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            print(f"[{name}] page {page} error: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(0.6)

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", action="append", choices=list(SCOPES.keys()),
                        help="restrict to specific scope(s); default = all")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="cap pages per scope (for testing)")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    targets = args.scope or list(SCOPES)

    all_rows: list[dict] = []
    with httpx.Client() as client:
        for name in targets:
            try:
                all_rows.extend(scrape_scope(client, name, SCOPES[name], args.max_pages))
            except httpx.HTTPError as e:
                print(f"[{name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)

    if not all_rows:
        print("no rows scraped", file=sys.stderr)
        return 1

    df = pl.DataFrame(all_rows)
    out = OUT / f"pf_listings_{date.today().isoformat()}.parquet"
    df.write_parquet(out, compression="zstd")
    print(f"\nwrote {len(df):,} listings -> {out}")

    # quick summary
    print("\nby (area, intent, bedrooms):")
    summary = df.group_by(["scope_area", "scope_intent", "bedrooms"]).agg(
        pl.len().alias("n"),
        pl.col("price_aed").median().alias("median_price"),
        pl.col("size_sqft").median().alias("median_sqft"),
    ).sort(["scope_area", "scope_intent", "bedrooms"])
    with pl.Config(tbl_rows=30, tbl_cols=10):
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
