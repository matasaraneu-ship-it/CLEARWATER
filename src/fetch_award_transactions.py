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


# ── LOGGING SETUP ───────────────────────────────────────────────────────
# Appends to the same clearwater.log as the rest of the pipeline.

logging.basicConfig(
    filename=LOGS_DIR / "clearwater.log",
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

TODAY = datetime.today().strftime("%Y-%m-%d")


# ── CONSTANTS ─────────────────────────────────────────────────────────────
# One or more POSTs per award, verified 2026-09-04 against real responses.
# The endpoint paginates 10 transactions per page, newest modification
# first, with the base award (modification_number "0") on the LAST page.
# page_metadata comes back as {"page", "next", "previous", "hasNext",
# "hasPrevious"} — loop while hasNext is true.
TRANSACTIONS_URL = "https://api.usaspending.gov/api/v2/transactions/"
PAUSE_SEC     = 3    # pacing between awards
PAGE_PAUSE_SEC = 1   # pacing between pages of the same award
PAGE_LIMIT    = 10
MAX_PAGES     = 25   # safety cap so one runaway award can't hang the run


# ── FIND THE MOST RECENT FLAGGED-MARKETS FILE ──────────────────────────────
def find_flagged_file():
    matches = sorted(DATA_PROCESSED.glob("flagged_vendor_concentration_*.json"))
    if not matches:
        print("Error: no flagged_vendor_concentration_*.json found. Run flag_vendor_concentration.py first.")
        return None
    return matches[-1]


def collect_award_ids(flagged_file):
    flags = json.loads(flagged_file.read_text())
    all_ids = []
    for market in flags:
        all_ids.extend(market["award_ids"])
    return sorted(set(all_ids))


# ── FETCH ONE AWARD'S FULL TRANSACTION HISTORY ──────────────────────────────
# Walks every page for this award (not just the first) so long modification
# histories — the exact contracts Signal 3 cares about — aren't truncated.
def fetch_transactions(award_id):
    all_results = []
    page = 1

    while True:
        try:
            response = requests.post(
                TRANSACTIONS_URL,
                json={"award_id": award_id, "page": page, "limit": PAGE_LIMIT},
                timeout=30,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.warning(f"Transaction fetch failed for {award_id} (page {page}): {e}")
            return all_results if all_results else None

        data = response.json()
        all_results.extend(data.get("results", []))

        meta = data.get("page_metadata", {})
        if not meta.get("hasNext"):
            break

        page += 1
        if page > MAX_PAGES:
            logging.warning(f"{award_id}: hit MAX_PAGES ({MAX_PAGES}) with more pages still available.")
            break

        time.sleep(PAGE_PAUSE_SEC)

    if page > 1:
        logging.info(f"{award_id}: pulled {page} pages ({len(all_results)} transactions).")

    return all_results


# ── SUMMARIZE ONE AWARD'S MODIFICATION HISTORY ──────────────────────────────
# The base transaction is the one carrying modification_number "0" — the
# original award itself, not just whichever transaction has the earliest
# action_date in whatever page happened to be pulled. Everything else is a
# modification. This only aggregates the numbers; deciding what counts as
# "ballooning" is a separate step (flag_contract_ballooning.py).
def summarize(award_id, transactions):
    if not transactions:
        return None

    base_tx = next((t for t in transactions if str(t.get("modification_number")) == "0"), None)
    if base_tx is None:
        logging.warning(f"{award_id}: no modification_number '0' transaction found among "
                         f"{len(transactions)} pulled transactions; skipping ratio calc.")
        return None

    base_amount  = base_tx.get("federal_action_obligation") or 0.0
    total_amount = sum(t.get("federal_action_obligation") or 0.0 for t in transactions)
    mod_amount   = total_amount - base_amount   # everything added after the base award

    ratio = (total_amount / base_amount) if base_amount else None

    return {
        "generated_internal_id": award_id,
        "n_transactions": len(transactions),
        "base_obligation": base_amount,
        "total_obligation": total_amount,
        "modification_amount": mod_amount,
        "modification_ratio": ratio,
    }


# ── MAIN ────────────────────────────────────────────────────────────────
def main():
    print("─" * 50)
    print("CLEARWATER — Transaction History Pull (for Signal 3)")
    print("─" * 50)

    flagged_file = find_flagged_file()
    if flagged_file is None:
        return

    award_ids = collect_award_ids(flagged_file)
    print(f"Loaded {len(award_ids)} unique award IDs from {flagged_file.name}.")
    print(f"Estimated time: several minutes — longer modification histories take extra pages.\n")

    logging.info(f"Starting transaction-history pull for {len(award_ids)} flagged awards from {flagged_file.name}.")

    summaries   = []
    raw_all     = {}
    failed      = []
    no_base_tx  = []

    for i, award_id in enumerate(award_ids, start=1):
        time.sleep(PAUSE_SEC)
        txs = fetch_transactions(award_id)

        if txs is None:
            failed.append(award_id)
        else:
            raw_all[award_id] = txs
            summary = summarize(award_id, txs)
            if summary:
                summaries.append(summary)
            else:
                no_base_tx.append(award_id)

        if i % 20 == 0 or i == len(award_ids):
            print(f"  {i}/{len(award_ids)} awards fetched ({len(failed)} failed so far)...")

    out_file     = DATA_PROCESSED / f"award_transactions_summary_{TODAY}.json"
    raw_out_file = DATA_RAW / f"award_transactions_raw_{TODAY}.json"

    with open(out_file, "w") as f:
        json.dump(summaries, f, indent=2)
    with open(raw_out_file, "w") as f:
        json.dump(raw_all, f, indent=2)

    print(f"\nDone. {len(summaries)} summarized, {len(failed)} failed, {len(no_base_tx)} missing a base transaction.")
    print(f"Saved: {out_file}")
    print(f"Saved (raw, for reprocessing later without re-calling the API): {raw_out_file}")
    if failed:
        print(f"Failed award IDs: {failed}")
    if no_base_tx:
        print(f"Award IDs with no modification_number '0' found: {no_base_tx}")

    logging.info(
        f"Transaction-history pull complete. {len(summaries)} summarized, {len(failed)} failed, "
        f"{len(no_base_tx)} missing a base transaction. Output: {out_file}."
    )
    print("─" * 50)


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
