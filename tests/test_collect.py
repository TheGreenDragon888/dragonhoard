"""
Tests for /collect reaching every server a player has drills in.

Two halves: the two SELECTs the command picks between, run against a
throwaway database so a change to either one has to be deliberate, and the
pure rendering of a haul into lines. The rest of /collect needs a live
Interaction, so the guarded UPDATE it does per drill is covered by hand in
Discord.
"""
import tempfile
import unittest
from pathlib import Path

from cogs.mining import COLLECT_EVERYWHERE_SQL, COLLECT_HERE_SQL
from database.db import Database
from utils.db_helpers import adjust_user_quantity, ensure_user_row
from utils.drills import material_breakdown_lines

USER = 4242
OTHER_USER = 9999
HERE = 100
THERE = 200


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

    def test_lines_are_ordered_by_rarity_not_insertion(self):
        # RAW_MATERIAL_ORDER, commonest first - the same order regardless of
        # what order the haul dict happened to be built in.
        haul = {"diamond": 1, "coal": 1, "obsidian": 1, "iron_ore": 1, "ruby": 1, "copper_ore": 1}
        expected = ["Iron Ore", "Copper Ore", "Coal", "Ruby", "Obsidian", "Diamond"]
        for ordering in (haul, dict(reversed(haul.items()))):
            with self.subTest(ordering=list(ordering)):
                lines = material_breakdown_lines(ordering)
                for name, line in zip(expected, lines):
                    self.assertIn(name, line)

    def test_an_empty_haul_produces_no_lines(self):
        self.assertEqual(material_breakdown_lines({}, {}), [])


if __name__ == "__main__":
    unittest.main()
