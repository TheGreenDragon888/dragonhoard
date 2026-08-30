"""
web/app.py

Dragonhoard Ops: a private, read-only developer dashboard over
dragonhoard.db. Run it as its own tiny process alongside the bot (see
web/README.md) - it never writes to the database and never talks to
Discord, so it can be opened to a browser without carrying any of the
bot's own credentials beyond what config.py already loads.

    uvicorn web.app:app --host 127.0.0.1 --port 8420

The single endpoint, /api/ops, opens the SQLite file in SQLite's own
mode=ro (read-only, refuses to create the file if it's missing) rather than
just avoiding writes in code - a real guarantee at the driver level, not a
convention this file could later violate by accident.
"""
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from web.queries import build_payload

app = FastAPI(title="Dragonhoard Ops")


def _connect_readonly() -> sqlite3.Connection:
    db_path = Path(config.DATABASE_PATH).resolve()
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} does not exist. Run this alongside a bot instance that "
            f"has already created it, or point DATABASE_PATH at one that has."
        )
    # uri=True + mode=ro: SQLite itself refuses any write this process might
    # accidentally attempt, rather than relying on every query here being
    # hand-audited as read-only forever.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/ops")
def get_ops(
    anonymize: bool = Query(False),
    hideDeparted: bool = Query(True),
    dormantDays: int = Query(14, ge=1, le=90),
    burnFloor: float = Query(15, ge=0, le=60),
    stalledDays: int = Query(5, ge=1, le=30),
):
    try:
        conn = _connect_readonly()
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    try:
        payload = build_payload(
            conn,
            anonymize=anonymize,
            hide_departed=hideDeparted,
            dormant_days=dormantDays,
            burn_floor=burnFloor,
            stalled_days=stalledDays,
        )
    finally:
        conn.close()
    return payload


# Serves web/static/ at "/" - index.html, app.js, styles.css, the vendored
# design-system tokens, and the branding assets. html=True makes "/" resolve
# to index.html. Mounted last: FastAPI matches routes in registration order,
# and /api/ops has to win over the catch-all static mount.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
