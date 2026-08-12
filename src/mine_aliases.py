#!/usr/bin/env python3
"""Corpus-wide alias miner -- surface the raw statement labels we EXTRACT but never
map to a canonical key, so a whole metric zero-fills even though the number is on
the filing face. This is the engine that scales the manual capex discovery
(gurufocus_compare found "net acquisitions of premises and equipment" -> PurchaseOfPPE)
into a repeatable pass over all ~2,459 companies.

    python src/mine_aliases.py                          # corpus-wide, free
    python src/mine_aliases.py --min-tickers 8          # only widely-recurring labels
    python src/mine_aliases.py --gf-csv output/gurufocus_compare.csv   # + GF cross-confirm

Output: output/alias_candidates.csv, ranked by how many distinct companies share the
unmapped label. Each row proposes a canonical key two ways:
  - fuzzy: the label's closest match to an EXISTING alias/humanized key in that
    statement's vocab, scored below the pipeline's 0.90 auto-map cutoff (so these are
    genuinely un-auto-mappable and need a curated alias). For REVIEW, not auto-add.
  - gf_confirmed: where a cached GuruFocus value for the same ticker/year/statement
    equals this line's value, the GF field's canonical key -- high confidence.

The miner NEVER edits code. A human vets the list, then adds the good ones via
rule_extract._extend_aliases({...}) and re-runs `run.py --step 4 --force`.

Read-only against output/pdf_financials.db (line_items_full).
"""
from __future__ import annotations

import argparse
import csv
import difflib
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rule_extract import (  # noqa: E402  reuse the exact normalization + alias index
    _normalize_label, build_alias_index, CURATED_ALIASES, STATEMENTS,
)
from llm_extract import _load_vocab, DEFAULT_VOCAB  # noqa: E402

DEFAULT_DB = ROOT / "output" / "pdf_financials.db"
MILLIONS = 1_000_000.0

# A label is worth proposing only if it looks like a real statement concept, not a
# heading / value-glued fragment / bare number.
_STOP = {"total", "other", "note", "notes", "current", "december", "january",
         "balance", "cash", "deficit", "basic", "diluted", "net", "and", "of", "the",
         "for", "year", "period", "as at", "continuing operations"}


def _looks_mineable(norm: str) -> bool:
    if not norm or len(norm) < 5:
        return False
    words = norm.split()
    if len(words) > 9:
        return False
    if norm in _STOP:
        return False
    # must contain at least one "contentful" word beyond stopwords
    return any(w not in _STOP for w in words)


def _best_fuzzy(norm: str, alias_map: dict[str, str]) -> tuple[str | None, float]:
    """Closest existing alias in this statement -> (key, ratio). Lower threshold than
    the pipeline's 0.90 (these are, by construction, below it) so we can SUGGEST."""
    best_key, best_r = None, 0.0
    for alias, key in alias_map.items():
        r = difflib.SequenceMatcher(None, norm, alias).ratio()
        if r > best_r:
            best_key, best_r = key, r
    return best_key, best_r


def load_unmapped(conn: sqlite3.Connection) -> dict:
    """{(statement_type, normalized_label): {n_tickers, n_lines, example, section,
    tickers:set}} over the NEWEST source_pdf_year per cell (avoids counting the same
    line duplicated across filings)."""
    # newest source_pdf_year per (ticker, fiscal_year, statement, line_no) so a value
    # present in several filings is counted once.
    rows = conn.execute("""
        SELECT lf.ticker, lf.statement_type, lf.raw_label, lf.section, lf.value
        FROM line_items_full lf
        JOIN (SELECT ticker, fiscal_year, statement_type, line_no,
                     MAX(source_pdf_year) AS mx
              FROM line_items_full
              WHERE canonical_key IS NULL AND value IS NOT NULL
              GROUP BY ticker, fiscal_year, statement_type, line_no) newest
          ON lf.ticker=newest.ticker AND lf.fiscal_year=newest.fiscal_year
         AND lf.statement_type=newest.statement_type AND lf.line_no=newest.line_no
         AND lf.source_pdf_year=newest.mx
        WHERE lf.canonical_key IS NULL AND lf.value IS NOT NULL AND lf.raw_label IS NOT NULL
    """)
    agg: dict = {}
    for r in rows:
        norm = _normalize_label(r[2])
        if not _looks_mineable(norm):
            continue
        key = (r[1], norm)
        d = agg.setdefault(key, {"tickers": set(), "n_lines": 0,
                                 "example": r[2], "section": r[3] or ""})
        d["tickers"].add(r[0])
        d["n_lines"] += 1
    return agg


def load_gf_confirmations(gf_csv: Path) -> dict:
    """From a gurufocus_compare.csv: {(statement_type, normalized_pdf_or_gf_label):
    canonical_key} for zero-fill-gap rows where GuruFocus supplies the value. Because
    a zero-fill row carries no pdf_raw_label, we instead key GF confirmation by the
    (statement, canonical_key) the GF field wants, and confirm a mined label if its
    value matches -- handled by the caller. Here we just return, per statement, the
    set of canonical keys GuruFocus reports a value for (the fillable targets)."""
    wanted: dict = defaultdict(set)
    if not gf_csv.exists():
        return wanted
    for r in csv.DictReader(gf_csv.open(encoding="utf-8")):
        if r.get("verdict") == "ours_zerofill_gap" and r.get("gurufocus_value") not in ("", "0.0", "0"):
            wanted[r["statement_type"]].add(r["canonical_key"])
    return wanted


# ---------------------------------------------------------- value-confirmed mining
_VC_TOL = 0.02          # value must match GF within 2%
_VC_MIN_ABS = 0.02      # ignore GF values below this (in millions) -- rounding dust


def value_confirmed_mine(conn: sqlite3.Connection, gf_csv: Path) -> tuple[list[dict], int]:
    """The GuruFocus-value-confirmed miner: for every cell where GF has a real value
    and we zero-filled (ours_zerofill_gap / gf_only) OR our derived number disagrees
    (ours_derived_diff), find an UNMAPPED raw line in the same (ticker, year,
    statement) whose value equals GF's -- that line IS the metric, and its label is a
    missing alias. Three matchers per cell:
      single : one unmapped line == GF value
      sumlab : all same-normalized-label unmapped siblings sum to GF value
      residual: (GF value - our current value) == one unmapped line (a sibling
                missing from an AGGREGATE key's sum, e.g. warrants -> APIC)
    FX-aware: applies the compare run's per-row fx_factor before matching. Returns
    (proposals ranked by distinct-company confirmations, n_cells_scanned)."""
    if not gf_csv.exists():
        return [], 0
    targets = [r for r in csv.DictReader(gf_csv.open(encoding="utf-8"))
               if r.get("verdict") in ("ours_zerofill_gap", "gf_only", "ours_derived_diff")
               and r.get("gurufocus_value") not in ("", None)]
    # preload every ticker's unmapped lines once: (ticker, fy, stmt) -> [(norm, value)]
    # newest source_pdf_year per (fy, stmt, line_no) to avoid multi-filing dupes.
    tickers = {r["ticker"] for r in targets}
    unmapped: dict = defaultdict(list)
    for tk in tickers:
        for r in conn.execute("""
            SELECT lf.fiscal_year, lf.statement_type, lf.raw_label, lf.value
            FROM line_items_full lf
            JOIN (SELECT fiscal_year, statement_type, line_no, MAX(source_pdf_year) mx
                  FROM line_items_full WHERE ticker=? AND canonical_key IS NULL
                        AND value IS NOT NULL
                  GROUP BY fiscal_year, statement_type, line_no) nw
              ON lf.fiscal_year=nw.fiscal_year AND lf.statement_type=nw.statement_type
             AND lf.line_no=nw.line_no AND lf.source_pdf_year=nw.mx
            WHERE lf.ticker=? AND lf.canonical_key IS NULL AND lf.value IS NOT NULL
        """, (tk, tk)):
            norm = _normalize_label(r[2] or "")
            if _looks_mineable(norm):
                unmapped[(tk, r[0], r[1])].append((norm, r[3]))

    props: dict = {}   # (stmt, norm, key) -> {tickers:set, contradict:set, how:Counter}
    n_cells = 0
    for r in targets:
        try:
            gf_val = float(r["gurufocus_value"])
        except (TypeError, ValueError):
            continue
        if abs(gf_val) < _VC_MIN_ABS:
            continue
        fx = float(r["fx_factor"]) if r.get("fx_factor") else 1.0
        unit = r.get("unit", "money")
        scale = MILLIONS if unit in ("money", "shares") else 1.0
        want = abs(gf_val * (fx if unit in ("money", "pershare") else 1.0)) * scale
        key = r["canonical_key"]; stmt = r["statement_type"]
        tk = r["ticker"]; fy = int(r["fiscal_year"])
        lines = unmapped.get((tk, fy, stmt))
        if not lines:
            continue
        n_cells += 1
        ours = None
        if r.get("verdict") == "ours_derived_diff" and r.get("pipeline_value"):
            try:
                ours = abs(float(r["pipeline_value"])) * scale
            except ValueError:
                ours = None

        def hit(norm, how):
            p = props.setdefault((stmt, norm, key),
                                 {"tickers": set(), "how": Counter()})
            p["tickers"].add(tk)
            p["how"][how] += 1

        # single-line + residual matchers
        by_label: dict = defaultdict(float)
        for norm, val in lines:
            by_label[norm] += val
            if abs(abs(val) - want) <= _VC_TOL * want:
                hit(norm, "single")
            if ours is not None and want > ours:      # residual: missing sibling
                resid = want - ours
                if resid > _VC_MIN_ABS * MILLIONS and \
                        abs(abs(val) - resid) <= _VC_TOL * resid:
                    hit(norm, "residual")
        # same-label sibling-sum matcher (one concept split over several lines)
        for norm, total in by_label.items():
            if abs(abs(total) - want) <= _VC_TOL * want:
                p = props.get((stmt, norm, key))
                if not p or "single" not in p["how"]:
                    hit(norm, "sumlab")

    # contradiction detection: same (stmt, norm) proposing DIFFERENT keys
    by_label_key: dict = defaultdict(set)
    for (stmt, norm, key) in props:
        by_label_key[(stmt, norm)].add(key)
    out = []
    for (stmt, norm, key), p in props.items():
        rivals = by_label_key[(stmt, norm)] - {key}
        out.append({
            "statement_type": stmt, "normalized_label": norm,
            "proposed_key": key,
            "n_confirmed_tickers": len(p["tickers"]),
            "how": "+".join(f"{k}:{v}" for k, v in p["how"].most_common()),
            "conflicting_keys": ";".join(sorted(rivals)) if rivals else "",
            "example_tickers": ",".join(sorted(p["tickers"])[:5]),
        })
    out.sort(key=lambda d: (-d["n_confirmed_tickers"], d["conflicting_keys"] != ""))
    return out, n_cells


def main() -> None:
    ap = argparse.ArgumentParser(description="Mine unmapped statement labels -> alias candidates.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--gf-csv", type=Path, default=ROOT / "output" / "gurufocus_compare.csv",
                    help="gurufocus_compare.csv to mark which canonical keys GuruFocus "
                         "confirms are fillable (raises a candidate's priority).")
    ap.add_argument("--min-tickers", type=int, default=5,
                    help="Only report labels shared by >= this many companies (default 5).")
    ap.add_argument("--fuzzy-floor", type=float, default=0.6,
                    help="Min fuzzy ratio to a known alias to bother suggesting a key.")
    ap.add_argument("--out", type=Path, default=ROOT / "output" / "alias_candidates.csv")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr); sys.exit(1)

    vocab = _load_vocab(DEFAULT_VOCAB)
    alias_index = build_alias_index(vocab)  # {stmt: {norm_alias: key}}
    # normalized aliases already known per statement -- if an unmapped label IS one of
    # these yet still unmapped, it's a gating/section bug (different class), so skip it
    # from the missing-alias list and count it separately.
    known = {s: set(alias_index.get(s, {})) for s in STATEMENTS}

    conn = sqlite3.connect(args.db)

    # ---- VALUE-CONFIRMED pass (primary when a compare CSV exists) ----------------
    vc_rows, vc_cells = value_confirmed_mine(conn, args.gf_csv)
    if vc_rows:
        vc_out = args.out.with_name("alias_candidates_value_confirmed.csv")
        with vc_out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(vc_rows[0].keys()))
            w.writeheader()
            for r in vc_rows:
                w.writerow(r)
        print(f"VALUE-CONFIRMED: scanned {vc_cells} GF-backed gap cells -> "
              f"{len(vc_rows)} (label,key) proposals -> {vc_out}")
        print(f"  {'stmt':<16} {'#co':>4} {'label':<40} {'-> key':<30} how / conflicts")
        for r in vc_rows[:25]:
            conf = f"  CONFLICT:{r['conflicting_keys']}" if r["conflicting_keys"] else ""
            print(f"  {r['statement_type']:<16} {r['n_confirmed_tickers']:>4} "
                  f"{r['normalized_label'][:40]:<40} {r['proposed_key'][:30]:<30} "
                  f"{r['how']}{conf}")
        print()

    print("scanning unmapped raw lines corpus-wide ...")
    agg = load_unmapped(conn)
    conn.close()
    gf_targets = load_gf_confirmations(args.gf_csv)

    rows = []
    gating_bugs = 0
    for (stmt, norm), d in agg.items():
        n_tk = len(d["tickers"])
        if n_tk < args.min_tickers:
            continue
        if norm in known.get(stmt, set()):
            gating_bugs += 1     # alias exists but line still unmapped -> gating/section
            continue
        sug_key, ratio = _best_fuzzy(norm, alias_index.get(stmt, {}))
        if ratio < args.fuzzy_floor:
            sug_key = ""         # no confident suggestion; needs GF/human
        gf_conf = "yes" if (sug_key and sug_key in gf_targets.get(stmt, set())) else ""
        rows.append({
            "statement_type": stmt,
            "normalized_label": norm,
            "n_tickers": n_tk,
            "n_lines": d["n_lines"],
            "example_raw_label": d["example"],
            "section": d["section"][:40],
            "suggested_key": sug_key or "",
            "fuzzy_ratio": round(ratio, 3),
            "gf_confirms_key_fillable": gf_conf,
        })

    # rank: GF-confirmed first, then by breadth (distinct companies), then fuzzy conf
    rows.sort(key=lambda r: (r["gf_confirms_key_fillable"] != "yes",
                             -r["n_tickers"], -r["fuzzy_ratio"]))
    cols = ["statement_type", "normalized_label", "n_tickers", "n_lines",
            "example_raw_label", "section", "suggested_key", "fuzzy_ratio",
            "gf_confirms_key_fillable"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"\n{len(rows)} candidate labels (>= {args.min_tickers} companies), "
          f"{gating_bugs} already-aliased-but-unmapped (gating/section bugs, separate class)")
    print(f"written: {args.out}\n")
    print("Top 30 missing-alias candidates (breadth-ranked):")
    print(f"  {'stmt':<16} {'#co':>4} {'label':<44} {'->suggest':<26} fuzzy gf")
    for r in rows[:30]:
        print(f"  {r['statement_type']:<16} {r['n_tickers']:>4} "
              f"{r['normalized_label'][:44]:<44} {r['suggested_key'][:26]:<26} "
              f"{r['fuzzy_ratio']:>4} {r['gf_confirms_key_fillable']}")


if __name__ == "__main__":
    main()
