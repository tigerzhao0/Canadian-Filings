"""One-off generator for data/canonical_lineitems.json — the controlled
vocabulary handed to the step-3 LLM (see llm_extract.py).

The canonical line-item names are exactly the QuoteMedia field names already
present in the existing exports (output/companies/*.json). We take the most
common keys per statement so the LLM maps CSE-PDF lines onto the SAME vocabulary
QuoteMedia companies use, keeping the two sources query/dashboard-compatible.

    python src/build_vocab.py            # writes data/canonical_lineitems.json
    python src/build_vocab.py --top 60   # keep only the top-60 keys per statement

Re-run whenever the QuoteMedia export vocabulary grows.

Default is 250 (effectively FULL coverage -- the real universe across all
companies is only ~130-210 keys per statement). A low --top keeps only the keys
most common ACROSS THE WHOLE universe, which silently drops industry-specific
line items (bank interest income/expense, insurance premiums, etc.) that are
common within their own industry but rare overall -- the matcher then has no
canonical target for them.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPANIES_DIR = ROOT / "output" / "companies"
OUT_PATH = ROOT / "data" / "canonical_lineitems.json"

# JSON export key (camelCase) -> statement_type used everywhere else.
_STMT_KEYS = {
    "incomeStatement": "income_statement",
    "balanceSheet": "balance_sheet",
    "cashFlow": "cash_flow",
}


def build_vocab(top: int) -> dict[str, list[str]]:
    counters: dict[str, Counter] = {v: Counter() for v in _STMT_KEYS.values()}
    files = sorted(COMPANIES_DIR.glob("*.json"))
    for fp in files:
        try:
            doc = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a malformed export shouldn't stop the build
            continue
        for year in doc.get("financials", []) or []:
            for json_key, stmt in _STMT_KEYS.items():
                for line_item in (year.get(json_key) or {}):
                    counters[stmt][line_item] += 1
    return {
        stmt: [k for k, _ in counter.most_common(top)]
        for stmt, counter in counters.items()
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=250,
                    help="Keep the N most-common keys per statement (default 250 -- "
                         "effectively full coverage; a small N silently drops "
                         "industry-specific line items, see module docstring).")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    if not COMPANIES_DIR.exists():
        raise SystemExit(f"No exports found at {COMPANIES_DIR} — run the "
                         "financials stage + company_export.py --all first.")

    vocab = build_vocab(args.top)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(vocab, indent=2), encoding="utf-8")
    counts = ", ".join(f"{s}={len(v)}" for s, v in vocab.items())
    print(f"Wrote {args.out}  ({counts})")


if __name__ == "__main__":
    main()
