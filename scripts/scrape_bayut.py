"""Scrape Bayut listings via their Algolia search backend.

Bayut renders the page shell server-side but loads listings client-side
from Algolia. Credentials (appId, apiKey) are injected at JS runtime
and are not present in the HTML. We use Playwright once to load a
search page, intercept the Algolia request, capture the credentials
and request shape, then paginate via direct httpx calls.

⚠️  BAYUT SHOWS hCAPTCHA TO HEADLESS BROWSERS.
On detection, Bayut serves an hCaptcha page instead of the listing page —
no Algolia request fires, the scraper times out. Run with --headful and
solve the hCaptcha manually within ~25s; the script then captures the
session's Algolia credentials and paginates via direct API calls until
the session expires (typically 30–60 min).

The captured request is cached in data/raw/_bayut_algolia.json and reused
on subsequent runs until expired. Re-run with --refresh-creds when stale.

Run:
    # First time / when session expired — solve hCaptcha live:
    uv run python scripts/scrape_bayut.py --headful --refresh-creds
    # Subsequent quick runs while creds are still valid:
    uv run python scripts/scrape_bayut.py
    # Limit scope:
    uv run python scripts/scrape_bayut.py --scope jvc_sale_studio --max-pages 3
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import polars as pl
from playwright.sync_api import sync_playwright
from tqdm import tqdm

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
CREDS_FILE = RAW / "_bayut_algolia.json"

# Map our scope names to the Bayut search URL used to seed credential capture.
# Bayut uses category-slug URLs like /for-sale/apartments/dubai/<area-slug>/
_AREAS = [
    ("marina",            "Dubai Marina",              "dubai-marina"),
    ("jlt",               "Jumeirah Lake Towers",       "jumeirah-lake-towers-jlt"),
    ("jvc",               "Jumeirah Village Circle",    "jumeirah-village-circle-jvc"),
    ("jvt",               "Jumeirah Village Triangle",  "jumeirah-village-triangle-jvt"),
    ("al_furjan",         "Al Furjan",                  "al-furjan"),
    ("town_square",       "Town Square",                "town-square"),
    ("discovery_gardens", "Discovery Gardens",          "discovery-gardens"),
    ("sports_city",       "Dubai Sports City",          "dubai-sports-city"),
    ("impz",              "Dubai Production City",      "dubai-production-city-impz"),
    ("dso",               "Dubai Silicon Oasis",        "dubai-silicon-oasis"),
    ("damac_hills_2",     "DAMAC Hills 2",              "damac-hills-2-akoya-by-damac"),
    ("dubai_south",       "Dubai South",                "dubai-south-dubai-world-central"),
    ("dubai_hills",       "Dubai Hills Estate",         "dubai-hills-estate"),
    ("arjan",             "Arjan",                      "arjan"),
    ("business_bay",      "Business Bay",               "business-bay"),
]

SCOPES: dict[str, dict] = {}
for short, label, slug in _AREAS:
    for cfg_key, cfg_path in (("studio", "studio"), ("1br", "1-bedroom-apartments")):
        # Bayut studio path: /for-sale/studio-apartments/dubai/<slug>/
        # Bayut 1-bed path:  /for-sale/1-bedroom-apartments/dubai/<slug>/
        path_segment = "studio-apartments" if cfg_key == "studio" else "1-bedroom-apartments"
        SCOPES[f"{short}_sale_{cfg_key}"] = {
            "url": f"https://www.bayut.com/for-sale/{path_segment}/dubai/{slug}/",
            "intent": "sale", "area": label,
        }
        SCOPES[f"{short}_rent_{cfg_key}"] = {
            "url": f"https://www.bayut.com/to-rent/{path_segment}/dubai/{slug}/",
            "intent": "rent", "area": label,
        }


_STEALTH_JS = """
// Hide webdriver flag and a few common automation tells.
Object.defineProperty(navigator, 'webdriver', { get: () => false });
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
const orig = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => p.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : orig(p);
"""


def capture_creds(seed_url: str, headful: bool = False) -> dict:
    """Open seed_url in a real browser, intercept the Algolia search request,
    and return its host, headers, and body template."""
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 800},
            locale="en-US",
        )
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()

        def on_request(req):
            try:
                url = req.url
                if req.method != "POST":
                    return
                low = url.lower()
                # Skip third-party telemetry — these often have binary bodies
                # that crash post_data decoding.
                if any(s in low for s in ("sentry", "google-analytics", "googletagmanager",
                                          "facebook.com", "doubleclick", "criteo", "adservice")):
                    return
                # Match Algolia and Bayut-internal search endpoints
                if not any(k in low for k in ("algolia", "/api/search", "bayut-production",
                                              "bayut-search", "/_search")):
                    return
                try:
                    body = req.post_data or ""
                except Exception:
                    body = ""
                captured.append({
                    "url": url,
                    "method": req.method,
                    "headers": dict(req.headers),
                    "post_data": body,
                })
            except Exception:
                # Never let a request handler kill the capture
                pass

        page.on("request", on_request)
        page.goto(seed_url, wait_until="domcontentloaded", timeout=60_000)

        if headful:
            print("\n>>> If you see an hCaptcha challenge, solve it now. <<<", file=sys.stderr)
            print(">>> Waiting up to 120s for the search request to fire... <<<\n", file=sys.stderr)
            wait_seconds = 120
        else:
            wait_seconds = 25

        # Encourage lazy-loaded search to fire
        for _ in range(3):
            try:
                page.mouse.wheel(0, 800)
            except Exception:
                pass
            page.wait_for_timeout(800)

        deadline = time.time() + wait_seconds
        while time.time() < deadline and not captured:
            page.wait_for_timeout(500)

        if not captured:
            try:
                shot = RAW / "_bayut_debug.png"
                page.screenshot(path=str(shot), full_page=True)
                print(f"\n  no Algolia request captured — screenshot saved to {shot}", file=sys.stderr)
                print("  most likely Bayut showed an hCaptcha; re-run with --headful to solve it.", file=sys.stderr)
            except Exception:
                pass
        browser.close()

    if not captured:
        raise RuntimeError("did not capture an Algolia request — Bayut layout may have changed")

    # Pick the search request — it's the one with body containing 'requests' or 'queries'
    search_reqs = [
        c for c in captured
        if c["post_data"]
        and ("requests" in c["post_data"] or "params" in c["post_data"])
    ]
    if not search_reqs:
        search_reqs = captured

    chosen = search_reqs[0]
    print(f"captured Algolia request: {chosen['url'][:120]}")
    return chosen


def algolia_search(creds: dict, scope_url: str, page_no: int) -> dict:
    """Issue a single Algolia search request modelled on the captured one,
    overriding the URL params with the desired page number and scope.

    Bayut's queries use refinements derived from URL slugs. The simplest
    way is: re-issue the captured query body but bump the page param.
    """
    headers = {k: v for k, v in creds["headers"].items()
               if not k.lower().startswith((":", "host", "content-length"))}
    headers.setdefault("Origin", "https://www.bayut.com")
    headers.setdefault("Referer", scope_url)

    body = creds["post_data"]
    # Replace page=N in body. Bayut Algolia bodies use "page=0" as a URL-encoded param.
    body = re.sub(r"page=\d+", f"page={page_no}", body)

    r = httpx.post(creds["url"], headers=headers, content=body, timeout=30.0)
    r.raise_for_status()
    return r.json()


def to_row(hit: dict, scope: dict) -> dict:
    """Map a Bayut Algolia hit to our common listing schema."""
    location = hit.get("location") or []
    # location is typically a list of breadcrumbs; the most-specific is last.
    loc_names = [l.get("name") for l in location if isinstance(l, dict) and l.get("name")]
    building_guess = loc_names[-1] if loc_names else None
    geography = hit.get("geography") or {}

    return {
        "scope_intent": scope["intent"],
        "scope_area": scope["area"],
        "id": hit.get("externalID") or hit.get("id"),
        "reference": hit.get("referenceNumber"),
        "title": hit.get("title"),
        "offering_type": hit.get("rentFrequency") or hit.get("purpose"),
        "completion_status": hit.get("completionStatus"),
        "property_type": hit.get("category", [{}])[0].get("nameSingular") if hit.get("category") else None,
        "bedrooms": str(hit.get("rooms") or "0"),
        "bathrooms": hit.get("baths"),
        "size_sqft": (hit.get("area") or 0) * 10.7639 if hit.get("areaUnit") == "sqm" else hit.get("area"),
        "size_unit": hit.get("areaUnit") or "sqft",
        "price_aed": hit.get("price"),
        "price_period": hit.get("rentFrequency") or "sell",
        "price_per_sqft": None,
        "furnished": hit.get("furnishingStatus"),
        "is_verified": hit.get("isVerified"),
        "is_pf_exclusive": None,
        "is_smart_ad": None,
        "is_premium": hit.get("isPremium"),
        "is_new_construction": (hit.get("completionStatus") == "off-plan"),
        "listed_date": (datetime.fromtimestamp(hit["createdAt"]).isoformat()
                        if isinstance(hit.get("createdAt"), (int, float)) else None),
        "last_refreshed_at": (datetime.fromtimestamp(hit["reactivatedAt"]).isoformat()
                              if isinstance(hit.get("reactivatedAt"), (int, float)) else None),
        "permit_number": hit.get("permitNumber"),
        "location_full_name": ", ".join(loc_names),
        "building": building_guess,
        "lat": geography.get("lat"),
        "lon": geography.get("lng"),
        "broker_name": (hit.get("agency") or {}).get("name"),
        "agent_name": None,
        "share_url": f"https://www.bayut.com/property/details-{hit.get('externalID') or hit.get('id')}.html",
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def scrape_scope(creds: dict, scope_name: str, scope: dict, max_pages: int | None) -> list[dict]:
    rows: list[dict] = []
    # Page 1 to learn pagination
    j = algolia_search(creds, scope["url"], 0)
    results = (j.get("results") or [j])[0] if isinstance(j.get("results"), list) else j
    hits = results.get("hits") or []
    nb_pages = results.get("nbPages") or 1
    nb_hits = results.get("nbHits") or len(hits)
    if max_pages:
        nb_pages = min(nb_pages, max_pages)
    print(f"[{scope_name}] nb_hits={nb_hits}  pages={nb_pages}")
    rows.extend(to_row(h, scope) for h in hits)

    for p_no in tqdm(range(1, nb_pages), desc=scope_name):
        try:
            j = algolia_search(creds, scope["url"], p_no)
            results = (j.get("results") or [j])[0] if isinstance(j.get("results"), list) else j
            hits = results.get("hits") or []
            rows.extend(to_row(h, scope) for h in hits)
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            print(f"[{scope_name}] page {p_no} error: {e}", file=sys.stderr)
        time.sleep(0.4)

    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", action="append", choices=list(SCOPES))
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--refresh-creds", action="store_true",
                    help="re-capture Algolia creds via Playwright even if cached")
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)

    if args.refresh_creds or not CREDS_FILE.exists():
        mode = "headful" if args.headful else "headless"
        print(f"capturing Algolia creds via {mode} browser ...")
        seed = SCOPES["jvc_sale_studio"]["url"]
        creds = capture_creds(seed, headful=args.headful)
        CREDS_FILE.write_text(json.dumps(creds, default=str))
        print(f"saved {CREDS_FILE}")
    else:
        creds = json.loads(CREDS_FILE.read_text())
        print(f"loaded cached creds from {CREDS_FILE.name}")

    targets = args.scope or list(SCOPES)
    all_rows: list[dict] = []
    for scope_name in targets:
        try:
            all_rows.extend(scrape_scope(creds, scope_name, SCOPES[scope_name], args.max_pages))
        except Exception as e:
            print(f"[{scope_name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)

    if not all_rows:
        print("no rows scraped", file=sys.stderr)
        return 1

    df = pl.DataFrame(all_rows)
    out = RAW / f"bayut_listings_{date.today().isoformat()}.parquet"
    df.write_parquet(out, compression="zstd")
    print(f"\nwrote {len(df):,} listings -> {out}")

    print("\nby (area, intent, bedrooms):")
    summary = df.group_by(["scope_area", "scope_intent", "bedrooms"]).agg(
        pl.len().alias("n"),
        pl.col("price_aed").median().alias("median_price"),
    ).sort(["scope_area", "scope_intent", "bedrooms"])
    with pl.Config(tbl_rows=40, tbl_cols=10):
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
