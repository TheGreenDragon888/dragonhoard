"""
Tests for the display helpers in utils/formatting.py.

Duration text is what the furnace, factory and press status embeds promise a
player, so the rounding direction matters: a machine may finish later than it
said, never sooner. Pure string arithmetic - no database, no discord.py.
"""
import re
import unittest
from datetime import datetime, timezone

from utils.formatting import (
    format_compact_price,
    format_duration,
    format_price,
    format_relative_timestamp,
)

TIMESTAMP = re.compile(r"^<t:(?P<epoch>\d+):R>$")


class FormatPriceTests(unittest.TestCase):
    """The 1e-9 nudge in format_price had no test, and the worked example in
    its docstring turned out not to reproduce. These pin the behaviour the
    nudge exists for so the next reader can check the reasoning against
    something real."""

    def test_a_value_whose_cents_land_low_still_shows_its_full_cent(self):
        # Each of these multiplies to just under its exact cent in binary, so a
        # bare floor drops a whole cent. 0.29 is the one the docstring cites.
        for amount, expected in ((0.29, "0.29"), (1.15, "1.15"), (4.35, "4.35")):
            with self.subTest(amount=amount):
                self.assertLess(amount * 100, round(amount * 100))
                self.assertEqual(format_price(amount), expected)

    def test_rounding_up_never_understates_a_charge(self):
        self.assertEqual(format_price(1.234, round_up=True), "1.24")
        self.assertEqual(format_price(0.001, round_up=True), "0.01")

    def test_an_exact_value_is_not_pushed_a_cent_either_way(self):
        for amount in (1.10, 2.50, 0.05, 10.00):
            with self.subTest(amount=amount):
                self.assertEqual(format_price(amount), format_price(amount, round_up=True))

    def test_sub_cent_prices_extend_rather_than_reading_as_zero(self):
        # A market price well past target stock really is worth less than a
        # cent, and "0.00" would be a lie about a price a player is paid. The
        # extension only kicks in once two decimals would show nothing at all -
        # 0.0106 still reads as "0.01", because that is a cent.
        self.assertEqual(format_price(0.005), "0.0050")
        self.assertEqual(format_price(0.0001), "0.0001")
        self.assertEqual(format_price(0.010588), "0.01")
        self.assertEqual(format_price(0.0), "0.00")

    def test_thousands_are_separated(self):
        self.assertEqual(format_price(11458.0), "11,458.00")


class FormatDurationTests(unittest.TestCase):
    def test_minutes_below_an_hour(self):
        self.assertEqual(format_duration(45 / 60), "45m")
        self.assertEqual(format_duration(1 / 60), "1m")

    def test_hours_and_minutes(self):
        self.assertEqual(format_duration(2.25), "2h 15m")
        self.assertEqual(format_duration(1.5), "1h 30m")

    def test_a_whole_number_of_hours_drops_the_minutes(self):
        self.assertEqual(format_duration(2), "2h")
        self.assertEqual(format_duration(23), "23h")

    def test_a_day_or_more_reads_in_days(self):
        self.assertEqual(format_duration(24), "1d")
        self.assertEqual(format_duration(27 * 24), "27d")
        self.assertEqual(format_duration(24 + 4), "1d 4h")

    def test_part_minutes_always_round_up(self):
        # The promise a receipt makes: a job quoted at 3m may take 3m, never 4m.
        self.assertEqual(format_duration(2.5 / 60), "3m")
        self.assertEqual(format_duration(2.01 / 60), "3m")
        self.assertEqual(format_duration(59.5 / 60), "1h")

    def test_an_exact_value_is_not_rounded_up_a_minute(self):
        # Float division lands 5/5 items an atom either side of a whole number;
        # without the tolerance every ETA would gain a spurious minute.
        self.assertEqual(format_duration(120 / 60), "2h")
        self.assertEqual(format_duration(3 * (1 / 3)), "1h")

    def test_a_wait_shorter_than_a_minute_still_rounds_up_to_one(self):
        self.assertEqual(format_duration(0.2 / 60), "1m")

    def test_only_no_wait_at_all_reads_as_under_a_minute(self):
        # Reachable: the press subtracts its banked progress from a job's cost
        # and clamps at zero, so a job about to finish quotes exactly 0 hours.
        self.assertEqual(format_duration(0), "under a minute")
        self.assertEqual(format_duration(-1), "under a minute")


class FormatRelativeTimestampTests(unittest.TestCase):
    def parse(self, hours):
        match = TIMESTAMP.match(format_relative_timestamp(hours))
        self.assertIsNotNone(
            match, f"format_relative_timestamp({hours}) didn't match the expected shape"
        )
        return match

    def test_it_lands_the_requested_number_of_hours_out(self):
        match = self.parse(2.25)
        expected = datetime.now(timezone.utc).timestamp() + 2.25 * 3600
        self.assertAlmostEqual(int(match["epoch"]), expected, delta=5)

    def test_long_waits_are_expressed_the_same_way(self):
        # The R style counts itself - there's no short/long variant to pick, so
        # a month-out press job needs no special handling.
        match = self.parse(27 * 24)
        expected = datetime.now(timezone.utc).timestamp() + 27 * 24 * 3600
        self.assertAlmostEqual(int(match["epoch"]), expected, delta=5)

    def test_a_negative_wait_lands_now_rather_than_in_the_past(self):
        match = self.parse(-5)
        self.assertAlmostEqual(
            int(match["epoch"]), datetime.now(timezone.utc).timestamp(), delta=5
        )


if __name__ == "__main__":
    unittest.main()


class FormatCompactPriceTests(unittest.TestCase):
    """The fixed-width player-price column (utils/formatting.py).

    Deleted in 1.3 when the server's prices became whole cents and needed no
    padding; restored in 1.4, when players gained the ability to set a price of
    any magnitude. The invariant is the width, not any one rendering.
    """

    @staticmethod
    def width(text: str) -> int:
        """Digits plus any metric suffix, NOT counting the decimal point -
        the measure the docstring promises is constant."""
        return len(text.replace(".", ""))

    def test_every_magnitude_is_the_same_width(self):
        for value in (
            0.0001, 0.0101, 0.48, 9.5, 99.99, 123.4, 999.4,
            1500, 99_999, 250_000, 5_500_000, 5e11, 9e14,
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self.width(format_compact_price(value)), 5,
                    "the whole point is that a column of these lines up",
                )

    def test_a_sub_cent_price_keeps_its_precision(self):
        """The case the server's own prices never hit, and the reason
        format_price alone is not enough for a player book: 0.0001 shown at two
        decimals is 0.00, which reads as free."""
        self.assertEqual(format_compact_price(0.0001), "0.0001")
        self.assertEqual(format_compact_price(0.0199), "0.0199")

    def test_large_values_take_a_metric_suffix(self):
        self.assertTrue(format_compact_price(1500).endswith("K"))
        self.assertTrue(format_compact_price(5_500_000).endswith("M"))
        self.assertTrue(format_compact_price(5e9).endswith("B"))
        self.assertTrue(format_compact_price(5e12).endswith("T"))

    def test_rounding_up_a_tier_moves_to_the_next_one(self):
        """999.996 would render as "1000.0" at its own tier, which is six
        digits - _format_compact_tier returns None and the caller retries."""
        self.assertEqual(format_compact_price(999.996), "1.000K")

    def test_a_value_past_the_last_tier_grows_rather_than_losing_precision(self):
        text = format_compact_price(9e15)
        self.assertTrue(text.endswith("T"))
        self.assertEqual(float(text[:-1]), 9000.0)
