"""
database/db.py

A small wrapper around Python's built-in `sqlite3` module.

Why this exists: discord.py runs on asyncio (everything is `async def`), but
Python's standard sqlite3 library is synchronous/blocking. Calling a blocking
function directly inside an async function would freeze the ENTIRE bot
(all servers, all users) while that one query runs. To avoid that, every
query here is pushed to a background thread via `asyncio.to_thread`, so the
bot keeps responding to other events while a query is in flight.

You won't need to touch this file often - cogs import `Database` and call
`fetchone`, `fetchall`, or `execute`.

Those three each open their own connection and commit on their own, which is
fine for a single standalone statement but NOT for an operation made of
several. Anything that reads a value, decides something from it, and then
writes - selling materials, paying a fee, swapping a container - has to run
inside `async with db.transaction()` instead. See Database.transaction for
what goes wrong otherwise.
"""
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import config

# How long a statement waits for another writer to finish before giving up.
# Generous because the alternative - an OperationalError surfacing mid-command
# - is far worse than a slow command, and because a transaction now holds the
# write lock for the length of a whole operation rather than one statement.
BUSY_TIMEOUT_SECONDS = 30.0


class InsufficientQuantity(Exception):
    """Raised when a deduction would take an inventory below zero. Raised
    rather than clamped so it aborts (and rolls back) the operation that asked
    for it, instead of quietly destroying the difference."""


class _Executor:
    """The query surface shared by Database and Transaction, so the helpers in
    utils/db_helpers.py work identically whether they're handed a Database (one
    connection per statement) or a Transaction (one shared connection)."""

    async def execute(self, query: str, params: tuple = ()) -> int:
        raise NotImplementedError

    async def execute_changes(self, query: str, params: tuple = ()) -> int:
        raise NotImplementedError

    async def fetchone(self, query: str, params: tuple = ()):
        raise NotImplementedError

    async def fetchall(self, query: str, params: tuple = ()):
        raise NotImplementedError


class Transaction(_Executor):
    """One operation's worth of queries against a single connection inside a
    single transaction. Created by Database.transaction(); not instantiated
    directly."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _execute_sync(self, query: str, params: tuple):
        return self._conn.execute(query, params)

    async def execute(self, query: str, params: tuple = ()) -> int:
        cur = await asyncio.to_thread(self._execute_sync, query, params)
        return cur.lastrowid

    async def execute_changes(self, query: str, params: tuple = ()) -> int:
        """Runs a statement and reports how many rows it actually changed -
        the difference between "the guarded UPDATE applied" and "its WHERE
        clause matched nothing"."""
        cur = await asyncio.to_thread(self._execute_sync, query, params)
        return cur.rowcount

    async def fetchone(self, query: str, params: tuple = ()):
        cur = await asyncio.to_thread(self._execute_sync, query, params)
        return cur.fetchone()

    async def fetchall(self, query: str, params: tuple = ()):
        cur = await asyncio.to_thread(self._execute_sync, query, params)
        return cur.fetchall()


class Database(_Executor):
    def __init__(self, path: str):
        self.path = path
        # Make sure the parent folder (e.g. "data/") exists before sqlite3
        # tries to create the .db file inside it.
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Serialises transactions against each other. SQLite allows only one
        # writer at a time anyway, so queueing them here means waiting on an
        # asyncio lock instead of on SQLITE_BUSY, and it's what makes a
        # transaction's reads and writes safe to interleave with the rest of
        # the bot: another command can't slip a write in partway through.
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False because a transaction's connection outlives
        # a single asyncio.to_thread call, and the thread pool hands successive
        # statements to different workers. Safe here: the statements within a
        # transaction are awaited one at a time, and _write_lock stops two
        # transactions from sharing a connection.
        conn = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False
        )
        # row_factory makes query results behave like dicts (row["column"])
        # instead of plain tuples (row[0], row[1]...) - much easier to read.
        conn.row_factory = sqlite3.Row
        # Enforces foreign key constraints (off by default in SQLite).
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers carry on while a write is in flight. Under the
        # default journal mode a writer blocks every reader, and with four
        # background loops plus user commands all querying constantly, that
        # contention is the likeliest way a statement fails partway through an
        # operation. The setting is a property of the database file, so it
        # persists once set; re-applying it per connection is harmless.
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @asynccontextmanager
    async def transaction(self):
        """Runs everything in the block against one connection, committing at
        the end and rolling back if anything raises.

        Use this whenever an operation reads something and then writes based on
        what it read. Two problems come from not doing so, and only this fixes
        both:

        Torn writes - `execute` commits each statement separately, so an
        exception partway through leaves the earlier ones applied. Selling
        materials would take the items and never pay for them.

        Read-then-write races - discord.py runs each command as its own task,
        so two invocations can interleave at any `await`. Both read "you have
        10", both pass validation, and both deduct: the player is paid twice
        for one stack. Wrapping only the writes would not help, because the
        read that authorised them happened outside. The reads have to be in
        here too.

        Do NOT do network work inside the block - awaiting Discord (say,
        human_member_count chunking a guild) while holding the write lock
        stalls every other command. Gather that first, then open the
        transaction.
        """
        async with self._write_lock:
            conn = await asyncio.to_thread(self._connect)
            try:
                # IMMEDIATE takes the write lock up front rather than on the
                # first write, so a transaction that reads and later writes
                # can't have another writer commit underneath it in between.
                await asyncio.to_thread(conn.execute, "BEGIN IMMEDIATE")
                yield Transaction(conn)
                await asyncio.to_thread(conn.commit)
            except BaseException:
                await asyncio.to_thread(conn.rollback)
                raise
            finally:
                await asyncio.to_thread(conn.close)

    # The drills table as schema.sql declares it, for the rebuild below.
    # SQLite can add a nullable column in place but cannot relax a NOT NULL,
    # so turning guild_id nullable means building a new table and swapping it.
    # Keep this in sync with schema.sql - it's the same DDL minus IF NOT EXISTS.
    _DRILLS_REBUILD_DDL = """
        CREATE TABLE drills_new (
            drill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id         INTEGER,
            owner_id         INTEGER NOT NULL,
            drill_type       TEXT NOT NULL,
            level            INTEGER NOT NULL DEFAULT 1,
            container_type   TEXT,
            stored_amount    INTEGER NOT NULL DEFAULT 0,
            harvest_progress REAL NOT NULL DEFAULT 0.0,
            is_full          INTEGER NOT NULL DEFAULT 0,
            locked_job_id    INTEGER,
            CHECK (level >= 1),
            CHECK (guild_id IS NOT NULL OR (stored_amount = 0 AND is_full = 0))
        )
    """

    # production_jobs as schema.sql declares it, for the rebuild below. SQLite
    # cannot widen a CHECK constraint in place, so admitting another job type
    # means building a new table and swapping it. Keep in sync with schema.sql -
    # a fresh database gets that CHECK and a migrated one gets this, so the two
    # have to name exactly the same job types.
    _PRODUCTION_JOBS_REBUILD_DDL = """
        CREATE TABLE production_jobs_new (
            job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            job_type        TEXT NOT NULL CHECK (job_type IN ('furnace', 'blast_furnace', 'factory', 'press', 'scrapper')),
            target_id       TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            queued_at       TEXT NOT NULL DEFAULT (datetime('now')),
            status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'in_progress', 'complete')),
            target_drill_id INTEGER
        )
    """

    def _init_schema_sync(self):
        schema_path = Path(__file__).parent / "schema.sql"
        with self._connect() as conn:
            conn.executescript(schema_path.read_text())

            # Migrations for databases created before a schema change go here,
            # guarded so they only run once. Keep each until every deployed
            # database has been started on the new code at least once.
            #
            # Structural changes are gated on introspecting the live table;
            # pure data changes are gated on user_version. The distinction
            # matters because a brand-new database has user_version 0 AND the
            # current table shape, so a version-only gate would try to rebuild
            # a table that is already correct.
            #
            # sqlite3's legacy isolation mode opens a transaction before DML
            # but NOT before DDL, so a table rebuild left to the defaults would
            # commit its CREATE and then strand a half-built drills_new if a
            # later statement threw. Switching to autocommit makes the explicit
            # BEGIN/COMMIT below mean exactly what they say.
            conn.isolation_level = None
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            drill_columns = {row[1] for row in conn.execute("PRAGMA table_info(drills)")}
            job_columns = {row[1] for row in conn.execute("PRAGMA table_info(production_jobs)")}

            if "last_harvest_at" in drill_columns:
                # Was never read or written anywhere - harvesting is driven
                # entirely by the tick loop, not per-drill timestamps.
                conn.execute("ALTER TABLE drills DROP COLUMN last_harvest_at")

            # Fee defaults changed from 0.0 to the config.DEFAULT_*_FEE values.
            # Bump servers still sitting on the old default; guarded by
            # user_version so a fee a manager later sets back to 0 stays 0.
            if version < 1:
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        "UPDATE server_config SET furnace_fee = ? WHERE furnace_fee = 0.0",
                        (config.DEFAULT_FURNACE_FEE,),
                    )
                    conn.execute(
                        "UPDATE server_config SET factory_fee = ? WHERE factory_fee = 0.0",
                        (config.DEFAULT_FACTORY_FEE,),
                    )
                    conn.execute("PRAGMA user_version = 1")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            # Drills became individually tracked instances that keep their
            # level and container while unplaced, so guild_id has to allow
            # NULL ("in inventory") and four new columns have to exist.
            if "level" not in drill_columns:
                conn.execute("BEGIN")
                try:
                    # Left behind only by a previous attempt that was interrupted.
                    conn.execute("DROP TABLE IF EXISTS drills_new")
                    conn.execute(self._DRILLS_REBUILD_DDL)
                    # Carrying drill_id across explicitly keeps every placed
                    # drill's identity and reseeds sqlite_sequence to the old
                    # high-water mark, so new drills still get fresh ids rather
                    # than reusing a retired one.
                    conn.execute(
                        """
                        INSERT INTO drills_new
                            (drill_id, guild_id, owner_id, drill_type, stored_amount, is_full)
                        SELECT drill_id, guild_id, owner_id, drill_type, stored_amount, is_full
                        FROM drills
                        """
                    )
                    conn.execute("DROP TABLE drills")
                    conn.execute("ALTER TABLE drills_new RENAME TO drills")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_drills_owner ON drills(owner_id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_drills_guild ON drills(guild_id)")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            # Drill level-up jobs point at the drill they're upgrading. Adding
            # a nullable column is legal in place - deliberately NOT a rebuild,
            # since this table holds every in-flight queue entry.
            if "target_drill_id" not in job_columns:
                conn.execute("ALTER TABLE production_jobs ADD COLUMN target_drill_id INTEGER")

            if version < 2:
                self._migrate_drill_stacks_to_instances(conn)

            # Per-server settings added to server_config after it first
            # shipped - the hydraulic press, then the scrapper and the
            # designated bot channel. All plain columns with defaults (or
            # nullable), which SQLite can add in place, so each is gated on
            # simply not being there yet.
            config_columns = {row[1] for row in conn.execute("PRAGMA table_info(server_config)")}
            added_config_columns = (
                ("press_level", "INTEGER NOT NULL DEFAULT 1"),
                # The blast furnace, added in 1.3. Its fee is quoted per BATCH
                # of data.materials.BLAST_FURNACE_BATCH_SIZE items, which is
                # why its default dwarfs the furnace's - see config.py.
                ("blast_furnace_level", "INTEGER NOT NULL DEFAULT 1"),
                ("blast_furnace_fee", f"REAL NOT NULL DEFAULT {config.DEFAULT_BLAST_FURNACE_FEE}"),
                ("blast_furnace_fees_collected", "REAL NOT NULL DEFAULT 0.0"),
                ("blast_furnace_max_queue", "INTEGER NOT NULL DEFAULT 5"),
                ("press_fee", f"REAL NOT NULL DEFAULT {config.DEFAULT_PRESS_FEE}"),
                ("press_fees_collected", "REAL NOT NULL DEFAULT 0.0"),
                ("press_max_queue", "INTEGER NOT NULL DEFAULT 1"),
                ("press_progress", "REAL NOT NULL DEFAULT 0.0"),
                ("scrapper_level", "INTEGER NOT NULL DEFAULT 1"),
                ("scrapper_fee", f"REAL NOT NULL DEFAULT {config.DEFAULT_SCRAPPER_FEE}"),
                ("scrapper_fees_collected", "REAL NOT NULL DEFAULT 0.0"),
                ("scrapper_max_queue", "INTEGER NOT NULL DEFAULT 5"),
                # Mining slots, added in 1.3. Deliberately NOT a level column:
                # the cap is summed on read from the <machine>_fees_collected
                # columns already here, which is what makes an existing server's
                # slots reflect fees it paid long before this shipped. All this
                # stores is how much of that has been announced (see
                # utils/db_helpers.py: announce_mining_slot_unlocks), so 1 - the
                # level every server starts at - is the right value for a row
                # that predates the feature: whatever it has already unlocked is
                # announced the next time it pays a fee.
                ("mining_slots_announced", "INTEGER NOT NULL DEFAULT 1"),
                # Nullable with no default: NULL means "answer in every
                # channel", which is what every existing server was doing
                # before this column existed and should keep doing.
                ("bot_channel_id", "INTEGER"),
                # Defaults to 0, so a server that predates the prompt gets it
                # the next time the bot joins - not retroactively on startup,
                # which would post into servers that have been running happily
                # for months.
                ("setup_prompt_sent", "INTEGER NOT NULL DEFAULT 0"),
            )
            for column, definition in added_config_columns:
                if column not in config_columns:
                    conn.execute(f"ALTER TABLE server_config ADD COLUMN {column} {definition}")

            # Tracks whether the bot is still in a server, so a removal can hide
            # that server's currency without deleting anyone's balance. Also an
            # in-place add: the DEFAULT backfills every existing row as present,
            # which is right - the reconciliation pass in cogs/mining.py corrects
            # any that aren't the first time the bot is ready.
            if "bot_present" not in config_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN bot_present INTEGER NOT NULL DEFAULT 1"
                )

            # Every machine shares production_jobs, but its job_type CHECK
            # constraint names them explicitly and SQLite can only widen a CHECK
            # by rebuilding the table. This is the table holding every in-flight
            # queue entry, so the rebuild copies each job_id across explicitly
            # and runs inside one transaction.
            #
            # Gating on the NEWEST job type SUBSUMES every older gate rather
            # than stacking beside it: a database predating the press also
            # predates the scrapper and the blast furnace, so it fails this
            # check too, and the one rebuild writes the DDL naming all five
            # types. That is why this gate has moved from 'press' to 'scrapper'
            # to 'blast_furnace' instead of accumulating - two gates against the
            # same rebuild would just make the second one dead code.
            if not self._job_type_allows(conn, "blast_furnace"):
                conn.execute("BEGIN")
                try:
                    conn.execute("DROP TABLE IF EXISTS production_jobs_new")
                    conn.execute(self._PRODUCTION_JOBS_REBUILD_DDL)
                    conn.execute(
                        """
                        INSERT INTO production_jobs_new
                            (job_id, guild_id, user_id, job_type, target_id, quantity,
                             queued_at, status, target_drill_id)
                        SELECT job_id, guild_id, user_id, job_type, target_id, quantity,
                               queued_at, status, target_drill_id
                        FROM production_jobs
                        """
                    )
                    conn.execute("DROP TABLE production_jobs")
                    conn.execute("ALTER TABLE production_jobs_new RENAME TO production_jobs")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            # The press fee default went from 1.00 to 5.00 shortly after the
            # press shipped. Bump servers still sitting on the old default;
            # guarded by user_version so a fee a manager later sets back to
            # 1.00 deliberately stays there.
            if version < 3:
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        "UPDATE server_config SET press_fee = ? WHERE press_fee = 1.0",
                        (config.DEFAULT_PRESS_FEE,),
                    )
                    conn.execute("PRAGMA user_version = 3")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            # 1.2 gave drills and pools a composition: what used to be a bare
            # count of "raw materials" is now a per-material breakdown, decided
            # when an item is mined rather than when it is collected. Both
            # backfills have to CHANGE existing rows and can't tell from the
            # schema alone whether they already ran - the tables they fill are
            # created empty by CREATE TABLE IF NOT EXISTS either way - so this
            # is gated on user_version rather than on introspection.
            if version < 4:
                self._migrate_pools_and_drills_to_materials(conn)

            # The daily top-up and its cap were removed outright: the pool is
            # now a bag of MINING_POOL_BAG_SIZE items that refills when it runs
            # out, so every server needs a real bag rather than the few thousand
            # items its allowance had accrued. Pure data, and the schema can't
            # tell whether it has run, so it is gated on user_version.
            if version < 5:
                self._migrate_pool_to_bag(conn)

            # server_mining_pool.carry banked the fraction of a gemstone a pool
            # had accrued from the daily top-up. The bag replaced that outright
            # - the gems are simply in it - so nothing has read or written this
            # since 1.2, and a column no code touches is one somebody later has
            # to work out the meaning of. Gated on introspection rather than
            # user_version because dropping a column IS visible in the schema.
            pool_columns = {row[1] for row in conn.execute("PRAGMA table_info(server_mining_pool)")}
            if "carry" in pool_columns:
                conn.execute("ALTER TABLE server_mining_pool DROP COLUMN carry")

            # The job board's bonus is paid per completion rather than once
            # per player per day (1.3), so the boolean claimed_at answered the
            # wrong question and a count replaced it. Adding a NOT NULL column
            # with a DEFAULT is legal in place - deliberately not a rebuild,
            # since this table holds today's in-progress tasks.
            #
            # The backfill in the same branch is what makes an old row mean
            # what it says: everyone who had claimed under the old rule had
            # been paid exactly once, and leaving them at the column DEFAULT of
            # 0 would hand every one of them a second payout for progress they
            # had already been paid for. Safe to run unguarded because the
            # column not existing is itself the proof this hasn't run.
            progress_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(daily_job_progress)")
            }
            if "claims_paid" not in progress_columns:
                conn.execute(
                    "ALTER TABLE daily_job_progress "
                    "ADD COLUMN claims_paid INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    "UPDATE daily_job_progress SET claims_paid = 1 "
                    "WHERE claimed_at IS NOT NULL"
                )

            # user_version is untouched by 1.3 for the same reason it was by
            # 1.1: everything the blast furnace adds is structural and gated on
            # introspection above - four server_config columns and a widened
            # job_type CHECK, both of which the schema itself reveals. The job
            # board's claims_paid is the same story: the column's absence is
            # what says the migration hasn't run.
            #
            # Personal notifications add nothing here either. user_notifications
            # is a NEW table, which CREATE TABLE IF NOT EXISTS handles on its
            # own, and it starts empty on purpose: the rows in it are the record
            # of who has been told what, so an existing player who already owns
            # a ruby has simply not been told yet and gets the notice on their
            # next one. There is nothing to backfill and no way to work out
            # retroactively who would have wanted it.
            #
            # Mining slots add nothing here either, and notably no data
            # migration: a server's slot cap is summed on read from fee columns
            # that already hold every figure it needs, so there is no stored
            # level to backfill and an old database is correct the moment new
            # code opens it. mining_slots_announced is the one column, and its
            # DEFAULT is already the right answer for every existing row.
            #
            # user_version deliberately stays at 3 through 1.1. Everything that
            # release added is structural and gated on introspection above: the
            # scrapper's columns, bot_channel_id, the widened job_type CHECK,
            # and two new tables that CREATE TABLE IF NOT EXISTS handles on its
            # own. The max-queue change is a read-time reinterpretation of a
            # column that already existed (see effective_max_queue), not a
            # rewrite of stored data. Bump this only for a migration that has to
            # CHANGE existing rows and can't tell from the schema alone whether
            # it already ran.

    @staticmethod
    def _job_type_allows(conn: sqlite3.Connection, job_type: str) -> bool:
        """Whether production_jobs' job_type CHECK already permits `job_type`.
        Read off the stored CREATE statement rather than attempted by trial
        insert, so this never leaves a stray row behind."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'production_jobs'"
        ).fetchone()
        return bool(row) and f"'{job_type}'" in row[0]

    @staticmethod
    def _migrate_drill_stacks_to_instances(conn: sqlite3.Connection):
        """Turns the drill counts that used to live in user_materials into one
        drills row each, since a drill now carries a level and a container and
        can no longer be a fungible stack."""
        # Imported here rather than at module scope to keep this file's
        # dependency on game data confined to the migration that needs it.
        from data.materials import DRILLS, BASE_STORAGE_CAPACITY

        placeholders = ",".join("?" * len(DRILLS))
        drill_ids = tuple(DRILLS)

        conn.execute("BEGIN")
        try:
            stacks = conn.execute(
                f"SELECT user_id, material_id, quantity FROM user_materials "
                f"WHERE material_id IN ({placeholders}) AND quantity > 0",
                drill_ids,
            ).fetchall()
            for row in stacks:
                for _ in range(row["quantity"]):
                    conn.execute(
                        "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (NULL, ?, ?)",
                        (row["user_id"], row["material_id"]),
                    )
            conn.execute(
                f"DELETE FROM user_materials WHERE material_id IN ({placeholders})",
                drill_ids,
            )

            # Base capacity is now a flat BASE_STORAGE_CAPACITY for every drill
            # type, so drills built under the old per-tier capacities can be
            # sitting above it. Flag those full here: the harvest loop bails
            # out before the UPDATE that would otherwise set the flag, so
            # without this they'd linger in its result set doing nothing while
            # /mine status reported them as still mining. Their contents are
            # deliberately left alone - /collect credits every last item and
            # resets the drill, so the over-capacity state clears itself.
            conn.execute(
                "UPDATE drills SET is_full = 1 "
                "WHERE guild_id IS NOT NULL AND is_full = 0 AND stored_amount >= ?",
                (BASE_STORAGE_CAPACITY,),
            )
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _migrate_pools_and_drills_to_materials(conn: sqlite3.Connection):
        """Gives every existing drill and mining pool the per-material
        composition 1.2 needs, derived from the bare counts they held before it.

        Both are filled by rolling each item against the drop table - which is
        exactly what /collect would have done to those same items a moment
        later, since a drill decided what it had mined only at handover. So no
        player gains or loses anything by this running: it fixes in advance an
        answer that was previously computed on demand.

        Rolling independently here rather than drawing from a pool is
        deliberate, and is the one place that stays right. There is no pool
        composition yet to draw from - creating it is what this is for - and the
        material already in flight was mined under the old independent-roll
        rules. Applying the new guarantee retroactively would conjure gemstones
        into pools that never produced them.
        """
        from data.materials import roll_raw_material

        conn.execute("BEGIN")
        try:
            # Pools first: a server's remaining count becomes a bag of that many
            # rolled items. Nothing is created or destroyed - the same total
            # comes out the far side, itemised.
            pools = conn.execute(
                "SELECT guild_id, mining_pool_remaining FROM server_config "
                "WHERE mining_pool_remaining > 0"
            ).fetchall()
            for row in pools:
                counts: dict[str, int] = {}
                for _ in range(row["mining_pool_remaining"]):
                    material_id = roll_raw_material()
                    counts[material_id] = counts.get(material_id, 0) + 1
                for material_id, quantity in counts.items():
                    conn.execute(
                        "INSERT INTO server_mining_pool (guild_id, material_id, quantity) "
                        "VALUES (?, ?, ?) ON CONFLICT(guild_id, material_id) DO UPDATE "
                        "SET quantity = excluded.quantity",
                        (row["guild_id"], material_id, quantity),
                    )

            # Then whatever the drills are already holding. stored_amount is
            # left exactly as it is: it stays the authoritative total, and these
            # rows have to sum to it.
            drills = conn.execute(
                "SELECT drill_id, stored_amount FROM drills WHERE stored_amount > 0"
            ).fetchall()
            for row in drills:
                counts = {}
                for _ in range(row["stored_amount"]):
                    material_id = roll_raw_material()
                    counts[material_id] = counts.get(material_id, 0) + 1
                for material_id, quantity in counts.items():
                    conn.execute(
                        "INSERT INTO drill_contents (drill_id, material_id, quantity) "
                        "VALUES (?, ?, ?) ON CONFLICT(drill_id, material_id) DO UPDATE "
                        "SET quantity = excluded.quantity",
                        (row["drill_id"], material_id, quantity),
                    )

            conn.execute("PRAGMA user_version = 4")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _migrate_pool_to_bag(conn: sqlite3.Connection):
        """Tops every server's pool up to a full bag, and retires the daily
        top-up's bookkeeping.

        Every server gains material rather than losing any: the bag is added to
        whatever the old allowance had accrued, so nothing anyone had mined
        toward disappears. The old cap (three days of production) was at most a
        few thousand items against a million, so in practice this is simply
        "here is your bag".

        mining_pool_last_topup goes with it. Nothing reads it any more - there
        is no daily event left for it to record - and a column that no code
        touches is one somebody later has to work out the meaning of.
        """
        from data.materials import pool_bag_contents

        bag = pool_bag_contents()
        conn.execute("BEGIN")
        try:
            for row in conn.execute("SELECT guild_id FROM server_config").fetchall():
                for material_id, quantity in bag.items():
                    conn.execute(
                        "INSERT INTO server_mining_pool (guild_id, material_id, quantity) "
                        "VALUES (?, ?, ?) ON CONFLICT(guild_id, material_id) DO UPDATE "
                        "SET quantity = quantity + excluded.quantity",
                        (row["guild_id"], material_id, quantity),
                    )
                conn.execute(
                    "UPDATE server_config SET mining_pool_remaining = mining_pool_remaining + ? "
                    "WHERE guild_id = ?",
                    (sum(bag.values()), row["guild_id"]),
                )

            columns = {r[1] for r in conn.execute("PRAGMA table_info(server_config)")}
            if "mining_pool_last_topup" in columns:
                conn.execute("ALTER TABLE server_config DROP COLUMN mining_pool_last_topup")

            conn.execute("PRAGMA user_version = 5")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    async def init_schema(self):
        """Creates all tables from schema.sql if they don't already exist.
        Call this once, right after the bot logs in."""
        await asyncio.to_thread(self._init_schema_sync)

    def _execute_sync(self, query: str, params: tuple):
        with self._connect() as conn:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.lastrowid, cur.rowcount

    async def execute(self, query: str, params: tuple = ()) -> int:
        """Run a single standalone INSERT/UPDATE/DELETE, committing it on its
        own connection. Returns the last inserted row id.

        If this statement is one of several that have to succeed or fail
        together, use `async with db.transaction()` instead."""
        lastrowid, _ = await asyncio.to_thread(self._execute_sync, query, params)
        return lastrowid

    async def execute_changes(self, query: str, params: tuple = ()) -> int:
        """As `execute`, but returns how many rows the statement changed."""
        _, rowcount = await asyncio.to_thread(self._execute_sync, query, params)
        return rowcount

    def _fetchone_sync(self, query: str, params: tuple):
        with self._connect() as conn:
            cur = conn.execute(query, params)
            return cur.fetchone()

    async def fetchone(self, query: str, params: tuple = ()):
        """Run a SELECT and return the first matching row (or None)."""
        return await asyncio.to_thread(self._fetchone_sync, query, params)

    def _fetchall_sync(self, query: str, params: tuple):
        with self._connect() as conn:
            cur = conn.execute(query, params)
            return cur.fetchall()

    async def fetchall(self, query: str, params: tuple = ()):
        """Run a SELECT and return all matching rows as a list."""
        return await asyncio.to_thread(self._fetchall_sync, query, params)
