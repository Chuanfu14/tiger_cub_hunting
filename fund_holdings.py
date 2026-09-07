#!/usr/bin/env python3
"""
13F Top-Holdings Tracker
========================

Reads a list of institutional managers from funds.json, pulls their last N
quarters of SEC 13F-HR filings, computes each filing's top-K holdings by
market value and their share of the reported portfolio, then writes a flat
table to Google Sheets and/or CSV.

Quick start:
    1. Set CONTACT_EMAIL below (or the SEC_CONTACT env var).
    2. python fund_holdings.py --save-ciks --dry-run
    3. python fund_holdings.py --sheet "13F Tracker"

What 13F data is and isn't:
    - Long US-listed equity positions only. No shorts, bonds, cash,
      foreign listings, or most derivatives.
    - Filed 45 days after quarter end, so the newest quarter lags.
    - "% of portfolio" throughout means "% of 13F-reported holdings",
      which is not the same as % of the fund's actual book.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# SEC requires a descriptive User-Agent with a real contact address.
# Requests without one get rate-limited or blocked outright.
CONTACT_NAME = os.environ.get("SEC_CONTACT_NAME", "Stan Fu")
CONTACT_EMAIL = os.environ.get("SEC_CONTACT_EMAIL", "your.email@example.com")
USER_AGENT = f"{CONTACT_NAME} {CONTACT_EMAIL}"

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_DIR / "funds.json"
CACHE_DIR = PROJECT_DIR / ".edgar_cache"

DEFAULT_QUARTERS = 6
DEFAULT_TOP_N = 5
SEC_RATE_LIMIT_SECONDS = 0.12  # SEC allows ~10 req/sec; stay comfortably under

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})

_last_request_at = 0.0


def _get(url: str, cache_key: str | None = None) -> bytes:
    """Rate-limited GET with optional on-disk caching."""
    global _last_request_at

    if cache_key:
        CACHE_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", cache_key)
        cached = CACHE_DIR / safe
        if cached.exists():
            return cached.read_bytes()

    elapsed = time.time() - _last_request_at
    if elapsed < SEC_RATE_LIMIT_SECONDS:
        time.sleep(SEC_RATE_LIMIT_SECONDS - elapsed)

    resp = SESSION.get(url, timeout=30)
    _last_request_at = time.time()
    resp.raise_for_status()

    if cache_key:
        (CACHE_DIR / safe).write_bytes(resp.content)
    return resp.content


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class FundConfig:
    label: str
    search: str
    cik: str | None
    enabled: bool = True
    note: str = ""


def load_config(path: Path) -> tuple[list[FundConfig], dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    funds = [
        FundConfig(
            label=entry["label"],
            search=entry.get("search", entry["label"]),
            cik=(str(entry["cik"]).zfill(10) if entry.get("cik") else None),
            enabled=entry.get("enabled", True),
            note=entry.get("note", ""),
        )
        for entry in raw["funds"]
    ]
    return funds, raw


def save_ciks(path: Path, raw: dict, resolved: dict[str, str]) -> None:
    """Write resolved CIKs back into funds.json, preserving everything else."""
    changed = 0
    for entry in raw["funds"]:
        cik = resolved.get(entry["label"])
        if cik and not entry.get("cik"):
            entry["cik"] = cik
            changed += 1
    if changed:
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        print(f"Saved {changed} CIK(s) back to {path.name}")


# ---------------------------------------------------------------------------
# Step 1: fund name -> CIK
# ---------------------------------------------------------------------------

def resolve_cik(company_name: str) -> list[tuple[str, str]]:
    """Search EDGAR for 13F filers matching a name. Returns [(cik, name), ...]."""
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&company={requests.utils.quote(company_name)}"
        "&type=13F-HR&dateb=&owner=include&count=40&output=atom"
    )
    text = _get(url).decode("utf-8", errors="replace")
    matches: list[tuple[str, str]] = []

    # Case A: multiple hits -> results list with <company-info> blocks
    for block in re.findall(r"<company-info>(.*?)</company-info>", text, re.S):
        cik = re.search(r"<CIK>(\d+)</CIK>", block)
        name = re.search(r"<conformed-name>(.*?)</conformed-name>", block, re.S)
        if cik:
            matches.append((cik.group(1).zfill(10),
                            name.group(1).strip() if name else company_name))

    # Case B: single hit -> EDGAR serves that filer's feed directly
    if not matches:
        cik = re.search(r"<cik>(\d+)</cik>", text, re.I)
        name = re.search(r"<conformed-name>(.*?)</conformed-name>", text, re.S)
        if cik:
            matches.append((cik.group(1).zfill(10),
                            name.group(1).strip() if name else company_name))

    return matches


# ---------------------------------------------------------------------------
# Step 2: list 13F-HR filings
# ---------------------------------------------------------------------------

@dataclass
class Filing:
    cik: str
    accession: str   # with dashes
    form: str        # 13F-HR or 13F-HR/A
    period: str      # YYYY-MM-DD quarter end being reported
    filed: str       # YYYY-MM-DD

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")

    @property
    def quarter_label(self) -> str:
        y, m, _ = self.period.split("-")
        return f"{y}Q{(int(m) - 1) // 3 + 1}"


def list_13f_filings(cik: str, limit: int) -> list[Filing]:
    """Most recent 13F-HR filings, newest first, deduped to one per period."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = json.loads(_get(url, cache_key=f"sub_{cik}.json"))

    recent = data.get("filings", {}).get("recent", {})
    rows = zip(
        recent.get("accessionNumber", []),
        recent.get("form", []),
        recent.get("reportDate", []),
        recent.get("filingDate", []),
    )

    by_period: dict[str, Filing] = {}
    for accession, form, period, filed in rows:
        if not form.startswith("13F-HR") or not period:
            continue
        existing = by_period.get(period)
        # prefer the original filing over an amendment
        if existing is None or (existing.form.endswith("/A") and not form.endswith("/A")):
            by_period[period] = Filing(cik, accession, form, period, filed)

    return sorted(by_period.values(), key=lambda f: f.period, reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Step 3: parse the information table
# ---------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


@dataclass
class Position:
    issuer: str
    cusip: str
    value: float
    shares: float


def fetch_positions(filing: Filing) -> list[Position]:
    """Download a filing's INFORMATION TABLE and return its positions.

    Units note: before 2023-01-03 <value> was reported in thousands, after
    that in whole dollars. We only ever express a position as a share of the
    filing total, so units cancel and percentages are right either way.
    """
    base = (f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(filing.cik)}/{filing.accession_nodash}")
    index = json.loads(_get(f"{base}/index.json",
                            cache_key=f"idx_{filing.accession_nodash}.json"))

    items = [i["name"] for i in index.get("directory", {}).get("item", [])
             if i["name"].lower().endswith(".xml")]
    # primary_doc.xml is the cover page; the info table is the other one
    ordered = ([n for n in items if "primary_doc" not in n.lower()]
               + [n for n in items if "primary_doc" in n.lower()])

    for name in ordered:
        raw = _get(f"{base}/{name}",
                   cache_key=f"tbl_{filing.accession_nodash}_{name}")
        if b"infoTable" in raw or b"informationTable" in raw:
            positions = _parse_info_table(raw)
            if positions:
                return positions
    return []


def _parse_info_table(raw: bytes) -> list[Position]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    positions: list[Position] = []
    for node in root.iter():
        if _strip_ns(node.tag) != "infoTable":
            continue

        fields: dict[str, str] = {}
        for child in node.iter():
            if child.text and child.text.strip():
                fields[_strip_ns(child.tag)] = child.text.strip()

        def num(key: str) -> float:
            try:
                return float(fields.get(key, "0").replace(",", ""))
            except ValueError:
                return 0.0

        positions.append(Position(
            issuer=fields.get("nameOfIssuer", "UNKNOWN"),
            cusip=fields.get("cusip", ""),
            value=num("value"),
            shares=num("sshPrnamt"),
        ))
    return positions


# ---------------------------------------------------------------------------
# Step 4: aggregate to top-N
# ---------------------------------------------------------------------------

@dataclass
class Holding:
    fund: str
    quarter: str
    period: str
    filed: str
    form: str
    rank: int
    issuer: str
    cusip6: str
    value: float
    pct: float
    positions_in_filing: int


def top_holdings(fund: str, filing: Filing, positions: list[Position],
                 top_n: int) -> list[Holding]:
    """Roll up share classes and sub-managers into one line per issuer."""
    if not positions:
        return []

    grouped: dict[str, float] = defaultdict(float)
    names: dict[str, Counter] = defaultdict(Counter)

    for pos in positions:
        key = pos.cusip[:6] if len(pos.cusip) >= 6 else pos.issuer.upper()
        grouped[key] += pos.value
        names[key][pos.issuer] += 1

    total = sum(grouped.values())
    if total <= 0:
        return []

    ranked = sorted(grouped.items(), key=lambda kv: kv[1], reverse=True)
    return [
        Holding(
            fund=fund,
            quarter=filing.quarter_label,
            period=filing.period,
            filed=filing.filed,
            form=filing.form,
            rank=i,
            issuer=names[key].most_common(1)[0][0],
            cusip6=key,
            value=value,
            pct=round(value / total * 100, 2),
            positions_in_filing=len(grouped),
        )
        for i, (key, value) in enumerate(ranked[:top_n], start=1)
    ]


# ---------------------------------------------------------------------------
# Step 5: output
# ---------------------------------------------------------------------------

LOG_BANNER = ("Holdings log - one row per fund, per quarter, per top-5 "
              "holding. Source of truth; other tabs are built from this.")

HEADER = ["Fund", "Quarter", "Period End", "Filed", "Form", "Rank",
          "Holding", "CUSIP6", "Value (as reported)", "% of 13F Portfolio",
          "Total Issuers in Filing"]


def to_rows(holdings: Iterable[Holding]) -> list[list]:
    return [[h.fund, h.quarter, h.period, h.filed, h.form, h.rank,
             h.issuer, h.cusip6, round(h.value, 2), h.pct,
             h.positions_in_filing] for h in holdings]


def write_csv(rows: list[list], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {path}")


# Column positions within HEADER, used when reading rows back from Sheets.
C_FUND, C_QUARTER, C_PERIOD = 0, 1, 2
C_RANK, C_HOLDING, C_CUSIP6 = 5, 6, 7
C_PCT = 9

# A row's identity, for dedupe on append.
def _row_key(row: list) -> tuple:
    return (str(row[C_FUND]), str(row[C_PERIOD]), str(row[C_CUSIP6]))


def _safe_tab_name(name: str) -> str:
    """Google Sheets rejects [ ] * ? : / \\ in tab names, and caps length."""
    cleaned = re.sub(r"[\[\]\*\?:/\\]", "-", name).strip()
    return (cleaned or "Fund")[:95]


def _get_or_create(spreadsheet, title: str, rows: int, cols: int):
    import gspread
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title,
                                         rows=max(rows, 50), cols=max(cols, 12))


def build_matrix(all_rows: list[list], fund_filter: str | None = None
                 ) -> tuple[list[str], list[list]]:
    """Reshape the flat log into holding-by-quarter weights.

    Rows are one holding per fund; columns are quarters in chronological
    order. Cells are % of 13F portfolio, blank where the name wasn't in the
    top N that quarter. Reads from the accumulated log, so it covers every
    quarter ever collected, not just this run.
    """
    rows = [r for r in all_rows
            if fund_filter is None or str(r[C_FUND]) == fund_filter]
    if not rows:
        return [], []

    # chronological quarter columns, keyed by period-end so sorting is right
    periods = sorted({(str(r[C_PERIOD]), str(r[C_QUARTER])) for r in rows})
    quarters = [label for _, label in periods]

    cells: dict[tuple, dict[str, float]] = defaultdict(dict)
    names: dict[tuple, str] = {}

    for r in rows:
        key = (str(r[C_FUND]), str(r[C_CUSIP6]))
        names[key] = str(r[C_HOLDING])
        try:
            cells[key][str(r[C_QUARTER])] = float(r[C_PCT])
        except (ValueError, TypeError):
            continue

    def sort_key(key: tuple) -> tuple:
        # newest quarter weight first, then by how often it appears
        latest = cells[key].get(quarters[-1], 0.0)
        return (str(key[0]), -latest, -len(cells[key]))

    if fund_filter is None:
        header = ["Fund", "Holding", "CUSIP6"] + quarters
        body = [[key[0], names[key], key[1]]
                + [cells[key].get(q, "") for q in quarters]
                for key in sorted(cells, key=sort_key)]
    else:
        header = ["Holding", "CUSIP6"] + quarters
        body = [[names[key], key[1]]
                + [cells[key].get(q, "") for q in quarters]
                for key in sorted(cells, key=sort_key)]

    return header, body


FOOTNOTE = ("Cells are the stock's share of that fund's 13F-reported holdings "
            "for the quarter. Blank = not in the fund's top 5 that quarter. "
            "13F covers long US-listed equity only; it excludes shorts, bonds, "
            "cash, and foreign listings, and is filed 45 days after quarter end. "
            "A weight can move without any trading if the stock price moved. "
            "See the About tab.")


def _write_tab(spreadsheet, title: str, header: list, body: list[list],
               banner: str = "", footnote: str = "") -> None:
    """Overwrite a derived tab: banner row, header row, body, optional footnote."""
    height = len(body) + 30
    ws = _get_or_create(spreadsheet, title, height, len(header) + 2)
    ws.clear()

    block: list[list] = [[banner] + [""] * (len(header) - 1)] if banner else []
    block.append(header)
    block.extend(body)
    if footnote:
        block.append([""] * len(header))
        block.append([footnote] + [""] * (len(header) - 1))

    ws.update(values=block, range_name="A1", value_input_option="USER_ENTERED")

    last_col = gspread_utils_col(len(header))
    if banner:
        ws.merge_cells(f"A1:{last_col}1")
        ws.format(f"A1:{last_col}1", {
            "textFormat": {"bold": True, "fontSize": 12},
            "backgroundColor": {"red": 0.92, "green": 0.94, "blue": 0.98},
        })
        ws.format(f"A2:{last_col}2", {"textFormat": {"bold": True}})
        ws.freeze(rows=2)
    else:
        ws.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)

    if footnote:
        note_row = len(block)
        ws.merge_cells(f"A{note_row}:{last_col}{note_row}")
        ws.format(f"A{note_row}:{last_col}{note_row}", {
            "textFormat": {"italic": True, "fontSize": 9},
            "wrapStrategy": "WRAP",
        })


ABOUT_ROWS = [
    ["13F Holdings Tracker"],
    [""],
    ["What this is"],
    ["Top 5 equity holdings and portfolio weights for a set of hedge funds, "
     "pulled from their quarterly SEC 13F-HR filings."],
    [""],
    ["The tabs"],
    ["Holdings", "Append-only log. One row per fund, per quarter, per holding. "
                 "This is the source of truth; the other tabs are built from it."],
    ["<fund name>", "One tab per fund. Holdings down the left, quarters across "
                    "the top, portfolio weight in the cells."],
    [""],
    ["What the numbers mean"],
    ["Every percentage is that stock's share of the fund's total 13F-reported "
     "holdings for that quarter, by market value at quarter end."],
    ["A blank cell means the stock was not in that fund's top 5 that quarter. "
     "It does not necessarily mean the fund sold out -- it may just have been "
     "pushed down the list."],
    [""],
    ["Important limits"],
    ["1.", "13F covers long US-listed equity positions only. No short positions, "
           "bonds, cash, foreign listings, or most derivatives. So 'percent of "
           "portfolio' means percent of the 13F slice, not percent of the fund."],
    ["2.", "Filings are due 45 days after quarter end, so the newest data is at "
           "least six weeks stale when it appears."],
    ["3.", "A weight can rise or fall with no trading at all. If a stock gains "
           "30% and the manager does nothing, its weight still goes up. Weights "
           "show sizing, not activity."],
    ["4.", "For long/short funds, a name shown as a top long says nothing about "
           "net exposure -- it may be hedged or offset elsewhere."],
    ["5.", "When a private company lists publicly, a long-held stake appears in "
           "the 13F for the first time and can dominate the portfolio overnight. "
           "That is a visibility event, not a new purchase."],
    [""],
    ["Maintenance"],
    ["Matrix and the fund tabs are cleared and rebuilt on every run -- do not "
     "hand-edit them. Holdings is only ever appended to, so notes added in "
     "columns to the right of it will survive."],
    ["New quarters are added by re-running the script; existing data is kept."],
]


def write_about_tab(spreadsheet) -> None:
    """Create or refresh the explanatory first tab."""
    import gspread
    try:
        ws = spreadsheet.worksheet("About")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="About", rows=60, cols=4, index=0)

    ws.update(values=ABOUT_ROWS, range_name="A1",
              value_input_option="USER_ENTERED")
    ws.format("A1:D1", {"textFormat": {"bold": True, "fontSize": 14}})
    for row in (3, 6, 11, 15, 22):
        ws.format(f"A{row}:D{row}", {"textFormat": {"bold": True, "fontSize": 11}})
    ws.columns_auto_resize(0, 1)


def gspread_utils_col(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def write_sheet(rows: list[list], sheet_name: str | None, worksheet: str,
                creds_path: str, matrix_worksheet: str = "Matrix",
                per_fund_tabs: bool = True, sheet_id: str | None = None,
                matrix_tab: bool = True) -> None:
    """Append new rows to the log tab, then rebuild the derived tabs.

    The log tab ('Holdings') accumulates: re-running never duplicates a row,
    and quarters collected in earlier runs survive. The Matrix and per-fund
    tabs are views, so they're rebuilt from the full log every time.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    # Opening by ID needs only the spreadsheets scope. Opening by *title*
    # additionally searches Drive, which needs a Drive scope and the Drive
    # API enabled on the project -- so prefer --sheet-id.
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(creds)

    if sheet_id:
        spreadsheet = client.open_by_key(sheet_id)
    else:
        try:
            spreadsheet = client.open(sheet_name)
        except gspread.SpreadsheetNotFound:
            print(f"Spreadsheet '{sheet_name}' not found. Check the title, or "
                  f"use --sheet-id with the key from the sheet's URL.",
                  file=sys.stderr)
            raise

    # --- log tab: append only what's new -----------------------------------
    log = _get_or_create(spreadsheet, worksheet, len(rows) + 50, len(HEADER))
    existing = log.get_all_values()
    last_col = gspread_utils_col(len(HEADER))

    # Find the header row rather than assuming row 1, so the banner can be
    # added to sheets that were created before banners existed.
    header_idx = next((i for i, r in enumerate(existing)
                       if r and r[0] == HEADER[0]), None)

    if header_idx is None:
        log.update(values=[[LOG_BANNER] + [""] * (len(HEADER) - 1), HEADER],
                   range_name="A1", value_input_option="USER_ENTERED")
        prior: list[list] = []
        header_idx = 1
    elif header_idx == 0:
        # older sheet: no banner yet, insert one above the header
        log.insert_row([LOG_BANNER] + [""] * (len(HEADER) - 1), index=1,
                       value_input_option="USER_ENTERED")
        prior = existing[1:]
        header_idx = 1
    else:
        prior = existing[header_idx + 1:]

    log.merge_cells(f"A1:{last_col}1")
    log.format(f"A1:{last_col}1", {
        "textFormat": {"bold": True, "fontSize": 12},
        "backgroundColor": {"red": 0.92, "green": 0.94, "blue": 0.98},
    })
    log.format(f"A2:{last_col}2", {"textFormat": {"bold": True}})
    log.freeze(rows=2)

    seen = {_row_key(r) for r in prior if len(r) > C_PCT}
    fresh = [r for r in rows if _row_key(r) not in seen]

    if fresh:
        log.append_rows(fresh, value_input_option="USER_ENTERED")
        print(f"Appended {len(fresh)} new row(s) to '{worksheet}' "
              f"({len(rows) - len(fresh)} already present)")
    else:
        print(f"No new rows — '{worksheet}' already has all {len(rows)}")

    combined = prior + fresh

    # --- derived tabs: rebuilt from the whole log --------------------------
    header, body = build_matrix(combined) if matrix_tab else ([], [])
    if body:
        _write_tab(spreadsheet, matrix_worksheet, header, body,
                   banner="All funds - top 5 holdings as % of each fund's 13F "
                          "portfolio, by quarter",
                   footnote=FOOTNOTE)
        print(f"Rebuilt '{matrix_worksheet}' ({len(body)} holdings x "
              f"{len(header) - 3} quarters)")

    if per_fund_tabs:
        for fund in sorted({str(r[C_FUND]) for r in combined}):
            f_header, f_body = build_matrix(combined, fund_filter=fund)
            if f_body:
                _write_tab(spreadsheet, _safe_tab_name(fund), f_header, f_body,
                           banner=f"{fund} - top 5 holdings as % of 13F "
                                  f"portfolio, by quarter",
                           footnote=FOOTNOTE)
        print(f"Rebuilt {len({str(r[C_FUND]) for r in combined})} per-fund tab(s)")

    write_about_tab(spreadsheet)
    print("Rebuilt 'About'")


def print_table(holdings: list[Holding]) -> None:
    current = None
    for h in holdings:
        key = (h.fund, h.quarter)
        if key != current:
            current = key
            print(f"\n{h.fund} — {h.quarter}  (filed {h.filed}, {h.form}, "
                  f"{h.positions_in_filing} issuers)")
            print("-" * 64)
        print(f"  {h.rank}. {h.issuer[:40]:<40} {h.pct:>6.2f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="path to funds.json")
    p.add_argument("--quarters", type=int, default=DEFAULT_QUARTERS)
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--only", nargs="+", metavar="LABEL",
                   help="run only these fund labels")
    p.add_argument("--save-ciks", action="store_true",
                   help="write resolved CIKs back into funds.json")
    p.add_argument("--dry-run", action="store_true",
                   help="print to console, write nothing")
    p.add_argument("--csv", metavar="PATH")
    p.add_argument("--sheet", metavar="NAME", help="Google Sheets file title")
    p.add_argument("--sheet-id", metavar="KEY",
                   help="spreadsheet ID from the URL (preferred over --sheet: "
                        "needs no Drive access)")
    p.add_argument("--worksheet", default="Holdings",
                   help="name of the append-only log tab")
    p.add_argument("--matrix-worksheet", default="Matrix",
                   help="name of the all-funds matrix tab")
    p.add_argument("--no-fund-tabs", action="store_true",
                   help="skip the per-fund tabs")
    p.add_argument("--no-matrix", action="store_true",
                   help="skip the combined all-funds Matrix tab")
    p.add_argument("--creds", default=str(PROJECT_DIR / "service_account.json"))
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    if "example.com" in CONTACT_EMAIL:
        print("WARNING: set SEC_CONTACT_EMAIL (or edit CONTACT_EMAIL). "
              "SEC blocks generic user agents.\n", file=sys.stderr)

    if args.no_cache and CACHE_DIR.exists():
        for f in CACHE_DIR.iterdir():
            f.unlink()

    funds, raw_config = load_config(args.config)
    if args.only:
        wanted = {label.lower() for label in args.only}
        funds = [f for f in funds if f.label.lower() in wanted]
    funds = [f for f in funds if f.enabled]

    if not funds:
        print("No enabled funds matched.", file=sys.stderr)
        return 1

    resolved: dict[str, str] = {}
    all_holdings: list[Holding] = []

    for fund in funds:
        cik = fund.cik
        if cik is None:
            matches = resolve_cik(fund.search)
            if not matches:
                print(f"!! No 13F filer found for '{fund.search}' — skipping",
                      file=sys.stderr)
                continue
            if len(matches) > 1:
                print(f"?? '{fund.label}' matched {len(matches)} filers:",
                      file=sys.stderr)
                for c, n in matches:
                    print(f"      {c}  {n}", file=sys.stderr)
                print("   Using the first. Set the cik in funds.json to pin it.",
                      file=sys.stderr)
            cik, name = matches[0]
            resolved[fund.label] = cik
            print(f"Resolved '{fund.label}' -> CIK {cik} ({name})")

        filings = list_13f_filings(cik, args.quarters)
        if not filings:
            print(f"!! No 13F-HR filings for {fund.label} (CIK {cik})",
                  file=sys.stderr)
            continue
        if len(filings) < args.quarters:
            print(f"   Note: {fund.label} has only {len(filings)} 13F filings "
                  f"(asked for {args.quarters})")

        for filing in filings:
            positions = fetch_positions(filing)
            if not positions:
                print(f"!! Couldn't parse {fund.label} {filing.quarter_label} "
                      f"({filing.accession})", file=sys.stderr)
                continue
            all_holdings.extend(top_holdings(fund.label, filing, positions, args.top))

    if args.save_ciks and resolved:
        save_ciks(args.config, raw_config, resolved)

    if not all_holdings:
        print("No holdings collected.", file=sys.stderr)
        return 1

    all_holdings.sort(key=lambda h: (h.fund, h.period, h.rank))
    print_table(all_holdings)

    if args.dry_run:
        return 0

    rows = to_rows(all_holdings)
    if args.csv:
        write_csv(rows, args.csv)
    if args.sheet or args.sheet_id:
        write_sheet(rows, args.sheet, args.worksheet, args.creds,
                    matrix_worksheet=args.matrix_worksheet,
                    per_fund_tabs=not args.no_fund_tabs,
                    sheet_id=args.sheet_id,
                    matrix_tab=not args.no_matrix)
    if not args.csv and not args.sheet and not args.sheet_id:
        print("\n(Nothing written — pass --csv and/or --sheet.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
