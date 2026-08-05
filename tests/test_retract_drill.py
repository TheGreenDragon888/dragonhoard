"""
Tests for retract_drill, the shared "pull a placed drill back to its owner's
inventory" helper.

Three commands now go through it: leaving a server, /mine remove, and the
/factory upgrade auto-retraction added in 1.1. It exists because /mine remove
had a bug all three would otherwise have inherited - it read the drill OUTSIDE
its transaction, built the material breakdown from that stale stored_amount,
and then unplaced the drill with no stored_amount guard. A /collect committing
in between paid the same haul out twice.

test_a_collect_landing_first_credits_nothing is that bug, pinned.
"""
import tempfile
import unittest
from pathlib import Path

from database.db import Database
from utils.drills import DrillScope, drill_choices, is_local_drill, retract_drill

OWNER = 4242
STRANGER = 9999
HERE = 100
ELSEWHERE = 200


class DrillTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def add_drill(self, guild_id=HERE, owner_id=OWNER, stored_amount=0, **columns):
        defaults = {"drill_type": "iron_drill", "level": 1, "container_type": None,
                    "locked_job_id": None}
        defaults.update(columns)
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, container_type, "
            "stored_amount, is_full, locked_job_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, owner_id, defaults["drill_type"], defaults["level"],
                defaults["container_type"], stored_amount, 1 if stored_amount else 0,
                defaults["locked_job_id"],
            ),
        )
        return await self.drill(drill_id)

    async def drill(self, drill_id):
        return await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))

    async def total_materials(self, user_id=OWNER):
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(quantity), 0) AS total FROM user_materials WHERE user_id = ?",
            (user_id,),
        )
        return row["total"]


class RetractDrillTests(DrillTestCase):
    async def test_it_unplaces_the_drill_and_credits_its_contents(self):
        row = await self.add_drill(stored_amount=140)

        async with self.db.transaction() as tx:
            breakdown = await retract_drill(tx, row)

        self.assertEqual(sum(breakdown.values()), 140)
        self.assertEqual(await self.total_materials(), 140)
        after = await self.drill(row["drill_id"])
        self.assertIsNone(after["guild_id"])
        self.assertEqual(after["stored_amount"], 0)
        self.assertEqual(after["is_full"], 0)

    async def test_an_empty_drill_still_comes_home(self):
        row = await self.add_drill(stored_amount=0)

        async with self.db.transaction() as tx:
            breakdown = await retract_drill(tx, row)

        # An empty haul, not a failure - those are different answers and the
        # caller distinguishes them by `is None`.
        self.assertEqual(breakdown, {})
        self.assertIsNone((await self.drill(row["drill_id"]))["guild_id"])

    async def test_it_keeps_the_drills_level_and_container(self):
        row = await self.add_drill(
            stored_amount=10, level=6, drill_type="steel_drill",
            container_type="ruby_container",
        )

        async with self.db.transaction() as tx:
            await retract_drill(tx, row)

        after = await self.drill(row["drill_id"])
        self.assertEqual(after["level"], 6)
        self.assertEqual(after["drill_type"], "steel_drill")
        self.assertEqual(after["container_type"], "ruby_container")

    async def test_retracting_twice_credits_nothing_the_second_time(self):
        row = await self.add_drill(stored_amount=75)

        async with self.db.transaction() as tx:
            self.assertIsNotNone(await retract_drill(tx, row))
        async with self.db.transaction() as tx:
            self.assertIsNone(await retract_drill(tx, row))

        self.assertEqual(await self.total_materials(), 75)

    async def test_a_collect_landing_first_credits_nothing(self):
        """The 1.1 bug fix. `row` here is a pre-collect snapshot, exactly as a
        command that read the drill before opening its transaction would hold -
        and the drill has since been emptied. The stored_amount guard is what
        makes this credit zero instead of another 200."""
        row = await self.add_drill(stored_amount=200)

        # A /collect commits in the gap: contents credited, drill emptied.
        await self.db.execute(
            "UPDATE drills SET stored_amount = 0, is_full = 0 WHERE drill_id = ?",
            (row["drill_id"],),
        )
        await self.db.execute(
            "INSERT INTO users (user_id) VALUES (?)", (OWNER,)
        )
        await self.db.execute(
            "INSERT INTO user_materials (user_id, material_id, quantity) VALUES (?, 'iron_ore', 200)",
            (OWNER,),
        )

        async with self.db.transaction() as tx:
            self.assertIsNone(await retract_drill(tx, row))

        # 200 from the collect, and not a single item more.
        self.assertEqual(await self.total_materials(), 200)

    async def test_the_drill_is_left_placed_when_the_retraction_loses(self):
        row = await self.add_drill(stored_amount=50)
        await self.db.execute(
            "UPDATE drills SET stored_amount = 10 WHERE drill_id = ?", (row["drill_id"],)
        )

        async with self.db.transaction() as tx:
            self.assertIsNone(await retract_drill(tx, row))

        self.assertEqual((await self.drill(row["drill_id"]))["guild_id"], HERE)


class IsLocalDrillTests(DrillTestCase):
    async def test_an_unplaced_drill_is_local_anywhere(self):
        row = await self.add_drill(guild_id=None)
        self.assertTrue(is_local_drill(row, HERE))
        self.assertTrue(is_local_drill(row, ELSEWHERE))

    async def test_a_drill_placed_here_is_local(self):
        row = await self.add_drill(guild_id=HERE)
        self.assertTrue(is_local_drill(row, HERE))

    async def test_a_drill_placed_elsewhere_is_not(self):
        row = await self.add_drill(guild_id=ELSEWHERE)
        self.assertFalse(is_local_drill(row, HERE))


class DrillScopeTests(DrillTestCase):
    async def choices(self, scope, guild_id=HERE, **kwargs):
        result = await drill_choices(
            self.db, OWNER, "", scope=scope, guild_id=guild_id, **kwargs
        )
        return {choice.value for choice in result}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.unplaced = (await self.add_drill(guild_id=None))["drill_id"]
        self.here = (await self.add_drill(guild_id=HERE))["drill_id"]
        self.elsewhere = (await self.add_drill(guild_id=ELSEWHERE))["drill_id"]
        self.locked = (await self.add_drill(guild_id=None, locked_job_id=99))["drill_id"]
        self.someone_elses = (await self.add_drill(guild_id=HERE, owner_id=STRANGER))["drill_id"]

    async def test_local_covers_unplaced_and_placed_here(self):
        """The scope 1.1 needed and the old boolean pair couldn't express -
        /factory upgrade and /mine attach both want exactly this set."""
        self.assertEqual(await self.choices(DrillScope.LOCAL), {self.unplaced, self.here})

    async def test_unplaced_covers_only_inventory(self):
        self.assertEqual(await self.choices(DrillScope.UNPLACED), {self.unplaced})

    async def test_placed_here_covers_only_this_server(self):
        self.assertEqual(await self.choices(DrillScope.PLACED_HERE), {self.here})

    async def test_any_covers_every_drill_you_own(self):
        self.assertEqual(
            await self.choices(DrillScope.ANY),
            {self.unplaced, self.here, self.elsewhere},
        )

    async def test_no_scope_ever_offers_someone_elses_drill(self):
        for scope in DrillScope:
            with self.subTest(scope=scope):
                self.assertNotIn(self.someone_elses, await self.choices(scope))

    async def test_locked_drills_are_excluded_by_default(self):
        for scope in (DrillScope.ANY, DrillScope.LOCAL, DrillScope.UNPLACED):
            with self.subTest(scope=scope):
                self.assertNotIn(self.locked, await self.choices(scope))

    async def test_a_guild_scoped_lookup_without_a_guild_is_a_programming_error(self):
        # Silently returning every drill in existence would be a data leak
        # dressed up as a convenience.
        for scope in (DrillScope.LOCAL, DrillScope.PLACED_HERE):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    await drill_choices(self.db, OWNER, "", scope=scope, guild_id=None)


if __name__ == "__main__":
    unittest.main()
