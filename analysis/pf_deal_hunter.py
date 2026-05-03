"""Find specific PF listings that look like deals.

A deal = a sale listing in a target tower whose asking price is meaningfully
below the tower's median asking price for the same config (studio or 1BR),
AND whose tower has a healthy asking-rent base (otherwise the discount is
probably the market repricing the whole tower down).

Output: a ranked CSV of listing IDs with the discount %, the implied yield,
direct PF URL, agent, listed_date, and freshness.

Run:
    uv run python analysis/pf_deal_hunter.py
    uv run python analysis/pf_deal_hunter.py --budget-max 800000 --min-discount 8
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


def latest_pf() -> Path | None:
    matches = sorted(RAW.glob("pf_listings_*.parquet"))
    return matches[-1] if matches else None


def normalize_bedrooms(s: pl.Expr) -> pl.Expr:
    s = s.cast(pl.Utf8).str.to_lowercase().str.strip_chars()
    return (
        pl.when(s == "studio").then(pl.lit("STUDIO"))
        .when(s.is_in(["1", "1.0", "1br", "1 br"])).then(pl.lit("1BR"))
        .otherwise(pl.lit("OTHER"))
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget-max", type=int, default=1_000_000,
                    help="exclude listings priced above this AED (default 1M)")
    ap.add_argument("--budget-min", type=int, default=300_000,
                    help="exclude listings priced below this AED (default 300k)")
    ap.add_argument("--min-discount", type=float, default=5.0,
                    help="minimum % below tower median to qualify (default 5)")
    ap.add_argument("--min-tower-listings", type=int, default=4,
                    help="tower must have at least this many sale listings to be benchmark-able")
    ap.add_argument("--min-yield", type=float, default=6.0,
                    help="minimum implied tower-asking gross yield (default 6%%)")
    ap.add_argument("--exclude-off-plan", action="store_true",
                    help="exclude off-plan listings")
    args = ap.parse_args()

    src = latest_pf()
    if not src:
        print("no pf_listings parquet — run scripts/scrape_pf.py first", file=sys.stderr)
        return 1

    df = pl.read_parquet(src).with_columns(
        normalize_bedrooms(pl.col("bedrooms")).alias("config"),
    ).filter(pl.col("config").is_in(["STUDIO", "1BR"]))

    sale = df.filter(pl.col("scope_intent") == "sale").filter(
        pl.col("price_aed").is_between(args.budget_min, args.budget_max),
    )
    if args.exclude_off_plan:
        sale = sale.filter(pl.col("completion_status") != "off_plan")

    rent = df.filter(pl.col("scope_intent") == "rent")

    # Tower medians
    tower_sale = sale.group_by(["scope_area", "building", "config"]).agg(
        pl.len().alias("tower_n_sale"),
        pl.col("price_aed").median().alias("tower_median_sale_aed"),
        pl.col("size_sqft").median().alias("tower_median_sqft"),
    ).filter(pl.col("tower_n_sale") >= args.min_tower_listings)

    tower_rent = rent.group_by(["scope_area", "building", "config"]).agg(
        pl.len().alias("tower_n_rent"),
        pl.col("price_aed").median().alias("tower_median_rent_aed"),
    )

    benchmark = tower_sale.join(
        tower_rent, on=["scope_area", "building", "config"], how="inner"
    ).with_columns(
        (pl.col("tower_median_rent_aed") / pl.col("tower_median_sale_aed") * 100)
        .alias("tower_asking_yield_pct"),
    ).filter(pl.col("tower_asking_yield_pct") >= args.min_yield)

    # Join each individual sale listing back to its tower benchmark
    deals = sale.join(
        benchmark, on=["scope_area", "building", "config"], how="inner"
    ).with_columns(
        ((1 - pl.col("price_aed") / pl.col("tower_median_sale_aed")) * 100)
        .alias("discount_vs_tower_pct"),
        # Implied yield assumes this unit will rent at the tower's median rent
        (pl.col("tower_median_rent_aed") / pl.col("price_aed") * 100)
        .alias("implied_yield_pct"),
    ).filter(pl.col("discount_vs_tower_pct") >= args.min_discount)

    # Days listed (freshness)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    parsed = pl.col("listed_date").str.strptime(
        pl.Datetime, format="%Y-%m-%dT%H:%M:%SZ", strict=False
    )
    deals = deals.with_columns(
        ((pl.lit(now) - parsed).dt.total_days()).alias("days_listed"),
    )

    # Final shortlist
    cols = [
        "scope_area", "building", "config", "price_aed", "size_sqft",
        "discount_vs_tower_pct", "implied_yield_pct", "tower_asking_yield_pct",
        "tower_median_sale_aed", "tower_median_rent_aed",
        "tower_n_sale", "tower_n_rent",
        "completion_status", "is_verified", "is_pf_exclusive",
        "days_listed", "agent_name", "broker_name", "title", "share_url", "id",
    ]
    available = [c for c in cols if c in deals.columns]
    deals = deals.select(available).sort("discount_vs_tower_pct", descending=True)

    OUT.mkdir(parents=True, exist_ok=True)
    out_csv = OUT / "pf_deals.csv"
    deals.write_csv(out_csv)

    print(f"\nfound {len(deals):,} candidate deals")
    print(f"  budget {args.budget_min:,} – {args.budget_max:,}")
    print(f"  min discount: {args.min_discount}%   min tower yield: {args.min_yield}%")
    print(f"  saved to {out_csv}\n")

    if len(deals):
        with pl.Config(tbl_rows=25, tbl_cols=12, fmt_str_lengths=40, float_precision=1):
            print(deals.head(25))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
