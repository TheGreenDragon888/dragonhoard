"""
Tests that a database built by an OLDER version of the schema survives being
opened by this one.

This is the release's highest-risk change: widening production_jobs' job_type
CHECK to admit a new machine's jobs can't be done in place, so init_schema
rebuilds the table that holds every in-flight queue entry. If that rebuild
loses a row, or loses a column off a row, a server's whole queue quietly
evaporates on restart and there is nothing to restore it from. It has now
happened three times - the press, then the scrapper, then 1.3's blast furnace -
and each time the gate moved to the newest job type rather than a fourth gate
being added beside the others (database/db.py).

Each test builds a pre-1.1 database by hand rather than checking in a fixture
file, so what "before" means is visible in the test rather than in a binary.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config
from database.db import Database
from data.materials import (
    BASE_MINING_SLOTS,
    MINING_POOL_BAG_SIZE,
    mining_slot_threshold,
)
from utils.db_helpers import mining_slot_status

GUILD = 8484
USER = 4242

# server_config and production_jobs exactly as they stood in 1.0: no scrapper
# columns, no bot_channel_id, and a job_type CHECK naming only three machines.
# Only the columns the migration has to preserve are included.
_PRE_1_1_SCHEMA = """
CREATE TABLE server_config (
    guild_id            INTEGER PRIMARY KEY,
    currency_name       TEXT,
    currency_emoji      TEXT,
    furnace_level       INTEGER NOT NULL DEFAULT 1,
    factory_level       INTEGER NOT NULL DEFAULT 1,
    furnace_fee         REAL NOT NULL DEFAULT 0.01,
    factory_fee         REAL NOT NULL DEFAULT 0.25,
    furnace_fees_collected REAL NOT NULL DEFAULT 0.0,
    factory_fees_collected REAL NOT NULL DEFAULT 0.0,
    furnace_max_queue   INTEGER NOT NULL DEFAULT 25,
    factory_max_queue   INTEGER NOT NULL DEFAULT 5,
    press_level         INTEGER NOT NULL DEFAULT 1,
    press_fee           REAL NOT NULL DEFAULT 5.0,
    press_fees_collected REAL NOT NULL DEFAULT 0.0,
    press_max_queue     INTEGER NOT NULL DEFAULT 1,
    press_progress      REAL NOT NULL DEFAULT 0.0,
    public_messages     INTEGER NOT NULL DEFAULT 0,
    bot_present         INTEGER NOT NULL DEFAULT 1,
    mining_pool_remaining  INTEGER NOT NULL DEFAULT 0,
    mining_pool_last_topup TEXT NOT NULL DEFAULT '',
    currency_minted_total  REAL NOT NULL DEFAULT 0.0,
    currency_burned_total  REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE production_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    job_type        TEXT NOT NULL CHECK (job_type IN ('furnace', 'factory', 'press')),
    target_id       TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    queued_at       TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'in_progress', 'complete')),
    target_drill_id INTEGER
);

CREATE TABLE drills (
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
);

PRAGMA user_version = 3;
"""


class Pre11UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "old.db")

        conn = sqlite3.connect(self.path)
        conn.executescript(_PRE_1_1_SCHEMA)
        # A pool with something in it, so 1.2's composition backfill has
        # material to explode into per-material rows.
        #
        # The two fee totals are what 1.3's mining slots have to reckon with:
        # this server banked them years before slots existed, and they sum to
        # exactly mining_slot_threshold(3). Neither machine paid enough on its
        # own, so this also pins that the sum - not the larger of the two - is
        # what the slot ladder reads.
        conn.execute(
            "INSERT INTO server_config (guild_id, mining_pool_remaining, "
            "furnace_fees_collected, factory_fees_collected) VALUES (?, 2884, 75.0, 50.0)",
            (GUILD,),
        )
        # An in-flight queue entry of each pre-1.1 job type, including a drill
        # upgrade - the rows the rebuild has to carry across untouched.
        conn.execute(
            "INSERT INTO drills (drill_id, guild_id, owner_id, drill_type, level) "
            "VALUES (7, NULL, ?, 'iron_drill', 3)",
            (USER,),
        )
        # A placed drill holding a bare count, which is what every drill looked
        # like before 1.2 gave them a per-material composition.
        conn.execute(
            "INSERT INTO drills (drill_id, guild_id, owner_id, drill_type, level, stored_amount, is_full) "
            "VALUES (8, ?, ?, 'iron_drill', 1, 100, 1)",
            (GUILD, USER),
        )
        conn.executemany(
            "INSERT INTO production_jobs (job_id, guild_id, user_id, job_type, target_id, "
            "quantity, queued_at, status, target_drill_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (11, GUILD, USER, "furnace", "iron", 40, "2026-08-01 10:00:00", "in_progress", None),
                (12, GUILD, USER, "factory", "wiring", 3, "2026-08-01 11:00:00", "queued", None),
                (13, GUILD, USER, "press", "ruby", 1, "2026-08-01 12:00:00", "queued", None),
                (14, GUILD, USER, "factory", "drill_upgrade", 1, "2026-08-01 13:00:00", "queued", 7),
            ],
        )
        conn.commit()
        conn.close()

        self.db = Database(self.path)
        await self.db.init_schema()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_scrapper_jobs_are_accepted_afterwards(self):
        # The point of the rebuild. Before it, this INSERT raises IntegrityError.
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'scrapper', 'wiring', 2)",
            (GUILD, USER),
        )
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM production_jobs WHERE job_type = 'scrapper'"
        )
        self.assertEqual(row["n"], 1)

    async def test_blast_furnace_jobs_are_accepted_afterwards(self):
        # 1.3's half of the same rebuild. A database this old predates BOTH the
        # scrapper and the blast furnace, and one rebuild has to admit each -
        # which is exactly what would break if the gate were ever left naming an
        # older job type than the DDL does.
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'blast_furnace', 'steel', 3)",
            (GUILD, USER),
        )
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM production_jobs WHERE job_type = 'blast_furnace'"
        )
        self.assertEqual(row["n"], 1)

    async def test_mining_slots_announced_was_added_at_the_base_level(self):
        # Structural, so it is gated on introspecting server_config like every
        # other added column. The DEFAULT is the base level rather than 0: an
        # existing server has been "told" about the slots it already had.
        row = await self.db.fetchone(
            "SELECT mining_slots_announced FROM server_config WHERE guild_id = ?",
            (GUILD,),
        )
        self.assertEqual(row["mining_slots_announced"], 1)

    async def test_fees_banked_before_1_3_already_paid_for_their_slots(self):
        # The whole point of deriving the cap instead of storing it. This
        # database's 125.00 was collected by a version that had never heard of
        # mining slots, and the slots are simply there when new code opens it -
        # there is no backfill step that could have been missed.
        slots = await mining_slot_status(self.db, GUILD)
        self.assertEqual(slots.invested, mining_slot_threshold(3))
        self.assertEqual(slots.level, 3)
        self.assertEqual(slots.slots, BASE_MINING_SLOTS + 2)

    async def test_the_blast_furnace_columns_were_added_with_their_defaults(self):
        row = await self.db.fetchone(
            "SELECT blast_furnace_level, blast_furnace_fee, blast_furnace_fees_collected, "
            "blast_furnace_max_queue FROM server_config WHERE guild_id = ?",
            (GUILD,),
        )
        self.assertEqual(row["blast_furnace_level"], 1)
        self.assertEqual(row["blast_furnace_fee"], config.DEFAULT_BLAST_FURNACE_FEE)
        self.assertEqual(row["blast_furnace_fees_collected"], 0.0)
        self.assertEqual(row["blast_furnace_max_queue"], 5)

    async def test_the_old_job_types_still_work(self):
        for job_type in ("furnace", "factory", "press"):
            with self.subTest(job_type=job_type):
                await self.db.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                    "VALUES (?, ?, ?, 'iron', 1)",
                    (GUILD, USER, job_type),
                )

    async def test_a_nonsense_job_type_is_still_refused(self):
        # The CHECK has to be widened, not dropped.
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute(
                "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                "VALUES (?, ?, 'teleporter', 'iron', 1)",
                (GUILD, USER),
            )

    async def test_every_in_flight_job_survived_intact(self):
        rows = await self.db.fetchall("SELECT * FROM production_jobs ORDER BY job_id")
        self.assertEqual([row["job_id"] for row in rows], [11, 12, 13, 14])
        by_id = {row["job_id"]: row for row in rows}
        self.assertEqual(by_id[11]["quantity"], 40)
        self.assertEqual(by_id[11]["status"], "in_progress")
        self.assertEqual(by_id[11]["queued_at"], "2026-08-01 10:00:00")
        self.assertEqual(by_id[13]["target_id"], "ruby")
        # A queued drill upgrade must keep pointing at its drill, or that drill
        # is locked out of every command forever.
        self.assertEqual(by_id[14]["target_drill_id"], 7)

    async def test_new_job_ids_do_not_reuse_a_retired_one(self):
        # The rebuild copies job_id explicitly so sqlite_sequence is reseeded
        # to the old high-water mark rather than restarting from 1.
        job_id = await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'furnace', 'iron', 1)",
            (GUILD, USER),
        )
        self.assertGreater(job_id, 14)

    async def test_the_scrapper_columns_were_added_with_their_defaults(self):
        row = await self.db.fetchone(
            "SELECT scrapper_level, scrapper_fee, scrapper_fees_collected, scrapper_max_queue "
            "FROM server_config WHERE guild_id = ?",
            (GUILD,),
        )
        self.assertEqual(row["scrapper_level"], 1)
        self.assertEqual(row["scrapper_fee"], 0.10)
        self.assertEqual(row["scrapper_fees_collected"], 0.0)
        self.assertEqual(row["scrapper_max_queue"], 5)

    async def test_an_existing_server_is_not_suddenly_channel_restricted(self):
        # bot_channel_id is nullable with no default precisely so every server
        # that predates the setting keeps answering everywhere.
        row = await self.db.fetchone(
            "SELECT bot_channel_id FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        self.assertIsNone(row["bot_channel_id"])

    async def test_tables_added_since_are_created(self):
        # Every one of these arrived after this database was built, and all of
        # them are plain CREATE TABLE IF NOT EXISTS - no migration, so what
        # this really checks is that init_schema runs the schema file before
        # anything that reads them.
        for table in ("daily_jobs", "daily_job_progress", "user_notifications"):
            with self.subTest(table=table):
                row = await self.db.fetchone(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                )
                self.assertIsNotNone(row)

    async def test_an_existing_player_is_not_retroactively_notified(self):
        """user_notifications starts empty, and that is the intended state for
        a database that predates it. The rows are the record of who has been
        told; there is no way to work out who WOULD have been told, so somebody
        already holding a ruby hears about /focus on their next one or not at
        all - the harmless direction."""
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM user_notifications")
        self.assertEqual(row["n"], 0)

    async def test_it_lands_on_the_current_user_version(self):
        """1.1 added no pure-data migration - everything it changed was gated on
        introspecting the schema - so a database opened on 1.1 sat at 3. 1.2
        adds two, both pure data (the tables they fill are created empty either
        way, so the schema can't say whether they've run): 4 gave pools and
        drills a per-material composition, and 5 replaced the daily allowance
        with a full bag."""
        row = await self.db.fetchone("PRAGMA user_version")
        self.assertEqual(row[0], 5)

    async def test_the_daily_top_ups_bookkeeping_is_gone(self):
        # There is no daily event left for it to record, and a column nothing
        # reads is one somebody later has to work out the meaning of.
        columns = {
            row[1] for row in await self.db.fetchall("PRAGMA table_info(server_config)")
        }
        self.assertNotIn("mining_pool_last_topup", columns)

    async def test_the_pools_gemstone_accrual_carry_is_gone(self):
        """The other half of the same removal, missed at the time. carry banked
        the fraction of a gemstone a pool had accrued from the daily top-up;
        the bag made it meaningless - the gems are simply in it - and nothing
        has read or written it since. Dropped structurally rather than on
        user_version, because a missing column IS visible in the schema."""
        columns = {
            row[1]
            for row in await self.db.fetchall("PRAGMA table_info(server_mining_pool)")
        }
        self.assertNotIn("carry", columns)
        # The counts the column sat beside are what the bag is made of, so the
        # drop must not have taken them with it.
        self.assertEqual(columns, {"guild_id", "material_id", "quantity"})

    async def test_every_server_ends_up_with_a_full_bag(self):
        # The migration adds a bag rather than replacing what was there, so a
        # server keeps whatever its old allowance had accrued on top.
        rows = await self.db.fetchall(
            "SELECT guild_id, mining_pool_remaining FROM server_config"
        )
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(guild=row["guild_id"]):
                self.assertGreaterEqual(row["mining_pool_remaining"], MINING_POOL_BAG_SIZE)

    async def test_the_drill_keeps_its_items_and_gains_a_composition(self):
        """The 1.2 backfill. A drill that was holding a bare count now has to
        say WHAT it holds, and the two have to agree - stored_amount stays
        authoritative, and drill_contents has to sum to it or the player loses
        material at the next /collect."""
        drills = await self.db.fetchall(
            "SELECT drill_id, stored_amount FROM drills WHERE stored_amount > 0"
        )
        self.assertTrue(drills, "fixture should have a drill holding something")
        for drill in drills:
            with self.subTest(drill=drill["drill_id"]):
                rows = await self.db.fetchall(
                    "SELECT quantity FROM drill_contents WHERE drill_id = ?",
                    (drill["drill_id"],),
                )
                self.assertEqual(
                    sum(row["quantity"] for row in rows), drill["stored_amount"]
                )

    async def test_the_pool_keeps_its_size_and_gains_a_composition(self):
        pools = await self.db.fetchall(
            "SELECT guild_id, mining_pool_remaining FROM server_config "
            "WHERE mining_pool_remaining > 0"
        )
        for pool in pools:
            with self.subTest(guild=pool["guild_id"]):
                rows = await self.db.fetchall(
                    "SELECT quantity FROM server_mining_pool WHERE guild_id = ?",
                    (pool["guild_id"],),
                )
                self.assertEqual(
                    sum(row["quantity"] for row in rows), pool["mining_pool_remaining"]
                )

    async def test_opening_it_again_changes_nothing(self):
        # init_schema runs on every boot, so it has to be idempotent.
        before = await self.db.fetchall("SELECT * FROM production_jobs ORDER BY job_id")
        await self.db.init_schema()
        after = await self.db.fetchall("SELECT * FROM production_jobs ORDER BY job_id")
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])


# daily_job_progress exactly as it stood from 1.1 to 1.2: a claimed_at
# timestamp and nothing else, because the bonus was paid once per player per
# day and "have they had it?" was the only question the row had to answer.
_PRE_1_3_JOB_PROGRESS_SCHEMA = """
CREATE TABLE daily_job_progress (
    guild_id        INTEGER NOT NULL,
    job_date        TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    sold            INTEGER NOT NULL DEFAULT 0,
    claimed_at      TEXT,
    PRIMARY KEY (guild_id, job_date, user_id)
);
"""


class JobBoardClaimsMigrationTests(unittest.IsolatedAsyncioTestCase):
    """1.3 pays the job board bonus per completion, so claimed_at's boolean
    answer became a count. The backfill is what stops a player who had already
    claimed today being paid a second time for the same progress the first time
    they sell after the upgrade."""

    CLAIMED = 8888
    UNCLAIMED = 7777

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "old.db")

        conn = sqlite3.connect(self.path)
        conn.executescript(_PRE_1_3_JOB_PROGRESS_SCHEMA)
        conn.executemany(
            "INSERT INTO daily_job_progress (guild_id, job_date, user_id, sold, claimed_at) "
            "VALUES (?, '2026-08-26', ?, ?, ?)",
            [
                # Sold five times the task and was paid once, which is exactly
                # what the old rule did.
                (GUILD, self.CLAIMED, 500, "2026-08-26 09:00:00"),
                # Partway through, never claimed.
                (GUILD, self.UNCLAIMED, 40, None),
            ],
        )
        conn.commit()
        conn.close()

        self.db = Database(self.path)
        await self.db.init_schema()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def claims_paid(self, user_id):
        row = await self.db.fetchone(
            "SELECT claims_paid, sold FROM daily_job_progress WHERE user_id = ?", (user_id,)
        )
        return row

    async def test_a_claimed_row_counts_as_one_completion_already_paid(self):
        # NOT sold/quantity - the old rule paid once however much was sold, so
        # backfilling from the quantity would hand out four completions this
        # player was never paid for.
        row = await self.claims_paid(self.CLAIMED)
        self.assertEqual(row["claims_paid"], 1)
        self.assertEqual(row["sold"], 500)

    async def test_an_unclaimed_row_starts_at_zero(self):
        row = await self.claims_paid(self.UNCLAIMED)
        self.assertEqual(row["claims_paid"], 0)
        self.assertEqual(row["sold"], 40)

    async def test_opening_it_again_changes_nothing(self):
        # The gate is the column's absence, so a second open must not re-run
        # the backfill and overwrite a count that has since moved.
        await self.db.execute(
            "UPDATE daily_job_progress SET claims_paid = 6 WHERE user_id = ?", (self.CLAIMED,)
        )
        await Database(self.path).init_schema()
        row = await self.claims_paid(self.CLAIMED)
        self.assertEqual(row["claims_paid"], 6)


class FreshDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """A brand new database has to end up in exactly the state a migrated one
    does - the CHECK clause in schema.sql and the one in the rebuild DDL are
    two separate strings, and only one of them runs for any given database."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "new.db"))
        await self.db.init_schema()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_it_accepts_every_machines_jobs(self):
        await self.db.execute("INSERT INTO server_config (guild_id) VALUES (?)", (GUILD,))
        for job_type in ("furnace", "blast_furnace", "factory", "press", "scrapper"):
            with self.subTest(job_type=job_type):
                await self.db.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                    "VALUES (?, ?, ?, 'iron', 1)",
                    (GUILD, USER, job_type),
                )

    async def test_it_refuses_an_unknown_job_type(self):
        await self.db.execute("INSERT INTO server_config (guild_id) VALUES (?)", (GUILD,))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute(
                "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                "VALUES (?, ?, 'teleporter', 'iron', 1)",
                (GUILD, USER),
            )


if __name__ == "__main__":
    unittest.main()
