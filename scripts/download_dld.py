"""Download DLD open-data CSVs by driving the official form via Playwright.

DLD's public download path is the form-based UI at
https://dubailand.gov.ae/en/open-data/real-estate-data/ — tabs for
Transactions, Rents, etc., each requires a date range and triggers a
server-side CSV export when "Download as CSV" is clicked.

The export backend appears UAE-restricted. Run from a Dubai network /
Dubai-routed VPN if the click hangs.

Run:
    uv run python scripts/download_dld.py                   # transactions + rents, default ranges
    uv run python scripts/download_dld.py transactions --from 2018-01-01 --to 2026-05-03
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
PAGE = "https://dubailand.gov.ae/en/open-data/real-estate-data/"

# Each tab in the form. The required date input ids vary per tab — DLD
# names them with a tab-specific prefix (transaction_pFromDate, rent_pFromDate, etc.).
TABS = {
    "transactions": {"label": "Transactions", "from_id": "transaction_pFromDate", "to_id": "transaction_pToDate"},
    "rents":        {"label": "Rents",        "from_id": "rent_pFromDate",        "to_id": "rent_pToDate"},
    "projects":     {"label": "Project",      "from_id": "project_pFromDate",     "to_id": "project_pToDate"},
    "valuations":   {"label": "Valuations",   "from_id": "valuations_pFromDate",  "to_id": "valuations_pToDate"},
    "lands":        {"label": "Land",         "from_id": "land_pFromDate",        "to_id": "land_pToDate"},
    "buildings":    {"label": "Building",     "from_id": "building_pFromDate",    "to_id": "building_pToDate"},
    "units":        {"label": "Unit",         "from_id": "unit_pFromDate",        "to_id": "unit_pToDate"},
    "brokers":      {"label": "Broker",       "from_id": "broker_pFromDate",      "to_id": "broker_pToDate"},
    "developers":   {"label": "Developer",    "from_id": "developer_pFromDate",   "to_id": "developer_pToDate"},
}

# Default date ranges per dataset. Transactions: long history for trend analysis.
# Rents: recent only — older Ejari contracts are stale for current yield calc.
DEFAULT_RANGES = {
    "transactions": (date(2018, 1, 1), date.today()),
    "rents":        (date.today() - timedelta(days=730), date.today()),  # 2 years
}


def fmt_date(d: date) -> str:
    """DLD date inputs use DD/MM/YYYY."""
    return d.strftime("%d/%m/%Y")


def download_tab(page, short: str, cfg: dict, from_d: date, to_d: date, dest: Path, timeout_ms: int) -> Path:
    label = cfg["label"]
    print(f"[{label}] activating tab...")
    page.locator(f"a:has-text('{label}')").first.click()
    page.wait_for_timeout(700)

    # Fill date inputs by id (some are wrapped in datepicker pickers — JS-set the value
    # and dispatch input/change to satisfy any validation listeners).
    for fid, value in ((cfg["from_id"], fmt_date(from_d)), (cfg["to_id"], fmt_date(to_d))):
        sel = f"#{fid}"
        if not page.locator(sel).count():
            print(f"[{label}] WARN: input #{fid} not found", file=sys.stderr)
            continue
        page.evaluate(
            """({selector, value}) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return true;
            }""",
            {"selector": sel, "value": value},
        )
        print(f"[{label}] set {fid} = {value}")

    page.wait_for_timeout(400)
    btn = page.locator("button:has-text('Download as CSV'):visible").first
    btn.wait_for(state="visible", timeout=10_000)
    print(f"[{label}] clicking Download as CSV (range {fmt_date(from_d)} → {fmt_date(to_d)})...")
    with page.expect_download(timeout=timeout_ms) as dl_info:
        btn.click()
    download = dl_info.value
    suggested = download.suggested_filename or f"{short}.csv"
    final = dest / f"{short}_{date.today().isoformat()}_{suggested}"
    download.save_as(final)
    size = final.stat().st_size
    print(f"[{label}] saved {size:,} bytes -> {final}")
    if size < 10_000:
        print(f"[{label}] WARN: file is very small ({size} bytes) — likely an error response, not real data.", file=sys.stderr)
    return final


def parse_iso(s: str) -> date:
    return date.fromisoformat(s)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tabs",
        nargs="*",
        choices=[*TABS.keys(), "all"],
        help="datasets to download (default: transactions + rents)",
    )
    parser.add_argument("--from", dest="from_date", type=parse_iso, default=None,
                        help="from date YYYY-MM-DD (overrides the per-dataset default)")
    parser.add_argument("--to", dest="to_date", type=parse_iso, default=None,
                        help="to date YYYY-MM-DD (overrides the per-dataset default)")
    parser.add_argument("--timeout-min", type=int, default=20,
                        help="per-download timeout in minutes (default 20)")
    parser.add_argument("--headful", action="store_true", help="show the browser (debugging)")
    args = parser.parse_args()

    if not args.tabs:
        wanted = ["transactions", "rents"]
    elif "all" in args.tabs:
        wanted = list(TABS)
    else:
        wanted = args.tabs

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timeout_ms = args.timeout_min * 60 * 1000

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        print(f"loading {PAGE} ...")
        page.goto(PAGE, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_load_state("networkidle", timeout=90_000)

        for short in wanted:
            cfg = TABS[short]
            default_from, default_to = DEFAULT_RANGES.get(short, (date(2020, 1, 1), date.today()))
            from_d = args.from_date or default_from
            to_d = args.to_date or default_to
            try:
                download_tab(page, short, cfg, from_d, to_d, DATA_DIR, timeout_ms)
            except PWTimeout as e:
                print(f"[{cfg['label']}] TIMEOUT: {e}", file=sys.stderr)
                shot = DATA_DIR / f"_error_{short}.png"
                page.screenshot(path=str(shot), full_page=True)
                print(f"  screenshot saved: {shot}", file=sys.stderr)
            except Exception as e:
                print(f"[{cfg['label']}] ERROR: {type(e).__name__}: {e}", file=sys.stderr)

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
