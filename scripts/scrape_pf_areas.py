"""Scrape selected PF areas by short name and append to today's JSONL.

Use this when SCOPES grows after the main scrape — instead of re-running
all 60+ scopes, just hit the new ones.

Run:
    uv run python scripts/scrape_pf_areas.py motor_city majan meydan_one
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import httpx
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from scrape_pf import SCOPES, scrape_scope  # noqa: E402

OUT = ROOT / "data" / "raw"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: scrape_pf_areas.py <area_short> [<area_short> ...]", file=sys.stderr)
        return 2

    wanted_areas = set(sys.argv[1:])
    target = [
        k for k in SCOPES
        if any(k == f"{a}_sale_studio" or k == f"{a}_rent_studio"
               or k == f"{a}_sale_1br" or k == f"{a}_rent_1br"
               for a in wanted_areas)
    ]
    if not target:
        print(f"no scopes match {wanted_areas}", file=sys.stderr)
        return 1

    print(f"scraping {len(target)} scopes:", flush=True)
    for s in target:
        print(f"  {s}", flush=True)

    all_rows = []
    with httpx.Client() as client:
        for name in target:
            try:
                rows = scrape_scope(client, name, SCOPES[name], None)
                all_rows.extend(rows)
                print(f"[{name}] +{len(rows)}  total {len(all_rows):,}", flush=True)
            except Exception as e:
                print(f"[{name}] FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    if not all_rows:
        print("no rows scraped", file=sys.stderr)
        return 1

    today = date.today().isoformat()
    jsonl = OUT / f"pf_listings_{today}.jsonl"
    parquet = OUT / f"pf_listings_{today}.parquet"

    with jsonl.open("a") as fh:
        for r in all_rows:
            fh.write(json.dumps(r, default=str) + "\n")
    print(f"appended {len(all_rows)} rows to {jsonl}", flush=True)

    df = pl.read_ndjson(jsonl, infer_schema_length=None)
    df.write_parquet(parquet, compression="zstd")
    print(f"rebuilt {parquet} ({len(df):,} rows total)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
