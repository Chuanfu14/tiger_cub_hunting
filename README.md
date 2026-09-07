# 13F Holdings Tracker

A simple automation that gathers the top holdings of any hedge fund from their
public SEC filings and drops them into a Google Sheet — one tab per fund,
showing how each position's weight has moved quarter by quarter.

## Why

Hedge funds are required to disclose their US stock positions every quarter in
a filing called a 13F. The data is public and free, but it arrives as raw XML
buried in government archives — one file per fund, per quarter, with no
percentages and no way to compare across time.

Answering something simple like "what are Coatue's five biggest positions, and
how have they changed over the last six quarters?" means opening a dozen
filings and doing the math by hand.

This does it for you.

## What you get

Each fund gets its own tab. Holdings run down the left, quarters across the top:

| Holding | 2025Q1 | 2025Q2 | 2025Q3 | 2025Q4 | 2026Q1 | 2026Q2 |
|---|---|---|---|---|---|---|
| TAIWAN SEMICONDUCTOR | 5.83 | | 5.53 | 6.56 | 10.80 | 8.76 |
| LAM RESEARCH CORP | | | | | 7.39 | 8.41 |
| META PLATFORMS INC | 9.55 | 7.57 | 7.27 | 6.25 | | |

Each number is that stock's percentage of the fund's reported portfolio. A
blank means it wasn't in the fund's top five that quarter — so you can see
positions entering and exiting at a glance.

There's also an `About` tab explaining the data, and a `Holdings` tab with the
full underlying records.

## Choosing which funds to track

Everything lives in one file, `funds.json`. To add a fund, copy an entry and
change the name:

```json
{
  "label": "Coatue",
  "search": "Coatue Management",
  "cik": null,
  "enabled": true
}
```

`label` is what appears on the tab. `search` is the fund's registered name,
used to find them in the SEC database. Leave `cik` as `null` — the tool looks
it up and fills it in for you.

## Getting started

```bash
pip install -r requirements.txt
export SEC_CONTACT_EMAIL="you@example.com"

python fund_holdings.py --save-ciks --dry-run    # preview in the terminal
python fund_holdings.py --csv holdings.csv       # save to a spreadsheet file
python fund_holdings.py --sheet-id "<id>"        # push to Google Sheets
```

The SEC asks automated tools to identify themselves with a contact email. It's
not an account or a signup — nothing is registered, and the data is free.

Writing to Google Sheets needs a one-time credentials setup. See
[SETUP.md](SETUP.md).

## Options

| Flag | What it does |
|---|---|
| `--quarters N` | How many quarters back to pull (default 6) |
| `--top N` | How many holdings per fund (default 5) |
| `--only "Coatue"` | Just one fund, or a few |
| `--save-ciks` | Look up and save fund IDs |
| `--dry-run` | Preview only, saves nothing |
| `--csv holdings.csv` | Save to a spreadsheet file |
| `--sheet-id "<id>"` | Send to a Google Sheet |
| `--no-matrix` | Skip the combined all-funds tab |
| `--no-fund-tabs` | Skip the individual fund tabs |
| `--no-cache` | Fetch fresh data instead of reusing saved copies |

Re-running is safe — it adds new quarters without duplicating or overwriting
what's already there.

## Reading the results carefully

13F filings show less than people assume. Four things to keep in mind:

**They're incomplete.** Only US-listed stocks the fund owns. No bets against
companies, no bonds, no cash, no foreign listings. "Percent of portfolio"
means percent of what gets reported, not percent of the whole fund.

**They're late.** Funds have 45 days after each quarter ends to file, so the
newest data is at least six weeks old when it appears.

**Rising doesn't mean buying.** If a stock gains 30% and the fund does nothing
at all, its weight still goes up. These numbers show position sizes, not
trading activity.

**Big jumps can be illusions.** When a private company goes public, a fund's
long-held stake suddenly becomes reportable and can appear to take over the
portfolio overnight. Nothing was bought — it just became visible.