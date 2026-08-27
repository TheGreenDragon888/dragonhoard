"""
Tests for /mine status's "Server Mining Speed" figure and drill storage
display against a real database.

Server Mining Speed, and the drill-type breakdown beneath it, used to be
computed from every OTHER player's drills only, under a field literally named
"Other Active Drills in Server" - accurate for what the field was, but not
what "server mining speed" should mean: both the total and the breakdown have
to include the viewer's own drills alongside everyone else's.

The storage display (stored_amount/capacity per drill) has to read with
thousands separators now that a Diamond Container holds 32,000.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cogs.mining import MiningCog
from database.db import Database
from utils.db_helpers import ensure_server_row, ensure_user_row

GUILD = 7171
USER = 6161
OTHER_USER = 6162


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.guild = None  # skips human_member_count, unused by mine_status's speed line
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class MineStatusTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

        self.cog = MiningCog.__new__(MiningCog)
        self.cog.db = self.db

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def add_drill(self, owner_id, drill_type="iron_drill", level=1, is_full=0,
                         container_type=None, stored_amount=0):
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, is_full, "
            "container_type, stored_amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (GUILD, owner_id, drill_type, level, is_full, container_type, stored_amount),
        )

    async def field(self, name):
        interaction = FakeInteraction(GUILD, USER)
        # mine_status is wrapped in an app_commands.Command by the decorator,
        # so the cog instance has to be passed to the raw callback by hand
        # rather than called as a bound method.
        await MiningCog.mine_status.callback(self.cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        embed = kwargs["embeds"][0] if "embeds" in kwargs else kwargs["embed"]
        return next((f.value for f in embed.fields if f.name == name), None)


class MineStatusServerSpeedTests(MineStatusTestCase):
    async def test_the_viewers_own_drill_counts_toward_the_total(self):
        # Iron Drill at level 1 mines 5/hour = 120/day, with no one else here.
        await self.add_drill(USER)
        field = await self.field("Server Mining Speed")
        self.assertIn("120/day", field)

    async def test_it_shows_even_when_no_one_else_is_mining(self):
        # The old field only appeared if someone ELSE had a drill placed -
        # a solo miner saw nothing at all.
        await self.add_drill(USER)
        field = await self.field("Server Mining Speed")
        self.assertIsNotNone(field)

    async def test_a_full_drill_does_not_count_toward_the_total(self):
        # A full drill has stopped mining until /collect empties it - the
        # total should reflect only the still-active one alongside it.
        await self.add_drill(USER, is_full=1)
        await self.add_drill(OTHER_USER, "steel_drill")  # 7.5/hour = 180/day
        field = await self.field("Server Mining Speed")
        self.assertIn("180/day", field)

    async def test_other_players_drills_are_added_to_the_total(self):
        await self.add_drill(USER)                          # 5/hour
        await self.add_drill(OTHER_USER, "steel_drill")      # 7.5/hour
        field = await self.field("Server Mining Speed")
        self.assertIn("300/day", field)  # (5 + 7.5) * 24

    async def test_the_breakdown_includes_the_viewers_own_drill_type(self):
        # The breakdown is server-wide, not "everyone but you" - a solo miner
        # should see their own drill type represented in it too.
        await self.add_drill(USER, "diamond_drill")
        await self.add_drill(OTHER_USER, "steel_drill")
        field = await self.field("Server Mining Speed")
        self.assertIn("Diamond", field)
        self.assertIn("Steel", field)

    async def test_the_first_line_is_just_the_number_with_no_repeated_label(self):
        # The field's own title already says "Server Mining Speed" - the
        # first line of the value shouldn't repeat it.
        await self.add_drill(USER)
        field = await self.field("Server Mining Speed")
        self.assertEqual(field.splitlines()[0], "120/day")


class MineStatusDrillStorageTests(MineStatusTestCase):
    async def test_a_four_figure_capacity_is_comma_separated(self):
        await self.add_drill(USER, "diamond_drill", container_type="diamond_container", stored_amount=12345)
        field = await self.field("Your Drills")
        self.assertIn("12,345/32,000", field)

    async def test_a_three_figure_amount_has_no_stray_comma(self):
        await self.add_drill(USER, stored_amount=42)
        field = await self.field("Your Drills")
        self.assertIn("42/100", field)
        self.assertNotIn(",", field)


if __name__ == "__main__":
    unittest.main()
