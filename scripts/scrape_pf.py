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
# Each PF search URL uses an area-slug. Studio + 1BR both for sale and rent.
# Areas chosen: existing mature freehold (Marina, JLT) + budget-relevant
# affordable (JVC, JVT, Al Furjan, Town Square, Discovery Gardens, Sports
# City, IMPZ, DSO, Damac Hills 2) + southern-thesis (Dubai South, Dubai Hills,
# Arjan, MBR City). Easy to add more — copy a row.
_AREAS = [
    ("marina",                  "Dubai Marina",                 "dubai-marina"),
    ("jlt",                     "Jumeirah Lake Towers",          "jumeirah-lake-towers"),
    ("jvc",                     "Jumeirah Village Circle",       "jumeirah-village-circle"),
    ("jvt",                     "Jumeirah Village Triangle",     "jumeirah-village-triangle"),
    ("al_furjan",               "Al Furjan",                     "al-furjan"),
    ("town_square",             "Town Square",                   "town-square"),
    ("discovery_gardens",       "Discovery Gardens",             "discovery-gardens"),
    ("sports_city",             "Dubai Sports City",             "dubai-sports-city"),
    ("impz",                    "Dubai Production City",         "dubai-production-city-impz"),
    ("dso",                     "Dubai Silicon Oasis",           "dubai-silicon-oasis"),
    ("damac_hills_2",           "DAMAC Hills 2",                 "damac-hills-2"),
    ("dubai_south",             "Dubai South",                   "dubai-south-dubai-world-central"),
    ("dubai_hills",             "Dubai Hills Estate",            "dubai-hills-estate"),
    ("arjan",                   "Arjan",                         "arjan"),
    ("business_bay",            "Business Bay",                  "business-bay"),
    # round 2: high-volume areas DLD shows transactions in but were missing
    ("motor_city",              "Motor City",                    "motor-city"),
    ("majan",                   "Majan",                         "majan"),
    ("meydan_one",              "Meydan One",                    "meydan-one"),
    ("liwan",                   "Liwan",                         "liwan"),
    ("damac_hills",             "DAMAC Hills",                   "damac-hills"),
    ("dubai_studio_city",       "Dubai Studio City",             "dubai-studio-city"),
    ("international_city",      "International City",            "international-city"),
    ("dubai_creek_harbour",     "Dubai Creek Harbour",           "dubai-creek-harbour-the-lagoons"),
    ("dubai_science_park",      "Dubai Science Park",            "dubai-science-park"),
    ("dubai_land_residence",    "Dubai Land Residence Complex",  "dubai-land-residence-complex"),
    ("palm_jumeirah",           "Palm Jumeirah",                 "palm-jumeirah"),
    ("downtown",                "Downtown Dubai",                "downtown-dubai"),
]

_BUY_TPL = "https://www.propertyfinder.ae/en/buy/dubai/{config}-apartments-for-sale-{slug}.html"
_RENT_TPL = "https://www.propertyfinder.ae/en/rent/dubai/{config}-apartments-for-rent-{slug}.html"

SCOPES: dict[str, dict] = {}
for short, label, slug in _AREAS:
    for config_key, config_url in (("studio", "studio"), ("1br", "1-bedroom")):
        SCOPES[f"{short}_sale_{config_key}"] = {
            "url": _BUY_TPL.format(config=config_url, slug=slug),
            "intent": "sale", "area": label,
        }
        SCOPES[f"{short}_rent_{config_key}"] = {
            "url": _RENT_TPL.format(config=config_url, slug=slug),
            "intent": "rent", "area": label,
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


def _str_or_none(v):
    return None if v is None else str(v)


def _float_or_none(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool_or_none(v):
    if v is None:
        return None
    return bool(v)


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


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
    building = parts[0] if parts else None

    return {
        "scope_intent": intent,
        "scope_area": area,
        "id": _int_or_none(p.get("id")),
        "reference": _str_or_none(p.get("reference")),
        "title": _str_or_none(p.get("title")),
        "offering_type": _str_or_none(p.get("offering_type")),
        "completion_status": _str_or_none(p.get("completion_status")),
        "property_type": _str_or_none(p.get("property_type")),
        "bedrooms": _str_or_none(p.get("bedrooms")),
        "bathrooms": _str_or_none(p.get("bathrooms")),
        "size_sqft": _float_or_none(size.get("value")) if size.get("unit") == "sqft" else None,
        "size_unit": _str_or_none(size.get("unit")),
        "price_aed": _float_or_none(price.get("value")),
        "price_period": _str_or_none(price.get("period")),
        "price_per_sqft": _float_or_none(p.get("price_per_area")),
        "furnished": _str_or_none(p.get("furnished")),
        "is_verified": _bool_or_none(p.get("is_verified")),
        "is_pf_exclusive": _bool_or_none(p.get("is_pf_exclusive")),
        "is_smart_ad": _bool_or_none(p.get("is_smart_ad")),
        "is_premium": _bool_or_none(p.get("is_premium")),
        "is_new_construction": _bool_or_none(p.get("is_new_construction")),
        "listed_date": _str_or_none(p.get("listed_date")),
        "last_refreshed_at": _str_or_none(p.get("last_refreshed_at")),
        "permit_number": _str_or_none(p.get("rera") if isinstance(p.get("rera"), str) else p.get("permit_number")),
        "location_full_name": full_name,
        "building": _str_or_none(building),
        "lat": _float_or_none((location.get("coordinates") or {}).get("lat")),
        "lon": _float_or_none((location.get("coordinates") or {}).get("lon")),
        "broker_name": _str_or_none(broker.get("name")),
        "agent_name": _str_or_none(agent.get("name")),
        "share_url": _str_or_none(p.get("share_url")),
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

    # Save incrementally per-scope so a crash doesn't lose hours of work.
    today = date.today().isoformat()
    incremental_jsonl = OUT / f"pf_listings_{today}.jsonl"
    incremental_jsonl.unlink(missing_ok=True)

    all_rows: list[dict] = []
    with httpx.Client() as client:
        for name in targets:
            try:
                rows = scrape_scope(client, name, SCOPES[name], args.max_pages)
                all_rows.extend(rows)
                # Append to JSONL after each scope completes
                with incremental_jsonl.open("a") as fh:
                    for r in rows:
                        fh.write(json.dumps(r, default=str) + "\n")
                print(f"[{name}] +{len(rows)} rows (total: {len(all_rows):,})")
            except httpx.HTTPError as e:
                print(f"[{name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)

    if not all_rows:
        print("no rows scraped", file=sys.stderr)
        return 1

    # Build DataFrame from JSONL (more robust than from a list of dicts —
    # polars handles the schema across heterogeneous nulls better).
    df = pl.read_ndjson(incremental_jsonl, infer_schema_length=None)
    out = OUT / f"pf_listings_{today}.parquet"
    df.write_parquet(out, compression="zstd")
    print(f"\nwrote {len(df):,} listings -> {out}")
    print(f"  (raw JSONL kept at {incremental_jsonl} as a backup)")

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
