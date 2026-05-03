"""Clean DLD raw CSVs into filtered Parquet tables for fast slicing.

The DLD transactions CSV is very large (millions of rows). We:
  1. Stream-read with polars
  2. Keep only apartments (drop villas/land/commercial)
  3. Keep only sales (drop mortgages, gifts, etc.) for the transactions table
  4. Keep only residential leases for the rent_contracts table
  5. Save as Parquet, partitioned by year, in data/processed/

Run:
    uv run python analysis/clean.py

Schema names below are based on DLD's published open-data schema. If a
column is missing or renamed in your downloaded CSV, the script logs the
mismatch and you can update the COL_* mappings.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed"

# Logical column → likely DLD column names (we'll match the first one present)
TX_COLS = {
    "tx_date": ["instance_date", "transaction_date", "date"],
    "tx_type": ["trans_group_en", "transaction_group", "transaction_type"],
    "procedure": ["procedure_name_en", "procedure_en"],
    "area_name": ["area_name_en", "area_en"],
    "building_name": ["building_name_en", "tower_name_en"],
    "project_name": ["project_name_en"],
    "property_type": ["property_type_en", "property_sub_type_en"],
    "rooms": ["rooms_en", "no_of_rooms", "rooms"],
    "sqm": ["procedure_area", "area_sqm", "actual_area"],
    "price_aed": ["actual_worth", "amount", "price"],
    "price_per_sqm": ["meter_sale_price", "sale_price_per_sqm"],
}

RC_COLS = {
    "start_date": ["contract_start_date", "start_date"],
    "end_date": ["contract_end_date", "end_date"],
    "registration_date": ["registration_date", "ejari_date"],
    "area_name": ["area_name_en", "area_en"],
    "building_name": ["building_name_en", "property_name_en"],
    "project_name": ["project_name_en"],
    "property_type": ["ejari_bus_property_type_en", "property_type_en"],
    "property_subtype": ["ejari_property_sub_type_en", "property_sub_type_en"],
    "rooms": ["no_of_rooms", "rooms"],
    "annual_rent_aed": ["annual_amount", "contract_amount", "amount"],
    "sqm": ["actual_area", "property_size"],
}


def latest_csv(prefix: str) -> Path | None:
    matches = sorted(RAW.glob(f"{prefix}_*.csv"))
    return matches[-1] if matches else None


def resolve_columns(found: list[str], wanted: dict[str, list[str]]) -> dict[str, str | None]:
    found_lower = {c.lower(): c for c in found}
    out: dict[str, str | None] = {}
    for logical, candidates in wanted.items():
        out[logical] = next((found_lower[c.lower()] for c in candidates if c.lower() in found_lower), None)
    return out


def clean_transactions(path: Path) -> pl.DataFrame:
    print(f"\n=== transactions: {path.name} ===")
    schema = pl.scan_csv(path, infer_schema_length=10000).collect_schema().names()
    cols = resolve_columns(schema, TX_COLS)
    print("column mapping:")
    for logical, actual in cols.items():
        print(f"  {logical:<18} -> {actual}")
    missing = [k for k, v in cols.items() if v is None]
    if missing:
        print(f"WARNING: missing columns: {missing}", file=sys.stderr)
        print("Edit TX_COLS in analysis/clean.py to match your CSV schema.", file=sys.stderr)

    select = [pl.col(actual).alias(logical) for logical, actual in cols.items() if actual]
    lf = pl.scan_csv(path, infer_schema_length=10000, ignore_errors=True).select(select)

    # Filter: apartments only, sales only
    if "property_type" in [c.name for c in lf.collect_schema()]:
        lf = lf.filter(pl.col("property_type").str.contains("(?i)apartment|flat|unit"))
    if "tx_type" in [c.name for c in lf.collect_schema()]:
        lf = lf.filter(pl.col("tx_type").str.contains("(?i)sale|sales"))

    lf = lf.with_columns(
        pl.col("tx_date").str.to_date(strict=False, format=None).alias("tx_date"),
        (pl.col("price_aed").cast(pl.Float64) / pl.col("sqm").cast(pl.Float64) * 0.092903)
        .alias("price_per_sqft"),
    ).filter(
        pl.col("price_aed").cast(pl.Float64).is_between(50_000, 50_000_000),
        pl.col("sqm").cast(pl.Float64).is_between(15, 1500),
    )

    df = lf.collect()
    print(f"rows after clean: {len(df):,}")
    return df


def clean_rent_contracts(path: Path) -> pl.DataFrame:
    print(f"\n=== rent_contracts: {path.name} ===")
    schema = pl.scan_csv(path, infer_schema_length=10000).collect_schema().names()
    cols = resolve_columns(schema, RC_COLS)
    print("column mapping:")
    for logical, actual in cols.items():
        print(f"  {logical:<22} -> {actual}")
    missing = [k for k, v in cols.items() if v is None]
    if missing:
        print(f"WARNING: missing columns: {missing}", file=sys.stderr)
        print("Edit RC_COLS in analysis/clean.py to match your CSV schema.", file=sys.stderr)

    select = [pl.col(actual).alias(logical) for logical, actual in cols.items() if actual]
    lf = pl.scan_csv(path, infer_schema_length=10000, ignore_errors=True).select(select)

    # Filter: residential apartments only
    if "property_type" in [c.name for c in lf.collect_schema()]:
        lf = lf.filter(pl.col("property_type").str.contains("(?i)residential|apartment|flat|unit"))

    lf = lf.with_columns(
        pl.col("start_date").str.to_date(strict=False, format=None).alias("start_date"),
        pl.col("annual_rent_aed").cast(pl.Float64).alias("annual_rent_aed"),
    ).filter(
        pl.col("annual_rent_aed").is_between(10_000, 5_000_000),
    )

    df = lf.collect()
    print(f"rows after clean: {len(df):,}")
    return df


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    tx_path = latest_csv("transactions")
    if tx_path:
        df = clean_transactions(tx_path)
        out = OUT / "transactions.parquet"
        df.write_parquet(out, compression="zstd")
        print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    else:
        print("no transactions CSV found in data/raw/", file=sys.stderr)

    rc_path = latest_csv("rent_contracts")
    if rc_path:
        df = clean_rent_contracts(rc_path)
        out = OUT / "rent_contracts.parquet"
        df.write_parquet(out, compression="zstd")
        print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    else:
        print("no rent_contracts CSV found in data/raw/", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
