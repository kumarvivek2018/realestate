"""Cross-check: PF asking prices vs DLD recent transaction prices.

For each (area, config) and each (project/tower, config) we compute:
  - PF median asking sale price + asking yield
  - DLD median actual sale price (last N months)
  - asking_premium_pct = (PF_asking − DLD_actual) / DLD_actual × 100
  - DLD median price/sqft (the real per-sqft)
  - n transactions / n active asking listings

If PF asking is well above DLD actual, sellers are fishing. If close,
asking is realistic. A negative gap means asking is below recent clearing
(could be a deal).

Run:
    uv run python analysis/cross_check.py
    uv run python analysis/cross_check.py --area "Marina" --area "Damac Hills 2"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed"


def load() -> tuple[pl.DataFrame, pl.DataFrame]:
    tx_path = OUT / "transactions.parquet"
    if not tx_path.exists():
        print("missing transactions.parquet — run analysis/clean.py first", file=sys.stderr)
        sys.exit(1)
    pf_path_candidates = sorted(RAW.glob("pf_listings_*.parquet"))
    if not pf_path_candidates:
        print("missing pf_listings parquet — run scripts/scrape_pf.py first", file=sys.stderr)
        sys.exit(1)
    return pl.read_parquet(tx_path), pl.read_parquet(pf_path_candidates[-1])


def normalize_pf_bedrooms(s: pl.Expr) -> pl.Expr:
    s = s.cast(pl.Utf8).str.to_lowercase().str.strip_chars()
    return (
        pl.when(s == "studio").then(pl.lit("STUDIO"))
        .when(s.is_in(["1", "1.0"])).then(pl.lit("1BR"))
        .when(s.is_in(["2", "2.0"])).then(pl.lit("2BR"))
        .otherwise(pl.lit("OTHER"))
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--area", action="append", default=None,
                   help="restrict to area (substring, can pass multiple)")
    p.add_argument("--budget-max", type=int, default=1_500_000)
    p.add_argument("--min-tx", type=int, default=5,
                   help="DLD must have ≥ this many tx in (area, config) bucket")
    p.add_argument("--ready-only", action="store_true",
                   help="exclude off-plan from DLD and PF (apples-to-apples ready stock)")
    args = p.parse_args()

    tx, pf = load()

    # Normalize area name for join: lowercase + handle plural mismatches
    # (DLD says "Jumeirah Lakes Towers", PF says "Jumeirah Lake Towers", etc.)
    def norm_area(c: pl.Expr) -> pl.Expr:
        return (
            c.cast(pl.Utf8).str.to_lowercase()
            .str.replace_all(r"\blakes\b", "lake")
            .str.replace_all(r"\btowers\b", "towers")  # keep towers
            .str.strip_chars()
        )

    pf_sale = pf.filter(pl.col("scope_intent") == "sale").with_columns(
        normalize_pf_bedrooms(pl.col("bedrooms")).alias("config"),
        norm_area(pl.col("scope_area")).alias("area_lower"),
    ).filter(pl.col("config").is_in(["STUDIO", "1BR", "2BR"]))

    pf_rent = pf.filter(pl.col("scope_intent") == "rent").with_columns(
        normalize_pf_bedrooms(pl.col("bedrooms")).alias("config"),
        norm_area(pl.col("scope_area")).alias("area_lower"),
    ).filter(pl.col("config").is_in(["STUDIO", "1BR", "2BR"]))

    # Also re-normalize DLD area_lower with the same plural fix
    tx = tx.with_columns(norm_area(pl.col("AREA_EN")).alias("area_lower"))

    if args.ready_only:
        tx = tx.filter(~pl.col("is_offplan"))
        pf_sale = pf_sale.filter(pl.col("completion_status") != "off_plan")
        print("[ready-only mode: excluding off-plan from both PF and DLD]")

    # PF area-level
    pf_sale_area = pf_sale.group_by(["area_lower", "config"]).agg(
        pl.len().alias("pf_n_sale"),
        pl.col("price_aed").median().alias("pf_asking_aed"),
        ((pl.col("price_aed") / pl.col("size_sqft")).median()).alias("pf_asking_psf"),
    )
    pf_rent_area = pf_rent.group_by(["area_lower", "config"]).agg(
        pl.len().alias("pf_n_rent"),
        pl.col("price_aed").median().alias("pf_rent_aed"),
    )

    # DLD area-level (recent transactions)
    dld_area = tx.group_by(["area_lower", "config"]).agg(
        pl.len().alias("dld_n_tx"),
        pl.col("price_aed").median().alias("dld_actual_aed"),
        pl.col("price_per_sqft").median().alias("dld_actual_psf"),
        ((pl.col("is_offplan").cast(pl.Float64)).mean() * 100).alias("dld_share_offplan_pct"),
    ).filter(pl.col("dld_n_tx") >= args.min_tx)

    area = (
        dld_area
        .join(pf_sale_area, on=["area_lower", "config"], how="left")
        .join(pf_rent_area, on=["area_lower", "config"], how="left")
        .with_columns(
            ((pl.col("pf_asking_aed") - pl.col("dld_actual_aed")) / pl.col("dld_actual_aed") * 100)
            .alias("asking_premium_pct"),
            (pl.col("pf_rent_aed") / pl.col("dld_actual_aed") * 100)
            .alias("dld_implied_yield_pct"),
            (pl.col("pf_rent_aed") / pl.col("pf_asking_aed") * 100)
            .alias("pf_implied_yield_pct"),
        )
        .filter(pl.col("dld_actual_aed") <= args.budget_max)
        .sort("dld_implied_yield_pct", descending=True, nulls_last=True)
    )

    if args.area:
        pat = "(?i)" + "|".join(args.area)
        area = area.filter(pl.col("area_lower").str.contains(pat))

    OUT.mkdir(parents=True, exist_ok=True)
    area.write_csv(OUT / "cross_check_area.csv")

    print(f"\n=== AREA × CONFIG: PF asking vs DLD clearing (last 4 months) ===\n")
    print("Columns: dld_actual_aed = median DLD sale price (real)")
    print("         pf_asking_aed   = median PF asking sale price")
    print("         asking_premium  = how much PF asking is above DLD actual %")
    print("         dld_implied_yield = PF rent / DLD actual sale price (real-yield estimate)")
    print()

    cols = ["area_lower", "config", "dld_n_tx", "dld_actual_aed", "dld_actual_psf",
            "dld_share_offplan_pct", "pf_n_sale", "pf_asking_aed", "asking_premium_pct",
            "pf_n_rent", "pf_rent_aed", "dld_implied_yield_pct", "pf_implied_yield_pct"]
    avail = [c for c in cols if c in area.columns]
    with pl.Config(tbl_rows=60, tbl_cols=14, fmt_str_lengths=30, float_precision=0,
                   tbl_width_chars=240):
        print(area.select(avail))

    print(f"\nfull output: {OUT / 'cross_check_area.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
