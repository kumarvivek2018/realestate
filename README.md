# Dubai property investment research

Personal research project: filter Dubai studio / 1BHK buy-to-let opportunities by yield + appreciation, using public data only.

## Two data paths

### A. Live asking prices (Property Finder, public listings)
What's currently *advertised* for sale and rent. Reachable from any IP. Gives us:
- Current asking-price/sqft per tower
- Current asking annual rent per tower
- Implied "asking yield" per tower
- Days-on-market proxy via `listed_date`
- Active supply per tower (number of live listings)

### B. Registered transactions (Dubai Land Department)
What actually *cleared* (sales) and what tenants are *paying* (Ejari rent contracts). UAE-only — needs a Dubai-routed connection. Gives us:
- True transaction prices per tower over time
- Pre-boom (2018–2020) baseline vs current
- Real (not asking) rents
- Net absorption / supply pressure

The PF asking data and DLD transaction data should be combined: PF tells you what to bid against; DLD tells you whether the asking prices are realistic.

## Setup

```bash
uv sync
uv run playwright install chromium   # only needed for DLD download
```

## Pull data

### PF asking prices (no VPN needed)

```bash
uv run python scripts/scrape_pf.py                # all 8 scopes, ~30 min
uv run python scripts/scrape_pf.py --scope jlt_sale_studio --max-pages 3   # quick test
```

Output: `data/raw/pf_listings_<date>.parquet` (~ thousands of rows).

### DLD transactions + Ejari rents (Dubai network only)

The official `dubailand.gov.ae/en/open-data/real-estate-data/` form requires
date inputs. The script fills them and clicks "Download as CSV" via headless
Chromium.

```bash
# defaults: transactions 2018-now, rents last 24 months
uv run python scripts/download_dld.py

# custom
uv run python scripts/download_dld.py transactions --from 2020-01-01 --to 2026-05-03

# debug visually
uv run python scripts/download_dld.py transactions --headful --timeout-min 30
```

Output: `data/raw/transactions_<date>_*.csv`, `data/raw/rents_<date>_*.csv`.

## Analyze

### Asking-price summary (PF only — runs immediately)

```bash
uv run python analysis/pf_tower_summary.py --min-listings 4 --budget-max 1000000
uv run python analysis/pf_deal_hunter.py --budget-max 800000 --min-discount 8 --min-yield 7
```

### Transaction-based summary (after DLD download)

```bash
uv run python analysis/clean.py
uv run python analysis/area_summary.py --area "Marina" --area "Jumeirah Lake Towers"
```

`area_summary.py` reports 2020 baseline / current / 12-month change separately
so you can see how much "appreciation" is just 2021–2024 boom inflation.

## Layout

```
scripts/
  download_dld.py        # Playwright form-driver, requires Dubai network
  discover_dld_form.py   # Diagnostic: dumps form fields if download breaks
  scrape_pf.py           # Pulls __NEXT_DATA__ from PF search pages
  inspect_csv.py         # Schema + row-count for any CSV

analysis/
  pf_tower_summary.py    # Per-tower asking price/rent/yield (no DLD needed)
  pf_deal_hunter.py      # Lists individual PF properties priced below tower median
  clean.py               # Filters DLD CSVs to apartments → Parquet
  area_summary.py        # Per-area + per-tower DLD-based yields and momentum

data/
  raw/                   # source files (gitignored)
  processed/             # cleaned parquet + summary CSVs (gitignored)
```

## Notes

- **Property Finder is your employer.** This pipeline uses only public listings; if you want richer signals (asking-price history, time-on-market, view counts, etc.), the internal data warehouse will be far better than scraping.
- **Bayut** — currently blocked by Cloudflare bot challenge. Adding it would require a full Playwright session with anti-detection. PF alone gives full inventory so this is deprioritized.
- **DLD geo-restriction** — `dubaipulse.gov.ae` (the old API host) and the DLD CSV-export backend appear to UAE-only. The page itself loads anywhere via Cloudflare, but the export hangs without a UAE IP.
