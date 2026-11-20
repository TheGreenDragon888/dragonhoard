"""
The "Queue Limit" field on all five machine status pages quotes the cap
queue_room actually enforces - which, since 1.5, includes the machine's
Infrastructure Enhancement multiplier (data/materials.py: effective_max_queue).

A status page that read only the base and the level would tell a player they
could queue half of what an enhanced machine accepts, so each page is rendered
against a real database rather than trusting the helper's own tests.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.blastfurnace import BlastFurnaceCog
from cogs.factory import FactoryCog
from cogs.furnace import FurnaceCog
from cogs.press import PressCog
from cogs.scrapper import ScrapperCog
from database.db import Database
from utils.db_helpers import ensure_server_row

GUILD = 7171
USER = 4242

# (machine, cog class, status method name)
STATUS_PAGES = (
    ("furnace", FurnaceCog, "_furnace_status_impl"),
    ("blast_furnace", BlastFurnaceCog, "_blast_status_impl"),
    ("factory", FactoryCog, "_factory_status_impl"),
    ("press", PressCog, "_press_status_impl"),
    ("scrapper", ScrapperCog, "_scrapper_status_impl"),
)


class FakeInteraction:
    def __init__(self):
        self.guild_id = GUILD
        self.guild = None
        self.user = SimpleNamespace(id=USER, display_name="Tester")
        self.response = AsyncMock()

    @property
    def embed(self):
        call = self.response.send_message.call_args
        embeds = call.kwargs.get("embeds")
        return embeds[0] if embeds else call.kwargs.get("embed")


class StatusQueueLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def queue_limit(self, cog_class, method):
        cog = cog_class.__new__(cog_class)
        cog.db = self.db
        cog.bot = None
        i = FakeInteraction()
        await getattr(cog, method)(i)
        return next(f.value for f in i.embed.fields if f.name == "Queue Limit")

    async def test_every_status_page_counts_the_enhancement(self):
        for machine, cog_class, method in STATUS_PAGES:
            with self.subTest(machine=machine):
                await self.db.execute(
                    f"UPDATE server_config SET {machine}_max_queue = 3, {machine}_level = 1, "
                    f"{machine}_enhancement_level = 2 WHERE guild_id = ?",
                    (GUILD,),
                )
                value = await self.queue_limit(cog_class, method)
                self.assertTrue(value.startswith("**12**"), value)
                self.assertIn("4 enhanced", value)

    async def test_an_unenhanced_page_is_unchanged(self):
        for machine, cog_class, method in STATUS_PAGES:
            with self.subTest(machine=machine):
                await self.db.execute(
                    f"UPDATE server_config SET {machine}_max_queue = 3, {machine}_level = 1, "
                    f"{machine}_enhancement_level = 0 WHERE guild_id = ?",
                    (GUILD,),
                )
                value = await self.queue_limit(cog_class, method)
                self.assertTrue(value.startswith("**3**"), value)
                self.assertNotIn("enhanced", value)


if __name__ == "__main__":
    unittest.main()
