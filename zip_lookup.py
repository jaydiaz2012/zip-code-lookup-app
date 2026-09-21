#!/usr/bin/env python3
"""
zip_lookup.py — Batch-look-up ZIP codes for every school in a spreadsheet,
using the shared geocoding logic in zip_lookup_core.py.

Usage:
    python zip_lookup.py --input schools.xlsx --output schools_with_zipcodes.xlsx
    python zip_lookup.py --input schools.csv  --output schools_with_zipcodes.csv

Requirements:
    pip install pandas openpyxl geopy tqdm

See zip_lookup_core.py for notes on Nominatim's usage policy and the
USER_AGENT setting you should update before running this for real.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from zip_lookup_core import (
    NOT_FOUND_MARKER,
    build_geocoder,
    cell_needs_lookup,
    load_cache,
    lookup_zip,
    save_cache,
)

try:
    from tqdm import tqdm
except ImportError:  # optional dependency — script still works without it
    tqdm = None

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

DEFAULT_INPUT = "schools.xlsx"
DEFAULT_OUTPUT = "schools_with_zipcodes.xlsx"
CACHE_FILE = "geocode_cache.json"
LOG_FILE = "zip_lookup.log"

SAVE_EVERY = 25  # checkpoint the output + cache every N processed rows

REQUIRED_COLUMNS = {"School_Name", "Country", "Sales_Territory"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("zip_lookup")


# ----------------------------------------------------------------------
# FILE I/O (xlsx or csv, based on extension)
# ----------------------------------------------------------------------

def read_table(path: str) -> pd.DataFrame:
    """
    Read the input, forcing the 'Zip Code' column to string type.

    Without this, pandas infers a mixed blank/numeric 'Zip Code' column as
    float64 — which silently strips leading zeros from zips like "02138"
    (-> 2138.0) and later crashes when a geocoded string is written into
    that float column. dtype=str on a column that doesn't exist yet is a
    no-op, so this is safe whether or not 'Zip Code' is already present.
    """
    suffix = Path(path).suffix.lower()
    dtype = {"Zip Code": str}
    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, engine="openpyxl", dtype=dtype)
    elif suffix == ".csv":
        df = pd.read_csv(path, dtype=dtype)
    else:
        raise ValueError(f"Unsupported input file type: {suffix!r} (use .xlsx or .csv)")

    if "Zip Code" in df.columns:
        df["Zip Code"] = df["Zip Code"].apply(lambda v: "" if pd.isna(v) else str(v).strip())
    return df


def write_table(df: pd.DataFrame, path: str) -> None:
    suffix = Path(path).suffix.lower()
    if suffix in (".xlsx", ".xls"):
        df.to_excel(path, index=False)
    elif suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"Unsupported output file type: {suffix!r} (use .xlsx or .csv)")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Look up ZIP codes for schools by name/territory.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Input .xlsx or .csv file")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output .xlsx or .csv file")
    parser.add_argument("--cache", default=CACHE_FILE, help="Geocode cache JSON path")
    parser.add_argument("--save-every", type=int, default=SAVE_EVERY, help="Rows between checkpoint saves")
    args = parser.parse_args()

    log.info("Loading %s ...", args.input)
    df = read_table(args.input)

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        log.error("Input file is missing required column(s): %s", ", ".join(sorted(missing)))
        sys.exit(1)

    if "Zip Code" not in df.columns:
        df["Zip Code"] = ""

    cache = load_cache(args.cache)
    geocode, reverse = build_geocoder()

    todo_mask = df["Zip Code"].apply(cell_needs_lookup)
    todo_idx = df.index[todo_mask]
    log.info("%d of %d rows need a ZIP code lookup.", len(todo_idx), len(df))

    iterator = tqdm(todo_idx, desc="Geocoding") if tqdm else todo_idx
    processed_since_save = 0

    try:
        for idx in iterator:
            row = df.loc[idx]
            zipcode = lookup_zip(
                row["School_Name"], row["Country"], row["Sales_Territory"],
                geocode, reverse, cache,
            )
            df.at[idx, "Zip Code"] = zipcode if zipcode else NOT_FOUND_MARKER

            if not tqdm:
                log.info("%s -> %s", row["School_Name"], zipcode or NOT_FOUND_MARKER)

            processed_since_save += 1
            if processed_since_save >= args.save_every:
                write_table(df, args.output)
                save_cache(cache, args.cache)
                processed_since_save = 0
    except KeyboardInterrupt:
        log.warning("Interrupted by user — saving progress before exiting.")
    finally:
        write_table(df, args.output)
        save_cache(cache, args.cache)

    found = df["Zip Code"].apply(lambda v: not cell_needs_lookup(v))
    log.info("Done. %d/%d rows have a ZIP code. Saved to %s", found.sum(), len(df), args.output)


if __name__ == "__main__":
    main()
