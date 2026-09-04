# Clearwater

A risk-triage tool for DoD IT/professional-services contracts, built on public USASpending.gov data. It flags markets and vendors worth a closer look across three independent signals — vendor concentration, competition avoidance, and post-award contract growth — and rolls the results into an interactive dashboard.

**This is a triage tool, not an accusation engine.** Every number below comes from public procurement data. Nothing here is evidence of wrongdoing — it's a starting point for the kind of question a contracting officer, auditor, or journalist would ask next.

[**Open the dashboard**](dashboard.html) for the full interactive walkthrough — this README covers the methodology and how to reproduce it.

## The three signals

**Signal 1 — Vendor concentration (HHI).** HHI computed per (sub-agency × year) market for 2022–2024, restricted to markets with at least 5 contracts (smaller markets are too noisy to trust). 45 eligible markets; 15 exceed the standard 2500 "highly concentrated" threshold. Every contract inside those 15 markets — 190 total — is the population the other two signals scope down to. Key finding: concentration tracks market size, not anything more sinister — DISA, the Air Force, and the Army run hundreds of contracts a year and stay under 1000 HHI, while niche markets with 5–50 contracts routinely land at 3000–5000. High HHI in a small market isn't automatically suspicious; it's why Signals 2 and 3 exist.

**Signal 2 — Competition avoidance.** Within the 190, two different patterns are tracked separately rather than merged into one flag, because they have different "who benefits" stories:
- **Group A** (26 contracts) — the solicitation was legally open to competition, but only one vendor bid.
- **Group B** (66 contracts) — the government didn't open it to competition at all, under a FAR exception.

For Group B, the actual legal justification is pulled from each award's FAR citation rather than assumed: 48% is authorized by statute (small-business/set-aside programs Congress mandates — policy working as intended, not a risk indicator), 35% is agency judgment that only one source could meet the need (the category actually worth case-by-case scrutiny), and the remaining 17% is SAP dollar-threshold exemptions and a handful of narrow exceptions (national security, brand-name, follow-on).

**Signal 3 — Contract ballooning.** total_obligation ÷ base_obligation across the same population (178 of 190 have a usable ratio). Growth after award turns out to be the norm, not the exception — 78% of contracts obligated more than their original award, 65% at least doubled, median growth 2.69×. Rather than pick an arbitrary cutoff, the signal reports the distribution itself plus two vendor leaderboards (how often a vendor's contracts grow, and how much growth by dollars) and a cross-cut by competition status. That cross-cut is itself a finding: competed and not-competed contracts balloon at nearly identical rates (median 2.68× vs. 2.76×, ~78% grew either way) — the three most extreme outliers in the whole dataset (Leidos 364×, Amentum 47×, ManTech 25×) are all in *competed* awards, mostly large IDIQ task orders where the competition happened once at the vehicle level, long before the growth.

**Across signals:** HHI and modification ratio are statistically independent (Pearson r ≈ −0.02) — concentration, competition avoidance, and ballooning are three separate risk dimensions in this data, not three symptoms of one underlying cause. That's why the dashboard reports them side by side with a company-level star system instead of collapsing them into a single composite score.

## Repo structure

```
src/
  data_pull.py                 bulk contract search, USASpending.gov API      (Step 1)
  clean.py                     normalize the raw pull                         (Step 2)
  load_db.py                   load into data/db/clearwater.db (SQLite)       (Step 3)
  flag_vendor_concentration.py Signal 1 — HHI per market                      (Step 4)
  fetch_award_details.py       pull award detail for the 190 flagged awards   (Step 5)
  fetch_award_transactions.py  pull transaction/modification history         (Step 6)
  flag_sole_source.py          Signal 2 — Group A / Group B                   (Step 7)
  flag_contract_ballooning.py  Signal 3 — distribution + leaderboards         (Step 8)

data/
  db/clearwater.db             all contracts, loaded from the cleaned bulk pull
  raw/                         cached raw API responses for the 190 flagged awards
  processed/                   every signal's output (JSON), used to build the dashboard

dashboard.html                 self-contained interactive dashboard (open in any browser)
requirements.txt
```

## Running it

Steps 1–6 already ran — their output is cached in `data/`, so reproducing the two signal scripts doesn't touch the API or need any dependencies beyond the Python standard library:

```
pip install -r requirements.txt   # only needed for steps 1-6 (requests, pandas)
python src/flag_sole_source.py
python src/flag_contract_ballooning.py
```

Each prints a summary to the console and writes a dated JSON file to `data/processed/`. To rebuild from a cold start — including the live API pulls — run the eight scripts in the order listed above. Steps 1, 5, and 6 hit the public USASpending.gov API with paced requests (a few minutes each); since it's live public data, a from-scratch rerun months from now may turn up slightly different numbers than what's reported here as new contracts are added.

## Data source

[USASpending.gov API](https://api.usaspending.gov/) — no key required, public federal spending data. Extent-of-competition codes and definitions: [fpds.gov/help/Extent_Competed](https://www.fpds.gov/help/Extent_Competed.htm). Sole-source justification authority: FAR Part 6.
