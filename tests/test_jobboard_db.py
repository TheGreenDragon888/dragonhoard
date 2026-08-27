"""
Tests for the job board against a real database: posting once a day, progress
accumulating across sales, and the reward paying once per COMPLETION.

Paying per completion rather than once a day (1.3) is the part worth being
careful about, and it cuts both ways. Every completion has to pay - including
several at once, when one sale finishes the task more than over
(test_one_sale_can_complete_the_task_many_times) - and none of them may pay
twice, including when two sales race for the same one
(test_two_racing_sales_pay_each_completion_once). Both come out of the same
place: claims_paid is banked in the same guarded statement that computes the
payout from it.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from database.db import Database
from utils.db_helpers import (
    adjust_currency_balance,
    adjust_user_quantity,
    deduct_user_quantity,
    ensure_server_row,
    ensure_user_row,
    get_currency_balance,
    get_user_quantity,
)
from utils.job_board import (
    credit_job_progress,
    ensure_todays_job,
    get_progress,
    job_board_today,
)
from data.materials import (
    JOB_BOARD_MATERIALS,
    JOB_BOARD_TARGET_PAYOUT,
    job_quantity,
    target_stock,
)

GUILD = 8484
USER = 4242
OTHER_USER = 9999
MEMBERS = 20


class JobBoardTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)
        await ensure_user_row(self.db, OTHER_USER)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def post(self):
        async with self.db.transaction() as tx:
            return await ensure_todays_job(tx, GUILD, MEMBERS)

    async def sell(self, material_id, quantity, user_id=USER):
        """The bonus a sale earned. credit_job_progress returns (reward,
        completions); the completion count is checked where it is the point of
        the test rather than unpacked into every caller."""
        reward, _ = await self.sell_with_count(material_id, quantity, user_id)
        return reward

    async def sell_with_count(self, material_id, quantity, user_id=USER):
        async with self.db.transaction() as tx:
            return await credit_job_progress(tx, GUILD, user_id, material_id, quantity, MEMBERS)

    async def minted(self):
        row = await self.db.fetchone(
            "SELECT currency_minted_total FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        return row["currency_minted_total"]


class PostingTests(JobBoardTestCase):
    async def test_a_job_is_posted_for_today(self):
        job = await self.post()
        self.assertEqual(job["job_date"], job_board_today())
        self.assertGreaterEqual(job["quantity"], 1)
        self.assertGreater(job["reward"], 0)

    async def test_asking_twice_posts_one_job(self):
        """Called from both /jobboard and every /market sell, so it has to be
        safe to call constantly - and it must not reroll the task under
        someone halfway through it."""
        first = await self.post()
        second = await self.post()
        self.assertEqual(first["material_id"], second["material_id"])
        self.assertEqual(first["quantity"], second["quantity"])

        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM daily_jobs")
        self.assertEqual(row["n"], 1)

    async def test_concurrent_posting_still_posts_one_job(self):
        await asyncio.gather(*(self.post() for _ in range(5)))
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM daily_jobs")
        self.assertEqual(row["n"], 1)

    async def test_the_quantity_and_reward_are_frozen_at_posting(self):
        """Neither derives from the server's stock or size any more (1.3), so
        this is no longer load-bearing against the day's own selling - but the
        row is still what a task in progress is defined by, and a balance
        retune between two of a player's sales must not move it."""
        job = await self.post()
        await self.db.execute(
            "INSERT INTO server_material_storage (guild_id, material_id, quantity) "
            "VALUES (?, ?, ?) ON CONFLICT (guild_id, material_id) "
            "DO UPDATE SET quantity = excluded.quantity",
            (GUILD, job["material_id"], 10_000_000),
        )
        async with self.db.transaction() as tx:
            later = await ensure_todays_job(tx, GUILD, MEMBERS * 10)
        self.assertEqual(later["quantity"], job["quantity"])
        self.assertEqual(later["reward"], job["reward"])

    async def test_the_task_is_sized_for_the_material_it_picked(self):
        """The wiring the arithmetic tests can't see: that ensure_todays_job
        sizes the task for the material it CHOSE rather than for another one.
        Every eligible material asks for a different quantity, so crossing the
        wires gives a different answer for all but one of them."""
        job = await self.post()
        self.assertEqual(job["quantity"], job_quantity(job["material_id"]))

    async def test_the_reward_is_the_flat_target_payout(self):
        """One completion, one target payout - not a figure derived from stock
        (1.3). Stocking the server to wildly different fractions of target must
        not move it, whichever material comes up."""
        for i, material_id in enumerate(JOB_BOARD_MATERIALS):
            await self.db.execute(
                "INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)",
                (GUILD, material_id, target_stock(MEMBERS, material_id) * i),
            )
        job = await self.post()
        self.assertEqual(job["reward"], JOB_BOARD_TARGET_PAYOUT)

    async def test_it_asks_for_what_the_server_is_short_of(self):
        # The selection is weighted, not deterministic, so this checks the
        # tendency: with every other material stocked far past target, the one
        # left at zero should come up most of the time.
        for material_id in ("iron_ore", "copper_ore", "iron", "copper", "steel"):
            await self.db.execute(
                "INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)",
                (GUILD, material_id, 10_000_000),
            )
        picks = []
        for _ in range(40):
            await self.db.execute("DELETE FROM daily_jobs")
            picks.append((await self.post())["material_id"])
        self.assertGreater(picks.count("coal"), 20)

    async def test_each_server_gets_its_own_job(self):
        await ensure_server_row(self.db, 555)
        await self.post()
        async with self.db.transaction() as tx:
            await ensure_todays_job(tx, 555, MEMBERS)
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM daily_jobs")
        self.assertEqual(row["n"], 2)


class ProgressTests(JobBoardTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.job = await self.post()
        self.material = self.job["material_id"]
        self.needed = self.job["quantity"]

    async def sold(self, user_id=USER):
        row = await get_progress(self.db, GUILD, user_id, self.job["job_date"])
        return row["sold"] if row else 0

    async def test_progress_accumulates_across_several_sales(self):
        """The task is completed by selling however you like - ten now and the
        rest later has to count the same as one big sale."""
        await self.sell(self.material, 1)
        await self.sell(self.material, 2)
        await self.sell(self.material, 3)
        self.assertEqual(await self.sold(), 6)

    async def test_selling_the_wrong_material_does_nothing(self):
        other = "steel" if self.material != "steel" else "iron"
        self.assertEqual(await self.sell(other, 10_000), 0.0)
        self.assertEqual(await self.sold(), 0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 0.0)

    async def test_falling_short_pays_nothing(self):
        if self.needed < 2:
            self.skipTest("task is a single unit, so there is no short of it")
        self.assertEqual(await self.sell(self.material, self.needed - 1), 0.0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, USER), 0.0)

    async def test_completing_it_pays_the_reward(self):
        paid, completions = await self.sell_with_count(self.material, self.needed)
        self.assertEqual(completions, 1)
        self.assertAlmostEqual(paid, self.job["reward"])
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, USER), self.job["reward"]
        )

    async def test_the_reward_is_recorded_as_minted(self):
        # It's a currency faucet, and docs/market.md section 4's accounting has
        # to see it or the minted total stops matching what's in circulation.
        await self.sell(self.material, self.needed)
        self.assertAlmostEqual(await self.minted(), self.job["reward"])

    async def test_one_sale_can_complete_the_task_many_times(self):
        """The 1.3 change, and the reason /market sell's limit went to a
        million: one command can deliver far more than one task's worth, and
        every completion in it is paid."""
        reward, completions = await self.sell_with_count(self.material, self.needed * 5)
        self.assertEqual(completions, 5)
        self.assertAlmostEqual(reward, self.job["reward"] * 5)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, USER), self.job["reward"] * 5
        )

    async def test_a_partial_lap_is_not_paid_until_it_is_finished(self):
        """The remainder carries, it doesn't round up: selling one and a half
        tasks pays once, and the half that is left counts toward the next."""
        if self.needed < 2:
            self.skipTest("task is a single unit, so there are no partial laps")
        reward, completions = await self.sell_with_count(
            self.material, self.needed + self.needed // 2
        )
        self.assertEqual(completions, 1)
        self.assertAlmostEqual(reward, self.job["reward"])

        # The carried remainder plus enough to finish the second lap.
        _, completions = await self.sell_with_count(
            self.material, self.needed - self.needed // 2
        )
        self.assertEqual(completions, 1)

    async def test_completions_accumulate_across_separate_sales(self):
        # Three sales of the task quantity are three completions, exactly as
        # one sale of three times it is.
        for expected in range(1, 4):
            _, completions = await self.sell_with_count(self.material, self.needed)
            self.assertEqual(completions, 1)
            self.assertAlmostEqual(
                await get_currency_balance(self.db, GUILD, USER),
                self.job["reward"] * expected,
            )

    async def test_each_completion_is_paid_exactly_once(self):
        # The claims_paid ledger, checked against what was actually credited.
        await self.sell(self.material, self.needed * 3)
        await self.sell(self.material, self.needed)
        row = await get_progress(self.db, GUILD, USER, self.job["job_date"])
        self.assertEqual(row["claims_paid"], 4)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, USER), self.job["reward"] * 4
        )

    async def test_two_racing_sales_pay_each_completion_once(self):
        """The guarded claim, tested the way it would actually fail. Four
        sales of one task quantity each are four completions in total, however
        they interleave - not eight, and not one."""
        results = await asyncio.gather(
            *(self.sell_with_count(self.material, self.needed) for _ in range(4))
        )
        self.assertEqual(sum(completions for _, completions in results), 4)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, USER), self.job["reward"] * 4
        )
        self.assertAlmostEqual(await self.minted(), self.job["reward"] * 4)

    async def test_every_player_can_claim_it(self):
        # It isn't a race - one player finishing doesn't use the job up.
        await self.sell(self.material, self.needed, user_id=USER)
        await self.sell(self.material, self.needed, user_id=OTHER_USER)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, OTHER_USER), self.job["reward"]
        )

    async def test_one_players_progress_is_their_own(self):
        await self.sell(self.material, 3, user_id=OTHER_USER)
        self.assertEqual(await self.sold(USER), 0)
        self.assertEqual(await self.sold(OTHER_USER), 3)


class RolloverTests(JobBoardTestCase):
    async def test_a_new_day_posts_a_new_job_and_resets_progress(self):
        job = await self.post()
        await self.sell(job["material_id"], 1)

        # Roll the clock by relabelling today's rows as yesterday's, which is
        # what a real midnight leaves behind.
        await self.db.execute("UPDATE daily_jobs SET job_date = '2000-01-01'")
        await self.db.execute("UPDATE daily_job_progress SET job_date = '2000-01-01'")

        today = await self.post()
        self.assertEqual(today["job_date"], job_board_today())
        self.assertIsNone(await get_progress(self.db, GUILD, USER, today["job_date"]))

    async def test_old_rows_are_pruned_when_a_new_job_is_posted(self):
        await self.db.execute(
            "INSERT INTO daily_jobs (guild_id, job_date, material_id, quantity, reward) "
            "VALUES (?, '2000-01-01', 'iron_ore', 10, 1.0)",
            (GUILD,),
        )
        await self.db.execute(
            "INSERT INTO daily_job_progress (guild_id, job_date, user_id, sold) "
            "VALUES (?, '2000-01-01', ?, 5)",
            (GUILD, USER),
        )

        await self.post()

        for table in ("daily_jobs", "daily_job_progress"):
            with self.subTest(table=table):
                row = await self.db.fetchone(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE job_date = '2000-01-01'"
                )
                self.assertEqual(row["n"], 0)


class SaleReceiptTotalsTests(JobBoardTestCase):
    """The /market sell receipt's post-transaction reads (cogs/economy.py:
    market_sell), mirroring market_sell's real write order - deduct the sold
    material, pay for it, then credit the job board - so the totals the
    receipt shows can never be stale relative to those writes. Direct analogue
    of tests/test_collect.py::CollectQueryTests::
    test_the_totals_lookup_reports_the_post_credit_amounts."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.job = await self.post()
        self.material = self.job["material_id"]
        self.needed = self.job["quantity"]
        async with self.db.transaction() as tx:
            await adjust_user_quantity(tx, USER, self.material, self.needed + 100)

    async def sell_and_read_totals(self, quantity, unit_price=1.0):
        async with self.db.transaction() as tx:
            await deduct_user_quantity(tx, USER, self.material, quantity)
            await adjust_currency_balance(tx, GUILD, USER, quantity * unit_price)
            bonus, _ = await credit_job_progress(tx, GUILD, USER, self.material, quantity, MEMBERS)
            remaining = await get_user_quantity(tx, USER, self.material)
            new_balance = await get_currency_balance(tx, GUILD, USER)
        return bonus, remaining, new_balance

    async def test_the_totals_lookup_reports_the_post_write_amounts(self):
        if self.needed < 2:
            self.skipTest("task is a single unit, so any sale completes it")
        quantity = 1
        bonus, remaining, new_balance = await self.sell_and_read_totals(quantity, unit_price=2.0)
        self.assertEqual(bonus, 0.0)
        self.assertEqual(remaining, self.needed + 100 - quantity)
        self.assertAlmostEqual(new_balance, quantity * 2.0)

    async def test_a_sale_that_completes_the_task_shows_the_bonus_already_folded_in(self):
        bonus, remaining, new_balance = await self.sell_and_read_totals(self.needed, unit_price=1.0)
        self.assertGreater(bonus, 0.0)
        self.assertEqual(remaining, 100)
        self.assertAlmostEqual(new_balance, self.needed * 1.0 + bonus)


if __name__ == "__main__":
    unittest.main()
