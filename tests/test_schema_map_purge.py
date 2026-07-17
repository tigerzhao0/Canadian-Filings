"""Regression test for a stale-ghost-data bug in run_schema_mapping: the purge
before rewrite used to target only BRAND-NEW tickers ({r["ticker"] for r in
company_rows}, gated on `ticker not in prior_tickers`), so any ticker already
known from an earlier run was NEVER purged on a later --force rerun. Confirmed
in production: RY's CashAndCashEquivalents/ShortTermInvestments stayed frozen
at a stale zero-filled value from before a code fix, surviving several later
--force reruns untouched, because RY had already been processed once before.

Fixed: purge scope is now every ticker in `grouped` (everyone actually being
rebuilt this run), not just newly-discovered ones.

Runs under pytest, or standalone:  python tests/test_schema_map_purge.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from line_items import _RAW_TABLE_SQL  # noqa: E402
from schema_map import run_schema_mapping  # noqa: E402

_NOW = "2026-01-01T00:00:00"


def _seed_raw_line_items(db_path: str, label: str, value: float) -> None:
    """One BS row for ticker ACME, pdf_year 2025, with a single mapped label
    whose alias-hit depends on `label` -- lets a test simulate 'the matcher
    used to hit X, now hits Y' across two runs of the SAME ticker. Also seeds
    the minimal `filings` / `filing_pdfs` tables _load_raw_line_items joins
    against (identity + pdf_url hint lookups)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_RAW_TABLE_SQL)
    conn.execute("CREATE TABLE IF NOT EXISTS filings "
                 "(ticker TEXT, company_name TEXT, exchange TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS filing_pdfs "
                 "(ticker TEXT, fiscal_year INTEGER, pdf_url TEXT)")
    conn.execute("DELETE FROM raw_line_items WHERE ticker='ACME'")
    conn.execute("DELETE FROM filings WHERE ticker='ACME'")
    conn.execute("DELETE FROM filing_pdfs WHERE ticker='ACME'")
    conn.execute("INSERT INTO filings VALUES ('ACME', 'Acme Corp', 'TSX')")
    conn.execute("INSERT INTO filing_pdfs VALUES ('ACME', 2025, "
                 "'https://example.com/acme_2025.pdf')")
    conn.execute(
        "INSERT INTO raw_line_items (ticker, pdf_year, statement_type, line_no, "
        "section, zone, label, synthetic, col_year, period_end, value_printed, "
        "unit_scale, currency, parsed_at) VALUES "
        "('ACME', 2025, 'balance_sheet', 1, NULL, NULL, ?, 0, 2025, NULL, ?, "
        "1.0, 'CAD', ?)", (label, value, _NOW))
    conn.commit()
    conn.close()


def test_rerunning_a_known_ticker_purges_stale_mapped_keys(tmp_path):
    src_db = str(tmp_path / "filings.db")
    fin_db = str(tmp_path / "pdf_financials.db")
    cfg = {"storage": {"db_path": src_db}, "rules": {"db_path": fin_db}}

    # Run 1: a real vocab alias -> CashAndCashEquivalents.
    _seed_raw_line_items(src_db, "Cash and cash equivalents", 500.0)
    run_schema_mapping(cfg, force=True, tickers={"ACME"})
    conn = sqlite3.connect(fin_db)
    v1 = conn.execute(
        "SELECT value FROM statement_lines WHERE ticker='ACME' AND "
        "line_item='CashAndCashEquivalents'").fetchall()
    conn.close()
    assert v1 == [(500.0,)], "sanity: first run should map Cash -> 500"

    # Run 2 (ACME is now a KNOWN ticker -- the exact scenario that broke):
    # the raw line's label changes to something that does NOT map at all.
    # A correct purge means the stale 500.0 must be GONE, not survive as a ghost.
    _seed_raw_line_items(src_db, "totally unmapped gibberish label", 500.0)
    run_schema_mapping(cfg, force=True, tickers={"ACME"})
    conn = sqlite3.connect(fin_db)
    v2 = conn.execute(
        "SELECT value FROM statement_lines WHERE ticker='ACME' AND "
        "line_item='CashAndCashEquivalents'").fetchall()
    conn.close()
    assert v2 == [], (
        f"stale ghost survived a --force rerun of an already-known ticker: {v2}")


def _run():
    import tempfile
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
