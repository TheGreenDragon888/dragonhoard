"""
Tests for the market's pricing arithmetic as the player sees it.

max_affordable is what /market buy quotes back when someone asks for more than
they can pay for, so it has to be exactly right in both directions: quoting one
too many sends them into a purchase that fails, and quoting one too few is the
confusion the message exists to remove. Pure arithmetic - no database.
"""
import unittest

from cogs.economy import EconomyCog, max_affordable


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


class CannotAffordMessageTests(unittest.TestCase):
    """The message itself, since its whole job is being accurate about a number
    the player is about to act on."""

    def setUp(self):
        self.cog = EconomyCog.__new__(EconomyCog)

    def message(self, quantity, balance, *, ceiling=1.0, stock=1000, target=1000):
        total = self.cog._sell_price(ceiling, stock, quantity, target)
        return self.cog._cannot_afford_message(
            ceiling, stock, target, quantity, total, balance, "💰"
        )

    def test_it_names_the_quantity_the_balance_covers(self):
        # At 1000 stock and 1000 target the unit cost is 1.5x the ceiling.
        self.assertIn("**6**", self.message(100, 10.0))

    def test_the_quoted_quantity_is_actually_affordable(self):
        unit = self.cog._sell_price(1.0, 1000, 1, 1000)
        for balance in (0.5, 1.51, 9.99, 100.0, 1234.56):
            quoted = max_affordable(unit, balance)
            self.assertLessEqual(unit * quoted, balance + 1e-9)
            self.assertGreater(unit * (quoted + 1), balance)

    def test_a_balance_short_of_one_unit_says_so_plainly(self):
        message = self.message(5, 0.5)
        self.assertIn("isn't enough for even one", message)
        self.assertNotIn("You can afford up to", message)

    def test_the_requested_quantity_is_never_quoted_back_as_the_answer(self):
        # The branch is only reached because `quantity` is unaffordable, so
        # float noise must not let it be offered as what to buy instead.
        for quantity in range(1, 20):
            unit = self.cog._sell_price(1.0, 1000, 1, 1000)
            message = self.message(quantity, unit * quantity - 1e-12)
            self.assertNotIn(f"**{quantity}**", message)


if __name__ == "__main__":
    unittest.main()
