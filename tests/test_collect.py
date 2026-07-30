"""
Tests for /collect reaching every server a player has drills in.

Two halves: the per-server summary lines, which are pure, and the two SELECTs
the command picks between, run against a throwaway database so a change to
either one has to be deliberate. The rest of /collect needs a live Interaction,
so the guarded UPDATE it does per drill is covered by hand in Discord.
"""
import tempfile
import unittest
from pathlib import Path

from cogs.mining import COLLECT_EVERYWHERE_SQL, COLLECT_HERE_SQL
from database.db import Database
from utils.drills import collection_summary_lines

USER = 4242
OTHER_USER = 9999
HERE = 100
THERE = 200
LEFT = 300


class CollectionSummaryTests(unittest.TestCase):
    NAMES = {HERE: "Dragon's Den", THERE: "Test Server"}

    def test_the_current_server_comes_first_even_when_it_is_the_smallest(self):
        lines = collection_summary_lines(
            [(THERE, 500), (HERE, 10)], self.NAMES, current_guild_id=HERE
        )
        self.assertEqual(
            lines, ["**Dragon's Den** - 1 drill · 10", "**Test Server** - 1 drill · 500"]
        )

    def test_other_servers_follow_by_total_descending(self):
        names = {1: "one", 2: "two", 3: "three"}
        lines = collection_summary_lines(
            [(1, 5), (2, 50), (3, 500)], names, current_guild_id=None
        )
        self.assertEqual(
            lines, ["**three** - 1 drill · 500", "**two** - 1 drill · 50", "**one** - 1 drill · 5"]
        )

    def test_drills_in_one_server_are_tallied_together(self):
        lines = collection_summary_lines(
            [(HERE, 100), (HERE, 250), (HERE, 3)], self.NAMES, current_guild_id=HERE
        )
        self.assertEqual(lines, ["**Dragon's Den** - 3 drills · 353"])

    def test_a_server_the_bot_cannot_see_still_gets_named(self):
        # The drill kept mining after the bot left, so the haul is real and has
        # to be attributed to something.
        lines = collection_summary_lines([(LEFT, 42)], self.NAMES, current_guild_id=HERE)
        self.assertEqual(lines, [f"**server {LEFT}** - 1 drill · 42"])

    def test_totals_use_thousands_separators(self):
        lines = collection_summary_lines([(HERE, 12345)], self.NAMES, current_guild_id=HERE)
        self.assertEqual(lines, ["**Dragon's Den** - 1 drill · 12,345"])

    def test_servers_past_the_limit_collapse_into_one_line(self):
        hauls = [(guild_id, guild_id) for guild_id in range(1, 15)]
        lines = collection_summary_lines(hauls, {}, current_guild_id=None, limit=10)
        self.assertEqual(len(lines), 11)
        self.assertEqual(lines[-1], "... and 4 more")

    def test_no_hauls_produces_no_lines(self):
        self.assertEqual(collection_summary_lines([], self.NAMES, current_guild_id=HERE), [])


class CollectQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()

        # Two servers holding a full drill each, plus the three rows the query
        # has to leave alone: an unplaced drill, an empty placed one, and
        # someone else's.
        self.here_drill = await self.add_drill(USER, HERE, 120)
        self.there_drill = await self.add_drill(USER, THERE, 80)
        await self.add_drill(USER, None, 0)
        await self.add_drill(USER, HERE, 0)
        await self.add_drill(OTHER_USER, HERE, 300)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def add_drill(self, owner_id, guild_id, stored_amount):
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, stored_amount) "
            "VALUES (?, ?, 'iron_drill', ?)",
            (guild_id, owner_id, stored_amount),
        )

    async def test_the_default_collects_from_every_server(self):
        rows = await self.db.fetchall(COLLECT_EVERYWHERE_SQL, (USER,))
        self.assertEqual(
            sorted(row["drill_id"] for row in rows), [self.here_drill, self.there_drill]
        )

    async def test_here_collects_only_from_the_current_server(self):
        rows = await self.db.fetchall(COLLECT_HERE_SQL, (HERE, USER))
        self.assertEqual([row["drill_id"] for row in rows], [self.here_drill])

    async def test_neither_query_touches_another_players_drills(self):
        for sql, params in ((COLLECT_EVERYWHERE_SQL, (USER,)), (COLLECT_HERE_SQL, (HERE, USER))):
            rows = await self.db.fetchall(sql, params)
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["owner_id"], USER)

    async def test_an_unplaced_drill_is_never_collected(self):
        # It can't hold anything (the CHECK on drills enforces that), but the
        # query still has to exclude it rather than rely on the constraint.
        rows = await self.db.fetchall(COLLECT_EVERYWHERE_SQL, (USER,))
        for row in rows:
            self.assertIsNotNone(row["guild_id"])


if __name__ == "__main__":
    unittest.main()
