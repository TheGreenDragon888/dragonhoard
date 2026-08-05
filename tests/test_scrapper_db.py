"""
Tests for the scrapper's drain loop against a real database.

What matters here is the drill half. A stack of components can be scrapped
partially and picked up again next tick, but a drill is one row that is either
still there or gone, and the row is deleted only when the job completes. If
that delete and the credit for it ever came apart, a player would either lose a
drill for nothing or get paid for one that still exists.

The cog is built with __new__ rather than its constructor, which would start
the drain loop for real; the loop body is then driven a tick at a time.
"""
import tempfile
import unittest
from pathlib import Path

from cogs.scrapper import ScrapperCog
from database.db import Database
from data.materials import scrap_yield
from utils.db_helpers import ensure_server_row, ensure_user_row

GUILD = 8484
USER = 4242


class ScrapperDrainTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

        self.cog = ScrapperCog.__new__(ScrapperCog)
        self.cog.db = self.db
        self.cog._production_progress = {}

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def tick(self, times=1):
        """Runs the drain loop's body. A level 1 scrapper does 2 items/hour
        over 12 ticks an hour, so several ticks are needed per item."""
        for _ in range(times):
            await ScrapperCog.process_loop.coro(self.cog)

    async def set_level(self, level):
        await self.db.execute(
            "UPDATE server_config SET scrapper_level = ? WHERE guild_id = ?", (level, GUILD)
        )

    async def add_drill(self, drill_type="iron_drill", level=1, **columns):
        defaults = {"guild_id": None, "container_type": None, "locked_job_id": None}
        defaults.update(columns)
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, container_type, locked_job_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (defaults["guild_id"], USER, drill_type, level,
             defaults["container_type"], defaults["locked_job_id"]),
        )

    async def queue_drill_scrap(self, drill_id):
        job_id = await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, target_drill_id) "
            "VALUES (?, ?, 'scrapper', 'drill_scrap', 1, ?)",
            (GUILD, USER, drill_id),
        )
        await self.db.execute(
            "UPDATE drills SET locked_job_id = ? WHERE drill_id = ?", (job_id, drill_id)
        )
        return job_id

    async def queue_stack(self, material_id, quantity):
        return await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'scrapper', ?, ?)",
            (GUILD, USER, material_id, quantity),
        )

    async def quantity(self, material_id):
        row = await self.db.fetchone(
            "SELECT quantity FROM user_materials WHERE user_id = ? AND material_id = ?",
            (USER, material_id),
        )
        return row["quantity"] if row else 0

    async def drill_exists(self, drill_id):
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM drills WHERE drill_id = ?", (drill_id,)
        )
        return bool(row["n"])

    async def job_status(self, job_id):
        row = await self.db.fetchone(
            "SELECT status, quantity FROM production_jobs WHERE job_id = ?", (job_id,)
        )
        return row["status"], row["quantity"]


class StackScrappingTests(ScrapperDrainTestCase):
    async def test_a_scrapped_stack_credits_its_yield(self):
        await self.set_level(30)  # 2/level = 60 items/hour, i.e. 5 per tick
        await self.queue_stack("wiring", 3)

        await self.tick()

        # wiring costs 12 copper, so half of three of them is 18.
        self.assertEqual(await self.quantity("copper"), 3 * scrap_yield("wiring")["copper"])

    async def test_a_stack_can_drain_over_several_ticks(self):
        await self.set_level(6)  # 2/level = 12 items/hour, i.e. exactly 1 per tick
        job_id = await self.queue_stack("wiring", 3)

        await self.tick()
        self.assertEqual(await self.job_status(job_id), ("in_progress", 2))
        self.assertEqual(await self.quantity("copper"), scrap_yield("wiring")["copper"])

        await self.tick(2)
        self.assertEqual(await self.job_status(job_id), ("complete", 0))
        self.assertEqual(await self.quantity("copper"), 3 * scrap_yield("wiring")["copper"])

    async def test_a_slow_scrapper_produces_nothing_most_ticks(self):
        # Level 1 is 2 items/hour against 12 ticks/hour, so the fractional
        # accumulator is what stops it rounding up to an item every tick.
        await self.queue_stack("wiring", 10)
        await self.tick(5)
        self.assertEqual(await self.quantity("copper"), 0)


class DrillScrappingTests(ScrapperDrainTestCase):
    async def test_a_completed_drill_scrap_deletes_the_drill_and_pays_out(self):
        drill_id = await self.add_drill("iron_drill")
        job_id = await self.queue_drill_scrap(drill_id)

        await self.set_level(30)
        await self.tick()

        self.assertFalse(await self.drill_exists(drill_id))
        self.assertEqual(await self.job_status(job_id), ("complete", 0))
        for material_id, quantity in scrap_yield("iron_drill").items():
            self.assertEqual(await self.quantity(material_id), quantity)

    async def test_the_drill_survives_while_the_job_is_still_queued(self):
        """What lets /scrapper status name the drill it's working on, and what
        keeps the drill locked out of every other command meanwhile."""
        drill_id = await self.add_drill()
        await self.queue_drill_scrap(drill_id)

        await self.tick(3)  # level 1: not enough capacity to finish an item

        self.assertTrue(await self.drill_exists(drill_id))

    async def test_a_levelled_drill_returns_no_more_than_a_stock_one(self):
        # Level investment is deliberately not refunded.
        cheap = await self.add_drill("iron_drill", level=1)
        await self.queue_drill_scrap(cheap)
        await self.set_level(30)
        await self.tick()
        stock_yield = {m: await self.quantity(m) for m in scrap_yield("iron_drill")}

        expensive = await self.add_drill("iron_drill", level=9)
        await self.queue_drill_scrap(expensive)
        await self.tick()

        for material_id, quantity in stock_yield.items():
            self.assertEqual(await self.quantity(material_id), quantity * 2)

    async def test_draining_the_same_job_twice_deletes_one_drill(self):
        """The locked_job_id match in the DELETE is what makes this idempotent.
        Without it a job that somehow drained twice could take out a drill that
        had since been locked by something else."""
        drill_id = await self.add_drill()
        job_id = await self.queue_drill_scrap(drill_id)

        await self.set_level(30)
        await self.tick()

        # Re-open the finished job, as a double drain would leave it.
        await self.db.execute(
            "UPDATE production_jobs SET status = 'queued', quantity = 1 WHERE job_id = ?",
            (job_id,),
        )
        survivor = await self.add_drill()
        await self.db.execute(
            "UPDATE drills SET locked_job_id = ? WHERE drill_id = ?", (job_id + 999, survivor)
        )

        await self.tick()

        self.assertTrue(await self.drill_exists(survivor))

    async def test_gem_drills_keep_their_gem_through_the_whole_chain(self):
        # Scrap the drill, then scrap what it gave back: the ruby has to
        # survive both steps rather than being floored away at either.
        drill_id = await self.add_drill("ruby_drill")
        await self.queue_drill_scrap(drill_id)
        await self.set_level(30)
        await self.tick()

        self.assertEqual(await self.quantity("ruby_drill_bit"), 1)

        await self.queue_stack("ruby_drill_bit", 1)
        await self.tick()

        self.assertGreaterEqual(await self.quantity("ruby"), 1)


if __name__ == "__main__":
    unittest.main()
