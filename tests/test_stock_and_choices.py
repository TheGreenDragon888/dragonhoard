"""
tests/test_stock_and_choices.py

Two small 1.4 changes that each turned several queries into one:

  * get_server_stocks reads a server's whole market inventory at once, for
    /market status and the furnace's auto-smelt, which read one material at a
    time before.
  * drill_choices resolves server names from the rows it already fetched
    when handed the bot, where callers used to run an identical second query
    to build the name map themselves - on every autocomplete keystroke.
"""
import tempfile
import unittest
from pathlib import Path

from database.db import Database
from utils.db_helpers import adjust_server_stock, ensure_server_row, get_server_stocks
from utils.drills import DrillScope, drill_choices

GUILD = 4040
OWNER = 12


class _Guild:
    def __init__(self, name):
        self.name = name


class _Bot:
    def __init__(self, names):
        self._names = names

    def get_guild(self, guild_id):
        name = self._names.get(guild_id)
        return _Guild(name) if name else None


class ServerStocksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def test_it_returns_every_material_the_server_holds(self):
        await adjust_server_stock(self.db, GUILD, "iron_ore", 40)
        await adjust_server_stock(self.db, GUILD, "coal", 7)
        self.assertEqual(await get_server_stocks(self.db, GUILD), {"iron_ore": 40, "coal": 7})

    async def test_a_server_with_no_stock_reads_as_empty(self):
        self.assertEqual(await get_server_stocks(self.db, GUILD), {})


class DrillChoiceNamesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (?, ?, 'iron_drill')",
            (GUILD, OWNER),
        )

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def test_the_label_names_the_server_when_given_the_bot(self):
        choices = await drill_choices(
            self.db, OWNER, "", scope=DrillScope.PLACED_HERE, guild_id=GUILD,
            bot=_Bot({GUILD: "The Hoard"}),
        )
        self.assertIn("The Hoard", choices[0].name)

    async def test_a_server_the_bot_cannot_see_falls_back_to_its_id(self):
        choices = await drill_choices(
            self.db, OWNER, "", scope=DrillScope.PLACED_HERE, guild_id=GUILD, bot=_Bot({}),
        )
        self.assertIn(f"server {GUILD}", choices[0].name)

    async def test_without_the_bot_the_label_carries_no_location(self):
        choices = await drill_choices(
            self.db, OWNER, "", scope=DrillScope.PLACED_HERE, guild_id=GUILD,
        )
        self.assertNotIn("server", choices[0].name)


if __name__ == "__main__":
    unittest.main()
