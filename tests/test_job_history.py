"""
tests/test_job_history.py

What happens to a production job after its machine finishes it, and how the
machines find the work they have to do.

Two things 1.4 changed about production_jobs, both for the sake of cheap
ticks on small hardware, and both easy to regress silently:

  * Finished rows are kept for COMPLETED_JOB_HISTORY_DAYS and then pruned
    (utils/db_helpers.py: complete_job, prune_completed_jobs). Before, they
    were kept forever and every live-job lookup scanned them all.
  * Each processing loop visits only the servers that have a live job on its
    machine (guilds_with_queued_work), rather than every server every tick.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database.db import Database
from utils import db_helpers
from utils.db_helpers import (
    COMPLETED_JOB_HISTORY_DAYS,
    advance_job,
    complete_job,
    ensure_server_row,
    guilds_with_queued_work,
    prune_completed_jobs,
)

BUSY = 1001
IDLE = 1002
USER = 77


class JobHistoryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        for guild_id in (BUSY, IDLE):
            await ensure_server_row(self.db, guild_id)
        # The prune's once-a-day marker is process-local; start each test
        # with it clear so a prune in one can't suppress the next's.
        db_helpers._jobs_last_pruned = None

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def queue(self, guild_id, machine="furnace", quantity=3, queued_at=None):
        if queued_at is None:
            return await self.db.execute(
                "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                "VALUES (?, ?, ?, 'iron', ?)",
                (guild_id, USER, machine, quantity),
            )
        return await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, queued_at) "
            "VALUES (?, ?, ?, 'iron', ?, ?)",
            (guild_id, USER, machine, quantity, queued_at),
        )

    async def job(self, job_id):
        return await self.db.fetchone(
            "SELECT status, quantity FROM production_jobs WHERE job_id = ?", (job_id,)
        )


class WorkListTests(JobHistoryTestCase):
    async def test_only_servers_with_a_live_job_on_that_machine_are_listed(self):
        await self.queue(BUSY, "furnace")
        await self.queue(IDLE, "factory")   # work, but not for the furnace

        rows = await guilds_with_queued_work(self.db, "furnace")
        self.assertEqual([row["guild_id"] for row in rows], [BUSY])

    async def test_a_finished_job_takes_its_server_off_the_list(self):
        job_id = await self.queue(BUSY)
        await complete_job(self.db, job_id)
        self.assertEqual(await guilds_with_queued_work(self.db, "furnace"), [])

    async def test_it_carries_that_machines_level(self):
        await self.db.execute(
            "UPDATE server_config SET press_level = 4 WHERE guild_id = ?", (BUSY,)
        )
        await self.queue(BUSY, "press")
        rows = await guilds_with_queued_work(self.db, "press")
        self.assertEqual(rows[0]["level"], 4)

    async def test_an_unknown_machine_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            await guilds_with_queued_work(self.db, "smelter")


class CompletionTests(JobHistoryTestCase):
    async def test_completing_zeroes_the_quantity_and_keeps_the_row(self):
        job_id = await self.queue(BUSY)
        await complete_job(self.db, job_id)
        self.assertEqual(tuple(await self.job(job_id)), ("complete", 0))

    async def test_advancing_records_what_is_left(self):
        job_id = await self.queue(BUSY, quantity=3)
        await advance_job(self.db, job_id, 2)
        self.assertEqual(tuple(await self.job(job_id)), ("in_progress", 2))


class PruneTests(JobHistoryTestCase):
    def stamp(self, days_ago):
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")

    async def test_old_finished_jobs_are_dropped_and_recent_ones_kept(self):
        old = await self.queue(BUSY, queued_at=self.stamp(COMPLETED_JOB_HISTORY_DAYS + 1))
        recent = await self.queue(BUSY, queued_at=self.stamp(COMPLETED_JOB_HISTORY_DAYS - 1))
        await complete_job(self.db, old)
        await complete_job(self.db, recent)

        self.assertIsNone(await self.job(old))
        self.assertIsNotNone(await self.job(recent))

    async def test_a_live_job_is_never_pruned_however_old(self):
        """queued_at is the only timestamp a job has, so an ancient job that
        is somehow still queued must be left alone - it is still owed."""
        ancient = await self.queue(BUSY, queued_at=self.stamp(COMPLETED_JOB_HISTORY_DAYS * 2))
        await prune_completed_jobs(self.db)
        self.assertIsNotNone(await self.job(ancient))

    async def test_it_prunes_at_most_once_a_day(self):
        await prune_completed_jobs(self.db)
        old = await self.queue(BUSY, queued_at=self.stamp(COMPLETED_JOB_HISTORY_DAYS + 1))
        await complete_job(self.db, old)   # same day: the prune inside is skipped
        self.assertIsNotNone(await self.job(old))

        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        await prune_completed_jobs(self.db, now=tomorrow)
        self.assertIsNone(await self.job(old))


if __name__ == "__main__":
    unittest.main()
