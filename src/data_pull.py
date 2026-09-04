# ── IMPORTS ──────────────────────────────────────────────────────────────
# These are toolboxes Python pulls in before the script runs.

import requests   # handles the HTTP conversation with USASpending.gov
import sqlite3    # lets us create and write to a local .db file
import json       # parses the API's raw text response into Python objects
import time       # we'll use this to pause between API pages (be polite)
import logging    # Python's built-in way to write structured log messages
from datetime import datetime   # used to stamp output filenames with today's date
from pathlib import Path # used to name and manipulate file paths 

# ── PROJECT PATHS ─────────────────────────────────────────────────────────
# Anchor everything to the project root (one level up from src/), so it
# doesn't matter what folder you're sitting in when you run this script.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_RAW = BASE_DIR / "data" / "raw"
DATA_DB  = BASE_DIR / "data" / "db"
LOGS_DIR = BASE_DIR / "logs"

# ── LOGGING SETUP ─────────────────────────────────────────────────────────
# basicConfig creates clearwater.log in the same folder as this script
# the first time the script runs. On every run after that, it reopens
# that file and appends to the bottom ("a" = append, not overwrite).
#
# Every line written to the log will look like this:
#   2026-06-22 14:03:11 | INFO    | Page 3 fetched — 100 records
#   2026-06-22 14:03:13 | WARNING | Page 3 failed: 422 Client Error
#
# The three parts are: timestamp | severity level | your message.
# INFO = normal event. WARNING = something's off but we kept going.

logging.basicConfig(
    filename=LOGS_DIR / "clearwater.log",         # where the log goes
    filemode="a",                      # "a" = append, not overwrite
    level=logging.INFO,                # log INFO and above (INFO, WARNING, ERROR)
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ── CONSTANTS ─────────────────────────────────────────────────────────────
# Fixed values. ALL_CAPS is the Python convention for "this doesn't change."
# Put them here so you never have to hunt through the script to update them.

API_URL   = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
DB_PATH   = DATA_DB / "clearwater.db"   # the database file we'll create or write to
PAGE_SIZE = 100               # how many records the API returns per page
PAUSE_SEC = 3                 # seconds to wait between requests — 3 is polite, unhurried
MAX_PAGES = 100               # hard ceiling: stop after 100 pages (10,000 records)
                              # raise this later once you confirm the data looks right

TODAY = datetime.today().strftime("%Y-%m-%d")   # e.g. "2026-07-05" — used in filenames


# ── BLOCK 2: API REQUEST (CURSOR VERSION) ────────────────────────────────
# Sends one POST request and returns one page of results.
#
# The key difference from before: instead of a page number, this function
# takes a "bookmark" — two values from the previous response that tell the
# API exactly where to pick up. First call passes None (no bookmark yet).
#
# bookmark is either None (first call) or a dict:
#   {"sort_value": "...", "unique_id": 12345}

def fetch_page(bookmark):

    # These are the fields the API will return for each contract.
    # Field names must exactly match the API's internal list — wrong names = 422 error.
    fields = [
        "Award ID",              # contract identifier
        "Recipient Name",        # vendor / company name
        "Recipient UEI",         # unique vendor ID (replaced DUNS in 2022)
        "Awarding Agency",       # top-level DoD agency
        "Awarding Sub Agency",   # sub-agency (e.g., Army, Navy, DARPA)
        "Award Amount",          # total obligated dollars
        "Total Outlays",         # dollars actually paid out so far
        "Description",           # plain-text description of what was bought
        "Contract Award Type",   # e.g., "Definitive Contract", "Purchase Order"
        "Start Date",            # period of performance start
        "End Date",              # period of performance end
        "Base Obligation Date",  # date the contract was originally signed
        "PSC",                   # Product Service Code — returns {code, description}
    ]

    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],   # contracts only, no grants
            "agencies": [
                {
                    "type": "awarding",
                    "tier": "toptier",
                    "name": "Department of Defense"      # DoD only
                }
            ],
            "psc_codes": ["D"],                          # D = IT & tech services
            "time_period": [
                {
                    "start_date": "2021-10-01",          # start of FY2022
                    "end_date":   "2024-09-30"           # end of FY2024
                }
            ]
        },
        "fields": fields,
        "limit": PAGE_SIZE,
        "sort":  "Award Amount",
        "order": "desc"          # largest contracts first
    }

    # If we have a bookmark from a previous page, attach it to the payload.
    # This tells the API: "start after the record I last saw."
    # Without this, every request would return the same first 100 records.
    if bookmark is not None:
        payload["last_record_sort_value"] = bookmark["sort_value"]
        payload["last_record_unique_id"]  = bookmark["unique_id"]

    try:
        response = requests.post(
            API_URL,
            json=payload,
            timeout=30                  # give up after 30 seconds of no response
        )
        response.raise_for_status()     # if server returned an error code, raise it now

        logging.info("Page fetched successfully.")
        return response.json()

    except requests.exceptions.RequestException as e:
        logging.warning(f"Request failed: {e}")
        return None


# ── BLOCK 3: CURSOR LOOP & RAW SAVE ──────────────────────────────────────
# Calls fetch_page() repeatedly, passing the bookmark forward each time.
# Stops when the API says hasNext: false, or when we hit MAX_PAGES.
# Saves everything to a date-stamped raw_pull file when done.

def pull_all_records():

    all_records = []   # the growing pile of records across all pages
    bookmark    = None # start with no bookmark — first request has none
    page_count  = 0    # how many pages we've pulled so far

    logging.info("Starting full data pull.")

    while True:
        time.sleep(PAUSE_SEC)           # pause before every request, including the first
        response = fetch_page(bookmark)

        if response is None:
            # Request failed — log it and stop rather than saving incomplete data
            logging.warning(f"Pull stopped at page {page_count + 1} — request returned nothing.")
            break

        results = response.get("results", [])
        all_records.extend(results)
        page_count += 1

        # Read the bookmark out of the response for the next request.
        # The API puts it in page_metadata — we save both pieces.
        meta          = response.get("page_metadata", {})
        has_next      = meta.get("hasNext", False)
        next_sort_val = meta.get("last_record_sort_value")
        next_unique   = meta.get("last_record_unique_id")

        logging.info(
            f"Page {page_count} done — {len(results)} records this page, "
            f"{len(all_records)} total so far. hasNext: {has_next}"
        )

        # Three reasons to stop:
        if not has_next:
            logging.info("API says no more pages. Pull complete.")
            break

        if page_count >= MAX_PAGES:
            # We hit our self-imposed ceiling — safe stopping point
            logging.info(f"Reached MAX_PAGES ({MAX_PAGES}). Stopping early.")
            print(f"Note: stopped at {MAX_PAGES} pages. Raise MAX_PAGES to pull more.")
            break

        if next_sort_val is None or next_unique is None:
            # Bookmark is missing — can't continue safely
            logging.warning("No bookmark in response. Cannot continue pagination.")
            break

        # Update the bookmark for the next loop iteration
        bookmark = {"sort_value": next_sort_val, "unique_id": next_unique}

    # ── SAVE ─────────────────────────────────────────────────────────────
    # Filename includes today's date so pulls don't overwrite each other.
    # Example: raw_pull_2026-07-05.json
    filename = DATA_RAW / f"raw_pull_{TODAY}.json"
    with open(filename, "w") as f:
        json.dump(all_records, f, indent=2)

    logging.info(f"Saved {len(all_records)} records to {filename}.")
    print(f"Done. {len(all_records)} records saved to {filename}.")


# ── BLOCK 4: ENTRY POINT ─────────────────────────────────────────────────
# The ignition switch. Running "python data_pull.py" hits this line first,
# which calls pull_all_records() and sets everything in motion.

if __name__ == "__main__":
    pull_all_records()
