"""Local control-panel web app (Flask) — a friendly front end to run.py.

`gui.py` (repo root) launches this. It:
- Shells out to `python run.py ...` and the export scripts as SUBPROCESSES and
  streams their stdout live to the browser over Server-Sent Events, so it
  literally "acts as run.py" (exact CLI parity, isolation, killability) rather
  than reimplementing any stage.
- Imports the fast READ-ONLY helpers (company_export.build_document, sqlite
  queries) directly for the stats bar, company browser, and diagnostics.

Safety: localhost-only, one job at a time, and every action maps to a fixed
argv template with shell=False -- user-supplied fields are appended as separate,
validated argv items, never interpolated into a shell string.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import (Flask, Response, jsonify, request,
                   send_from_directory, stream_with_context)

import company_export

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"
USER_CONFIG = REPO_ROOT / "config.yaml"
DEFAULT_INPUT = REPO_ROOT / "data" / "Canadian Companies.xlsx"

_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


def _config_path() -> Path:
    """Prefer config.yaml if the user has created one, else the checked-in
    example (matches run.py's DEFAULT_CONFIG behaviour)."""
    return USER_CONFIG if USER_CONFIG.exists() else EXAMPLE_CONFIG


def _load_config() -> dict:
    with _config_path().open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------- #
#  Job manager: one active subprocess, stdout tailed by a daemon thread.
# --------------------------------------------------------------------------- #
class Busy(Exception):
    pass


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.label: str | None = None
        self.action: str | None = None
        self.started_at: str | None = None
        self.lines: list[str] = []
        self.returncode: int | None = None
        self.finished: bool = True

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, argv: list[str], label: str, action: str | None = None) -> None:
        with self._lock:
            if self.is_running():
                raise Busy(self.label or "a job")
            self.proc = subprocess.Popen(
                argv, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
            self.label = label
            self.action = action
            self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.lines = [f"$ {' '.join(argv)}", ""]
            self.returncode = None
            self.finished = False
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()

    def _drain(self, proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                self.lines.append(line.rstrip("\n"))
        finally:
            proc.stdout.close()
            self.returncode = proc.wait()
            self.lines.append("")
            self.lines.append(f"[process exited with code {self.returncode}]")
            self.finished = True

    def stop(self) -> bool:
        with self._lock:
            if not self.is_running():
                return False
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return True


JOB = JobManager()


# --------------------------------------------------------------------------- #
#  Action allow-list: name -> builder(options) -> argv (python + script + args)
# --------------------------------------------------------------------------- #
def _mode_flags(opts: dict) -> list[str]:
    mode = (opts.get("mode") or "pilot").lower()
    if mode == "full":
        return ["--full"]
    if mode == "resume":
        return ["--resume"]
    flags = ["--pilot"]
    if opts.get("sample_size"):
        flags += ["--sample-size", str(int(opts["sample_size"]))]
    return flags


def _input_arg(opts: dict) -> list[str]:
    raw = (opts.get("input") or "").strip() or str(DEFAULT_INPUT)
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / raw
    if not p.exists():
        raise ValueError(f"Input file not found: {p}")
    return ["--input", str(p)]


def _clean_tickers(raw: str) -> list[str]:
    out = [t.strip().upper() for t in (raw or "").split(",") if t.strip()]
    bad = [t for t in out if not _TICKER_RE.match(t)]
    if bad:
        raise ValueError(f"Invalid ticker(s): {', '.join(bad)}")
    return out


def _build_argv(action: str, opts: dict, cfg_path: Path) -> tuple[list[str], str]:
    py = sys.executable
    run = str(REPO_ROOT / "run.py")
    cfg = ["--config", str(cfg_path)]

    if action == "financials":
        return ([py, run, "--financials", *_input_arg(opts), *_mode_flags(opts), *cfg],
                "Structured financials (QuoteMedia)")
    if action == "pipeline":
        argv = [py, run, "--step", "1", *_input_arg(opts), *_mode_flags(opts), *cfg]
        if opts.get("no_render"):
            argv.append("--no-render")
        return argv, "Full pipeline (step 1: financials + PDF finder)"
    if action == "process_pdfs":
        return ([py, run, "--step", "2", *cfg], "Process PDFs (step 2)")
    if action == "llm_extract":
        argv = [py, run, "--step", "3", *cfg]
        if opts.get("force"):
            argv.append("--force")
        if opts.get("limit"):
            argv += ["--limit", str(int(opts["limit"]))]
        tickers = _clean_tickers(opts.get("tickers", ""))
        if tickers:
            argv += ["--tickers", ",".join(tickers)]
        return argv, "LLM extraction (step 3)"
    if action == "dashboard":
        return ([py, str(REPO_ROOT / "src" / "build_dashboard.py")], "Build HTML dashboard")
    if action == "export_json":
        return ([py, str(REPO_ROOT / "src" / "company_export.py"), "--all"],
                "Export all company JSON")
    if action == "export_xlsx":
        return ([py, str(REPO_ROOT / "src" / "export_financials.py"), "--summary"],
                "Export Excel summary")
    raise ValueError(f"Unknown action: {action}")


# --------------------------------------------------------------------------- #
#  Read-only DB helpers
# --------------------------------------------------------------------------- #
def _connect(db_path: str) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.is_absolute():
        p = REPO_ROOT / db_path
    if not p.exists():
        return None
    return sqlite3.connect(str(p))


def _scalar(conn, sql, params=()):
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


# Human-readable labels for the raw failure_reason / status codes the pipeline
# stores, used to group the Error Center.
_REASON_LABELS = {
    "no_corporate_domain": "No corporate domain found",
    "no_pdf_after_render": "No PDF after render",
    "stale_annual_report": "Stale annual report",
    "scanned_no_ocr": "Scanned PDF (OCR needed)",
    "download_failed": "PDF download failed",
    "unparseable": "PDF unparseable",
    "not_a_pdf": "Not a PDF",
    "no_primary_statements_found": "No primary statements found",
    "invalid_json": "LLM invalid JSON",
    "no_columns": "No fiscal-year columns",
    "llm_error": "LLM call error",
    "empty": "Empty section",
    "search_rate_limited": "Search rate-limited",
    "search_error": "Search error",
}


def _pretty_reason(code: str | None) -> str:
    if not code:
        return "Unknown"
    return _REASON_LABELS.get(code, code.replace("_", " ").capitalize())


def _fmt_val(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{'-' if v < 0 else ''}{a/div:.2f}{suf}"
    return f"{v:,.2f}"


def create_app() -> Flask:
    app = Flask(__name__)
    cfg = _load_config()
    fin_db = cfg.get("tmx_financials", {}).get("db_path", "output/financials.db")
    fil_db = cfg.get("storage", {}).get("db_path", "output/filings.db")
    output_dir = REPO_ROOT / "output"

    # ----- page --------------------------------------------------------- #
    @app.get("/")
    def index():
        return Response(PAGE, mimetype="text/html")

    # ----- run / stream / stop / status --------------------------------- #
    @app.post("/api/run")
    def api_run():
        body = request.get_json(force=True, silent=True) or {}
        action = body.get("action", "")
        opts = body.get("options", {}) or {}
        try:
            argv, label = _build_argv(action, opts, _config_path())
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        try:
            JOB.start(argv, label, action=action)
        except Busy as exc:
            return jsonify(busy=True, label=str(exc)), 409
        return jsonify(started=True, label=label)

    @app.get("/api/stream")
    def api_stream():
        @stream_with_context
        def gen():
            idx = 0
            while True:
                buf = JOB.lines
                while idx < len(buf):
                    yield f"data: {json.dumps(buf[idx])}\n\n"
                    idx += 1
                if JOB.finished and idx >= len(JOB.lines):
                    yield f"event: done\ndata: {json.dumps(JOB.returncode)}\n\n"
                    return
                time.sleep(0.25)
        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/stop")
    def api_stop():
        return jsonify(stopped=JOB.stop())

    @app.get("/api/status")
    def api_status():
        running = JOB.is_running()
        # Map the running action to the pipeline stage(s) it touches, so the DAG
        # can pulse the active node(s).
        stage_map = {"financials": ["financials"], "pipeline": ["financials", "discovery"],
                     "process_pdfs": ["extract"], "llm_extract": ["normalize"]}
        return jsonify(running=running, label=JOB.label, action=JOB.action,
                       activeStages=(stage_map.get(JOB.action or "", []) if running else []),
                       started_at=JOB.started_at, finished=JOB.finished,
                       returncode=JOB.returncode)

    # ----- stats -------------------------------------------------------- #
    @app.get("/api/stats")
    def api_stats():
        out = {"bySource": {}, "companies": 0, "statementLines": 0,
               "companyYears": 0, "pdfExtractionsUsable": 0,
               "step3": {"ok": 0, "invalid_json": 0, "empty": 0, "no_columns": 0,
                         "llm_error": 0},
               "consistencyWarnings": 0}
        conn = _connect(fin_db)
        if conn:
            try:
                out["bySource"] = dict(_rows(conn,
                    "SELECT primary_source, COUNT(*) FROM companies GROUP BY primary_source"))
                out["companies"] = _scalar(conn, "SELECT COUNT(*) FROM companies")
                out["statementLines"] = _scalar(conn, "SELECT COUNT(*) FROM statement_lines")
                out["companyYears"] = _scalar(conn, "SELECT COUNT(*) FROM company_years")
                out["step3"] = {"ok": 0, "invalid_json": 0, "empty": 0,
                                "no_columns": 0, "llm_error": 0}
                for status, n in _rows(conn,
                        "SELECT status, COUNT(*) FROM pdf_llm_status GROUP BY status"):
                    out["step3"][status] = n
                out["consistencyWarnings"] = _scalar(conn,
                    "SELECT COUNT(*) FROM pdf_llm_consistency")
            finally:
                conn.close()
        conn2 = _connect(fil_db)
        if conn2:
            try:
                out["pdfExtractionsUsable"] = _scalar(conn2,
                    "SELECT COUNT(*) FROM pdf_extractions WHERE extract_ok=1 AND scanned=0")
            finally:
                conn2.close()
        return jsonify(out)

    # ----- company browser --------------------------------------------- #
    @app.get("/api/companies")
    def api_companies():
        source = request.args.get("source", "all")
        q = request.args.get("q", "").strip().lower()
        limit = min(int(request.args.get("limit", 500)), 5000)
        conn = _connect(fin_db)
        if not conn:
            return jsonify(companies=[])
        try:
            sql = ("SELECT c.ticker, c.company_name, c.exchange, c.primary_source, "
                   "MAX(cy.fiscal_year) "
                   "FROM companies c LEFT JOIN company_years cy ON cy.ticker=c.ticker ")
            where, params = [], []
            if source and source != "all":
                where.append("c.primary_source = ?")
                params.append(source)
            if q:
                where.append("(LOWER(c.ticker) LIKE ? OR LOWER(c.company_name) LIKE ?)")
                params += [f"%{q}%", f"%{q}%"]
            if where:
                sql += "WHERE " + " AND ".join(where) + " "
            sql += "GROUP BY c.ticker ORDER BY c.ticker LIMIT ?"
            params.append(limit)
            rows = _rows(conn, sql, params)
        finally:
            conn.close()
        return jsonify(companies=[
            {"ticker": t, "name": n or "", "exchange": e or "",
             "source": s or "", "latestYear": y} for t, n, e, s, y in rows])

    @app.get("/api/company/<ticker>")
    def api_company(ticker):
        conn = _connect(fin_db)
        if not conn:
            return jsonify(error="financials.db not found"), 404
        try:
            doc = company_export.build_document(conn, ticker)
        finally:
            conn.close()
        if doc is None:
            return jsonify(error=f"{ticker} not in companies table"), 404
        schema_path = REPO_ROOT / "sql" / "company_schema.json"
        err = company_export._validate(doc, schema_path)
        return jsonify(document=doc, schemaValid=(err is None), schemaError=err)

    @app.get("/api/diagnostics/<ticker>")
    def api_diagnostics(ticker):
        conn = _connect(fin_db)
        if not conn:
            return jsonify(status=[], consistency=[])
        try:
            status = [
                {"fiscalYear": fy, "statementType": st, "status": s,
                 "nLines": n, "reason": r}
                for fy, st, s, n, r in _rows(conn,
                    "SELECT fiscal_year, statement_type, status, n_lines, reason "
                    "FROM pdf_llm_status WHERE ticker=? ORDER BY fiscal_year DESC, statement_type",
                    (ticker,))]
            consistency = [
                {"conceptGroup": g, "pattern": p, "keysUsed": k}
                for g, p, k in _rows(conn,
                    "SELECT concept_group, pattern, keys_used FROM pdf_llm_consistency "
                    "WHERE ticker=?", (ticker,))]
        finally:
            conn.close()
        return jsonify(status=status, consistency=consistency)

    # ----- config get / save ------------------------------------------- #
    EDITABLE = {
        "llm.model": str, "llm.concurrency": int, "llm.combine_statements": bool,
        "llm.temperature": float, "pilot.default_sample_size": int,
    }

    @app.get("/api/config")
    def api_config_get():
        c = _load_config()
        def get(path, default=None):
            cur = c
            for part in path.split("."):
                cur = (cur or {}).get(part) if isinstance(cur, dict) else None
            return cur if cur is not None else default
        return jsonify(
            path=str(_config_path().name),
            editable={k: get(k) for k in EDITABLE},
            readonly={
                "financials_db": get("tmx_financials.db_path", "output/financials.db"),
                "filings_db": get("storage.db_path", "output/filings.db"),
                "default_input": str(DEFAULT_INPUT),
            })

    @app.post("/api/config")
    def api_config_save():
        body = request.get_json(force=True, silent=True) or {}
        # Start from config.yaml if present, else seed from the example so we
        # never lose the other keys.
        base = {}
        if USER_CONFIG.exists():
            base = yaml.safe_load(USER_CONFIG.read_text(encoding="utf-8")) or {}
        elif EXAMPLE_CONFIG.exists():
            base = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")) or {}
        for key, caster in EDITABLE.items():
            if key not in body:
                continue
            val = body[key]
            try:
                val = caster(val) if not isinstance(val, bool) or caster is bool else val
                if caster is bool:
                    val = bool(val)
                elif caster is int:
                    val = int(val)
                elif caster is float:
                    val = float(val)
            except (TypeError, ValueError):
                return jsonify(error=f"Bad value for {key}: {body[key]!r}"), 400
            section, leaf = key.split(".")
            base.setdefault(section, {})[leaf] = val
        USER_CONFIG.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
        return jsonify(saved=True, path=USER_CONFIG.name)

    # ===== OPS: pipeline DAG ============================================== #
    @app.get("/api/pipeline")
    def api_pipeline():
        fin = _connect(fin_db)
        fil = _connect(fil_db)
        try:
            tmx = dict(_rows(fin, "SELECT status, COUNT(*) FROM tmx_financials_status "
                                  "GROUP BY status")) if fin else {}
            universe = sum(tmx.values()) or _scalar(fin, "SELECT COUNT(*) FROM companies") if fin else 0
            companies_ct = _scalar(fin, "SELECT COUNT(*) FROM companies") if fin else 0
            fin_ok = tmx.get("ok", 0)
            fin_fail = tmx.get("no_data", 0)
            fin_excl = tmx.get("capital_pool_company", 0) + tmx.get("trust_or_fund", 0)

            filst = dict(_rows(fil, "SELECT status, COUNT(*) FROM filings GROUP BY status")) if fil else {}
            ext = {(o, s): n for o, s, n in _rows(
                fil, "SELECT extract_ok, scanned, COUNT(*) FROM pdf_extractions "
                     "GROUP BY extract_ok, scanned")} if fil else {}
            ext_ok = sum(n for (o, s), n in ext.items() if o == 1)
            ext_fail = sum(n for (o, s), n in ext.items() if o == 0)

            llm = dict(_rows(fin, "SELECT status, COUNT(*) FROM pdf_llm_status GROUP BY status")) if fin else {}
            llm_ok = llm.get("ok", 0)
            llm_fail = llm.get("invalid_json", 0) + llm.get("no_columns", 0) + llm.get("llm_error", 0)
            llm_empty = llm.get("empty", 0)
            drops = _scalar(fin, "SELECT COUNT(*) FROM pdf_llm_status "
                                 "WHERE reason LIKE '%text-verification%'") if fin else 0
            consistency = _scalar(fin, "SELECT COUNT(*) FROM pdf_llm_consistency") if fin else 0

            def stage(sid, label, desc, success=0, failed=0, review=0, remaining=0,
                      total=None, breakdown=None):
                tot = total if total is not None else (success + failed + review + remaining)
                return {"id": sid, "label": label, "desc": desc, "success": success,
                        "failed": failed, "review": review, "remaining": remaining,
                        "total": tot, "breakdown": breakdown or []}

            stages = [
                stage("list", "Company List", "Universe of tickers attempted",
                      success=universe, total=universe,
                      breakdown=[{"label": "companies", "count": universe, "tone": "ok"}]),
                stage("financials", "Structured Financials", "QuoteMedia exchange APIs",
                      success=fin_ok, failed=fin_fail, review=0,
                      remaining=fin_excl, total=universe,
                      breakdown=[{"label": "resolved", "count": fin_ok, "tone": "ok"},
                                 {"label": "no data", "count": fin_fail, "tone": "err"},
                                 {"label": "CPC/trust (excluded)", "count": fin_excl, "tone": "muted"}]),
                stage("discovery", "Filing Discovery", "Find annual-report PDFs for the tail",
                      success=filst.get("found", 0), review=filst.get("needs_review", 0),
                      failed=filst.get("not_found", 0),
                      breakdown=[{"label": "found", "count": filst.get("found", 0), "tone": "ok"},
                                 {"label": "needs review", "count": filst.get("needs_review", 0), "tone": "warn"},
                                 {"label": "not found", "count": filst.get("not_found", 0), "tone": "err"}]),
                stage("extract", "Download & Extract", "PDF text/table extraction (step 2)",
                      success=ext_ok, failed=ext_fail,
                      breakdown=[{"label": "extracted", "count": ext_ok, "tone": "ok"},
                                 {"label": "scanned / failed", "count": ext_fail, "tone": "err"}]),
                stage("normalize", "LLM Normalize", "Local LLM maps text to schema (step 3)",
                      success=llm_ok, failed=llm_fail, review=llm_empty,
                      breakdown=[{"label": "parsed ok", "count": llm_ok, "tone": "ok"},
                                 {"label": "invalid / error", "count": llm_fail, "tone": "err"},
                                 {"label": "empty section", "count": llm_empty, "tone": "warn"}]),
                stage("validate", "Validate", "Schema + hallucination guard + consistency",
                      success=max(llm_ok - drops, 0), review=drops + consistency,
                      total=llm_ok,
                      breakdown=[{"label": "clean", "count": max(llm_ok - drops, 0), "tone": "ok"},
                                 {"label": "values dropped (unverified)", "count": drops, "tone": "warn"},
                                 {"label": "consistency flags", "count": consistency, "tone": "warn"}]),
                stage("approved", "Approved Data", "Companies with structured financials",
                      success=companies_ct, total=companies_ct,
                      breakdown=[{"label": "companies with data", "count": companies_ct, "tone": "ok"}]),
            ]
            return jsonify(stages=stages)
        finally:
            if fin:
                fin.close()
            if fil:
                fil.close()

    # ===== OPS: coverage heatmap ========================================= #
    @app.get("/api/coverage/filters")
    def api_coverage_filters():
        fin = _connect(fin_db)
        if not fin:
            return jsonify(exchanges=[], sources=[], statuses=[], years=[])
        try:
            exchanges = [r[0] for r in _rows(fin, "SELECT DISTINCT exchange FROM tmx_financials_status "
                                                  "WHERE exchange IS NOT NULL ORDER BY exchange")]
            sources = [r[0] for r in _rows(fin, "SELECT DISTINCT primary_source FROM companies "
                                                "WHERE primary_source IS NOT NULL ORDER BY primary_source")]
            years = [r[0] for r in _rows(fin, "SELECT DISTINCT fiscal_year FROM company_years "
                                              "ORDER BY fiscal_year DESC")]
            return jsonify(exchanges=exchanges, sources=sources,
                           statuses=["ok", "no_data", "capital_pool_company", "trust_or_fund"],
                           years=years)
        finally:
            fin.close()

    @app.get("/api/coverage")
    def api_coverage():
        exchange = request.args.get("exchange", "all")
        source = request.args.get("source", "all")
        status = request.args.get("status", "all")
        q = request.args.get("q", "").strip().lower()
        page = max(int(request.args.get("page", 0)), 0)
        per = min(int(request.args.get("per", 60)), 200)
        # QuoteMedia now returns up to ~14 years; show them all by default
        # (cap 20 as a safety bound on the number of heatmap columns).
        n_years = min(int(request.args.get("years", 14)), 20)

        fin = _connect(fin_db)
        fil = _connect(fil_db)
        if not fin:
            return jsonify(years=[], rows=[], total=0, page=page, per=per, summary=[])
        try:
            years = [r[0] for r in _rows(fin, "SELECT DISTINCT fiscal_year FROM company_years "
                                              "ORDER BY fiscal_year DESC")][:n_years]
            years = sorted(years, reverse=True)

            # Universe rows come from tmx_financials_status (everything attempted),
            # left-joined to companies for the resolved source.
            where, params = [], []
            if exchange != "all":
                where.append("s.exchange = ?"); params.append(exchange)
            if status != "all":
                where.append("s.status = ?"); params.append(status)
            if source != "all":
                where.append("c.primary_source = ?"); params.append(source)
            if q:
                where.append("(LOWER(s.ticker) LIKE ? OR LOWER(s.company_name) LIKE ?)")
                params += [f"%{q}%", f"%{q}%"]
            wsql = ("WHERE " + " AND ".join(where)) if where else ""
            total = _scalar(fin, f"SELECT COUNT(*) FROM tmx_financials_status s "
                                 f"LEFT JOIN companies c ON c.ticker=s.ticker {wsql}", params)
            base = _rows(fin,
                f"SELECT s.ticker, s.company_name, s.exchange, s.status, c.primary_source "
                f"FROM tmx_financials_status s LEFT JOIN companies c ON c.ticker=s.ticker "
                f"{wsql} ORDER BY s.ticker LIMIT ? OFFSET ?", params + [per, page * per])
            tickers = [r[0] for r in base]

            cy = {}          # (ticker, year) -> source
            stmts = {}       # (ticker, year) -> set(statement_type)
            llmbad = set()   # (ticker, year) with any non-ok pdf_llm_status
            cons = set()     # tickers with a consistency flag
            extfail = set()  # (ticker, year) failed extraction
            if tickers:
                ph = ",".join("?" for _ in tickers)
                for t, y, src in _rows(fin, f"SELECT ticker, fiscal_year, source FROM company_years "
                                            f"WHERE ticker IN ({ph})", tickers):
                    cy[(t, y)] = src
                for t, y, st in _rows(fin, f"SELECT ticker, fiscal_year, statement_type "
                                           f"FROM statement_lines WHERE ticker IN ({ph}) "
                                           f"GROUP BY ticker, fiscal_year, statement_type", tickers):
                    stmts.setdefault((t, y), set()).add(st)
                for t, y in _rows(fin, f"SELECT DISTINCT ticker, fiscal_year FROM pdf_llm_status "
                                       f"WHERE status != 'ok' AND ticker IN ({ph})", tickers):
                    llmbad.add((t, y))
                cons = {r[0] for r in _rows(fin, f"SELECT DISTINCT ticker FROM pdf_llm_consistency "
                                                 f"WHERE ticker IN ({ph})", tickers)}
                if fil:
                    for t, y in _rows(fil, f"SELECT ticker, fiscal_year FROM pdf_extractions "
                                           f"WHERE extract_ok=0 AND ticker IN ({ph})", tickers):
                        extfail.add((t, y))

            def cell(ticker, status_col, year):
                if (ticker, year) in cy:
                    src = cy[(ticker, year)]
                    n_st = len(stmts.get((ticker, year), ()))
                    if src == "cse_pdf_extract":
                        if (ticker, year) in llmbad or ticker in cons:
                            return "review"
                        return "complete" if n_st >= 3 else "partial"
                    if 0 < n_st < 3:
                        return "partial"
                    return "complete"
                if (ticker, year) in extfail:
                    return "failed"
                if status_col == "no_data":
                    return "nodata"
                return "none"

            rows = []
            for t, name, exch, st_col, src in base:
                rows.append({"ticker": t, "name": name or "", "exchange": exch or "",
                             "source": src or "", "status": st_col,
                             "cells": {str(y): cell(t, st_col, y) for y in years}})

            # Whole-universe coverage % per year (not just the page).
            summary = []
            covered_by_year = dict(_rows(fin, "SELECT fiscal_year, COUNT(DISTINCT ticker) "
                                              "FROM company_years GROUP BY fiscal_year"))
            uni = _scalar(fin, "SELECT COUNT(*) FROM tmx_financials_status") or 1
            for y in years:
                c = covered_by_year.get(y, 0)
                summary.append({"year": y, "covered": c, "pct": round(c / uni * 100, 1)})

            return jsonify(years=years, rows=rows, total=total, page=page, per=per, summary=summary)
        finally:
            fin.close()
            if fil:
                fil.close()

    # ===== OPS: error center ============================================= #
    @app.get("/api/errors")
    def api_errors():
        fin = _connect(fin_db)
        fil = _connect(fil_db)
        try:
            cats = {}
            items = []

            def add(cat, stage, tone, ticker, reason, retryable=False, year=None):
                cats.setdefault(cat, {"category": cat, "stage": stage, "tone": tone, "count": 0})
                cats[cat]["count"] += 1
                if len(items) < 800:
                    items.append({"category": cat, "stage": stage, "ticker": ticker,
                                  "year": year, "reason": reason or "", "retryable": retryable})

            # Structured no-data (genuine misses, not the .P/.UN exclusions)
            if fin:
                for t, r in _rows(fin, "SELECT ticker, reason FROM tmx_financials_status "
                                       "WHERE status='no_data'"):
                    add("No structured data", "financials", "err", t, r)
            # Discovery failures
            if fil:
                for t, st, r in _rows(fil, "SELECT ticker, status, failure_reason FROM filings "
                                           "WHERE status != 'found' AND failure_reason IS NOT NULL"):
                    add(_pretty_reason(r), "discovery", "warn" if st == "needs_review" else "err", t, r)
                # Extraction failures
                for t, y, r in _rows(fil, "SELECT ticker, fiscal_year, reason FROM pdf_extractions "
                                          "WHERE extract_ok=0"):
                    add(_pretty_reason(r), "extract", "err", t, r, year=y)
            # LLM failures
            if fin:
                for t, y, s, r in _rows(fin, "SELECT ticker, fiscal_year, status, reason "
                                             "FROM pdf_llm_status WHERE status != 'ok'"):
                    tone = "warn" if s == "empty" else "err"
                    add(_pretty_reason(s), "normalize", tone, t, r, retryable=True, year=y)

            cards = sorted(cats.values(), key=lambda c: -c["count"])
            return jsonify(cards=cards, items=items, total=sum(c["count"] for c in cards))
        finally:
            if fin:
                fin.close()
            if fil:
                fil.close()

    # ===== OPS: data lineage ============================================= #
    @app.get("/api/lineage")
    def api_lineage():
        ticker = request.args.get("ticker", "").strip()
        year = request.args.get("year", "").strip()
        item = request.args.get("item", "").strip()
        fin = _connect(fin_db)
        fil = _connect(fil_db)
        if not fin or not ticker:
            return jsonify(error="ticker required"), 400
        try:
            comp = fin.execute("SELECT company_name, exchange, currency, primary_source "
                               "FROM companies WHERE ticker=?", (ticker,)).fetchone()
            if not comp:
                return jsonify(error=f"{ticker} not resolved"), 404
            years = [r[0] for r in _rows(fin, "SELECT fiscal_year FROM company_years "
                                              "WHERE ticker=? ORDER BY fiscal_year DESC", (ticker,))]
            fy = int(year) if year else (years[0] if years else None)
            cy = fin.execute("SELECT period_end, currency, source, source_ref FROM company_years "
                             "WHERE ticker=? AND fiscal_year=?", (ticker, fy)).fetchone() if fy else None
            lines = [{"item": li, "value": v} for li, v in _rows(
                fin, "SELECT line_item, value FROM statement_lines "
                     "WHERE ticker=? AND fiscal_year=? ORDER BY statement_type, line_item",
                     (ticker, fy))] if fy else []

            chain = []
            source = cy[2] if cy else comp[3]
            value = None
            if item and fy:
                row = fin.execute("SELECT value FROM statement_lines WHERE ticker=? AND fiscal_year=? "
                                  "AND line_item=?", (ticker, fy, item)).fetchone()
                value = row[0] if row else None
                chain.append({"title": item, "detail": _fmt_val(value),
                              "meta": f"FY{fy} · {(cy[1] if cy else comp[2]) or ''}",
                              "tone": "accent", "kind": "value"})
            chain.append({"title": comp[0] or ticker, "detail": f"{ticker} · {comp[1] or ''}",
                          "meta": f"reporting currency {comp[2] or '—'}", "tone": "ok", "kind": "company"})
            if cy:
                chain.append({"title": "Fiscal year " + str(fy),
                              "detail": f"period end {cy[0] or '—'}",
                              "meta": f"source: {source}", "tone": "ok", "kind": "year"})

            if source == "cse_pdf_extract":
                raw = _rows(fin, "SELECT statement_type, model, prompt_version, unit_scale, extracted_at "
                                 "FROM pdf_llm_raw WHERE ticker=? AND fiscal_year=?", (ticker, fy))
                if raw:
                    st, model, pv, scale, ts = raw[0]
                    chain.append({"title": "LLM extraction", "detail": f"model {model}",
                                  "meta": f"prompt {pv} · unit scale ×{int(scale or 1)} · {ts or ''}",
                                  "tone": "accent", "kind": "llm"})
                if fil:
                    ex = fil.execute("SELECT pdf_url, reason FROM pdf_extractions "
                                     "WHERE ticker=? AND fiscal_year=?", (ticker, fy)).fetchone()
                    fp = fil.execute("SELECT pdf_url, discovery_method, verified FROM filing_pdfs "
                                     "WHERE ticker=? AND fiscal_year=?", (ticker, fy)).fetchone()
                    url = (cy[3] if cy else None) or (ex[0] if ex else None) or (fp[0] if fp else None)
                    chain.append({"title": "Source document",
                                  "detail": "Annual-report PDF (text extracted, file discarded)",
                                  "meta": (fp[1] if fp else "cse_filings"), "tone": "ok",
                                  "kind": "doc", "url": url})
            else:
                chain.append({"title": "QuoteMedia", "detail": "Structured exchange-API feed",
                              "meta": (cy[3] if cy else ""), "tone": "ok", "kind": "api"})

            # validation
            vfin = _connect(fin_db)
            doc = company_export.build_document(vfin, ticker) if vfin else None
            if vfin:
                vfin.close()
            schema_ok = None
            if doc is not None:
                schema_ok = company_export._validate(doc, REPO_ROOT / "sql" / "company_schema.json") is None
            ncons = _scalar(fin, "SELECT COUNT(*) FROM pdf_llm_consistency WHERE ticker=?", (ticker,))
            chain.append({"title": "Validation",
                          "detail": ("schema valid" if schema_ok else "schema issue") if schema_ok is not None else "n/a",
                          "meta": f"{ncons} consistency flag(s)",
                          "tone": "ok" if schema_ok else "warn", "kind": "validate"})

            return jsonify(ticker=ticker, years=years, fy=fy, source=source,
                           lines=lines, chain=chain, value=value)
        finally:
            fin.close()
            if fil:
                fil.close()

    # ----- serve generated output files (dashboard iframe, xlsx dl) ------ #
    @app.get("/output/<path:fn>")
    def api_output(fn):
        return send_from_directory(str(output_dir), fn)

    return app


# --------------------------------------------------------------------------- #
#  Single-page front end (inline, vanilla JS + EventSource).
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Canadian Filings — Control Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#06070d;
  --ink:#eef1fb;--muted:#98a0bd;--muted2:#5b607a;
  --line:rgba(255,255,255,.065);--line2:rgba(255,255,255,.11);
  --glass:rgba(19,21,33,.55);--glass2:rgba(26,29,44,.6);--glass3:rgba(34,38,58,.7);
  --accent:#8a7bff;--accent2:#3fd8e8;--accent-dim:rgba(138,123,255,.14);--accent-glow:rgba(138,123,255,.35);
  --grad:linear-gradient(115deg,#a99bff 0%,#7c6cff 40%,#3fd8e8 100%);
  --grad-soft:linear-gradient(115deg,rgba(169,155,255,.9),rgba(63,216,232,.9));
  --green:#4fd6a4;--green-dim:rgba(79,214,164,.13);--red:#ff6b7c;--red-dim:rgba(255,107,124,.13);
  --amber:#ffca6a;--amber-dim:rgba(255,202,106,.14);
  --r:18px;--r2:12px;--r3:8px;
  --shadow:0 1px 1px rgba(0,0,0,.4),0 10px 30px -12px rgba(0,0,0,.5);
  --shadow-lg:0 20px 60px -20px rgba(0,0,0,.7);
  --font:'Space Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --mono:'JetBrains Mono',ui-monospace,'Cascadia Code',Consolas,monospace;
  --mx:50vw;--my:50vh;
}
*{box-sizing:border-box;margin:0;padding:0;}
html{scroll-behavior:smooth;}
body{font-family:var(--font);background:var(--bg);color:var(--ink);
line-height:1.55;font-size:15px;min-height:100vh;overflow-x:hidden;
-webkit-font-smoothing:antialiased;cursor:none;}
::selection{background:var(--accent-glow);color:#fff;}

/* ===== animated background layers ===== */
.bg-mesh,.bg-dots,.bg-dots-hot,.bg-spot,.bg-grain{position:fixed;inset:0;pointer-events:none;}
.bg-mesh{z-index:-4;inset:-25%;filter:blur(60px) saturate(1.3);opacity:.85;
background:
  radial-gradient(38% 38% at 22% 28%, rgba(96,71,255,.40), transparent 60%),
  radial-gradient(34% 34% at 82% 18%, rgba(35,199,224,.28), transparent 62%),
  radial-gradient(44% 44% at 72% 82%, rgba(150,71,255,.30), transparent 60%),
  radial-gradient(32% 32% at 26% 80%, rgba(28,201,150,.20), transparent 62%);
animation:drift 26s ease-in-out infinite alternate;}
@keyframes drift{
  0%{transform:translate3d(-1.5%,-1%,0) rotate(0deg) scale(1);}
  100%{transform:translate3d(2%,2.5%,0) rotate(6deg) scale(1.12);}}
.bg-dots{z-index:-3;background-image:radial-gradient(circle, rgba(255,255,255,.055) 1px, transparent 1.5px);
background-size:32px 32px;-webkit-mask:linear-gradient(#000,rgba(0,0,0,.25) 85%,transparent);
mask:linear-gradient(#000,rgba(0,0,0,.25) 85%,transparent);}
.bg-dots-hot{z-index:-3;background-image:radial-gradient(circle, rgba(155,145,255,.7) 1px, transparent 1.6px);
background-size:32px 32px;
-webkit-mask:radial-gradient(220px circle at var(--mx) var(--my), #000 0%, transparent 62%);
mask:radial-gradient(220px circle at var(--mx) var(--my), #000 0%, transparent 62%);}
.bg-spot{z-index:-2;background:radial-gradient(600px circle at var(--mx) var(--my),
rgba(138,123,255,.11), rgba(63,216,232,.05) 40%, transparent 62%);}
.bg-grain{z-index:-1;opacity:.035;mix-blend-mode:overlay;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");}

/* ===== custom cursor ===== */
#cur-dot,#cur-ring{position:fixed;top:0;left:0;pointer-events:none;z-index:9999;border-radius:50%;
will-change:transform;}
#cur-dot{width:7px;height:7px;margin:-3.5px 0 0 -3.5px;background:var(--accent2);
box-shadow:0 0 10px var(--accent2);}
#cur-ring{width:34px;height:34px;margin:-17px 0 0 -17px;border:1.5px solid rgba(160,150,255,.7);
transition:width .18s ease,height .18s ease,margin .18s ease,border-color .18s ease,background .18s ease;
mix-blend-mode:difference;}
body.cur-hot #cur-ring{width:52px;height:52px;margin:-26px 0 0 -26px;border-color:rgba(63,216,232,.9);
background:rgba(138,123,255,.08);}
body.cur-down #cur-ring{width:26px;height:26px;margin:-13px 0 0 -13px;}

.wrap{max-width:1180px;margin:0 auto;padding:36px 24px 90px;position:relative;z-index:1;}

/* ===== header ===== */
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:30px;gap:14px;flex-wrap:wrap;}
.brand{display:flex;align-items:center;gap:14px;}
.logo{width:44px;height:44px;border-radius:13px;background:var(--grad);display:grid;place-items:center;
box-shadow:0 8px 24px -6px var(--accent-glow),inset 0 1px 0 rgba(255,255,255,.35);flex-shrink:0;}
.logo svg{width:24px;height:24px;}
header h1{font-size:clamp(20px,2.3vw,27px);font-weight:700;letter-spacing:-.5px;line-height:1.1;
background:linear-gradient(180deg,#fff,#b9bedd);-webkit-background-clip:text;background-clip:text;
-webkit-text-fill-color:transparent;}
header .subtitle{font-size:13px;color:var(--muted);margin-top:3px;letter-spacing:.1px;}
.hdr-right{display:flex;align-items:center;gap:14px;}
.timer{font-size:13px;color:var(--muted);font-family:var(--mono);font-variant-numeric:tabular-nums;
min-width:48px;text-align:right;}

/* ===== pill ===== */
.pill{display:inline-flex;align-items:center;gap:8px;padding:7px 15px 7px 12px;border-radius:30px;
font-size:12.5px;font-weight:600;letter-spacing:.2px;border:1px solid var(--line2);
background:var(--glass);backdrop-filter:blur(12px);transition:all .25s ease;}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor;}
.pill.idle{color:var(--muted);}
.pill.run{color:var(--amber);border-color:rgba(255,202,106,.4);background:var(--amber-dim);}
.pill.run .dot{animation:pulse 1.1s ease-in-out infinite;}
.pill.ok{color:var(--green);border-color:rgba(79,214,164,.4);background:var(--green-dim);}
.pill.err{color:var(--red);border-color:rgba(255,107,124,.4);background:var(--red-dim);}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.35;transform:scale(.6);}}

/* ===== tabs ===== */
.tabs{display:flex;gap:4px;margin-bottom:26px;padding:5px;border-radius:14px;
background:var(--glass);backdrop-filter:blur(12px);border:1px solid var(--line);width:fit-content;}
.tab{padding:9px 20px;cursor:none;color:var(--muted);border-radius:10px;
font-size:13.5px;font-weight:600;letter-spacing:.2px;transition:all .2s ease;user-select:none;position:relative;}
.tab:hover{color:var(--ink);}
.tab.active{color:#fff;background:linear-gradient(180deg,rgba(138,123,255,.25),rgba(138,123,255,.12));
box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 4px 14px -6px var(--accent-glow);}
.view{display:none;}.view.active{display:block;animation:rise .35s cubic-bezier(.2,.7,.3,1);}
@keyframes rise{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:none;}}

/* ===== glass surface (shared) ===== */
.step-card,.panel,.stepper{position:relative;background:var(--glass);backdrop-filter:blur(16px) saturate(1.2);
border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);}
.step-card::before,.panel::before,.stepper::before{content:"";position:absolute;inset:0;border-radius:inherit;
padding:1px;background:linear-gradient(160deg,rgba(255,255,255,.14),transparent 40%);
-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
-webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none;}

/* ===== stepper ===== */
.stepper{display:flex;align-items:center;margin-bottom:22px;padding:16px 22px;}
.step-item{display:flex;align-items:center;gap:11px;flex:1;cursor:none;transition:transform .2s ease;}
.step-item:hover{transform:translateY(-1px);}
.step-num{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;
font-size:13px;font-weight:700;font-family:var(--mono);background:var(--glass3);color:var(--muted);flex-shrink:0;
border:1px solid var(--line2);transition:all .2s ease;}
.step-item:hover .step-num{border-color:transparent;background:var(--grad);color:#fff;
box-shadow:0 6px 18px -6px var(--accent-glow);}
.step-label{font-size:13px;font-weight:600;color:var(--muted);white-space:nowrap;transition:color .2s ease;}
.step-item:hover .step-label{color:var(--ink);}
.step-line{flex:0 0 30px;height:1.5px;background:linear-gradient(90deg,var(--line2),transparent);margin:0 8px;}
@media(max-width:900px){.stepper{display:none;}}

/* ===== step cards ===== */
.step-card{padding:26px;margin-bottom:18px;scroll-margin-top:20px;transition:transform .25s ease,box-shadow .25s ease;}
.step-card:hover{box-shadow:var(--shadow-lg);}
.sc-head{display:flex;align-items:flex-start;gap:15px;margin-bottom:20px;}
.sc-badge{width:38px;height:38px;border-radius:12px;background:var(--accent-dim);color:#c3bcff;
display:grid;place-items:center;font-weight:700;font-size:16px;font-family:var(--mono);flex-shrink:0;
border:1px solid rgba(138,123,255,.3);}
.sc-badge.exports{background:var(--glass3);color:var(--muted);border-color:var(--line2);}
.sc-title{font-size:18px;font-weight:700;letter-spacing:-.3px;}
.sc-desc{font-size:13px;color:var(--muted);margin-top:4px;max-width:64ch;}

/* ===== generic panel ===== */
.panel{padding:22px;margin-bottom:18px;}
.panel h3{font-size:11.5px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);
margin-bottom:18px;font-weight:700;display:flex;align-items:center;justify-content:space-between;}

/* ===== form controls ===== */
.field{margin-bottom:18px;}
.field:last-child{margin-bottom:0;}
.field-row{display:flex;gap:18px;flex-wrap:wrap;}
.field-row .field{flex:1;min-width:170px;}
label.f-label{display:block;font-size:12.5px;font-weight:600;color:var(--ink);margin-bottom:7px;letter-spacing:.2px;}
.hint{font-size:12px;color:var(--muted2);margin-top:7px;line-height:1.45;}
input[type=text],input[type=number],select{
  width:100%;background:rgba(0,0,0,.25);border:1px solid var(--line2);color:var(--ink);font-family:var(--font);
  padding:11px 13px;border-radius:var(--r3);font-size:14px;transition:all .18s ease;cursor:none;}
input[type=text]:focus,input[type=number]:focus,select:focus{
  outline:none;border-color:var(--accent);background:rgba(0,0,0,.35);box-shadow:0 0 0 3px var(--accent-dim);}
input::placeholder{color:var(--muted2);}
select{cursor:none;appearance:none;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='none' stroke='%2398a0bd' stroke-width='2'%3E%3Cpath d='M2 4l4 4 4-4'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:right 13px center;padding-right:34px;}
.chk-row{display:flex;align-items:center;gap:10px;}
.chk-row input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent);cursor:none;}
.chk-row label{font-size:14px;color:var(--ink);cursor:none;user-select:none;}

/* ===== buttons ===== */
button{
  display:inline-flex;align-items:center;justify-content:center;gap:9px;background:var(--glass3);color:var(--ink);
  border:1px solid var(--line2);padding:12px 20px;border-radius:11px;font-family:var(--font);
  cursor:none;font-size:14px;font-weight:600;letter-spacing:.2px;transition:all .2s cubic-bezier(.2,.7,.3,1);
  white-space:nowrap;position:relative;overflow:hidden;}
button:hover{transform:translateY(-2px);border-color:var(--line2);box-shadow:0 10px 24px -10px rgba(0,0,0,.6);}
button:active{transform:translateY(0);}
button:disabled{opacity:.35;transform:none;box-shadow:none;}
button.primary{background:var(--grad);border:none;color:#fff;padding:13px 26px;font-size:14.5px;
box-shadow:0 8px 22px -8px var(--accent-glow),inset 0 1px 0 rgba(255,255,255,.3);}
button.primary:hover{box-shadow:0 14px 34px -10px var(--accent-glow),inset 0 1px 0 rgba(255,255,255,.3);filter:brightness(1.06);}
button.primary::after{content:"";position:absolute;inset:0;background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,.25) 50%,transparent 70%);
transform:translateX(-120%);transition:transform .6s ease;}
button.primary:hover::after{transform:translateX(120%);}
button.ghost{background:rgba(255,255,255,.03);}
button.ghost:hover{background:rgba(255,255,255,.06);border-color:rgba(138,123,255,.5);}
button.danger{background:var(--red-dim);border-color:rgba(255,107,124,.4);color:var(--red);}
button.danger:hover{background:rgba(255,107,124,.2);}
button.small{padding:7px 13px;font-size:12.5px;border-radius:9px;}
.ic{width:16px;height:16px;flex-shrink:0;display:block;}
.btngrid{display:flex;gap:12px;flex-wrap:wrap;align-items:center;}

/* advanced disclosure */
.adv-toggle{font-size:13px;color:#a99bff;cursor:none;user-select:none;
display:inline-flex;align-items:center;gap:7px;margin-bottom:16px;font-weight:600;}
.adv-toggle .chev{transition:transform .2s ease;display:grid;place-items:center;}
.adv-toggle.open .chev{transform:rotate(90deg);}
.adv-body{display:none;}
.adv-body.open{display:block;animation:rise .25s ease;}

/* ===== console ===== */
.console-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;}
.console-label{font-size:11.5px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-weight:700;}
pre#console{background:rgba(0,0,0,.4);border:1px solid var(--line);border-radius:var(--r2);
padding:18px;height:330px;overflow:auto;font-family:var(--mono);
font-size:12.5px;white-space:pre-wrap;color:#c8cfe6;line-height:1.7;}
pre#console:empty::before{content:'Ready — pick a stage to begin.';color:var(--muted2);}

/* ===== stats cards ===== */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:16px;}
.card{position:relative;background:rgba(0,0,0,.22);border:1px solid var(--line);
border-radius:var(--r2);padding:18px 20px;overflow:hidden;transition:transform .25s ease,border-color .25s ease;}
.card:hover{transform:translateY(-3px);border-color:var(--line2);}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line2);}
.card.c-accent::before{background:var(--grad);}
.card.c-green::before{background:linear-gradient(180deg,#4fd6a4,#2ea);}
.card.c-amber::before{background:linear-gradient(180deg,#ffca6a,#f90);}
.card .v{font-size:28px;font-weight:700;letter-spacing:-.6px;line-height:1.1;}
.card .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;font-weight:700;margin-top:4px;}
.card .s{font-size:12px;color:var(--muted2);margin-top:5px;}

/* ===== tables ===== */
table{width:100%;border-collapse:collapse;font-size:13.5px;}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);}
th{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;font-weight:700;cursor:default;}
tbody tr{cursor:none;transition:background .12s ease;}
tbody tr:hover{background:rgba(138,123,255,.07);}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;font-family:var(--mono);
background:rgba(138,123,255,.12);border:1px solid rgba(138,123,255,.22);color:#b6acff;}
.split{display:grid;grid-template-columns:1fr 1fr;gap:18px;}
pre.json{background:rgba(0,0,0,.4);border:1px solid var(--line);border-radius:var(--r2);padding:16px;
overflow:auto;max-height:480px;font-size:12.5px;font-family:var(--mono);color:#c8cfe6;line-height:1.7;}
iframe{width:100%;height:660px;border:1px solid var(--line);border-radius:var(--r);background:#fff;}
.ok-c{color:var(--green);}.err-c{color:var(--red);}
a{color:#a99bff;text-decoration:none;}a:hover{color:#c3bcff;}
.empty-state{color:var(--muted2);font-size:13.5px;padding:40px 14px;text-align:center;}
@media(max-width:800px){.split{grid-template-columns:1fr;}}

/* ===== toasts ===== */
#toast-stack{position:fixed;bottom:26px;right:26px;display:flex;flex-direction:column;gap:10px;z-index:9998;}
.toast{background:var(--glass3);backdrop-filter:blur(16px);border:1px solid var(--line2);
border-left:3px solid var(--accent);border-radius:var(--r2);padding:14px 18px;font-size:13.5px;
box-shadow:var(--shadow-lg);max-width:360px;animation:toast-in .3s cubic-bezier(.2,.7,.3,1);}
.toast.err{border-left-color:var(--red);}
.toast.ok{border-left-color:var(--green);}
@keyframes toast-in{from{opacity:0;transform:translateX(30px) scale(.96);}to{opacity:1;transform:none;}}

/* ===== tab bar scroll on narrow ===== */
.tabs{overflow-x:auto;max-width:100%;}
.tabs::-webkit-scrollbar{height:0;}

/* ===== OPS: pipeline DAG ===== */
.dag-rail{display:flex;align-items:stretch;overflow-x:auto;padding:8px 2px 18px;}
.dag-node{flex:0 0 165px;background:var(--glass);border:1px solid var(--line);border-radius:var(--r2);
padding:15px;position:relative;transition:transform .25s ease,border-color .25s ease,box-shadow .25s ease;cursor:none;}
.dag-node:hover{border-color:var(--line2);transform:translateY(-3px);}
.dag-node.sel{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim);}
.dag-node.active{border-color:rgba(255,202,106,.6);animation:nodepulse 1.5s ease-in-out infinite;}
@keyframes nodepulse{0%,100%{box-shadow:0 0 20px -6px rgba(255,202,106,.5);}50%{box-shadow:0 0 34px 2px rgba(255,202,106,.8);}}
.dag-node .n-label{font-size:12.5px;font-weight:700;letter-spacing:.2px;}
.dag-node .n-desc{font-size:10.5px;color:var(--muted2);line-height:1.35;height:28px;overflow:hidden;margin:3px 0 12px;}
.dag-node .n-main{font-size:27px;font-weight:700;letter-spacing:-.6px;line-height:1;color:var(--green);}
.dag-node.z .n-main{color:var(--muted);}
.dag-node .n-sub{font-size:10.5px;color:var(--muted);margin-top:3px;}
.dag-node .n-chips{display:flex;gap:6px;margin-top:11px;flex-wrap:wrap;min-height:20px;}
.n-chip{font-size:10.5px;font-weight:600;padding:2px 8px;border-radius:20px;font-family:var(--mono);}
.n-chip.err{background:var(--red-dim);color:var(--red);}
.n-chip.warn{background:var(--amber-dim);color:var(--amber);}
.dag-node .n-bar{display:flex;height:4px;border-radius:3px;overflow:hidden;margin-top:13px;background:rgba(255,255,255,.06);}
.dag-node .n-bar i{height:100%;}
.dag-conn{flex:0 0 40px;position:relative;align-self:center;height:2px;
background:linear-gradient(90deg,var(--line2),var(--line2));}
.dag-conn::after{content:"";position:absolute;top:-2.5px;left:0;width:6px;height:6px;border-radius:50%;
background:var(--accent2);box-shadow:0 0 10px var(--accent2);animation:flow 2.6s linear infinite;}
@keyframes flow{0%{left:-4px;opacity:0;}12%{opacity:1;}88%{opacity:1;}100%{left:38px;opacity:0;}}
.dag-legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-top:2px;}
.dag-legend b{color:var(--ink);font-weight:600;}

/* ===== OPS: coverage heatmap ===== */
.hm-legend{display:flex;gap:16px;flex-wrap:wrap;margin:4px 0 18px;font-size:12px;color:var(--muted);}
.hm-legend span{display:inline-flex;align-items:center;gap:7px;}
.hm-sw{width:13px;height:13px;border-radius:4px;display:inline-block;}
.sw-complete{background:#4fd6a4;}.sw-partial{background:#a97bff;}.sw-review{background:#ffca6a;}
.sw-failed{background:#ff6b7c;}.sw-nodata{background:#3a3f52;}
.sw-none{background:rgba(255,255,255,.05);border:1px solid var(--line2);}
.hm-summary{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;}
.hm-ybar{flex:1;min-width:78px;background:rgba(0,0,0,.22);border:1px solid var(--line);
border-radius:var(--r3);padding:11px 12px;text-align:center;}
.hm-ybar .y{font-size:12px;color:var(--muted);font-family:var(--mono);}
.hm-ybar .p{font-size:20px;font-weight:700;letter-spacing:-.4px;margin-top:2px;}
.hm-ybar .track{height:4px;border-radius:2px;background:rgba(255,255,255,.06);margin-top:8px;overflow:hidden;}
.hm-ybar .track i{display:block;height:100%;background:var(--grad);}
.hm-scroll{overflow:auto;max-height:560px;}
.hm-grid{border-collapse:separate;border-spacing:0;}
.hm-grid th{position:sticky;top:0;background:rgba(12,14,22,.92);backdrop-filter:blur(4px);
font-size:10.5px;text-align:center;padding:7px 4px;color:var(--muted);z-index:2;font-family:var(--mono);}
.hm-grid th.tkh{text-align:left;left:0;z-index:3;padding-left:2px;}
.hm-grid td.tk{font-size:12.5px;padding:3px 14px 3px 2px;white-space:nowrap;max-width:230px;
overflow:hidden;text-overflow:ellipsis;position:sticky;left:0;background:var(--bg);z-index:1;}
.hm-grid td.tk b{color:var(--ink);}.hm-grid td.tk small{color:var(--muted2);}
.hm-cell{width:26px;height:26px;border-radius:6px;margin:2px auto;cursor:none;transition:transform .12s ease;}
.hm-cell:hover{transform:scale(1.28);box-shadow:0 0 0 2px rgba(255,255,255,.25);}
.c-complete{background:#4fd6a4;}.c-partial{background:#a97bff;}.c-review{background:#ffca6a;}
.c-failed{background:#ff6b7c;}.c-nodata{background:#3a3f52;}.c-none{background:rgba(255,255,255,.045);}
.pager{display:flex;gap:10px;align-items:center;margin-top:16px;font-size:13px;color:var(--muted);}

/* ===== OPS: error center ===== */
.err-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(195px,1fr));gap:13px;margin-bottom:20px;}
.err-card{background:rgba(0,0,0,.2);border:1px solid var(--line);border-left:3px solid var(--muted2);
border-radius:var(--r2);padding:15px;cursor:none;transition:transform .2s ease,border-color .2s ease;}
.err-card:hover{transform:translateY(-3px);border-color:var(--line2);}
.err-card.sel{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim);}
.err-card.err{border-left-color:var(--red);}.err-card.warn{border-left-color:var(--amber);}
.err-card .c{font-size:26px;font-weight:700;letter-spacing:-.5px;}
.err-card .t{font-size:13px;font-weight:600;margin-top:3px;line-height:1.3;}
.err-card .st{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;margin-top:6px;}
.err-tree{display:flex;height:34px;border-radius:var(--r3);overflow:hidden;margin-bottom:20px;border:1px solid var(--line);}
.err-tree i{height:100%;min-width:3px;transition:opacity .2s ease;position:relative;cursor:none;}
.err-tree i:hover{opacity:.82;}
.err-tree i span{position:absolute;inset:0;display:grid;place-items:center;font-size:10px;
color:rgba(0,0,0,.65);font-weight:800;overflow:hidden;}

/* ===== OPS: lineage ===== */
.ln-controls{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin-bottom:6px;}
.ln-chain{position:relative;padding-left:30px;margin-top:10px;}
.ln-chain::before{content:"";position:absolute;left:10px;top:8px;bottom:8px;width:2px;
background:linear-gradient(180deg,var(--accent),var(--accent2));}
.ln-step{position:relative;margin-bottom:14px;background:var(--glass2);border:1px solid var(--line);
border-radius:var(--r2);padding:13px 16px;animation:rise .3s ease;}
.ln-step::before{content:"";position:absolute;left:-25px;top:16px;width:13px;height:13px;border-radius:50%;
background:var(--bg);border:2.5px solid var(--accent);}
.ln-step.tone-warn::before{border-color:var(--amber);}
.ln-step.tone-accent::before{border-color:var(--accent2);box-shadow:0 0 10px var(--accent2);}
.ln-step .st-title{font-size:14px;font-weight:700;}
.ln-step .st-detail{font-size:13.5px;color:var(--ink);margin-top:3px;}
.ln-step .st-meta{font-size:11.5px;color:var(--muted2);margin-top:4px;font-family:var(--mono);word-break:break-all;}
.ln-items{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px;max-height:190px;overflow:auto;}
.ln-items button{font-family:var(--mono);font-size:11.5px;font-weight:500;padding:5px 10px;}
.ln-items button.on{background:var(--grad);border:none;color:#fff;}

/* ===== reduced motion / touch: restore native cursor ===== */
@media (hover:none),(prefers-reduced-motion:reduce){
  body{cursor:auto;}
  #cur-dot,#cur-ring,.bg-dots-hot{display:none;}
  .bg-mesh{animation:none;}
  *{cursor:auto !important;}
}
</style></head><body>
<div class="bg-mesh"></div>
<div class="bg-dots"></div>
<div class="bg-dots-hot"></div>
<div class="bg-spot"></div>
<div class="bg-grain"></div>
<div id="cur-ring"></div>
<div id="cur-dot"></div>
<div id="toast-stack"></div>
<div class="wrap">
<header>
  <div class="brand">
    <div class="logo"><svg viewBox="0 0 24 24" fill="none"><path d="M12 2.5c.35 4.9 3.7 8.25 8.6 8.6-4.9.35-8.25 3.7-8.6 8.6-.35-4.9-3.7-8.25-8.6-8.6 4.9-.35 8.25-3.7 8.6-8.6z" fill="#fff"/></svg></div>
    <div>
      <h1>Canadian Filings</h1>
      <div class="subtitle">Financials &amp; PDF/LLM extraction pipeline — local control panel</div>
    </div>
  </div>
  <div class="hdr-right">
    <span class="timer" id="timer"></span>
    <span id="status-pill" class="pill idle"><span class="dot"></span>idle</span>
  </div>
</header>

<div class="tabs">
  <div class="tab active" data-view="pipeline">Pipeline</div>
  <div class="tab" data-view="coverage">Coverage</div>
  <div class="tab" data-view="errors">Errors</div>
  <div class="tab" data-view="lineage">Lineage</div>
  <div class="tab" data-view="run">Run</div>
  <div class="tab" data-view="browse">Browse</div>
  <div class="tab" data-view="dashboard">Dashboard</div>
  <div class="tab" data-view="settings">Settings</div>
</div>

<!-- ============ PIPELINE (mission control) ============ -->
<div class="view active" id="view-pipeline">
  <div class="panel">
    <h3>Pipeline Flow
      <button class="small ghost" onclick="loadPipeline()"><svg class="ic" style="width:13px;height:13px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>Refresh</button>
    </h3>
    <div class="dag-rail" id="dag-rail"></div>
    <div class="dag-legend">
      <span><b style="color:var(--green)">■</b> completed</span>
      <span><b style="color:var(--amber)">■</b> needs review</span>
      <span><b style="color:var(--red)">■</b> failed</span>
      <span style="color:var(--muted2)">click a stage for its breakdown · an amber glow marks a currently-running stage</span>
    </div>
  </div>
  <div class="panel" id="dag-detail-panel" style="display:none;">
    <h3 id="dag-detail-title">Stage detail</h3>
    <div id="dag-detail-body"></div>
  </div>
</div>

<!-- ============ COVERAGE ============ -->
<div class="view" id="view-coverage">
  <div class="panel">
    <div class="field-row" style="align-items:flex-end;">
      <div class="field"><label class="f-label">Exchange</label><select id="cov-exchange" onchange="loadCoverage(0)"></select></div>
      <div class="field"><label class="f-label">Source</label><select id="cov-source" onchange="loadCoverage(0)"></select></div>
      <div class="field"><label class="f-label">Status</label><select id="cov-status" onchange="loadCoverage(0)"></select></div>
      <div class="field" style="flex:2"><label class="f-label">Search</label><input type="text" id="cov-q" placeholder="ticker or name…" oninput="covDebounce()"></div>
    </div>
    <div class="hm-legend">
      <span><i class="hm-sw sw-complete"></i>Complete</span>
      <span><i class="hm-sw sw-partial"></i>Partial</span>
      <span><i class="hm-sw sw-review"></i>Needs review</span>
      <span><i class="hm-sw sw-failed"></i>Failed</span>
      <span><i class="hm-sw sw-nodata"></i>No data</span>
      <span><i class="hm-sw sw-none"></i>Not reported</span>
    </div>
    <div class="hm-summary" id="hm-summary"></div>
    <div class="hm-scroll"><table class="hm-grid" id="hm-grid"></table></div>
    <div class="pager">
      <button class="small ghost" id="cov-prev" onclick="loadCoverage(covPage-1)">Prev</button>
      <span id="cov-pageinfo"></span>
      <button class="small ghost" id="cov-next" onclick="loadCoverage(covPage+1)">Next</button>
      <span style="margin-left:auto;color:var(--muted2)" id="cov-tip">Click any cell to trace that company-year's lineage.</span>
    </div>
  </div>
</div>

<!-- ============ ERRORS ============ -->
<div class="view" id="view-errors">
  <div class="panel">
    <h3>What's broken today
      <button class="small ghost" onclick="loadErrors()"><svg class="ic" style="width:13px;height:13px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>Refresh</button>
    </h3>
    <div class="err-tree" id="err-tree"></div>
    <div class="err-cards" id="err-cards"></div>
  </div>
  <div class="panel">
    <h3 id="err-table-title">Failed jobs</h3>
    <div style="overflow:auto;max-height:480px;"><table><thead><tr><th>Ticker</th><th>Stage</th><th>Category</th><th>Reason</th><th></th></tr></thead>
    <tbody id="err-body"></tbody></table></div>
  </div>
</div>

<!-- ============ LINEAGE ============ -->
<div class="view" id="view-lineage">
  <div class="panel">
    <h3>Trace a number to its source</h3>
    <div class="ln-controls">
      <div class="field" style="margin-bottom:0;"><label class="f-label">Ticker</label><input type="text" id="ln-ticker" placeholder="e.g. AAA.P" style="min-width:150px"></div>
      <div class="field" style="margin-bottom:0;"><label class="f-label">Fiscal year</label><select id="ln-year" onchange="traceLineage()"></select></div>
      <button class="primary" onclick="traceLineage()">Trace</button>
    </div>
    <div id="ln-items" class="ln-items"></div>
  </div>
  <div class="panel" id="ln-panel" style="display:none;">
    <h3 id="ln-title">Provenance</h3>
    <div class="ln-chain" id="ln-chain"></div>
  </div>
</div>

<!-- ============ RUN ============ -->
<div class="view" id="view-run">

  <div class="stepper">
    <div class="step-item" onclick="scrollToStep('step-1')">
      <span class="step-num">1</span><span class="step-label">Gather Data</span></div>
    <div class="step-line"></div>
    <div class="step-item" onclick="scrollToStep('step-2')">
      <span class="step-num">2</span><span class="step-label">Process PDFs</span></div>
    <div class="step-line"></div>
    <div class="step-item" onclick="scrollToStep('step-3')">
      <span class="step-num">3</span><span class="step-label">LLM Extraction</span></div>
    <div class="step-line"></div>
    <div class="step-item exports" onclick="scrollToStep('step-4')">
      <span class="step-num">4</span><span class="step-label">Reports &amp; Exports</span></div>
  </div>

  <!-- STEP 1 -->
  <div class="step-card" id="step-1">
    <div class="sc-head">
      <div class="sc-badge">1</div>
      <div>
        <div class="sc-title">Gather Data</div>
        <div class="sc-desc">Pull structured financials straight from exchange APIs, then find annual-report
          PDFs for whatever's left. Financials-only is the quick path (~99% coverage); Full Pipeline also
          runs the PDF finder on the rest.</div>
      </div>
    </div>
    <div class="field-row">
      <div class="field" style="flex:2;">
        <label class="f-label">Input file</label>
        <input type="text" id="input-file" placeholder="data/Canadian Companies.xlsx">
        <div class="hint">GuruFocus .xlsx export, or a CSV with ticker,legal_company_name,exchange columns.</div>
      </div>
      <div class="field">
        <label class="f-label">Mode</label>
        <select id="mode" onchange="onModeChange()">
          <option value="pilot">Pilot (sample)</option>
          <option value="full">Full (everything)</option>
          <option value="resume">Resume (continue)</option>
        </select>
        <div class="hint" id="mode-hint">Samples ~40 companies for a quick test run.</div>
      </div>
      <div class="field" id="sample-size-field">
        <label class="f-label">Sample size</label>
        <input type="number" id="sample-size" placeholder="40">
      </div>
    </div>
    <div class="field">
      <div class="chk-row"><input type="checkbox" id="no-render"><label for="no-render">Skip slow-render fallback (no-render)</label></div>
      <div class="hint">Skips the headless-browser pass for stubborn IR sites — faster, slightly lower success rate.</div>
    </div>
    <div class="btngrid" style="margin-top:6px;">
      <button class="ghost" onclick="run('financials')"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/></svg>Financials only</button>
      <button class="primary" onclick="run('pipeline')"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11-6.86a1 1 0 0 0 0-1.72l-11-6.86A1 1 0 0 0 8 5.14z"/></svg>Run full pipeline</button>
    </div>
  </div>

  <!-- STEP 2 -->
  <div class="step-card" id="step-2">
    <div class="sc-head">
      <div class="sc-badge">2</div>
      <div>
        <div class="sc-title">Process PDFs</div>
        <div class="sc-desc">Downloads each PDF step 1 found, extracts the income/balance/cash-flow statement
          text, then deletes the file — nothing is kept on disk. No extra options needed.</div>
      </div>
    </div>
    <div class="btngrid">
      <button class="primary" onclick="run('process_pdfs')"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11-6.86a1 1 0 0 0 0-1.72l-11-6.86A1 1 0 0 0 8 5.14z"/></svg>Process PDFs</button>
    </div>
  </div>

  <!-- STEP 3 -->
  <div class="step-card" id="step-3">
    <div class="sc-head">
      <div class="sc-badge">3</div>
      <div>
        <div class="sc-title">LLM Extraction</div>
        <div class="sc-desc">Reads the extracted statement text and maps it onto the canonical financials
          schema using a local Ollama model. <span id="step3-ready-hint"></span></div>
      </div>
    </div>
    <div class="adv-toggle" onclick="toggleAdv(this)"><span class="chev"><svg class="ic" style="width:14px;height:14px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg></span>Advanced options (force, limit, tickers)</div>
    <div class="adv-body">
      <div class="field-row">
        <div class="field">
          <div class="chk-row"><input type="checkbox" id="force"><label for="force">Force re-run</label></div>
          <div class="hint">Re-process statements already marked ok — use after tweaking the prompt or vocab.</div>
        </div>
        <div class="field">
          <label class="f-label">Limit</label>
          <input type="number" id="limit" placeholder="all">
          <div class="hint">Only process the first N PDF rows — handy for a quick smoke test.</div>
        </div>
        <div class="field" style="flex:2;">
          <label class="f-label">Tickers</label>
          <input type="text" id="tickers" placeholder="e.g. AAA.P, BBB.P">
          <div class="hint">Comma-separated allow-list to restrict this run to specific companies.</div>
        </div>
      </div>
    </div>
    <div class="btngrid" style="margin-top:6px;">
      <button class="primary" onclick="run('llm_extract')"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11-6.86a1 1 0 0 0 0-1.72l-11-6.86A1 1 0 0 0 8 5.14z"/></svg>Run LLM extraction</button>
    </div>
  </div>

  <!-- STEP 4 -->
  <div class="step-card" id="step-4">
    <div class="sc-head">
      <div class="sc-badge exports">4</div>
      <div>
        <div class="sc-title">Reports &amp; Exports</div>
        <div class="sc-desc">Regenerate outputs from whatever's currently in the databases — safe to re-run any time.</div>
      </div>
    </div>
    <div class="btngrid">
      <button class="ghost" onclick="run('dashboard')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 21V11M12 21V5M19 21v-8"/><path d="M3 21h18"/></svg>Build dashboard</button>
      <button class="ghost" onclick="run('export_json')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H7a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1"/><path d="M16 3h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-1"/></svg>Export all JSON</button>
      <button class="ghost" onclick="run('export_xlsx')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2h8l4 4v16H6z"/><path d="M14 2v4h4"/></svg>Export Excel summary</button>
      <a href="/output/financials_summary.xlsx" download><button class="ghost"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M5 21h14"/></svg>Download xlsx</button></a>
    </div>
  </div>

  <div class="panel">
    <div class="console-head">
      <span class="console-label">Console</span>
      <div class="btngrid">
        <button class="small ghost" onclick="copyConsole()"><svg class="ic" style="width:14px;height:14px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>Copy</button>
        <button class="small danger" id="stop-btn" onclick="stop()" disabled><svg class="ic" style="width:13px;height:13px" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>Stop</button>
      </div>
    </div>
    <pre id="console"></pre>
  </div>

  <div class="panel">
    <h3>Live Stats <button class="small ghost" onclick="loadStats()"><svg class="ic" style="width:13px;height:13px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>Refresh</button></h3>
    <div class="cards" id="stats-cards"></div>
  </div>
</div>

<!-- ============ BROWSE ============ -->
<div class="view" id="view-browse">
  <div class="panel">
    <div class="field-row">
      <div class="field">
        <label class="f-label">Source</label>
        <select id="src-filter" onchange="loadCompanies()">
          <option value="all">All sources</option>
          <option value="cse_pdf_extract">cse_pdf_extract (PDF/LLM)</option>
          <option value="tmx_quotemedia">tmx_quotemedia</option>
          <option value="cse_quotemedia">cse_quotemedia</option>
          <option value="neo_quotemedia">neo_quotemedia</option>
        </select>
      </div>
      <div class="field" style="flex:2;">
        <label class="f-label">Search</label>
        <input type="text" id="co-search" placeholder="Search ticker or company name…" oninput="debouncedCompanies()">
      </div>
    </div>
    <div class="hint" id="co-count"></div>
  </div>
  <div class="split">
    <div class="panel" style="max-height:600px;overflow:auto;padding:8px 0 8px 18px;">
      <table><thead><tr><th>Ticker</th><th>Company</th><th>Exch</th><th>Source</th><th>Yr</th></tr></thead>
      <tbody id="co-body"></tbody></table>
    </div>
    <div class="panel">
      <div id="co-detail" class="empty-state">Select a company on the left to view its exported JSON and step-3 diagnostics.</div>
    </div>
  </div>
</div>

<!-- ============ DASHBOARD ============ -->
<div class="view" id="view-dashboard">
  <div class="panel">
    <div class="btngrid" style="margin-bottom:14px;">
      <button class="primary" onclick="run('dashboard')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>Rebuild dashboard</button>
      <a href="/output/financials_dashboard.html" target="_blank"><button class="ghost">Open in new tab<svg class="ic" style="width:14px;height:14px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M9 7h8v8"/></svg></button></a>
    </div>
    <iframe id="dash-frame" src="/output/financials_dashboard.html"></iframe>
  </div>
</div>

<!-- ============ SETTINGS ============ -->
<div class="view" id="view-settings">
  <div class="panel">
    <h3>Editable config <span id="cfg-path" style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;"></span></h3>
    <div class="field-row">
      <div class="field">
        <label class="f-label">LLM model</label>
        <input type="text" id="cfg-model">
        <div class="hint">The Ollama model tag used for step-3 extraction (must already be pulled).</div>
      </div>
      <div class="field">
        <label class="f-label">Concurrency</label>
        <input type="number" id="cfg-concurrency">
        <div class="hint">Parallel PDF-row fetches. Keep at 1 unless your GPU has VRAM to spare.</div>
      </div>
    </div>
    <div class="field-row">
      <div class="field">
        <div class="chk-row"><input type="checkbox" id="cfg-combine"><label for="cfg-combine">Combine statements per call</label></div>
        <div class="hint">One LLM call per PDF row instead of three — faster, recommended.</div>
      </div>
      <div class="field">
        <label class="f-label">Temperature</label>
        <input type="number" step="0.1" id="cfg-temp">
        <div class="hint">Keep at 0 for deterministic extraction.</div>
      </div>
      <div class="field">
        <label class="f-label">Pilot sample size</label>
        <input type="number" id="cfg-sample">
        <div class="hint">Default company count for pilot-mode runs.</div>
      </div>
    </div>
    <div class="btngrid" style="margin-top:4px;">
      <button class="primary" onclick="saveConfig()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>Save to config.yaml</button>
    </div>
  </div>
  <div class="panel">
    <h3>Read-only paths</h3>
    <div id="cfg-readonly" style="color:var(--muted);font-size:13px;line-height:2;"></div>
  </div>
</div>

</div>
<script>
const $ = s => document.querySelector(s);
const consoleEl = $('#console');
let es = null, runStart = null, timerHandle = null;

// ---- toasts ----
function toast(msg, kind){
  const stack = $('#toast-stack');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' '+kind : '');
  el.textContent = msg;
  stack.appendChild(el);
  setTimeout(() => { el.style.opacity='0'; el.style.transition='opacity .25s ease'; setTimeout(()=>el.remove(),250); }, 3800);
}

// ---- tabs ----
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $('#view-' + t.dataset.view).classList.add('active');
  const v = t.dataset.view;
  if (v === 'browse') loadCompanies();
  if (v === 'settings') loadConfig();
  if (v === 'pipeline') loadPipeline();
  if (v === 'coverage') loadCoverage(0);
  if (v === 'errors') loadErrors();
  if (v === 'lineage' && !$('#ln-year').options.length) initLineage();
});
function scrollToStep(id){ document.getElementById(id).scrollIntoView({behavior:'smooth', block:'start'}); }

// ---- mode / advanced toggles ----
const MODE_HINTS = {
  pilot: 'Samples ~40 companies for a quick test run.',
  full: 'Processes the entire input file — the real run.',
  resume: 'Continues an existing database; already-found rows are skipped.',
};
function onModeChange(){
  const m = $('#mode').value;
  $('#mode-hint').textContent = MODE_HINTS[m] || '';
  $('#sample-size-field').style.display = (m === 'pilot') ? '' : 'none';
}
function toggleAdv(el){
  el.classList.toggle('open');
  el.nextElementSibling.classList.toggle('open');
}

// ---- status / timer ----
function setStatus(cls, txt){
  const p=$('#status-pill');
  p.className='pill '+cls;
  p.innerHTML = '<span class="dot"></span>'+txt;
}
function setRunning(on){
  document.querySelectorAll('button').forEach(b => { if(b.id!=='stop-btn') b.disabled=on; });
  $('#stop-btn').disabled = !on;
  if(on){
    runStart = Date.now();
    timerHandle = setInterval(() => {
      const s = Math.floor((Date.now()-runStart)/1000);
      $('#timer').textContent = String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
    }, 1000);
  } else if(timerHandle){ clearInterval(timerHandle); timerHandle=null; }
}

async function run(action){
  const options = {
    input: $('#input-file').value, mode: $('#mode').value,
    sample_size: $('#sample-size').value || null, no_render: $('#no-render').checked,
    force: $('#force').checked, limit: $('#limit').value || null, tickers: $('#tickers').value,
  };
  const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action, options})});
  if(r.status === 409){ const d=await r.json(); toast('Busy: '+d.label+' is already running.', 'err'); return; }
  if(!r.ok){ const d=await r.json(); toast('Error: '+(d.error||r.status), 'err'); return; }
  const d = await r.json();
  consoleEl.textContent = '';
  setRunning(true); setStatus('run', d.label);
  if(es) es.close();
  es = new EventSource('/api/stream');
  es.onmessage = e => { consoleEl.textContent += JSON.parse(e.data)+'\n'; consoleEl.scrollTop = consoleEl.scrollHeight; };
  es.addEventListener('done', e => {
    const code = JSON.parse(e.data);
    setRunning(false);
    setStatus(code===0?'ok':'err', code===0?'Done':'Exit '+code);
    es.close(); es=null; loadStats();
    if(code===0) toast(d.label+' finished successfully.', 'ok');
    else toast(d.label+' exited with code '+code+'.', 'err');
    if(action==='dashboard'){ $('#dash-frame').src = '/output/financials_dashboard.html?t='+Date.now(); }
  });
}
async function stop(){ await fetch('/api/stop', {method:'POST'}); toast('Stop requested.'); }
function copyConsole(){
  navigator.clipboard.writeText(consoleEl.textContent).then(()=>toast('Console copied to clipboard.', 'ok'));
}

// ---- stats ----
async function loadStats(){
  const s = await (await fetch('/api/stats')).json();
  const pdf = s.bySource['cse_pdf_extract'] || 0;
  const t = s.step3 || {};
  const invalid = (t.invalid_json||0)+(t.no_columns||0)+(t.llm_error||0);
  $('#stats-cards').innerHTML = `
    <div class="card c-accent"><div class="v">${(s.companies||0).toLocaleString()}</div>
      <div class="l">Companies</div><div class="s">${pdf} via PDF/LLM</div></div>
    <div class="card"><div class="v">${(s.statementLines||0).toLocaleString()}</div>
      <div class="l">Statement lines</div><div class="s">${(s.companyYears||0).toLocaleString()} company-years</div></div>
    <div class="card c-green"><div class="v">${t.ok||0}</div>
      <div class="l">Step-3 parsed ok</div><div class="s">${invalid} invalid · ${t.empty||0} empty</div></div>
    <div class="card c-amber"><div class="v">${s.consistencyWarnings||0}</div>
      <div class="l">Consistency warnings</div><div class="s">${s.pdfExtractionsUsable||0} PDF rows ready</div></div>`;
  const ready = s.pdfExtractionsUsable || 0;
  $('#step3-ready-hint').textContent = ready
    ? `${ready} PDF row(s) currently ready to extract.` : 'Run step 2 first to populate extractable text.';
}

// ---- browse ----
let coTimer=null;
function debouncedCompanies(){ clearTimeout(coTimer); coTimer=setTimeout(loadCompanies, 250); }
async function loadCompanies(){
  const src=$('#src-filter').value, q=$('#co-search').value;
  const d = await (await fetch(`/api/companies?source=${src}&q=${encodeURIComponent(q)}`)).json();
  $('#co-count').textContent = d.companies.length.toLocaleString() + ' compan' + (d.companies.length===1?'y':'ies') + ' shown';
  if(!d.companies.length){
    $('#co-body').innerHTML = '<tr><td colspan="5" class="empty-state">No companies match this filter.</td></tr>';
    return;
  }
  $('#co-body').innerHTML = d.companies.map(c =>
    `<tr onclick="viewCompany('${c.ticker.replace(/'/g,"")}')"><td><strong>${c.ticker}</strong></td>
     <td>${c.name}</td><td>${c.exchange}</td><td><span class="badge">${c.source}</span></td>
     <td>${c.latestYear??'—'}</td></tr>`).join('');
}
async function viewCompany(ticker){
  const el = $('#co-detail'); el.innerHTML = '<div class="empty-state">Loading '+ticker+'…</div>';
  const [cRes, dRes] = await Promise.all([
    fetch('/api/company/'+encodeURIComponent(ticker)),
    fetch('/api/diagnostics/'+encodeURIComponent(ticker))]);
  if(!cRes.ok){ el.innerHTML = '<div class="empty-state err-c">'+ticker+' not found</div>'; return; }
  const c = await cRes.json(), diag = await dRes.json();
  const okMark = '<svg class="ic" style="width:13px;height:13px;display:inline-block;vertical-align:-2px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  const noMark = '<svg class="ic" style="width:13px;height:13px;display:inline-block;vertical-align:-2px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  const badge = c.schemaValid ? '<span class="ok-c">'+okMark+' schema valid</span>'
    : '<span class="err-c">'+noMark+' '+(c.schemaError||'invalid')+'</span>';
  let diagHtml = '';
  if(diag.status.length){
    diagHtml += '<h3 style="margin-top:16px;text-transform:none;letter-spacing:0;">Step-3 status</h3><table><thead><tr><th>Yr</th><th>Statement</th>'
      +'<th>Status</th><th>#</th><th>Reason</th></tr></thead><tbody>'
      + diag.status.map(s=>`<tr style="cursor:default"><td>${s.fiscalYear}</td><td>${s.statementType}</td>
        <td class="${s.status==='ok'?'ok-c':'err-c'}">${s.status}</td><td>${s.nLines??''}</td>
        <td style="color:var(--muted);font-size:11px;">${s.reason||''}</td></tr>`).join('')
      + '</tbody></table>';
  }
  if(diag.consistency.length){
    diagHtml += '<h3 style="margin-top:16px;text-transform:none;letter-spacing:0;">Consistency flags</h3>'
      + diag.consistency.map(x=>`<div style="font-size:12px;margin-bottom:8px;">
        <span class="badge">${x.pattern}</span> <code style="color:var(--muted);">${x.conceptGroup}</code>
        <div style="color:var(--muted);font-size:11px;margin-top:2px;">${x.keysUsed}</div></div>`).join('');
  }
  el.innerHTML = `<div class="field-row" style="justify-content:space-between;align-items:center;margin-bottom:10px;">
      <strong style="font-size:16px;">${ticker}</strong> ${badge}</div>
    <pre class="json">${JSON.stringify(c.document, null, 2)}</pre>${diagHtml}`;
}

// ---- settings ----
async function loadConfig(){
  const c = await (await fetch('/api/config')).json();
  $('#cfg-path').textContent = '('+c.path+')';
  $('#cfg-model').value = c.editable['llm.model'] ?? '';
  $('#cfg-concurrency').value = c.editable['llm.concurrency'] ?? '';
  $('#cfg-combine').checked = !!c.editable['llm.combine_statements'];
  $('#cfg-temp').value = c.editable['llm.temperature'] ?? '';
  $('#cfg-sample').value = c.editable['pilot.default_sample_size'] ?? '';
  $('#cfg-readonly').innerHTML = Object.entries(c.readonly)
    .map(([k,v])=>`<div><strong style="color:var(--text);">${k}</strong>: ${v}</div>`).join('');
}
async function saveConfig(){
  const body = {
    'llm.model': $('#cfg-model').value,
    'llm.concurrency': $('#cfg-concurrency').value,
    'llm.combine_statements': $('#cfg-combine').checked,
    'llm.temperature': $('#cfg-temp').value,
    'pilot.default_sample_size': $('#cfg-sample').value,
  };
  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)});
  const d = await r.json();
  toast(r.ok ? ('Saved to '+d.path) : ('Error: '+d.error), r.ok ? 'ok' : 'err');
}

// ================= OPS: helpers =================
function fmtN(v){ return (v==null?0:v).toLocaleString(); }
function big(v){ if(v==null) return '—'; const a=Math.abs(v);
  if(a>=1e9)return (v<0?'-':'')+(a/1e9).toFixed(2)+'B'; if(a>=1e6)return (v<0?'-':'')+(a/1e6).toFixed(1)+'M';
  if(a>=1e3)return (v<0?'-':'')+(a/1e3).toFixed(1)+'K'; return (+v).toFixed(2); }

// ================= OPS: pipeline DAG =================
let dagStages = [], dagSel = null;
async function loadPipeline(){
  const d = await (await fetch('/api/pipeline')).json();
  dagStages = d.stages;
  const rail = $('#dag-rail'); rail.innerHTML = '';
  d.stages.forEach((s, i) => {
    if(i>0){ const conn=document.createElement('div'); conn.className='dag-conn'; rail.appendChild(conn); }
    const tot = Math.max(s.total, s.success+s.failed+s.review, 1);
    const node = document.createElement('div');
    node.className = 'dag-node' + (s.success===0?' z':'') + (s.id===dagSel?' sel':'');
    node.dataset.id = s.id;
    const chips = [];
    if(s.failed) chips.push(`<span class="n-chip err">${fmtN(s.failed)} failed</span>`);
    if(s.review) chips.push(`<span class="n-chip warn">${fmtN(s.review)} review</span>`);
    node.innerHTML = `<div class="n-label">${s.label}</div>
      <div class="n-desc">${s.desc}</div>
      <div class="n-main">${fmtN(s.success)}</div>
      <div class="n-sub">of ${fmtN(s.total)} · ${(s.success/tot*100).toFixed(0)}%</div>
      <div class="n-chips">${chips.join('')||'<span class="n-chip" style="background:var(--green-dim);color:var(--green)">clean</span>'}</div>
      <div class="n-bar">
        <i style="width:${s.success/tot*100}%;background:var(--green)"></i>
        <i style="width:${s.review/tot*100}%;background:var(--amber)"></i>
        <i style="width:${s.failed/tot*100}%;background:var(--red)"></i></div>`;
    node.onclick = () => selectStage(s.id);
    rail.appendChild(node);
  });
  applyActiveStages();
}
function selectStage(id){
  dagSel = (dagSel===id?null:id);
  document.querySelectorAll('.dag-node').forEach(n => n.classList.toggle('sel', n.dataset.id===dagSel));
  const s = dagStages.find(x=>x.id===dagSel);
  const panel = $('#dag-detail-panel');
  if(!s){ panel.style.display='none'; return; }
  panel.style.display='';
  $('#dag-detail-title').textContent = s.label + ' — breakdown';
  const TONE = {ok:'var(--green)', err:'var(--red)', warn:'var(--amber)', muted:'var(--muted)'};
  $('#dag-detail-body').innerHTML = s.breakdown.map(b =>
    `<div style="display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--line);">
      <span style="color:${TONE[b.tone]||'var(--ink)'}">${b.label}</span>
      <strong style="font-family:var(--mono)">${fmtN(b.count)}</strong></div>`).join('')
    + (['discovery','extract','normalize','financials'].includes(s.id)
       ? `<div style="margin-top:12px"><button class="small ghost" onclick="gotoErrors()">View failures in Error Center →</button></div>` : '');
}
function gotoErrors(){ document.querySelector('.tab[data-view="errors"]').click(); }
let activeStages = [];
function applyActiveStages(){
  document.querySelectorAll('.dag-node').forEach(n =>
    n.classList.toggle('active', activeStages.includes(n.dataset.id)));
}

// ================= OPS: coverage heatmap =================
let covPage = 0, covTimer = null;
function covDebounce(){ clearTimeout(covTimer); covTimer=setTimeout(()=>loadCoverage(0), 300); }
async function initCoverageFilters(){
  const f = await (await fetch('/api/coverage/filters')).json();
  const opt = (arr, all) => [`<option value="all">${all}</option>`].concat(arr.map(x=>`<option>${x}</option>`)).join('');
  $('#cov-exchange').innerHTML = opt(f.exchanges, 'All exchanges');
  $('#cov-source').innerHTML = opt(f.sources, 'All sources');
  $('#cov-status').innerHTML = opt(f.statuses, 'All statuses');
}
async function loadCoverage(page){
  if(page<0) return;
  if(!$('#cov-exchange').options.length) await initCoverageFilters();
  const qs = new URLSearchParams({exchange:$('#cov-exchange').value, source:$('#cov-source').value,
    status:$('#cov-status').value, q:$('#cov-q').value, page, per:60});
  const d = await (await fetch('/api/coverage?'+qs)).json();
  covPage = d.page;
  $('#hm-summary').innerHTML = d.summary.map(s =>
    `<div class="hm-ybar"><div class="y">${s.year}</div><div class="p">${s.pct}%</div>
     <div class="track"><i style="width:${s.pct}%"></i></div></div>`).join('');
  const g = $('#hm-grid');
  g.innerHTML = `<thead><tr><th class="tkh">Company</th>${d.years.map(y=>`<th>${y}</th>`).join('')}</tr></thead>`
    + '<tbody>' + d.rows.map(r =>
      `<tr><td class="tk"><b>${r.ticker}</b> <small>${r.name}</small></td>` +
      d.years.map(y=>`<td><div class="hm-cell c-${r.cells[String(y)]}" title="${r.ticker} ${y}: ${r.cells[String(y)]}"
        onclick="cellTrace('${r.ticker.replace(/'/g,'')}',${y})"></div></td>`).join('') + '</tr>').join('')
    + '</tbody>';
  const pages = Math.max(1, Math.ceil(d.total/d.per));
  $('#cov-pageinfo').textContent = `Page ${covPage+1} of ${pages} · ${fmtN(d.total)} companies`;
  $('#cov-prev').disabled = covPage<=0; $('#cov-next').disabled = covPage>=pages-1;
}
function cellTrace(ticker, year){
  document.querySelector('.tab[data-view="lineage"]').click();
  $('#ln-ticker').value = ticker;
  traceLineage(year);
}

// ================= OPS: error center =================
let errItems = [], errSel = null;
async function loadErrors(){
  const d = await (await fetch('/api/errors')).json();
  errItems = d.items;
  const TONE = {err:'var(--red)', warn:'var(--amber)', muted:'var(--muted2)'};
  const total = d.total||1;
  $('#err-tree').innerHTML = d.cards.map(c =>
    `<i style="flex:${c.count};background:${TONE[c.tone]}" title="${c.category}: ${c.count}"
       onclick="filterErr('${c.category.replace(/'/g,'')}')"><span>${c.count/total>0.06?c.count:''}</span></i>`).join('');
  $('#err-cards').innerHTML = d.cards.map(c =>
    `<div class="err-card ${c.tone}" onclick="filterErr('${c.category.replace(/'/g,'')}')">
      <div class="c">${fmtN(c.count)}</div><div class="t">${c.category}</div>
      <div class="st">${c.stage}</div></div>`).join('');
  renderErrTable(null);
}
function filterErr(cat){ errSel = (errSel===cat?null:cat); renderErrTable(errSel);
  document.querySelectorAll('.err-card').forEach(c=>c.classList.toggle('sel', errSel && c.querySelector('.t').textContent===errSel)); }
function renderErrTable(cat){
  const rows = errItems.filter(it => !cat || it.category===cat);
  $('#err-table-title').textContent = `Failed jobs${cat?' — '+cat:''} (${rows.length})`;
  $('#err-body').innerHTML = rows.slice(0,300).map(it =>
    `<tr style="cursor:default"><td><b>${it.ticker}</b>${it.year?' <small style="color:var(--muted2)">FY'+it.year+'</small>':''}</td>
     <td><span class="badge">${it.stage}</span></td><td>${it.category}</td>
     <td style="color:var(--muted);font-size:12px">${it.reason}</td>
     <td style="white-space:nowrap">${it.retryable?`<button class="small ghost" onclick="retryJob('${it.ticker.replace(/'/g,'')}')">Retry</button> `:''}<button class="small ghost" onclick="lineageFor('${it.ticker.replace(/'/g,'')}',${it.year||'null'})">Trace</button></td></tr>`).join('')
    || '<tr><td colspan="5" class="empty-state">No failures in this category.</td></tr>';
}
async function retryJob(ticker){
  const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'llm_extract', options:{force:true, tickers:ticker}})});
  if(r.status===409){ toast('Busy — a job is already running.', 'err'); return; }
  toast('Re-running LLM extraction for '+ticker+'. See the Run tab console.', 'ok');
  document.querySelector('.tab[data-view="run"]').click();
  const d = await r.json(); consoleEl.textContent=''; setRunning(true); setStatus('run', d.label);
  if(es) es.close(); es = new EventSource('/api/stream');
  es.onmessage = e => { consoleEl.textContent += JSON.parse(e.data)+'\n'; consoleEl.scrollTop = consoleEl.scrollHeight; };
  es.addEventListener('done', e => { setRunning(false); const c=JSON.parse(e.data);
    setStatus(c===0?'ok':'err', c===0?'Done':'Exit '+c); es.close(); es=null; loadStats(); });
}
function lineageFor(ticker, year){ document.querySelector('.tab[data-view="lineage"]').click();
  $('#ln-ticker').value = ticker; traceLineage(year); }

// ================= OPS: lineage =================
function initLineage(){ if(!$('#ln-ticker').value) $('#ln-ticker').value = 'AAA.P'; }
async function traceLineage(forceYear){
  const ticker = $('#ln-ticker').value.trim().toUpperCase();
  if(!ticker){ toast('Enter a ticker to trace.', 'err'); return; }
  const yr = (typeof forceYear==='number') ? forceYear : ($('#ln-year').value || '');
  const d = await (await fetch(`/api/lineage?ticker=${encodeURIComponent(ticker)}&year=${yr}`)).json();
  if(d.error){ $('#ln-panel').style.display='none'; $('#ln-items').innerHTML=''; toast(ticker+': '+d.error, 'err'); return; }
  $('#ln-year').innerHTML = d.years.map(y=>`<option ${y==d.fy?'selected':''}>${y}</option>`).join('');
  $('#ln-items').innerHTML = d.lines.map(li =>
    `<button class="small ghost" onclick="traceValue('${li.item}')" title="${li.item} = ${big(li.value)}">${li.item}</button>`).join('')
    || '<span class="empty-state">No line items for this year.</span>';
  renderChain(d.chain, ticker, d.fy);
}
async function traceValue(item){
  const ticker = $('#ln-ticker').value.trim().toUpperCase();
  const yr = $('#ln-year').value || '';
  const d = await (await fetch(`/api/lineage?ticker=${encodeURIComponent(ticker)}&year=${yr}&item=${encodeURIComponent(item)}`)).json();
  document.querySelectorAll('#ln-items button').forEach(b=>b.classList.toggle('on', b.textContent===item));
  renderChain(d.chain, ticker, d.fy);
}
function renderChain(chain, ticker, fy){
  $('#ln-panel').style.display='';
  $('#ln-title').textContent = `Provenance — ${ticker} · FY${fy}`;
  $('#ln-chain').innerHTML = chain.map(s =>
    `<div class="ln-step tone-${s.tone}"><div class="st-title">${s.title}</div>
     <div class="st-detail">${s.detail}</div>
     <div class="st-meta">${s.meta||''}${s.url?` · <a href="${s.url}" target="_blank">open source ↗</a>`:''}</div></div>`).join('');
}

// ---- custom cursor + cursor-reactive background ----
(function(){
  const fine = matchMedia('(hover:hover) and (pointer:fine)').matches
    && !matchMedia('(prefers-reduced-motion:reduce)').matches;
  if(!fine) return;
  const dot = $('#cur-dot'), ring = $('#cur-ring'), root = document.documentElement;
  let mx = innerWidth/2, my = innerHeight/2, rx = mx, ry = my;
  addEventListener('pointermove', e => {
    mx = e.clientX; my = e.clientY;
    root.style.setProperty('--mx', mx+'px');
    root.style.setProperty('--my', my+'px');
    dot.style.transform = `translate(${mx}px,${my}px)`;
  }, {passive:true});
  addEventListener('pointerdown', () => document.body.classList.add('cur-down'));
  addEventListener('pointerup', () => document.body.classList.remove('cur-down'));
  const HOT = 'button,a,.tab,.step-item,tr,input,select,.adv-toggle,label';
  addEventListener('pointerover', e => { if(e.target.closest(HOT)) document.body.classList.add('cur-hot'); });
  addEventListener('pointerout', e => { if(e.target.closest(HOT)) document.body.classList.remove('cur-hot'); });
  (function loop(){
    rx += (mx-rx)*0.16; ry += (my-ry)*0.16;
    ring.style.transform = `translate(${rx}px,${ry}px)`;
    requestAnimationFrame(loop);
  })();
})();

// ---- live active-stage poller (drives the DAG glow) ----
let lastRunning = false;
async function pollStatus(){
  try{
    const s = await (await fetch('/api/status')).json();
    activeStages = s.activeStages || [];
    applyActiveStages();
    if(lastRunning && !s.running){ loadStats(); if($('#view-pipeline').classList.contains('active')) loadPipeline(); }
    lastRunning = s.running;
  }catch(e){}
}
setInterval(pollStatus, 2500);

onModeChange();
loadStats();
loadPipeline();
</script>
</body></html>
"""
