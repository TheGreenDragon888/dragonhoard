"""
/factory upgrade against a real database.

Two things are pinned here (1.5). An upgrade takes the drill's container off
and hands it back, so the container isn't locked away on a drill nobody can
use until the factory finishes. And a REFUSED upgrade changes nothing: the
command pulls a placed drill out of the ground before it checks the cost, the
queue and the balance, and until 1.5 each refusal returned from inside the
transaction - which commits - so a player told "you can't afford that" found
their drill stopped and its haul banked anyway.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cogs.factory import FactoryCog
from database.db import Database
from utils.db_helpers import (
    adjust_currency_balance,
    adjust_user_quantity,
    ensure_server_row,
    ensure_user_row,
    get_user_quantity,
)

GUILD = 6060
ALICE = 111


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = f"User{user_id}"


class FakeGuild:
    id = GUILD
    name = "Test Server"


class FakeInteraction:
    def __init__(self, user_id=ALICE):
        self.guild_id = GUILD
        self.guild = FakeGuild()
        self.user = FakeUser(user_id)
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


class FactoryUpgradeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, ALICE)
        self.cog = FactoryCog.__new__(FactoryCog)
        self.cog.db = self.db
        self.cog.bot = None

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def add_drill(self, placed=False, container=None, iron_ore=0):
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, container_type, stored_amount, "
            "mined_until) VALUES (?, ?, 'iron_drill', ?, ?, datetime('now'))",
            (GUILD if placed else None, ALICE, container, iron_ore),
        )
        if iron_ore:
            await self.db.execute(
                "INSERT INTO drill_contents (drill_id, material_id, quantity) VALUES (?, 'iron_ore', ?)",
                (drill_id, iron_ore),
            )
        return drill_id

    async def drill(self, drill_id):
        return await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))

    async def can_afford(self, materials=True, balance=True):
        # An Iron Drill's first level: one Upgrade Pack and 10 Iron, plus the
        # factory fee.
        if materials:
            await adjust_user_quantity(self.db, ALICE, "drill_upgrade_pack", 1)
            await adjust_user_quantity(self.db, ALICE, "iron", 10)
        if balance:
            await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)

    async def upgrade(self, drill_id):
        i = FakeInteraction()
        await FactoryCog.factory_upgrade.callback(self.cog, i, drill_id)
        return i

    def field(self, embed, name):
        return next((f for f in embed.fields if f.name == name), None)


class ContainerComesOffTests(FactoryUpgradeTestCase):
    async def test_an_unplaced_drills_container_comes_back(self):
        drill_id = await self.add_drill(container="steel_container")
        await self.can_afford()
        i = await self.upgrade(drill_id)
        self.assertIsNone(i.sent, i.sent)
        row = await self.drill(drill_id)
        self.assertIsNone(row["container_type"])
        self.assertIsNotNone(row["locked_job_id"])
        self.assertEqual(await get_user_quantity(self.db, ALICE, "steel_container"), 1)
        returned = self.field(i.embed, "Container Returned")
        self.assertIsNotNone(returned)
        self.assertIn("Steel Container", returned.value)

    async def test_a_placed_drill_comes_out_bare(self):
        drill_id = await self.add_drill(placed=True, container="iron_container", iron_ore=40)
        await self.can_afford()
        i = await self.upgrade(drill_id)
        self.assertIsNone(i.sent, i.sent)
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertIsNone(row["container_type"])
        self.assertIsNotNone(row["locked_job_id"])
        self.assertEqual(await get_user_quantity(self.db, ALICE, "iron_container"), 1)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "iron_ore"), 40)
        pulled = self.field(i.embed, "Pulled Out Of The Ground")
        self.assertNotIn("keeping its level and container", pulled.value)

    async def test_a_bare_drill_upgrades_as_before(self):
        drill_id = await self.add_drill()
        await self.can_afford()
        i = await self.upgrade(drill_id)
        self.assertIsNone(i.sent, i.sent)
        self.assertIsNotNone((await self.drill(drill_id))["locked_job_id"])
        self.assertIsNone(self.field(i.embed, "Container Returned"))


class RefusalChangesNothingTests(FactoryUpgradeTestCase):
    """Every refusal after the drill has been pulled out of the ground has to
    put it back exactly as it was - placed, container on, haul still in it."""

    async def assert_untouched(self, drill_id):
        row = await self.drill(drill_id)
        self.assertEqual(row["guild_id"], GUILD, "still in the ground")
        self.assertEqual(row["container_type"], "iron_container")
        self.assertEqual(row["stored_amount"], 40)
        self.assertIsNone(row["locked_job_id"])
        contents = await self.db.fetchone(
            "SELECT quantity FROM drill_contents WHERE drill_id = ? AND material_id = 'iron_ore'",
            (drill_id,),
        )
        self.assertEqual(contents["quantity"], 40)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "iron_ore"), 0)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "iron_container"), 0)

    async def test_missing_materials(self):
        drill_id = await self.add_drill(placed=True, container="iron_container", iron_ore=40)
        await self.can_afford(materials=False)
        i = await self.upgrade(drill_id)
        self.assertIn("costs", i.sent)
        await self.assert_untouched(drill_id)

    async def test_too_little_balance(self):
        drill_id = await self.add_drill(placed=True, container="iron_container", iron_ore=40)
        await self.can_afford(balance=False)
        i = await self.upgrade(drill_id)
        self.assertIn("up front", i.sent)
        await self.assert_untouched(drill_id)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "iron"), 10, "nothing was spent")

    async def test_a_full_queue(self):
        drill_id = await self.add_drill(placed=True, container="iron_container", iron_ore=40)
        await self.can_afford()
        await self.db.execute(
            "UPDATE server_config SET factory_max_queue = 1 WHERE guild_id = ?", (GUILD,)
        )
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'factory', 'wiring', 1)",
            (GUILD, ALICE),
        )
        i = await self.upgrade(drill_id)
        self.assertIn("queue", i.sent)
        await self.assert_untouched(drill_id)

    async def test_a_refusal_is_ephemeral(self):
        drill_id = await self.add_drill(placed=True, container="iron_container", iron_ore=40)
        i = await self.upgrade(drill_id)
        self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
