# ── IMPORTS ──────────────────────────────────────────────────────────────

import json
import logging
import sqlite3
import pandas as pd
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


# ── SIGNAL THRESHOLDS ────────────────────────────────────────────────────
# HHI bands follow the standard FTC/DOJ merger-guideline convention — borrowed
# here as a screening threshold, not a literal antitrust judgment.
HHI_FLAG_THRESHOLD = 2500                       # above this = "highly concentrated"
MIN_CONTRACTS      = 5                          # smaller markets are too noisy to trust
STUDY_YEARS        = ("2022", "2023", "2024")   # the project's actual scope —
                                                 # see activity_year note in clean.py


# ── LOAD THE CONTRACT ROWS WE NEED ──────────────────────────────────────────
def load_contracts():
    conn = sqlite3.connect(DATA_DB / "clearwater.db")
    df = pd.read_sql(
        """
        SELECT generated_internal_id, award_id, sub_agency, vendor_name,
               award_amount, activity_year
        FROM contracts
        WHERE activity_year IN (?, ?, ?)
        """,
        conn,
        params=STUDY_YEARS,
    )
    conn.close()
    return df


# ── COMPUTE HHI PER (SUB_AGENCY, YEAR) MARKET ───────────────────────────────
# HHI = sum of each vendor's market share squared, scaled to a 0-10,000 range.
# One dominant vendor pushes this toward 10,000; an evenly split market
# pushes it toward 0.
def compute_hhi(df):
    vendor_totals = df.groupby(["sub_agency", "activity_year", "vendor_name"])["award_amount"].sum().reset_index()

    market_totals = vendor_totals.groupby(["sub_agency", "activity_year"])["award_amount"].sum().reset_index()
    market_totals = market_totals.rename(columns={"award_amount": "market_total"})

    merged = vendor_totals.merge(market_totals, on=["sub_agency", "activity_year"])
    merged["share"] = merged["award_amount"] / merged["market_total"]
    merged["share_sq"] = merged["share"] ** 2

    hhi = merged.groupby(["sub_agency", "activity_year"])["share_sq"].sum().reset_index()
    hhi["hhi"] = hhi["share_sq"] * 10000
    hhi = hhi.drop(columns=["share_sq"])

    contract_counts = df.groupby(["sub_agency", "activity_year"]).size().reset_index(name="n_contracts")

    top_vendor = merged.loc[merged.groupby(["sub_agency", "activity_year"])["share"].idxmax()]
    top_vendor = top_vendor[["sub_agency", "activity_year", "vendor_name", "share"]]
    top_vendor = top_vendor.rename(columns={"vendor_name": "top_vendor", "share": "top_vendor_share"})

    result = hhi.merge(contract_counts, on=["sub_agency", "activity_year"]).merge(top_vendor, on=["sub_agency", "activity_year"])
    return result.sort_values("hhi", ascending=False)


# ── BUILD THE FLAG LIST ─────────────────────────────────────────────────────
# Turns each flagged market into a record that signals 2 and 3 can read
# directly — including the actual award IDs in that market, so nothing
# downstream has to re-derive this list from scratch.
def flag_markets(result, df):
    eligible = result[result["n_contracts"] >= MIN_CONTRACTS]
    flagged = eligible[eligible["hhi"] > HHI_FLAG_THRESHOLD]

    flags = []
    for _, row in flagged.iterrows():
        in_market = df[
            (df["sub_agency"] == row["sub_agency"]) &
            (df["activity_year"] == row["activity_year"])
        ]
        flags.append({
            "sub_agency": row["sub_agency"],
            "activity_year": row["activity_year"],
            "n_contracts": int(row["n_contracts"]),
            "hhi": round(float(row["hhi"]), 1),
            "top_vendor": row["top_vendor"],
            "top_vendor_share": round(float(row["top_vendor_share"]), 4),
            "award_ids": in_market["generated_internal_id"].tolist(),
        })
    return flags


# ── MAIN ─────────────────────────────────────────────────────────────────
def main():
    print("─" * 50)
    print("CLEARWATER — Signal 1: Vendor Concentration (HHI)")
    print("─" * 50)

    df = load_contracts()
    print(f"Loaded {len(df):,} contracts from {STUDY_YEARS[0]}-{STUDY_YEARS[-1]}.")

    result = compute_hhi(df)
    eligible = result[result["n_contracts"] >= MIN_CONTRACTS]
    flags = flag_markets(result, df)
    total_flagged_contracts = sum(f["n_contracts"] for f in flags)

    print(f"\n{len(result)} sub_agency x year markets total, {len(eligible)} with >= {MIN_CONTRACTS} contracts.")
    print(f"{len(flags)} flagged at HHI > {HHI_FLAG_THRESHOLD}.")
    print(f"Total contracts inside flagged markets: {total_flagged_contracts:,}")

    out_file = DATA_PROCESSED / f"flagged_vendor_concentration_{TODAY}.json"
    with open(out_file, "w") as f:
        json.dump(flags, f, indent=2)
    print(f"\nSaved: {out_file}")

    logging.info(
        f"Vendor concentration signal: {len(flags)} markets flagged "
        f"({total_flagged_contracts} contracts) out of {len(eligible)} eligible markets."
    )
    print("─" * 50)


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
