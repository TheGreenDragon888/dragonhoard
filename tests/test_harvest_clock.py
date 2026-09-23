"""
Drills and the press are paid for time that has actually passed.

The same rule tests/test_machine_speed.py pins for the four item machines
(utils/db_helpers.py: elapsed_work_hours), applied where the state is stored
rather than held in memory:

  * A drill's harvest used to credit a whole tick to every placed, non-full
    drill - including one placed, emptied by /collect, or given room by a
    container a second before the tick. drills.mined_until now records when
    its mining was last paid up to, and anything that starts it mining resets
    that to now.
  * The press used to credit a whole tick to a job queued a second before the
    tick, and to spend the fraction its last run had left in press_progress on
    the next one. Its first tick now pays only for the time since the job was
    queued, and a job queued onto an empty press clears what was left.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from discord import app_commands

from cogs.mining import COLLECT_EMPTY_DRILL_SQL, HARVEST_TICK_MINUTES, MiningCog
from cogs.press import PRESS_TICK_MINUTES, PressCog
from data.materials import (
    BASE_STORAGE_CAPACITY,
    PRESS_RECIPES,
    effective_capacity,
    effective_rate,
    press_rate_per_day,
)
from database.db import Database
from utils.db_helpers import (
    adjust_currency_balance,
    adjust_user_quantity,
    ensure_server_row,
    ensure_user_row,
    sqlite_timestamp,
)
from utils.drills import set_container

GUILD = 7171
USER = 8181
OTHER_USER = 9191
TIMESTAMP = "%Y-%m-%d %H:%M:%S"


def parse(text):
    return datetime.strptime(text, TIMESTAMP).replace(tzinfo=timezone.utc)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class _DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

    async def asyncTearDown(self):
        self._dir.cleanup()


class HarvestClockTests(_DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.cog = MiningCog.__new__(MiningCog)
        self.cog.db = self.db
        self.cog._now = lambda: self.now

    async def add_drill(self, drill_type="diamond_drill", guild_id=GUILD, **columns):
        row = {"stored_amount": 0, "is_full": 0, "mined_until": None, "container_type": None}
        row.update(columns)
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, stored_amount, is_full, "
            "mined_until, container_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, USER, drill_type, row["stored_amount"], row["is_full"],
             row["mined_until"], row["container_type"]),
        )

    async def drill(self, drill_id):
        return await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))

    async def harvest_at(self, when):
        self.now = when
        await MiningCog.harvest_loop.coro(self.cog)

    async def test_a_drill_placed_just_before_a_tick_mines_only_since_it_was_placed(self):
        drill_id = await self.add_drill(guild_id=None)
        await MiningCog.mine_place.callback(self.cog, FakeInteraction(GUILD, USER), drill_id)
        placed_at = parse((await self.drill(drill_id))["mined_until"])

        await self.harvest_at(placed_at + timedelta(minutes=1))

        # A minute's worth, where it used to get the whole tick.
        per_minute = effective_rate("diamond_drill", 1) / 60
        self.assertEqual((await self.drill(drill_id))["stored_amount"], int(per_minute))
        self.assertLess(int(per_minute), int(per_minute * HARVEST_TICK_MINUTES))

    async def test_a_drill_mining_all_along_gets_a_whole_tick_every_tick(self):
        # NULL is every drill that predates the column: one whole tick.
        drill_id = await self.add_drill("iron_drill")
        for n in range(1, 13):
            await self.harvest_at(self.now + timedelta(minutes=HARVEST_TICK_MINUTES))
        self.assertEqual((await self.drill(drill_id))["stored_amount"], effective_rate("iron_drill", 1))

    async def test_each_tick_records_how_far_it_paid(self):
        drill_id = await self.add_drill("iron_drill")
        await self.harvest_at(self.now + timedelta(minutes=HARVEST_TICK_MINUTES))
        self.assertEqual((await self.drill(drill_id))["mined_until"], sqlite_timestamp(self.now))

    async def test_a_late_tick_still_pays_one_tick(self):
        drill_id = await self.add_drill(
            mined_until=sqlite_timestamp(self.now - timedelta(days=2))
        )
        await self.harvest_at(self.now)
        per_tick = effective_rate("diamond_drill", 1) * HARVEST_TICK_MINUTES / 60
        self.assertEqual((await self.drill(drill_id))["stored_amount"], int(per_tick))

    async def test_collecting_a_full_drill_restarts_its_clock(self):
        long_ago = sqlite_timestamp(self.now - timedelta(days=1))
        full = await self.add_drill(
            stored_amount=BASE_STORAGE_CAPACITY, is_full=1, mined_until=long_ago
        )
        await self.db.execute(COLLECT_EMPTY_DRILL_SQL, (full, BASE_STORAGE_CAPACITY))
        row = await self.drill(full)
        self.assertEqual((row["stored_amount"], row["is_full"]), (0, 0))
        self.assertGreater(row["mined_until"], long_ago)

    async def test_collecting_a_drill_that_never_stopped_keeps_its_clock(self):
        recent = sqlite_timestamp(self.now - timedelta(minutes=3))
        mining = await self.add_drill(stored_amount=10, mined_until=recent)
        await self.db.execute(COLLECT_EMPTY_DRILL_SQL, (mining, 10))
        self.assertEqual((await self.drill(mining))["mined_until"], recent)

    async def test_a_container_that_frees_a_full_drill_restarts_its_clock(self):
        long_ago = sqlite_timestamp(self.now - timedelta(days=1))
        full = await self.add_drill(
            stored_amount=BASE_STORAGE_CAPACITY, is_full=1, mined_until=long_ago
        )
        async with self.db.transaction() as tx:
            await set_container(tx, await self.drill(full), "iron_container")
        row = await self.drill(full)
        self.assertEqual(row["is_full"], 0)
        self.assertGreater(row["mined_until"], long_ago)

    async def test_a_container_that_changes_nothing_leaves_the_clock_alone(self):
        recent = sqlite_timestamp(self.now - timedelta(minutes=3))
        # Not full, so it was mining all along; and a full drill that loses
        # its container is still full, so it hasn't started mining either.
        mining = await self.add_drill(stored_amount=10, mined_until=recent)
        still_full = await self.add_drill(
            stored_amount=effective_capacity("iron_container"), is_full=1,
            mined_until=recent, container_type="iron_container",
        )
        async with self.db.transaction() as tx:
            await set_container(tx, await self.drill(mining), "iron_container")
            await set_container(tx, await self.drill(still_full), None)
        self.assertEqual((await self.drill(mining))["mined_until"], recent)
        self.assertEqual((await self.drill(still_full))["mined_until"], recent)


class PressClockTests(_DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.cog = PressCog.__new__(PressCog)
        self.cog.db = self.db
        self.cog._now = lambda: self.now

    async def queue(self, queued_at, product="ruby", user_id=USER):
        return await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, queued_at) "
            "VALUES (?, ?, 'press', ?, 1, ?)",
            (GUILD, user_id, product, sqlite_timestamp(queued_at)),
        )

    async def progress(self):
        row = await self.db.fetchone(
            "SELECT press_progress FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        return row["press_progress"]

    async def test_a_job_queued_just_before_a_tick_is_paid_only_for_its_wait(self):
        await self.queue(self.now - timedelta(minutes=1))
        await PressCog.process_loop.coro(self.cog)
        # One minute of a level 1 press, in press-days.
        self.assertAlmostEqual(await self.progress(), press_rate_per_day(1) / (24 * 60))

    async def test_a_run_under_way_gets_a_whole_tick(self):
        await self.queue(self.now - timedelta(days=3))
        await PressCog.process_loop.coro(self.cog)
        self.assertAlmostEqual(
            await self.progress(), press_rate_per_day(1) * PRESS_TICK_MINUTES / (24 * 60)
        )

    async def queue_through_the_command(self):
        recipe = PRESS_RECIPES["ruby"]
        for material_id, quantity in recipe["inputs"].items():
            await adjust_user_quantity(self.db, USER, material_id, quantity)
        await adjust_currency_balance(self.db, GUILD, USER, 1_000_000)
        await PressCog.press_craft.callback(
            self.cog, FakeInteraction(GUILD, USER), app_commands.Choice(name="Ruby", value="ruby")
        )

    async def test_a_job_queued_onto_an_empty_press_does_not_inherit_the_last_runs_leftover(self):
        await self.db.execute(
            "UPDATE server_config SET press_progress = 0.4 WHERE guild_id = ?", (GUILD,)
        )
        await self.queue_through_the_command()
        self.assertEqual(await self.progress(), 0.0)

    async def test_a_job_queued_behind_another_leaves_its_progress_alone(self):
        # Someone else's, so this player's own queue cap has room.
        await self.queue(self.now - timedelta(days=3), product="diamond", user_id=OTHER_USER)
        await self.db.execute(
            "UPDATE server_config SET press_progress = 0.4 WHERE guild_id = ?", (GUILD,)
        )
        await self.queue_through_the_command()
        self.assertEqual(await self.progress(), 0.4)


if __name__ == "__main__":
    unittest.main()
