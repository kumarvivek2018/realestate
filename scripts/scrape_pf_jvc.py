"""One-shot append of JVC scopes to the existing PF JSONL.

Run after the main scrape if a scope was missed:
    uv run python scripts/scrape_pf_jvc.py
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
    target = [k for k in SCOPES if k.startswith("jvc_")]
    print(f"running JVC scopes: {target}", flush=True)

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
