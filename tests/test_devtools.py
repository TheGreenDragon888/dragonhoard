"""
Tests for the beta-only dev item-giving tool (cogs/devtools.py).

Layer 3 (@app_commands.checks.has_permissions) is never directly unit-tested
anywhere in this codebase - there's no Mock-based interaction harness for
discord.py's own permission-check machinery, and /setup (cogs/setup.py)
doesn't test it either. This trusts discord.py's own behavior there, same as
/setup already does, and focuses on what's cheaply and directly testable:
layer 2 (BetaDevGroup.interaction_check) and the pure item-list logic.
"""
import unittest
from unittest.mock import AsyncMock, patch

from cogs.devtools import GIVEABLE_ITEMS, DevToolsCog
from data.materials import ALL_MATERIALS, DRILLS

devtools_group = DevToolsCog.devtools_group

BETA_GUILD = 111
OTHER_GUILD = 222


class FakeInteraction:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.response = AsyncMock()


class GiveableItemsTests(unittest.TestCase):
    def test_it_is_every_material_except_drills(self):
        self.assertEqual(set(GIVEABLE_ITEMS), set(ALL_MATERIALS) - set(DRILLS))

    def test_no_drill_type_is_giveable_as_a_plain_item(self):
        for drill_type in DRILLS:
            self.assertNotIn(drill_type, GIVEABLE_ITEMS)


class ItemAutocompleteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = DevToolsCog.__new__(DevToolsCog)

    async def test_it_filters_by_substring_of_the_name(self):
        choices = await self.cog._item_autocomplete(FakeInteraction(BETA_GUILD), "iron ore")
        self.assertEqual([c.value for c in choices], ["iron_ore"])

    async def test_an_empty_search_returns_up_to_the_result_cap(self):
        choices = await self.cog._item_autocomplete(FakeInteraction(BETA_GUILD), "")
        self.assertLessEqual(len(choices), 25)
        self.assertGreater(len(choices), 0)

    async def test_no_drill_is_ever_offered(self):
        choices = await self.cog._item_autocomplete(FakeInteraction(BETA_GUILD), "")
        self.assertTrue(set(c.value for c in choices).isdisjoint(DRILLS))


class InteractionCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_refuses_when_not_beta(self):
        with patch("cogs.devtools.config.IS_BETA", False), patch("cogs.devtools.config.DEV_GUILD_ID", BETA_GUILD):
            allowed = await devtools_group.interaction_check(FakeInteraction(BETA_GUILD))
        self.assertFalse(allowed)

    async def test_it_refuses_a_guild_that_is_not_the_dev_guild(self):
        with patch("cogs.devtools.config.IS_BETA", True), patch("cogs.devtools.config.DEV_GUILD_ID", BETA_GUILD):
            allowed = await devtools_group.interaction_check(FakeInteraction(OTHER_GUILD))
        self.assertFalse(allowed)

    async def test_it_refuses_when_dev_guild_id_is_unset(self):
        # None must never equal a real guild_id.
        with patch("cogs.devtools.config.IS_BETA", True), patch("cogs.devtools.config.DEV_GUILD_ID", None):
            allowed = await devtools_group.interaction_check(FakeInteraction(BETA_GUILD))
        self.assertFalse(allowed)

    async def test_a_refusal_sends_an_ephemeral_message(self):
        interaction = FakeInteraction(OTHER_GUILD)
        with patch("cogs.devtools.config.IS_BETA", True), patch("cogs.devtools.config.DEV_GUILD_ID", BETA_GUILD):
            await devtools_group.interaction_check(interaction)
        interaction.response.send_message.assert_awaited_once()
        _, kwargs = interaction.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_it_allows_the_beta_dev_guild(self):
        with patch("cogs.devtools.config.IS_BETA", True), patch("cogs.devtools.config.DEV_GUILD_ID", BETA_GUILD):
            allowed = await devtools_group.interaction_check(FakeInteraction(BETA_GUILD))
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
