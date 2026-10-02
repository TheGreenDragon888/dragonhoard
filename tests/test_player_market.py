"""
Tests for the player market (1.4): /market list, /market order, /market cancel,
and the routing that /market buy and /market sell do across both books.

The first class here is the important one. Everything else pins behaviour a
player would notice; JobBoardCreditTests pins the property the server's whole
currency supply rests on, and it is the one that would be silently wrong.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cogs.economy import (
    BOOK_DISPLAY_LIMIT,
    ENTRIES_DISPLAY_LIMIT,
    EconomyCog,
    ORDERABLE_MATERIALS,
)
from cogs.mining import MiningCog
from data.materials import (
    ALL_MATERIALS,
    DRILLS,
    PERMANENT_MATERIALS,
    PLAYER_PRICE_SCALE,
    JOB_BOARD_MATERIALS,
    SERVER_ONLY_MATERIALS,
    player_price_total,
    player_price_units,
    sale_unit_price,
)
from database.db import Database
from utils.db_helpers import (
    adjust_currency_balance,
    adjust_server_stock,
    adjust_user_quantity,
    circulating_currency_for,
    ensure_server_row,
    ensure_user_row,
    get_currency_balance,
    get_user_quantity,
)
from utils.drills import release_stale_drill_locks
from utils.job_board import ensure_todays_job
from utils.market_book import (
    MARKET_ENTRY_LIFETIME_DAYS,
    MAX_OPEN_LISTINGS,
    MAX_OPEN_ORDERS,
    guilds_with_expired_entries,
    permanent_material_error,
    server_only_error,
)

GUILD = 8080
ALICE = 111
BOB = 222
MEMBERS = 10

# What can go on a player book: everything tradeable except the job board's six
# (SERVER_ONLY_MATERIALS), which trade only with the server.
LISTABLE = tuple(m for m in ORDERABLE_MATERIALS if m not in SERVER_ONLY_MATERIALS)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = f"User{user_id}"


class FakeGuild:
    """Enough of discord.Guild for human_member_count - see
    tests/test_market_sell_db.py, which needs the same thing for the same
    reason."""
    id = GUILD

    async def chunk(self, *, cache=True):
        return []


class FakeInteraction:
    def __init__(self, user_id, guild_id=GUILD):
        self.guild_id = guild_id
        self.guild = FakeGuild()
        self.user = FakeUser(user_id)
        self.response = AsyncMock()

    @property
    def sent(self):
        """The plain-text rejection this interaction received, or None if it
        was answered with an embed instead."""
        call = self.response.send_message.call_args
        if call is None or not call.args:
            return None
        return call.args[0]


class MarketTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        for user in (ALICE, BOB):
            await ensure_user_row(self.db, user)
        self.cog = EconomyCog.__new__(EconomyCog)
        self.cog.db = self.db
        self.cog.bot = None

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    # -- command shims ------------------------------------------------------
    # Each command is wrapped in an app_commands.Command by its decorator, so
    # the cog has to be passed to the raw callback by hand.

    async def list_item(self, user, item, price, quantity=1):
        i = FakeInteraction(user)
        await EconomyCog.market_list.callback(self.cog, i, item, price, quantity)
        return i

    async def order(self, user, item, quantity, price):
        i = FakeInteraction(user)
        await EconomyCog.market_order.callback(self.cog, i, item, quantity, price)
        return i

    async def cancel(self, user, entry):
        """`entry` is what /market cancel's autocomplete submits:
        "listing:<id>" or "order:<id>"."""
        i = FakeInteraction(user)
        await EconomyCog.market_cancel.callback(self.cog, i, entry)
        return i

    async def buy(self, user, material_id, quantity):
        i = FakeInteraction(user)
        await EconomyCog.market_buy.callback(self.cog, i, material_id, quantity)
        return i

    async def sell(self, user, material_id, quantity):
        i = FakeInteraction(user)
        await EconomyCog.market_sell.callback(self.cog, i, material_id, quantity)
        return i

    async def list_directly(self, user, material_id, quantity, price):
        """A listing written straight into the book, escrow and all - for
        building books bigger than one player's MAX_OPEN_LISTINGS allows through
        /market list, like the ones players had before the limit existed."""
        await self.db.execute(
            "INSERT INTO market_listings (guild_id, seller_id, material_id, quantity, price_units, expires_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', '+7 days'))",
            (GUILD, user, material_id, quantity, player_price_units(price)),
        )

    async def list_drill_directly(self, user, price):
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, container_type) "
            "VALUES (NULL, ?, 'diamond_drill', 5, 'diamond_container')", (user,)
        )
        listing_id = await self.db.execute(
            "INSERT INTO market_listings (guild_id, seller_id, drill_id, quantity, price_units, expires_at) "
            "VALUES (?, ?, ?, 1, ?, datetime('now', '+7 days'))",
            (GUILD, user, drill_id, player_price_units(price)),
        )
        await self.db.execute(
            "UPDATE drills SET listed_id = ? WHERE drill_id = ?", (listing_id, drill_id)
        )

    async def order_directly(self, user, material_id, quantity, price):
        """The order-side twin of list_directly, escrow included."""
        units = player_price_units(price)
        await adjust_currency_balance(
            self.db, GUILD, user, -player_price_total(units, quantity)
        )
        await self.db.execute(
            "INSERT INTO market_orders (guild_id, buyer_id, material_id, quantity, price_units, expires_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', '+7 days'))",
            (GUILD, user, material_id, quantity, units),
        )

    async def totals(self):
        row = await self.db.fetchone(
            "SELECT currency_minted_total, currency_burned_total FROM server_config "
            "WHERE guild_id = ?", (GUILD,)
        )
        return row["currency_minted_total"], row["currency_burned_total"]


class JobBoardCreditTests(MarketTestCase):
    """The job board must be credited ONLY for what cleared against the server.

    This is the property that keeps the player market from printing currency.
    The board pays JOB_BOARD_TARGET_PAYOUT per completion with no daily cap,
    and what bounds it is that goods only re-enter the player sector by being
    bought back from the server at twice what selling them paid
    (data/materials.py, under JOB_BOARD_TARGET_PAYOUT). A player-to-player sale
    never removes goods from that sector - so if it credited the board, two
    players could pass one stack back and forth and mint the bonus on every leg
    out of nothing at all.
    """

    async def today(self):
        async with self.db.transaction() as tx:
            return await ensure_todays_job(tx, GUILD, MEMBERS)

    async def progress(self, user):
        row = await self.db.fetchone(
            "SELECT sold, claims_paid FROM daily_job_progress "
            "WHERE guild_id = ? AND user_id = ?", (GUILD, user),
        )
        return (row["sold"], row["claims_paid"]) if row else (0, 0)

    async def bid_directly(self, user, material, quantity, price_units):
        """A standing bid written straight into the book, escrow and all.

        /market order refuses the job board's materials as of 1.4.1, so the
        guard these tests pin can no longer be reached through the command. It
        is kept anyway - it is what makes the rule hold if a row for one of them
        ever reaches the book some other way - and this is how it is reached.
        """
        await adjust_currency_balance(
            self.db, GUILD, user, -player_price_total(price_units, quantity)
        )
        await self.db.execute(
            "INSERT INTO market_orders (guild_id, buyer_id, material_id, quantity, price_units, expires_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', '+7 days'))",
            (GUILD, user, material, quantity, price_units),
        )

    def above_the_server(self, material):
        """One ten-thousandth over what the server pays, so plan_sell prefers
        the bid."""
        return round(sale_unit_price(material) * PLAYER_PRICE_SCALE) + 1

    async def test_ordering_a_job_material_is_refused(self):
        """The 1.4.1 rule that keeps the board's sales away from player bids
        in the first place."""
        job = await self.today()
        await adjust_currency_balance(self.db, GUILD, BOB, 10_000.0)
        interaction = await self.order(BOB, job["material_id"], job["quantity"], 1.0)
        self.assertIn("only with the server", interaction.sent or "")
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_orders"), [])

    async def test_a_sale_to_a_player_credits_the_board_with_nothing(self):
        job = await self.today()
        material, need = job["material_id"], job["quantity"]

        await adjust_currency_balance(self.db, GUILD, BOB, 10_000.0)
        await self.bid_directly(BOB, material, need, self.above_the_server(material))

        await adjust_user_quantity(self.db, ALICE, material, need)
        await self.sell(ALICE, material, need)

        sold, claims = await self.progress(ALICE)
        self.assertEqual(
            (sold, claims), (0, 0),
            "a sale filled entirely by a player bid must not touch the board",
        )

    async def test_a_split_sale_credits_only_the_servers_half(self):
        job = await self.today()
        material, need = job["material_id"], job["quantity"]

        await adjust_currency_balance(self.db, GUILD, BOB, 10_000.0)
        await self.bid_directly(BOB, material, need, self.above_the_server(material))

        # Two tasks' worth: one clears against Bob's bid, one against the server.
        await adjust_user_quantity(self.db, ALICE, material, need * 2)
        await self.sell(ALICE, material, need * 2)

        sold, claims = await self.progress(ALICE)
        self.assertEqual(sold, need, "only the server's half is progress")
        self.assertEqual(claims, 1, "one completion, not two")

    async def test_a_wash_trade_between_two_players_mints_nothing(self):
        """Alice sells to Bob's bid, Bob sells back into Alice's bid. Goods and
        currency end where they started; the point is that the supply does too."""
        job = await self.today()
        material, need = job["material_id"], job["quantity"]
        price = self.above_the_server(material)

        for user in (ALICE, BOB):
            await adjust_currency_balance(self.db, GUILD, user, 10_000.0)
        minted_before, burned_before = await self.totals()
        circulating_before = await circulating_currency_for(self.db, GUILD)

        await adjust_user_quantity(self.db, ALICE, material, need)
        await self.bid_directly(BOB, material, need, price)
        await self.sell(ALICE, material, need)      # goods Alice -> Bob
        await self.bid_directly(ALICE, material, need, price)
        await self.sell(BOB, material, need)        # goods Bob -> Alice

        minted_after, burned_after = await self.totals()
        self.assertEqual(minted_after, minted_before, "a P2P leg mints nothing")
        self.assertEqual(burned_after, burned_before, "a P2P leg burns nothing")
        self.assertAlmostEqual(
            await circulating_currency_for(self.db, GUILD), circulating_before, places=9,
            msg="the money supply is unchanged by goods going round in a circle",
        )
        self.assertEqual(
            await self.progress(ALICE), (0, 0), "no board credit for either leg"
        )
        self.assertEqual(await self.progress(BOB), (0, 0))

    async def test_a_sale_to_the_server_still_credits_the_board(self):
        """The guard above must not have turned the board off entirely."""
        job = await self.today()
        material, need = job["material_id"], job["quantity"]
        await adjust_user_quantity(self.db, ALICE, material, need)
        await self.sell(ALICE, material, need)
        sold, claims = await self.progress(ALICE)
        self.assertEqual((sold, claims), (need, 1))


class ServerOnlyTests(MarketTestCase):
    """The job board's materials are traded only with the server (1.4.1).

    /market sell fills the dearest player bid first and only the server's share
    of a sale counts toward the board, so a bid for the day's material took a
    player's job sale and paid them no progress for it.
    """

    def test_the_rule_is_the_job_boards_materials(self):
        """SERVER_ONLY_MATERIALS must BE the board's list, not a copy of it, so
        a material added to the board comes off the books by the same edit."""
        self.assertEqual(SERVER_ONLY_MATERIALS, frozenset(JOB_BOARD_MATERIALS))
        self.assertEqual(len(SERVER_ONLY_MATERIALS), 6)

    def test_every_one_is_refused_by_both_commands(self):
        for material_id in SERVER_ONLY_MATERIALS:
            with self.subTest(material_id):
                self.assertIsNotNone(server_only_error(material_id))

    def test_nothing_else_is(self):
        for material_id in ALL_MATERIALS:
            if material_id not in SERVER_ONLY_MATERIALS:
                with self.subTest(material_id):
                    self.assertIsNone(server_only_error(material_id))

    async def test_listing_one_is_refused_and_takes_nothing(self):
        for material_id in SERVER_ONLY_MATERIALS:
            with self.subTest(material_id):
                await adjust_user_quantity(self.db, ALICE, material_id, 50)
                interaction = await self.list_item(ALICE, material_id, 1.0, 50)
                self.assertIn("only with the server", interaction.sent or "")
                self.assertEqual(await get_user_quantity(self.db, ALICE, material_id), 50)
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])

    async def test_ordering_one_is_refused_and_holds_nothing(self):
        await adjust_currency_balance(self.db, GUILD, ALICE, 500.0)
        for material_id in SERVER_ONLY_MATERIALS:
            with self.subTest(material_id):
                interaction = await self.order(ALICE, material_id, 10, 1.0)
                self.assertIn("only with the server", interaction.sent or "")
                self.assertEqual(await get_currency_balance(self.db, GUILD, ALICE), 500.0)
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_orders"), [])

    async def test_neither_autocomplete_offers_them(self):
        for material_id in SERVER_ONLY_MATERIALS:
            await adjust_user_quantity(self.db, ALICE, material_id, 50)
        await adjust_user_quantity(self.db, ALICE, "wiring", 5)
        listable = [c.value for c in await self.cog._listable_autocomplete(FakeInteraction(ALICE), "")]
        orderable = [c.value for c in await self.cog._orderable_autocomplete(FakeInteraction(ALICE), "")]
        self.assertEqual(listable, ["wiring"])
        for material_id in SERVER_ONLY_MATERIALS:
            self.assertNotIn(material_id, orderable)

    async def test_selling_and_buying_them_still_goes_to_the_server(self):
        await adjust_user_quantity(self.db, ALICE, "steel", 100)
        await self.sell(ALICE, "steel", 100)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "steel"), 0)

        await adjust_server_stock(self.db, GUILD, "steel", 10)
        await adjust_currency_balance(self.db, GUILD, BOB, 100.0)
        interaction = await self.buy(BOB, "steel", 10)
        self.assertIsNone(interaction.sent)
        self.assertEqual(await get_user_quantity(self.db, BOB, "steel"), 10)


class PricePrecisionTests(unittest.TestCase):
    def test_a_price_finer_than_the_scale_is_refused_rather_than_rounded(self):
        self.assertIsNone(player_price_units(0.00015))
        self.assertIsNone(player_price_units(0.012345))

    def test_prices_on_the_scale_are_exact(self):
        for price, expected in ((0.0001, 1), (0.0101, 101), (0.07, 700), (1.0, 10_000)):
            with self.subTest(price):
                self.assertEqual(player_price_units(price), expected)

    def test_zero_and_negative_are_refused(self):
        self.assertIsNone(player_price_units(0))
        self.assertIsNone(player_price_units(-0.5))

    def test_a_total_is_exact_at_the_largest_quantity_a_command_accepts(self):
        """Integer units times an integer quantity, divided once - the reason
        price_units is stored as an integer at all."""
        self.assertEqual(player_price_total(9999, 1_000_000), 999_900.0)
        self.assertEqual(player_price_total(1, 1_000_000), 100.0)


class PermanentMaterialTests(MarketTestCase):
    """Exotic Matter accrues and is never disposed of."""

    def test_the_rule_is_keyed_on_the_exotic_matter_table(self):
        """PERMANENT_MATERIALS must BE the Exotic Matter category, not a copy
        of its current contents - that is what makes a second exotic material
        inherit every exclusion by being added to one table."""
        from data.materials import PRESS_MATERIALS

        self.assertEqual(set(PERMANENT_MATERIALS), set(PRESS_MATERIALS))

    def test_every_exotic_material_is_refused_by_both_commands(self):
        for material_id in PERMANENT_MATERIALS:
            with self.subTest(material_id):
                self.assertIsNotNone(permanent_material_error(material_id))

    async def test_listing_exotic_matter_is_refused_and_escrows_nothing(self):
        for material_id in PERMANENT_MATERIALS:
            await adjust_user_quantity(self.db, ALICE, material_id, 5)
            interaction = await self.list_item(ALICE, material_id, 1.0, 5)
            self.assertIn("can't be traded", interaction.sent or "")
            self.assertEqual(
                await get_user_quantity(self.db, ALICE, material_id), 5,
                "a refused listing must not take the goods",
            )
            self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])

    async def test_ordering_exotic_matter_is_refused_and_escrows_nothing(self):
        """The order side matters as much as the listing side: a bid nobody may
        fill would hold the buyer's currency against a trade that cannot
        happen."""
        await adjust_currency_balance(self.db, GUILD, ALICE, 500.0)
        for material_id in PERMANENT_MATERIALS:
            interaction = await self.order(ALICE, material_id, 1, 1.0)
            self.assertIn("can't be traded", interaction.sent or "")
            self.assertEqual(await get_currency_balance(self.db, GUILD, ALICE), 500.0)
            self.assertEqual(await self.db.fetchall("SELECT * FROM market_orders"), [])

    def test_it_is_not_orderable_and_neither_are_drills(self):
        from cogs.economy import ORDERABLE_MATERIALS

        self.assertEqual(len(ORDERABLE_MATERIALS), 22)
        for material_id in PERMANENT_MATERIALS:
            self.assertNotIn(material_id, ORDERABLE_MATERIALS)
        for drill_id in DRILLS:
            self.assertNotIn(drill_id, ORDERABLE_MATERIALS)


class EscrowTests(MarketTestCase):
    """Listing holds goods and ordering holds currency, and cancelling returns
    exactly what was held. A book entry that could not be withdrawn would be a
    permanent hole in somebody's inventory."""

    async def test_listing_takes_the_goods_and_cancelling_gives_them_back(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 500)
        interaction = await self.list_item(ALICE, "wiring", 0.60, 200)
        self.assertIsNone(interaction.sent, "the listing should have been accepted")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 300)

        row = await self.db.fetchone("SELECT listing_id FROM market_listings")
        await self.cancel(ALICE, f"listing:{row['listing_id']}")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 500)
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])

    async def test_ordering_takes_the_currency_and_cancelling_refunds_it(self):
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        await self.order(ALICE, "wiring", 100, 0.60)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 40.0)

        row = await self.db.fetchone("SELECT order_id FROM market_orders")
        await self.cancel(ALICE, f"order:{row['order_id']}")
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 100.0)
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_orders"), [])

    async def test_escrowed_currency_still_counts_as_circulating(self):
        """It has left a balance but not the economy - docs/market.md 4."""
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        before = await circulating_currency_for(self.db, GUILD)
        await self.order(ALICE, "wiring", 100, 0.60)
        self.assertAlmostEqual(await circulating_currency_for(self.db, GUILD), before)

    async def test_an_order_mints_and_burns_nothing(self):
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        before = await self.totals()
        await self.order(ALICE, "wiring", 100, 0.60)
        row = await self.db.fetchone("SELECT order_id FROM market_orders")
        await self.cancel(ALICE, f"order:{row['order_id']}")
        self.assertEqual(await self.totals(), before)

    async def test_you_cannot_cancel_somebody_elses(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await self.list_item(ALICE, "wiring", 0.60, 10)
        row = await self.db.fetchone("SELECT listing_id FROM market_listings")
        interaction = await self.cancel(BOB, f"listing:{row['listing_id']}")
        self.assertIn("no listing numbered", interaction.sent or "")
        self.assertEqual(len(await self.db.fetchall("SELECT * FROM market_listings")), 1)

    async def test_cancel_offers_only_your_own_entries(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        await self.list_item(ALICE, "wiring", 0.60, 10)
        await self.list_item(BOB, "wiring", 0.60, 10)
        await self.order(ALICE, "drill_chassis", 5, 0.40)
        listing = await self.db.fetchone(
            "SELECT listing_id FROM market_listings WHERE seller_id = ?", (ALICE,)
        )
        order = await self.db.fetchone("SELECT order_id FROM market_orders")

        choices = await self.cog._cancellable_autocomplete(FakeInteraction(ALICE), "")
        self.assertEqual(
            [c.value for c in choices],
            [f"listing:{listing['listing_id']}", f"order:{order['order_id']}"],
        )
        self.assertTrue(choices[0].name.startswith("Selling 10 Wiring"))
        self.assertTrue(choices[1].name.startswith("Buying 5 Drill Chassis"))

        typed = await self.cog._cancellable_autocomplete(FakeInteraction(ALICE), "chassis")
        self.assertEqual([c.value for c in typed], [f"order:{order['order_id']}"])

    async def test_a_listing_and_an_order_with_the_same_number_are_told_apart(self):
        """The two books number their rows separately, so a bare id could name
        one of each - the prefix is what picks the book."""
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        await self.list_item(ALICE, "wiring", 0.60, 10)
        await self.order(ALICE, "drill_chassis", 10, 0.40)
        listing = await self.db.fetchone("SELECT listing_id FROM market_listings")
        order = await self.db.fetchone("SELECT order_id FROM market_orders")
        self.assertEqual(listing["listing_id"], order["order_id"])

        await self.cancel(ALICE, f"order:{order['order_id']}")
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_orders"), [])
        self.assertEqual(len(await self.db.fetchall("SELECT * FROM market_listings")), 1)

    async def test_cancel_refuses_a_value_not_from_the_list(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await self.list_item(ALICE, "wiring", 0.60, 10)
        row = await self.db.fetchone("SELECT listing_id FROM market_listings")
        interaction = await self.cancel(ALICE, str(row["listing_id"]))
        self.assertIn("Pick one", interaction.sent or "")
        self.assertEqual(len(await self.db.fetchall("SELECT * FROM market_listings")), 1)

    async def test_listing_more_than_you_hold_is_refused(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 5)
        interaction = await self.list_item(ALICE, "wiring", 0.60, 10)
        self.assertIn("only have", interaction.sent or "")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 5)

    async def test_ordering_beyond_your_balance_is_refused(self):
        await adjust_currency_balance(self.db, GUILD, ALICE, 1.0)
        interaction = await self.order(ALICE, "wiring", 100, 0.60)
        self.assertIn("only have", interaction.sent or "")
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 1.0)


class EntryLimitTests(MarketTestCase):
    """At most MAX_OPEN_LISTINGS listings and MAX_OPEN_ORDERS orders per
    player per server (1.4.1). A refused entry takes nothing."""

    OTHER_GUILD = GUILD + 1

    async def fill_listings(self, user=ALICE, count=MAX_OPEN_LISTINGS):
        await adjust_user_quantity(self.db, user, "wiring", count)
        for _ in range(count):
            interaction = await self.list_item(user, "wiring", 5.0, 1)
            self.assertIsNone(interaction.sent, "each listing up to the limit is accepted")

    async def fill_orders(self, user=ALICE, count=MAX_OPEN_ORDERS):
        await adjust_currency_balance(self.db, GUILD, user, 1000.0)
        for _ in range(count):
            interaction = await self.order(user, "wiring", 1, 5.0)
            self.assertIsNone(interaction.sent, "each order up to the limit is accepted")

    async def listing_count(self):
        return len(await self.db.fetchall("SELECT * FROM market_listings"))

    async def test_the_limits_are_five_each(self):
        self.assertEqual((MAX_OPEN_LISTINGS, MAX_OPEN_ORDERS), (5, 5))

    async def test_a_sixth_listing_is_refused_and_takes_nothing(self):
        await self.fill_listings()
        await adjust_user_quantity(self.db, ALICE, "ruby", 1)
        interaction = await self.list_item(ALICE, "ruby", 5000.0, 1)
        self.assertIn("5 listings", interaction.sent or "")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "ruby"), 1)
        self.assertEqual(await self.listing_count(), MAX_OPEN_LISTINGS)

    async def test_a_sixth_order_is_refused_and_holds_nothing(self):
        await self.fill_orders()
        before = await get_currency_balance(self.db, GUILD, ALICE)
        interaction = await self.order(ALICE, "ruby", 1, 100.0)
        self.assertIn("5 orders", interaction.sent or "")
        self.assertEqual(await get_currency_balance(self.db, GUILD, ALICE), before)
        self.assertEqual(
            len(await self.db.fetchall("SELECT * FROM market_orders")), MAX_OPEN_ORDERS
        )

    async def test_the_two_limits_are_separate(self):
        await self.fill_listings()
        await self.fill_orders()

    async def test_a_drill_listing_counts(self):
        await self.fill_listings(count=MAX_OPEN_LISTINGS - 1)
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level) "
            "VALUES (NULL, ?, 'iron_drill', 1)", (ALICE,)
        )
        self.assertIsNone((await self.list_item(ALICE, f"drill:{drill_id}", 50.0)).sent)

        other = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level) "
            "VALUES (NULL, ?, 'iron_drill', 1)", (ALICE,)
        )
        interaction = await self.list_item(ALICE, f"drill:{other}", 50.0)
        self.assertIn("5 listings", interaction.sent or "")
        row = await self.db.fetchone("SELECT listed_id FROM drills WHERE drill_id = ?", (other,))
        self.assertIsNone(row["listed_id"], "the refused drill is not escrowed")

    async def test_cancelling_one_frees_a_slot(self):
        await self.fill_listings()
        row = await self.db.fetchone("SELECT listing_id FROM market_listings LIMIT 1")
        await self.cancel(ALICE, f"listing:{row['listing_id']}")
        await adjust_user_quantity(self.db, ALICE, "ruby", 1)
        self.assertIsNone((await self.list_item(ALICE, "ruby", 5000.0, 1)).sent)

    async def test_a_listing_that_sells_out_frees_a_slot(self):
        await self.fill_listings()
        await adjust_currency_balance(self.db, GUILD, BOB, 100.0)
        await self.buy(BOB, "wiring", 1)
        self.assertEqual(await self.listing_count(), MAX_OPEN_LISTINGS - 1)
        await adjust_user_quantity(self.db, ALICE, "ruby", 1)
        self.assertIsNone((await self.list_item(ALICE, "ruby", 5000.0, 1)).sent)

    async def test_a_partly_filled_listing_still_counts(self):
        await self.fill_listings(count=MAX_OPEN_LISTINGS - 1)
        await adjust_user_quantity(self.db, ALICE, "ruby", 3)
        await self.list_item(ALICE, "ruby", 5000.0, 3)
        await adjust_currency_balance(self.db, GUILD, BOB, 10_000.0)
        await self.buy(BOB, "ruby", 1)
        await adjust_user_quantity(self.db, ALICE, "drill_chassis", 1)
        interaction = await self.list_item(ALICE, "drill_chassis", 5.0, 1)
        self.assertIn("5 listings", interaction.sent or "")

    async def test_it_is_per_player(self):
        await self.fill_listings(ALICE)
        await self.fill_listings(BOB)

    async def test_it_is_per_server(self):
        await self.fill_listings()
        await ensure_server_row(self.db, self.OTHER_GUILD)
        await adjust_user_quantity(self.db, ALICE, "ruby", 1)
        interaction = FakeInteraction(ALICE, guild_id=self.OTHER_GUILD)
        await EconomyCog.market_list.callback(self.cog, interaction, "ruby", 5000.0, 1)
        self.assertIsNone(interaction.response.send_message.call_args.args or None)

    async def test_a_player_already_over_the_limit_keeps_their_entries(self):
        """Entries were uncapped before 1.4.1. Nothing is taken away; they just
        can't add another until they are under the limit."""
        for _ in range(MAX_OPEN_LISTINGS + 3):
            await self.list_directly(ALICE, "wiring", 1, 5.0)
        await adjust_user_quantity(self.db, ALICE, "ruby", 1)
        interaction = await self.list_item(ALICE, "ruby", 5000.0, 1)
        self.assertIn("5 listings", interaction.sent or "")
        self.assertEqual(await self.listing_count(), MAX_OPEN_LISTINGS + 3)

    async def test_entries_shows_the_count_against_the_limit(self):
        await self.fill_listings(count=3)
        await self.fill_orders(count=1)
        interaction = FakeInteraction(ALICE)
        await EconomyCog.market_entries.callback(self.cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        embed = (kwargs.get("embeds") or [kwargs["embed"]])[0]
        names = [field.name for field in embed.fields]
        self.assertTrue(any(name.startswith("Listings · 3/5") for name in names), names)
        self.assertTrue(any(name.startswith("Orders · 1/5") for name in names), names)


class ExpiryTests(MarketTestCase):
    """Listings and orders expire MARKET_ENTRY_LIFETIME_DAYS after they are
    placed (1.4.1). The hourly sweep hands back whatever is left; until it
    runs, an expired entry is already off the books."""

    async def expire_everything(self):
        await self.db.execute(
            "UPDATE market_listings SET expires_at = datetime('now', '-1 minute')"
        )
        await self.db.execute(
            "UPDATE market_orders SET expires_at = datetime('now', '-1 minute')"
        )

    async def sweep(self):
        for guild_id in await guilds_with_expired_entries(self.db):
            await self.cog._expire_guild_entries(guild_id)

    async def notices(self, user):
        return await self.db.fetchall(
            "SELECT notice_key, title, body FROM user_notifications WHERE user_id = ?", (user,)
        )

    async def test_the_lifetime_is_a_week(self):
        self.assertEqual(MARKET_ENTRY_LIFETIME_DAYS, 7)

    async def test_a_new_entry_expires_a_week_from_now(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 1)
        await self.list_item(ALICE, "wiring", 5.0, 1)
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        await self.order(ALICE, "ruby", 1, 50.0)
        for table in ("market_listings", "market_orders"):
            with self.subTest(table):
                row = await self.db.fetchone(
                    f"SELECT (julianday(expires_at) - julianday('now')) AS days FROM {table}"
                )
                self.assertAlmostEqual(row["days"], 7, delta=0.01)

    async def test_an_expired_listing_cannot_be_bought_before_the_sweep(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 5.0, 10)
        await self.expire_everything()
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)
        interaction = await self.buy(ALICE, "wiring", 1)
        self.assertIn("Only 0", interaction.sent or "")

    async def test_an_expired_order_cannot_be_filled_before_the_sweep(self):
        await adjust_currency_balance(self.db, GUILD, BOB, 100.0)
        await self.order(BOB, "wiring", 5, 5.0)
        await self.expire_everything()
        await adjust_user_quantity(self.db, ALICE, "wiring", 5)
        interaction = await self.sell(ALICE, "wiring", 5)
        self.assertIn("Nobody is buying", interaction.sent or "")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 5)

    async def test_an_expired_entry_frees_its_slot_before_the_sweep(self):
        for _ in range(MAX_OPEN_LISTINGS):
            await self.list_directly(ALICE, "wiring", 1, 5.0)
        await self.expire_everything()
        await adjust_user_quantity(self.db, ALICE, "ruby", 1)
        self.assertIsNone((await self.list_item(ALICE, "ruby", 5000.0, 1)).sent)

    async def test_the_sweep_returns_goods_and_currency(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 5.0, 10)
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        await self.order(ALICE, "ruby", 2, 20.0)
        circulating_before = await circulating_currency_for(self.db, GUILD)
        minted_before = await self.totals()

        await self.expire_everything()
        await self.sweep()

        self.assertEqual(await get_user_quantity(self.db, BOB, "wiring"), 10)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 100.0)
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_orders"), [])
        self.assertAlmostEqual(await circulating_currency_for(self.db, GUILD), circulating_before)
        self.assertEqual(await self.totals(), minted_before, "a return mints and burns nothing")

    async def test_a_partly_filled_entry_returns_only_what_is_left(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 5.0, 10)
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)
        await self.buy(ALICE, "wiring", 4)
        await self.expire_everything()
        await self.sweep()
        self.assertEqual(await get_user_quantity(self.db, BOB, "wiring"), 6)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 4)

    async def test_an_expired_drill_listing_puts_the_drill_back(self):
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, container_type) "
            "VALUES (NULL, ?, 'ruby_drill', 3, 'steel_container')", (BOB,)
        )
        await self.list_item(BOB, f"drill:{drill_id}", 900.0)
        await self.expire_everything()
        await self.sweep()
        row = await self.db.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))
        self.assertIsNone(row["listed_id"])
        self.assertEqual((row["owner_id"], row["level"], row["container_type"]),
                         (BOB, 3, "steel_container"))
        notice = (await self.notices(BOB))[0]
        self.assertIn("Lv.3", notice["body"])

    async def test_each_owner_gets_one_notice_naming_everything(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 5.0, 10)
        await adjust_currency_balance(self.db, GUILD, BOB, 100.0)
        await self.order(BOB, "ruby", 2, 20.0)
        await self.expire_everything()
        await self.sweep()

        notices = await self.notices(BOB)
        self.assertEqual(len(notices), 1)
        self.assertIn("10 Wiring", notices[0]["body"])
        self.assertIn("2 Rubies", notices[0]["body"])
        self.assertIn("40.00", notices[0]["body"])
        self.assertEqual(await self.notices(ALICE), [])

    async def test_live_entries_are_untouched_by_the_sweep(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 5.0, 10)
        await self.sweep()
        self.assertEqual(len(await self.db.fetchall("SELECT * FROM market_listings")), 1)
        self.assertEqual(await self.notices(BOB), [])

    async def test_a_server_with_nothing_expired_is_not_visited(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 5.0, 10)
        self.assertEqual(await guilds_with_expired_entries(self.db), [])
        await self.expire_everything()
        self.assertEqual(await guilds_with_expired_entries(self.db), [GUILD])

    async def test_entries_and_receipts_say_when_it_expires(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 1)
        listed = await self.list_item(ALICE, "wiring", 5.0, 1)
        receipt = listed.response.send_message.call_args.kwargs
        receipt = (receipt.get("embeds") or [receipt["embed"]])[0]
        self.assertIn("<t:", receipt.description)

        interaction = FakeInteraction(ALICE)
        await EconomyCog.market_entries.callback(self.cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        embed = (kwargs.get("embeds") or [kwargs["embed"]])[0]
        selling = next(f for f in embed.fields if f.name.startswith("Listings"))
        self.assertIn("expires <t:", selling.value)


class RoutingTests(MarketTestCase):
    """How /market buy and /market sell fill against the player books.

    As of 1.4.1 a material has either a server leg or player legs, never both
    (SERVER_ONLY_MATERIALS), so these run on materials only players trade.
    """

    async def test_the_cheapest_listing_clears_first(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 60)
        await self.list_item(BOB, "wiring", 6.00, 60)
        await adjust_user_quantity(self.db, BOB, "wiring", 40)
        await self.list_item(BOB, "wiring", 5.00, 40)
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)

        await self.buy(ALICE, "wiring", 50)

        # 40 at 5.00, then 10 of the 60 at 6.00.
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, ALICE), 1000.0 - (40 * 5.00 + 10 * 6.00)
        )
        rows = await self.db.fetchall("SELECT quantity, price_units FROM market_listings")
        self.assertEqual([(r["quantity"], r["price_units"]) for r in rows], [(50, 60_000)])

    async def test_the_dearest_bid_clears_first(self):
        await adjust_currency_balance(self.db, GUILD, BOB, 1000.0)
        await self.order(BOB, "wiring", 30, 5.00)
        await self.order(BOB, "wiring", 30, 6.00)
        await adjust_user_quantity(self.db, ALICE, "wiring", 40)

        await self.sell(ALICE, "wiring", 40)

        # 30 at 6.00, then 10 at 5.00.
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, ALICE), 30 * 6.00 + 10 * 5.00
        )
        self.assertEqual(await get_user_quantity(self.db, BOB, "wiring"), 40)

    async def test_you_cannot_fill_your_own_listing(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 50)
        await self.list_item(ALICE, "wiring", 0.50, 50)
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)
        interaction = await self.buy(ALICE, "wiring", 50)
        self.assertIn("Only 0", interaction.sent or "")

    async def test_a_purchase_beyond_the_book_is_refused_outright(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 15)
        await self.list_item(BOB, "wiring", 0.72, 15)
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)

        interaction = await self.buy(ALICE, "wiring", 100)
        self.assertIn("Only 15", interaction.sent or "", "names what the book holds")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 0)
        self.assertEqual(await get_currency_balance(self.db, GUILD, ALICE), 1000.0)

    async def test_a_player_trade_mints_and_burns_nothing(self):
        """A gemstone has no server leg at all, which is the point of letting
        players trade one."""
        await adjust_user_quantity(self.db, BOB, "ruby", 2)
        await self.list_item(BOB, "ruby", 4000.0, 2)
        await adjust_currency_balance(self.db, GUILD, ALICE, 10_000.0)

        minted_before, burned_before = await self.totals()
        await self.buy(ALICE, "ruby", 2)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "ruby"), 2)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 8000.0)
        self.assertEqual(await self.totals(), (minted_before, burned_before))

    async def test_a_partly_filled_listing_keeps_the_rest(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 100)
        await self.list_item(BOB, "wiring", 0.72, 100)
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)
        await self.buy(ALICE, "wiring", 40)

        row = await self.db.fetchone("SELECT quantity FROM market_listings")
        self.assertEqual(row["quantity"], 60)

    async def test_a_fully_filled_listing_is_deleted(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 100)
        await self.list_item(BOB, "wiring", 0.72, 100)
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)
        await self.buy(ALICE, "wiring", 100)
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])



class MarketTaxTests(MarketTestCase):
    """The Market Tax (utils/government.py: pay_market_seller): a share of every
    player-to-player trade the government holds, out of what the SELLER
    receives. The buyer pays the quoted price at any rate, and a trade with the
    server carries none."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_rate(10.0)

    async def set_rate(self, percent):
        await self.db.execute(
            "UPDATE server_config SET market_tax_percent = ? WHERE guild_id = ?", (percent, GUILD)
        )

    async def held(self):
        row = await self.db.fetchone(
            "SELECT treasury, repayment_pool FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        return row["treasury"] + row["repayment_pool"]

    async def test_buying_from_a_listing_pays_the_seller_less_the_tax(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 0.50, 10)
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)

        await self.buy(ALICE, "wiring", 10)

        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 95.0,
                               msg="the buyer pays the listed price, nothing on top")
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 4.5)
        self.assertAlmostEqual(await self.held(), 0.5)

    async def test_selling_into_a_bid_pays_the_seller_less_the_tax(self):
        await adjust_currency_balance(self.db, GUILD, BOB, 100.0)
        await self.order(BOB, "wiring", 10, 0.50)
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)

        interaction = await self.sell(ALICE, "wiring", 10)

        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 4.5)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 95.0)
        self.assertAlmostEqual(await self.held(), 0.5)
        kwargs = interaction.response.send_message.call_args.kwargs
        embed = (kwargs.get("embeds") or [kwargs["embed"]])[0]
        received = next(f for f in embed.fields if f.name == "Received")
        self.assertIn("4.50", received.value, "the receipt shows what actually arrived")
        self.assertIn("10% Market Tax", embed.description)

    async def test_a_listed_drill_pays_its_seller_less_the_tax(self):
        await self.list_drill_directly(BOB, 250.0)
        row = await self.db.fetchone("SELECT listing_id FROM market_listings")
        await adjust_currency_balance(self.db, GUILD, ALICE, 1000.0)

        await self.buy(ALICE, f"listing:{row['listing_id']}", 1)

        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 750.0)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 225.0)
        self.assertAlmostEqual(await self.held(), 25.0)

    async def test_a_sale_to_the_server_carries_none(self):
        material = next(iter(SERVER_ONLY_MATERIALS))
        await adjust_user_quantity(self.db, ALICE, material, 1)
        minted_before, _ = await self.totals()
        await self.sell(ALICE, material, 1)
        minted_after, _ = await self.totals()
        self.assertAlmostEqual(minted_after - minted_before, sale_unit_price(material))
        self.assertEqual(await self.held(), 0.0)

    async def test_a_purchase_from_the_server_carries_none(self):
        material = next(iter(SERVER_ONLY_MATERIALS))
        await adjust_server_stock(self.db, GUILD, material, 5)
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        _, burned_before = await self.totals()
        await self.buy(ALICE, material, 1)
        _, burned_after = await self.totals()
        self.assertAlmostEqual(burned_after - burned_before, 100.0 - await get_currency_balance(self.db, GUILD, ALICE))
        self.assertEqual(await self.held(), 0.0)

    async def test_the_tax_mints_and_burns_nothing(self):
        await adjust_user_quantity(self.db, BOB, "ruby", 2)
        await self.list_item(BOB, "ruby", 4000.0, 2)
        await adjust_currency_balance(self.db, GUILD, ALICE, 10_000.0)
        circulating_before = await circulating_currency_for(self.db, GUILD)
        totals_before = await self.totals()

        await self.buy(ALICE, "ruby", 2)

        self.assertEqual(await self.totals(), totals_before)
        self.assertAlmostEqual(
            await circulating_currency_for(self.db, GUILD), circulating_before, places=9,
            msg="the held tax is still in circulation",
        )

    async def test_the_rate_is_the_one_in_force_when_it_sells(self):
        await self.set_rate(0.0)
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        await self.list_item(BOB, "wiring", 1.00, 10)
        await self.set_rate(20.0)
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)

        await self.buy(ALICE, "wiring", 10)

        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 8.0)

    async def test_a_fraction_of_a_unit_stays_with_the_seller(self):
        # 3 units at 6.25% is 0.1875 of a unit of tax, which rounds down to 0.
        await self.set_rate(6.25)
        await adjust_user_quantity(self.db, BOB, "wiring", 3)
        await self.list_item(BOB, "wiring", 0.0001, 3)
        await adjust_currency_balance(self.db, GUILD, ALICE, 1.0)

        await self.buy(ALICE, "wiring", 3)

        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 0.0003)
        self.assertEqual(await self.held(), 0.0)

    async def test_the_listing_receipt_shows_what_the_seller_would_receive(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        interaction = await self.list_item(BOB, "wiring", 0.50, 10)
        kwargs = interaction.response.send_message.call_args.kwargs
        embed = (kwargs.get("embeds") or [kwargs["embed"]])[0]
        field = next(f for f in embed.fields if "Market Tax" in f.name)
        self.assertIn("4.5", field.value)

    async def test_no_tax_leaves_the_listing_receipt_as_it_was(self):
        await self.set_rate(0.0)
        await adjust_user_quantity(self.db, BOB, "wiring", 10)
        interaction = await self.list_item(BOB, "wiring", 0.50, 10)
        kwargs = interaction.response.send_message.call_args.kwargs
        embed = (kwargs.get("embeds") or [kwargs["embed"]])[0]
        self.assertFalse(any("Market Tax" in f.name for f in embed.fields))

class ListedDrillTests(MarketTestCase):
    """A listed drill is escrowed, and every command that acts on a drill has
    to refuse it - otherwise it can be placed, upgraded or scrapped out from
    under its own listing.

    One test per command rather than one test of the helper, because the bug
    this guards against is a call site that was never routed through the helper
    at all (utils/drills.py: drill_unavailable_reason).
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mining = MiningCog.__new__(MiningCog)
        self.mining.db = self.db
        self.mining.bot = None
        self.drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level) "
            "VALUES (NULL, ?, 'iron_drill', 1)", (ALICE,)
        )

    async def list_the_drill(self, price=250.0):
        interaction = await self.list_item(ALICE, f"drill:{self.drill_id}", price)
        self.assertIsNone(interaction.sent, "the drill listing should have been accepted")
        return interaction

    async def listed_id(self):
        row = await self.db.fetchone(
            "SELECT listed_id FROM drills WHERE drill_id = ?", (self.drill_id,)
        )
        return row["listed_id"]

    async def test_listing_a_drill_escrows_it(self):
        await self.list_the_drill()
        self.assertIsNotNone(await self.listed_id())

    async def test_a_listed_drill_cannot_be_placed(self):
        await self.list_the_drill()
        i = FakeInteraction(ALICE)
        await MiningCog.mine_place.callback(self.mining, i, self.drill_id)
        self.assertIn("listed on the player market", i.sent or "")

    async def test_a_listed_drill_cannot_have_a_container_fitted(self):
        await adjust_user_quantity(self.db, ALICE, "iron_container", 1)
        await self.list_the_drill()
        i = FakeInteraction(ALICE)
        await MiningCog.mine_attach.callback(self.mining, i, self.drill_id, "iron_container")
        self.assertIn("listed on the player market", i.sent or "")

    async def test_a_listed_drill_cannot_be_queued_for_an_upgrade(self):
        from cogs.factory import FactoryCog

        factory = FactoryCog.__new__(FactoryCog)
        factory.db = self.db
        factory.bot = None
        await self.list_the_drill()
        i = FakeInteraction(ALICE)
        await FactoryCog.factory_upgrade.callback(factory, i, self.drill_id)
        self.assertIn("listed on the player market", i.sent or "")

    async def test_a_listed_drill_cannot_be_scrapped(self):
        from cogs.scrapper import ScrapperCog

        scrapper = ScrapperCog.__new__(ScrapperCog)
        scrapper.db = self.db
        scrapper.bot = None
        await self.list_the_drill()
        i = FakeInteraction(ALICE)
        await ScrapperCog.scrapper_drill.callback(scrapper, i, self.drill_id)
        self.assertIn("listed on the player market", i.sent or "")

    async def test_a_listed_drill_is_not_offered_by_autocomplete(self):
        from utils.drills import DrillScope, drill_choices

        before = await drill_choices(self.db, ALICE, "", scope=DrillScope.UNPLACED)
        self.assertEqual(len(before), 1)
        await self.list_the_drill()
        after = await drill_choices(self.db, ALICE, "", scope=DrillScope.UNPLACED)
        self.assertEqual(after, [], "an escrowed drill is not a drill you can act on")

    async def test_the_stale_lock_sweep_leaves_a_listing_alone(self):
        """release_stale_drill_locks frees any locked_job_id that doesn't name
        a live job. A listing is not a job, which is exactly why listed_id is
        its own column - reusing locked_job_id would have handed the drill back
        while it was still on the book."""
        await self.list_the_drill()
        listing = await self.listed_id()
        await release_stale_drill_locks(self.db)
        self.assertEqual(await self.listed_id(), listing)

    async def test_cancelling_returns_the_drill(self):
        await self.list_the_drill()
        row = await self.db.fetchone("SELECT listing_id FROM market_listings")
        await self.cancel(ALICE, f"listing:{row['listing_id']}")
        self.assertIsNone(await self.listed_id())
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])

    async def test_buying_a_listed_drill_transfers_it_intact(self):
        await self.db.execute(
            "UPDATE drills SET level = 3, container_type = 'steel_container' WHERE drill_id = ?",
            (self.drill_id,),
        )
        await self.list_the_drill(250.0)
        row = await self.db.fetchone("SELECT listing_id FROM market_listings")
        await adjust_currency_balance(self.db, GUILD, BOB, 1000.0)

        minted_before, burned_before = await self.totals()
        await self.buy(BOB, f"listing:{row['listing_id']}", 1)

        drill = await self.db.fetchone(
            "SELECT * FROM drills WHERE drill_id = ?", (self.drill_id,)
        )
        self.assertEqual(drill["owner_id"], BOB)
        self.assertIsNone(drill["listed_id"])
        self.assertEqual(drill["level"], 3, "level survives the sale")
        self.assertEqual(drill["container_type"], "steel_container", "so does the container")
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 250.0)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 750.0)
        self.assertEqual(
            await self.totals(), (minted_before, burned_before),
            "a drill changing hands is a transfer, not a faucet",
        )

    async def test_a_placed_drill_cannot_be_listed(self):
        await self.db.execute(
            "UPDATE drills SET guild_id = ? WHERE drill_id = ?", (GUILD, self.drill_id)
        )
        i = await self.list_item(ALICE, f"drill:{self.drill_id}", 250.0)
        self.assertIn("placed in a server", i.sent or "")
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])

    async def test_you_cannot_list_a_drill_you_do_not_own(self):
        i = await self.list_item(BOB, f"drill:{self.drill_id}", 250.0)
        self.assertIn("don't own that drill", i.sent or "")


class GuildRemovalTests(MarketTestCase):
    async def test_leaving_a_server_returns_every_escrow(self):
        mining = MiningCog.__new__(MiningCog)
        mining.db = self.db
        mining.bot = None

        await adjust_user_quantity(self.db, ALICE, "wiring", 100)
        await self.list_item(ALICE, "wiring", 0.60, 100)
        await adjust_currency_balance(self.db, GUILD, BOB, 100.0)
        await self.order(BOB, "wiring", 100, 0.60)

        withdrawn = await mining._withdraw_guild_market(GUILD)

        self.assertEqual(withdrawn, 2)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 100)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, BOB), 100.0)
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_listings"), [])
        self.assertEqual(await self.db.fetchall("SELECT * FROM market_orders"), [])


class AutocompleteTests(MarketTestCase):
    """The item lists offer only what the command could actually do right now.

    Both commands are all-or-nothing against the books, so a name you cannot
    trade is a name whose only possible outcome is a rejection. With sixteen of
    the twenty-two materials being ones the server never touches, an unfiltered
    list would be mostly dead ends on a quiet server.
    """

    async def buyable(self, user=ALICE):
        return [c.name for c in await self.cog._buyable_autocomplete(FakeInteraction(user), "")]

    async def sellable(self, user=ALICE):
        return [c.name for c in await self.cog._sellable_autocomplete(FakeInteraction(user), "")]

    async def test_an_empty_server_offers_nothing_either_way(self):
        self.assertEqual(await self.buyable(), [])
        self.assertEqual(await self.sellable(), [])

    async def test_buying_offers_only_what_has_stock_behind_it(self):
        await adjust_server_stock(self.db, GUILD, "iron", 200)
        names = await self.buyable()
        self.assertTrue(any(n.startswith("Iron -") for n in names))
        self.assertFalse(
            any(n.startswith("Steel") for n in names),
            "the server holds no steel and nobody has listed any",
        )

    async def test_buying_offers_a_material_the_server_never_stocks(self):
        """The point of the player book: a ruby has no server leg at all."""
        await adjust_user_quantity(self.db, BOB, "ruby", 3)
        await self.list_item(BOB, "ruby", 4800.0, 3)
        self.assertTrue(any(n.startswith("Ruby -") for n in await self.buyable()))

    async def test_buying_quotes_the_cheapest_listing_and_the_combined_total(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 150)
        await self.list_item(BOB, "wiring", 0.90, 50)
        await self.list_item(BOB, "wiring", 0.72, 100)
        wiring = next(n for n in await self.buyable() if n.startswith("Wiring"))
        # 150 listed in all, at the cheaper of the two asks.
        self.assertIn("150", wiring)
        self.assertIn("0.72", wiring)

    async def test_buying_excludes_your_own_listings(self):
        """plan_buy will not fill against them, so offering them would name
        stock the caller cannot buy."""
        await adjust_user_quantity(self.db, ALICE, "ruby", 3)
        await self.list_item(ALICE, "ruby", 4800.0, 3)
        self.assertEqual(await self.buyable(ALICE), [])
        self.assertTrue(any(n.startswith("Ruby -") for n in await self.buyable(BOB)))

    async def test_a_listed_drill_is_offered_with_its_level_and_container(self):
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, container_type) "
            "VALUES (NULL, ?, 'ruby_drill', 2, 'steel_container')", (BOB,)
        )
        await self.list_item(BOB, f"drill:{drill_id}", 900.0)
        drill = next(n for n in await self.buyable() if "Drill" in n)
        self.assertIn("Lv.2", drill)
        self.assertIn("Steel Container", drill)

    async def test_selling_offers_only_what_you_hold(self):
        await adjust_user_quantity(self.db, ALICE, "coal", 500)
        names = await self.sellable()
        self.assertTrue(any(n.startswith("Coal -") for n in names))
        self.assertFalse(any(n.startswith("Iron ") for n in names))

    async def test_selling_omits_what_you_hold_but_nobody_buys(self):
        """Wiring is not in TRADEABLE_ORDER, so the server will not take it.
        With no standing bid either, this genuinely cannot be sold - and
        `/market list` is the command for that, not this one."""
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        self.assertEqual(await self.sellable(), [])

    async def test_a_standing_bid_makes_an_untraded_material_sellable(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await adjust_currency_balance(self.db, GUILD, BOB, 500.0)
        await self.order(BOB, "wiring", 5, 12.5)
        self.assertTrue(any(n.startswith("Wiring -") for n in await self.sellable()))

    async def test_selling_quotes_the_dearest_bid(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 100)
        await adjust_currency_balance(self.db, GUILD, BOB, 500.0)
        await self.order(BOB, "wiring", 30, 0.60)
        await self.order(BOB, "wiring", 30, 0.80)
        wiring = next(n for n in await self.sellable() if n.startswith("Wiring"))
        self.assertIn("0.80", wiring, "the dearer of the two bids")

    async def test_selling_excludes_your_own_bids(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await adjust_currency_balance(self.db, GUILD, ALICE, 500.0)
        await self.order(ALICE, "wiring", 5, 12.5)
        self.assertEqual(
            await self.sellable(), [], "you cannot fill your own bid, so it is not a buyer"
        )

    async def test_exotic_matter_is_never_offered_by_either(self):
        for material_id in PERMANENT_MATERIALS:
            await adjust_user_quantity(self.db, ALICE, material_id, 5)
            await adjust_server_stock(self.db, GUILD, material_id, 5)
        self.assertEqual(await self.sellable(), [])
        self.assertEqual(await self.buyable(), [])


class SellShortfallTests(MarketTestCase):
    """/market sell is all-or-nothing, like /market buy.

    This exists because it once was not. `plan_sell` returned a shortfall and
    market_sell discarded it, while the deduction below took the full quantity
    regardless - so selling something nobody was bidding for destroyed the
    goods and paid nothing. The autocomplete now hides that case, but an
    autocomplete is never the enforcement: a submitted value need not have come
    from it.

    A shortfall is only possible outside TRADEABLE_ORDER, since the server
    accepts any quantity of the six it trades.
    """

    async def test_selling_with_no_buyer_at_all_keeps_the_goods(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        interaction = await self.sell(ALICE, "wiring", 10)
        self.assertIn("Nobody is buying", interaction.sent or "")
        self.assertEqual(
            await get_user_quantity(self.db, ALICE, "wiring"), 10,
            "goods must survive a sale that could not happen",
        )
        self.assertEqual(await get_currency_balance(self.db, GUILD, ALICE), 0.0)

    async def test_a_partial_bid_does_not_eat_the_remainder(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await adjust_currency_balance(self.db, GUILD, BOB, 500.0)
        await self.order(BOB, "wiring", 4, 12.5)

        interaction = await self.sell(ALICE, "wiring", 10)
        self.assertIn("Only 4", interaction.sent or "")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 10)
        self.assertEqual(await get_currency_balance(self.db, GUILD, ALICE), 0.0)

    async def test_selling_exactly_what_is_bid_for_goes_through(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await adjust_currency_balance(self.db, GUILD, BOB, 500.0)
        await self.order(BOB, "wiring", 4, 12.5)

        await self.sell(ALICE, "wiring", 4)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 6)
        self.assertAlmostEqual(await get_currency_balance(self.db, GUILD, ALICE), 50.0)
        self.assertEqual(await get_user_quantity(self.db, BOB, "wiring"), 4)

    async def test_your_own_bid_is_not_a_buyer(self):
        """The self-trade exclusion and the shortfall check meet here: your own
        order is skipped by plan_sell, so it leaves a shortfall rather than
        filling."""
        await adjust_user_quantity(self.db, ALICE, "wiring", 10)
        await adjust_currency_balance(self.db, GUILD, ALICE, 500.0)
        await self.order(ALICE, "wiring", 5, 12.5)

        interaction = await self.sell(ALICE, "wiring", 5)
        self.assertIn("Nobody is buying", interaction.sent or "")
        self.assertEqual(await get_user_quantity(self.db, ALICE, "wiring"), 10)

    async def test_a_tradeable_material_never_comes_up_short(self):
        """The server's appetite is unlimited for the six it trades, so this
        path can't be reached for them however much is sold."""
        await adjust_user_quantity(self.db, ALICE, "steel", 5000)
        interaction = await self.sell(ALICE, "steel", 5000)
        self.assertIsNone(interaction.sent)
        self.assertEqual(await get_user_quantity(self.db, ALICE, "steel"), 0)


class EmbedBudgetTests(MarketTestCase):
    """/market status has to fit inside Discord's embed limits on the busiest
    book the game can produce.

    Discord rejects an over-length embed outright - the message fails, it is
    not truncated - so this is the difference between a crowded page and a
    command that does not work at all. It got within 54 characters of the
    ceiling during 1.4's development on a server with a long animated currency
    emoji, which is why the emoji is named once per FIELD rather than once per
    line.

    The worst case is every listable material on both books at once, plus a
    full drill field, which is what this builds.
    """

    LONG_EMOJI = "<a:SuperLongAnimatedCurrencyName:1533722773418016868>"

    async def build_maximal_book(self, currency_emoji):
        await self.db.execute(
            "UPDATE server_config SET currency_emoji = ? WHERE guild_id = ?",
            (currency_emoji, GUILD),
        )
        await adjust_currency_balance(self.db, GUILD, ALICE, 10 ** 9)
        # Written straight into the books: no one player may hold this many
        # entries (MAX_OPEN_LISTINGS), but a server full of players can.
        for material_id in LISTABLE:
            await self.list_directly(BOB, material_id, 1000, 1000.0)
            await self.order_directly(ALICE, material_id, 50, 500.0)
        # More drills than the field will name, so the "and N more" line is in.
        for _ in range(BOOK_DISPLAY_LIMIT + 5):
            await self.list_drill_directly(BOB, 50000.0)

    async def render(self):
        interaction = FakeInteraction(ALICE)
        await EconomyCog.market_status.callback(self.cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        return (kwargs.get("embeds") or [kwargs["embed"]])[0]

    async def test_the_busiest_book_fits_with_a_long_custom_currency_emoji(self):
        await self.build_maximal_book(self.LONG_EMOJI)
        embed = await self.render()
        total = (
            len(embed.title or "") + len(embed.description or "")
            + sum(len(f.name) + len(f.value) for f in embed.fields)
        )
        self.assertLessEqual(total, 6000, f"embed renders to {total} characters")

    async def test_no_single_field_exceeds_the_per_field_limit(self):
        """add_multi_field splits at 1024; this is the assertion that it is
        actually being used for these fields rather than a bare add_field."""
        await self.build_maximal_book(self.LONG_EMOJI)
        for field in (await self.render()).fields:
            with self.subTest(field=field.name):
                self.assertLessEqual(len(field.value), 1024)

    async def test_every_material_on_the_book_is_named(self):
        """The reason the books are aggregated per material. Listing row by row
        let one heavily-undercut material fill the field and hide every other
        one behind an "... and N more" - a player looking for steel saw nothing
        and concluded there was none for sale."""
        await self.build_maximal_book(None)
        embed = await self.render()
        book = "\n".join(
            f.value for f in embed.fields if f.name.startswith("Listings")
        )
        for material_id in LISTABLE:
            with self.subTest(material_id):
                self.assertIn(ALL_MATERIALS[material_id]["emoji"], book)

    async def test_an_undercut_shows_the_best_price_and_the_depth_behind_it(self):
        """Five listings undercutting each other are one line, not five. Only
        the cheapest is fillable - plan_buy takes it first - so the rest are not
        actionable, and what a buyer wants instead is how much is behind it."""
        for i in range(5):
            await adjust_user_quantity(self.db, BOB, "wiring", 50)
            await self.list_item(BOB, "wiring", round(0.95 - i * 0.01, 4), 50)
        embed = await self.render()
        book = "\n".join(
            f.value for f in embed.fields if f.name.startswith("Listings")
        )
        wiring = [ln for ln in book.splitlines() if ALL_MATERIALS["wiring"]["emoji"] in ln]
        self.assertEqual(len(wiring), 1, "one line per material, however many sellers")
        self.assertIn("0.9100", wiring[0], "the cheapest ask, which is the fillable one")
        self.assertIn("250", wiring[0], "the total depth behind it")
        self.assertIn("1 seller", wiring[0], "all five listings are Bob's")

    async def test_status_carries_only_the_market_not_your_own_entries(self):
        """/market status answers "what is the market doing". What the caller
        personally has on the book is /market entries, which is a different
        question and a per-viewer read this page should not be paying for."""
        await adjust_user_quantity(self.db, ALICE, "wiring", 50)
        await self.list_item(ALICE, "wiring", 0.72, 50)
        names = [f.name for f in (await self.render()).fields]
        self.assertTrue(any(n.startswith("Server") for n in names))
        self.assertTrue(any(n.startswith("Listings") for n in names))
        self.assertTrue(any(n.startswith("Orders") for n in names))
        self.assertFalse(
            any("Your" in n or "Entries" in n for n in names),
            f"status should carry no per-viewer field, got {names}",
        )


class MarketEntriesTests(MarketTestCase):
    """/market entries: what the caller personally has on the books.

    Split out of /market status, which answers the other question - what the
    market is doing - and is run by everyone to check prices, where this is a
    per-viewer read only the person acting on their own entries needs.
    """

    async def entries(self, user=ALICE):
        interaction = FakeInteraction(user)
        await EconomyCog.market_entries.callback(self.cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        return (kwargs.get("embeds") or [kwargs["embed"]])[0]

    def field(self, embed, prefix):
        return next((f for f in embed.fields if f.name.startswith(prefix)), None)

    def field_text(self, embed, prefix):
        """Every field under one heading joined, including the "(cont.)" ones
        add_multi_field splits off at 1024 characters - so an assertion about
        a long list doesn't silently read only its first chunk."""
        return "\n".join(f.value for f in embed.fields if f.name.startswith(prefix))

    async def test_an_empty_page_says_so_and_points_at_both_commands(self):
        embed = await self.entries()
        empty = self.field(embed, "Nothing on the market")
        self.assertIsNotNone(empty)
        self.assertIn("/market list", empty.value)
        self.assertIn("/market order", empty.value)

    async def test_a_listing_appears_with_the_id_cancel_takes(self):
        await adjust_user_quantity(self.db, ALICE, "wiring", 50)
        await self.list_item(ALICE, "wiring", 0.72, 50)
        row = await self.db.fetchone("SELECT listing_id FROM market_listings")
        selling = self.field(await self.entries(), "Listings")
        self.assertIn(f"#{row['listing_id']}", selling.value)
        # The material's EMOJI, not its name. Asserting on "Wiring" would pass
        # whether or not the line named the material, because the custom emoji
        # markup is <:Wiring:...> and contains that word.
        self.assertIn(ALL_MATERIALS["wiring"]["emoji"], selling.value)
        self.assertIn("0.7200", selling.value)
        self.assertIn("50", selling.value)

    async def test_a_line_carries_no_material_name(self):
        """Names are proportional-width text: including one shifts every
        column after it by a different amount per line and defeats the
        alignment format_compact_price exists to provide. /market status leaves
        them out for the same reason."""
        await adjust_user_quantity(self.db, ALICE, "drill_chassis", 500)
        await self.list_item(ALICE, "drill_chassis", 0.0155, 500)
        selling = self.field(await self.entries(), "Listings")
        emoji = ALL_MATERIALS["drill_chassis"]["emoji"]
        self.assertNotIn("Drill Chassis", selling.value.replace(emoji, ""))

    async def test_an_entry_reads_the_same_way_as_the_market_book(self):
        """The two pages share one line shape - id (where there is one), the
        material's emoji, a fixed-width price, then the counts - so a player
        reads /market entries the same way they read /market status."""
        await adjust_user_quantity(self.db, ALICE, "wiring", 50)
        await self.list_item(ALICE, "wiring", 0.72, 50)

        entry = self.field(await self.entries(), "Listings").value.splitlines()[0]
        status = FakeInteraction(ALICE)
        await EconomyCog.market_status.callback(self.cog, status)
        kwargs = status.response.send_message.call_args.kwargs
        embed = (kwargs.get("embeds") or [kwargs["embed"]])[0]
        book = next(
            f for f in embed.fields if f.name.startswith("Listings")
        ).value.splitlines()[0]

        emoji = ALL_MATERIALS["wiring"]["emoji"]
        self.assertTrue(entry.startswith("`#"), "an entry leads with its id")
        self.assertTrue(book.startswith(emoji), "the book has no id to lead with")
        # Past the id, the two are the same construction.
        self.assertEqual(entry.split("` ", 1)[1].split(" · ")[0], f"{emoji} `0.7200`")
        self.assertEqual(book.split(" · ")[0], f"{emoji} `0.7200`")

    async def test_an_order_shows_what_it_is_holding(self):
        """The figure a player is looking for when they wonder where their
        balance went - the escrow has left it but not the economy."""
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        await self.order(ALICE, "iron_drill_bit", 500, 0.05)
        embed = await self.entries()
        self.assertIn("Held in bids", embed.description)
        buying = self.field(embed, "Orders")
        self.assertIn("500", buying.value)
        self.assertIn("25.000", buying.value, "500 x 0.05 = 25.00 escrowed")

    async def test_a_listed_drill_is_named_by_what_it_is(self):
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, container_type) "
            "VALUES (NULL, ?, 'ruby_drill', 3, 'steel_container')", (ALICE,)
        )
        await self.list_item(ALICE, f"drill:{drill_id}", 900.0)
        selling = self.field(await self.entries(), "Listings")
        self.assertIn("Lv.3", selling.value)
        self.assertIn("Steel Container", selling.value)

    async def test_it_shows_only_your_own(self):
        await adjust_user_quantity(self.db, BOB, "wiring", 50)
        await self.list_item(BOB, "wiring", 0.72, 50)
        await adjust_currency_balance(self.db, GUILD, BOB, 100.0)
        await self.order(BOB, "iron_drill_bit", 100, 0.05)

        embed = await self.entries(ALICE)
        self.assertIsNotNone(self.field(embed, "Nothing on the market"))
        self.assertIsNone(self.field(embed, "Listings"))

    async def test_the_balance_shown_excludes_what_bids_are_holding(self):
        await adjust_currency_balance(self.db, GUILD, ALICE, 100.0)
        await self.order(ALICE, "iron_drill_bit", 500, 0.05)
        embed = await self.entries()
        # 100.00 - 25.00 escrowed; the two lines together account for the lot.
        self.assertIn("75.00", embed.description)
        self.assertIn("25.00", embed.description)

    async def test_a_page_full_of_entries_still_fits(self):
        """The page lifts BOOK_DISPLAY_LIMIT to ENTRIES_DISPLAY_LIMIT because
        it no longer shares an embed - so the larger cap has to fit too. Only a
        player who had this many entries before MAX_OPEN_LISTINGS existed can
        reach it, and they keep them, so it still has to render."""
        await self.db.execute(
            "UPDATE server_config SET currency_emoji = ? WHERE guild_id = ?",
            (EmbedBudgetTests.LONG_EMOJI, GUILD),
        )
        await adjust_currency_balance(self.db, GUILD, ALICE, 10 ** 9)
        for _ in range(ENTRIES_DISPLAY_LIMIT + 5):
            await self.list_directly(ALICE, "wiring", 10, 0.72)
            await self.order_directly(ALICE, "iron_drill_bit", 10, 0.05)

        embed = await self.entries()
        total = (
            len(embed.title or "") + len(embed.description or "")
            + sum(len(f.name) + len(f.value) for f in embed.fields)
        )
        self.assertLessEqual(total, 6000, f"embed renders to {total} characters")
        for field in embed.fields:
            with self.subTest(field=field.name):
                self.assertLessEqual(len(field.value), 1024)
        self.assertIn("more", self.field_text(embed, "Listings"))
