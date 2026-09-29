# MOP Reconciliation

A Streamlit dashboard that traces every rupee a Citykart store collects on a
business date through to where the money finally rests — cash in the drawer
or the bank, digital collections credited by Paytm, Axis or Bajaj Pay.

**Cash reconciliation to follow.** R3a and R3b (the two cash-side
reconciliations below) are still being built out, so this app currently
reconciles the digital side end to end and lays the groundwork for the cash
side. The name reflects that; it becomes "MOP & Cash Reconciliation" once
both are live.

## ⚠️ No data lives in this repository

This repo is code, tests, docs and configuration **only**. It contains
**zero** store-level financial data — no POS exports, no gateway settlement
files, no petty-cash ledgers, no database, no warehouse cache. Every one of
those is excluded by `.gitignore`, both by folder name and by file-extension
pattern, and must never be added back with `git add -f` or any other
override. If you're setting this up for the first time, see "Your own copy
of the data" below — you supply it locally, it never gets committed.

## What this system does

Three independent reconciliations, joined only for display, plus a fourth
that traces the two together:

| | Reconciliation | Grain | Question |
|---|---|---|---|
| **R1** | POS bill ↔ gateway transaction | Bill line ↔ transaction | Was every digital bill paid? Any payment with no bill? |
| **R2** | Transaction ↔ settlement ↔ bank | Transaction → payout → bank credit | Did the money reach the bank, net of correct charges, on time? |
| **R3a** | Store cash book | Store × date | Does the day's cash movement explain the drawer? |
| **R3b** | CMS deposit ↔ bank credit | Pickup | Did the cash handed to the agency reach the bank? |

R1 is fully built: POS bill lines are matched against Paytm, Axis and Bajaj
gateway transactions, with manual-match and refund overlays for anything the
engine can't resolve automatically. The store-wise sale and cash sheet, the
Float page and the CMS cash-with-agency ledger lay the groundwork for R3a/R3b.
R2 (settlement → bank) is not yet built.

Every business rule — nil tolerance, no locking a business date, no
write-offs, nothing guessed, business date always read from the data — is
documented in full in `CLAUDE.md`, which is the single source of truth for
this project's behaviour and column dictionary.

## Running it locally

### What you need

- **Python 3.14** — this is what the project is built and tested against
  (see `CLAUDE.md` section 3 for why). A close 3.x version will likely run
  it, but 3.14 is the only version this has actually been verified on.
- The packages pinned in `requirements.txt` (exact versions, not ranges —
  so your install matches what's been tested).
- **Windows** — `Start MOP Reco.bat` and some of the process-management code
  (`scripts/stop_dashboards.py`) assume a Windows machine. The Streamlit app
  itself is plain Python and should run cross-platform if you launch it
  directly instead of via the batch file.

### Install

```bash
git clone https://github.com/sahilsharan-art/Mop_reco.git
cd Mop_reco
pip install -r requirements.txt
```

### Configure

Copy the config template and point it at your own data folders:

```bash
cp app/config.example.yaml app/config.yaml
```

Edit `app/config.yaml`'s `sources:` block so each logical source (POS,
Paytm, Axis, Bajaj, PTC, CMS, the masters, opening balance) points at
wherever you've put the matching data on your own machine. `base_dir: "."`
means every path is resolved relative to the project root — see
`CLAUDE.md` sections 4–5 for the exact folder layout and file formats each
source expects.

### Run

On Windows, double-click `Start MOP Reco.bat` — it checks Python and the
required packages are installed, stops any dashboard already running (so you
never end up serving stale code on a leftover process), and opens
`http://localhost:8501` in your browser.

To run it directly instead:

```bash
python -m streamlit run app/Summary.py --server.port 8501
```

Nothing reconciles until you run it from the **Run** page — opening the
dashboard alone never touches a raw source file (see `CLAUDE.md` section
6.14). A cold full-month run takes several minutes; once a source is
warehoused as Parquet, re-runs of the same range are much faster.

## Your own copy of the data

**This repository ships no data at all — you supply your own copy of every
source folder before anything will reconcile.** The six numbered folders
this project was originally built against (`1. POS Sales`,
`2. Settlement reports`, `3. CMS received`, `4. Petty cash ledger`,
`5.CMS DEPOSIT MASTER`, `6.MID WISE STORE CODE MASTER`), plus the pickup
location master and the opening-balance folder, all need to exist locally
and be pointed at from `app/config.yaml`'s `sources:` block. None of that
is optional scaffolding — without it there is nothing to reconcile.

Exact file formats, header rows, encodings and column names for every
source are documented in full in `CLAUDE.md` section 5 — read that before
wiring up a new data folder, since several of these files have real quirks
(wrong encodings, header rows that aren't row 1, ragged CSVs, Excel serial
dates) that the loaders already work around.

## Tests

```bash
pytest
```

One file per reconciliation stage, asserting the acceptance numbers
recorded in `CLAUDE.md` section 7. A stage isn't considered complete until
its numbers match exactly, and the tests are how that's enforced.

## Project layout

```
app/
  config.yaml            your local config — gitignored, copy from config.example.yaml
  config.example.yaml     the template, safe to commit (no real paths or secrets)
  Summary.py              entrypoint / landing page
  dashboard_common.py     shared Streamlit chrome (sidebar, tiles, exports)
  mop_reco/               the pure, tested reconciliation engines — no Streamlit imports
  pages/                  the rest of the dashboard's pages
tests/                    one file per stage, pytest
docs/                     data inventory, diagnostics and performance notes
scripts/                  one-off backfills, diagnostics and the dashboard launcher's helper
CLAUDE.md                 the project brief — business rules, column dictionary,
                          acceptance numbers. Read this first.
```
