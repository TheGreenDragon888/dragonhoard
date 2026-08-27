"""
Tests for /mine attach's container picker and its underlying command, against
a real database.

The container choice used to be a static list of all five container types
regardless of what the player actually owned, so /mine attach would offer a
Diamond Container to someone with none and then refuse it a step later. The
autocomplete now only offers what's actually in the player's inventory - the
same "don't offer what you can't act on" rule the drill autocompletes follow.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cogs.mining import MiningCog
from database.db import Database
from utils.db_helpers import ensure_server_row, ensure_user_row, adjust_user_quantity

GUILD = 5151
USER = 8181


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class MineAttachTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def add_drill(self, container_type=None):
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, container_type) "
            "VALUES (?, ?, 'iron_drill', ?)",
            (GUILD, USER, container_type),
        )


class OwnedContainerAutocompleteTests(MineAttachTestCase):
    async def test_only_owned_containers_are_offered(self):
        await adjust_user_quantity(self.db, USER, "iron_container", 1)
        choices = await self.cog._owned_container_autocomplete(FakeInteraction(GUILD, USER), "")
        self.assertEqual([c.value for c in choices], ["iron_container"])

    async def test_owning_none_offers_nothing(self):
        choices = await self.cog._owned_container_autocomplete(FakeInteraction(GUILD, USER), "")
        self.assertEqual(choices, [])

    async def test_a_zero_quantity_row_is_not_offered(self):
        # Fitting one and then having it deducted back to 0 leaves a row
        # behind, per adjust_user_quantity - it must not read as "owned".
        await adjust_user_quantity(self.db, USER, "iron_container", 1)
        await adjust_user_quantity(self.db, USER, "iron_container", -1)
        choices = await self.cog._owned_container_autocomplete(FakeInteraction(GUILD, USER), "")
        self.assertEqual(choices, [])

    async def test_search_filters_by_name_substring(self):
        await adjust_user_quantity(self.db, USER, "iron_container", 1)
        await adjust_user_quantity(self.db, USER, "steel_container", 1)
        choices = await self.cog._owned_container_autocomplete(FakeInteraction(GUILD, USER), "iron")
        self.assertEqual([c.value for c in choices], ["iron_container"])


class MineAttachCommandTests(MineAttachTestCase):
    async def test_fitting_an_owned_container_still_works(self):
        drill_id = await self.add_drill()
        await adjust_user_quantity(self.db, USER, "steel_container", 1)
        interaction = FakeInteraction(GUILD, USER)
        # mine_attach is wrapped in an app_commands.Command by the decorator,
        # so the cog instance has to be passed to the raw callback by hand
        # rather than called as a bound method. container is now a plain
        # string (the autocompleted value), not an app_commands.Choice.
        await MiningCog.mine_attach.callback(self.cog, interaction, drill_id, "steel_container")

        row = await self.db.fetchone("SELECT container_type FROM drills WHERE drill_id = ?", (drill_id,))
        self.assertEqual(row["container_type"], "steel_container")


if __name__ == "__main__":
    unittest.main()
