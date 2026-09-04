# ── IMPORTS ──────────────────────────────────────────────────────────────

import json         # reads raw pull and writes output files
import logging      # writes human-readable events to clearwater.log
import glob         # finds files matching a pattern (used to locate latest raw pull)
import pandas as pd # the DataFrame does the actual cleaning work below
import numpy as np  # gives us np.where() for vectorized if/else logic
from pathlib import Path
from datetime import datetime
from collections import Counter


# ── PROJECT PATHS ─────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent.parent
DATA_RAW       = BASE_DIR / "data" / "raw"
DATA_PROCESSED = BASE_DIR / "data" / "processed"
LOGS_DIR       = BASE_DIR / "logs"


# ── LOGGING SETUP ─────────────────────────────────────────────────────────
# Appends to the same clearwater.log as data_pull.py.

logging.basicConfig(
    filename=LOGS_DIR / "clearwater.log",
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

TODAY = datetime.today().strftime("%Y-%m-%d")   # e.g. "2026-07-05" — used in filenames


# ── FIELD NAME MAP ─────────────────────────────────────────────────────────
# Maps the API's field names (left) to our clean database names (right).
# PSC is handled separately below because it's nested, not a simple rename.

FIELD_MAP = {
    "Award ID":              "award_id",
    "Recipient Name":        "vendor_name",
    "Recipient UEI":         "vendor_uei",
    "Awarding Agency":       "agency",
    "Awarding Sub Agency":   "sub_agency",
    "Award Amount":          "award_amount",
    "Total Outlays":         "total_outlays",
    "Description":           "description",
    "Contract Award Type":   "contract_type",
    "Start Date":            "start_date",
    "End Date":              "end_date",
    "Base Obligation Date":  "base_obligation_date",
    "internal_id":           "internal_id",
    "generated_internal_id": "generated_internal_id",
    "awarding_agency_id":    "awarding_agency_id",
    "agency_slug":           "agency_slug",
}

# Fields where a missing value should become 0.0 (we'll do math on these later)
NUMERIC_FIELDS = {"award_amount", "total_outlays"}

# Fields where a missing value is a real problem — fill "UNKNOWN" and log it
REQUIRED_TEXT_FIELDS = {"award_id", "vendor_name", "sub_agency"}


# ── LOG ONE GROUP OF MISSING-VALUE EVENTS ───────────────────────────────────
# null_mask is df.isna() — a same-shaped table of True/False. This walks the
# flagged cells for a given set of columns and turns each one into the same
# event record clean.py has always produced, so the cleaning_log format
# doesn't change even though the logic behind it did.

def log_missing(null_mask, award_id_labels, columns, action):
    events = []
    for col in columns:
        flagged_rows = null_mask.index[null_mask[col]]
        for row in flagged_rows:
            label = award_id_labels.loc[row]
            events.append({
                "award_id": None if pd.isna(label) else label,
                "field": col,
                "issue": "null_value",
                "action": action,
                "original": None,
            })
    return events


# ── MAIN CLEANING PASS ──────────────────────────────────────────────────────
# Finds the latest raw pull file, cleans every record, saves both output files.

def clean_all_records():
    print("─" * 50)
    print("CLEARWATER — Cleaning Pass")
    print("─" * 50)

    # ── Find the input file ───────────────────────────────────────────────
    preferred = str(DATA_RAW / f"raw_pull_{TODAY}.json")
    matches   = sorted(glob.glob(str(DATA_RAW / "raw_pull_*.json")))

    if preferred in matches:
        input_file = preferred
    elif matches:
        input_file = matches[-1]
        print(f"Note: {preferred} not found. Using {input_file} instead.")
    else:
        print("Error: no raw_pull_*.json file found. Run data_pull.py first.")
        return

    print(f"Loading {input_file}...", end=" ", flush=True)
    with open(input_file, "r") as f:
        raw_records = json.load(f)
    print(f"{len(raw_records)} records loaded.")

    logging.info(f"Starting cleaning pass on {len(raw_records)} records from {input_file}.")

    # ── Flatten PSC before it becomes a table column ────────────────────────
    # PSC arrives nested — {"code": ..., "description": ...} — splitting it
    # into two plain fields now means every column below is a plain value,
    # which is what fillna() / np.where() expect to work with.
    for r in raw_records:
        psc = r.get("PSC")
        if isinstance(psc, dict):
            r["psc_code"]        = psc.get("code")
            r["psc_description"] = psc.get("description")
        else:
            r["psc_code"]        = None
            r["psc_description"] = None

    # ── Load into a DataFrame ─────────────────────────────────────────────
    # One row per contract, one column per field. Everything from here on
    # operates on the whole table at once, instead of one dict at a time —
    # this replaces the old per-record for-loop entirely.
    df = pd.DataFrame(raw_records)
    df = df.rename(columns=FIELD_MAP)

    # Derive a plain activity year from start_date — this is what the anomaly
    # flaggers group and filter by. base_obligation_date can't be used for this:
    # it reflects the ORIGINAL contract vintage, which for an old IDIQ still
    # issuing task orders today can predate our pull window by decades.
    df["activity_year"] = df["start_date"].str[:4]

    keep_cols = list(FIELD_MAP.values()) + ["psc_code", "psc_description", "activity_year"]
    df = df.reindex(columns=keep_cols)   # reindex (not plain selection) so a
                                          # field missing from every record
                                          # becomes a column of Nones instead
                                          # of crashing the run

    award_id_labels = df["award_id"]   # captured before any cleaning touches it

    # ── Find what's missing, before fixing it ─────────────────────────────
    # isna() flags every null cell across the whole table in one call — the
    # direct replacement for the old "if value is None" check repeated per
    # field, per record.
    null_before = df.isna()

    events = []
    events += log_missing(null_before, award_id_labels, NUMERIC_FIELDS, "set_to_zero")
    events += log_missing(null_before, award_id_labels, REQUIRED_TEXT_FIELDS, "set_to_UNKNOWN")

    # PSC split into two columns above, but the two are only ever missing
    # together, so it still gets one event per record, labeled "psc" — same
    # as before.
    for row in null_before.index[null_before["psc_code"]]:
        label = award_id_labels.loc[row]
        events.append({
            "award_id": None if pd.isna(label) else label,
            "field": "psc",
            "issue": "null_value",
            "action": "set_to_UNKNOWN",
            "original": None,
        })

    # ── Fill it in ──────────────────────────────────────────────────────────
    # Numeric gaps just become 0 — fillna() is the direct tool for a flat
    # constant fill.
    df[list(NUMERIC_FIELDS)] = df[list(NUMERIC_FIELDS)].fillna(0.0)

    # Required text fields get an explicit if/else instead: "if this cell is
    # missing, use UNKNOWN, otherwise keep what's there." np.where() is the
    # vectorized version of that same if/else, applied to a whole column at once.
    for col in list(REQUIRED_TEXT_FIELDS) + ["psc_code", "psc_description"]:
        df[col] = np.where(df[col].isna(), "UNKNOWN", df[col])

    # Every other optional field that's still missing stays missing — but
    # pandas marks "missing" with NaN, not Python's None, and json.dump()
    # would write that out as an invalid "NaN" token instead of "null".
    # Swap it back before this becomes a file.
    df = df.where(pd.notna(df), None)

    all_cleaned = df.to_dict(orient="records")   # back to plain dicts for JSON

    print(f"  {len(all_cleaned):,} / {len(all_cleaned):,} records cleaned.")

    # ── Save cleaned records ──────────────────────────────────────────────
    cleaned_file = DATA_PROCESSED / f"cleaned_records_{TODAY}.json"
    print(f"\nSaving {cleaned_file}...", end=" ", flush=True)
    with open(cleaned_file, "w") as f:
        json.dump(all_cleaned, f, indent=2)
    print("Done.")

    # ── Save cleaning log ───────────────────────────────────────────────────
    log_file = DATA_PROCESSED / f"cleaning_log_{TODAY}.json"
    print(f"Saving {log_file}...", end=" ", flush=True)
    with open(log_file, "w") as f:
        json.dump(events, f, indent=2)
    print("Done.")

    # ── Print summary ───────────────────────────────────────────────────────
    print("\n── Cleaning Summary ─────────────────────────────────────")
    print(f"  Total records:         {len(all_cleaned):,}")
    print(f"  Total cleaning events: {len(events):,}")

    if events:
        print("\n  Nulls by field:")
        field_counts = Counter(e["field"] for e in events)
        for field, count in field_counts.most_common():
            pct = (count / len(all_cleaned)) * 100
            print(f"    {field:<25} {count:>5,} records  ({pct:.1f}%)")
    else:
        print("  No nulls found — data was complete.")

    print("─" * 50)
    logging.info(
        f"Cleaning complete. {len(all_cleaned)} records, {len(events)} events. "
        f"Output: {cleaned_file}, {log_file}."
    )


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    clean_all_records()
