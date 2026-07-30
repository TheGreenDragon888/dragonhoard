"""
Tests for the transaction layer and the guarded deduction helpers.

These cover the two failure modes Database.transaction exists to prevent: a
multi-write operation tearing partway through, and two concurrent operations
both acting on the same read. Each test builds its own throwaway database, so
nothing here touches the live one.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from database.db import Database, InsufficientQuantity
from utils.db_helpers import (
    adjust_currency_balance,
    adjust_user_quantity,
    charge_user_fee,
    deduct_currency_balance,
    deduct_server_stock,
    deduct_user_quantity,
    ensure_server_row,
    ensure_user_row,
    get_currency_balance,
    get_user_quantity,
)

USER = 4242
GUILD = 8484


class TransactionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_user_row(self.db, USER)
        await ensure_server_row(self.db, GUILD)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def give(self, material_id, quantity):
        await adjust_user_quantity(self.db, USER, material_id, quantity)


class WalTests(TransactionTestCase):
    async def test_database_uses_wal(self):
        # Under the default journal mode a writer blocks every reader, which is
        # the likeliest way a statement fails partway through an operation.
        row = await self.db.fetchone("PRAGMA journal_mode")
        self.assertEqual(row[0], "wal")


class AtomicityTests(TransactionTestCase):
    async def test_a_failed_transaction_writes_nothing(self):
        await self.give("iron", 10)
        with self.assertRaises(RuntimeError):
            async with self.db.transaction() as tx:
                await deduct_user_quantity(tx, USER, "iron", 10)
                await adjust_currency_balance(tx, GUILD, USER, 500)
                raise RuntimeError("failure partway through")

        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 10)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 0.0)

    async def test_a_successful_transaction_writes_everything(self):
        await self.give("iron", 10)
        async with self.db.transaction() as tx:
            await deduct_user_quantity(tx, USER, "iron", 10)
            await adjust_currency_balance(tx, GUILD, USER, 500)

        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 500.0)

    async def test_an_insufficient_deduction_rolls_back_its_whole_operation(self):
        # The shape of a craft: several inputs deducted in a row, where the
        # last one turns out not to be affordable. None of it should stick.
        await self.give("iron", 10)
        await self.give("copper", 1)
        with self.assertRaises(InsufficientQuantity):
            async with self.db.transaction() as tx:
                await deduct_user_quantity(tx, USER, "iron", 10)
                await deduct_user_quantity(tx, USER, "copper", 5)

        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 10)
        self.assertEqual(await get_user_quantity(self.db, USER, "copper"), 1)

    async def test_reads_inside_a_transaction_see_its_own_writes(self):
        async with self.db.transaction() as tx:
            await adjust_user_quantity(tx, USER, "coal", 7)
            self.assertEqual(await get_user_quantity(tx, USER, "coal"), 7)


class GuardedDeductionTests(TransactionTestCase):
    async def test_deducting_more_than_held_raises_and_changes_nothing(self):
        await self.give("iron", 3)
        with self.assertRaises(InsufficientQuantity):
            await deduct_user_quantity(self.db, USER, "iron", 4)
        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 3)

    async def test_deducting_exactly_what_is_held_succeeds(self):
        await self.give("iron", 3)
        await deduct_user_quantity(self.db, USER, "iron", 3)
        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 0)

    async def test_deducting_from_a_missing_row_raises(self):
        with self.assertRaises(InsufficientQuantity):
            await deduct_user_quantity(self.db, USER, "diamond", 1)

    async def test_overdrawing_a_balance_raises(self):
        await adjust_currency_balance(self.db, GUILD, USER, 5.0)
        with self.assertRaises(InsufficientQuantity):
            await deduct_currency_balance(self.db, GUILD, USER, 5.01)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 5.0)

    async def test_overdrawing_server_stock_raises(self):
        with self.assertRaises(InsufficientQuantity):
            await deduct_server_stock(self.db, GUILD, "iron_ore", 1)

    async def test_an_unaffordable_fee_raises_instead_of_zeroing_the_balance(self):
        # It used to clamp to zero, so a fee charged against too small a
        # balance burned less than it recorded and drifted the burn total.
        await adjust_currency_balance(self.db, GUILD, USER, 1.0)
        with self.assertRaises(InsufficientQuantity):
            await charge_user_fee(self.db, GUILD, USER, 10.0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 1.0)
        cfg = await self.db.fetchone(
            "SELECT currency_burned_total FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        self.assertEqual(cfg["currency_burned_total"], 0.0)

    async def test_an_affordable_fee_burns_exactly_what_it_records(self):
        await adjust_currency_balance(self.db, GUILD, USER, 10.0)
        await charge_user_fee(self.db, GUILD, USER, 2.5)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 7.5)
        cfg = await self.db.fetchone(
            "SELECT currency_burned_total FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        self.assertEqual(cfg["currency_burned_total"], 2.5)


class IsolationTests(TransactionTestCase):
    async def _sell(self, quantity, payout):
        """The shape of /market sell: read, validate, then deduct and pay."""
        async with self.db.transaction() as tx:
            have = await get_user_quantity(tx, USER, "iron")
            if have < quantity:
                return False
            # Yielding here is what a real command does constantly (every await
            # is a chance for another task to run). Before transactions, this
            # is exactly where a second invocation slipped in.
            await asyncio.sleep(0)
            await deduct_user_quantity(tx, USER, "iron", quantity)
            await adjust_currency_balance(tx, GUILD, USER, payout)
            return True

    async def test_concurrent_sells_cannot_be_paid_twice_for_one_stack(self):
        await self.give("iron", 10)
        results = await asyncio.gather(self._sell(10, 500), self._sell(10, 500))

        self.assertEqual(sorted(results), [False, True], "exactly one sale should succeed")
        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 500.0)

    async def test_many_concurrent_sells_pay_out_only_what_was_held(self):
        await self.give("iron", 50)
        results = await asyncio.gather(*(self._sell(10, 100) for _ in range(12)))

        self.assertEqual(sum(results), 5, "only five stacks of ten existed")
        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 500.0)

    async def test_inventory_never_goes_negative_under_contention(self):
        await self.give("coal", 20)

        async def spend():
            try:
                async with self.db.transaction() as tx:
                    await asyncio.sleep(0)
                    await deduct_user_quantity(tx, USER, "coal", 3)
            except InsufficientQuantity:
                pass

        await asyncio.gather(*(spend() for _ in range(15)))
        remaining = await get_user_quantity(self.db, USER, "coal")
        self.assertGreaterEqual(remaining, 0)
        self.assertEqual(remaining, 20 % 3)


if __name__ == "__main__":
    unittest.main()
