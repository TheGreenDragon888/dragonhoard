"""
Tests for /mine place's free-drill grant, against a real database.

As of 1.3 the free Iron Drill a brand-new player is handed isn't just placed
empty - it's drawn full from the server's own pool in the same transaction, so
their very next command can be /collect instead of a wait. This is deliberately
NOT a fabricated starting kit: the contents come from take_from_pool, the same
draw a real drill uses, so a lucky gemstone in someone's first hundred items is
mined rather than invented (docs/mining.txt).
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cogs.mining import MiningCog
from database.db import Database
from data.materials import BASE_STORAGE_CAPACITY, MINING_POOL_BAG_SIZE
from utils.db_helpers import ensure_server_row

GUILD = 5252
USER = 8282


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class FreeDrillFillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

        self.cog = MiningCog.__new__(MiningCog)
        self.cog.db = self.db

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def place(self):
        # No drill argument: this is exactly the brand-new-player path, where
        # the autocomplete list is empty and _grant_fallback_drill fires.
        await MiningCog.mine_place.callback(self.cog, FakeInteraction(GUILD, USER), None)

    async def the_drill(self):
        return await self.db.fetchone(
            "SELECT * FROM drills WHERE owner_id = ?", (USER,)
        )

    async def test_the_free_drill_is_placed_and_full(self):
        await self.place()
        row = await self.the_drill()
        self.assertEqual(row["guild_id"], GUILD)
        self.assertEqual(row["stored_amount"], BASE_STORAGE_CAPACITY)
        self.assertEqual(row["is_full"], 1)

    async def test_its_contents_add_up_to_stored_amount(self):
        # drills.stored_amount is denormalised from drill_contents - the two
        # must agree, the same invariant a real harvest tick keeps.
        await self.place()
        row = await self.the_drill()
        contents = await self.db.fetchall(
            "SELECT quantity FROM drill_contents WHERE drill_id = ?", (row["drill_id"],)
        )
        self.assertEqual(sum(r["quantity"] for r in contents), row["stored_amount"])

    async def test_the_fill_is_drawn_from_the_servers_own_pool(self):
        # Not fabricated: this server's bag is BASE_STORAGE_CAPACITY items
        # lighter afterwards, exactly what a real drill draws.
        await self.place()
        cfg = await self.db.fetchone(
            "SELECT mining_pool_remaining FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        self.assertEqual(cfg["mining_pool_remaining"], MINING_POOL_BAG_SIZE - BASE_STORAGE_CAPACITY)

    async def test_a_second_drill_the_player_already_owns_is_not_filled(self):
        # Only the free grant starts full - a player who already has a drill
        # and places another one of their own gets the ordinary empty start.
        await self.place()
        second = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (NULL, ?, 'iron_drill')",
            (USER,),
        )
        await MiningCog.mine_place.callback(
            self.cog, FakeInteraction(GUILD, USER), second
        )
        row = await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (second,))
        self.assertEqual(row["guild_id"], GUILD)
        self.assertEqual(row["stored_amount"], 0)
        self.assertEqual(row["is_full"], 0)


if __name__ == "__main__":
    unittest.main()
