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
from utils.db_helpers import adjust_user_quantity, ensure_user_row
from utils.drills import collection_summary_lines, material_breakdown_lines

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

    async def test_the_totals_lookup_reports_the_post_credit_amounts(self):
        """The query behind /collect's "(N total)" figures, run the way the
        command runs it: after the credits, in one statement, for exactly the
        materials that just came in.

        It has to report what the player holds ALTOGETHER, not what the haul
        contained - that difference is the whole point of showing it, and it's
        why this is a read rather than a sum of the breakdown."""
        await ensure_user_row(self.db, USER)
        await adjust_user_quantity(self.db, USER, "iron_ore", 900)   # already banked
        await adjust_user_quantity(self.db, USER, "coal", 5)

        breakdown = {"iron_ore": 100, "coal": 20}
        for material_id, quantity in breakdown.items():
            await adjust_user_quantity(self.db, USER, material_id, quantity)

        placeholders = ",".join("?" * len(breakdown))
        rows = await self.db.fetchall(
            f"SELECT material_id, quantity FROM user_materials "
            f"WHERE user_id = ? AND material_id IN ({placeholders})",
            (USER, *breakdown),
        )
        totals = {row["material_id"]: row["quantity"] for row in rows}
        self.assertEqual(totals, {"iron_ore": 1000, "coal": 25})

    async def test_the_totals_lookup_ignores_materials_not_in_the_haul(self):
        await ensure_user_row(self.db, USER)
        await adjust_user_quantity(self.db, USER, "iron_ore", 10)
        await adjust_user_quantity(self.db, USER, "diamond", 1)

        rows = await self.db.fetchall(
            "SELECT material_id, quantity FROM user_materials "
            "WHERE user_id = ? AND material_id IN (?)",
            (USER, "iron_ore"),
        )
        self.assertEqual([row["material_id"] for row in rows], ["iron_ore"])


class CollectionLineTests(unittest.TestCase):
    """How a haul renders once the totals are in hand."""

    def test_a_line_names_the_haul_and_the_new_total(self):
        lines = material_breakdown_lines({"iron_ore": 120}, {"iron_ore": 1340})
        self.assertEqual(len(lines), 1)
        self.assertIn("120 Iron Ore", lines[0])
        self.assertIn("(1,340 total)", lines[0])

    def test_totals_use_thousands_separators(self):
        lines = material_breakdown_lines({"iron_ore": 12345}, {"iron_ore": 1234567})
        self.assertIn("12,345", lines[0])
        self.assertIn("1,234,567", lines[0])

    def test_without_totals_the_plain_form_is_used(self):
        # /mine remove and the /factory upgrade receipt pass no totals, and
        # must not gain an empty or zero parenthetical.
        lines = material_breakdown_lines({"iron_ore": 120})
        self.assertNotIn("total", lines[0])
        self.assertIn("120 Iron Ore", lines[0])

    def test_a_material_missing_from_totals_falls_back_rather_than_claiming_zero(self):
        # The player just received some of it, so zero is the one figure that
        # cannot be true - better to say nothing than to say something false.
        lines = material_breakdown_lines({"iron_ore": 5, "coal": 3}, {"coal": 3})
        iron_line = next(line for line in lines if "Iron Ore" in line)
        self.assertNotIn("total", iron_line)

    def test_lines_are_ordered_the_same_way_every_time(self):
        haul = {"coal": 1, "iron_ore": 2, "copper_ore": 3}
        self.assertEqual(
            material_breakdown_lines(haul), material_breakdown_lines(dict(reversed(haul.items())))
        )

    def test_an_empty_haul_produces_no_lines(self):
        self.assertEqual(material_breakdown_lines({}, {}), [])


if __name__ == "__main__":
    unittest.main()
