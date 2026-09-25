"""
Tests for the server government (utils/government.py, docs/government.md)
against a real database.

SupplyTests is the class that matters. Everything else pins a rule a player
would notice; that one pins the promise the design rests on - that the
government changes WHEN fees are burned, not how much, apart from the bond
premium - and it is the failure that would be silent.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord import app_commands

from cogs.government import GovernmentCog
from database.db import Database
from data.materials import (
    BONANZA_MINIMUM_PRICE,
    ENHANCEMENT_PRICE_BASE,
    ENHANCEMENT_PRICE_STEP,
    furnace_rate,
    mining_slot_threshold,
)
from utils.db_helpers import (
    adjust_currency_balance,
    circulating_currency_for,
    ensure_server_row,
    get_currency_balance,
    machine_fee_rate,
    machine_speed_level,
    mining_slot_status,
    sqlite_timestamp,
)
from utils.government import (
    MAYOR,
    TREASURER,
    GovernmentError,
    announce_voting,
    bonanza_quote,
    bond_owed_cents,
    buy_bond,
    buy_enhancement,
    can_vote,
    cast_vote,
    charge_machine_fee,
    count_election,
    debt_cap_cents,
    decide,
    due_voting_day,
    fund_machine,
    fund_mining_slots,
    member_left,
    member_returned,
    open_bond_sale,
    outstanding_debt_cents,
    pay_bondholders,
    set_bond_rate,
    set_fee_multiplier,
    set_tax,
    start_bonanza,
    tax_collected,
)

GUILD = 7070
MAYOR_ID = 101
TREASURER_ID = 102
ALICE = 111
BOB = 222
CARA = 333
DAVE = 444

# Thursday 2026-09-24, noon and one in the afternoon on the game clock
# (America/Phoenix, UTC-7 all year), and the Friday after it.
THURSDAY = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)
FRIDAY = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)
WEDNESDAY = datetime(2026, 9, 23, 19, 0, tzinfo=timezone.utc)

STARTING_BALANCE = 1_000.0


class _GovernmentTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        for user in (MAYOR_ID, TREASURER_ID, ALICE, BOB, CARA, DAVE):
            await adjust_currency_balance(self.db, GUILD, user, STARTING_BALANCE)
        await self.db.execute(
            "UPDATE server_config SET mayor_id = ?, treasurer_id = ? WHERE guild_id = ?",
            (MAYOR_ID, TREASURER_ID, GUILD),
        )

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def cfg(self):
        return await self.db.fetchone("SELECT * FROM server_config WHERE guild_id = ?", (GUILD,))

    async def set(self, **columns):
        assignments = ", ".join(f"{column} = ?" for column in columns)
        await self.db.execute(
            f"UPDATE server_config SET {assignments} WHERE guild_id = ?",
            (*columns.values(), GUILD),
        )

    async def fee(self, amount, user=ALICE, machine="furnace", now=None):
        async with self.db.transaction() as tx:
            await charge_machine_fee(tx, GUILD, user, machine, amount, now)

    async def record_tax_days_ago(self, amount, days, now=THURSDAY):
        """Tax collected `days` game days before `now`, without the fee."""
        day = (now.astimezone(timezone(timedelta(hours=-7))).date() - timedelta(days=days)).isoformat()
        await self.db.execute(
            "INSERT INTO government_tax_daily (guild_id, day, amount) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, day) DO UPDATE SET amount = amount + excluded.amount",
            (GUILD, day, amount),
        )

    async def open_sale(self, cents, cap=10_000.0, now=THURSDAY):
        """Room for the sale under the debt cap, then the sale."""
        await self.record_tax_days_ago(cap, 1, now)
        async with self.db.transaction() as tx:
            await open_bond_sale(tx, GUILD, MAYOR_ID, cents, now)

    async def buy(self, user, cents, now=THURSDAY):
        async with self.db.transaction() as tx:
            return await buy_bond(tx, GUILD, user, cents, now)

    async def place_drill(self, owner, placed_at):
        await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, placed_at) VALUES (?, ?, 'iron_drill', ?)",
            (GUILD, owner, sqlite_timestamp(placed_at)),
        )


class FeeTaxTests(_GovernmentTestCase):
    async def test_an_untaxed_fee_is_burned_and_banked_whole(self):
        await self.fee(2.0)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["currency_burned_total"], 2.0)
        self.assertAlmostEqual(cfg["furnace_fees_collected"], 2.0)
        self.assertEqual(cfg["treasury"], 0.0)

    async def test_the_taxed_share_is_held_not_burned_and_levels_nothing(self):
        await self.set(tax_percent=25)
        await self.fee(4.0, now=THURSDAY)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["currency_burned_total"], 3.0)
        self.assertAlmostEqual(cfg["furnace_fees_collected"], 3.0)
        self.assertAlmostEqual(cfg["treasury"], 1.0)
        self.assertAlmostEqual(await tax_collected(self.db, GUILD, 1, THURSDAY + timedelta(days=1)), 1.0)

    async def test_a_full_tax_burns_and_banks_exactly_nothing(self):
        await self.set(tax_percent=100)
        await self.fee(0.01)
        cfg = await self.cfg()
        self.assertEqual(cfg["currency_burned_total"], 0.0)
        self.assertEqual(cfg["furnace_fees_collected"], 0.0)
        self.assertAlmostEqual(cfg["treasury"], 0.01)

    async def test_held_tax_still_counts_as_circulating(self):
        before = await circulating_currency_for(self.db, GUILD)
        await self.set(tax_percent=50)
        await self.fee(10.0)
        after = await circulating_currency_for(self.db, GUILD)
        # Only the burned half has left the economy.
        self.assertAlmostEqual(before - after, 5.0)

    async def test_an_unaffordable_fee_takes_nothing(self):
        await self.set(tax_percent=50)
        with self.assertRaises(Exception):
            await self.fee(STARTING_BALANCE + 1)
        cfg = await self.cfg()
        self.assertEqual(cfg["treasury"], 0.0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE)

    async def test_the_fee_is_the_default_times_the_multiplier(self):
        default = await machine_fee_rate(self.db, GUILD, "press")
        await self.set(press_fee_multiplier=4.0)
        self.assertAlmostEqual(await machine_fee_rate(self.db, GUILD, "press"), default * 4)


class SupplyTests(_GovernmentTestCase):
    """The government moves WHEN fees are burned, not how much - except the
    bond premium, which is exactly what it leaks."""

    async def run_cycle(self, rate_percent):
        await self.set(tax_percent=100, bond_rate_percent=rate_percent)
        start = await circulating_currency_for(self.db, GUILD)
        await self.open_sale(10_000)
        await self.buy(BOB, 5_000)                     # lends 100.00
        await self.buy(BOB, 5_000)
        async with self.db.transaction() as tx:        # ...which the Mayor burns
            await fund_machine(tx, GUILD, MAYOR_ID, "factory", 100.0)
        for _ in range(4):                             # 200.00 of fees, all taxed
            await self.fee(50.0)
        async with self.db.transaction() as tx:
            await pay_bondholders(tx, GUILD)
        cfg = await self.cfg()
        async with self.db.transaction() as tx:        # the rest of the tax, burned
            await fund_machine(tx, GUILD, MAYOR_ID, "furnace", cfg["treasury"])
        return start, await circulating_currency_for(self.db, GUILD), await self.cfg()

    async def test_with_no_premium_the_cycle_burns_exactly_the_fees(self):
        start, end, cfg = await self.run_cycle(0)
        self.assertAlmostEqual(start - end, 200.0, places=6)
        self.assertAlmostEqual(cfg["currency_burned_total"], 200.0, places=6)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE, places=6)
        self.assertEqual(await outstanding_debt_cents(self.db, GUILD), 0)
        self.assertAlmostEqual(cfg["treasury"], 0.0, places=6)
        self.assertAlmostEqual(cfg["repayment_pool"], 0.0, places=6)

    async def test_the_premium_is_the_whole_leak(self):
        start, end, cfg = await self.run_cycle(5)
        self.assertAlmostEqual(start - end, 195.0, places=6)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE + 5.0, places=6)


class TreasurerTests(_GovernmentTestCase):
    async def test_only_the_treasurer_may_change_settings(self):
        for actor in (MAYOR_ID, ALICE):
            with self.subTest(actor=actor):
                with self.assertRaises(GovernmentError):
                    async with self.db.transaction() as tx:
                        await set_tax(tx, GUILD, actor, 10, THURSDAY)

    async def test_the_multiplier_must_be_one_of_the_steps(self):
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await set_fee_multiplier(tx, GUILD, TREASURER_ID, "furnace", 3.0, THURSDAY)

    async def test_each_setting_changes_once_per_game_day(self):
        async with self.db.transaction() as tx:
            await set_fee_multiplier(tx, GUILD, TREASURER_ID, "furnace", 2.0, THURSDAY)
        # A different machine is a different setting.
        async with self.db.transaction() as tx:
            await set_fee_multiplier(tx, GUILD, TREASURER_ID, "factory", 0.5, THURSDAY)
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await set_fee_multiplier(tx, GUILD, TREASURER_ID, "furnace", 4.0, THURSDAY + timedelta(hours=1))
        async with self.db.transaction() as tx:
            await set_fee_multiplier(tx, GUILD, TREASURER_ID, "furnace", 4.0, FRIDAY)
        self.assertEqual((await self.cfg())["furnace_fee_multiplier"], 4.0)

    async def test_the_game_day_turns_at_phoenix_midnight_not_utc(self):
        # 06:59 UTC Friday is still Thursday in Phoenix.
        async with self.db.transaction() as tx:
            await set_tax(tx, GUILD, TREASURER_ID, 10, THURSDAY)
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await set_tax(tx, GUILD, TREASURER_ID, 20, datetime(2026, 9, 25, 6, 59, tzinfo=timezone.utc))
        async with self.db.transaction() as tx:
            await set_tax(tx, GUILD, TREASURER_ID, 20, datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc))

    async def test_tax_cannot_drop_below_the_rate_bonds_were_sold_at(self):
        await self.set(tax_percent=40)
        await self.open_sale(500)
        await self.buy(BOB, 500)
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await set_tax(tx, GUILD, TREASURER_ID, 39, THURSDAY)
        async with self.db.transaction() as tx:
            await set_tax(tx, GUILD, TREASURER_ID, 60, THURSDAY)

    async def test_the_bond_rate_is_capped(self):
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await set_bond_rate(tx, GUILD, TREASURER_ID, 6, THURSDAY)


class BondTests(_GovernmentTestCase):
    async def test_nothing_can_be_bought_without_a_sale(self):
        await self.record_tax_days_ago(100.0, 1)
        with self.assertRaises(GovernmentError):
            await self.buy(BOB, 100)

    async def test_the_cap_is_the_previous_seven_days_of_tax(self):
        await self.record_tax_days_ago(3.0, 1)
        await self.record_tax_days_ago(4.0, 7)
        await self.record_tax_days_ago(99.0, 8)   # too old
        await self.record_tax_days_ago(99.0, 0)   # today, not over yet
        self.assertEqual(await debt_cap_cents(self.db, GUILD, THURSDAY), 700)

    async def test_a_sale_or_a_purchase_past_the_cap_is_refused(self):
        await self.set(bond_rate_percent=5)
        await self.record_tax_days_ago(10.0, 1)
        with self.assertRaises(GovernmentError):   # 10.00 owes 10.50
            async with self.db.transaction() as tx:
                await open_bond_sale(tx, GUILD, MAYOR_ID, 1_000, THURSDAY)
        async with self.db.transaction() as tx:
            await open_bond_sale(tx, GUILD, MAYOR_ID, 500, THURSDAY)
        await self.buy(BOB, 500)                    # owes 5.25
        # The cap at purchase is what's enforced: the sale is fine, but a
        # second 5.00 would take the debt to 10.50.
        async with self.db.transaction() as tx:
            await open_bond_sale(tx, GUILD, MAYOR_ID, 0, THURSDAY)
        await self.set(bond_sale_cents=500)
        with self.assertRaises(GovernmentError):
            await self.buy(CARA, 500)

    async def test_a_bond_is_priced_and_recorded_at_sale(self):
        await self.set(bond_rate_percent=5, tax_percent=20)
        await self.open_sale(1_000)
        bond = await self.buy(BOB, 1_000)
        self.assertEqual(bond.owed_cents, 1_050)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE - 10.0)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["treasury"], 10.0)
        self.assertEqual(cfg["bond_sale_cents"], 0)
        # A later rate change is for later bonds.
        async with self.db.transaction() as tx:
            await set_bond_rate(tx, GUILD, TREASURER_ID, 0, THURSDAY)
        row = await self.db.fetchone("SELECT * FROM government_bonds WHERE bond_id = ?", (bond.bond_id,))
        self.assertEqual(row["remaining_cents"], 1_050)
        self.assertEqual(row["tax_percent_at_sale"], 20)

    async def test_repayment_is_pro_rata_on_what_is_still_owed(self):
        # The design doc's worked split: 105.00 and 52.50 owed, a 10.00 pool.
        for holder, owed in ((BOB, 10_500), (CARA, 5_250)):
            await self.db.execute(
                "INSERT INTO government_bonds (guild_id, holder_id, principal_cents, rate_percent, "
                "owed_cents, remaining_cents, tax_percent_at_sale) VALUES (?, ?, ?, 5, ?, ?, 0)",
                (GUILD, holder, owed * 100 // 105, owed, owed),
            )
        await self.set(repayment_pool=10.0)
        async with self.db.transaction() as tx:
            shares = await pay_bondholders(tx, GUILD)
        self.assertEqual(sorted(shares.values()), [333, 667])
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE + 6.67)
        self.assertAlmostEqual((await self.cfg())["repayment_pool"], 0.0)

    async def test_while_owed_all_tax_repays_and_after_it_the_treasury_gets_it(self):
        await self.set(tax_percent=100)
        await self.open_sale(100)
        await self.buy(BOB, 100)
        await self.fee(0.6)
        self.assertAlmostEqual((await self.cfg())["repayment_pool"], 0.6)
        await self.fee(0.6)
        async with self.db.transaction() as tx:
            await pay_bondholders(tx, GUILD)
        cfg = await self.cfg()
        # 1.00 owed and repaid; the 0.20 over it is the treasury's.
        self.assertEqual(await outstanding_debt_cents(self.db, GUILD), 0)
        self.assertAlmostEqual(cfg["repayment_pool"], 0.0)
        self.assertAlmostEqual(cfg["treasury"], 1.0 + 0.2)   # the bond's 1.00, then the excess
        await self.fee(0.5)
        self.assertAlmostEqual((await self.cfg())["treasury"], 1.7)
        repaid = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM user_notifications WHERE user_id = ? AND notice_key LIKE 'bond_repaid:%'",
            (BOB,),
        )
        self.assertEqual(repaid["n"], 1)

    async def test_a_fraction_of_a_cent_waits_for_the_next_payout(self):
        await self.open_sale(100)
        await self.buy(BOB, 100)
        await self.set(repayment_pool=0.005)
        async with self.db.transaction() as tx:
            self.assertEqual(await pay_bondholders(tx, GUILD), {})
        self.assertAlmostEqual((await self.cfg())["repayment_pool"], 0.005)

    async def test_a_departed_creditor_is_frozen_skipped_and_restored(self):
        await self.set(tax_percent=100)
        await self.open_sale(200)
        await self.buy(BOB, 100)
        await self.buy(CARA, 100)
        async with self.db.transaction() as tx:
            await member_left(tx, GUILD, CARA)
        # The cap and the payout both ignore her; the debt is not voided.
        self.assertEqual(await outstanding_debt_cents(self.db, GUILD), 100)
        await self.fee(2.0)
        async with self.db.transaction() as tx:
            await pay_bondholders(tx, GUILD)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, CARA), STARTING_BALANCE - 1.0)
        # Bob is repaid, and with nobody active left owed, tax goes to the treasury.
        await self.fee(1.0)
        self.assertAlmostEqual((await self.cfg())["repayment_pool"], 0.0)
        async with self.db.transaction() as tx:
            await member_returned(tx, GUILD, CARA)
        await self.fee(1.0)
        async with self.db.transaction() as tx:
            await pay_bondholders(tx, GUILD)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, CARA), STARTING_BALANCE)

    async def test_owed_is_the_principal_plus_a_whole_cent_premium(self):
        self.assertEqual(bond_owed_cents(5_000, 5), 5_250)
        self.assertEqual(bond_owed_cents(100, 0), 100)


class ElectionTests(_GovernmentTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Everyone has a drill that has been down for over a week.
        for user in (MAYOR_ID, TREASURER_ID, ALICE, BOB, CARA, DAVE):
            await self.place_drill(user, THURSDAY - timedelta(days=10))

    async def vote(self, office, voter, candidate, now=THURSDAY, **kwargs):
        async with self.db.transaction() as tx:
            await cast_vote(tx, GUILD, office, voter, candidate, now=now, **kwargs)

    async def count(self, now=FRIDAY, is_member=None):
        return await count_election(self.db, GUILD, now, is_member)

    async def refused(self, *args, **kwargs):
        with self.assertRaises(GovernmentError):
            await self.vote(*args, **kwargs)

    async def test_voting_is_thursdays_only(self):
        await self.refused(MAYOR, ALICE, BOB, now=WEDNESDAY)
        await self.refused(MAYOR, ALICE, BOB, now=FRIDAY)

    async def test_you_cannot_vote_for_yourself_or_a_bot(self):
        await self.refused(MAYOR, ALICE, ALICE)
        await self.refused(MAYOR, ALICE, BOB, candidate_is_bot=True)

    async def test_a_voter_needs_a_drill_placed_for_a_week(self):
        voter = 555
        await adjust_currency_balance(self.db, GUILD, voter, 1.0)
        await self.refused(MAYOR, voter, BOB)
        # Six days before voting opened is not enough; seven is.
        opened = datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc)
        await self.place_drill(voter, opened - timedelta(days=6))
        await self.refused(MAYOR, voter, BOB)
        await self.place_drill(voter, opened - timedelta(days=7))
        await self.vote(MAYOR, voter, BOB)

    async def test_a_candidate_must_have_played_here(self):
        await self.refused(MAYOR, ALICE, 9999)

    async def test_the_latest_vote_counts(self):
        await self.vote(MAYOR, ALICE, BOB)
        await self.vote(MAYOR, ALICE, CARA)
        result = await self.count()
        self.assertEqual(result.mayor, CARA)

    async def test_most_votes_win_and_the_votes_are_spent(self):
        await self.vote(MAYOR, ALICE, BOB)
        await self.vote(MAYOR, CARA, BOB)
        await self.vote(MAYOR, DAVE, CARA)
        self.assertEqual((await self.count()).mayor, BOB)
        self.assertIsNone(await self.count())
        left = await self.db.fetchone("SELECT COUNT(*) AS n FROM government_votes")
        self.assertEqual(left["n"], 0)

    async def test_nothing_is_counted_until_voting_day_is_over(self):
        await self.vote(MAYOR, ALICE, BOB)
        self.assertIsNone(await self.count(now=THURSDAY + timedelta(hours=1)))

    async def test_a_tie_goes_to_the_incumbent(self):
        await self.vote(MAYOR, ALICE, BOB, now=THURSDAY)
        await self.vote(MAYOR, CARA, MAYOR_ID, now=THURSDAY + timedelta(hours=1))
        self.assertEqual((await self.count()).mayor, MAYOR_ID)

    async def test_between_challengers_a_tie_goes_to_whoever_got_there_first(self):
        await self.vote(MAYOR, ALICE, CARA, now=THURSDAY + timedelta(hours=1))
        await self.vote(MAYOR, DAVE, BOB, now=THURSDAY)
        self.assertEqual((await self.count()).mayor, BOB)

    async def test_an_office_nobody_votes_on_keeps_its_holder(self):
        await self.vote(TREASURER, ALICE, BOB)
        result = await self.count()
        self.assertEqual(result.mayor, MAYOR_ID)
        self.assertEqual(result.treasurer, BOB)

    async def test_the_treasurer_ballot_skips_the_new_mayor(self):
        # Q18: the sitting Treasurer wins Mayor; the next candidate takes the seat.
        await self.vote(MAYOR, ALICE, TREASURER_ID)
        await self.vote(TREASURER, ALICE, TREASURER_ID)
        await self.vote(TREASURER, BOB, TREASURER_ID)
        await self.vote(TREASURER, CARA, DAVE)
        result = await self.count()
        self.assertEqual((result.mayor, result.treasurer), (TREASURER_ID, DAVE))

    def test_the_seating_rules(self):
        # Q17: only the new Mayor on the Treasurer ballot - the sitting
        # Treasurer stays, unless the sitting Treasurer IS the new Mayor.
        self.assertEqual(decide([ALICE], [ALICE], MAYOR_ID, TREASURER_ID), (ALICE, TREASURER_ID))
        self.assertEqual(decide([TREASURER_ID], [TREASURER_ID], MAYOR_ID, TREASURER_ID), (TREASURER_ID, None))
        self.assertEqual(decide([TREASURER_ID], [], MAYOR_ID, TREASURER_ID), (TREASURER_ID, None))
        self.assertEqual(decide([], [], MAYOR_ID, TREASURER_ID), (MAYOR_ID, TREASURER_ID))
        # The sitting Mayor, unchallenged, can't also be elected Treasurer.
        self.assertEqual(decide([], [MAYOR_ID, BOB], MAYOR_ID, TREASURER_ID), (MAYOR_ID, BOB))

    async def test_a_winner_who_has_left_is_skipped(self):
        await self.vote(MAYOR, ALICE, BOB)
        await self.vote(MAYOR, CARA, BOB)
        await self.vote(MAYOR, DAVE, CARA)

        async def is_member(user_id):
            return user_id != BOB

        self.assertEqual((await self.count(is_member=is_member)).mayor, CARA)

    async def test_a_new_mayor_ends_the_old_mayors_bond_sale(self):
        await self.set(bond_sale_cents=500)
        await self.vote(MAYOR, ALICE, BOB)
        await self.count()
        self.assertEqual((await self.cfg())["bond_sale_cents"], 0)

    async def test_leaving_vacates_an_office_and_withdraws_votes(self):
        await self.vote(MAYOR, ALICE, BOB)
        await self.vote(MAYOR, BOB, CARA)
        async with self.db.transaction() as tx:
            vacated = await member_left(tx, GUILD, MAYOR_ID)
        self.assertEqual(vacated, [MAYOR])
        self.assertIsNone((await self.cfg())["mayor_id"])
        async with self.db.transaction() as tx:
            await member_left(tx, GUILD, BOB)
        # BOB's own vote and every vote for him are gone; CARA is left unopposed.
        self.assertIsNone(await self.count())

    async def test_voting_is_announced_once_per_thursday(self):
        async with self.db.transaction() as tx:
            self.assertFalse(await announce_voting(tx, GUILD, WEDNESDAY))
            self.assertTrue(await announce_voting(tx, GUILD, THURSDAY))
            self.assertFalse(await announce_voting(tx, GUILD, THURSDAY + timedelta(hours=3)))

    def test_the_due_voting_day_is_the_last_one_that_has_closed(self):
        self.assertEqual(due_voting_day(THURSDAY), "2026-09-17")
        self.assertEqual(due_voting_day(FRIDAY), "2026-09-24")


class ProjectTests(_GovernmentTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set(treasury=100_000.0)

    async def test_only_the_mayor_may_spend(self):
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await fund_machine(tx, GUILD, TREASURER_ID, "furnace", 1.0)

    async def test_the_treasury_cannot_be_overspent(self):
        await self.set(treasury=1.0)
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await fund_machine(tx, GUILD, MAYOR_ID, "furnace", 1.01)

    async def test_funding_a_machine_burns_and_levels_it(self):
        async with self.db.transaction() as tx:
            await fund_machine(tx, GUILD, MAYOR_ID, "furnace", 30.0)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["treasury"], 100_000.0 - 30.0)
        self.assertAlmostEqual(cfg["currency_burned_total"], 30.0)
        self.assertEqual(cfg["furnace_level"], 3)
        self.assertAlmostEqual((await mining_slot_status(self.db, GUILD)).progress, 30.0)

    async def test_enhancements_double_speed_on_a_five_times_ladder(self):
        base = await machine_speed_level(self.db, GUILD, "furnace")
        async with self.db.transaction() as tx:
            level, price = await buy_enhancement(tx, GUILD, MAYOR_ID, "furnace")
        self.assertEqual((level, price), (1, ENHANCEMENT_PRICE_BASE))
        async with self.db.transaction() as tx:
            level, price = await buy_enhancement(tx, GUILD, MAYOR_ID, "furnace")
        self.assertEqual((level, price), (2, ENHANCEMENT_PRICE_BASE * ENHANCEMENT_PRICE_STEP))
        self.assertAlmostEqual(await machine_speed_level(self.db, GUILD, "furnace"), base * 4)
        # Every government burn counts toward mining slots, once.
        self.assertAlmostEqual(
            (await mining_slot_status(self.db, GUILD)).progress,
            ENHANCEMENT_PRICE_BASE * (1 + ENHANCEMENT_PRICE_STEP),
        )

    async def test_mining_slot_enhancement_counts_five_times(self):
        async with self.db.transaction() as tx:
            credit = await fund_mining_slots(tx, GUILD, MAYOR_ID, 5.0)
        self.assertEqual(credit, 25.0)
        slots = await mining_slot_status(self.db, GUILD)
        self.assertEqual(slots.progress, mining_slot_threshold(2))
        self.assertEqual(slots.level, 2)
        # It levels no machine.
        self.assertEqual((await self.cfg())["furnace_fees_collected"], 0.0)

    async def ledger(self, days_ago, value, now):
        await self.db.execute(
            "INSERT INTO production_ledger (guild_id, occurred_at, source, material_id, quantity, "
            "output_value, input_value) VALUES (?, ?, 'mining', 'iron_ore', 1, ?, 0)",
            (GUILD, sqlite_timestamp(now - timedelta(days=days_ago)), value),
        )

    async def test_a_bonanza_needs_a_week_of_history(self):
        now = THURSDAY
        await self.ledger(3, 500.0, now)
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await start_bonanza(tx, GUILD, MAYOR_ID, now)

    async def test_a_bonanza_costs_half_the_weeks_gdp_and_never_under_the_minimum(self):
        now = THURSDAY
        await self.ledger(8, 1.0, now)        # history, outside the window
        await self.ledger(1, 60.0, now)
        self.assertEqual((await bonanza_quote(self.db, GUILD, now)).price, BONANZA_MINIMUM_PRICE)
        await self.ledger(2, 140.0, now)      # 200.00 this week
        self.assertEqual((await bonanza_quote(self.db, GUILD, now)).price, 100.0)

    async def test_a_bonanza_doubles_machines_for_its_duration_and_cannot_stack(self):
        now = datetime.now(timezone.utc)
        await self.ledger(8, 1.0, now)
        base = await machine_speed_level(self.db, GUILD, "press")
        async with self.db.transaction() as tx:
            await start_bonanza(tx, GUILD, MAYOR_ID, now)
        self.assertAlmostEqual(await machine_speed_level(self.db, GUILD, "press"), base * 2)
        self.assertAlmostEqual(furnace_rate(await machine_speed_level(self.db, GUILD, "furnace")), furnace_rate(base) * 2)
        with self.assertRaises(GovernmentError):
            async with self.db.transaction() as tx:
                await start_bonanza(tx, GUILD, MAYOR_ID, now)
        await self.set(bonanza_until=sqlite_timestamp(now - timedelta(seconds=1)))
        self.assertAlmostEqual(await machine_speed_level(self.db, GUILD, "press"), base)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The cog: its commands and its loop, driven without a gateway
# ---------------------------------------------------------------------------

class FakeInteraction:
    def __init__(self, user_id, guild_id=GUILD):
        self.guild_id = guild_id
        self.guild = None
        self.user = SimpleNamespace(id=user_id, display_name=f"User{user_id}")
        self.response = AsyncMock()

    @property
    def refusal(self):
        call = self.response.send_message.call_args
        return call.args[0] if call and call.args else None

    @property
    def embed(self):
        call = self.response.send_message.call_args
        embeds = call.kwargs.get("embeds")
        return embeds[0] if embeds else call.kwargs.get("embed")


class CogTests(_GovernmentTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog = GovernmentCog.__new__(GovernmentCog)
        self.cog.db = self.db
        self.cog.bot = SimpleNamespace(get_guild=lambda guild_id: None)

    async def test_the_status_page_renders(self):
        await self.set(tax_percent=10, treasury=12.5, bond_sale_cents=500)
        i = FakeInteraction(ALICE)
        await GovernmentCog.government_status_command.callback(self.cog, i)
        self.assertIsNone(i.refusal)
        text = i.embed.description + "".join(field.value for field in i.embed.fields)
        self.assertIn(f"<@{MAYOR_ID}>", text)
        self.assertIn("12.50", text)

    async def test_a_player_who_is_not_treasurer_is_refused_privately(self):
        i = FakeInteraction(ALICE)
        await GovernmentCog.treasurer_tax.callback(self.cog, i, 10)
        self.assertIn("Only the Treasurer", i.refusal)
        self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])

    async def test_the_treasurer_sets_a_fee(self):
        i = FakeInteraction(TREASURER_ID)
        await GovernmentCog.treasurer_fee.callback(
            self.cog, i,
            app_commands.Choice(name="furnace", value="furnace"),
            app_commands.Choice(name="x2", value="2.0"),
        )
        self.assertIsNone(i.refusal)
        self.assertEqual((await self.cfg())["furnace_fee_multiplier"], 2.0)

    async def test_the_mayor_funds_slots_and_a_bond_is_bought(self):
        await self.set(treasury=10.0)
        i = FakeInteraction(MAYOR_ID)
        await GovernmentCog.mayor_slots.callback(self.cog, i, 5.0)
        self.assertIsNone(i.refusal)
        await self.record_tax_days_ago(50.0, 1, datetime.now(timezone.utc))
        i = FakeInteraction(MAYOR_ID)
        await GovernmentCog.mayor_bonds.callback(self.cog, i, 10)
        self.assertIsNone(i.refusal)
        i = FakeInteraction(BOB)
        await GovernmentCog.bonds_buy.callback(self.cog, i, app_commands.Choice(name="10", value=1_000))
        self.assertIsNone(i.refusal)
        i = FakeInteraction(BOB)
        await GovernmentCog.bonds_holdings.callback(self.cog, i)
        self.assertIn("10.00", i.embed.description)

    async def test_the_loop_pays_bondholders(self):
        await self.db.execute(
            "INSERT INTO government_bonds (guild_id, holder_id, principal_cents, rate_percent, "
            "owed_cents, remaining_cents, tax_percent_at_sale) VALUES (?, ?, 100, 0, 100, 100, 0)",
            (GUILD, BOB),
        )
        await self.set(repayment_pool=0.40)
        await GovernmentCog.government_loop.coro(self.cog)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE + 0.40)

    async def test_leaving_and_rejoining_freeze_and_unfreeze(self):
        await self.db.execute(
            "INSERT INTO government_bonds (guild_id, holder_id, principal_cents, rate_percent, "
            "owed_cents, remaining_cents, tax_percent_at_sale) VALUES (?, ?, 100, 0, 100, 100, 0)",
            (GUILD, BOB),
        )
        await self.cog.on_raw_member_remove(SimpleNamespace(guild_id=GUILD, user=SimpleNamespace(id=BOB)))
        self.assertEqual(await outstanding_debt_cents(self.db, GUILD), 0)
        await self.cog.on_member_join(SimpleNamespace(guild=SimpleNamespace(id=GUILD), id=BOB))
        self.assertEqual(await outstanding_debt_cents(self.db, GUILD), 100)

    async def vote_through_the_command(self, voter, candidate_id, bot=False, now=THURSDAY):
        await self.place_drill(voter, now - timedelta(days=10))
        await self.place_drill(candidate_id, now - timedelta(days=10))
        i = FakeInteraction(voter)
        member = SimpleNamespace(id=candidate_id, bot=bot, mention=f"<@{candidate_id}>")
        with patch("utils.government.clock_now", return_value=now):
            await GovernmentCog.vote_mayor.callback(self.cog, i, member)
        return i

    async def test_voting_for_yourself_through_the_command_is_refused(self):
        # A fully eligible voter, on a Thursday, naming themselves.
        i = await self.vote_through_the_command(ALICE, ALICE)
        self.assertEqual(i.refusal, "You can't vote for yourself.")
        self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])
        votes = await self.db.fetchone("SELECT COUNT(*) AS n FROM government_votes")
        self.assertEqual(votes["n"], 0)
        # And the same voter naming somebody else goes through.
        i = await self.vote_through_the_command(ALICE, BOB)
        self.assertIsNone(i.refusal)

    async def test_voting_for_yourself_is_refused_for_treasurer_too(self):
        await self.place_drill(ALICE, THURSDAY - timedelta(days=10))
        i = FakeInteraction(ALICE)
        member = SimpleNamespace(id=ALICE, bot=False, mention=f"<@{ALICE}>")
        with patch("utils.government.clock_now", return_value=THURSDAY):
            await GovernmentCog.vote_treasurer.callback(self.cog, i, member)
        self.assertEqual(i.refusal, "You can't vote for yourself.")

    async def test_a_drill_placed_before_the_update_votes_in_the_first_election(self):
        # A placed drill with no placed_at predates the update (schema.sql):
        # its owner qualifies on the very first Thursday.
        voter = 556
        await adjust_currency_balance(self.db, GUILD, voter, 1.0)
        await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, placed_at) VALUES (?, ?, 'iron_drill', NULL)",
            (GUILD, voter),
        )
        i = FakeInteraction(voter)
        member = SimpleNamespace(id=BOB, bot=False, mention=f"<@{BOB}>")
        await self.place_drill(BOB, THURSDAY - timedelta(days=1))
        with patch("utils.government.clock_now", return_value=THURSDAY):
            await GovernmentCog.vote_mayor.callback(self.cog, i, member)
        self.assertIsNone(i.refusal)

    async def test_an_unplaced_drill_does_not_qualify(self):
        # NULL placed_at only means anything on a drill that is in the ground.
        voter = 557
        await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, placed_at) VALUES (NULL, ?, 'iron_drill', NULL)",
            (voter,),
        )
        self.assertFalse(await can_vote(self.db, GUILD, voter, "2026-09-24"))
