"""Tower-level PF×DLD cross-check: building-by-building asking premium.

Joins:
  PF (scope_area, building, config, scope_intent=sale)
  PF (scope_area, building, config, scope_intent=rent)
  DLD (AREA_EN, PROJECT_EN, config)

via a normalized building key (lower-cased, "Tower" suffix stripped,
spacing canonicalized so "NEW DUBAI GATE2" matches "New Dubai Gate 2").

Output: per-tower median DLD actual sale, median PF asking sale,
asking premium %, median PF rent, real implied yield, transaction
counts on both sides, and whether ready / off-plan.

Run:
    uv run python analysis/cross_check_tower.py
    uv run python analysis/cross_check_tower.py --area "marina" --area "lake towers"
    uv run python analysis/cross_check_tower.py --budget-max 700000 --min-tx 4
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed"


def _norm_building_py(s: str | None) -> str | None:
    if s is None:
        return None
    t = s.lower().strip()
    # space-separate trailing digits: "gate2" → "gate 2"
    t = re.sub(r"([a-z])(\d)", r"\1 \2", t)
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    # remove punctuation
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    # remove very common suffix/qualifier words that vary between sources
    t = re.sub(r"\b(tower|towers|residence|residences|apartments|apartment|building|the|by\s+\w+)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def _norm_area_py(s: str | None) -> str | None:
    if s is None:
        return None
    t = s.lower().strip()
    # plural normalize "lakes" → "lake"
    t = re.sub(r"\blakes\b", "lake", t)
    return t


def normalize_pf_bedrooms(s: pl.Expr) -> pl.Expr:
    s = s.cast(pl.Utf8).str.to_lowercase().str.strip_chars()
    return (
        pl.when(s == "studio").then(pl.lit("STUDIO"))
        .when(s.is_in(["1", "1.0"])).then(pl.lit("1BR"))
        .when(s.is_in(["2", "2.0"])).then(pl.lit("2BR"))
        .otherwise(pl.lit("OTHER"))
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--area", action="append", default=None,
                    help="restrict (substring match on area_norm; can pass multiple)")
    ap.add_argument("--budget-max", type=int, default=1_500_000)
    ap.add_argument("--budget-min", type=int, default=300_000)
    ap.add_argument("--min-tx", type=int, default=4,
                    help="DLD must have ≥ this many sales in (area, building, config)")
    ap.add_argument("--min-pf-sale", type=int, default=3,
                    help="PF must have ≥ this many sale listings to compare")
    ap.add_argument("--min-pf-rent", type=int, default=3)
    ap.add_argument("--ready-only", action="store_true",
                    help="exclude off-plan from DLD and PF")
    args = ap.parse_args()

    tx_path = OUT / "transactions.parquet"
    pf_paths = sorted(RAW.glob("pf_listings_*.parquet"))
    if not tx_path.exists() or not pf_paths:
        print("missing input parquet — run analysis/clean.py and scripts/scrape_pf.py first", file=sys.stderr)
        return 1

    tx = pl.read_parquet(tx_path)
    pf = pl.read_parquet(pf_paths[-1])

    if args.ready_only:
        tx = tx.filter(~pl.col("is_offplan"))
        pf = pf.filter(pl.col("completion_status") != "off_plan")
        print("[ready-only mode]")

    # Normalize area + building keys on both sides
    tx = tx.with_columns(
        pl.col("AREA_EN").map_elements(_norm_area_py, return_dtype=pl.Utf8).alias("area_key"),
        pl.col("PROJECT_EN").map_elements(_norm_building_py, return_dtype=pl.Utf8).alias("building_key"),
    ).filter(
        pl.col("building_key").is_not_null(),
        pl.col("building_key") != "",
        pl.col("config").is_in(["STUDIO", "1BR", "2BR"]),
    )

    pf = pf.with_columns(
        pl.col("scope_area").map_elements(_norm_area_py, return_dtype=pl.Utf8).alias("area_key"),
        pl.col("building").map_elements(_norm_building_py, return_dtype=pl.Utf8).alias("building_key"),
        normalize_pf_bedrooms(pl.col("bedrooms")).alias("config"),
    ).filter(
        pl.col("building_key").is_not_null(),
        pl.col("building_key") != "",
        pl.col("config").is_in(["STUDIO", "1BR", "2BR"]),
    )

    pf_sale = pf.filter(pl.col("scope_intent") == "sale")
    pf_rent = pf.filter(pl.col("scope_intent") == "rent")

    # DLD per-tower
    dld_tower = (
        tx.group_by(["area_key", "building_key", "config"])
        .agg(
            pl.len().alias("dld_n_tx"),
            pl.col("price_aed").median().alias("dld_actual_aed"),
            pl.col("price_per_sqft").median().alias("dld_actual_psf"),
            pl.col("sqft").median().alias("dld_median_sqft"),
            pl.col("PROJECT_EN").mode().first().alias("dld_project_en"),
            pl.col("AREA_EN").mode().first().alias("dld_area_en"),
            pl.col("NEAREST_METRO_EN").mode().first().alias("nearest_metro"),
        )
        .filter(pl.col("dld_n_tx") >= args.min_tx)
    )

    # PF per-tower (sale)
    pf_sale_tower = (
        pf_sale.group_by(["area_key", "building_key", "config"])
        .agg(
            pl.len().alias("pf_n_sale"),
            pl.col("price_aed").median().alias("pf_asking_aed"),
            pl.col("price_aed").quantile(0.25).alias("pf_p25_asking"),
            ((pl.col("price_aed") / pl.col("size_sqft")).median()).alias("pf_asking_psf"),
            pl.col("building").mode().first().alias("pf_building_name"),
            pl.col("scope_area").mode().first().alias("pf_area_name"),
        )
        .filter(pl.col("pf_n_sale") >= args.min_pf_sale)
    )

    # PF per-tower (rent)
    pf_rent_tower = (
        pf_rent.group_by(["area_key", "building_key", "config"])
        .agg(
            pl.len().alias("pf_n_rent"),
            pl.col("price_aed").median().alias("pf_rent_aed"),
            pl.col("price_aed").quantile(0.75).alias("pf_p75_rent"),
        )
        .filter(pl.col("pf_n_rent") >= args.min_pf_rent)
    )

    joined = (
        dld_tower
        .join(pf_sale_tower, on=["area_key", "building_key", "config"], how="inner")
        .join(pf_rent_tower, on=["area_key", "building_key", "config"], how="left")
        .with_columns(
            ((pl.col("pf_asking_aed") - pl.col("dld_actual_aed")) / pl.col("dld_actual_aed") * 100)
            .alias("asking_premium_pct"),
            (pl.col("pf_rent_aed") / pl.col("dld_actual_aed") * 100).alias("dld_implied_yield_pct"),
            (pl.col("pf_rent_aed") / pl.col("pf_asking_aed") * 100).alias("pf_implied_yield_pct"),
        )
        .filter(pl.col("dld_actual_aed").is_between(args.budget_min, args.budget_max))
    )

    if args.area:
        pat = "(?i)" + "|".join(args.area)
        joined = joined.filter(pl.col("area_key").str.contains(pat))

    OUT.mkdir(parents=True, exist_ok=True)
    joined.write_csv(OUT / "cross_check_tower.csv")

    print(f"\n=== TOWER-LEVEL PF asking vs DLD clearing  (n={len(joined)} matched towers) ===\n")
    cols = ["pf_area_name", "pf_building_name", "config",
            "dld_n_tx", "pf_n_sale", "pf_n_rent",
            "dld_actual_aed", "pf_asking_aed", "asking_premium_pct",
            "pf_rent_aed", "dld_implied_yield_pct", "pf_implied_yield_pct",
            "nearest_metro"]
    avail = [c for c in cols if c in joined.columns]

    # Sort by real yield desc
    by_yield = joined.sort("dld_implied_yield_pct", descending=True, nulls_last=True).select(avail).head(40)
    print("--- by real yield (top 40) ---")
    with pl.Config(tbl_rows=40, tbl_cols=14, fmt_str_lengths=30, float_precision=0,
                   tbl_width_chars=240):
        print(by_yield)

    # Most over-asked (sellers fishing)
    print("\n--- most over-asked (top 20 by asking_premium_pct) ---")
    with pl.Config(tbl_rows=20, tbl_cols=14, fmt_str_lengths=30, float_precision=0,
                   tbl_width_chars=240):
        print(joined.sort("asking_premium_pct", descending=True, nulls_last=True).select(avail).head(20))

    # Most realistic (low/negative asking premium = sellers near or below clearing)
    print("\n--- most realistic / undervalued (lowest asking_premium_pct) ---")
    with pl.Config(tbl_rows=20, tbl_cols=14, fmt_str_lengths=30, float_precision=0,
                   tbl_width_chars=240):
        print(
            joined.filter(pl.col("asking_premium_pct").is_not_null())
            .sort("asking_premium_pct").select(avail).head(20)
        )

    print(f"\nfull output: {OUT / 'cross_check_tower.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
