#!/usr/bin/env python3
"""
scripts/revert_gem_sales.py

The one-time repair for gemstone sales made before 1.2 removed gemstones from
the market (docs/market.md section 3).

WHAT WENT WRONG

A ruby's market_ceiling_price is 5,500 and a diamond's is 500,000, against iron
ore at 0.0106. The server's buy price halves at that material's target stock,
but target stock for a gemstone is max(1, round(members * 0.015)) - which stays
at 1 ruby on any server of realistic size. So the curve that is meant to damp
repeated sales barely engaged: the first four rubies sold into a small server
paid 5,500 + 2,750 + 1,833 + 1,375 = 11,458.

For scale, a full day's job board task pays a little over 1.00, and the first
three rungs of the infrastructure ladder are 5, 25 and 125. A single gem sale
did not skew a server's economy, it ended it: every price, fee and threshold in
that server became meaningless in one command.

WHAT THIS DOES

Per server, for every user holding more than BALANCE_THRESHOLD:

  1. sets their balance to 0, and records the removal as burned currency so the
     faucet/sink accounting in docs/market.md section 4 stays honest;
  2. credits them REFUND_MATERIALS - the gem back, since they did genuinely mine
     or press it and the sale is what is being undone, not the gem;
  3. removes the server's own gemstone stock, because the server bought those
     with the currency being reversed and nobody can buy them back now that
     gemstones have left the market.

WHAT IT CANNOT DO, AND WHY THE THRESHOLD IS A HEURISTIC

There is no transaction log anywhere in the schema - no table records who sold
what to whom. server_material_storage proves which SERVERS took gemstones in
and how many, and that is exact; attributing those sales to a USER is not
possible from the data, so a balance threshold stands in for it.

That heuristic is wrong in three known directions, and the dry run prints
enough to see all three before committing:

  - Someone who sold a gem and then SPENT the currency is under the threshold
    and keeps both the goods and the gains.
  - Someone who sold several gems loses far more than REFUND_MATERIALS returns
    (four rubies is 11,458; they get one ruby back).
  - Someone who legitimately accumulated past the threshold without ever
    touching a gemstone loses all of it. At the rates above this is close to
    impossible, but "close to" is doing real work in that sentence.

Read the dry run against the SERVER GEMSTONE STOCK section before applying. If
the number of users caught doesn't roughly match the number of gems the server
took in, the threshold is wrong for that server and should be tuned here rather
than worked around.

RUNNING IT - AND WHEN

STOP THE BOT FIRST. Not for safety of the writes (the balance guard below
handles a concurrent change by aborting), but because there is a window on
either side of the deploy and stopping the service is what closes both:

  - Run it BEFORE 1.2 is deployed and the market still buys gemstones, so
    somebody can sell another one between the sweep and the restart.
  - Run it AFTER 1.2 is live and /donate exists, so somebody sitting on gem
    proceeds can move them out of reach - to another player, whose balance is
    then under the threshold, or into a machine. The second one is not
    recoverable by re-running this: it zeroes balances, and nothing here
    un-levels a machine. Under 1.2's fee ladder a dodged 5,500 dumped into one
    machine takes it from level 1 to level 6, permanently.

So the order is: stop, back up, put the new code on disk, sweep, start.

    sudo systemctl stop dragonhoard
    cd /opt/dragonhoard
    sudo -u dragonbot venv/bin/python -c "
    import sqlite3
    src = sqlite3.connect('data/dragonhoard.db')
    dst = sqlite3.connect('data/backup-before-gem-revert.db')
    with dst: src.backup(dst)
    dst.close(); src.close()"
    git pull origin main
    venv/bin/python scripts/revert_gem_sales.py --db data/dragonhoard.db          # read this
    venv/bin/python scripts/revert_gem_sales.py --db data/dragonhoard.db --apply
    sudo systemctl start dragonhoard

Note update.sh is NOT the tool for this deploy: it pulls and restarts in one
step, leaving no gap to sweep in. Its backup step is the one worth copying
though, and is what the command above is - a real sqlite3 .backup() rather than
a cp, because the database is in WAL mode and a plain file copy can catch it
mid-write and produce a torn snapshot.

Dry run is the default and the only mode that runs without --apply.

This script does NOT depend on 1.2's migration having run - every table it
touches (server_currency_balances, server_config, server_material_storage,
user_materials) exists unchanged in both schema versions. So it works whether
the bot has been started on the new code or not, which is what makes "sweep
while stopped" possible at all.

Idempotent by construction rather than by a marker: it acts on balances above
the threshold and on gemstone stock, and after a successful run there is
neither. Running it twice is a no-op, and running it later catches nothing new
because /market sell can no longer accept a gemstone at all.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# Let the script import the bot's own modules when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.materials import GEMSTONES, RAW_MATERIALS  # noqa: E402

# Above this, a balance is treated as gemstone proceeds. Originally set to
# 2,500 against the sale ladder (the fourth successive ruby into a stocked-out
# server still pays 1,375), but confirmed sellers who spent proceeds back down
# to 2,300 slipped under that line and would have kept both the ruby and the
# spend. Dropped to 500: still an order of magnitude above anything the
# legitimate economy produced at the time (the job board pays ~1.00/day), with
# margin below every known case for anyone who spent even further down.
BALANCE_THRESHOLD = 500.0

# What each caught user gets back. One ruby, matching the sale being undone:
# the currency is reversed, so the goods have to come back with it.
REFUND_MATERIALS = {"ruby": 1}


def rows_to_revert(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT guild_id, user_id, balance FROM server_currency_balances "
        "WHERE balance > ? ORDER BY guild_id, balance DESC",
        (BALANCE_THRESHOLD,),
    ).fetchall()


def gem_stock(conn: sqlite3.Connection):
    placeholders = ",".join("?" * len(GEMSTONES))
    return conn.execute(
        f"SELECT guild_id, material_id, quantity FROM server_material_storage "
        f"WHERE material_id IN ({placeholders}) AND quantity > 0 "
        f"ORDER BY guild_id, material_id",
        tuple(GEMSTONES),
    ).fetchall()


def report(conn: sqlite3.Connection) -> tuple[list, list]:
    """Prints what a run would do and returns the two row sets it would act on."""
    stock = gem_stock(conn)
    print("=== SERVER GEMSTONE STOCK (what the market actually took in) ===")
    if not stock:
        print("  none - no server ever bought a gemstone")
    for row in stock:
        # What the server paid for these, near enough: the first unit of a
        # gemstone costs full ceiling price on any server small enough for
        # target stock to be 1, which is all of them so far.
        ceiling = RAW_MATERIALS[row["material_id"]]["market_ceiling_price"]
        print(
            f"  guild {row['guild_id']}  {row['material_id']:9s} x{row['quantity']}"
            f"   (first unit alone paid ~{ceiling:,.2f})"
        )

    balances = rows_to_revert(conn)
    print()
    print(f"=== BALANCES OVER {BALANCE_THRESHOLD:,.2f} (what would be zeroed) ===")
    if not balances:
        print("  none")
    for row in balances:
        print(
            f"  guild {row['guild_id']}  user {row['user_id']}  "
            f"balance {row['balance']:,.2f}  -> 0.00, refund {REFUND_MATERIALS}"
        )

    print()
    print("=== EVERY BALANCE, FOR CONTEXT ===")
    for row in conn.execute(
        "SELECT guild_id, user_id, balance FROM server_currency_balances "
        "ORDER BY guild_id, balance DESC"
    ):
        flag = "  <-- CAUGHT" if row["balance"] > BALANCE_THRESHOLD else ""
        print(f"  guild {row['guild_id']}  user {row['user_id']}  {row['balance']:,.2f}{flag}")

    return balances, stock


def apply(conn: sqlite3.Connection, balances, stock) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in balances:
            # Guarded on the balance we read, so a sale landing between the
            # report and the write can't be silently swallowed by a blind
            # UPDATE ... SET balance = 0.
            changed = conn.execute(
                "UPDATE server_currency_balances SET balance = 0 "
                "WHERE guild_id = ? AND user_id = ? AND balance = ?",
                (row["guild_id"], row["user_id"], row["balance"]),
            ).rowcount
            if not changed:
                raise RuntimeError(
                    f"balance for user {row['user_id']} in guild {row['guild_id']} "
                    f"changed while this was running - nothing has been committed, "
                    f"re-run the dry run"
                )
            # Currency leaving circulation is a burn, and section 4's ledger is
            # only meaningful if every removal goes through it.
            conn.execute(
                "UPDATE server_config SET currency_burned_total = currency_burned_total + ? "
                "WHERE guild_id = ?",
                (row["balance"], row["guild_id"]),
            )
            for material_id, quantity in REFUND_MATERIALS.items():
                conn.execute(
                    "INSERT INTO users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
                    (row["user_id"],),
                )
                conn.execute(
                    "INSERT INTO user_materials (user_id, material_id, quantity) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, material_id) DO UPDATE "
                    "SET quantity = quantity + excluded.quantity",
                    (row["user_id"], material_id, quantity),
                )

        for row in stock:
            conn.execute(
                "UPDATE server_material_storage SET quantity = 0 "
                "WHERE guild_id = ? AND material_id = ?",
                (row["guild_id"], row["material_id"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    parser.add_argument("--db", required=True, help="path to the database file")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write. Without it this only reports, which is the default "
             "precisely because there is no undo.",
    )
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"No such database: {path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        balances, stock = report(conn)
        print()
        if not args.apply:
            print("DRY RUN - nothing was written. Re-run with --apply to commit.")
            print("Stop the bot and take a real sqlite3 .backup() first - see this")
            print("script's module docstring for the full sequence and why the order")
            print("matters (/donate can move gem proceeds out of reach once 1.2 is up).")
            return 0
        if not balances and not stock:
            print("Nothing to do.")
            return 0
        apply(conn, balances, stock)
        print(
            f"APPLIED: zeroed {len(balances)} balance(s), "
            f"cleared {len(stock)} gemstone stock row(s)."
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
