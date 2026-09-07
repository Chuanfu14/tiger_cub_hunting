# 13F Top-Holdings Tracker

Pulls top-N holdings and portfolio weights for a list of hedge funds from SEC
13F filings, across the last N quarters, and writes them to Google Sheets.

## Setup

```bash
# in VS Code: File > Open Folder > this directory, then open a terminal
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

In VS Code, hit `Cmd/Ctrl+Shift+P` → **Python: Select Interpreter** → pick the
`.venv` one. The three launch configs in `.vscode/launch.json` then work off
the Run and Debug panel (F5).

## First run

Set your contact info first — SEC blocks generic user agents:

```bash
export SEC_CONTACT_NAME="Your Name"
export SEC_CONTACT_EMAIL="you@example.com"
```

Or edit the `env` block in `.vscode/launch.json`.

Then resolve CIKs and preview without writing anywhere:

```bash
python fund_holdings.py --save-ciks --dry-run
```

This prints what each fund name matched on EDGAR. **Check these.** Manager
names are messy — "Tiger Global Management" may match several registered
entities, and the wrong pick gives you a wrong portfolio. Once you've
confirmed, the CIKs are written into `funds.json` and future runs skip the
lookup entirely.

## Google Sheets auth

1. Google Cloud Console → new project → enable the **Google Sheets API**.
2. Credentials → Create Credentials → **Service Account**.
3. On the service account, Keys → Add Key → JSON. Save it as
   `service_account.json` in this folder. (It's gitignored.)
4. Open that JSON, copy the `client_email` value.
5. Create your spreadsheet in Google Sheets, hit Share, and share it with
   that email as **Editor**.

Then:

```bash
python fund_holdings.py --sheet "13F Tracker"
```

## Adding a fund

Open `funds.json`, copy any block, change `label` and `search`, set
`"cik": null`, and run with `--save-ciks`. Nothing else to touch.

## Useful flags

| Flag | Effect |
|---|---|
| `--quarters 8` | more history (default 6) |
| `--top 10` | top 10 instead of top 5 |
| `--only "Tiger Global" "Coatue"` | run a subset |
| `--csv out.csv` | also write CSV |
| `--dry-run` | console only |
| `--no-cache` | clear cached EDGAR responses and refetch |

Responses are cached in `.edgar_cache/` so re-runs are fast and you aren't
hammering EDGAR while iterating. Filings never change once posted, so caching
is safe — except at the start of a filing window, when `--no-cache` picks up
newly posted filings.

## Sheet layout

Three kinds of tab:

**`Holdings`** — the append-only log. One row per holding per fund per
quarter:

`Fund | Quarter | Period End | Filed | Form | Rank | Holding | CUSIP6 | Value | % of 13F Portfolio | Total Issuers in Filing`

Re-running never duplicates a row — dedupe is on Fund + Period End + CUSIP6.
Quarters collected in earlier runs stay put. This is the source of truth.

**`Matrix`** — all funds, holdings down the left, quarters across the top,
weights in the cells. Blank means the name wasn't in the top N that quarter.

**One tab per fund** — the same matrix, filtered. Tab name matches the
`label` in `funds.json`. Disable with `--no-fund-tabs`.

The Matrix and per-fund tabs are *views*: they get cleared and rebuilt from
the whole `Holdings` log on every run. So don't edit them by hand — your
edits get wiped next run. If you want to annotate, do it on a new tab that
references them, or add columns to the right of `Holdings`.

Because the views read from the accumulated log rather than the current run,
you can pull six quarters today and one quarter next February, and the matrix
will show all seven.

`CUSIP6` is the issuer-level CUSIP, so multiple share classes of the same
company roll into one line. It's a stable join key — better than the issuer
name, which funds spell inconsistently.

## What this data is not

13F covers **long US-listed equity positions only**. It excludes short
positions, bonds, cash, foreign listings, and most derivatives. Filings are
due 45 days after quarter end, so the newest quarter lags and the positions
you see are already stale by at least six weeks.

This matters most for the long/short funds here. A fund could be net short a
name that shows up as a top-five long in this table. And for D1 in
particular, a large private book means the 13F is a small slice of the actual
portfolio. "% of 13F portfolio" is a real number; "% of the fund" is not
something 13F can tell you.
