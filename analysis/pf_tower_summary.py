"""Tower-level summary computed purely from Property Finder listings.

This is the analysis we can run *without* DLD data. It uses *asking* prices
and *asking* rents — not registered transactions — so it tells us what the
market is currently advertising, not what's actually clearing. Use it as a
fast proxy until the DLD download finishes.

For each (area, building):
  - n_sale, n_rent              — number of active asking listings
  - median_sale_aed             — median asking price (sale)
  - median_rent_aed             — median asking annual rent
  - median_sale_psf             — median asking AED/sqft
  - asking_gross_yield_pct      — median_rent / median_sale * 100
  - median_days_listed          — proxy for days-on-market
  - share_off_plan_sale_pct     — what fraction of sale supply is off-plan

We separate studios from 1BHK in the output.

Run:
    uv run python analysis/pf_tower_summary.py
    uv run python analysis/pf_tower_summary.py --min-listings 3 --budget-max 1000000
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed"


def latest_pf_parquet() -> Path | None:
    matches = sorted(RAW.glob("pf_listings_*.parquet"))
    return matches[-1] if matches else None


def normalize_bedrooms(s: pl.Expr) -> pl.Expr:
    s = s.cast(pl.Utf8).str.to_lowercase().str.strip_chars()
    return (
        pl.when(s == "studio").then(pl.lit("STUDIO"))
        .when(s.is_in(["1", "1.0", "1br", "1 br"])).then(pl.lit("1BR"))
        .otherwise(pl.lit("OTHER"))
    )


def days_listed(col: str) -> pl.Expr:
    """Days between listed_date (ISO 8601 with Z) and now."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    parsed = pl.col(col).str.strptime(
        pl.Datetime, format="%Y-%m-%dT%H:%M:%SZ", strict=False
    )
    return (pl.lit(now) - parsed).dt.total_days()


def compute(df: pl.DataFrame, min_listings: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    df = df.with_columns(
        normalize_bedrooms(pl.col("bedrooms")).alias("config"),
        days_listed("listed_date").alias("days_listed"),
    ).filter(pl.col("config").is_in(["STUDIO", "1BR"]))

    sale = df.filter(pl.col("scope_intent") == "sale")
    rent = df.filter(pl.col("scope_intent") == "rent")

    sale_agg = sale.group_by(["scope_area", "building", "config"]).agg(
        pl.len().alias("n_sale"),
        pl.col("price_aed").median().alias("median_sale_aed"),
        pl.col("price_aed").quantile(0.25).alias("p25_sale_aed"),
        pl.col("size_sqft").median().alias("median_sqft"),
        ((pl.col("price_aed") / pl.col("size_sqft")).median()).alias("median_sale_psf"),
        pl.col("days_listed").median().alias("median_days_listed_sale"),
        ((pl.col("completion_status") == "off_plan").mean() * 100).alias("share_off_plan_pct"),
    )

    rent_agg = rent.group_by(["scope_area", "building", "config"]).agg(
        pl.len().alias("n_rent"),
        pl.col("price_aed").median().alias("median_rent_aed"),
        pl.col("price_aed").quantile(0.75).alias("p75_rent_aed"),
        pl.col("days_listed").median().alias("median_days_listed_rent"),
    )

    tower = (
        sale_agg.join(rent_agg, on=["scope_area", "building", "config"], how="inner")
        .filter((pl.col("n_sale") >= min_listings) & (pl.col("n_rent") >= min_listings))
        .with_columns(
            (pl.col("median_rent_aed") / pl.col("median_sale_aed") * 100)
            .alias("asking_gross_yield_pct"),
            # An optimistic "deal" yield: best-25% sale price vs best-75% rent
            (pl.col("p75_rent_aed") / pl.col("p25_sale_aed") * 100)
            .alias("deal_yield_pct"),
        )
        .sort("asking_gross_yield_pct", descending=True)
    )

    # Area-level fallback (when individual towers don't have enough listings)
    sale_area = sale.group_by(["scope_area", "config"]).agg(
        pl.len().alias("n_sale"),
        pl.col("price_aed").median().alias("median_sale_aed"),
        ((pl.col("price_aed") / pl.col("size_sqft")).median()).alias("median_sale_psf"),
        pl.col("days_listed").median().alias("median_days_listed_sale"),
        ((pl.col("completion_status") == "off_plan").mean() * 100).alias("share_off_plan_pct"),
    )
    rent_area = rent.group_by(["scope_area", "config"]).agg(
        pl.len().alias("n_rent"),
        pl.col("price_aed").median().alias("median_rent_aed"),
        pl.col("days_listed").median().alias("median_days_listed_rent"),
    )
    area = (
        sale_area.join(rent_area, on=["scope_area", "config"], how="inner")
        .with_columns(
            (pl.col("median_rent_aed") / pl.col("median_sale_aed") * 100)
            .alias("asking_gross_yield_pct"),
        )
        .sort(["scope_area", "config"])
    )

    return area, tower


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--min-listings", type=int, default=3,
                   help="towers need at least N sale AND N rent listings to appear")
    p.add_argument("--budget-max", type=int, default=1_500_000,
                   help="filter tower table to median sale price <= this")
    args = p.parse_args()

    src = latest_pf_parquet()
    if not src:
        print("no pf_listings_*.parquet in data/raw/. Run scripts/scrape_pf.py first.", file=sys.stderr)
        return 1

    df = pl.read_parquet(src)
    print(f"loaded {len(df):,} listings from {src.name}")
    print(f"breakdown: {df.group_by(['scope_area', 'scope_intent']).agg(pl.len().alias('n')).sort(['scope_area','scope_intent'])}")

    area, tower = compute(df, args.min_listings)

    OUT.mkdir(parents=True, exist_ok=True)
    area.write_csv(OUT / "pf_area_summary.csv")
    tower.write_csv(OUT / "pf_tower_summary.csv")

    print("\n=== AREA × CONFIG (asking prices & rents) ===")
    with pl.Config(tbl_rows=20, tbl_cols=12, fmt_str_lengths=40, float_precision=0):
        print(area)

    tower_filtered = tower.filter(pl.col("median_sale_aed") <= args.budget_max)
    print(f"\n=== TOWERS within budget {args.budget_max:,} (top 30 by yield) ===")
    with pl.Config(tbl_rows=30, tbl_cols=12, fmt_str_lengths=45, float_precision=0):
        print(tower_filtered.head(30))

    print(f"\nfull outputs:")
    print(f"  {OUT / 'pf_area_summary.csv'}")
    print(f"  {OUT / 'pf_tower_summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
