"""
Tests for what happens when Dragonhoard is removed from a server.

Two things have to be true afterwards. Every drill placed there comes home to
its owner's inventory with its contents credited - the schema won't even let an
unplaced drill hold materials, so "leave them where they are" was never an
option. And the server's currency stops being listed without being deleted, so
a re-invite restores every balance untouched.

Runs against a throwaway database. The cog is built with __new__ rather than
its constructor, which would start the harvest and pool-top-up loops.
"""
import tempfile
import unittest
from pathlib import Path

from cogs.mining import MiningCog
from database.db import Database
from utils.db_helpers import ensure_server_row

OWNER = 4242
OTHER_OWNER = 9999
LEFT = 100
KEPT = 200


class GuildTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture. Holds no tests of its own, so subclassing it doesn't
    re-run anything."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()

        self.cog = MiningCog.__new__(MiningCog)
        self.cog.db = self.db

        await ensure_server_row(self.db, LEFT)
        await ensure_server_row(self.db, KEPT)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def add_drill(self, owner_id, guild_id, stored_amount=0, **columns):
        defaults = {"drill_type": "iron_drill", "level": 1, "container_type": None}
        defaults.update(columns)
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, container_type, "
            "stored_amount, is_full) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, owner_id, defaults["drill_type"], defaults["level"],
                defaults["container_type"], stored_amount, 1 if stored_amount else 0,
            ),
        )

    async def drill(self, drill_id):
        return await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))

    async def total_materials(self, user_id):
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(quantity), 0) AS total FROM user_materials WHERE user_id = ?",
            (user_id,),
        )
        return row["total"]


class GuildRemovalTests(GuildTestCase):
    async def test_a_placed_drill_comes_back_to_its_owners_inventory(self):
        drill_id = await self.add_drill(OWNER, LEFT, stored_amount=120)

        retracted = await self.cog._retract_guild_drills(LEFT)

        self.assertEqual(retracted, 1)
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertEqual(row["stored_amount"], 0)
        self.assertEqual(row["is_full"], 0)

    async def test_the_contents_are_credited_rather_than_destroyed(self):
        await self.add_drill(OWNER, LEFT, stored_amount=250)

        await self.cog._retract_guild_drills(LEFT)

        # Which materials came out is a roll, but the count is not.
        self.assertEqual(await self.total_materials(OWNER), 250)

    async def test_a_drill_keeps_its_level_and_container(self):
        # The whole reason drills are tracked per instance: a removal must not
        # quietly reset an upgraded drill back to a stock one.
        drill_id = await self.add_drill(
            OWNER, LEFT, stored_amount=40, level=4,
            drill_type="steel_drill", container_type="ruby_container",
        )

        await self.cog._retract_guild_drills(LEFT)

        row = await self.drill(drill_id)
        self.assertEqual(row["level"], 4)
        self.assertEqual(row["drill_type"], "steel_drill")
        self.assertEqual(row["container_type"], "ruby_container")

    async def test_every_owners_drills_are_retracted_not_just_one(self):
        await self.add_drill(OWNER, LEFT, stored_amount=10)
        await self.add_drill(OWNER, LEFT, stored_amount=20)
        await self.add_drill(OTHER_OWNER, LEFT, stored_amount=30)

        self.assertEqual(await self.cog._retract_guild_drills(LEFT), 3)
        self.assertEqual(await self.total_materials(OWNER), 30)
        self.assertEqual(await self.total_materials(OTHER_OWNER), 30)

    async def test_drills_in_other_servers_are_left_alone(self):
        kept = await self.add_drill(OWNER, KEPT, stored_amount=99)

        await self.cog._retract_guild_drills(LEFT)

        row = await self.drill(kept)
        self.assertEqual(row["guild_id"], KEPT)
        self.assertEqual(row["stored_amount"], 99)

    async def test_an_empty_drill_still_comes_home(self):
        drill_id = await self.add_drill(OWNER, LEFT, stored_amount=0)

        self.assertEqual(await self.cog._retract_guild_drills(LEFT), 1)
        self.assertIsNone((await self.drill(drill_id))["guild_id"])
        self.assertEqual(await self.total_materials(OWNER), 0)

    async def test_retracting_twice_credits_nothing_the_second_time(self):
        # Idempotence matters: on_ready re-fires on every reconnect, and it
        # calls this for any server the bot is no longer in.
        await self.add_drill(OWNER, LEFT, stored_amount=75)

        await self.cog._retract_guild_drills(LEFT)
        self.assertEqual(await self.cog._retract_guild_drills(LEFT), 0)
        self.assertEqual(await self.total_materials(OWNER), 75)


class GuildPresenceTests(GuildTestCase):
    async def presence(self, guild_id):
        row = await self.db.fetchone(
            "SELECT bot_present FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["bot_present"]

    async def test_servers_start_out_present(self):
        self.assertEqual(await self.presence(KEPT), 1)

    async def test_marking_a_server_absent_and_present_again(self):
        await self.cog._set_guild_presence(LEFT, False)
        self.assertEqual(await self.presence(LEFT), 0)

        await self.cog._set_guild_presence(LEFT, True)
        self.assertEqual(await self.presence(LEFT), 1)

    async def test_a_departed_servers_balances_are_kept_not_deleted(self):
        await self.db.execute(
            "INSERT INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, ?)",
            (LEFT, OWNER, 512.5),
        )

        await self.cog._set_guild_presence(LEFT, False)

        row = await self.db.fetchone(
            "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
            (LEFT, OWNER),
        )
        self.assertEqual(row["balance"], 512.5)

    async def test_the_currency_query_hides_departed_servers(self):
        """The exact JOIN /balance and /inventory run, since that filter is the
        only thing separating "hidden" from "deleted"."""
        for guild_id in (LEFT, KEPT):
            await self.db.execute(
                "INSERT INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, 100.0)",
                (guild_id, OWNER),
            )
        await self.cog._set_guild_presence(LEFT, False)

        rows = await self.db.fetchall(
            """
            SELECT scb.guild_id FROM server_currency_balances scb
            JOIN server_config sc ON sc.guild_id = scb.guild_id
            WHERE scb.user_id = ? AND sc.bot_present = 1
            """,
            (OWNER,),
        )
        self.assertEqual([row["guild_id"] for row in rows], [KEPT])


if __name__ == "__main__":
    unittest.main()
