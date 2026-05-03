"""Per-area + per-tower price/rent/yield summary for studios + 1BHK.

Reads cleaned Parquet tables and produces:
  1. Area-level table: median price/sqft, median rent, gross yield, transaction
     volume, 12-month and 5-year price momentum.
  2. Tower-level table: same metrics per building, ranked.

Output is written to data/processed/ as both Parquet and a printable CSV.
The boom-distortion adjustment: we report price as of 2019 baseline, current,
and 12-month change separately — never as a single 5Y CAGR — so you can see
how much of any "growth" is just boom inflation.

Run:
    uv run python analysis/area_summary.py
    uv run python analysis/area_summary.py --area "Marina" --area "Jumeirah Lake Towers"
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"

DEFAULT_AREAS = [
    "Marina",
    "Jumeirah Lake Towers",
    "Business Bay",
    "Jumeirah Village Circle",
    "Al Furjan",
    "Dubai Hills",
    "Downtown",
    "Discovery Gardens",
    "Dubai Silicon Oasis",
    "Dubai South",
]


def normalize_rooms(s: pl.Expr) -> pl.Expr:
    """Map various room labels to canonical: STUDIO / 1BR / 2BR / 3BR+."""
    s = s.cast(pl.Utf8).str.to_uppercase().str.strip_chars()
    return (
        pl.when(s.str.contains("STUDIO"))
        .then(pl.lit("STUDIO"))
        .when(s.str.contains(r"^1\s*B|ONE\s*B|^1$"))
        .then(pl.lit("1BR"))
        .when(s.str.contains(r"^2\s*B|TWO\s*B|^2$"))
        .then(pl.lit("2BR"))
        .when(s.str.contains(r"^[3-9]"))
        .then(pl.lit("3BR+"))
        .otherwise(pl.lit("OTHER"))
    )


def filter_areas(df: pl.DataFrame, areas: list[str]) -> pl.DataFrame:
    pat = "(?i)" + "|".join(areas)
    return df.filter(pl.col("area_name").cast(pl.Utf8).str.contains(pat))


def area_summary(tx: pl.DataFrame, rc: pl.DataFrame) -> pl.DataFrame:
    today = date.today()
    one_year_ago = date(today.year - 1, today.month, 1)
    five_year_ago = date(today.year - 5, today.month, 1)
    pre_boom = date(2020, 1, 1)

    tx = tx.with_columns(normalize_rooms(pl.col("rooms")).alias("config"))
    rc = rc.with_columns(normalize_rooms(pl.col("rooms")).alias("config"))

    # Median price/sqft per (area, config) at three time slices
    def median_price(window_start: date, window_end: date | None = None) -> pl.DataFrame:
        f = tx.filter(pl.col("tx_date") >= window_start)
        if window_end:
            f = f.filter(pl.col("tx_date") < window_end)
        return f.group_by(["area_name", "config"]).agg(
            pl.col("price_per_sqft").median().alias("p_sqft"),
            pl.col("price_aed").median().alias("p_total"),
            pl.len().alias("n_tx"),
        )

    current = median_price(one_year_ago).rename({
        "p_sqft": "p_sqft_now", "p_total": "p_total_now", "n_tx": "n_tx_12m"
    })
    five_y = median_price(five_year_ago, one_year_ago).rename({
        "p_sqft": "p_sqft_5y", "p_total": "p_total_5y", "n_tx": "n_tx_5y"
    })
    pre = median_price(pre_boom, date(2021, 1, 1)).rename({
        "p_sqft": "p_sqft_2020", "p_total": "p_total_2020", "n_tx": "n_tx_2020"
    })

    summary = current.join(pre, on=["area_name", "config"], how="left").join(
        five_y, on=["area_name", "config"], how="left"
    )

    # Median rent per (area, config) — last 12 months
    rent = (
        rc.filter(pl.col("start_date") >= one_year_ago)
        .group_by(["area_name", "config"])
        .agg(
            pl.col("annual_rent_aed").median().alias("rent_now"),
            pl.len().alias("n_contracts_12m"),
        )
    )

    summary = summary.join(rent, on=["area_name", "config"], how="left")

    # Gross yield = annual rent / median total price
    summary = summary.with_columns(
        (pl.col("rent_now") / pl.col("p_total_now") * 100).alias("gross_yield_pct"),
        ((pl.col("p_sqft_now") / pl.col("p_sqft_2020") - 1) * 100).alias("appreciation_since_2020_pct"),
        ((pl.col("p_sqft_now") / pl.col("p_sqft_5y") - 1) * 100).alias("change_vs_prior_4y_pct"),
    ).sort(["area_name", "config"])

    return summary


def tower_summary(tx: pl.DataFrame, rc: pl.DataFrame, top_n: int = 50) -> pl.DataFrame:
    today = date.today()
    one_year_ago = date(today.year - 1, today.month, 1)

    tx = tx.with_columns(normalize_rooms(pl.col("rooms")).alias("config")).filter(
        pl.col("tx_date") >= one_year_ago, pl.col("config").is_in(["STUDIO", "1BR"])
    )
    rc = rc.with_columns(normalize_rooms(pl.col("rooms")).alias("config")).filter(
        pl.col("start_date") >= one_year_ago, pl.col("config").is_in(["STUDIO", "1BR"])
    )

    tx_per_tower = tx.group_by(["area_name", "building_name", "config"]).agg(
        pl.col("price_per_sqft").median().alias("p_sqft"),
        pl.col("price_aed").median().alias("p_total"),
        pl.len().alias("n_tx_12m"),
    ).filter(pl.col("n_tx_12m") >= 3)  # need volume to be meaningful

    rc_per_tower = rc.group_by(["area_name", "building_name", "config"]).agg(
        pl.col("annual_rent_aed").median().alias("rent_now"),
        pl.len().alias("n_contracts_12m"),
    ).filter(pl.col("n_contracts_12m") >= 3)

    return (
        tx_per_tower.join(
            rc_per_tower, on=["area_name", "building_name", "config"], how="inner"
        )
        .with_columns((pl.col("rent_now") / pl.col("p_total") * 100).alias("gross_yield_pct"))
        .sort("gross_yield_pct", descending=True)
        .head(top_n)
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--area", action="append", default=None,
                   help="restrict to area (substring match, can pass multiple)")
    p.add_argument("--budget-max", type=int, default=1_000_000,
                   help="filter tower table to median total price <= this (AED)")
    args = p.parse_args()

    tx = pl.read_parquet(PROCESSED / "transactions.parquet")
    rc = pl.read_parquet(PROCESSED / "rent_contracts.parquet")

    areas = args.area or DEFAULT_AREAS
    tx_a = filter_areas(tx, areas)
    rc_a = filter_areas(rc, areas)

    print(f"transactions in scope: {len(tx_a):,}  (of {len(tx):,})")
    print(f"rent contracts in scope: {len(rc_a):,}  (of {len(rc):,})")

    summary = area_summary(tx_a, rc_a)
    print("\n=== AREA × CONFIG SUMMARY ===")
    with pl.Config(tbl_rows=200, tbl_cols=20, fmt_str_lengths=40):
        print(summary)
    summary.write_csv(PROCESSED / "area_summary.csv")
    summary.write_parquet(PROCESSED / "area_summary.parquet")

    towers = tower_summary(tx_a, rc_a).filter(pl.col("p_total") <= args.budget_max)
    print(f"\n=== TOP TOWERS (studio/1BR, median price <= {args.budget_max:,}) ===")
    with pl.Config(tbl_rows=100, tbl_cols=20, fmt_str_lengths=40):
        print(towers)
    towers.write_csv(PROCESSED / "tower_summary.csv")
    towers.write_parquet(PROCESSED / "tower_summary.parquet")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
