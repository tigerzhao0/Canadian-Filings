#!/usr/bin/env python3
"""Local control-panel GUI — a friendly front end to run.py.

    python gui.py                 # serve on http://127.0.0.1:5000 and open a browser
    python gui.py --port 8080     # a different port
    python gui.py --no-browser    # don't auto-open a browser (e.g. headless / preview)

It's a thin Flask app (src/webapp.py) that shells out to the same run.py CLI and
export scripts, streaming their output live, plus read-only DB browsing. Nothing
here duplicates pipeline logic -- it just drives the CLI. Localhost only.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

# The project modules live in src/ (kept flat so their inter-imports stay bare,
# e.g. `import company_export`). Put src/ on the path before importing webapp.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the Canadian Filings control panel.")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address (default 127.0.0.1 -- localhost only; do not "
                         "expose this to a network, it runs local shell commands).")
    ap.add_argument("--no-browser", action="store_true",
                    help="Don't auto-open a browser tab.")
    args = ap.parse_args()

    from webapp import create_app
    app = create_app()

    url = f"http://{args.host}:{args.port}/"
    print(f"Canadian Filings control panel: {url}")
    print("  (Ctrl+C to stop. Localhost only -- do not expose to a network.)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # threaded=True so the SSE stream endpoint doesn't block other requests
    # (stats/companies polling) while a job streams. use_reloader=False so we
    # don't spawn a second process that would double-run background threads.
    app.run(host=args.host, port=args.port, threaded=True,
            debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
