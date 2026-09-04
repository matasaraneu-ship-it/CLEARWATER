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
logging.basicConfig(
    filename=LOGS_DIR / "clearwater.log",
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

TODAY = datetime.today().strftime("%Y-%m-%d")


# ── SIGNAL RULE (v2) ────────────────────────────────────────────────────
# v1 picked a fixed ratio cutoff and split contracts into confirmed /
# unconfirmed ballooning. That turned out to be the wrong shape for this
# data: growth after award is the norm (78% of contracts grew at all), so
# a pass/fail cutoff mostly just relabels the middle of a normal
# distribution as "flagged." v2 drops the cutoff and instead:
#   - reports the honest shape of the distribution (mean/median/quartiles),
#   - ranks vendors by how OFTEN and how MUCH their contracts grew
#     (two different leaderboards — frequency vs. dollar-weighted), and
#   - cross-cuts the same distribution by competition status, to check
#     whether ballooning is actually linked to Signal 2's competed /
#     not-competed split, or is an independent pattern.
LEADERBOARD_MIN_CONTRACTS = 2            # single-contract vendors are just noise
AGG_LEADERBOARD_MIN_BASE  = 1_000_000    # dollar-weighted ratio needs real dollars behind it
NOT_COMPETED_CATEGORIES   = {"NOT COMPETED", "NOT COMPETED UNDER SAP", "NOT AVAILABLE FOR COMPETITION"}


# ── FIND THE MOST RECENT INPUT FILES ────────────────────────────────────
def find_latest(pattern, script_hint):
    matches = sorted(DATA_PROCESSED.glob(pattern))
    if not matches:
        print(f"Error: no {pattern} found. Run {script_hint} first.")
        return None
    return matches[-1]


def load_vendor_map():
    conn = sqlite3.connect(DATA_DB / "clearwater.db")
    cur = conn.execute("SELECT generated_internal_id, vendor_name FROM contracts")
    vendor_map = dict(cur.fetchall())
    conn.close()
    return vendor_map


# ── COMPETITION-STATUS BUCKET (for the Signal 2 cross-cut) ──────────────
def offers_as_int(record):
    offers = record.get("number_of_offers_received")
    try:
        return int(offers)
    except (TypeError, ValueError):
        return None


def competition_bucket(detail_record):
    ec = detail_record.get("extent_competed_description")
    if ec in NOT_COMPETED_CATEGORIES:
        return "not_competed"
    if offers_as_int(detail_record) == 1:
        return "competed_1bid"
    return "competed_multi"


# ── DISTRIBUTION STATS ────────────────────────────────────────────────────
def distribution_stats(ratios):
    vals = sorted(ratios)
    n = len(vals)
    if n == 0:
        return None
    def pct(p):
        return vals[min(int(n * p / 100), n - 1)]
    return {
        "n": n,
        "mean": round(sum(vals) / n, 2),
        "median": round(pct(50), 2),
        "p25": round(pct(25), 2),
        "p75": round(pct(75), 2),
        "min": round(vals[0], 2),
        "max": round(vals[-1], 2),
        "pct_any_growth": round(100 * sum(1 for v in vals if v > 1) / n, 1),
        "pct_doubled":    round(100 * sum(1 for v in vals if v >= 2) / n, 1),
    }


HIST_EDGES  = [0, 1, 1.5, 2, 3, 5, 8, 15, 30, 60, 120, 400]
HIST_LABELS = ["0-1x","1-1.5x","1.5-2x","2-3x","3-5x","5-8x","8-15x","15-30x","30-60x","60-120x","120-400x"]

def histogram_bins(ratios):
    counts = [0] * len(HIST_LABELS)
    for v in ratios:
        for i in range(len(HIST_EDGES) - 1):
            if HIST_EDGES[i] <= v < HIST_EDGES[i + 1]:
                counts[i] += 1
                break
        else:
            if v >= HIST_EDGES[-1]:
                counts[-1] += 1
    return dict(zip(HIST_LABELS, counts))


# ── MAIN ─────────────────────────────────────────────────────────────────
def main():
    print("─" * 50)
    print("CLEARWATER — Signal 3: Contract Ballooning")
    print("─" * 50)

    tx_file = find_latest("award_transactions_summary_*.json", "fetch_award_transactions.py")
    details_file = find_latest("award_details_*.json", "fetch_award_details.py")
    if tx_file is None or details_file is None:
        return

    tx = json.loads(tx_file.read_text())
    details = json.loads(details_file.read_text())
    details_by_id = {d["generated_internal_id"]: d for d in details}
    vendor_map = load_vendor_map()

    print(f"Loaded {len(tx)} transaction summaries from {tx_file.name}.")
    print(f"Loaded {len(details)} award-detail records from {details_file.name}.")

    rows = []
    for t in tx:
        ratio = t.get("modification_ratio")
        if ratio is None:
            continue
        aid = t["generated_internal_id"]
        detail = details_by_id.get(aid)
        rows.append({
            "id": aid,
            "vendor": vendor_map.get(aid, "UNKNOWN"),
            "ratio": ratio,
            "base_obligation": t.get("base_obligation"),
            "total_obligation": t.get("total_obligation"),
            "bucket": competition_bucket(detail) if detail else "unknown",
        })

    print(f"{len(rows)} of {len(tx)} contracts have a usable ratio (real, nonzero base).")

    overall = distribution_stats([r["ratio"] for r in rows])
    print(f"\nOverall: median {overall['median']}x, {overall['pct_any_growth']}% grew, "
          f"{overall['pct_doubled']}% at least doubled.")

    # ── vendor leaderboards ──
    contract_count = Counter(r["vendor"] for r in rows)
    ballooned_count = Counter(r["vendor"] for r in rows if r["ratio"] > 1)
    base_sum, total_sum = Counter(), Counter()
    for r in rows:
        if r["base_obligation"]:
            base_sum[r["vendor"]] += r["base_obligation"]
            total_sum[r["vendor"]] += r["total_obligation"] or 0

    # Store both the ballooned count and the vendor's total contract count in
    # this population, so the leaderboard is self-contained (a reader can see
    # "9 of 14" without recomputing the denominator from the raw rows).
    freq_leaderboard = {
        v: {"ballooned": ballooned_count.get(v, 0), "of": n}
        for v, n in contract_count.items()
        if n >= LEADERBOARD_MIN_CONTRACTS
    }
    freq_leaderboard = dict(sorted(freq_leaderboard.items(), key=lambda kv: -kv[1]["ballooned"]))

    agg_leaderboard = {
        v: round(total_sum[v] / base_sum[v], 2)
        for v in base_sum
        if contract_count[v] >= LEADERBOARD_MIN_CONTRACTS and base_sum[v] >= AGG_LEADERBOARD_MIN_BASE
    }
    agg_leaderboard = dict(sorted(agg_leaderboard.items(), key=lambda kv: -kv[1]))

    n_freq_2plus = sum(1 for v in freq_leaderboard.values() if v["ballooned"] >= 2)
    print(f"Frequency leaderboard (>= {LEADERBOARD_MIN_CONTRACTS} contracts): "
          f"{len(freq_leaderboard)} vendors, {n_freq_2plus} ballooned 2+ times")
    print(f"Aggregate-ratio leaderboard (>= {LEADERBOARD_MIN_CONTRACTS} contracts, "
          f">= ${AGG_LEADERBOARD_MIN_BASE:,} base): {len(agg_leaderboard)} vendors")

    # ── competed vs. not-competed cross-cut ──
    by_bucket = {"not_competed": [], "competed_1bid": [], "competed_multi": []}
    for r in rows:
        if r["bucket"] in by_bucket:
            by_bucket[r["bucket"]].append(r["ratio"])

    bucket_stats = {k: distribution_stats(v) for k, v in by_bucket.items()}
    competed_all = by_bucket["competed_1bid"] + by_bucket["competed_multi"]
    two_way = {
        "competed": distribution_stats(competed_all),
        "not_competed": distribution_stats(by_bucket["not_competed"]),
    }
    two_way_bins = {
        "competed": histogram_bins(competed_all),
        "not_competed": histogram_bins(by_bucket["not_competed"]),
    }

    print("\nCompeted vs. not competed:")
    print(f"  Competed:     n={two_way['competed']['n']}, median {two_way['competed']['median']}x, "
          f"{two_way['competed']['pct_any_growth']}% grew")
    print(f"  Not competed: n={two_way['not_competed']['n']}, median {two_way['not_competed']['median']}x, "
          f"{two_way['not_competed']['pct_any_growth']}% grew")

    out = {
        "rule": {
            "version": 2,
            "note": "No pass/fail ratio cutoff — v1's fixed threshold mostly just relabeled "
                    "the middle of a normal distribution. This reports the distribution itself, "
                    "vendor leaderboards, and a competed vs. not-competed cross-cut instead.",
            "leaderboard_min_contracts": LEADERBOARD_MIN_CONTRACTS,
            "agg_leaderboard_min_base": AGG_LEADERBOARD_MIN_BASE,
        },
        "overall": overall,
        "overall_histogram": histogram_bins([r["ratio"] for r in rows]),
        "leaderboards": {
            "frequency": freq_leaderboard,
            "aggregate_ratio": agg_leaderboard,
        },
        "competition_cross_cut": {
            "three_way": bucket_stats,
            "two_way": two_way,
            "two_way_histograms": two_way_bins,
        },
    }

    out_file = DATA_PROCESSED / f"flagged_contract_ballooning_{TODAY}.json"
    with open(out_file, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved: {out_file}")

    logging.info(
        f"Ballooning signal v2: {len(rows)} contracts with a usable ratio, "
        f"median {overall['median']}x. {len(freq_leaderboard)} vendors on the frequency "
        f"leaderboard, {len(agg_leaderboard)} on the aggregate-ratio leaderboard. "
        f"Output: {out_file}."
    )
    print("─" * 50)


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
