"""
Tests for the job board against a real database: posting once a day, progress
accumulating across sales, and the reward paying exactly once per player.

The once-per-day-per-player rule is the part worth being careful about. It's
enforced by a single guarded UPDATE (claimed_at IS NULL AND sold >= quantity)
rather than by a read followed by a write, precisely so two sales racing to
finish the task can't both pay it - test_two_racing_sales_pay_the_bonus_once is
that guarantee, run the way it would actually be broken.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from database.db import Database
from utils.db_helpers import ensure_server_row, ensure_user_row, get_currency_balance
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
    job_reward,
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
        """Both derive from how well stocked the server is, and the day's own
        selling moves that constantly - so recomputing on read would grow the
        task under someone already partway through it every time anybody sold
        anything. Stock is the goalpost worth nailing down; member count can no
        longer reach either figure at all (see test_jobboard.py:
        test_it_does_not_scale_with_member_count)."""
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

    async def test_the_task_is_priced_off_the_stock_it_was_posted_against(self):
        """The wiring the arithmetic tests can't see: that ensure_todays_job
        hands job_quantity and job_reward the CHOSEN material's own stock and
        target, not another material's or a stale read. Every eligible material
        is stocked to a different fraction of its target, so crossing the wires
        gives a different answer for all but one of them."""
        fractions = {m: (i + 1) / 10 for i, m in enumerate(JOB_BOARD_MATERIALS)}
        for material_id, fraction in fractions.items():
            await self.db.execute(
                "INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)",
                (GUILD, material_id, int(target_stock(MEMBERS, material_id) * fraction)),
            )

        job = await self.post()
        material_id = job["material_id"]
        target = target_stock(MEMBERS, material_id)
        stock = int(target * fractions[material_id])

        self.assertEqual(job["quantity"], job_quantity(material_id, stock, target))
        self.assertAlmostEqual(job["reward"], job_reward(material_id, job["quantity"], stock, target))
        self.assertGreaterEqual(job["reward"], JOB_BOARD_TARGET_PAYOUT)

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
        paid = await self.sell(self.material, self.needed)
        self.assertAlmostEqual(paid, self.job["reward"])
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, USER), self.job["reward"]
        )

    async def test_the_reward_is_recorded_as_minted(self):
        # It's a currency faucet, and docs/market.md section 4's accounting has
        # to see it or the minted total stops matching what's in circulation.
        await self.sell(self.material, self.needed)
        self.assertAlmostEqual(await self.minted(), self.job["reward"])

    async def test_overshooting_pays_the_reward_once(self):
        self.assertGreater(await self.sell(self.material, self.needed * 5), 0.0)
        self.assertEqual(await self.sell(self.material, self.needed * 5), 0.0)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, USER), self.job["reward"]
        )

    async def test_selling_again_after_claiming_pays_nothing(self):
        await self.sell(self.material, self.needed)
        for _ in range(3):
            self.assertEqual(await self.sell(self.material, self.needed), 0.0)

    async def test_two_racing_sales_pay_the_bonus_once(self):
        """The guarded claim, tested the way it would actually fail. Read-then-
        write would let both of these see claimed_at NULL and both pay."""
        results = await asyncio.gather(
            *(self.sell(self.material, self.needed) for _ in range(4))
        )
        self.assertEqual(sum(1 for reward in results if reward > 0), 1)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, USER), self.job["reward"]
        )
        self.assertAlmostEqual(await self.minted(), self.job["reward"])

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


if __name__ == "__main__":
    unittest.main()
