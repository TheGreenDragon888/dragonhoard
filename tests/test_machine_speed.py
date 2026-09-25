"""
How fast a machine runs, and from when.

Two rules, both newer than the machines themselves:

  * Speed moves between levels with the fees paid in (data/materials.py:
    effective_level) rather than jumping only when a threshold is crossed.
  * A job is worked from when it was queued, never before (utils/db_helpers.py:
    ProductionClock). Each tick used to credit a whole tick's work to whatever
    was at the head of the queue however recently it had arrived, and to spend
    whatever fraction the last job had left behind on it too - so a furnace
    fast enough to make an item a tick filled a fresh one-item order on the
    next tick, seconds after the command, against a receipt quoting minutes.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cogs.furnace import PROCESS_TICK_MINUTES, FurnaceCog
from data.materials import (
    blast_furnace_rate,
    effective_level,
    factory_rate,
    furnace_rate,
    press_rate_per_day,
    scrapper_rate,
    upgrade_threshold,
)
from database.db import Database
from utils.db_helpers import (
    ProductionClock,
    bank_infrastructure_fee,
    ensure_server_row,
    ensure_user_row,
    get_user_quantity,
    guilds_with_queued_work,
    machine_speed_level,
)
from utils.formatting import format_rate

GUILD = 5150
USER = 6160
TIMESTAMP = "%Y-%m-%d %H:%M:%S"


class EffectiveLevelTests(unittest.TestCase):
    def test_halfway_through_level_one_is_halfway_between_the_speeds(self):
        # A server starts at level 1 with nothing paid, so level 1 runs from
        # zero to the level 2 threshold.
        halfway = upgrade_threshold(2) / 2
        self.assertEqual(furnace_rate(effective_level(1, halfway)), 7.5)

    def test_a_machine_with_nothing_paid_runs_at_its_whole_level(self):
        level = effective_level(1, 0.0)
        self.assertEqual(
            [furnace_rate(level), blast_furnace_rate(level), factory_rate(level),
             press_rate_per_day(level), scrapper_rate(level)],
            [5, 1, 1, 1, 2],
        )

    def test_speed_is_continuous_across_every_level_up(self):
        """Arriving at a threshold from below and landing exactly on it (which
        levels the machine up) give the same speed - no jump at the moment a
        fee crosses it."""
        for level in range(1, 8):
            threshold = upgrade_threshold(level + 1)
            below = effective_level(level, threshold - 1e-9)
            at = effective_level(level + 1, threshold)
            self.assertAlmostEqual(below, at, places=6)
            self.assertEqual(at, level + 1)

    def test_it_is_measured_across_the_level_not_against_the_next_threshold(self):
        # Level 2 runs from 5 to 25 collected. 15 is halfway through that,
        # although the status embed's "15.00 / 25.00" reads as more.
        self.assertEqual(effective_level(2, 15.0), 2.5)
        # The ratio reading would start each level already
        # 1 / UPGRADE_THRESHOLD_STEP of the way in; this starts it at zero.
        self.assertEqual(effective_level(2, upgrade_threshold(2)), 2)

    def test_a_stored_level_out_of_step_with_its_fees_reads_as_that_level(self):
        # Tests (and only tests) set a level without paying for it.
        self.assertEqual(effective_level(5, 0.0), 5)
        self.assertEqual(effective_level(1, 1e9), 2)


class FormatRateTests(unittest.TestCase):
    def test_single_digits_get_one_decimal_place_and_no_trailing_zero(self):
        self.assertEqual(format_rate(7.5), "7.5")
        self.assertEqual(format_rate(5), "5")
        self.assertEqual(format_rate(5.0), "5")

    def test_ten_and_up_are_whole_numbers(self):
        self.assertEqual(format_rate(12.5), "12")
        self.assertEqual(format_rate(1500.25), "1,500")

    def test_it_never_shows_a_speed_not_yet_reached(self):
        self.assertEqual(format_rate(9.99), "9.9")
        just_short = furnace_rate(effective_level(2, upgrade_threshold(3) - 0.01))
        self.assertEqual(format_rate(just_short), "14")

    def test_the_unit_agrees_with_the_figure_printed(self):
        self.assertEqual(format_rate(1, "item"), "1 item")
        self.assertEqual(format_rate(1.05, "item"), "1 item")
        self.assertEqual(format_rate(1.5, "batch"), "1.5 batches")
        self.assertEqual(format_rate(2, "press-day"), "2 press-days")


class ProductionClockTests(unittest.TestCase):
    """The accrual rules on their own, with the time passed in."""

    def setUp(self):
        self.start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.clock = ProductionClock(PROCESS_TICK_MINUTES)
        self.tick = timedelta(minutes=PROCESS_TICK_MINUTES)

    def stamp(self, when):
        return when.strftime(TIMESTAMP)

    def test_a_job_queued_just_before_a_tick_gets_only_the_time_it_waited(self):
        # 15/hour makes more than one item a tick - the case that used to fill
        # a fresh one-item order on the very next tick.
        queued = self.start - timedelta(seconds=1)
        self.assertEqual(self.clock.earn(GUILD, 15, self.stamp(queued), self.start), 0)
        # Four minutes is one item at 15/hour; the next tick is past that.
        self.assertEqual(self.clock.earn(GUILD, 15, self.stamp(queued), self.start + self.tick), 1)

    def test_a_continuous_run_carries_its_fractions(self):
        queued = self.stamp(self.start)
        earned = sum(
            self.clock.earn(GUILD, 5, queued, self.start + self.tick * n) for n in range(1, 13)
        )
        self.assertEqual(earned, 5)

    def test_what_is_left_when_a_queue_empties_is_not_spent_on_the_next_job(self):
        first = self.stamp(self.start)
        # 10/hour: 5/6 a tick, so two ticks make one item and leave 2/3.
        self.clock.earn(GUILD, 10, first, self.start + self.tick)
        self.assertEqual(self.clock.earn(GUILD, 10, first, self.start + self.tick * 2), 1)

        # The queue sits empty for a day - the loop doesn't visit the server -
        # and then a new job arrives. Its first tick is 5/6, not 2/3 + 5/6.
        later = self.start + timedelta(days=1)
        self.assertEqual(self.clock.earn(GUILD, 10, self.stamp(later), later + self.tick), 0)

    def test_a_restart_hands_a_long_running_job_at_most_one_tick(self):
        """This state is in memory, so a restarted bot sees every queue as a
        new run - one whose work was queued long ago."""
        long_ago = self.stamp(self.start - timedelta(days=3))
        fresh = ProductionClock(PROCESS_TICK_MINUTES)
        # 60/hour is 5 a tick; three days of it would be 4,320.
        self.assertEqual(fresh.earn(GUILD, 60, long_ago, self.start), 5)

    def test_a_late_tick_earns_one_tick(self):
        queued = self.stamp(self.start)
        self.clock.earn(GUILD, 60, queued, self.start + self.tick)
        self.assertEqual(self.clock.earn(GUILD, 60, queued, self.start + self.tick * 10), 5)

    def test_servers_are_clocked_separately(self):
        queued = self.stamp(self.start)
        self.assertEqual(self.clock.earn(GUILD, 60, queued, self.start + self.tick), 5)
        later = self.start + self.tick
        self.assertEqual(self.clock.earn(GUILD + 1, 60, self.stamp(later), later), 0)


class _NoGuildBot:
    """The furnace's loop ends with an auto-smelt pass that asks for the
    guild; None makes it decline."""

    def get_guild(self, guild_id):
        return None


class FurnaceLoopTests(unittest.IsolatedAsyncioTestCase):
    """The reported bug, through the real loop: a smelting order filled
    moments after it was placed."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.cog = FurnaceCog.__new__(FurnaceCog)
        self.cog.db = self.db
        self.cog.bot = _NoGuildBot()
        self.cog._production = ProductionClock(PROCESS_TICK_MINUTES, now=lambda: self.now)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def queue(self, quantity, queued_at):
        return await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, queued_at) "
            "VALUES (?, ?, 'furnace', 'iron', ?, ?)",
            (GUILD, USER, quantity, queued_at.strftime(TIMESTAMP)),
        )

    async def run_at(self, when):
        self.now = when
        await FurnaceCog.process_loop.coro(self.cog)

    async def bars(self):
        return await get_user_quantity(self.db, USER, "iron")

    async def test_an_order_placed_just_before_a_tick_is_not_filled_by_it(self):
        await self.db.execute(
            "UPDATE server_config SET furnace_level = 3 WHERE guild_id = ?", (GUILD,)
        )
        await self.queue(1, self.now - timedelta(seconds=1))
        await self.run_at(self.now)
        self.assertEqual(await self.bars(), 0)

        # 15/hour is four minutes a bar, and the next tick is five away.
        await self.run_at(self.now + timedelta(minutes=PROCESS_TICK_MINUTES))
        self.assertEqual(await self.bars(), 1)

    async def test_fees_part_way_to_a_level_speed_the_loop_up(self):
        start = self.now
        await self.queue(10, start)
        await bank_infrastructure_fee(self.db, GUILD, "furnace", upgrade_threshold(2) / 2)
        # 7.5/hour for forty minutes is exactly 5 bars; at level 1's flat 5/hour
        # it would be 3.
        for n in range(1, 9):
            await self.run_at(start + timedelta(minutes=PROCESS_TICK_MINUTES * n))
        self.assertEqual(await self.bars(), 5)


class WorkListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def queue(self, queued_at, status="queued"):
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, queued_at, status) "
            "VALUES (?, ?, 'furnace', 'iron', 1, ?, ?)",
            (GUILD, USER, queued_at, status),
        )

    async def test_work_started_is_the_oldest_live_job(self):
        await self.queue("2026-01-01 09:00:00", status="complete")
        await self.queue("2026-01-01 11:00:00")
        await self.queue("2026-01-01 10:00:00", status="in_progress")
        await bank_infrastructure_fee(self.db, GUILD, "furnace", 7.0)

        [row] = await guilds_with_queued_work(self.db, "furnace")
        self.assertEqual(row["work_started"], "2026-01-01 10:00:00")
        self.assertEqual((row["level"], row["collected"]), (2, 7.0))

    async def test_a_receipt_reads_the_speed_its_own_fee_just_bought(self):
        self.assertEqual(await machine_speed_level(self.db, GUILD, "furnace"), 1)
        await bank_infrastructure_fee(self.db, GUILD, "furnace", upgrade_threshold(2) / 2)
        self.assertEqual(await machine_speed_level(self.db, GUILD, "furnace"), 1.5)


if __name__ == "__main__":
    unittest.main()
