"""
Tests for the daily job board's balance data - which materials it can ask for,
how big a task it sets, and what it pays.

All of this is pure arithmetic over data/materials.py. The database side (a
task posting once per day, progress accumulating, the reward paying exactly
once) is in tests/test_jobboard_db.py.
"""
import random
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from utils.job_board import JOB_BOARD_TIMEZONE, hours_until_reset, job_board_today
from data.materials import (
    ALL_MATERIALS,
    GEMSTONES,
    ORES,
    SMELTED_MATERIALS,
    JOB_BOARD_MATERIALS,
    JOB_BOARD_TARGET_PAYOUT,
    job_quantity,
    pick_job_material,
    purchase_unit_price,
    sale_unit_price,
)


class ResetClockTests(unittest.TestCase):
    """The job board's day runs on Arizona time, not UTC. These pin the two
    things that would break silently: the offset itself, and the fact that
    Arizona does not observe daylight saving."""

    def test_the_board_runs_on_arizona_time(self):
        self.assertEqual(str(JOB_BOARD_TIMEZONE), "America/Phoenix")

    def test_arizona_is_seven_hours_behind_utc_all_year(self):
        # Arizona has not observed DST since 1968. If this ever starts failing,
        # the whole reset moves an hour twice a year and the manual's "midnight
        # Arizona time" stops being one fixed instant in UTC.
        for month in range(1, 13):
            moment = datetime(2026, month, 15, 12, 0, tzinfo=timezone.utc)
            with self.subTest(month=month):
                local = moment.astimezone(JOB_BOARD_TIMEZONE)
                self.assertEqual(local.utcoffset(), timedelta(hours=-7))
                self.assertEqual(local.dst(), timedelta(0))

    def test_the_date_changes_at_arizona_midnight_not_utc_midnight(self):
        """The seven hours where the two clocks disagree, which is where any
        timezone bug would actually show up. 03:00 UTC on the 4th is still the
        evening of the 3rd in Arizona."""
        just_before = datetime(2026, 8, 4, 6, 59, tzinfo=timezone.utc)
        just_after = datetime(2026, 8, 4, 7, 1, tzinfo=timezone.utc)
        self.assertEqual(just_before.astimezone(JOB_BOARD_TIMEZONE).date().isoformat(), "2026-08-03")
        self.assertEqual(just_after.astimezone(JOB_BOARD_TIMEZONE).date().isoformat(), "2026-08-04")

    def test_today_is_a_sortable_iso_date(self):
        # job_date is stored and compared as text, so the format is load-bearing.
        today = job_board_today()
        self.assertEqual(today, datetime.fromisoformat(today).date().isoformat())

    def test_the_countdown_runs_to_the_next_arizona_midnight(self):
        arizona = ZoneInfo("America/Phoenix")
        self.assertAlmostEqual(
            hours_until_reset(datetime(2026, 8, 3, 23, 0, tzinfo=arizona)), 1.0, places=6
        )
        self.assertAlmostEqual(
            hours_until_reset(datetime(2026, 8, 3, 0, 0, tzinfo=arizona)), 24.0, places=6
        )
        self.assertAlmostEqual(
            hours_until_reset(datetime(2026, 8, 3, 12, 30, tzinfo=arizona)), 11.5, places=6
        )

    def test_the_countdown_converts_a_utc_instant_rather_than_misreading_it(self):
        # /jobboard passes the board's own clock, but a caller handing this a
        # UTC datetime must not get an answer seven hours out.
        self.assertAlmostEqual(
            hours_until_reset(datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)), 1.0, places=6
        )

    def test_the_countdown_is_always_a_positive_part_of_a_day(self):
        arizona = ZoneInfo("America/Phoenix")
        for hour in range(24):
            with self.subTest(hour=hour):
                left = hours_until_reset(datetime(2026, 8, 3, hour, tzinfo=arizona))
                self.assertGreater(left, 0)
                self.assertLessEqual(left, 24)


class EligibleMaterialTests(unittest.TestCase):
    def test_gemstones_are_never_asked_for(self):
        """The balance decision the whole feature turns on. Gemstone prices run
        from 5,500 to 500,000, and a task floors at one unit, so one diamond
        task would pay every player who completed it more than every other
        source of currency in the game put together - and since 1.3 it would
        pay that once per completion rather than once a day."""
        for gem_id in GEMSTONES:
            self.assertNotIn(gem_id, JOB_BOARD_MATERIALS)

    def test_it_asks_for_ores_and_smelted_materials(self):
        self.assertEqual(set(JOB_BOARD_MATERIALS), set(ORES) | set(SMELTED_MATERIALS))

    def test_every_eligible_material_is_tradeable(self):
        # The task is completed by selling, so a material the market won't buy
        # would be an impossible task.
        for material_id in JOB_BOARD_MATERIALS:
            self.assertIn("market_price", ALL_MATERIALS[material_id], material_id)

    def test_ores_come_before_smelted_and_commonest_first(self):
        self.assertEqual(JOB_BOARD_MATERIALS[:3], ("iron_ore", "copper_ore", "coal"))


class JobQuantityTests(unittest.TestCase):
    def test_the_task_is_the_same_size_everywhere(self):
        """A constant of the material as of 1.3, since the price it is worked
        back from is. It used to grow with the server's stock and had to be
        argued into member-independence; now there is nowhere for a server's
        circumstances to reach it at all, which is also what makes it safe to
        complete the task repeatedly - nothing already sold today can change
        what the next completion costs."""
        self.assertEqual(
            {m: job_quantity(m) for m in JOB_BOARD_MATERIALS},
            {
                "iron_ore": 100, "copper_ore": 50, "coal": 34,
                "iron": 7, "copper": 4, "steel": 3,
            },
        )

    def test_the_task_pays_just_over_the_target(self):
        """"Just over" is the contract: the fewest units that clear the target,
        so the sale lands within one unit's worth of it and never under. This
        is what makes the flat bonus safe - a completion can never pay more
        than the goods it was paid for."""
        for material_id in JOB_BOARD_MATERIALS:
            with self.subTest(material=material_id):
                quantity = job_quantity(material_id)
                unit = sale_unit_price(material_id)
                self.assertGreaterEqual(quantity * unit, JOB_BOARD_TARGET_PAYOUT - 1e-9)
                self.assertLess(quantity * unit, JOB_BOARD_TARGET_PAYOUT + unit)

    def test_every_task_is_achievable(self):
        # "Sell 0 of something" would be either unclaimable or free.
        for material_id in JOB_BOARD_MATERIALS:
            with self.subTest(material=material_id):
                self.assertGreaterEqual(job_quantity(material_id), 1)

    def test_a_dearer_material_is_asked_for_in_smaller_amounts(self):
        # Fewer units of something worth more make the same payout. The rarity
        # scaling the old target-stock fraction gave for free has to survive.
        quantities = [job_quantity(m) for m in JOB_BOARD_MATERIALS]
        prices = [sale_unit_price(m) for m in JOB_BOARD_MATERIALS]
        self.assertEqual(
            [q for _, q in sorted(zip(prices, quantities))],
            sorted(quantities, reverse=True),
        )


class RepeatableRewardTests(unittest.TestCase):
    """The bonus is paid per completion with no daily cap (1.3), so what used
    to be bounded by "once each" is now bounded by arithmetic alone. These are
    that arithmetic."""

    def test_a_completion_never_pays_more_than_the_goods_are_worth(self):
        # The first half of why repeating it is safe: the bonus is at most a
        # second copy of the sale, never more.
        for material_id in JOB_BOARD_MATERIALS:
            with self.subTest(material=material_id):
                sale = job_quantity(material_id) * sale_unit_price(material_id)
                self.assertGreaterEqual(sale, JOB_BOARD_TARGET_PAYOUT - 1e-9)

    def test_buying_the_goods_back_costs_at_least_what_the_pair_paid(self):
        """The money loop, run the way it would actually be run: sell the task
        quantity, take the bonus, buy the same goods straight back out and do
        it again. Under the old decaying price curve this leaked on a thinly
        stocked server and the test here bounded the leak; with a flat price
        and a buy markup of exactly 2 it cannot leak at all, at any repetition
        count, on any server.

        Iron ore and copper ore break exactly even - their task quantity times
        their price is exactly the target payout - which is why this is
        assertLessEqual rather than assertLess."""
        for material_id in JOB_BOARD_MATERIALS:
            for repeats in (1, 2, 10, 1000):
                with self.subTest(material=material_id, repeats=repeats):
                    quantity = job_quantity(material_id) * repeats
                    received = (
                        quantity * sale_unit_price(material_id)
                        + JOB_BOARD_TARGET_PAYOUT * repeats
                    )
                    buyback = quantity * purchase_unit_price(material_id)
                    self.assertLessEqual(received, buyback + 1e-9)


class MaterialSelectionTests(unittest.TestCase):
    # target / (stock + target): stock=0 -> 1.0 (max); stock=target -> 0.5;
    # stock=20x target -> 1/21 (heavily overstocked, still > 0).
    EMPTY = 1.0
    AT_TARGET = 0.5
    HEAVILY_OVERSTOCKED = 1 / 21

    def test_an_empty_material_dominates_over_one_merely_at_target(self):
        deficits = {m: self.AT_TARGET for m in JOB_BOARD_MATERIALS}
        deficits["steel"] = self.EMPTY
        rng = random.Random(20260803)
        picks = [pick_job_material(deficits, rng) for _ in range(4000)]
        # True share: 1.0 / (1.0 + 5*0.5) = 2/7 ~= 28.6%.
        self.assertGreater(picks.count("steel") / 4000, 0.22)
        self.assertLess(picks.count("steel") / 4000, 0.36)

    def test_a_heavily_overstocked_material_is_still_reachable(self):
        """target / (stock + target) is never exactly zero for finite stock,
        unlike the old max(0, target - stock) / target, which clamped to
        exactly 0.0 for any stock at or above target. This guarantee is now a
        property of the formula itself and needs nothing added on top."""
        deficits = {m: self.HEAVILY_OVERSTOCKED for m in JOB_BOARD_MATERIALS}
        deficits["iron_ore"] = self.EMPTY
        rng = random.Random(1)
        picks = {pick_job_material(deficits, rng) for _ in range(4000)}
        self.assertEqual(picks, set(JOB_BOARD_MATERIALS))


if __name__ == "__main__":
    unittest.main()
