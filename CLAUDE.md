# CLAUDE.md

Notes for future Claude sessions working in this repo.

## What this is

Personal Dubai property investment research. Buy-to-let analysis using public data only. The user wants to identify studio / 1BHK opportunities optimizing for monthly rental income AND multi-year capital appreciation.

User is **Vivek Kumar Chaudhary** (`vivek.kumar@propertyfinder.ae`). He works at Property Finder (UAE) — so don't over-explain Dubai market basics. He understands freehold zones, RERA, DLD, etc. The personal repo is on his `kumarvivek2018` GitHub account.

## User constraints (load-bearing)

- **Budget**: AED 400k–1M total purchase. Sweet spot ≤600k. Above 600k must be justified by strong returns.
- **Financing**: mortgage, not cash. Means metric is Return on Equity, not gross yield. Leverage amplifies appreciation upside AND downside.
- **Strategy**: long-term rental only. NOT holiday-home / Airbnb. So no AirDNA, no STR analysis. Bias toward areas with stable family/professional tenants.
- **10-year thesis**: user is bullish on Dubai South / DWC airport corridor, NOT Business Bay. Always include Dubai South in long-horizon scoping. Don't push Business Bay for new positions.

## Critical data realities

- **2021–2024 boom**: Dubai apartment prices roughly **doubled** post-pandemic. Any 3-year or 5-year CAGR computed from 2021–now is heavily inflated. The user explicitly called this out — never present a single CAGR. Always separate **2020 baseline / current / 12-month momentum**.
- **2026–2028 supply trough**: ~100k+ new units delivering, with **~66% being studios + 1BR** (the user's exact target segment). Heavy supply concentrated in JVC, JVT, Dubai South, MBR City, Business Bay, Arjan, Damac Lagoons. Avoid off-plan in those areas.
- **2026 correction underway**: overall Dubai down ~5.9%, rents off 6.7%. JLT/Downtown/Palm seeing ~15% rental softening. JVC/JBR/Burj Khalifa apartments down ~10%.
- **Asking vs clearing gap is huge**: cross-check shows PF asking is **11–24% above DLD actual clearing prices** for most ready apartments. Bid 15–25% below asking on ready stock.

## Data sources & their quirks

| Source | Status | Notes |
|---|---|---|
| **PF (`propertyfinder.ae`)** | ✅ Works from any IP | Listings embedded in `<script id="__NEXT_DATA__">` JSON. `pageProps.searchResult.listings[]` with `property` sub-object. |
| **DLD (`dubailand.gov.ae`)** | ⚠️ Geo + reCAPTCHA-gated | Form at `/en/open-data/real-estate-data/`. Requires date range + reCAPTCHA + click "Search" before "Download as CSV" works. Backend appears UAE-IP-only. **User does the captcha + download manually**, drops CSV in `data/raw/transactions_<date>.csv`. |
| **Bayut (`bayut.com`)** | ❌ hCaptcha to all detected automation | Listings load client-side via Algolia; credentials JS-injected (not in HTML). Stealth shims weren't enough — Bayut serves hCaptcha to headless. `scrape_bayut.py` exists with `--headful` mode for human-in-loop, but defaulted to skip since PF gives full coverage. |
| **Dubai Pulse (`dubaipulse.gov.ae`)** | ⚠️ Migrated to `data.dubai`, geo-restricted | Old direct CSV URLs all 301 to a "Page under development" landing page. The DLD form is the working path. |

## Schema notes

### DLD `transactions_*.csv` columns
```
TRANSACTION_NUMBER, INSTANCE_DATE, GROUP_EN (Sales/Mortgage/Gifts),
PROCEDURE_EN, IS_OFFPLAN_EN ("Off-Plan"/"Ready"),
IS_FREE_HOLD_EN ("Free Hold"/"Non Free Hold"), USAGE_EN,
AREA_EN, PROP_TYPE_EN, PROP_SB_TYPE_EN (Flat/Villa/...),
TRANS_VALUE, PROCEDURE_AREA, ACTUAL_AREA (sqm), ROOMS_EN ("Studio"/"1 B/R"/...),
PARKING, NEAREST_METRO_EN, NEAREST_MALL_EN, NEAREST_LANDMARK_EN,
TOTAL_BUYER, TOTAL_SELLER, MASTER_PROJECT_EN, PROJECT_EN
```
- `PROJECT_EN` is the building/tower name (uppercase, often without "Tower" suffix).
- `MASTER_PROJECT_EN` is the larger community.
- Filter: `GROUP_EN == 'Sales' AND USAGE_EN == 'Residential' AND PROP_SB_TYPE_EN ~ 'flat|apartment|unit'`.

### PF `__NEXT_DATA__` structure
- `props.pageProps.searchResult.listings[]`: each has `listing_type` ("property" or other) + `property` object.
- `property` keys: `id`, `reference`, `price.{value,currency,period}`, `size.{value,unit}`, `bedrooms` ("studio"/"1"/"2"), `bathrooms`, `title`, `offering_type`, `completion_status` ("completed"/"off_plan"), `location.{full_name,coordinates,slug}`, `broker`, `agent`, `is_verified`, `listed_date` (ISO 8601 with Z), `share_url`, `rera` (string permit), etc.
- `meta.total_count`, `meta.page_count`, `meta.per_page` for pagination.
- `listing_type != 'property'` means an ad/project carousel — skip.

### Area name mismatches (DLD ↔ PF)
- `JUMEIRAH LAKES TOWERS` (DLD) ↔ `Jumeirah Lake Towers` (PF) — plural diff.
- `MADINAT AL MATAAR` (DLD) ↔ `Dubai South` (PF). DLD uses the official toponym, PF uses the colloquial.
- DLD areas are uppercase, PF is title-case.
- Always lowercase + plural-normalize (`lakes` → `lake`) before joining.

### Building name mismatches
- `LAKE VIEW` ↔ `Lake View Tower` — DLD often omits "Tower" suffix.
- `NEW DUBAI GATE2` ↔ `New Dubai Gate 2` — DLD sometimes loses spacing before trailing digits.
- `analysis/cross_check_tower.py:_norm_building_py` handles these via lowercase + suffix-strip + digit-spacing canonicalization.

## Repo layout

```
scripts/
  scrape_pf.py            # Full 27-area PF scrape, ~30k listings, ~45 min
  scrape_pf_areas.py      # Append a subset of areas to today's JSONL
  scrape_pf_jvc.py        # Specific helper for JVC after slug fix
  scrape_bayut.py         # Bayut Algolia scraper (--headful, hCaptcha-gated)
  download_dld.py         # Playwright form-driver for DLD (captcha-blocked headless)
  discover_dld_form.py    # Diagnostic for DLD form fields
  inspect_csv.py          # Schema + row-count helper

analysis/
  clean.py                # DLD CSV → filtered Parquet (apartments, sales only)
  area_summary.py         # DLD per-area + per-tower medians
  pf_tower_summary.py     # PF asking-only per-tower medians + DOM + supply
  pf_deal_hunter.py       # Specific PF listings priced ≥X% below tower asking median
  cross_check.py          # PF asking vs DLD clearing per (area, config)
  cross_check_tower.py    # Same but per (building, config) — the deal-finder

data/
  raw/
    transactions_<date>.csv         # Committed (DLD ground truth, captcha-gated)
    pf_listings_<date>.parquet      # Committed (current snapshot)
    pf_listings_<date>.jsonl        # NOT committed (32 MB redundant backup)
    _bayut_*, _error_*              # NOT committed (debug artifacts)
  processed/
    transactions.parquet            # Committed (cleaned DLD)
    *_summary.csv                   # Committed (analysis outputs)
    cross_check_*.csv               # Committed
```

## Run patterns

```bash
# Full pipeline (assuming DLD CSV is already in place):
uv sync
uv run python analysis/clean.py
uv run python scripts/scrape_pf.py            # ~45 min, 27 areas
uv run python analysis/cross_check.py --ready-only --budget-max 1000000
uv run python analysis/cross_check_tower.py --ready-only --budget-max 1000000

# Add a single area without re-scraping everything:
uv run python scripts/scrape_pf_areas.py motor_city majan
```

## Gotchas (lessons from this project)

1. **Polars schema inference bites with heterogeneous nulls.** If column N is None for the first 100 rows then a string later, `pl.DataFrame(list_of_dicts)` will throw `ComputeError: could not append value`. Either force types in the dict (preferred) OR write to JSONL first then `pl.read_ndjson(infer_schema_length=None)`. We do both. See `scripts/scrape_pf.py:_str_or_none` etc.

2. **`tail -N` in shell pipes hides background-script output until exit.** Symptom: `ps` shows the process alive but the output file is empty. Fix: run with `-u` (unbuffered Python) AND redirect to file directly without `| tail`.

3. **`gh auth setup-git` can leak a PAT** into `~/.gitconfig` as a `url.<TOKEN>@github.com.insteadOf=https://github.com/` rule. Token then shows in plaintext in `git remote -v`. Removed it once already; if you see one again, revoke that token at github.com/settings/tokens and `git config --global --unset-all url.<rule>.insteadof`.

4. **Push attribution**: the repo-local `user.email` is set to the GitHub noreply (`23425686+kumarvivek2018@users.noreply.github.com`). DO NOT change it to `vivek.kumar@propertyfinder.ae` — that's the work email and the user explicitly asked it stay out of personal-repo commits. Push via:
   ```bash
   TOKEN=$(gh auth token --user kumarvivek2018)
   git -c credential.helper= push "https://kumarvivek2018:${TOKEN}@github.com/kumarvivek2018/realestate.git" main:main
   ```
   (because keychain-cached creds default to `vivek-pf` SSH key, which doesn't have access to `kumarvivek2018/realestate`).

5. **PF pagination over-reports**: `meta.page_count` is computed from `total_count / per_page` but the actual server returns 404 for late pages. Stop on 3 consecutive 404s (already in `scrape_scope`).

6. **`expect_download` in Playwright on a captcha page hangs forever.** The DLD form silently fails when reCAPTCHA isn't solved. Before clicking "Download as CSV" via Playwright, you'd need to solve the captcha — which we can't do headless. User does it manually.

## Analytical strategies (what's worked + what to do next)

### What's working
- **Cross-check (PF asking ÷ DLD clearing)** is the single most useful number. It instantly separates "seller fishing" from "real market". Headline: median asking premium is **+15-25% in mature areas**, sometimes **+50–130% in specific buildings** with thin transaction history.
- **Real yield (PF rent ÷ DLD actual sale)** is consistently **1–2 percentage points higher** than the PF-asking yield. Use DLD-clearing as the denominator when sizing buys.
- **Ready-only mode** changes the picture: off-plan transactions are usually at developer sticker prices and skew clearing medians UP. Filter them out for honest read of the resale market.

### What to build next (open tasks)
- **Infra-pipeline tracker** (task #11): scrape RTA press releases + Dubai 2040 Master Plan + WAM news + developer IR pages. Tag announcements to areas. Combine with current yield data into a "forward-infra score". This is the user's leading-indicator request.
- **Tower-level deal hunter using cross-check_tower output**: surface specific listing IDs whose asking is meaningfully below their tower's DLD-clearing median (not just below tower-asking median, which is what `pf_deal_hunter.py` does today).
- **Historical DLD download**: only have Jan–May 2026. To do real boom-distortion analysis, user would need to download 2018, 2019, 2020 ranges separately. Each download requires solving the reCAPTCHA + clicking Search. Would unlock per-month price trends and the actual pre-boom baseline.
- **Auto-refresh PF daily**: the same scrape, run on a cron, would track DOM and price changes per listing. Would let us flag price drops as deal signals.

### Areas to bias toward / away from
- **Toward** (within budget, with data backing): Dubai South (Celestia, MAG 5 Boulevard), Al Furjan (Azizi Plaza, Azizi Star, AZIZI Berton, Prime Residency 3 — all Ibn Battuta metro), JLT (older towers — Lake View, New Dubai Gate, Goldcrest Views), Town Square (Una, Liva — most realistic asking premium near 0%), Arjan (Lincoln Park, Samana Hills, Skyz, Joya Blanca — ≥9% real yield, low asking premium).
- **Away from**: JVC studio (68% off-plan supply, heavy 2026-28 pipeline), Business Bay 1BR off-plan, IMPZ 1BR (44% asking premium = sellers way out of touch), anything with >30% asking premium without a specific catalyst.

## When the user asks for new analysis

Default approach:
1. **Always check `data/processed/cross_check_*.csv` first** — most questions have an answer there already. Saves a re-run.
2. **If a new area is needed**, add it to `scripts/scrape_pf.py:_AREAS` with the correct PF slug (verify the slug returns 200 with a quick HTTP test BEFORE adding). Then `scrape_pf_areas.py <short_name>` to backfill without redoing everything.
3. **Use `--ready-only` by default** for actionable comparisons — off-plan skews everything.
4. **Bid below asking**: when the user is evaluating a specific listing, look up the tower in `cross_check_tower.csv` and quote the DLD clearing price as the realistic bid anchor.
