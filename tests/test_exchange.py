"""
Tests for the DragonCoin exchange (utils/exchange.py, cogs/exchange.py,
docs/market.md section 2) against a real database.

SupplyTests is the class that matters: it pins that the exchange burns exactly
what it sells for and mints exactly what it buys back for, so a round trip in
one server creates no currency and a trip between two moves it at par.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord import app_commands

from cogs.exchange import ExchangeCog
from database.db import Database, InsufficientQuantity
from utils.db_helpers import (
    adjust_currency_balance,
    circulating_currency_for,
    ensure_server_row,
    get_currency_balance,
)
from utils.exchange import (
    BUY,
    DRAGONCOIN_RATE,
    SELL,
    buy_dragoncoin,
    dragoncoin_cents,
    get_dragoncoin_cents,
    max_buyable_cents,
    quote,
    sell_dragoncoin,
)
from utils.formatting import DRAGONCOIN_EMOJI, format_dragoncoin, format_dragoncoin_amount
from utils.government import RATE_PERCENTS

GUILD = 8080
OTHER_GUILD = 9090
ALICE = 111
BOB = 222

STARTING_BALANCE = 5_000.0


class RoundingTests(unittest.TestCase):
    def test_amounts_round_to_the_nearest_hundredth(self):
        self.assertEqual(dragoncoin_cents(1.234), 123)
        self.assertEqual(dragoncoin_cents(1.236), 124)
        self.assertEqual(dragoncoin_cents(1.0), 100)
        self.assertEqual(dragoncoin_cents(12.5), 1250)

    def test_halves_round_up_on_the_number_as_typed(self):
        self.assertEqual(dragoncoin_cents(1.235), 124)
        self.assertEqual(dragoncoin_cents(0.005), 1)
        # The float 2.675 is a hair under 2.675, which is why the rounding is
        # done on its text: round() on the float itself gives the lower cent.
        self.assertEqual(round(2.675, 2), 2.67)
        self.assertEqual(dragoncoin_cents(2.675), 268)

    def test_too_little_rounds_to_nothing(self):
        self.assertEqual(dragoncoin_cents(0.004), 0)
        self.assertEqual(dragoncoin_cents(0.0), 0)
        self.assertEqual(dragoncoin_cents(float("nan")), 0)
        self.assertEqual(dragoncoin_cents(float("inf")), 0)

    def test_figures_are_shown_exactly(self):
        self.assertEqual(format_dragoncoin_amount(123), "1.23")
        self.assertEqual(format_dragoncoin_amount(5), "0.05")
        self.assertEqual(format_dragoncoin_amount(123_456_700), "1,234,567.00")
        self.assertEqual(format_dragoncoin(100), f"{DRAGONCOIN_EMOJI} 1.00")


class QuoteTests(unittest.TestCase):
    def test_one_dragoncoin_is_the_rate_with_no_tax(self):
        priced = quote(100, 0.0)
        self.assertEqual(DRAGONCOIN_RATE, 1000)
        self.assertEqual(priced.base_cents, 100_000)
        self.assertEqual(priced.cost_cents, 100_000)
        self.assertEqual(priced.proceeds_cents, 100_000)

    def test_the_cost_is_worked_out_from_the_rounded_amount(self):
        self.assertEqual(quote(dragoncoin_cents(1.234), 0.0).cost_cents, 123_000)

    def test_the_tax_is_added_when_buying_and_taken_when_selling(self):
        priced = quote(100, 10.0)
        self.assertEqual(priced.cost_cents, 110_000)
        self.assertEqual(priced.proceeds_cents, 90_000)

    def test_a_tax_short_of_a_cent_is_rounded_down(self):
        # 6.25% of the 10.00 a hundredth costs is 0.625.
        priced = quote(1, 6.25)
        self.assertEqual(priced.base_cents, 1_000)
        self.assertEqual(priced.tax_cents, 62)

    def test_all_of_it_is_all_of_it(self):
        priced = quote(37, 100.0)
        self.assertEqual(priced.tax_cents, priced.base_cents)
        self.assertEqual(priced.proceeds_cents, 0)

    def test_the_sell_side_cut_survives_small_sales(self):
        # Counted in DragonCoin hundredths, 10% of 0.09 would round to 0.
        self.assertEqual(quote(9, 10.0).tax_cents, 900)


class MaxBuyableTests(unittest.TestCase):
    def test_it_is_the_largest_amount_the_balance_covers(self):
        for percent in RATE_PERCENTS:
            for balance in (0.0, 9.99, 10.0, 10.62, 10.63, 1_234.56, 99_999.99):
                with self.subTest(percent=percent, balance=balance):
                    most = max_buyable_cents(balance, percent)
                    balance_cents = round(balance * 100)
                    if most:
                        self.assertLessEqual(quote(most, percent).cost_cents, balance_cents)
                    self.assertGreater(quote(most + 1, percent).cost_cents, balance_cents)

    def test_a_float_a_hair_under_a_whole_cent_still_counts(self):
        self.assertEqual(max_buyable_cents(10.0 - 1e-12, 0.0), 1)


class _ExchangeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        for guild in (GUILD, OTHER_GUILD):
            await ensure_server_row(self.db, guild)
            await adjust_currency_balance(self.db, guild, ALICE, STARTING_BALANCE)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def cfg(self, guild=GUILD):
        return await self.db.fetchone("SELECT * FROM server_config WHERE guild_id = ?", (guild,))

    async def set_tax(self, percent, guild=GUILD):
        await self.db.execute(
            "UPDATE server_config SET exchange_tax_percent = ? WHERE guild_id = ?", (percent, guild)
        )

    async def buy(self, cents, user=ALICE, guild=GUILD):
        async with self.db.transaction() as tx:
            return await buy_dragoncoin(tx, guild, user, cents)

    async def sell(self, cents, user=ALICE, guild=GUILD):
        async with self.db.transaction() as tx:
            return await sell_dragoncoin(tx, guild, user, cents)

    async def revenue_today(self, guild=GUILD):
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM government_tax_daily WHERE guild_id = ?",
            (guild,),
        )
        return row["total"]


class BuyTests(_ExchangeTestCase):
    async def test_buying_burns_the_price_and_holds_the_tax(self):
        await self.set_tax(10.0)
        result = await self.buy(150)
        self.assertEqual(result.quote.cost_cents, 165_000)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE - 1_650)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 150)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["currency_burned_total"], 1_500)
        self.assertAlmostEqual(cfg["treasury"], 150)
        self.assertAlmostEqual(await self.revenue_today(), 150)
        self.assertEqual(result.dragoncoin_after, 150)
        self.assertAlmostEqual(result.balance_after, STARTING_BALANCE - 1_650)

    async def test_the_price_counts_toward_no_machine_or_mining_slot(self):
        await self.buy(100)
        cfg = await self.cfg()
        self.assertEqual(cfg["mining_slot_credit"], 0)
        for machine in ("furnace", "blast_furnace", "factory", "press", "scrapper"):
            self.assertEqual(cfg[f"{machine}_fees_collected"], 0)

    async def test_a_player_who_cannot_cover_the_tax_is_refused_and_nothing_moves(self):
        await self.set_tax(10.0)
        with self.assertRaises(InsufficientQuantity):
            await self.buy(500)  # 5,500 against 5,000
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 0)
        cfg = await self.cfg()
        self.assertEqual(cfg["currency_burned_total"], 0)
        self.assertEqual(cfg["treasury"], 0)

    async def test_a_player_with_no_balance_here_is_refused(self):
        with self.assertRaises(InsufficientQuantity):
            await self.buy(1, user=BOB)

    async def test_the_tax_repays_bondholders_while_the_server_owes(self):
        await self.set_tax(10.0)
        await self.db.execute(
            "INSERT INTO government_bonds (guild_id, holder_id, principal_cents, rate_percent, "
            "owed_cents, remaining_cents, tax_percent_at_sale) VALUES (?, ?, 500, 0, 500, 500, 0)",
            (GUILD, BOB),
        )
        await self.buy(100)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["repayment_pool"], 100)
        self.assertEqual(cfg["treasury"], 0)


class SellTests(_ExchangeTestCase):
    async def test_selling_mints_the_price_and_holds_the_tax(self):
        await self.buy(200)
        await self.set_tax(10.0)
        result = await self.sell(150)
        self.assertEqual(result.quote.proceeds_cents, 135_000)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE - 2_000 + 1_350
        )
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 50)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["currency_minted_total"], 1_500)
        self.assertAlmostEqual(cfg["treasury"], 150)
        self.assertEqual(result.dragoncoin_after, 50)

    async def test_selling_more_than_you_hold_is_refused_and_nothing_moves(self):
        await self.buy(100)
        with self.assertRaises(InsufficientQuantity):
            await self.sell(101)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 100)
        self.assertEqual((await self.cfg())["currency_minted_total"], 0)

    async def test_at_all_of_it_selling_pays_nothing(self):
        await self.buy(100)
        await self.set_tax(100.0)
        result = await self.sell(100)
        self.assertEqual(result.quote.proceeds_cents, 0)
        self.assertAlmostEqual((await self.cfg())["treasury"], 1_000)

    async def test_a_balance_built_from_pieces_sells_whole(self):
        """Why DragonCoin is integer hundredths: as floats, 0.70 + 0.10 is
        0.7999999999999999, and a guarded deduction of 0.80 would refuse."""
        self.assertLess(0.7 + 0.1, 0.8)
        await self.buy(70)
        await self.buy(10)
        await self.sell(80)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 0)


class SupplyTests(_ExchangeTestCase):
    async def test_a_round_trip_with_no_tax_creates_nothing(self):
        start = await circulating_currency_for(self.db, GUILD)
        await self.buy(345)
        await self.sell(345)
        self.assertAlmostEqual(await circulating_currency_for(self.db, GUILD), start)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["currency_minted_total"], cfg["currency_burned_total"])

    async def test_a_taxed_round_trip_costs_the_player_the_tax_twice(self):
        await self.set_tax(6.25)
        start = await circulating_currency_for(self.db, GUILD)
        bought = await self.buy(100)
        sold = await self.sell(100)
        tax = (bought.quote.tax_cents + sold.quote.tax_cents) / 100
        self.assertAlmostEqual(tax, 125)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE - tax)
        # The tax is held, not burned, so the server's supply is unchanged.
        self.assertAlmostEqual(await circulating_currency_for(self.db, GUILD), start)

    async def test_buying_in_one_server_and_selling_in_another_moves_currency_at_par(self):
        here = await circulating_currency_for(self.db, GUILD)
        there = await circulating_currency_for(self.db, OTHER_GUILD)
        await self.buy(100, guild=GUILD)
        await self.sell(100, guild=OTHER_GUILD)
        self.assertAlmostEqual(await circulating_currency_for(self.db, GUILD), here - 1_000)
        self.assertAlmostEqual(await circulating_currency_for(self.db, OTHER_GUILD), there + 1_000)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 0)

    async def test_buying_moves_the_supply_by_exactly_the_price(self):
        await self.set_tax(12.5)
        start = await circulating_currency_for(self.db, GUILD)
        result = await self.buy(77)
        self.assertAlmostEqual(
            await circulating_currency_for(self.db, GUILD), start - result.quote.base_cents / 100
        )


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

    def text(self):
        embed = self.embed
        return (embed.title or "") + (embed.description or "") + "".join(
            field.name + field.value for field in embed.fields
        )


class CogTests(_ExchangeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog = ExchangeCog.__new__(ExchangeCog)
        self.cog.db = self.db

    async def exchange(self, user=ALICE, amount=None, direction=None, guild_id=GUILD):
        i = FakeInteraction(user, guild_id)
        choice = app_commands.Choice(name=direction, value=direction) if direction else None
        await ExchangeCog.exchange.callback(self.cog, i, amount, choice)
        return i

    async def test_no_amount_shows_the_rate_after_tax_and_spends_nothing(self):
        await self.set_tax(10.0)
        i = await self.exchange()
        self.assertIsNone(i.refusal)
        text = i.text()
        self.assertIn("1 DragonCoin = 1,000", text)
        self.assertIn("1,100.00", text)
        self.assertIn("900.00", text)
        self.assertIn("10%", text)
        # 5,000 at 1,100 each buys 4.54.
        self.assertIn(format_dragoncoin(454), text)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE)

    async def test_buying_through_the_command(self):
        i = await self.exchange(amount=1.234)
        self.assertIsNone(i.refusal)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 123)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE - 1_230)
        self.assertIn("round to the nearest 0.01", i.embed.description)

    async def test_an_exact_amount_says_nothing_about_rounding(self):
        i = await self.exchange(amount=1.23, direction=BUY)
        self.assertNotIn("round", i.embed.description)

    async def test_selling_through_the_command(self):
        await self.buy(200)
        await self.set_tax(10.0)
        i = await self.exchange(amount=1, direction=SELL)
        self.assertIsNone(i.refusal)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 100)
        self.assertIn("Exchange Tax", i.text())
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, ALICE), STARTING_BALANCE - 2_000 + 900
        )

    async def test_the_tax_line_is_left_out_at_zero(self):
        i = await self.exchange(amount=1)
        self.assertNotIn("Exchange Tax", i.text())

    async def test_too_small_an_amount_is_refused(self):
        i = await self.exchange(amount=0.004)
        self.assertIn("smallest exchange", i.refusal)
        self.assertTrue(i.response.send_message.call_args.kwargs["ephemeral"])

    async def test_a_purchase_you_cannot_afford_names_the_most_you_can(self):
        await self.set_tax(10.0)
        i = await self.exchange(amount=5)
        self.assertIn("5,500.00", i.refusal)
        self.assertIn(format_dragoncoin(454), i.refusal)
        self.assertEqual(await get_dragoncoin_cents(self.db, ALICE), 0)

    async def test_selling_more_than_you_have_says_what_you_have(self):
        await self.buy(50)
        i = await self.exchange(amount=1, direction=SELL)
        self.assertEqual(i.refusal, f"You only have {format_dragoncoin(50)}.")

    async def test_it_is_refused_outside_a_server(self):
        i = await self.exchange(amount=1, guild_id=None)
        self.assertIn("server", i.refusal)


if __name__ == "__main__":
    unittest.main()
