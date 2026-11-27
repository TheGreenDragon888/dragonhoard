"""
Tests for server shares (utils/shares.py, cogs/shares.py, docs/shares.md)
against a real database.

SupplyTests is the class that matters: it pins that a government sale mints
exactly what the DragonCoin it destroyed was worth, that dividends burn exactly
the currency they turn into DragonCoin, and that a trade between players mints
and burns nothing - the promises that keep shares from printing money.
"""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.government import GovernmentCog
from cogs.shares import SharesCog
from database.db import Database
from utils.db_helpers import circulating_currency_for, ensure_server_row, ensure_user_row
from utils.exchange import get_dragoncoin_cents
from utils.government import (
    GovernmentError,
    collect_revenue,
    game_now,
    member_left,
    outstanding_shares,
)
from utils.shares import (
    MAX_OPEN_SHARE_LISTINGS,
    MAX_SHARES,
    ShareError,
    available_shares,
    buy_back_shares,
    buy_shares,
    cancel_share_entry,
    expire_share_entries,
    held_shares,
    list_shares,
    open_share_sale,
    order_shares,
    pay_dividends,
    price_floor_cents,
    sell_shares,
    set_share_ceiling,
    share_price_floor,
    withdraw_share_books,
)

GUILD = 6060
OTHER_GUILD = 6161
MAYOR_ID = 101
TREASURER_ID = 102
ALICE = 111
BOB = 222
CARA = 333

STARTING_DC = 10_000      # hundredths: 100 DragonCoin each


class PriceFloorTests(unittest.TestCase):
    def test_it_is_three_months_of_one_shares_dividends_rounded_up(self):
        # 1,000 a week: one share earns 10 a week, 130 over 13 weeks, which is
        # 0.13 DC.
        self.assertEqual(price_floor_cents(1000.0), 13)
        self.assertEqual(price_floor_cents(1000.01), 14)

    def test_it_is_never_under_a_hundredth(self):
        self.assertEqual(price_floor_cents(0.0), 1)
        self.assertEqual(price_floor_cents(0.5), 1)


class _SharesTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        for guild in (GUILD, OTHER_GUILD):
            await ensure_server_row(self.db, guild)
            await self.db.execute(
                "UPDATE server_config SET mayor_id = ?, treasurer_id = ?, share_ceiling = 20 "
                "WHERE guild_id = ?",
                (MAYOR_ID, TREASURER_ID, guild),
            )
        for user in (MAYOR_ID, TREASURER_ID, ALICE, BOB, CARA):
            await ensure_user_row(self.db, user)
            await self.db.execute(
                "UPDATE users SET dragoncoin_cents = ? WHERE user_id = ?", (STARTING_DC, user)
            )

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def cfg(self, guild=GUILD):
        return await self.db.fetchone("SELECT * FROM server_config WHERE guild_id = ?", (guild,))

    async def set(self, guild=GUILD, **columns):
        assignments = ", ".join(f"{column} = ?" for column in columns)
        await self.db.execute(
            f"UPDATE server_config SET {assignments} WHERE guild_id = ?", (*columns.values(), guild)
        )

    async def dc(self, user):
        return await get_dragoncoin_cents(self.db, user)

    async def grant(self, user, shares, guild=GUILD):
        """Shares handed straight to a player, for tests about what happens
        after they are owned."""
        await self.db.execute(
            "INSERT INTO share_holdings (guild_id, holder_id, shares) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, holder_id) DO UPDATE SET shares = shares + excluded.shares",
            (guild, user, shares),
        )

    async def tx(self, func, *args, **kwargs):
        async with self.db.transaction() as tx:
            return await func(tx, *args, **kwargs)

    async def week_of_revenue(self, amount, guild=GUILD):
        """`amount` of revenue recorded yesterday, inside the floor's window."""
        yesterday = (game_now().date() - timedelta(days=1)).isoformat()
        await self.db.execute(
            "INSERT INTO government_tax_daily (guild_id, day, amount) VALUES (?, ?, ?)",
            (guild, yesterday, amount),
        )


class CeilingTests(_SharesTestCase):
    async def test_only_the_treasurer_sets_it_and_only_once_a_day(self):
        with self.assertRaises(GovernmentError):
            await self.tx(set_share_ceiling, GUILD, ALICE, 10)
        await self.tx(set_share_ceiling, GUILD, TREASURER_ID, 10)
        self.assertEqual((await self.cfg())["share_ceiling"], 10)
        with self.assertRaises(GovernmentError):
            await self.tx(set_share_ceiling, GUILD, TREASURER_ID, 12)

    async def test_it_is_capped(self):
        with self.assertRaises(GovernmentError):
            await self.tx(set_share_ceiling, GUILD, TREASURER_ID, MAX_SHARES + 1)

    async def test_lowering_it_cancels_no_shares(self):
        await self.grant(ALICE, 15)
        await self.tx(set_share_ceiling, GUILD, TREASURER_ID, 5)
        self.assertEqual(await outstanding_shares(self.db, GUILD), 15)
        with self.assertRaises(GovernmentError):
            await self.tx(open_share_sale, GUILD, MAYOR_ID, 1, 10)


class SaleTests(_SharesTestCase):
    async def test_only_the_mayor_opens_one(self):
        with self.assertRaises(GovernmentError):
            await self.tx(open_share_sale, GUILD, ALICE, 5, 10)
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 10)
        cfg = await self.cfg()
        self.assertEqual((cfg["share_sale_quantity"], cfg["share_sale_price_cents"]), (5, 10))

    async def test_it_must_fit_under_the_ceiling(self):
        await self.grant(ALICE, 18)
        with self.assertRaises(GovernmentError):
            await self.tx(open_share_sale, GUILD, MAYOR_ID, 3, 10)
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 2, 10)

    async def test_it_may_not_ask_less_than_the_floor(self):
        await self.week_of_revenue(1000.0)
        self.assertEqual(await share_price_floor(self.db, GUILD), 13)
        with self.assertRaises(GovernmentError):
            await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 12)
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 13)

    async def test_the_floor_counts_the_shareholders_slice_too(self):
        yesterday = (game_now().date() - timedelta(days=1)).isoformat()
        await self.db.execute(
            "INSERT INTO government_tax_daily (guild_id, day, amount, dividends) VALUES (?, ?, 800, 200)",
            (GUILD, yesterday),
        )
        self.assertEqual(await share_price_floor(self.db, GUILD), 13)

    async def test_zero_withdraws_it(self):
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 10)
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 0, 0)
        self.assertEqual((await self.cfg())["share_sale_quantity"], 0)

    async def test_buying_from_it_pays_the_treasury(self):
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 10)
        trade = await self.tx(buy_shares, GUILD, ALICE, 3)
        self.assertEqual(trade.total_cents, 30)
        self.assertEqual(await self.dc(ALICE), STARTING_DC - 30)
        self.assertEqual(await held_shares(self.db, GUILD, ALICE), 3)
        cfg = await self.cfg()
        self.assertEqual(cfg["share_sale_quantity"], 2)
        self.assertAlmostEqual(cfg["treasury"], 300)              # 0.30 DC at 1,000 each
        self.assertAlmostEqual(cfg["currency_minted_total"], 300)

    async def test_a_lowered_ceiling_limits_what_the_sale_can_still_sell(self):
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 10, 10)
        await self.set(share_ceiling=4)
        with self.assertRaises(ShareError):
            await self.tx(buy_shares, GUILD, ALICE, 5)
        await self.tx(buy_shares, GUILD, ALICE, 4)
        self.assertEqual(await outstanding_shares(self.db, GUILD), 4)

    async def test_it_ends_when_the_mayor_leaves(self):
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 10)
        await self.tx(member_left, GUILD, MAYOR_ID)
        self.assertEqual((await self.cfg())["share_sale_quantity"], 0)

    async def test_a_buyer_who_cannot_pay_gets_nothing(self):
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 5_000)
        with self.assertRaises(ShareError):
            await self.tx(buy_shares, GUILD, ALICE, 3)
        self.assertEqual(await self.dc(ALICE), STARTING_DC)
        self.assertEqual(await held_shares(self.db, GUILD, ALICE), 0)
        self.assertEqual((await self.cfg())["share_sale_quantity"], 5)


class BookTests(_SharesTestCase):
    async def test_buying_takes_the_cheapest_first_and_players_first_at_one_price(self):
        await self.grant(BOB, 5)
        await self.grant(CARA, 5)
        await self.tx(list_shares, GUILD, BOB, 2, 12)
        await self.tx(list_shares, GUILD, CARA, 2, 10)
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 10)
        trade = await self.tx(buy_shares, GUILD, ALICE, 5)
        legs = [(fill.source, fill.counterparty, fill.quantity, fill.price_cents) for fill in trade.fills]
        self.assertEqual(legs, [
            ("player", CARA, 2, 10), ("government", None, 3, 10),
        ])

    async def test_your_own_listings_are_skipped(self):
        await self.grant(ALICE, 5)
        await self.tx(list_shares, GUILD, ALICE, 2, 10)
        with self.assertRaises(ShareError):
            await self.tx(buy_shares, GUILD, ALICE, 1)

    async def test_a_purchase_is_all_or_nothing(self):
        await self.grant(BOB, 5)
        await self.tx(list_shares, GUILD, BOB, 2, 10)
        with self.assertRaises(ShareError):
            await self.tx(buy_shares, GUILD, ALICE, 3)
        self.assertEqual(await held_shares(self.db, GUILD, BOB), 5)

    async def test_a_listing_reserves_its_shares_and_they_keep_earning(self):
        await self.grant(BOB, 5)
        await self.tx(list_shares, GUILD, BOB, 4, 10)
        self.assertEqual(await available_shares(self.db, GUILD, BOB), 1)
        with self.assertRaises(ShareError):
            await self.tx(list_shares, GUILD, BOB, 2, 10)
        await self.tx(collect_revenue, GUILD, 1_000.0)
        await self.tx(pay_dividends, GUILD)
        self.assertEqual(await self.dc(BOB), STARTING_DC + 5)  # 5% of 1,000 is 50, 0.05 DC

    async def test_selling_fills_the_dearest_bid_from_its_escrow(self):
        await self.grant(BOB, 5)
        await self.tx(order_shares, GUILD, ALICE, 3, 20)
        await self.tx(order_shares, GUILD, CARA, 3, 25)
        self.assertEqual(await self.dc(ALICE), STARTING_DC - 60)
        trade = await self.tx(sell_shares, GUILD, BOB, 4)
        self.assertEqual(trade.total_cents, 3 * 25 + 1 * 20)
        self.assertEqual(await self.dc(BOB), STARTING_DC + 95)
        self.assertEqual(await held_shares(self.db, GUILD, CARA), 3)
        self.assertEqual(await held_shares(self.db, GUILD, ALICE), 1)
        self.assertEqual(await held_shares(self.db, GUILD, BOB), 1)

    async def test_listed_shares_cannot_be_sold_into_a_bid(self):
        await self.grant(BOB, 2)
        await self.tx(list_shares, GUILD, BOB, 2, 10)
        await self.tx(order_shares, GUILD, ALICE, 1, 50)
        with self.assertRaises(ShareError):
            await self.tx(sell_shares, GUILD, BOB, 1)

    async def test_cancelling_an_order_returns_its_dragoncoin(self):
        order_id = await self.tx(order_shares, GUILD, ALICE, 3, 20)
        _, refund = await self.tx(cancel_share_entry, GUILD, ALICE, "order", order_id)
        self.assertEqual(refund, 60)
        self.assertEqual(await self.dc(ALICE), STARTING_DC)

    async def test_cancelling_a_listing_frees_its_shares(self):
        await self.grant(BOB, 3)
        listing_id = await self.tx(list_shares, GUILD, BOB, 3, 10)
        await self.tx(cancel_share_entry, GUILD, BOB, "listing", listing_id)
        self.assertEqual(await available_shares(self.db, GUILD, BOB), 3)

    async def test_nobody_cancels_anyone_elses_entry(self):
        order_id = await self.tx(order_shares, GUILD, ALICE, 3, 20)
        with self.assertRaises(ShareError):
            await self.tx(cancel_share_entry, GUILD, BOB, "order", order_id)

    async def test_entries_are_limited(self):
        await self.grant(BOB, 10)
        for _ in range(MAX_OPEN_SHARE_LISTINGS):
            await self.tx(list_shares, GUILD, BOB, 1, 10)
        with self.assertRaises(ShareError):
            await self.tx(list_shares, GUILD, BOB, 1, 10)

    async def test_expired_entries_are_never_filled_and_the_sweep_refunds_bids(self):
        await self.grant(BOB, 3)
        await self.tx(list_shares, GUILD, BOB, 3, 10)
        await self.tx(order_shares, GUILD, ALICE, 2, 20)
        await self.db.execute("UPDATE share_listings SET expires_at = datetime('now', '-1 minute')")
        await self.db.execute("UPDATE share_orders SET expires_at = datetime('now', '-1 minute')")
        with self.assertRaises(ShareError):
            await self.tx(buy_shares, GUILD, CARA, 1)
        with self.assertRaises(ShareError):
            await self.tx(sell_shares, GUILD, BOB, 1)
        self.assertEqual(await available_shares(self.db, GUILD, BOB), 3, "an expired listing reserves nothing")
        listings, orders = await self.tx(expire_share_entries, GUILD)
        self.assertEqual((len(listings), [refund for _, refund in orders]), (1, [40]))
        self.assertEqual(await self.dc(ALICE), STARTING_DC)

    async def test_removing_the_bot_withdraws_the_books_and_keeps_the_shares(self):
        await self.grant(BOB, 3)
        await self.tx(list_shares, GUILD, BOB, 3, 10)
        await self.tx(order_shares, GUILD, ALICE, 2, 20)
        self.assertEqual(await self.tx(withdraw_share_books, GUILD), 2)
        self.assertEqual(await self.dc(ALICE), STARTING_DC)
        self.assertEqual(await held_shares(self.db, GUILD, BOB), 3)


class BuybackTests(_SharesTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.week_of_revenue(1000.0)                     # a floor of 0.13 DC
        await self.set(treasury=1_000.0)
        await self.grant(BOB, 6)

    async def test_it_retires_shares_and_pays_from_the_treasury(self):
        await self.tx(list_shares, GUILD, BOB, 4, 10)
        bought = await self.tx(buy_back_shares, GUILD, MAYOR_ID, 3, 13)
        self.assertEqual(sum(fill.quantity for fill in bought.fills), 3)
        self.assertAlmostEqual(bought.cost, 300)
        self.assertEqual(await outstanding_shares(self.db, GUILD), 3)
        self.assertEqual(await self.dc(BOB), STARTING_DC + 30)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["treasury"], 700)
        self.assertAlmostEqual(cfg["currency_burned_total"], 300)
        self.assertEqual(cfg["mining_slot_credit"], 0, "a buyback buys the server nothing it keeps")

    async def test_it_never_pays_more_than_the_floor(self):
        await self.tx(list_shares, GUILD, BOB, 4, 14)
        with self.assertRaises(GovernmentError):
            await self.tx(buy_back_shares, GUILD, MAYOR_ID, 1, 500)
        self.assertEqual(await outstanding_shares(self.db, GUILD), 6)

    async def test_only_the_mayor_and_only_what_the_treasury_holds(self):
        await self.tx(list_shares, GUILD, BOB, 4, 13)
        with self.assertRaises(GovernmentError):
            await self.tx(buy_back_shares, GUILD, ALICE, 1, 13)
        await self.set(treasury=100.0)
        with self.assertRaises(GovernmentError):
            await self.tx(buy_back_shares, GUILD, MAYOR_ID, 1, 13)

    async def test_it_frees_room_under_the_ceiling(self):
        await self.set(share_ceiling=6)
        with self.assertRaises(GovernmentError):
            await self.tx(open_share_sale, GUILD, MAYOR_ID, 1, 13)
        await self.tx(list_shares, GUILD, BOB, 2, 13)
        await self.tx(buy_back_shares, GUILD, MAYOR_ID, 2, 13)
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 2, 13)


class DividendTests(_SharesTestCase):
    async def test_shareholders_are_paid_off_the_top(self):
        await self.grant(ALICE, 10)
        await self.tx(collect_revenue, GUILD, 100.0)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["dividend_pool"], 10)
        self.assertAlmostEqual(cfg["treasury"], 90)
        day = await self.db.fetchone("SELECT amount, dividends FROM government_tax_daily WHERE guild_id = ?", (GUILD,))
        self.assertAlmostEqual(day["amount"], 90, msg="the debt cap reads only what the government kept")
        self.assertAlmostEqual(day["dividends"], 10)

    async def test_bonds_still_get_what_the_government_keeps(self):
        await self.grant(ALICE, 10)
        await self.db.execute(
            "INSERT INTO government_bonds (guild_id, holder_id, principal_cents, rate_percent, "
            "owed_cents, remaining_cents, tax_percent_at_sale) VALUES (?, ?, 500, 0, 500, 500, 0)",
            (GUILD, BOB),
        )
        await self.tx(collect_revenue, GUILD, 100.0)
        cfg = await self.cfg()
        self.assertAlmostEqual(cfg["dividend_pool"], 10)
        self.assertAlmostEqual(cfg["repayment_pool"], 90)

    async def test_no_shares_means_no_slice(self):
        await self.tx(collect_revenue, GUILD, 100.0)
        self.assertEqual((await self.cfg())["dividend_pool"], 0)

    async def test_the_pool_is_split_by_shares_and_paid_a_hundredth_at_a_time(self):
        await self.grant(ALICE, 3)
        await self.grant(BOB, 1)
        await self.set(dividend_pool=45.0)
        paid = await self.tx(pay_dividends, GUILD)
        # 33.75 and 11.25 accrued: 3 and 1 whole hundredths, the rest waiting.
        self.assertEqual(paid, {ALICE: 3, BOB: 1})
        self.assertEqual(await self.dc(ALICE), STARTING_DC + 3)
        rows = await self.db.fetchall(
            "SELECT holder_id, accrued, dividends_paid_cents FROM share_holdings ORDER BY holder_id"
        )
        self.assertAlmostEqual(rows[0]["accrued"], 3.75)
        self.assertAlmostEqual(rows[1]["accrued"], 1.25)
        self.assertEqual(rows[0]["dividends_paid_cents"], 3)
        cfg = await self.cfg()
        self.assertEqual(cfg["dividend_pool"], 0)
        self.assertAlmostEqual(cfg["currency_burned_total"], 40)

    async def test_a_small_holder_gets_their_exact_share_of_many_tiny_pools(self):
        await self.grant(ALICE, 39)
        await self.grant(BOB, 1)
        for _ in range(1_000):
            await self.set(dividend_pool=0.01)
            await self.tx(pay_dividends, GUILD)
        row = await self.db.fetchone("SELECT accrued, dividends_paid_cents FROM share_holdings WHERE holder_id = ?", (BOB,))
        self.assertAlmostEqual(row["accrued"] + row["dividends_paid_cents"] * 10, 0.25)

    async def test_a_holder_who_has_left_keeps_earning(self):
        await self.grant(ALICE, 2)
        await self.tx(member_left, GUILD, ALICE)
        await self.set(dividend_pool=20.0)
        await self.tx(pay_dividends, GUILD)
        self.assertEqual(await self.dc(ALICE), STARTING_DC + 2)

    async def test_with_nobody_holding_a_share_the_pool_goes_back_to_the_government(self):
        await self.set(dividend_pool=5.0)
        await self.tx(pay_dividends, GUILD)
        cfg = await self.cfg()
        self.assertEqual(cfg["dividend_pool"], 0)
        self.assertAlmostEqual(cfg["treasury"], 5)


class SupplyTests(_SharesTestCase):
    async def total_dc(self):
        users = await self.db.fetchone("SELECT SUM(dragoncoin_cents) AS n FROM users")
        escrow = await self.db.fetchone("SELECT COALESCE(SUM(quantity * price_cents), 0) AS n FROM share_orders")
        return users["n"] + escrow["n"]

    async def test_a_government_sale_mints_exactly_what_the_dragoncoin_was_worth(self):
        before = await circulating_currency_for(self.db, GUILD)
        dc_before = await self.total_dc()
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 7)
        await self.tx(buy_shares, GUILD, ALICE, 5)
        self.assertEqual(await self.total_dc(), dc_before - 35)
        self.assertAlmostEqual(await circulating_currency_for(self.db, GUILD), before + 350)

    async def test_accrual_moves_no_money_and_conversion_burns_exactly_what_it_pays(self):
        await self.grant(ALICE, 3)
        await self.set(dividend_pool=25.0)
        before = await circulating_currency_for(self.db, GUILD)
        dc_before = await self.total_dc()
        await self.tx(pay_dividends, GUILD)
        converted = await self.total_dc() - dc_before          # hundredths minted
        self.assertEqual(converted, 2)                         # 25.00 is 2 hundredths and 5.00 over
        self.assertAlmostEqual(await circulating_currency_for(self.db, GUILD), before - converted * 10)
        self.assertAlmostEqual((await self.cfg())["currency_burned_total"], converted * 10)

    async def test_trades_between_players_mint_and_burn_nothing(self):
        await self.grant(BOB, 5)
        before = await self.cfg()
        dc_before = await self.total_dc()
        await self.tx(list_shares, GUILD, BOB, 2, 30)
        await self.tx(buy_shares, GUILD, ALICE, 2)
        await self.tx(order_shares, GUILD, CARA, 3, 25)
        await self.tx(sell_shares, GUILD, BOB, 3)
        after = await self.cfg()
        self.assertEqual(await self.total_dc(), dc_before)
        self.assertEqual(after["currency_minted_total"], before["currency_minted_total"])
        self.assertEqual(after["currency_burned_total"], before["currency_burned_total"])
        self.assertEqual(await outstanding_shares(self.db, GUILD), 5)


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


class CogTests(_SharesTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog = SharesCog.__new__(SharesCog)
        self.cog.db = self.db
        self.cog.bot = SimpleNamespace(get_guild=lambda guild_id: None)
        self.government = GovernmentCog.__new__(GovernmentCog)
        self.government.db = self.db
        self.government.bot = SimpleNamespace(get_guild=lambda guild_id: None)

    async def test_buying_trades_only_the_shares_of_the_server_it_is_run_in(self):
        await self.tx(open_share_sale, OTHER_GUILD, MAYOR_ID, 5, 10)
        i = FakeInteraction(ALICE, GUILD)
        await SharesCog.shares_buy.callback(self.cog, i, 1)
        self.assertIn("No shares are for sale here", i.refusal)
        i = FakeInteraction(ALICE, OTHER_GUILD)
        await SharesCog.shares_buy.callback(self.cog, i, 1)
        self.assertIsNone(i.refusal)
        self.assertEqual(await held_shares(self.db, OTHER_GUILD, ALICE), 1)
        self.assertEqual(await held_shares(self.db, GUILD, ALICE), 0)

    async def test_every_trading_command_refuses_outside_a_server(self):
        for command, args in (
            (SharesCog.shares_status, ()),
            (SharesCog.shares_buy, (1,)),
            (SharesCog.shares_sell, (1,)),
            (SharesCog.shares_list, (1, 0.1)),
            (SharesCog.shares_order, (1, 0.1)),
            (SharesCog.shares_cancel, ("order:1",)),
        ):
            with self.subTest(command=command.name):
                i = FakeInteraction(ALICE, None)
                await command.callback(self.cog, i, *args)
                self.assertIn("server", i.refusal)

    async def test_the_status_page_and_holdings_render(self):
        await self.tx(open_share_sale, GUILD, MAYOR_ID, 5, 10)
        await self.grant(ALICE, 3)
        i = FakeInteraction(ALICE)
        await SharesCog.shares_status.callback(self.cog, i)
        self.assertIn("3 of 20 shares", i.text())
        self.assertIn("Government sale", i.text())
        i = FakeInteraction(ALICE)
        await SharesCog.shares_holdings.callback(self.cog, i)
        self.assertEqual(i.embed.title, "3 shares in 1 server")

    async def test_the_treasurer_and_mayor_commands(self):
        i = FakeInteraction(TREASURER_ID)
        await GovernmentCog.treasurer_shares.callback(self.government, i, 8)
        self.assertIsNone(i.refusal)
        self.assertEqual((await self.cfg())["share_ceiling"], 8)

        i = FakeInteraction(MAYOR_ID)
        await GovernmentCog.mayor_shares.callback(self.government, i, 3, None)
        self.assertIn("price", i.refusal)
        i = FakeInteraction(MAYOR_ID)
        await GovernmentCog.mayor_shares.callback(self.government, i, 3, 0.25)
        self.assertIsNone(i.refusal)
        self.assertEqual((await self.cfg())["share_sale_price_cents"], 25)

    async def test_the_buyback_command_shows_the_treasury_spend(self):
        await self.set(treasury=100.0)
        await self.grant(BOB, 2)
        await self.tx(list_shares, GUILD, BOB, 1, 1)
        i = FakeInteraction(MAYOR_ID)
        await GovernmentCog.mayor_buyback.callback(self.government, i, 1, 0.01)
        self.assertIsNone(i.refusal)
        self.assertIn("retired **1**", i.embed.description)
        self.assertAlmostEqual((await self.cfg())["treasury"], 90)


if __name__ == "__main__":
    unittest.main()
