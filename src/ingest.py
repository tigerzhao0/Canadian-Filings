"""Stage 0: load the company list from the GuruFocus .xlsx export or a CSV.

The GuruFocus screener export has ~15 metadata rows before the real table.
We detect the header row (the one containing both "Symbol" and "Company")
rather than hard-coding row 16, so a slightly different export still works.

Normalised output: a list of Company(ticker, legal_name, exchange).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Company:
    ticker: str
    legal_name: str
    exchange: str


# Column header aliases -> normalised field. Lower-cased comparison.
_TICKER_HEADERS = {"symbol", "ticker"}
_NAME_HEADERS = {"company", "legal_company_name", "company name", "name"}
_EXCHANGE_HEADERS = {"exchange"}


def load_companies(path: str | Path) -> list[Company]:
    """Load companies from a .xlsx or .csv file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        rows = _read_xlsx_rows(path)
    elif suffix in (".csv", ".tsv"):
        rows = _read_csv_rows(path, delimiter="\t" if suffix == ".tsv" else ",")
    else:
        raise ValueError(f"Unsupported input type '{suffix}' (use .xlsx or .csv)")
    return _rows_to_companies(rows, source=str(path))


def _read_xlsx_rows(path: Path) -> list[list[str]]:
    import openpyxl  # imported lazily so `--help` works without deps installed

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append(["" if c is None else str(c).strip() for c in row])
    wb.close()
    return rows


def _read_csv_rows(path: Path, delimiter: str) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [[c.strip() for c in row] for row in csv.reader(fh, delimiter=delimiter)]


def _find_header(rows: list[list[str]]) -> int:
    """Return the index of the header row (has both a ticker and a name column)."""
    for i, row in enumerate(rows):
        lowered = {c.lower() for c in row}
        if lowered & _TICKER_HEADERS and lowered & _NAME_HEADERS:
            return i
    raise ValueError(
        "Could not find a header row containing both a ticker column "
        "(Symbol/Ticker) and a company-name column (Company/Name)."
    )


def _rows_to_companies(rows: list[list[str]], source: str) -> list[Company]:
    if not rows:
        raise ValueError(f"No rows found in {source}")
    header_idx = _find_header(rows)
    header = [c.lower() for c in rows[header_idx]]

    def col_for(aliases: set[str]) -> int | None:
        for idx, name in enumerate(header):
            if name in aliases:
                return idx
        return None

    t_col = col_for(_TICKER_HEADERS)
    n_col = col_for(_NAME_HEADERS)
    e_col = col_for(_EXCHANGE_HEADERS)
    if t_col is None or n_col is None:
        raise ValueError(f"Missing ticker or company-name column in {source}")

    companies: list[Company] = []
    seen: set[str] = set()
    for row in rows[header_idx + 1 :]:
        if t_col >= len(row) or n_col >= len(row):
            continue
        ticker = row[t_col].strip()
        name = row[n_col].strip()
        exchange = row[e_col].strip() if e_col is not None and e_col < len(row) else ""
        if not ticker or not name:
            continue
        # Skip obvious junk / repeated header fragments.
        if ticker.lower() in _TICKER_HEADERS:
            continue
        key = ticker.upper()
        if key in seen:
            continue
        seen.add(key)
        companies.append(Company(ticker=ticker, legal_name=name, exchange=exchange))
    if not companies:
        raise ValueError(f"Header found but no data rows parsed from {source}")
    return companies


def name_tokens(legal_name: str) -> list[str]:
    """Tokenise a legal name into meaningful lowercase tokens for domain matching.

    Drops common corporate suffixes/stopwords so "Royal Bank of Canada" yields
    {"royal", "bank"} which helps match rbc? no -- but matches royalbank.com.
    """
    stop = {
        "inc", "corp", "corporation", "ltd", "limited", "co", "company",
        "plc", "holdings", "holding", "group", "the", "of", "and", "&",
        "lp", "llc", "sa", "ag", "trust", "reit", "fund", "class", "series",
    }
    raw = re.split(r"[^a-z0-9]+", legal_name.lower())
    return [t for t in raw if t and t not in stop and len(t) > 1]
