# ── IMPORTS ──────────────────────────────────────────────────────────────
# All built-in — no pip install needed for this script.

import sqlite3      # creates and writes to the local .db file
import json         # reads the cleaned_records JSON file
import glob         # finds the latest cleaned_records_*.json automatically
import logging      # appends events to clearwater.log
from pathlib import Path        # used to name and manipulate file paths
from datetime import datetime   # used to find today's cleaned file


# ── PROJECT PATHS ─────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent.parent
DATA_PROCESSED = BASE_DIR / "data" / "processed"
DATA_DB        = BASE_DIR / "data" / "db"
LOGS_DIR       = BASE_DIR / "logs"


# ── LOGGING SETUP ─────────────────────────────────────────────────────────
# Same log file as data_pull.py and clean.py — all three scripts write
# to clearwater.log so you have one complete diary of every run.

logging.basicConfig(
    filename=LOGS_DIR / "clearwater.log",
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ── CONSTANTS ─────────────────────────────────────────────────────────────

DB_PATH = DATA_DB / "clearwater.db"   # the database file — created here if it doesn't exist
TODAY   = datetime.today().strftime("%Y-%m-%d")   # used to find today's cleaned file


# ── BLOCK 2: CREATE TABLE ─────────────────────────────────────────────────
# Opens (or creates) clearwater.db and creates the contracts table.
# Safe to run multiple times — IF NOT EXISTS means it won't overwrite.
#
# The connection is the open file. The cursor is the pen that writes to it.
# Everything goes through the cursor; the connection just holds it open.

def create_table(conn):
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            generated_internal_id   TEXT PRIMARY KEY,  -- globally unique per award (fixed 2026-09-03:
                                                         -- award_id collides for child task/delivery
                                                         -- orders under a parent IDIQ — see project notes)
            award_id                TEXT,              -- API's own ID; NOT unique on its own
            vendor_name             TEXT,
            vendor_uei              TEXT,              -- unique vendor identifier (replaced DUNS)
            agency                  TEXT,
            sub_agency              TEXT,
            award_amount            REAL,              -- total obligated dollars
            total_outlays           REAL,              -- dollars actually paid out
            description             TEXT,
            contract_type           TEXT,
            start_date              TEXT,              -- stored as text; SQLite has no date type
            end_date                TEXT,
            base_obligation_date    TEXT,
            internal_id             INTEGER,
            awarding_agency_id      INTEGER,
            agency_slug             TEXT,
            psc_code                TEXT,
            psc_description         TEXT,
            activity_year           TEXT               -- from start_date; group/filter anomaly
                                                         -- signals by this, not base_obligation_date
        )
    """)

    conn.commit()   # save the table structure to disk
    logging.info("contracts table ready.")


# ── BLOCK 3: INSERT RECORDS ───────────────────────────────────────────────
# Reads the latest cleaned_records file, inserts every record into the DB.
# INSERT OR REPLACE means re-running this is safe — no duplicates, no errors.

def load_records(conn):
    cursor = conn.cursor()

    # ── Find the input file ───────────────────────────────────────────────
    # Prefer today's file, fall back to most recent available.
    preferred = str(DATA_PROCESSED / f"cleaned_records_{TODAY}.json")
    matches   = sorted(glob.glob(str(DATA_PROCESSED / "cleaned_records_*.json")))

    if preferred in matches:
        input_file = preferred
    elif matches:
        input_file = matches[-1]
        print(f"Note: {preferred} not found. Using {input_file} instead.")
    else:
        print("Error: no cleaned_records_*.json file found. Run clean.py first.")
        return 0   # return 0 so the caller knows nothing happened

    print(f"Loading {input_file}...", end=" ", flush=True)
    with open(input_file, "r") as f:
        records = json.load(f)
    print(f"{len(records)} records found.")

    # ── Build rows for insertion ──────────────────────────────────────────
    # Each record becomes a tuple of values in the exact column order the
    # table expects. A tuple is a sealed, ordered group — one row per contract.
    # The ? placeholders keep special characters in the data from breaking the query.
    rows = [
        (
            r.get("generated_internal_id"),
            r.get("award_id"),
            r.get("vendor_name"),
            r.get("vendor_uei"),
            r.get("agency"),
            r.get("sub_agency"),
            r.get("award_amount"),
            r.get("total_outlays"),
            r.get("description"),
            r.get("contract_type"),
            r.get("start_date"),
            r.get("end_date"),
            r.get("base_obligation_date"),
            r.get("internal_id"),
            r.get("awarding_agency_id"),
            r.get("agency_slug"),
            r.get("psc_code"),
            r.get("psc_description"),
            r.get("activity_year"),
        )
        for r in records   # one tuple per record
    ]

    # executemany sends all rows in one efficient operation
    # rather than calling execute() 10,000 times in a loop
    cursor.executemany("""
        INSERT OR REPLACE INTO contracts (
            generated_internal_id, award_id, vendor_name, vendor_uei,
            agency, sub_agency,
            award_amount, total_outlays,
            description, contract_type,
            start_date, end_date, base_obligation_date,
            internal_id,
            awarding_agency_id, agency_slug,
            psc_code, psc_description,
            activity_year
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)

    conn.commit()   # write everything to disk

    inserted = len(rows)
    logging.info(f"Inserted {inserted} records into contracts table from {input_file}.")
    return inserted


# ── BLOCK 4: MAIN FUNCTION & ENTRY POINT ─────────────────────────────────
# Opens the database, runs create_table and load_records, then closes cleanly.
# try/finally guarantees the connection is closed even if something errors.

def main():
    print("─" * 50)
    print("CLEARWATER — Database Load")
    print("─" * 50)

    conn = sqlite3.connect(DB_PATH)   # opens clearwater.db (creates it if new)
    logging.info(f"Opened database: {DB_PATH}")

    try:
        print("Creating contracts table if needed...", end=" ", flush=True)
        create_table(conn)
        print("Ready.")

        print("Inserting records...")
        count = load_records(conn)

        print("─" * 50)
        print(f"Done. {count:,} records loaded into {DB_PATH}.")
        print("─" * 50)

    finally:
        conn.close()   # always runs — returns the file to the OS cleanly
        logging.info("Database connection closed.")


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
