"""Clean DLD transactions CSV → filtered Parquet.

Real DLD schema (downloaded via dubailand.gov.ae form):
  TRANSACTION_NUMBER, INSTANCE_DATE, GROUP_EN, PROCEDURE_EN,
  IS_OFFPLAN_EN, IS_FREE_HOLD_EN, USAGE_EN, AREA_EN,
  PROP_TYPE_EN, PROP_SB_TYPE_EN, TRANS_VALUE, PROCEDURE_AREA,
  ACTUAL_AREA, ROOMS_EN, PARKING, NEAREST_METRO_EN,
  NEAREST_MALL_EN, NEAREST_LANDMARK_EN, TOTAL_BUYER, TOTAL_SELLER,
  MASTER_PROJECT_EN, PROJECT_EN

We:
  - Keep only Sales (drop Mortgages, Gifts)
  - Keep only Residential apartments (drop villas, land, commercial)
  - Compute price_per_sqft from TRANS_VALUE / ACTUAL_AREA (DLD area is sqm)
  - Title-case AREA_EN so it joins case-insensitively with PF data
  - Map ROOMS_EN ("Studio", "1 B/R", "2 B/R", ...) to canonical
    STUDIO / 1BR / 2BR / 3BR+ for downstream joins
  - Save sales as data/processed/transactions.parquet

Run:
    uv run python analysis/clean.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed"

SQM_TO_SQFT = 10.7639


def latest(prefix: str) -> Path | None:
    matches = sorted(RAW.glob(f"{prefix}_*.csv"))
    return matches[-1] if matches else None


def normalize_rooms(s: pl.Expr) -> pl.Expr:
    s = s.cast(pl.Utf8).str.to_uppercase().str.strip_chars()
    return (
        pl.when(s.str.contains("STUDIO")).then(pl.lit("STUDIO"))
        .when(s.str.contains(r"^1\s*B")).then(pl.lit("1BR"))
        .when(s.str.contains(r"^2\s*B")).then(pl.lit("2BR"))
        .when(s.str.contains(r"^[3-9]\s*B")).then(pl.lit("3BR+"))
        .otherwise(pl.lit("OTHER"))
    )


def title_case_area(s: pl.Expr) -> pl.Expr:
    """DLD has mixed case ('BUSINESS BAY' vs 'Al Garhoud'). Normalize to title case
    for case-insensitive join with PF data."""
    return s.cast(pl.Utf8).str.to_lowercase().str.replace_all(
        r"\b(\w)", "${1}"
    ).pipe(lambda c: c.str.to_titlecase() if hasattr(c.str, "to_titlecase") else c)


def clean_transactions(path: Path) -> pl.DataFrame:
    print(f"loading {path.name}")
    lf = pl.scan_csv(path, infer_schema_length=10000, ignore_errors=True)

    # Filter: Sales only, Residential, Flats only (apartments)
    cleaned = (
        lf
        .filter(pl.col("GROUP_EN") == "Sales")
        .filter(pl.col("USAGE_EN") == "Residential")
        .filter(pl.col("PROP_SB_TYPE_EN").str.contains("(?i)flat|apartment|unit"))
        .with_columns(
            pl.col("INSTANCE_DATE").str.to_datetime(strict=False).alias("tx_datetime"),
            pl.col("INSTANCE_DATE").str.to_datetime(strict=False).dt.date().alias("tx_date"),
            normalize_rooms(pl.col("ROOMS_EN")).alias("config"),
            pl.col("AREA_EN").str.to_lowercase().alias("area_lower"),
            pl.col("TRANS_VALUE").cast(pl.Float64).alias("price_aed"),
            pl.col("ACTUAL_AREA").cast(pl.Float64).alias("sqm"),
            (pl.col("ACTUAL_AREA").cast(pl.Float64) * SQM_TO_SQFT).alias("sqft"),
        )
        .with_columns(
            (pl.col("price_aed") / pl.col("sqft")).alias("price_per_sqft"),
            (pl.col("IS_OFFPLAN_EN") == "Off-Plan").alias("is_offplan"),
            (pl.col("IS_FREE_HOLD_EN") == "Free Hold").alias("is_freehold"),
        )
        .filter(
            pl.col("price_aed").is_between(50_000, 50_000_000),
            pl.col("sqft").is_between(150, 15_000),
        )
        .select(
            "tx_date", "tx_datetime",
            "AREA_EN", "area_lower",
            "MASTER_PROJECT_EN", "PROJECT_EN",
            "config", "ROOMS_EN",
            "price_aed", "sqft", "sqm", "price_per_sqft",
            "is_offplan", "is_freehold",
            "NEAREST_METRO_EN", "NEAREST_MALL_EN", "NEAREST_LANDMARK_EN",
            "TRANSACTION_NUMBER",
        )
    )

    df = cleaned.collect()
    print(f"  → {len(df):,} sales (residential apartments) after filter")
    print(f"  date range: {df['tx_date'].min()} to {df['tx_date'].max()}")
    print(f"  config breakdown:")
    bd = df.group_by("config").agg(pl.len().alias("n"), pl.col("price_aed").median().alias("median_price")).sort("n", descending=True)
    print(bd)
    return df


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tx_path = latest("transactions")
    if not tx_path:
        print("no transactions_*.csv in data/raw/", file=sys.stderr)
        return 1
    df = clean_transactions(tx_path)
    out = OUT / "transactions.parquet"
    df.write_parquet(out, compression="zstd")
    print(f"\nwrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(df):,} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
