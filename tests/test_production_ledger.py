"""
Tests for the production ledger and /economy's figures (utils/production_ledger.py,
cogs/economy.py).

Like the rest of the suite these run against a throwaway SQLite database with
no Discord gateway - the ledger is deliberately a plain table plus pure
functions, so everything that decides a number can be exercised directly.

What is pinned here is the set of decisions documented in docs/market.md
section 5, because each of them is a judgement that reads as arbitrary from
the code alone and would be "tidied" back to something wrong:

  * value added is output MINUS input, and the input includes the furnace's
    fuel coal;
  * mined value belongs to the drill's guild, not the command's;
  * gemstones are counted but never summed;
  * GDP counts only the sources whose output the market actually prices.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

import utils.embeds as embeds
from cogs.blastfurnace import BlastFurnaceCog, PROCESS_TICK_MINUTES as BLAST_TICK_MINUTES
from cogs.economy import EconomyCog, GDP_SOURCE_LABEL, MACHINE_EMOJI
from cogs.furnace import (
    PROCESS_TICK_MINUTES as FURNACE_TICK_MINUTES,
    SERVER_JOB_USER_ID,
    FurnaceCog,
)
from data.materials import BLAST_FURNACE_BATCH_SIZE, GEMSTONES, SMELTED_MATERIALS
from database.db import Database
from utils.drills import retract_drill
from utils.db_helpers import (
    MACHINES,
    ProductionClock,
    bank_infrastructure_fee,
    ensure_server_row,
    ensure_user_row,
    slot_progress,
    mining_slot_status,
)
from utils.production_ledger import (
    GDP_SOURCES,
    LEDGER_SOURCES,
    gem_counts,
    market_value,
    record_mined,
    record_output,
    smelting_inputs,
    split_by_guild,
    tracked_since,
    utc_now,
    window_cutoff,
    window_totals,
)

GUILD = 4242
OTHER_GUILD = 9999
USER = 77

# One full day's window, which is what /economy's shorter figure uses.
DAY = 24


class _LedgerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_server_row(self.db, OTHER_GUILD)
        await ensure_user_row(self.db, USER)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def totals(self, guild_id=GUILD, hours=DAY):
        return await window_totals(self.db, guild_id, window_cutoff(hours))


class ValueAddedTests(_LedgerTestCase):
    """Value added is what a stage ADDED, not what came out of it."""

    async def test_a_smelt_credits_the_margin_not_the_bar(self):
        # Ten Iron Ore (0.01 each) plus the furnace's one fuel Coal (0.03)
        # becomes one Iron worth 0.15.
        async with self.db.transaction() as tx:
            await record_output(tx, GUILD, "furnace", "iron", 1, smelting_inputs("iron", 1))

        totals = await self.totals()
        self.assertAlmostEqual(totals.gdp, 0.02, places=6)
        # Not the bar's full value, which is what counting finished goods
        # instead would report - and not the recipe-only figure of 0.05, which
        # is what leaving the fuel coal out would report.
        self.assertNotAlmostEqual(totals.gdp, 0.15, places=6)
        self.assertNotAlmostEqual(totals.gdp, 0.05, places=6)

    async def test_the_fuel_coal_counts_as_input(self):
        """FURNACE_COAL_COST_PER_UNIT is really burned, so it is really an
        input. On Iron it is larger than the margin itself, so omitting it
        would more than double the figure."""
        for material_id in SMELTED_MATERIALS:
            with self.subTest(material=material_id):
                inputs = smelting_inputs(material_id, 1)
                recipe = SMELTED_MATERIALS[material_id]["inputs"]
                self.assertEqual(
                    inputs["coal"], recipe.get("coal", 0) + 1,
                    "one coal of fuel per item is missing from the recorded inputs",
                )

    async def test_the_three_recipes_add_what_the_design_doc_says(self):
        """docs/market.md section 5 quotes these three figures; this is where
        they come from, so a retune fails here rather than silently making the
        doc wrong."""
        expected = {"iron": 0.02, "copper": 0.07, "steel": 0.13}
        for material_id, added in expected.items():
            with self.subTest(material=material_id):
                value = market_value(material_id, 1)
                spent = sum(
                    market_value(input_id, qty)
                    for input_id, qty in smelting_inputs(material_id, 1).items()
                )
                self.assertAlmostEqual(value - spent, added, places=6)

    async def test_mining_has_no_input_so_adds_its_full_value(self):
        async with self.db.transaction() as tx:
            await record_mined(tx, GUILD, {"iron_ore": 10})
        totals = await self.totals()
        self.assertAlmostEqual(totals.gdp, 0.10, places=6)


class AttributionTests(_LedgerTestCase):
    """Mined value belongs to the pool the ore came out of."""

    async def test_mined_value_is_credited_to_the_drills_guild(self):
        async with self.db.transaction() as tx:
            await record_mined(tx, OTHER_GUILD, {"iron_ore": 100})

        # The guild the command would have been typed in gets nothing.
        self.assertAlmostEqual((await self.totals(GUILD)).gdp, 0.0, places=6)
        self.assertAlmostEqual((await self.totals(OTHER_GUILD)).gdp, 1.00, places=6)

    async def test_a_collect_spanning_two_servers_writes_rows_for_both(self):
        """The one most likely to be wrong. /collect empties a player's drills
        in every server at once, so one invocation has to split its haul
        between the guilds it actually came from."""
        # 300 items out of GUILD's pool and 100 out of OTHER_GUILD's, converted
        # as one haul the way /collect converts it.
        haul = {"iron_ore": 400}
        weights = {GUILD: 300, OTHER_GUILD: 100}
        split = split_by_guild(haul, weights)

        async with self.db.transaction() as tx:
            for guild_id, share in split.items():
                await record_mined(tx, guild_id, share)

        self.assertEqual(split[GUILD], {"iron_ore": 300})
        self.assertEqual(split[OTHER_GUILD], {"iron_ore": 100})
        self.assertAlmostEqual((await self.totals(GUILD)).gdp, 3.00, places=6)
        self.assertAlmostEqual((await self.totals(OTHER_GUILD)).gdp, 1.00, places=6)

    async def test_a_split_never_creates_or_destroys_items(self):
        """Largest remainder, so the parts sum to the whole however the
        weights divide. Plain rounding would let a server's GDP disagree with
        the ore that produced it."""
        for weights in (
            {1: 1, 2: 1, 3: 1},        # 7 does not divide by 3
            {1: 2, 2: 3, 3: 5},
            {1: 999, 2: 1},
            {1: 1},
        ):
            with self.subTest(weights=weights):
                split = split_by_guild({"coal": 7, "iron_ore": 13}, weights)
                for material_id, total in (("coal", 7), ("iron_ore", 13)):
                    self.assertEqual(
                        sum(share.get(material_id, 0) for share in split.values()), total
                    )

    async def test_a_single_server_collect_is_exact(self):
        """The approximation only ever bites across servers - which is what
        makes it acceptable, since all but the rarest collect covers one.

        Ores only: gemstones never reach this function, because the focus and
        the efficiency leave them alone and /collect attributes them straight
        to the drill they came out of."""
        split = split_by_guild({"coal": 7, "iron_ore": 55}, {GUILD: 55})
        self.assertEqual(split, {GUILD: {"coal": 7, "iron_ore": 55}})

    async def test_nothing_is_attributed_when_there_is_no_ore_to_weigh(self):
        """A haul of nothing but gemstones has no ore weight. Gems are
        attributed exactly elsewhere, so this must not divide by zero."""
        self.assertEqual(split_by_guild({"iron_ore": 5}, {GUILD: 0}), {})
        self.assertEqual(split_by_guild({}, {}), {})


class GemstoneTests(_LedgerTestCase):
    async def test_gemstones_are_excluded_from_gdp_and_counted_separately(self):
        async with self.db.transaction() as tx:
            await record_mined(tx, GUILD, {"iron_ore": 10, "ruby": 1, "diamond": 1})

        totals = await self.totals()
        # 10 iron ore at 0.01. The ruby (5,500) and diamond (500,000) would
        # each on their own dwarf every other figure the embed shows.
        self.assertAlmostEqual(totals.gdp, 0.10, places=6)

        counts = await gem_counts(self.db, GUILD, window_cutoff(DAY))
        self.assertEqual(counts, {"ruby": 1, "diamond": 1})

    async def test_a_pressed_gem_is_marked_as_one_too(self):
        """is_gemstone is decided by the material, not by the source, so a gem
        made at the press is excluded exactly as a mined one is."""
        async with self.db.transaction() as tx:
            await record_output(tx, GUILD, "press", "ruby", 1, {"iron": 600})
        counts = await gem_counts(self.db, GUILD, window_cutoff(DAY))
        self.assertEqual(counts, {"ruby": 1})
        self.assertAlmostEqual((await self.totals()).gdp, 0.0, places=6)

    async def test_every_gemstone_can_be_recorded(self):
        async with self.db.transaction() as tx:
            await record_mined(tx, GUILD, {material_id: 1 for material_id in GEMSTONES})
        counts = await gem_counts(self.db, GUILD, window_cutoff(DAY))
        self.assertEqual(set(counts), set(GEMSTONES))


class WindowTests(_LedgerTestCase):
    async def insert_at(self, occurred_at, output_value=1.0):
        await self.db.execute(
            "INSERT INTO production_ledger "
            "(guild_id, occurred_at, source, material_id, quantity, output_value, input_value) "
            "VALUES (?, ?, 'mining', 'iron_ore', 1, ?, 0)",
            (GUILD, occurred_at, output_value),
        )

    async def test_a_row_just_outside_the_window_is_excluded(self):
        now = utc_now()
        inside = (now - timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
        outside = (now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
        await self.insert_at(inside, 5.0)
        await self.insert_at(outside, 100.0)

        totals = await window_totals(self.db, GUILD, window_cutoff(DAY, now))
        self.assertAlmostEqual(totals.gdp, 5.0, places=6)
        self.assertEqual(totals.rows, 1)

    async def test_the_boundary_row_itself_is_inside(self):
        """The comparison is >=, so a row written at exactly the cutoff counts
        - a row can otherwise fall through the gap between two reads."""
        now = utc_now()
        cutoff = window_cutoff(DAY, now)
        await self.insert_at(cutoff, 3.0)
        self.assertAlmostEqual(
            (await window_totals(self.db, GUILD, cutoff)).gdp, 3.0, places=6
        )

    async def test_the_wider_window_contains_the_narrower_one(self):
        now = utc_now()
        await self.insert_at((now - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"), 7.0)
        day = await window_totals(self.db, GUILD, window_cutoff(DAY, now))
        week = await window_totals(self.db, GUILD, window_cutoff(24 * 7, now))
        self.assertAlmostEqual(day.gdp, 0.0, places=6)
        self.assertAlmostEqual(week.gdp, 7.0, places=6)

    async def test_tracked_since_reports_the_oldest_row(self):
        self.assertIsNone(await tracked_since(self.db, GUILD))
        await self.insert_at("2026-09-01 12:00:00")
        await self.insert_at("2026-09-05 12:00:00")
        self.assertEqual(await tracked_since(self.db, GUILD), "2026-09-01 12:00:00")


class SourceScopeTests(_LedgerTestCase):
    """Which sources GDP counts, and what the rest are recorded for."""

    async def test_gdp_counts_only_sources_whose_output_the_market_prices(self):
        self.assertEqual(GDP_SOURCES, ("mining", "furnace", "blast_furnace"))
        for source in GDP_SOURCES:
            self.assertIn(source, LEDGER_SOURCES)

    async def test_every_machine_can_be_recorded_even_when_it_is_not_in_gdp(self):
        self.assertEqual(set(LEDGER_SOURCES), {"mining", *MACHINES})

    async def test_a_factory_craft_is_consumption_not_gdp(self):
        """Twelve Copper worth 3.60 becomes a Wiring the market will not
        price. It moves the import/export comparison and not the headline."""
        async with self.db.transaction() as tx:
            await record_output(tx, GUILD, "factory", "wiring", 1, {"copper": 12})
        totals = await self.totals()
        self.assertAlmostEqual(totals.gdp, 0.0, places=6)
        self.assertAlmostEqual(totals.machine_input, 3.60, places=6)

    async def test_the_scrapper_does_not_look_like_it_creates_goods(self):
        """Its output is priced and its input is not, so summing it as value
        added would report recycling as production. Keeping it out of
        GDP_SOURCES is what stops that."""
        async with self.db.transaction() as tx:
            await record_output(tx, GUILD, "scrapper", "copper", 6)
        self.assertAlmostEqual((await self.totals()).gdp, 0.0, places=6)

    async def test_an_unknown_source_is_refused(self):
        async with self.db.transaction() as tx:
            with self.assertRaises(ValueError):
                await record_output(tx, GUILD, "smithy", "iron", 1)

    async def test_the_import_export_comparison_reads_both_sides(self):
        async with self.db.transaction() as tx:
            await record_mined(tx, GUILD, {"iron_ore": 100})           # 1.00 out
            await record_output(tx, GUILD, "furnace", "iron", 10,
                                smelting_inputs("iron", 10))           # 1.30 in
        totals = await self.totals()
        self.assertAlmostEqual(totals.mined_output, 1.00, places=6)
        self.assertAlmostEqual(totals.machine_input, 1.30, places=6)
        self.assertGreater(totals.machine_input, totals.mined_output)


class EmptyServerTests(_LedgerTestCase):
    """A server with no ledger rows at all, which is EVERY server on the day
    this ships."""

    async def test_the_totals_are_zero_rather_than_an_error(self):
        totals = await self.totals()
        self.assertEqual(
            (totals.gdp, totals.mined_output, totals.machine_input, totals.rows),
            (0.0, 0.0, 0.0, 0),
        )
        self.assertEqual(totals.added_by_source, {})

    async def test_the_gdp_field_says_so_rather_than_showing_a_blank(self):
        cog = EconomyCog.__new__(EconomyCog)
        totals = await self.totals()
        value = cog._gdp_windows_value(totals, totals, None, None)
        self.assertTrue(value.strip())
        self.assertIn("Nothing recorded yet", value)

    async def test_the_value_breakdown_is_omitted_rather_than_empty(self):
        """add_multi_field would otherwise render a field reading "None",
        which is worse than not showing the field at all."""
        cog = EconomyCog.__new__(EconomyCog)
        self.assertEqual(cog._value_breakdown_lines(await self.totals(), None), [])


class SlotProgressTests(_LedgerTestCase):
    """The figure /economy status quotes as "Mining slot progress"
    (utils/db_helpers.py: slot_progress)."""

    async def test_a_server_that_has_paid_nothing_has_collected_nothing(self):
        cfg = await self.db.fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        self.assertEqual(slot_progress(cfg), 0.0)

    async def test_it_sums_every_machine_rather_than_a_hardcoded_five(self):
        """A sixth machine's fees have to land in this figure by being in
        MACHINES, the same way they land in the mining slot total."""
        fees = {machine: i + 1 for i, machine in enumerate(MACHINES)}
        for machine, fee in fees.items():
            await bank_infrastructure_fee(self.db, GUILD, machine, fee)

        cfg = await self.db.fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        self.assertAlmostEqual(slot_progress(cfg), sum(fees.values()), places=6)

    async def test_mining_slots_are_bought_with_this_same_figure(self):
        """Not a coincidence of both adding the same columns: mining_slot_status
        calls slot_progress, so the progress /economy status shows and the cap
        the slot ladder derives can never be two different numbers."""
        await bank_infrastructure_fee(self.db, GUILD, "furnace", 2.50)
        await bank_infrastructure_fee(self.db, GUILD, "press", 7.50)

        cfg = await self.db.fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        slots = await mining_slot_status(self.db, GUILD)
        self.assertAlmostEqual(slot_progress(cfg), slots.progress, places=6)

    async def test_what_the_government_buys_for_slots_counts_too(self):
        """mining_slot_credit is the one column in the total that is not a
        machine's fees (1.4) - what the Mayor has spent toward slots."""
        await bank_infrastructure_fee(self.db, GUILD, "furnace", 2.50)
        await self.db.execute(
            "UPDATE server_config SET mining_slot_credit = 10.0 WHERE guild_id = ?", (GUILD,)
        )
        cfg = await self.db.fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        self.assertAlmostEqual(slot_progress(cfg), 12.50, places=6)
        slots = await mining_slot_status(self.db, GUILD)
        self.assertAlmostEqual(slots.progress, 12.50, places=6)


class EmbedTests(_LedgerTestCase):
    async def test_economy_uses_the_market_colour_and_adds_no_new_one(self):
        """docs/stylization.md: /economy shares the market's yellow rather than
        claiming a colour of its own, so the palette is unchanged."""
        palette = {
            name for name, value in vars(embeds).items()
            if name.endswith("_COLOR") and isinstance(value, discord.Color)
        }
        self.assertNotIn("ECONOMY_COLOR", palette)
        self.assertEqual(embeds.MARKET_COLOR, discord.Color(0xFFE600))

    async def test_every_machine_has_an_icon_and_every_gdp_source_a_label(self):
        """A sixth machine turning up with no icon is what this catches."""
        self.assertEqual(set(MACHINE_EMOJI), set(MACHINES))
        self.assertEqual(set(GDP_SOURCE_LABEL), set(GDP_SOURCES))

    async def test_reading_the_economy_changes_no_balance_stock_or_fee(self):
        """/economy is read-only against everything that is anybody's
        property. Its one write is the job board's own lazy posting, which is
        what /jobboard does too."""
        await self.db.execute(
            "INSERT INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, ?)",
            (GUILD, USER, 12.34),
        )
        await self.db.execute(
            "INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)",
            (GUILD, "iron_ore", 500),
        )
        before = {
            "balance": (await self.db.fetchone(
                "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
                (GUILD, USER),
            ))["balance"],
            "stock": (await self.db.fetchone(
                "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
                (GUILD, "iron_ore"),
            ))["quantity"],
            "config": dict(await self.db.fetchone(
                "SELECT * FROM server_config WHERE guild_id = ?", (GUILD,)
            )),
        }

        # Everything /economy reads, run the way it runs it.
        await window_totals(self.db, GUILD, window_cutoff(DAY))
        await gem_counts(self.db, GUILD, window_cutoff(DAY))
        await tracked_since(self.db, GUILD)

        after_config = dict(await self.db.fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (GUILD,)
        ))
        self.assertEqual(before["config"], after_config)
        for machine in MACHINES:
            self.assertEqual(after_config[f"{machine}_fees_collected"], 0.0)
        self.assertEqual(
            (await self.db.fetchone(
                "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
                (GUILD, USER),
            ))["balance"],
            before["balance"],
        )
        self.assertEqual(
            (await self.db.fetchone(
                "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
                (GUILD, "iron_ore"),
            ))["quantity"],
            before["stock"],
        )


class _NoGuildBot:
    """Enough of a bot for the furnace's drain loop. Once its queue empties it
    considers an auto-smelt, which needs a real guild to read a member count
    from - answering None makes it decline, which is all these tests want."""

    def get_guild(self, guild_id):
        return None


class WritePathTests(_LedgerTestCase):
    """The real code paths, not just the arithmetic - a rule that is right in
    a helper and never called from the cog is a rule that does nothing."""

    def clock(self, tick_minutes):
        """A clock the loop is driven on a tick at a time (tick_loop). It
        starts a minute ahead of real time so a job inserted here, stamped by
        SQLite's real clock, reads as queued before the first tick."""
        self.now = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.tick_minutes = tick_minutes
        return ProductionClock(tick_minutes, now=lambda: self.now)

    async def tick_loop(self, cog_class, cog, times):
        for _ in range(times):
            self.now += timedelta(minutes=self.tick_minutes)
            await cog_class.process_loop.coro(cog)

    def furnace_cog(self):
        cog = FurnaceCog.__new__(FurnaceCog)
        cog.db = self.db
        cog._production = self.clock(FURNACE_TICK_MINUTES)
        cog.bot = _NoGuildBot()
        return cog

    async def place_drill(self, guild_id, contents: dict[str, int]):
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, stored_amount) "
            "VALUES (?, ?, 'iron_drill', ?)",
            (guild_id, USER, sum(contents.values())),
        )
        for material_id, quantity in contents.items():
            await self.db.execute(
                "INSERT INTO drill_contents (drill_id, material_id, quantity) VALUES (?, ?, ?)",
                (drill_id, material_id, quantity),
            )
        return drill_id

    async def test_retracting_a_drill_credits_the_drills_own_server(self):
        """/mine remove and the sweep that empties a server the bot was
        removed from both go through retract_drill, so the ledger row has to
        be written there rather than in either command."""
        drill_id = await self.place_drill(OTHER_GUILD, {"iron_ore": 200})
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))
            self.assertIsNotNone(await retract_drill(tx, row))

        self.assertAlmostEqual((await self.totals(GUILD)).gdp, 0.0, places=6)
        self.assertAlmostEqual((await self.totals(OTHER_GUILD)).gdp, 2.00, places=6)

    async def test_a_drill_that_was_already_emptied_records_nothing(self):
        """retract_drill returns None when a racing /collect got there first,
        and a ledger row for a haul nobody received would be double counting
        the same ore."""
        drill_id = await self.place_drill(GUILD, {"iron_ore": 100})
        # The row as a racing caller would have read it, before the /collect
        # that beat it to the haul - retract_drill guards on stored_amount, so
        # this is exactly the state that has to record nothing.
        row = await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))
        await self.db.execute(
            "UPDATE drills SET stored_amount = 0 WHERE drill_id = ?", (drill_id,)
        )
        async with self.db.transaction() as tx:
            self.assertIsNone(await retract_drill(tx, row))

        self.assertEqual((await self.totals()).rows, 0)

    async def test_the_furnace_loop_records_what_it_smelted(self):
        cog = self.furnace_cog()
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'furnace', 'iron', 1)",
            (GUILD, USER),
        )
        # A level 1 furnace does five items an hour, so one hour of ticks is
        # more than enough for a single Iron.
        await self.tick_loop(FurnaceCog, cog, 12)

        totals = await self.totals()
        self.assertAlmostEqual(totals.gdp, 0.02, places=6)
        self.assertAlmostEqual(totals.added_by_source["furnace"], 0.02, places=6)

    async def test_the_servers_own_auto_smelt_is_production_too(self):
        """The market processing its own surplus is still output. The ledger
        has no opinion about who owned the job, so SERVER_JOB_USER_ID needs no
        special case."""
        cog = self.furnace_cog()
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'furnace', 'iron', 1)",
            (GUILD, SERVER_JOB_USER_ID),
        )
        await self.tick_loop(FurnaceCog, cog, 12)

        self.assertAlmostEqual((await self.totals()).gdp, 0.02, places=6)

    async def test_the_blast_furnace_records_items_not_batches(self):
        """Its production_jobs rows count batches of BLAST_FURNACE_BATCH_SIZE.
        Recording that number would understate the server's output by a factor
        of a hundred."""
        cog = BlastFurnaceCog.__new__(BlastFurnaceCog)
        cog.db = self.db
        cog._production = self.clock(BLAST_TICK_MINUTES)
        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
            "VALUES (?, ?, 'blast_furnace', 'iron', 1)",
            (GUILD, USER),
        )
        # A level 1 blast furnace does one batch an hour.
        await self.tick_loop(BlastFurnaceCog, cog, 12)

        row = await self.db.fetchone(
            "SELECT material_id, quantity FROM production_ledger "
            "WHERE guild_id = ? AND source = 'blast_furnace'",
            (GUILD,),
        )
        self.assertEqual(row["quantity"], BLAST_FURNACE_BATCH_SIZE)
        # One batch of Iron adds a hundred times what one Iron adds, which is
        # the whole identity the blast furnace is built on.
        self.assertAlmostEqual(
            (await self.totals()).gdp, 0.02 * BLAST_FURNACE_BATCH_SIZE, places=6
        )


if __name__ == "__main__":
    unittest.main()
