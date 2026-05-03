"""Inspect a downloaded DLD CSV: schema, row count, sample rows, areas, date range.

Usage:
    uv run python scripts/inspect_csv.py data/raw/transactions_2026-05-03.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl


def inspect(path: Path) -> None:
    print(f"file: {path}")
    print(f"size: {path.stat().st_size / 1e9:.2f} GB\n")

    schema = pl.scan_csv(path, infer_schema_length=10000).collect_schema()
    print("schema:")
    for col, dtype in schema.items():
        print(f"  {col:<40} {dtype}")
    print()

    head = pl.scan_csv(path, infer_schema_length=10000).head(5).collect()
    print("first 5 rows:")
    print(head)
    print()

    n = pl.scan_csv(path, infer_schema_length=10000).select(pl.len()).collect().item()
    print(f"row count: {n:,}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", type=Path)
    args = p.parse_args()
    if not args.path.exists():
        print(f"not found: {args.path}", file=sys.stderr)
        return 1
    inspect(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
