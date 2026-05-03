"""DLD transaction summary at area + tower (PROJECT_EN) level.

Reads data/processed/transactions.parquet (cleaned via analysis/clean.py)
and produces:
  1. Area-level snapshot (median price, median psf, n_tx, off-plan share,
     freehold share)
  2. Tower-level snapshot (same but per PROJECT_EN), filtered by min tx
     count and budget.

The downloaded DLD CSV is currently a 4-month slice (Jan 1 → May 2 2026),
so this is a *current-period* snapshot, not a multi-year trend. To extend,
download more date ranges via the DLD form and they'll concatenate cleanly.

Run:
    uv run python analysis/area_summary.py
    uv run python analysis/area_summary.py --area "Marina" --area "Lake Towers"
    uv run python analysis/area_summary.py --ready-only --budget-max 1000000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--area", action="append", default=None,
                   help="restrict to area (substring, can pass multiple)")
    p.add_argument("--budget-max", type=int, default=2_000_000,
                   help="filter tower table to median price <= this")
    p.add_argument("--min-tx", type=int, default=5,
                   help="towers need at least this many sales to appear")
    p.add_argument("--ready-only", action="store_true",
                   help="exclude off-plan transactions")
    args = p.parse_args()

    src = PROCESSED / "transactions.parquet"
    if not src.exists():
        print("missing transactions.parquet — run analysis/clean.py first", file=sys.stderr)
        return 1

    tx = pl.read_parquet(src)
    print(f"loaded {len(tx):,} sales (range {tx['tx_date'].min()} → {tx['tx_date'].max()})")

    if args.ready_only:
        tx = tx.filter(~pl.col("is_offplan"))
        print(f"  ready-only filter: {len(tx):,} remaining")

    if args.area:
        pat = "(?i)" + "|".join(args.area)
        tx = tx.filter(pl.col("AREA_EN").str.contains(pat))
        print(f"  area filter: {len(tx):,} remaining")

    tx = tx.filter(pl.col("config").is_in(["STUDIO", "1BR", "2BR", "3BR+"]))

    # Area summary
    area = (
        tx.group_by(["AREA_EN", "config"])
        .agg(
            pl.len().alias("n_tx"),
            pl.col("price_aed").median().alias("median_price"),
            pl.col("price_aed").quantile(0.25).alias("p25_price"),
            pl.col("price_aed").quantile(0.75).alias("p75_price"),
            pl.col("price_per_sqft").median().alias("median_psf"),
            pl.col("sqft").median().alias("median_sqft"),
            (pl.col("is_offplan").cast(pl.Float64).mean() * 100).alias("offplan_share_pct"),
            (pl.col("is_freehold").cast(pl.Float64).mean() * 100).alias("freehold_share_pct"),
        )
        .filter(pl.col("n_tx") >= args.min_tx)
        .sort(["AREA_EN", "config"])
    )

    # Tower summary (PROJECT_EN)
    tower = (
        tx
        .filter(pl.col("PROJECT_EN").is_not_null() & (pl.col("PROJECT_EN") != ""))
        .group_by(["AREA_EN", "PROJECT_EN", "config"])
        .agg(
            pl.len().alias("n_tx"),
            pl.col("price_aed").median().alias("median_price"),
            pl.col("price_aed").quantile(0.25).alias("p25_price"),
            pl.col("price_per_sqft").median().alias("median_psf"),
            pl.col("sqft").median().alias("median_sqft"),
            (pl.col("is_offplan").cast(pl.Float64).mean() * 100).alias("offplan_share_pct"),
            pl.col("NEAREST_METRO_EN").mode().first().alias("nearest_metro"),
            pl.col("NEAREST_LANDMARK_EN").mode().first().alias("nearest_landmark"),
        )
        .filter(pl.col("n_tx") >= args.min_tx)
        .filter(pl.col("median_price") <= args.budget_max)
        .sort(["n_tx"], descending=True)
    )

    PROCESSED.mkdir(parents=True, exist_ok=True)
    area.write_csv(PROCESSED / "dld_area_summary.csv")
    tower.write_csv(PROCESSED / "dld_tower_summary.csv")

    print(f"\n=== AREA × CONFIG (DLD actuals) ===\n")
    with pl.Config(tbl_rows=80, tbl_cols=12, fmt_str_lengths=30, float_precision=0,
                   tbl_width_chars=200):
        print(area)

    print(f"\n=== TOP TOWERS by transaction count (≤ {args.budget_max:,}, ≥ {args.min_tx} sales) ===\n")
    with pl.Config(tbl_rows=40, tbl_cols=12, fmt_str_lengths=35, float_precision=0,
                   tbl_width_chars=220):
        print(tower.head(40))

    print(f"\nfull outputs:")
    print(f"  {PROCESSED / 'dld_area_summary.csv'}")
    print(f"  {PROCESSED / 'dld_tower_summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
