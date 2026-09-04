# ── IMPORTS ──────────────────────────────────────────────────────────────

import json
import logging
import sqlite3
from collections import Counter
from pathlib import Path
from datetime import datetime


# ── PROJECT PATHS ─────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent.parent
DATA_DB        = BASE_DIR / "data" / "db"
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


# ── SIGNAL RULE (v2) ────────────────────────────────────────────────────
# v1 collapsed everything into one "confirmed sole-source" flag: not
# competed + over $1M + <=1 offer. Two things broke that on inspection:
#   1. The $1M floor barely filtered anything — removing it changed the
#      flagged count by zero, so it was a cosmetic gate, not a real one.
#   2. "Not competed" and "competed but only one bidder showed up" are
#      different phenomena with different "who benefits" stories. Merging
#      them into a single flag hid that distinction.
#
# v2 drops the dollar floor and keeps the two patterns separate:
#   Group A — legally open to competition (extent_competed_description ==
#             "FULL AND OPEN COMPETITION") but exactly one offer came in.
#             Nothing forced this outcome — arguably the more interesting
#             group, since agency discretion isn't the obvious explanation.
#   Group B — the government didn't open it to competition at all
#             (NOT COMPETED / NOT COMPETED UNDER SAP / NOT AVAILABLE FOR
#             COMPETITION). Most of this turns out to be FAR-justified
#             rather than a judgment call — see the "why" breakdown below,
#             pulled from other_than_full_and_open_description.
GROUP_A_COMPETED_DESC   = "FULL AND OPEN COMPETITION"
GROUP_B_CATEGORIES      = {"NOT COMPETED", "NOT COMPETED UNDER SAP", "NOT AVAILABLE FOR COMPETITION"}
REPEAT_VENDOR_THRESHOLD = 2   # a vendor "stars" at 2+ appearances in either group


# ── FIND THE MOST RECENT AWARD-DETAIL FILE ──────────────────────────────────
def find_details_file():
    matches = sorted(DATA_PROCESSED.glob("award_details_*.json"))
    if not matches:
        print("Error: no award_details_*.json found. Run fetch_award_details.py first.")
        return None
    return matches[-1]


# ── VENDOR NAMES ─────────────────────────────────────────────────────────
# award_details records don't carry a vendor name — join back to the DB.
def load_vendor_map():
    conn = sqlite3.connect(DATA_DB / "clearwater.db")
    cur = conn.execute("SELECT generated_internal_id, vendor_name FROM contracts")
    vendor_map = dict(cur.fetchall())
    conn.close()
    return vendor_map


def offers_as_int(record):
    # USASpending returns this as a string ("1", "7") when present, not a
    # number — cast before comparing, and treat anything that doesn't parse
    # cleanly the same as missing rather than raising.
    offers = record.get("number_of_offers_received")
    try:
        return int(offers)
    except (TypeError, ValueError):
        return None


def classify(record):
    ec = record.get("extent_competed_description")
    if ec == GROUP_A_COMPETED_DESC and offers_as_int(record) == 1:
        return "A"
    if ec in GROUP_B_CATEGORIES:
        return "B"
    return None


# ── MAIN ─────────────────────────────────────────────────────────────────
def main():
    print("─" * 50)
    print("CLEARWATER — Signal 2: Competition Avoidance (Group A / Group B)")
    print("─" * 50)

    details_file = find_details_file()
    if details_file is None:
        return

    records = json.loads(details_file.read_text())
    print(f"Loaded {len(records)} award-detail records from {details_file.name}.")

    vendor_map = load_vendor_map()

    group_a, group_b = [], []
    for r in records:
        group = classify(r)
        if group == "A":
            group_a.append(r)
        elif group == "B":
            group_b.append(r)

    print(f"\nGroup A (open competition, 1 offer): {len(group_a)}")
    print(f"Group B (not competed at all):       {len(group_b)}")

    # ── vendor repeat pattern within each group ──
    def vendor_counts(recs):
        c = Counter(vendor_map.get(r["generated_internal_id"], "UNKNOWN") for r in recs)
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    count_a = vendor_counts(group_a)
    count_b = vendor_counts(group_b)
    starred_a = {v: c for v, c in count_a.items() if c >= REPEAT_VENDOR_THRESHOLD}
    starred_b = {v: c for v, c in count_b.items() if c >= REPEAT_VENDOR_THRESHOLD}

    print(f"Repeat vendors (>= {REPEAT_VENDOR_THRESHOLD}x) in Group A: {len(starred_a)}")
    print(f"Repeat vendors (>= {REPEAT_VENDOR_THRESHOLD}x) in Group B: {len(starred_b)}")

    # ── Group B: the actual legal reason, pulled from the FAR citation on file ──
    why = Counter(
        r.get("other_than_full_and_open_description") or "NOT STATED"
        for r in group_b
    )
    why_breakdown = dict(sorted(why.items(), key=lambda kv: -kv[1]))
    print("\nGroup B — why (FAR justification on file):")
    for reason, n in why_breakdown.items():
        print(f"  {reason}: {n}")

    out = {
        "rule": {
            "version": 2,
            "group_a_definition": f'extent_competed_description == "{GROUP_A_COMPETED_DESC}" and exactly 1 offer',
            "group_b_categories": sorted(GROUP_B_CATEGORIES),
            "repeat_vendor_threshold": REPEAT_VENDOR_THRESHOLD,
            "note": "v1's $1,000,000 floor was dropped — verified against the real "
                    "distribution that it changed the flagged count by zero. "
                    "v1's single confirmed/unconfirmed flag was split into Group A "
                    "and Group B because they represent different phenomena.",
        },
        "group_a": {
            "n": len(group_a),
            "award_ids": [r["generated_internal_id"] for r in group_a],
            "vendor_counts": count_a,
            "starred_vendors": starred_a,
        },
        "group_b": {
            "n": len(group_b),
            "award_ids": [r["generated_internal_id"] for r in group_b],
            "vendor_counts": count_b,
            "starred_vendors": starred_b,
            "why_breakdown": why_breakdown,
        },
    }

    out_file = DATA_PROCESSED / f"flagged_sole_source_{TODAY}.json"
    with open(out_file, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved: {out_file}")

    logging.info(
        f"Sole-source signal v2: Group A {len(group_a)}, Group B {len(group_b)} "
        f"out of {len(records)} detail records. Output: {out_file}."
    )
    print("─" * 50)


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
