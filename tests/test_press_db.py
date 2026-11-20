"""
The hydraulic press against a real database: /press craft, /press status, and
the loop that finishes jobs.

1.5 gave the press recipes keyed by something other than what they make - the
gem fusions, "obsidian_from_ruby" and "diamond_from_obsidian" - so every place
that used to treat a job's target_id as the item made has to go through
press_output instead. Each of those places is pinned here, because one that
was missed would credit an item called "obsidian_from_ruby".
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord import app_commands

from cogs.press import PressCog, press_choice_label
from database.db import Database
from utils.db_helpers import (
    adjust_currency_balance,
    adjust_user_quantity,
    ensure_server_row,
    ensure_user_row,
    get_user_quantity,
    sqlite_timestamp,
)

GUILD = 5151
USER = 6161


class FakeInteraction:
    def __init__(self, user_id=USER, guild_id=GUILD):
        self.guild_id = guild_id
        self.guild = None
        self.user = SimpleNamespace(id=user_id, display_name=f"User{user_id}")
        self.response = AsyncMock()

    @property
    def sent(self):
        call = self.response.send_message.call_args
        if call is None or not call.args:
            return None
        return call.args[0]

    @property
    def embed(self):
        call = self.response.send_message.call_args
        embeds = call.kwargs.get("embeds")
        return embeds[0] if embeds else call.kwargs.get("embed")


class PressTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.cog = PressCog.__new__(PressCog)
        self.cog.db = self.db
        self.cog.bot = None
        self.cog._now = lambda: self.now

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def queue(self, recipe_id, quantity=1, user_id=USER, guild_id=GUILD):
        await ensure_server_row(self.db, guild_id)
        return await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, queued_at) "
            "VALUES (?, ?, 'press', ?, ?, ?)",
            (guild_id, user_id, recipe_id, quantity, sqlite_timestamp(self.now)),
        )

    async def run_press(self, press_days, guild_id=GUILD):
        """One loop tick with enough banked progress to finish `press_days`."""
        await self.db.execute(
            "UPDATE server_config SET press_progress = ? WHERE guild_id = ?", (press_days, guild_id)
        )
        await PressCog.process_loop.coro(self.cog)

    async def craft(self, recipe_id, quantity=1):
        i = FakeInteraction()
        await PressCog.press_craft.callback(
            self.cog, i, app_commands.Choice(name=press_choice_label(recipe_id), value=recipe_id), quantity
        )
        return i


class GemFusionTests(PressTestCase):
    async def test_crafting_a_fusion_takes_the_gems_and_queues_the_job(self):
        await adjust_user_quantity(self.db, USER, "ruby", 25)
        await adjust_currency_balance(self.db, GUILD, USER, 1_000.0)
        i = await self.craft("obsidian_from_ruby")
        self.assertIsNone(i.sent, i.sent)
        self.assertEqual(await get_user_quantity(self.db, USER, "ruby"), 15)
        job = await self.db.fetchone("SELECT target_id, quantity FROM production_jobs")
        self.assertEqual((job["target_id"], job["quantity"]), ("obsidian_from_ruby", 1))
        # The receipt is for what it makes.
        receipt = (i.embed.description or "") + "".join(f.value for f in i.embed.fields)
        self.assertIn("Obsidian", receipt)
        self.assertNotIn("obsidian_from_ruby", receipt)

    async def test_a_shortfall_names_the_gem_it_makes(self):
        await adjust_user_quantity(self.db, USER, "ruby", 3)
        i = await self.craft("obsidian_from_ruby")
        self.assertIn("Obsidian", i.sent)
        self.assertNotIn("from", i.sent.split("needs")[0], "the item, not the recipe label")
        self.assertEqual(await get_user_quantity(self.db, USER, "ruby"), 3)

    async def test_the_loop_credits_the_gem_not_the_recipe(self):
        await self.queue("obsidian_from_ruby")
        await self.run_press(3)
        self.assertEqual(await get_user_quantity(self.db, USER, "obsidian"), 1)
        self.assertEqual(await get_user_quantity(self.db, USER, "obsidian_from_ruby"), 0)
        ledger = await self.db.fetchone("SELECT material_id FROM production_ledger WHERE source = 'press'")
        self.assertEqual(ledger["material_id"], "obsidian")

    async def test_a_diamond_fusion_finishes_on_the_diamonds_press_time(self):
        await self.queue("diamond_from_obsidian")
        await self.run_press(8.5)
        self.assertEqual(await get_user_quantity(self.db, USER, "diamond"), 0, "9 press-days, not 8.5")
        await self.run_press(9)
        self.assertEqual(await get_user_quantity(self.db, USER, "diamond"), 1)

    async def test_a_job_queued_under_an_old_key_still_finishes(self):
        await self.queue("obsidian")
        await self.run_press(3)
        self.assertEqual(await get_user_quantity(self.db, USER, "obsidian"), 1)

    async def test_status_names_what_is_being_made(self):
        await self.queue("diamond_from_obsidian")
        i = FakeInteraction()
        await self.cog._press_status_impl(i)
        current = next(f.value for f in i.embed.fields if f.name == "Currently Pressing")
        self.assertIn("Diamond", current)
        self.assertIn("9 press-days", current)
        self.assertNotIn("diamond_from_obsidian", "".join(f.value for f in i.embed.fields))


class DashboardLabelTests(unittest.TestCase):
    def test_a_fusion_job_reads_as_the_gem_it_makes(self):
        from web.queries import _job_target_label

        job = {"target_id": "obsidian_from_ruby", "job_type": "press", "quantity": 2,
               "target_drill_id": None}
        self.assertEqual(_job_target_label(job, {}), "2× Obsidian")


if __name__ == "__main__":
    unittest.main()
