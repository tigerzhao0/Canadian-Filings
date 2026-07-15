"""Tests for the document-type gate (primary statements vs AIF / MD&A / interim).

Runs under pytest, or standalone:  python tests/test_doc_classify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import doc_classify as dc  # noqa: E402


def _rows(header, n=20):
    return f"{header}\n" + "\n".join(f"Line {i} 1,234 5,678" for i in range(n))


def test_primary_statements():
    pages = [
        "Cover\nAnnual Report 2025",
        "Independent Auditor's Report\nWe have audited the accompanying...",
        _rows("Consolidated Statements of Income"),
        _rows("Consolidated Balance Sheets"),
        _rows("Consolidated Statements of Cash Flows"),
        "Notes to the Consolidated Financial Statements\n1. Basis of preparation",
    ]
    k = dc.classify_from_pages(pages)
    assert k.doc_type == dc.PRIMARY and k.is_primary
    assert k.n_statement_types == 3 and k.has_auditor


def test_aif_rejected():
    pages = ["ANNUAL INFORMATION FORM\nOF GREAT PACIFIC GOLD CORP.",
             "TABLE OF CONTENTS\nCORPORATE STRUCTURE ... 2",
             "DESCRIPTION OF THE BUSINESS", "RISK FACTORS", "DIRECTORS AND OFFICERS"]
    k = dc.classify_from_pages(pages)
    assert k.doc_type == dc.AIF and not k.is_primary


def test_mda_rejected():
    pages = ["Management's Discussion and Analysis\nFor the year ended",
             "Results of operations\nRevenue increased...",
             "Liquidity and capital resources"]
    assert dc.classify_from_pages(pages).doc_type == dc.MDA


def test_interim_rejected():
    pages = ["Condensed Interim Financial Statements (Unaudited)\nThree months ended March 31, 2025",
             "some interim text"]
    assert dc.classify_from_pages(pages).doc_type == dc.INTERIM


def test_two_of_three_plus_auditor_is_primary():
    # a real statements block where the 3rd header evades top-3-lines matching,
    # but the auditor anchor + two statements makes it safe.
    pages = [
        "Independent Auditor's Report\nWe have audited...",
        _rows("Statements of Financial Position"),
        _rows("Statements of Loss and Comprehensive Loss"),
        "Notes to the Financial Statements",
    ]
    assert dc.classify_from_pages(pages).doc_type == dc.PRIMARY


def test_french_statements_primary():
    pages = [
        "Rapport de l'auditeur indépendant\nNous avons effectué l'audit",
        _rows("État de la situation financière"),
        _rows("État du résultat global"),
        _rows("Tableau des flux de trésorerie"),
        "Notes afférentes aux états financiers",
    ]
    assert dc.classify_from_pages(pages).doc_type == dc.PRIMARY


def test_empty_pages_unparseable():
    assert dc.classify_from_pages([]).doc_type == dc.UNPARSEABLE
    assert dc.classify_document(b"<html>not pdf</html>").doc_type == dc.UNPARSEABLE


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    _run()
