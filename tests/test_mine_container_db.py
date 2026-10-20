"""
Tests for fitting a container through /mine place and returning one through
/mine remove (1.4.2), against a real database.

/mine place takes an optional container and fits it in the same transaction
that places the drill, so a placement and its container succeed or fail
together. /mine remove now hands a drill's container back to the inventory by
default; keep_container=True leaves it fitted, as /mine remove always used to.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cogs.mining as mining
from cogs.mining import MiningCog
from database.db import Database, InsufficientQuantity
from data.materials import BASE_STORAGE_CAPACITY, effective_capacity
from utils.db_helpers import (
    adjust_user_quantity,
    ensure_server_row,
    ensure_user_row,
    get_user_quantity,
    mining_slot_status,
)

GUILD = 5353
USER = 8383


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class ContainerTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def add_drill(self, guild_id=None, container_type=None):
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, container_type) "
            "VALUES (?, ?, 'iron_drill', ?)",
            (guild_id, USER, container_type),
        )

    async def drill(self, drill_id):
        return await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))

    async def have(self, material_id):
        return await get_user_quantity(self.db, USER, material_id)

    async def place(self, drill_id, container=None):
        interaction = FakeInteraction(GUILD, USER)
        await MiningCog.mine_place.callback(self.cog, interaction, drill_id, container)
        return interaction

    async def remove(self, drill_id, keep_container=False):
        interaction = FakeInteraction(GUILD, USER)
        await MiningCog.mine_remove.callback(self.cog, interaction, drill_id, keep_container)
        return interaction


class PlaceWithContainerTests(ContainerTestCase):
    async def test_placing_without_a_container_is_unchanged(self):
        drill_id = await self.add_drill()
        await self.place(drill_id)
        row = await self.drill(drill_id)
        self.assertEqual(row["guild_id"], GUILD)
        self.assertIsNone(row["container_type"])

    async def test_a_named_container_is_fitted_and_used_up(self):
        drill_id = await self.add_drill()
        await adjust_user_quantity(self.db, USER, "steel_container", 1)
        await self.place(drill_id, "steel_container")
        row = await self.drill(drill_id)
        self.assertEqual(row["guild_id"], GUILD)
        self.assertEqual(row["container_type"], "steel_container")
        self.assertEqual(await self.have("steel_container"), 0)

    async def test_an_existing_container_is_swapped_back_to_the_inventory(self):
        drill_id = await self.add_drill(container_type="iron_container")
        await adjust_user_quantity(self.db, USER, "steel_container", 1)
        await self.place(drill_id, "steel_container")
        row = await self.drill(drill_id)
        self.assertEqual(row["container_type"], "steel_container")
        self.assertEqual(await self.have("steel_container"), 0)
        self.assertEqual(await self.have("iron_container"), 1)

    async def test_naming_the_container_already_fitted_uses_nothing(self):
        drill_id = await self.add_drill(container_type="iron_container")
        await self.place(drill_id, "iron_container")
        row = await self.drill(drill_id)
        self.assertEqual(row["guild_id"], GUILD)
        self.assertEqual(row["container_type"], "iron_container")
        self.assertEqual(await self.have("iron_container"), 0)

    async def test_something_that_is_not_a_container_is_refused(self):
        drill_id = await self.add_drill()
        await adjust_user_quantity(self.db, USER, "iron", 5)
        interaction = await self.place(drill_id, "iron")
        interaction.response.send_message.assert_awaited_once()
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertEqual(await self.have("iron"), 5)

    async def test_a_container_the_player_does_not_own_is_refused(self):
        drill_id = await self.add_drill()
        interaction = await self.place(drill_id, "steel_container")
        interaction.response.send_message.assert_awaited_once()
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertIsNone(row["container_type"])

    async def test_an_unowned_container_is_refused_before_the_free_drill_is_granted(self):
        # A brand-new player (no drills at all) naming a container they don't
        # have must not be handed a free drill they then never placed.
        interaction = await self.place(None, "steel_container")
        interaction.response.send_message.assert_awaited_once()
        owned = await self.db.fetchone("SELECT COUNT(*) AS cnt FROM drills WHERE owner_id = ?", (USER,))
        self.assertEqual(owned["cnt"], 0)

    async def test_a_placement_refused_for_slots_keeps_the_container(self):
        slots = await mining_slot_status(self.db, GUILD)
        for _ in range(slots.slots):
            await self.add_drill(guild_id=GUILD)
        drill_id = await self.add_drill()
        await adjust_user_quantity(self.db, USER, "steel_container", 1)
        await self.place(drill_id, "steel_container")
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertIsNone(row["container_type"])
        self.assertEqual(await self.have("steel_container"), 1)

    async def test_a_container_lost_mid_placement_undoes_the_placement(self):
        # The container vanishing between the up-front check and the fit (a
        # racing command spending it) raises inside the transaction - the
        # drill must not end up placed without the container asked for.
        drill_id = await self.add_drill()
        await adjust_user_quantity(self.db, USER, "steel_container", 1)
        with patch.object(mining, "fit_container", AsyncMock(side_effect=InsufficientQuantity())):
            interaction = await self.place(drill_id, "steel_container")
        interaction.response.send_message.assert_awaited_once()
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertIsNone(row["container_type"])
        self.assertEqual(await self.have("steel_container"), 1)

    async def test_the_free_drill_is_filled_to_its_plain_capacity_and_keeps_mining(self):
        # The free fill is the bare drill's capacity; the container's extra
        # room is mined, not handed out, so the drill is not full.
        await adjust_user_quantity(self.db, USER, "steel_container", 1)
        await self.place(None, "steel_container")
        row = await self.db.fetchone("SELECT * FROM drills WHERE owner_id = ?", (USER,))
        self.assertEqual(row["guild_id"], GUILD)
        self.assertEqual(row["container_type"], "steel_container")
        self.assertEqual(row["stored_amount"], BASE_STORAGE_CAPACITY)
        self.assertLess(row["stored_amount"], effective_capacity("steel_container"))
        self.assertEqual(row["is_full"], 0)


class RemoveReturnsContainerTests(ContainerTestCase):
    async def test_the_container_is_returned_by_default(self):
        drill_id = await self.add_drill(guild_id=GUILD, container_type="steel_container")
        await self.remove(drill_id)
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertIsNone(row["container_type"])
        self.assertEqual(await self.have("steel_container"), 1)

    async def test_keep_container_leaves_it_fitted(self):
        drill_id = await self.add_drill(guild_id=GUILD, container_type="steel_container")
        await self.remove(drill_id, keep_container=True)
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertEqual(row["container_type"], "steel_container")
        self.assertEqual(await self.have("steel_container"), 0)

    async def test_a_drill_with_no_container_is_removed_as_before(self):
        drill_id = await self.add_drill(guild_id=GUILD)
        await self.remove(drill_id)
        row = await self.drill(drill_id)
        self.assertIsNone(row["guild_id"])
        self.assertIsNone(row["container_type"])


if __name__ == "__main__":
    unittest.main()
