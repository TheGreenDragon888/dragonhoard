"""
Tests for the per-user queue cap, which as of 1.1 scales with the machine's
level.

queue_room replaced four near-identical copies of this check across the
furnace, factory and press, so these run once against the shared helper rather
than three times against three copies. The arithmetic itself is
effective_max_queue; what's tested here is that the right numbers reach it and
that one machine's queue can't consume another's.
"""
import tempfile
import unittest
from pathlib import Path

from database.db import Database
from data.materials import effective_max_queue
from utils.db_helpers import (
    MACHINES,
    ensure_server_row,
    queue_full_message,
    queue_room,
)

GUILD = 8484
USER = 4242
OTHER_USER = 9999


class EffectiveMaxQueueTests(unittest.TestCase):
    def test_the_cap_is_the_base_times_the_level(self):
        self.assertEqual(effective_max_queue(5, 1), 5)
        self.assertEqual(effective_max_queue(5, 2), 10)
        self.assertEqual(effective_max_queue(25, 4), 100)

    def test_a_level_one_machine_is_unchanged(self):
        # Nothing may regress for a server that has never levelled anything.
        for base in (1, 5, 25, 50):
            self.assertEqual(effective_max_queue(base, 1), base)

    def test_a_zero_level_is_floored_rather_than_zeroing_the_queue(self):
        # Defensive: nothing decrements a level, but a zero here would make the
        # machine unusable rather than merely stingy.
        self.assertEqual(effective_max_queue(5, 0), 5)


class QueueRoomTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def set_machine(self, machine, base, level):
        await self.db.execute(
            f"UPDATE server_config SET {machine}_max_queue = ?, {machine}_level = ? WHERE guild_id = ?",
            (base, level, GUILD),
        )

    async def queue(self, machine, quantity, user_id=USER, status="queued"):
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, status) "
            "VALUES (?, ?, ?, 'iron', ?, ?)",
            (GUILD, user_id, machine, quantity, status),
        )


class QueueRoomTests(QueueRoomTestCase):
    async def test_it_reports_the_effective_cap_not_the_base(self):
        await self.set_machine("furnace", 25, 3)
        room = await queue_room(self.db, GUILD, USER, "furnace", 1)
        self.assertEqual((room.base, room.level, room.effective), (25, 3, 75))

    async def test_a_levelled_machine_allows_more_than_its_base(self):
        await self.set_machine("factory", 5, 2)
        await self.queue("factory", 7)
        room = await queue_room(self.db, GUILD, USER, "factory", 3)
        self.assertTrue(room.fits)  # 7 + 3 <= 5 * 2

    async def test_exactly_filling_the_cap_still_fits(self):
        await self.set_machine("factory", 5, 2)
        await self.queue("factory", 6)
        self.assertTrue((await queue_room(self.db, GUILD, USER, "factory", 4)).fits)
        self.assertFalse((await queue_room(self.db, GUILD, USER, "factory", 5)).fits)

    async def test_it_counts_items_rather_than_jobs(self):
        await self.set_machine("furnace", 25, 1)
        await self.queue("furnace", 20)
        room = await queue_room(self.db, GUILD, USER, "furnace", 10)
        self.assertEqual(room.queued, 20)
        self.assertFalse(room.fits)

    async def test_completed_jobs_stop_counting(self):
        await self.set_machine("furnace", 25, 1)
        await self.queue("furnace", 25, status="complete")
        room = await queue_room(self.db, GUILD, USER, "furnace", 25)
        self.assertEqual(room.queued, 0)
        self.assertTrue(room.fits)

    async def test_in_progress_jobs_still_count(self):
        await self.set_machine("furnace", 25, 1)
        await self.queue("furnace", 25, status="in_progress")
        self.assertFalse((await queue_room(self.db, GUILD, USER, "furnace", 1)).fits)

    async def test_one_machines_queue_does_not_consume_another(self):
        for machine in MACHINES:
            await self.set_machine(machine, 5, 1)
            await self.queue(machine, 5)
        for machine in MACHINES:
            with self.subTest(machine=machine):
                room = await queue_room(self.db, GUILD, USER, machine, 1)
                self.assertEqual(room.queued, 5)

    async def test_one_users_queue_does_not_consume_anothers(self):
        await self.set_machine("furnace", 25, 1)
        await self.queue("furnace", 25, user_id=OTHER_USER)
        room = await queue_room(self.db, GUILD, USER, "furnace", 25)
        self.assertEqual(room.queued, 0)
        self.assertTrue(room.fits)

    async def test_every_machine_can_be_asked_about(self):
        # The whitelist is what makes interpolating `machine` into the column
        # names safe, so it has to actually cover every machine.
        for machine in MACHINES:
            with self.subTest(machine=machine):
                self.assertIsNotNone(await queue_room(self.db, GUILD, USER, machine, 1))

    async def test_an_unknown_machine_is_refused_rather_than_interpolated(self):
        with self.assertRaises(ValueError):
            await queue_room(self.db, GUILD, USER, "furnace_max_queue; DROP TABLE drills--", 1)

    async def test_a_server_with_no_config_row_does_not_crash(self):
        self.assertFalse((await queue_room(self.db, 12345, USER, "furnace", 1)).fits)


class QueueFullMessageTests(QueueRoomTestCase):
    async def test_the_rejection_explains_where_the_number_came_from(self):
        """A player who read "5 items" in /setup and is being refused at 15
        needs the multiplier spelled out, or the cap reads as a bug."""
        await self.set_machine("factory", 5, 3)
        await self.queue("factory", 15)
        room = await queue_room(self.db, GUILD, USER, "factory", 1)
        message = queue_full_message("factory", room)
        self.assertIn("15", message)   # the effective cap
        self.assertIn("5", message)    # the base
        self.assertIn("3", message)    # the level
        self.assertIn("factory", message)


if __name__ == "__main__":
    unittest.main()
