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
from data.materials import MINING_FOCUSES
from database.db import Database
from utils.db_helpers import ensure_server_row, ensure_user_row
from utils.mining_efficiency import set_efficiency
from utils.mining_focus import set_focus
from utils.mining_affinity import convert_gems, set_affinity

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
        self.guild = None  # mine_status reads nothing off the guild object
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
        self.db.close()
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


class MineStatusEnhancementsTests(MineStatusTestCase):
    """The "Mining Enhancements" field, which replaced 1.3.1's "Your Mining
    Focus".

    The rule that field exists to hold: it lists what a player has PAID FOR
    and nothing else. Every one of the three defaults reads identically to
    never having bought the feature (get_focus and friends return the default
    for a missing row), so listing unbought ones would fill the embed with
    rows of "None" advertising features the player can't reach - and there is
    no way for the field to tell those two states apart other than `unlocked`.
    """

    async def unlock_focus(self, focus_id="iron"):
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, focus_id, "2026-09-20")

    async def unlock_efficiency(self, efficiency_id="steel"):
        async with self.db.transaction() as tx:
            await set_efficiency(tx, USER, efficiency_id, "2026-09-20")

    async def unlock_affinity(self, affinity_id="diamond"):
        async with self.db.transaction() as tx:
            await set_affinity(tx, USER, affinity_id, "2026-09-20")

    async def test_a_player_who_has_bought_nothing_gets_no_field(self):
        await self.add_drill(USER)
        self.assertIsNone(await self.field("Mining Enhancements"))

    async def test_it_lists_only_what_has_been_unlocked(self):
        await self.unlock_focus()
        field = await self.field("Mining Enhancements")
        self.assertIn("Focus", field)
        self.assertNotIn("Efficiency", field)
        self.assertNotIn("Affinity", field)

    async def test_it_lists_all_three_once_all_three_are_unlocked(self):
        await self.unlock_focus()
        await self.unlock_efficiency()
        await self.unlock_affinity()
        field = await self.field("Mining Enhancements")
        for label in ("Focus", "Efficiency", "Affinity"):
            self.assertIn(label, field)

    async def test_the_chosen_option_is_named_not_just_the_feature(self):
        await self.unlock_focus("copper")
        field = await self.field("Mining Enhancements")
        self.assertIn(MINING_FOCUSES["copper"]["name"], field)

    async def test_affinity_progress_rides_along_with_the_affinity_line(self):
        await self.unlock_affinity("diamond")
        async with self.db.transaction() as tx:
            await convert_gems(tx, USER, {"ruby": 44})
        field = await self.field("Mining Enhancements")
        # 44 of the 45 rubies a diamond takes, as a percentage - the unit a
        # mixed haul can be quoted in honestly.
        self.assertIn("97%", field)

    async def test_a_affinity_with_nothing_accrued_shows_no_progress_line(self):
        await self.unlock_affinity("diamond")
        field = await self.field("Mining Enhancements")
        self.assertIn("Affinity", field)
        self.assertNotIn("toward", field)
