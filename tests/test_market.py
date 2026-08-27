"""
Tests for the market's pricing arithmetic as the player sees it.

max_affordable is what /market buy quotes back when someone asks for more than
they can pay for, so it has to be exactly right in both directions: quoting one
too many sends them into a purchase that fails, and quoting one too few is the
confusion the message exists to remove. Pure arithmetic - no database.
"""
import unittest

from cogs.economy import (
    EconomyCog,
    MAX_MARKET_QUANTITY,
    TRADEABLE_MATERIALS,
    max_affordable,
)
from data.materials import (
    ALL_MATERIALS,
    GEMSTONES,
    JOB_BOARD_MATERIALS,
    MARKET_PRICE_CENTS,
    ORES,
    RAW_MATERIALS,
    SMELTED_MARKUP,
    SMELTED_MATERIALS,
    TRADEABLE_ORDER,
    purchase_total,
    purchase_unit_price,
    raw_input_cost,
    sale_total,
    sale_unit_price,
)
from utils.embeds import MARKET_COLOR
from utils.formatting import DEFAULT_CURRENCY_EMOJI
from utils.receipts import build_market_receipt_embed


class TradeableOrderTests(unittest.TestCase):
    """The market's display order. One mapping drives all three surfaces -
    /market status's lines and both /market sell's and /market buy's choice
    lists - so these pin the order once for all of them."""

    def test_it_is_exactly_the_ores_and_smelted_materials(self):
        # Changing the ORDER must not change the SET: components and drills
        # stay out of the market entirely, and as of 1.2 so do gemstones
        # (docs/market.md section 3).
        self.assertEqual(set(TRADEABLE_ORDER), set(ORES) | set(SMELTED_MATERIALS))
        self.assertEqual(len(TRADEABLE_ORDER), len(set(TRADEABLE_ORDER)))

    def test_no_gemstone_can_be_traded(self):
        # The 1.2 economy fix, pinned on the list that drives all three market
        # surfaces at once. A ruby's ceiling price is 5,500 against iron ore at
        # 0.01, so a single sale mints more than a server earns by playing; the
        # first four alone paid 11,458. Re-adding one here would reopen it on
        # /market sell, /market buy AND the job board in one edit, since
        # JOB_BOARD_MATERIALS is now an alias of this.
        for gem in GEMSTONES:
            self.assertNotIn(gem, TRADEABLE_ORDER)
            self.assertNotIn(gem, TRADEABLE_MATERIALS)
            self.assertNotIn(gem, JOB_BOARD_MATERIALS)

    def test_it_runs_raw_then_smelted(self):
        kinds = ["ore" if m in ORES else "smelted" for m in TRADEABLE_ORDER]
        self.assertEqual(
            kinds, ["ore"] * len(ORES) + ["smelted"] * len(SMELTED_MATERIALS)
        )

    def test_ores_run_commonest_to_rarest(self):
        chances = [
            RAW_MATERIALS[m]["drop_chance"] for m in TRADEABLE_ORDER if m in ORES
        ]
        self.assertEqual(chances, sorted(chances, reverse=True))

    def test_smelted_materials_run_cheapest_to_dearest(self):
        costs = [raw_input_cost(m) for m in TRADEABLE_ORDER if m in SMELTED_MATERIALS]
        self.assertEqual(costs, sorted(costs))

    def test_the_market_iterates_in_that_order(self):
        # dict order IS display order here, so the mapping the commands read
        # has to preserve it rather than merely contain the same keys.
        self.assertEqual(tuple(TRADEABLE_MATERIALS), TRADEABLE_ORDER)

    def test_every_tradeable_material_has_a_price(self):
        for material_id, info in TRADEABLE_MATERIALS.items():
            self.assertIn("market_price", info, material_id)


class StaticPriceTests(unittest.TestCase):
    """Prices stopped moving with the server's stock in 1.3, and are whole
    numbers of cents. Both are properties a player is now told they can rely
    on (/market status says so in as many words), so they are pinned here
    rather than left to the pricing helpers' own arithmetic."""

    def test_the_price_table_is_what_the_market_quotes(self):
        # The exact table, not a property of it. These six numbers are what
        # every other figure in the economy is measured against, so a retune
        # should have to come through here.
        self.assertEqual(
            {m: sale_unit_price(m) for m in TRADEABLE_ORDER},
            {
                "iron_ore": 0.01, "copper_ore": 0.02, "coal": 0.03,
                "iron": 0.15, "copper": 0.30, "steel": 0.48,
            },
        )

    def test_every_price_is_a_whole_number_of_cents(self):
        # Includes the gemstones, which aren't tradeable but are still priced
        # (raw_input_cost reads them). MARKET_PRICE_CENTS asserts this at
        # import; this is the same rule stated where a reader looks for it.
        for material_id, cents in MARKET_PRICE_CENTS.items():
            with self.subTest(material_id):
                self.assertEqual(cents, int(cents))
                self.assertAlmostEqual(ALL_MATERIALS[material_id]["market_price"] * 100, cents)

    def test_a_smelted_price_is_150_percent_of_its_inputs(self):
        # The balance rule the smelted prices are derived from, checked
        # against the raw table rather than against the derivation.
        for material_id, info in SMELTED_MATERIALS.items():
            raw = sum(
                RAW_MATERIALS[i]["market_price"] * q for i, q in info["inputs"].items()
            )
            with self.subTest(material_id):
                self.assertAlmostEqual(info["market_price"], raw * SMELTED_MARKUP)

    def test_buying_costs_exactly_twice_what_selling_pays(self):
        # The spread is what stops a round trip being free, and since 1.3 it
        # is also what stops the repeatable job board printing currency (see
        # data/materials.py: MARKET_BUY_MARKUP).
        for material_id in TRADEABLE_ORDER:
            with self.subTest(material_id):
                self.assertAlmostEqual(
                    purchase_unit_price(material_id), 2 * sale_unit_price(material_id)
                )

    def test_a_total_is_an_exact_number_of_cents_at_any_quantity(self):
        """Prices are rounded to the cent "actually", not only on screen - so a
        total has to land on one too, right up to the million-unit cap. Scaling
        the float unit price does not: 0.15 * 3 is 0.44999999999999996.

        Compared against the nearest double to a whole cent count rather than
        by multiplying back up, because the multiplication reintroduces the
        error being tested for - 0.07 * 100 is 7.000000000000001 even though
        0.07 is exactly the double a total of seven cents should be."""
        for material_id in TRADEABLE_ORDER:
            for quantity in (1, 3, 7, 70, 333, 99_999, MAX_MARKET_QUANTITY):
                with self.subTest(material_id, quantity=quantity):
                    for total in (
                        sale_total(material_id, quantity),
                        purchase_total(material_id, quantity),
                    ):
                        self.assertEqual(total, round(total * 100) / 100)

    def test_a_total_is_the_unit_price_times_the_quantity(self):
        # The exactness above must not have come at the cost of the total
        # being something other than what the price says it is.
        for material_id in TRADEABLE_ORDER:
            for quantity in (1, 3, 70, 99_999, MAX_MARKET_QUANTITY):
                with self.subTest(material_id, quantity=quantity):
                    self.assertAlmostEqual(
                        sale_total(material_id, quantity),
                        sale_unit_price(material_id) * quantity,
                    )
                    self.assertAlmostEqual(
                        purchase_total(material_id, quantity),
                        purchase_unit_price(material_id) * quantity,
                    )

    def test_neither_price_depends_on_anything_but_the_material(self):
        # The regression that would reintroduce the curve: both helpers take
        # one argument, so there is nowhere for a stock level to get back in.
        for fn in (sale_unit_price, purchase_unit_price):
            with self.subTest(fn.__name__):
                self.assertEqual(fn.__code__.co_argcount, 1)


class MaxAffordableTests(unittest.TestCase):
    def test_it_divides_the_balance_by_the_unit_cost(self):
        self.assertEqual(max_affordable(2.5, 10.0), 4)
        self.assertEqual(max_affordable(3.0, 10.0), 3)

    def test_an_exactly_sufficient_balance_is_not_rounded_down(self):
        # The regression the 1e-9 nudge exists for: 1.1 * 3 is 3.3000000000000003,
        # so a plain division gives 2.9999999999999996 and quotes 2.
        self.assertEqual(max_affordable(1.1, 1.1 * 3), 3)
        self.assertEqual(max_affordable(0.017681, 0.017681 * 100), 100)

    def test_a_balance_a_hair_short_does_not_round_up(self):
        self.assertEqual(max_affordable(1.0, 0.999), 0)
        self.assertEqual(max_affordable(2.0, 5.9), 2)

    def test_nothing_is_affordable_on_an_empty_or_negative_balance(self):
        self.assertEqual(max_affordable(1.0, 0.0), 0)
        self.assertEqual(max_affordable(1.0, -5.0), 0)

    def test_a_free_item_reports_zero_rather_than_dividing_by_it(self):
        # Unreachable through /market buy - _sell_price is at least the ceiling
        # price - but a guard here is cheaper than a ZeroDivisionError would be.
        self.assertEqual(max_affordable(0.0, 100.0), 0)


class QuantityRangeTests(unittest.TestCase):
    """The per-command cap, read off the registered slash command rather than
    off the constant - the annotation is what Discord actually enforces, and
    it is easy to raise the constant and leave one of the two commands behind."""

    def test_both_market_commands_accept_up_to_the_cap(self):
        for command in (EconomyCog.market_sell, EconomyCog.market_buy):
            quantity = next(p for p in command.parameters if p.name == "quantity")
            with self.subTest(command.name):
                self.assertEqual(quantity.min_value, 1)
                self.assertEqual(quantity.max_value, MAX_MARKET_QUANTITY)

    def test_the_cap_is_a_million(self):
        self.assertEqual(MAX_MARKET_QUANTITY, 1_000_000)


class CannotAffordMessageTests(unittest.TestCase):
    """The message itself, since its whole job is being accurate about a number
    the player is about to act on."""

    # Steel: the dearest thing on the market, so the smallest quantities and
    # the arithmetic most likely to round somewhere visible. 0.96 per unit.
    MATERIAL = "steel"

    def setUp(self):
        self.cog = EconomyCog.__new__(EconomyCog)
        self.unit = purchase_unit_price(self.MATERIAL)

    def message(self, quantity, balance):
        total = self.unit * quantity
        return self.cog._cannot_afford_message(
            self.MATERIAL, quantity, total, balance, "💰"
        )

    def test_it_names_the_quantity_the_balance_covers(self):
        # 10.00 buys 10 steel at 0.96, with 0.40 left over.
        self.assertIn("**10**", self.message(100, 10.0))

    def test_the_quoted_quantity_is_actually_affordable(self):
        for balance in (0.5, 1.51, 9.99, 100.0, 1234.56):
            quoted = max_affordable(self.unit, balance)
            self.assertLessEqual(self.unit * quoted, balance + 1e-9)
            self.assertGreater(self.unit * (quoted + 1), balance)

    def test_a_balance_short_of_one_unit_says_so_plainly(self):
        message = self.message(5, 0.5)
        self.assertIn("isn't enough for even one", message)
        self.assertNotIn("You can afford up to", message)

    def test_the_requested_quantity_is_never_quoted_back_as_the_answer(self):
        # The branch is only reached because `quantity` is unaffordable, so
        # float noise must not let it be offered as what to buy instead.
        for quantity in range(1, 20):
            message = self.message(quantity, self.unit * quantity - 1e-12)
            self.assertNotIn(f"**{quantity}**", message)


class MarketReceiptEmbedTests(unittest.TestCase):
    """build_market_receipt_embed backs both /market sell's and /market buy's
    receipts - the two commands differ only in field names, direction of the
    currency rounding, and which way the description reads."""

    def setUp(self):
        self.info = TRADEABLE_MATERIALS["iron_ore"]

    def _embed(self, **overrides):
        kwargs = dict(
            title="🪙 Sale Receipt",
            color=MARKET_COLOR,
            description="Sold description",
            material_field="Sold",
            material_id="iron_ore",
            quantity=5,
            material_remaining=42,
            currency_field="Received",
            currency_amount=10.0,
            balance_after=25.0,
            currency_emoji="💰",
            round_up_currency=False,
        )
        kwargs.update(overrides)
        return build_market_receipt_embed(**kwargs)

    def _field(self, embed, name):
        return next(f.value for f in embed.fields if f.name == name)

    def test_material_field_names_the_amount_moved_and_what_remains(self):
        value = self._field(self._embed(), "Sold")
        self.assertIn(f"{self.info['emoji']} **5 {self.info['name']}**", value)
        self.assertIn("(42 remaining)", value)

    def test_currency_field_names_the_amount_moved_and_new_balance(self):
        value = self._field(self._embed(), "Received")
        self.assertIn("💰 **10.00**", value)
        self.assertIn("(25.00 remaining)", value)

    def test_a_missing_currency_emoji_falls_back_to_the_default(self):
        value = self._field(self._embed(currency_emoji=None), "Received")
        self.assertIn(DEFAULT_CURRENCY_EMOJI, value)

    def test_round_up_currency_false_rounds_a_sale_payout_down(self):
        # A sale must never look more generous than it was.
        value = self._field(self._embed(currency_amount=10.004, round_up_currency=False), "Received")
        self.assertIn("**10.00**", value)

    def test_round_up_currency_true_rounds_a_purchase_cost_up(self):
        # A purchase must never look cheaper than it was.
        value = self._field(
            self._embed(
                material_field="Bought", currency_field="Spent",
                currency_amount=10.004, round_up_currency=True,
            ),
            "Spent",
        )
        self.assertIn("**10.01**", value)


if __name__ == "__main__":
    unittest.main()
