# ── IMPORTS ──────────────────────────────────────────────────────────────

import requests   # handles the HTTP conversation with USASpending.gov
import json
import time
import logging
from pathlib import Path
from datetime import datetime


# ── PROJECT PATHS ─────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent.parent
DATA_RAW       = BASE_DIR / "data" / "raw"
DATA_PROCESSED = BASE_DIR / "data" / "processed"
LOGS_DIR       = BASE_DIR / "logs"


# ── LOGGING SETUP ─────────────────────────────────────────────────────────
# Appends to the same clearwater.log as the rest of the pipeline.

logging.basicConfig(
    filename=LOGS_DIR / "clearwater.log",
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

TODAY = datetime.today().strftime("%Y-%m-%d")


# ── CONSTANTS ─────────────────────────────────────────────────────────────
# Unlike data_pull.py's bulk search endpoint, this one returns full detail
# for ONE award per call — which is exactly why this script only targets the
# 190 flagged-market awards instead of all 10,000.
AWARD_DETAIL_URL = "https://api.usaspending.gov/api/v2/awards/{award_id}/"
PAUSE_SEC = 3   # same polite pacing as data_pull.py


# ── FIND THE MOST RECENT FLAGGED-MARKETS FILE ───────────────────────────────
def find_flagged_file():
    matches = sorted(DATA_PROCESSED.glob("flagged_vendor_concentration_*.json"))
    if not matches:
        print("Error: no flagged_vendor_concentration_*.json found. Run flag_vendor_concentration.py first.")
        return None
    return matches[-1]


# ── PULL THE AWARD LIST OUT OF THE FLAGGED MARKETS ──────────────────────────
# Every flagged market already carries its own award_ids list (from
# flag_vendor_concentration.py) — this just flattens and de-duplicates
# across all of them into one list to fetch.
def collect_award_ids(flagged_file):
    flags = json.loads(flagged_file.read_text())
    all_ids = []
    for market in flags:
        all_ids.extend(market["award_ids"])
    return sorted(set(all_ids))


# ── FETCH ONE AWARD'S DETAIL RECORD ─────────────────────────────────────────
def fetch_award_detail(award_id):
    url = AWARD_DETAIL_URL.format(award_id=award_id)
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.warning(f"Award detail fetch failed for {award_id}: {e}")
        return None


# ── FLATTEN ONE AWARD'S DETAIL INTO WHAT SIGNALS 2 & 3 ACTUALLY NEED ────────
# The raw response has a lot more in it than this. Competition/pricing fields
# turned out NOT to be top-level — they live under latest_transaction_contract_data,
# confirmed 2026-09-03 by reading a real raw response after the first pass came
# back with them all null. Value fields (base_exercised_options etc.) and
# parent_award genuinely are top-level, and were already correct.
def flatten_detail(award_id, detail):
    parent        = detail.get("parent_award") or {}
    contract_data = detail.get("latest_transaction_contract_data") or {}
    return {
        "generated_internal_id":                award_id,
        "extent_competed":                      contract_data.get("extent_competed"),
        "extent_competed_description":          contract_data.get("extent_competed_description"),
        "number_of_offers_received":            contract_data.get("number_of_offers_received"),
        # The FAR justification actually cited for a not-competed award — added
        # 2026-09-04 after the first pass came back with every Signal 2 "why"
        # as null. Same root cause as the competition fields above: it lives
        # under latest_transaction_contract_data, not top-level.
        "other_than_full_and_open":             contract_data.get("other_than_full_and_open"),
        "other_than_full_and_open_description":  contract_data.get("other_than_full_and_open_description"),
        "type_of_contract_pricing":             contract_data.get("type_of_contract_pricing"),
        "type_of_contract_pricing_description": contract_data.get("type_of_contract_pricing_description"),
        "solicitation_procedures":               contract_data.get("solicitation_procedures"),
        "solicitation_procedures_description":   contract_data.get("solicitation_procedures_description"),
        "base_exercised_options":     detail.get("base_exercised_options"),
        "base_and_all_options":       detail.get("base_and_all_options"),
        "total_obligation":           detail.get("total_obligation"),
        "parent_generated_id":        parent.get("generated_unique_award_id"),
        "parent_piid":                parent.get("piid"),
        "parent_idv_type":            parent.get("idv_type_description"),
    }


# ── MAIN ─────────────────────────────────────────────────────────────────
def main():
    print("─" * 50)
    print("CLEARWATER — Award Detail Pull (for Signals 2 & 3)")
    print("─" * 50)

    flagged_file = find_flagged_file()
    if flagged_file is None:
        return

    award_ids = collect_award_ids(flagged_file)
    print(f"Loaded {len(award_ids)} unique award IDs from {flagged_file.name}.")
    print(f"Estimated time: ~{len(award_ids) * PAUSE_SEC // 60} minutes at {PAUSE_SEC}s/request.\n")

    logging.info(f"Starting award-detail pull for {len(award_ids)} flagged awards from {flagged_file.name}.")

    details = []
    raw_details = []   # full, unflattened responses — saved so a field-mapping
                        # fix later doesn't require hitting the live API again
    failed  = []

    for i, award_id in enumerate(award_ids, start=1):
        time.sleep(PAUSE_SEC)   # pause before every request, including the first
        raw = fetch_award_detail(award_id)

        if raw is None:
            failed.append(award_id)
        else:
            raw_details.append(raw)
            details.append(flatten_detail(award_id, raw))

        if i % 20 == 0 or i == len(award_ids):
            print(f"  {i}/{len(award_ids)} awards fetched ({len(failed)} failed so far)...")

    out_file     = DATA_PROCESSED / f"award_details_{TODAY}.json"
    raw_out_file = DATA_RAW / f"award_details_raw_{TODAY}.json"

    with open(out_file, "w") as f:
        json.dump(details, f, indent=2)
    with open(raw_out_file, "w") as f:
        json.dump(raw_details, f, indent=2)

    print(f"\nDone. {len(details)} succeeded, {len(failed)} failed.")
    print(f"Saved: {out_file}")
    print(f"Saved (raw, for reprocessing later without re-calling the API): {raw_out_file}")
    if failed:
        print(f"Failed award IDs: {failed}")

    logging.info(
        f"Award-detail pull complete. {len(details)} succeeded, {len(failed)} failed. "
        f"Output: {out_file}."
    )
    print("─" * 50)


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
