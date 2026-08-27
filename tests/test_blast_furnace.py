"""
Tests for the blast furnace: the 100x smelting identity, the rate that is the
one thing about it that ISN'T the furnace's times a hundred, and the drain
loop that turns queued batches into items.

The identity tests are the load-bearing ones. The blast furnace exists to be
the furnace at scale, and every claim made about it in the manual, the recipe
book and docs/mining.txt rests on "same ore per bar, same coal per bar, same
fee per bar". Those three are derived rather than typed out (data/materials.py:
BLAST_FURNACE_RECIPES, config.DEFAULT_BLAST_FURNACE_FEE), so what's checked
here is that the derivation is the one being claimed - a furnace recipe that
was retuned without the bulk one following would break these rather than
quietly making bulk smelting the cheaper way to play.

The drain-loop tests build the cog with __new__ rather than its constructor,
which would start the loop for real; the loop body is then driven a tick at a
time.
"""
import tempfile
import unittest
from pathlib import Path

import config
from cogs.blastfurnace import BlastFurnaceCog, PROCESS_TICK_MINUTES
from database.db import Database
from data.materials import (
    BLAST_FURNACE_BATCH_SIZE,
    BLAST_FURNACE_COAL_COST_PER_BATCH,
    BLAST_FURNACE_RATE_PER_LEVEL,
    BLAST_FURNACE_RECIPES,
    BASE_MINING_SLOTS,
    FURNACE_COAL_COST_PER_UNIT,
    PRESS_RECIPES,
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    blast_furnace_rate,
    effective_rate,
    furnace_rate,
    upgrade_threshold,
)
from utils.db_helpers import (
    MACHINES,
    ensure_server_row,
    ensure_user_row,
    get_user_quantity,
    queue_room,
)

GUILD = 8484
USER = 4242
OTHER_USER = 9999


class BlastFurnaceRecipeTests(unittest.TestCase):
    def test_it_smelts_exactly_what_the_furnace_smelts(self):
        self.assertEqual(set(BLAST_FURNACE_RECIPES), set(SMELTED_MATERIALS))

    def test_every_input_is_the_furnaces_times_the_batch_size(self):
        for material_id, recipe in BLAST_FURNACE_RECIPES.items():
            furnace_inputs = SMELTED_MATERIALS[material_id]["inputs"]
            with self.subTest(material=material_id):
                self.assertEqual(set(recipe["inputs"]), set(furnace_inputs))
                for input_id, quantity in recipe["inputs"].items():
                    self.assertEqual(
                        quantity, furnace_inputs[input_id] * BLAST_FURNACE_BATCH_SIZE
                    )

    def test_a_batch_produces_a_batch_size_of_items(self):
        for recipe in BLAST_FURNACE_RECIPES.values():
            self.assertEqual(recipe["output"], BLAST_FURNACE_BATCH_SIZE)

    def test_the_ore_per_smelted_unit_is_unchanged(self):
        """The claim the whole feature is sold on. Whatever the batch size is,
        dividing a batch's inputs by its output has to give the furnace's own
        recipe back exactly."""
        for material_id, recipe in BLAST_FURNACE_RECIPES.items():
            per_unit = {
                input_id: quantity / recipe["output"]
                for input_id, quantity in recipe["inputs"].items()
            }
            self.assertEqual(per_unit, SMELTED_MATERIALS[material_id]["inputs"])

    def test_the_derived_costs_are_the_expected_figures(self):
        # Pinned so a furnace retune that moves these is noticed rather than
        # silently rebalancing bulk smelting. These are also the numbers
        # docs/mining.txt and the manual's blast furnace page quote.
        self.assertEqual(BLAST_FURNACE_RECIPES["iron"]["inputs"], {"iron_ore": 1000})
        self.assertEqual(BLAST_FURNACE_RECIPES["copper"]["inputs"], {"copper_ore": 1000})
        self.assertEqual(
            BLAST_FURNACE_RECIPES["steel"]["inputs"], {"iron_ore": 2000, "coal": 400}
        )

    def test_fuel_costs_the_same_per_smelted_unit_as_the_furnace(self):
        self.assertEqual(
            BLAST_FURNACE_COAL_COST_PER_BATCH,
            FURNACE_COAL_COST_PER_UNIT * BLAST_FURNACE_BATCH_SIZE,
        )

    def test_a_batch_of_steel_costs_what_the_manual_says(self):
        # Recipe coal plus fuel coal, which is the figure the manual's "Is it
        # worth it?" note quotes and the one a player has to actually hold.
        steel = BLAST_FURNACE_RECIPES["steel"]["inputs"]
        self.assertEqual(steel["iron_ore"], 2000)
        self.assertEqual(steel["coal"] + BLAST_FURNACE_COAL_COST_PER_BATCH, 500)

    def test_the_fee_costs_the_same_per_smelted_unit_as_the_furnace(self):
        """Bulk smelting is faster, not cheaper. A fee that didn't scale with
        the batch would make the blast furnace a hundredfold discount on the
        game's primary currency sink (docs/market.md section 1)."""
        self.assertAlmostEqual(
            config.DEFAULT_BLAST_FURNACE_FEE,
            config.DEFAULT_FURNACE_FEE * BLAST_FURNACE_BATCH_SIZE,
            places=6,
        )


class BlastFurnaceRateTests(unittest.TestCase):
    """The figures data/materials.py: BLAST_FURNACE_RATE_PER_LEVEL argues
    from. They are quoted in that comment, in docs/mining.txt and in the 1.3
    changelog, so they are pinned here rather than left to be recomputed by
    hand the next time somebody wonders whether 20x was right."""

    def items_per_hour(self, level):
        return blast_furnace_rate(level) * BLAST_FURNACE_BATCH_SIZE

    def test_the_rate_is_linear_in_the_level(self):
        for level in (1, 2, 5, 10):
            self.assertEqual(blast_furnace_rate(level), BLAST_FURNACE_RATE_PER_LEVEL * level)

    def test_it_smelts_twenty_times_what_the_furnace_does_at_every_level(self):
        for level in (1, 2, 5, 10):
            with self.subTest(level=level):
                self.assertEqual(self.items_per_hour(level), furnace_rate(level) * 20)

    def test_one_player_can_saturate_the_furnace_the_whole_server_shares(self):
        """Why 20x rather than parity: mining scales with a server's player
        count and one shared furnace does not. Recomputed from the raw tables
        rather than from the rate constant it justifies.

        BASE_MINING_SLOTS is the FLOOR of the slot ladder, not a cap, so a
        server that has unlocked slots only makes this worse - the demand
        computed here is the smallest it can be.

        1.3 quadrupled the Diamond Drill's rate (120 -> 480/hour) for reasons
        unrelated to the blast furnace, which had already shipped calibrated
        against the old figure. That was a deliberate, accepted tradeoff, not
        an oversight this test is failing to catch - see the numbers below,
        which are what actually changed. Demand quadrupled from 20.4 to 81.6,
        so a single such player now exceeds a level 5 furnace outright rather
        than merely matching it, and the blast furnace's room to spare at
        level 1 shrank from 4x that demand down to about 1.2x."""
        drills = BASE_MINING_SLOTS * effective_rate("diamond_drill", 1)
        iron_ore_per_hour = drills * RAW_MATERIALS["iron_ore"]["drop_chance"]
        demand = iron_ore_per_hour / SMELTED_MATERIALS["iron"]["inputs"]["iron_ore"]

        self.assertAlmostEqual(demand, 81.6, places=1)
        self.assertLess(furnace_rate(5), demand)  # one such player now exceeds it outright
        self.assertGreater(self.items_per_hour(1), demand)  # the blast furnace still (barely) keeps up

    def test_it_turns_a_pressed_diamond_from_a_season_into_a_fortnight(self):
        """The feature's reason for existing, in the units the changelog uses."""
        steel = PRESS_RECIPES["diamond"]["inputs"]["steel"]
        self.assertAlmostEqual(steel / furnace_rate(5) / 24, 45.0, places=2)
        self.assertAlmostEqual(steel / self.items_per_hour(1) / 24, 11.25, places=2)

    def test_the_ore_behind_that_diamond_takes_a_server_about_as_long(self):
        """The other half of "the constraint is back on the ore supply": ten
        players mining flat out used to take about as long as the blast
        furnace's own 11-day figure above. 1.3's Diamond Drill buff (see
        test_one_player_can_saturate_the_furnace_the_whole_server_shares)
        quartered that to about 3 days, since it's driven by the same
        per-player mining rate - ten maxed-out players now mine faster than
        even the blast furnace's 11-day pace. docs/mining.txt quotes the
        updated figure."""
        steel = PRESS_RECIPES["diamond"]["inputs"]["steel"]
        recipe = SMELTED_MATERIALS["steel"]["inputs"]
        per_player = BASE_MINING_SLOTS * effective_rate("diamond_drill", 1)

        # Whichever raw input runs out first is what the wait actually is.
        days = max(
            steel * per_unit / (10 * per_player * RAW_MATERIALS[raw]["drop_chance"]) / 24
            for raw, per_unit in recipe.items()
        )
        self.assertAlmostEqual(days, 2.76, places=1)

    def test_the_furnace_cannot_simply_be_levelled_out_of_the_problem(self):
        """The second leg of the argument in BLAST_FURNACE_RATE_PER_LEVEL's
        comment, and the 13,467 hours docs/mining.txt quotes: levels are a
        high-water mark on lifetime fees, so a level 6 furnace is 561 days of
        nonstop smelting away at the default fee."""
        self.assertEqual(upgrade_threshold(6), 3125.0)

        hours, level, collected = 0.0, 1, 0.0
        while level < 6:
            need = upgrade_threshold(level + 1) - collected
            hours += need / (furnace_rate(level) * config.DEFAULT_FURNACE_FEE)
            collected += need
            level += 1
        self.assertAlmostEqual(hours, 13467, delta=1)
        self.assertAlmostEqual(hours / 24, 561, delta=1)


class BlastFurnaceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

        self.cog = BlastFurnaceCog.__new__(BlastFurnaceCog)
        self.cog.db = self.db
        self.cog._production_progress = {}

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def tick(self, times=1):
        """Runs the drain loop's body. A level 1 blast furnace does one batch
        an hour, so a whole hour of ticks is one batch."""
        for _ in range(times):
            await BlastFurnaceCog.process_loop.coro(self.cog)

    def ticks_per_hour(self):
        return int(60 / PROCESS_TICK_MINUTES)

    async def set_level(self, level):
        await self.db.execute(
            "UPDATE server_config SET blast_furnace_level = ? WHERE guild_id = ?",
            (level, GUILD),
        )

    async def queue_job(self, batches, target_id="iron", user_id=USER):
        return await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'blast_furnace', ?, ?)",
            (GUILD, user_id, target_id, batches),
        )

    async def job(self, job_id):
        return await self.db.fetchone(
            "SELECT quantity, status FROM production_jobs WHERE job_id = ?", (job_id,)
        )


class BlastFurnaceDrainTests(BlastFurnaceTestCase):
    async def test_a_finished_batch_credits_a_batch_size_of_items(self):
        job_id = await self.queue_job(1)
        await self.tick(self.ticks_per_hour())

        self.assertEqual(
            await get_user_quantity(self.db, USER, "iron"), BLAST_FURNACE_BATCH_SIZE
        )
        self.assertEqual((await self.job(job_id))["status"], "complete")

    async def test_nothing_is_credited_part_way_through_a_batch(self):
        """A batch is indivisible: the machine has either produced 100 items or
        it has not. Crediting a fraction of one would hand out items the
        player's ore has only partly paid for."""
        await self.queue_job(1)
        await self.tick(self.ticks_per_hour() - 1)
        self.assertEqual(await get_user_quantity(self.db, USER, "iron"), 0)

    async def test_progress_carries_between_ticks_rather_than_rounding_away(self):
        # A level 1 machine earns 1/12 of a batch per tick, which would round
        # to nothing every single tick without the accumulator.
        await self.queue_job(2)
        await self.tick(self.ticks_per_hour() * 2)
        self.assertEqual(
            await get_user_quantity(self.db, USER, "iron"), 2 * BLAST_FURNACE_BATCH_SIZE
        )

    async def test_a_levelled_machine_gets_through_more_batches_an_hour(self):
        await self.set_level(3)
        await self.queue_job(3)
        await self.tick(self.ticks_per_hour())
        self.assertEqual(
            await get_user_quantity(self.db, USER, "iron"), 3 * BLAST_FURNACE_BATCH_SIZE
        )

    async def test_a_partly_drained_job_keeps_its_remaining_batches(self):
        job_id = await self.queue_job(3)
        await self.tick(self.ticks_per_hour())
        row = await self.job(job_id)
        self.assertEqual(row["quantity"], 2)
        self.assertEqual(row["status"], "in_progress")

    async def test_jobs_are_worked_in_the_order_they_were_queued(self):
        first = await self.queue_job(1, target_id="iron")
        second = await self.queue_job(1, target_id="copper", user_id=OTHER_USER)
        await self.tick(self.ticks_per_hour())

        self.assertEqual((await self.job(first))["status"], "complete")
        self.assertEqual((await self.job(second))["status"], "queued")
        self.assertEqual(await get_user_quantity(self.db, OTHER_USER, "copper"), 0)

    async def test_a_completed_job_is_never_credited_twice(self):
        await self.queue_job(1)
        await self.tick(self.ticks_per_hour() * 3)
        self.assertEqual(
            await get_user_quantity(self.db, USER, "iron"), BLAST_FURNACE_BATCH_SIZE
        )

    async def test_an_idle_machine_banks_nothing_it_can_spend_later(self):
        """The accumulator carries fractions of a batch, but a job queued after
        a long idle stretch must not be finished instantly by progress the
        machine 'earned' while empty - it is capped at one batch of carry the
        same way the furnace's is, because int() takes whole batches out of the
        accumulator every tick whether or not there is work."""
        await self.tick(self.ticks_per_hour() * 5)
        job_id = await self.queue_job(2)
        await self.tick()
        self.assertEqual((await self.job(job_id))["quantity"], 2)


class BlastFurnaceQueueTests(BlastFurnaceTestCase):
    async def test_it_is_one_of_the_servers_machines(self):
        self.assertIn("blast_furnace", MACHINES)

    async def test_the_queue_cap_counts_batches_not_items(self):
        """server_config.blast_furnace_max_queue defaults to 5, and what that
        has to mean is five BATCHES - the same job measured in items would be
        500 and would never fit."""
        await self.queue_job(4)
        room = await queue_room(self.db, GUILD, USER, "blast_furnace", 1)
        self.assertEqual((room.queued, room.base, room.effective), (4, 5, 5))
        self.assertTrue(room.fits)
        self.assertFalse((await queue_room(self.db, GUILD, USER, "blast_furnace", 2)).fits)

    async def test_its_queue_is_separate_from_the_furnaces(self):
        # Both machines share production_jobs, so a bulk job must not eat into
        # anyone's ordinary smelting allowance or vice versa.
        await self.queue_job(5)
        room = await queue_room(self.db, GUILD, USER, "furnace", 25)
        self.assertEqual(room.queued, 0)
        self.assertTrue(room.fits)

    async def test_a_new_server_starts_on_the_configured_fee(self):
        row = await self.db.fetchone(
            "SELECT blast_furnace_fee, blast_furnace_level FROM server_config WHERE guild_id = ?",
            (GUILD,),
        )
        self.assertEqual(row["blast_furnace_fee"], config.DEFAULT_BLAST_FURNACE_FEE)
        self.assertEqual(row["blast_furnace_level"], 1)


if __name__ == "__main__":
    unittest.main()
