"""
Tests for donations and the shared machine-upgrade rule.

The distinction being defended is the one from docs/market.md section 1: paying
into a machine BURNS currency (it leaves circulation, and levels the machine),
while handing it to another player merely MOVES it. Getting those the wrong way
round would either quietly inflate a server or quietly drain it, and neither
shows up anywhere except in the section 4 ledger months later.
"""
import tempfile
import unittest
from pathlib import Path

from database.db import Database, InsufficientQuantity
from data.materials import UPGRADE_THRESHOLD_BASE, upgrade_threshold
from utils.db_helpers import (
    MACHINES,
    adjust_currency_balance,
    apply_machine_upgrades,
    deduct_currency_balance,
    ensure_server_row,
    ensure_user_row,
    get_currency_balance,
    record_burned,
)

GUILD = 3131
GIVER = 11
TAKER = 22


class DonateTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        for user_id in (GIVER, TAKER):
            await ensure_user_row(self.db, user_id)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def credit(self, user_id, amount):
        await adjust_currency_balance(self.db, GUILD, user_id, amount)

    async def balance(self, user_id):
        return await get_currency_balance(self.db, GUILD, user_id)

    async def machine(self, name):
        row = await self.db.fetchone(
            f"SELECT {name}_level AS level, {name}_fees_collected AS collected "
            f"FROM server_config WHERE guild_id = ?",
            (GUILD,),
        )
        return row["level"], row["collected"]

    async def ledger(self):
        row = await self.db.fetchone(
            "SELECT currency_minted_total, currency_burned_total FROM server_config "
            "WHERE guild_id = ?",
            (GUILD,),
        )
        return row["currency_minted_total"], row["currency_burned_total"]

    async def donate_to_machine(self, name, amount):
        """What the command body does, minus the Discord parts."""
        async with self.db.transaction() as tx:
            await deduct_currency_balance(tx, GUILD, GIVER, amount)
            await tx.execute(
                f"UPDATE server_config SET {name}_fees_collected = "
                f"{name}_fees_collected + ? WHERE guild_id = ?",
                (amount, GUILD),
            )
            await record_burned(tx, GUILD, amount)
            return await apply_machine_upgrades(tx, GUILD, name)

    async def donate_to_player(self, amount):
        async with self.db.transaction() as tx:
            await deduct_currency_balance(tx, GUILD, GIVER, amount)
            await adjust_currency_balance(tx, GUILD, TAKER, amount)


class MachineUpgradeTests(DonateTestCase):
    def test_every_machine_shares_the_rule(self):
        # Not a database test - just that the four names the helper accepts are
        # the four the rest of the code knows about.
        self.assertEqual(set(MACHINES), {"furnace", "factory", "press", "scrapper"})

    async def test_an_unknown_machine_is_refused(self):
        # The helper interpolates its argument into SQL (the columns are named
        # after the machine), so the allowlist is load-bearing, not cosmetic.
        with self.assertRaises(ValueError):
            async with self.db.transaction() as tx:
                await apply_machine_upgrades(tx, GUILD, "furnace; DROP TABLE users")

    async def test_it_levels_up_at_the_threshold(self):
        for machine in MACHINES:
            with self.subTest(machine=machine):
                await self.db.execute(
                    f"UPDATE server_config SET {machine}_fees_collected = ?, {machine}_level = 1 "
                    f"WHERE guild_id = ?",
                    (UPGRADE_THRESHOLD_BASE, GUILD),
                )
                async with self.db.transaction() as tx:
                    self.assertEqual(await apply_machine_upgrades(tx, GUILD, machine), 2)

    async def test_one_payment_can_cross_several_thresholds(self):
        # Which is exactly what a large donation does, and why the helper loops.
        # 5 + 25 + 125 reaches level 4 under the 1.2 ladder.
        await self.db.execute(
            "UPDATE server_config SET furnace_fees_collected = ? WHERE guild_id = ?",
            (upgrade_threshold(4), GUILD),
        )
        async with self.db.transaction() as tx:
            self.assertEqual(await apply_machine_upgrades(tx, GUILD, "furnace"), 4)

    async def test_it_never_lowers_a_level(self):
        await self.db.execute(
            "UPDATE server_config SET furnace_level = 5, furnace_fees_collected = 0 "
            "WHERE guild_id = ?", (GUILD,)
        )
        async with self.db.transaction() as tx:
            self.assertEqual(await apply_machine_upgrades(tx, GUILD, "furnace"), 5)


class InfrastructureDonationTests(DonateTestCase):
    async def test_it_charges_the_donor_and_credits_the_machine(self):
        await self.credit(GIVER, 10.0)
        await self.donate_to_machine("factory", 4.0)
        self.assertAlmostEqual(await self.balance(GIVER), 6.0)
        level, collected = await self.machine("factory")
        self.assertAlmostEqual(collected, 4.0)
        self.assertEqual(level, 1)

    async def test_a_big_enough_donation_levels_the_machine(self):
        await self.credit(GIVER, 100.0)
        self.assertEqual(await self.donate_to_machine("press", UPGRADE_THRESHOLD_BASE), 2)

    async def test_the_currency_is_burned_not_moved(self):
        # The whole point of the sink. It leaves circulation; it does not turn
        # up in anybody else's balance.
        await self.credit(GIVER, 10.0)
        await self.donate_to_machine("furnace", 3.0)
        _, burned = await self.ledger()
        self.assertAlmostEqual(burned, 3.0)
        self.assertEqual(await self.balance(TAKER), 0.0)

    async def test_it_cannot_be_overdrawn(self):
        await self.credit(GIVER, 1.0)
        with self.assertRaises(InsufficientQuantity):
            await self.donate_to_machine("furnace", 5.0)
        # Rolled back whole - the machine got nothing either.
        self.assertAlmostEqual(await self.balance(GIVER), 1.0)
        self.assertAlmostEqual((await self.machine("furnace"))[1], 0.0)

    async def test_donations_stack_with_fees_already_paid(self):
        # Deliberately the same pot the machine's own fees go into: a level is
        # meant to say how much has gone through the machine, and money is money.
        await self.db.execute(
            "UPDATE server_config SET furnace_fees_collected = 3.0 WHERE guild_id = ?", (GUILD,)
        )
        await self.credit(GIVER, 10.0)
        self.assertEqual(await self.donate_to_machine("furnace", 2.0), 2)


class PlayerDonationTests(DonateTestCase):
    async def test_it_moves_the_money(self):
        await self.credit(GIVER, 10.0)
        await self.donate_to_player(4.0)
        self.assertAlmostEqual(await self.balance(GIVER), 6.0)
        self.assertAlmostEqual(await self.balance(TAKER), 4.0)

    async def test_the_supply_is_unchanged(self):
        # A transfer is neither a faucet nor a sink, so the section 4 ledger
        # must not move. Recording it as either would make every server's
        # minted/burned totals drift by however much its players traded.
        await self.credit(GIVER, 10.0)
        before = await self.ledger()
        await self.donate_to_player(4.0)
        self.assertEqual(await self.ledger(), before)

    async def test_it_cannot_create_currency_by_racing(self):
        # Debit before credit. If the balance moved since the check, the
        # deduction raises and the credit never runs - the other order would
        # mint the difference on exactly that race.
        await self.credit(GIVER, 1.0)
        with self.assertRaises(InsufficientQuantity):
            await self.donate_to_player(5.0)
        self.assertAlmostEqual(await self.balance(GIVER), 1.0)
        self.assertEqual(await self.balance(TAKER), 0.0)

    async def test_the_total_is_conserved(self):
        await self.credit(GIVER, 7.0)
        await self.credit(TAKER, 3.0)
        await self.donate_to_player(2.5)
        self.assertAlmostEqual(await self.balance(GIVER) + await self.balance(TAKER), 10.0)


if __name__ == "__main__":
    unittest.main()
