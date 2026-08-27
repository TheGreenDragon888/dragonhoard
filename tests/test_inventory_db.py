"""
Tests for /inventory's drill listing and material grid against a real database.

Placed drills are already excluded (guild_id IS NOT NULL); this pins the same
treatment for LOCKED ones - a drill mid-upgrade or already handed to the
scrapper is just as unusable as a placed one until its job finishes, so it
shouldn't read as stock on hand either.

Also covers the material grid's quantity formatting - a stack in the
thousands has to read with thousands separators, the same as every other
number the bot displays.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cogs.economy import EconomyCog
from database.db import Database
from utils.db_helpers import ensure_server_row, ensure_user_row

GUILD = 9191
USER = 5151


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class FakeBot:
    def get_guild(self, guild_id):
        return None


class InventoryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

        self.cog = EconomyCog.__new__(EconomyCog)
        self.cog.db = self.db
        self.cog.bot = FakeBot()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def add_drill(self, level, locked_job_id=None):
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, locked_job_id) "
            "VALUES (NULL, ?, 'iron_drill', ?, ?)",
            (USER, level, locked_job_id),
        )

    async def set_quantity(self, material_id, quantity):
        await self.db.execute(
            "INSERT INTO user_materials (user_id, material_id, quantity) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, material_id) DO UPDATE SET quantity = excluded.quantity",
            (USER, material_id, quantity),
        )

    async def field(self, name):
        interaction = FakeInteraction(GUILD, USER)
        # inventory is wrapped in an app_commands.Command by the decorator,
        # so the cog instance has to be passed to the raw callback by hand
        # rather than called as a bound method.
        await EconomyCog.inventory.callback(self.cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        # respond() only rewrites embed= into embeds=[...] when there's a
        # notice to attach - with none pending here it passes embed= through
        # unchanged.
        embed = kwargs["embeds"][0] if "embeds" in kwargs else kwargs["embed"]
        return next((f.value for f in embed.fields if f.name == name), None)

    async def drills_field(self):
        return await self.field("Drills")


class InventoryDrillListingTests(InventoryTestCase):
    async def test_an_unlocked_drill_is_listed(self):
        await self.add_drill(level=3)
        field = await self.drills_field()
        self.assertIn("Lv.3", field)

    async def test_a_locked_drill_is_not_listed(self):
        # Simulates a drill queued at /factory upgrade or /scrapper drill,
        # which sets locked_job_id to the job's id.
        await self.add_drill(level=7, locked_job_id=1)
        field = await self.drills_field()
        self.assertIsNone(field)

    async def test_a_locked_drill_does_not_hide_an_unlocked_one(self):
        await self.add_drill(level=3)
        await self.add_drill(level=7, locked_job_id=1)
        field = await self.drills_field()
        self.assertIn("Lv.3", field)
        self.assertNotIn("Lv.7", field)


class InventoryMaterialGridTests(InventoryTestCase):
    async def test_a_four_figure_quantity_is_comma_separated(self):
        await self.set_quantity("iron_ore", 12345)
        field = await self.field("Raw Materials")
        self.assertIn("12,345", field)

    async def test_a_three_figure_quantity_has_no_stray_comma(self):
        await self.set_quantity("iron_ore", 999)
        field = await self.field("Raw Materials")
        self.assertIn("999", field)
        self.assertNotIn(",", field)


if __name__ == "__main__":
    unittest.main()
