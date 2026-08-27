"""
Tests for mining slots: the per-server unlock (1.3) that raises how many drills
one player may keep in the ground, paid for by the SUM of every machine's
lifetime infrastructure fees.

Three properties are worth defending here, and they are the three that would
each fail silently:

  * the ladder's arithmetic, because a threshold that is off by one rung is a
    feature that either never unlocks or unlocks immediately;
  * that the total spans ALL machines rather than whichever one a fee happened
    to be paid to, which is the whole design and also the easy thing to break
    by adding a sixth machine and forgetting MACHINES;
  * that the unlock notice fires once per level and not once per fee, since
    post_server_notification deliberately refuses to dedupe for its callers.
"""
import tempfile
import unittest
from pathlib import Path

from cogs.mining import MiningCog
from database.db import Database
from data.materials import (
    BASE_MINING_SLOTS,
    MINING_SLOT_THRESHOLD_BASE,
    UPGRADE_THRESHOLD_BASE,
    UPGRADE_THRESHOLD_STEP,
    mining_slot_level,
    mining_slot_threshold,
    mining_slots,
    upgrade_threshold,
)
from utils.db_helpers import (
    MACHINES,
    announce_mining_slot_unlocks,
    bank_infrastructure_fee,
    ensure_server_row,
    mining_slot_status,
    mining_slots_full_message,
)

GUILD = 5150


class MiningSlotLadderTests(unittest.TestCase):
    """The arithmetic, with no database involved."""

    def test_the_first_extra_slot_costs_the_documented_base(self):
        self.assertEqual(mining_slot_threshold(2), MINING_SLOT_THRESHOLD_BASE)
        self.assertEqual(mining_slot_threshold(2), 25.00)

    def test_each_rung_is_STEP_times_the_last(self):
        for level in range(2, 12):
            self.assertAlmostEqual(
                mining_slot_threshold(level + 1),
                mining_slot_threshold(level) * UPGRADE_THRESHOLD_STEP,
            )

    def test_the_published_ladder(self):
        # The figures docs/mining.txt, the manual and the changelog all quote.
        self.assertEqual(
            [mining_slot_threshold(l) for l in range(2, 6)],
            [25.0, 125.0, 625.0, 3125.0],
        )

    def test_the_first_slot_costs_what_a_machine_level_3_costs(self):
        # The stated reason for the base being 5x UPGRADE_THRESHOLD_BASE
        # (data/materials.py): a server that has taken any single machine to
        # level 3 has necessarily paid for its first slot too.
        self.assertEqual(mining_slot_threshold(2), upgrade_threshold(3))
        self.assertEqual(MINING_SLOT_THRESHOLD_BASE, UPGRADE_THRESHOLD_BASE * UPGRADE_THRESHOLD_STEP)

    def test_a_server_that_has_paid_nothing_is_level_one(self):
        self.assertEqual(mining_slot_level(0.0), 1)
        self.assertEqual(mining_slots(mining_slot_level(0.0)), BASE_MINING_SLOTS)

    def test_paying_exactly_a_threshold_earns_that_level(self):
        # The boundary is the one place a float comparison must not be off by
        # one - a server that paid exactly 625.00 bought the slot.
        for level in range(2, 8):
            exact = mining_slot_threshold(level)
            self.assertEqual(mining_slot_level(exact), level)
            self.assertEqual(mining_slot_level(exact - 0.01), level - 1)

    def test_each_level_adds_exactly_one_slot(self):
        for level in range(1, 10):
            self.assertEqual(mining_slots(level), BASE_MINING_SLOTS + level - 1)

    def test_a_zero_level_is_floored_to_the_base(self):
        # Defensive, matching effective_max_queue: nothing produces a zero, but
        # one reaching here would strand every drill in the server.
        self.assertEqual(mining_slots(0), BASE_MINING_SLOTS)


class MiningSlotStatusTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def set_fees(self, **per_machine):
        for machine, amount in per_machine.items():
            await self.db.execute(
                f"UPDATE server_config SET {machine}_fees_collected = ? WHERE guild_id = ?",
                (amount, GUILD),
            )

    async def notices(self):
        return await self.db.fetchall(
            "SELECT title, body FROM notifications WHERE scope = 'server' AND guild_id = ? "
            "ORDER BY notification_id",
            (GUILD,),
        )

    async def test_a_new_server_starts_at_the_base(self):
        slots = await mining_slot_status(self.db, GUILD)
        self.assertEqual(slots.level, 1)
        self.assertEqual(slots.slots, BASE_MINING_SLOTS)
        self.assertEqual(slots.invested, 0.0)
        self.assertEqual(slots.next_threshold, MINING_SLOT_THRESHOLD_BASE)

    async def test_a_guild_with_no_row_is_level_one_rather_than_an_error(self):
        slots = await mining_slot_status(self.db, 999999)
        self.assertEqual(slots.level, 1)
        self.assertEqual(slots.slots, BASE_MINING_SLOTS)

    async def test_every_machine_counts_toward_the_same_total(self):
        # The design's core claim. No single machine here has paid enough for a
        # slot; together they have paid for exactly one.
        share = MINING_SLOT_THRESHOLD_BASE / len(MACHINES)
        await self.set_fees(**{machine: share for machine in MACHINES})

        slots = await mining_slot_status(self.db, GUILD)
        self.assertAlmostEqual(slots.invested, MINING_SLOT_THRESHOLD_BASE)
        self.assertEqual(slots.level, 2)
        self.assertEqual(slots.slots, BASE_MINING_SLOTS + 1)

    async def test_no_machine_is_left_out_of_the_total(self):
        # Guards the thing a sixth machine would break: a machine whose fees
        # are collected but not summed would make slots quietly cheaper to
        # reach for servers that use the other five.
        for machine in MACHINES:
            await self.set_fees(**{m: 0.0 for m in MACHINES})
            await self.set_fees(**{machine: MINING_SLOT_THRESHOLD_BASE})
            slots = await mining_slot_status(self.db, GUILD)
            self.assertEqual(
                slots.level, 2,
                f"{machine} fees did not count toward the mining slot total",
            )

    async def test_the_cap_is_derived_not_stored(self):
        # Fees written directly, with nothing calling an "apply" function:
        # the cap still moves. This is what makes the unlock retroactive for
        # servers that paid their way past a threshold before 1.3 shipped.
        await self.set_fees(furnace=mining_slot_threshold(4))
        slots = await mining_slot_status(self.db, GUILD)
        self.assertEqual(slots.slots, BASE_MINING_SLOTS + 3)

    async def test_banking_a_fee_unlocks_and_announces_once(self):
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "furnace", MINING_SLOT_THRESHOLD_BASE)

        slots = await mining_slot_status(self.db, GUILD)
        self.assertEqual(slots.slots, BASE_MINING_SLOTS + 1)

        notices = await self.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn(str(BASE_MINING_SLOTS + 1), notices[0]["body"])

    async def test_further_fees_below_the_next_threshold_do_not_repost(self):
        # post_server_notification does not dedupe; mining_slots_announced is
        # the guard, and without it every subsequent fee would repost this.
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "furnace", MINING_SLOT_THRESHOLD_BASE)
        for _ in range(5):
            async with self.db.transaction() as tx:
                await bank_infrastructure_fee(tx, GUILD, "factory", 1.0)

        self.assertEqual(len(await self.notices()), 1)

    async def test_each_further_threshold_announces_again(self):
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "furnace", mining_slot_threshold(2))
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(
                tx, GUILD, "press", mining_slot_threshold(3) - mining_slot_threshold(2)
            )

        notices = await self.notices()
        self.assertEqual(len(notices), 2)
        self.assertIn(str(BASE_MINING_SLOTS + 2), notices[1]["body"])

    async def test_crossing_several_thresholds_at_once_announces_the_real_total(self):
        # A large donation, or the first fee paid by a server that was already
        # past a threshold when 1.3 shipped. Announcing "+1" here would name a
        # number the player cannot reconcile with /mine status.
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "scrapper", mining_slot_threshold(5))

        notices = await self.notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["title"], "⛏️ New Mining Slots")
        self.assertIn(str(BASE_MINING_SLOTS + 4), notices[0]["body"])

    async def test_a_single_slot_is_announced_in_the_singular(self):
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "blast_furnace", mining_slot_threshold(2))
        notices = await self.notices()
        self.assertEqual(notices[0]["title"], "⛏️ New Mining Slot")

    async def test_announcing_is_idempotent_without_a_fee(self):
        await self.set_fees(furnace=mining_slot_threshold(3))
        for _ in range(3):
            level = await announce_mining_slot_unlocks(self.db, GUILD)
        self.assertEqual(level, 3)
        self.assertEqual(len(await self.notices()), 1)

    async def test_banking_still_levels_the_machine_itself(self):
        # The same money does both jobs; neither is deducted from the other.
        async with self.db.transaction() as tx:
            level = await bank_infrastructure_fee(
                tx, GUILD, "furnace", MINING_SLOT_THRESHOLD_BASE
            )
        self.assertEqual(level, 3)  # upgrade_threshold(3) == 25

        row = await self.db.fetchone(
            "SELECT furnace_level, furnace_fees_collected FROM server_config WHERE guild_id = ?",
            (GUILD,),
        )
        self.assertEqual(row["furnace_level"], 3)
        self.assertEqual(row["furnace_fees_collected"], MINING_SLOT_THRESHOLD_BASE)
        self.assertEqual((await mining_slot_status(self.db, GUILD)).slots, BASE_MINING_SLOTS + 1)

    async def test_an_unknown_machine_is_refused(self):
        # The machine name is interpolated into SQL, so this is the guard that
        # keeps that safe - the same one apply_machine_upgrades carries.
        with self.assertRaises(ValueError):
            async with self.db.transaction() as tx:
                await bank_infrastructure_fee(tx, GUILD, "furnace; DROP TABLE users", 1.0)


class MiningSlotsFullMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_the_rejection_names_the_cap_and_what_lifts_it(self):
        # A refused player may have no other reason to know slots exist, so the
        # message has to carry the cap, the next threshold and the progress.
        slots = await mining_slot_status(self.db, GUILD)
        message = mining_slots_full_message(slots, None)
        self.assertIn(str(BASE_MINING_SLOTS), message)
        self.assertIn("25.00", message)
        self.assertIn("infrastructure fees", message)


class _FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(content if content is not None else kwargs)


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeInteraction:
    """The three attributes mine_place and respond() actually touch. Building
    the cog with __new__ and the interaction by hand is what lets the real
    command run against a real database without a gateway connection - the same
    approach tests/test_guild_removal.py takes."""

    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.user = _FakeUser(user_id)
        self.response = _FakeResponse()


class MinePlaceSlotEnforcementTestCase(unittest.IsolatedAsyncioTestCase):
    """The cap as /mine place actually enforces it, rather than as the helper
    reports it. This is the wiring the helper tests above cannot see."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)

        self.cog = MiningCog.__new__(MiningCog)
        self.cog.db = self.db

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def add_drill(self, user_id, guild_id):
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (?, ?, 'iron_drill')",
            (guild_id, user_id),
        )

    async def place(self, user_id, drill_id):
        interaction = _FakeInteraction(GUILD, user_id)
        await MiningCog.mine_place.callback(self.cog, interaction, drill_id)
        return interaction.response.sent

    async def placed_count(self, user_id):
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND owner_id = ?",
            (GUILD, user_id),
        )
        return row["cnt"]

    async def test_a_player_is_refused_at_the_base_slot_count(self):
        user = 777
        for _ in range(BASE_MINING_SLOTS):
            await self.add_drill(user, GUILD)
        spare = await self.add_drill(user, None)

        sent = await self.place(user, spare)

        self.assertEqual(await self.placed_count(user), BASE_MINING_SLOTS)
        self.assertIn(str(BASE_MINING_SLOTS), sent[0])
        self.assertIn("25.00", sent[0])

    async def test_an_unlocked_slot_lets_the_same_placement_through(self):
        # The same player, the same spare drill - the only thing that changed
        # is that the server paid its fees.
        user = 778
        for _ in range(BASE_MINING_SLOTS):
            await self.add_drill(user, GUILD)
        spare = await self.add_drill(user, None)

        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "factory", MINING_SLOT_THRESHOLD_BASE)

        await self.place(user, spare)
        self.assertEqual(await self.placed_count(user), BASE_MINING_SLOTS + 1)

        row = await self.db.fetchone(
            "SELECT guild_id FROM drills WHERE drill_id = ?", (spare,)
        )
        self.assertEqual(row["guild_id"], GUILD)

    async def test_the_unlocked_slot_is_not_infinite(self):
        # One slot bought means one more drill, not an open cap.
        user = 779
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "press", MINING_SLOT_THRESHOLD_BASE)
        for _ in range(BASE_MINING_SLOTS + 1):
            await self.add_drill(user, GUILD)
        spare = await self.add_drill(user, None)

        sent = await self.place(user, spare)

        self.assertEqual(await self.placed_count(user), BASE_MINING_SLOTS + 1)
        self.assertIn(str(BASE_MINING_SLOTS + 1), sent[0])

    async def test_the_slots_belong_to_the_server_not_the_payer(self):
        # Nobody bought a slot for themselves. A player who has paid nothing
        # gets the slot the server's fees unlocked.
        payer, freeloader = 780, 781
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "scrapper", MINING_SLOT_THRESHOLD_BASE)

        for _ in range(BASE_MINING_SLOTS):
            await self.add_drill(freeloader, GUILD)
        spare = await self.add_drill(freeloader, None)

        await self.place(freeloader, spare)
        self.assertEqual(await self.placed_count(freeloader), BASE_MINING_SLOTS + 1)
        self.assertEqual(await self.placed_count(payer), 0)

    async def test_another_server_is_unaffected(self):
        # Slots are per server, like the pool and the currency that bought them.
        other_guild = GUILD + 1
        await ensure_server_row(self.db, other_guild)
        user = 782
        async with self.db.transaction() as tx:
            await bank_infrastructure_fee(tx, GUILD, "furnace", MINING_SLOT_THRESHOLD_BASE)

        for _ in range(BASE_MINING_SLOTS):
            await self.add_drill(user, other_guild)
        spare = await self.add_drill(user, None)

        interaction = _FakeInteraction(other_guild, user)
        await MiningCog.mine_place.callback(self.cog, interaction, spare)

        row = await self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND owner_id = ?",
            (other_guild, user),
        )
        self.assertEqual(row["cnt"], BASE_MINING_SLOTS)
        self.assertIn(str(BASE_MINING_SLOTS), interaction.response.sent[0])


if __name__ == "__main__":
    unittest.main()
