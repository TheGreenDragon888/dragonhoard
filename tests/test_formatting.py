"""
Tests for the display helpers in utils/formatting.py.

Duration and ETA text is what the furnace, factory and press receipts promise a
player, so the rounding direction matters: a machine may finish later than it
said, never sooner. Pure string arithmetic - no database, no discord.py.
"""
import re
import unittest
from datetime import datetime, timezone

from utils.formatting import format_duration, format_eta

TIMESTAMP = re.compile(r"^\*\*(?P<duration>[^*]+)\*\* · <t:(?P<epoch>\d+):(?P<style>[tf])>$")


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


class FormatEtaTests(unittest.TestCase):
    def parse(self, hours):
        match = TIMESTAMP.match(format_eta(hours))
        self.assertIsNotNone(match, f"format_eta({hours}) didn't match the expected shape")
        return match

    def test_it_pairs_the_duration_with_a_discord_timestamp(self):
        match = self.parse(2.25)
        self.assertEqual(match["duration"], "2h 15m")

        expected = datetime.now(timezone.utc).timestamp() + 2.25 * 3600
        self.assertAlmostEqual(int(match["epoch"]), expected, delta=5)

    def test_short_waits_show_a_clock_time_and_long_ones_a_date(self):
        # "8:15 PM" on its own is ambiguous once it's more than a day out.
        self.assertEqual(self.parse(3)["style"], "t")
        self.assertEqual(self.parse(11.9)["style"], "t")
        self.assertEqual(self.parse(12)["style"], "f")
        self.assertEqual(self.parse(27 * 24)["style"], "f")

    def test_a_negative_wait_lands_now_rather_than_in_the_past(self):
        match = self.parse(-5)
        self.assertEqual(match["duration"], "under a minute")
        self.assertAlmostEqual(
            int(match["epoch"]), datetime.now(timezone.utc).timestamp(), delta=5
        )


if __name__ == "__main__":
    unittest.main()
