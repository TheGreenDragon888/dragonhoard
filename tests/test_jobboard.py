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

from utils.formatting import utc_today
from utils.job_board import JOB_BOARD_TIMEZONE, hours_until_reset, job_board_today
from data.materials import (
    GEMSTONES,
    ORES,
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    JOB_BOARD_MATERIALS,
    JOB_BOARD_SELECTION_FLOOR,
    JOB_BOARD_TARGET_STOCK_FRACTION,
    job_quantity,
    job_reward,
    pick_job_material,
    target_stock,
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

    def test_the_mining_pool_still_rolls_over_on_utc(self):
        """Deliberately a different schedule. These shared one function until
        the board moved, so this is here to make a future "tidy-up" that
        re-merges them fail loudly rather than silently shifting the pool."""
        self.assertNotEqual(utc_today.__module__, job_board_today.__module__)
        self.assertEqual(utc_today(), datetime.now(timezone.utc).date().isoformat())


class EligibleMaterialTests(unittest.TestCase):
    def test_gemstones_are_never_asked_for(self):
        """The balance decision the whole feature turns on. Gemstone ceiling
        prices run from 5,500 to 500,000, and the reward is ceiling * quantity,
        so one diamond task would pay every player who completed it more than
        every other source of currency in the game put together."""
        for gem_id in GEMSTONES:
            self.assertNotIn(gem_id, JOB_BOARD_MATERIALS)

    def test_it_asks_for_ores_and_smelted_materials(self):
        self.assertEqual(set(JOB_BOARD_MATERIALS), set(ORES) | set(SMELTED_MATERIALS))

    def test_every_eligible_material_is_tradeable(self):
        # The task is completed by selling, so a material the market won't buy
        # would be an impossible task.
        for material_id in JOB_BOARD_MATERIALS:
            self.assertIn("market_ceiling_price", RAW_MATERIALS.get(material_id, {}) or SMELTED_MATERIALS[material_id])

    def test_ores_come_before_smelted_and_commonest_first(self):
        self.assertEqual(JOB_BOARD_MATERIALS[:3], ("iron_ore", "copper_ore", "coal"))


class JobQuantityTests(unittest.TestCase):
    def test_the_task_is_a_fraction_of_target_stock(self):
        for material_id in JOB_BOARD_MATERIALS:
            with self.subTest(material=material_id):
                expected = int(target_stock(20, material_id) * JOB_BOARD_TARGET_STOCK_FRACTION)
                self.assertEqual(job_quantity(20, material_id), max(1, expected))

    def test_it_scales_with_member_count(self):
        self.assertEqual(job_quantity(20, "iron_ore"), 4 * job_quantity(5, "iron_ore"))

    def test_the_smallest_server_still_gets_an_achievable_task(self):
        # 10% of a tiny target stock rounds to zero, and "sell 0 of something"
        # would either be unclaimable or claimable for free.
        for material_id in JOB_BOARD_MATERIALS:
            with self.subTest(material=material_id):
                self.assertGreaterEqual(job_quantity(1, material_id), 1)

    def test_a_rarer_material_is_asked_for_in_smaller_amounts(self):
        # Coal drops about a quarter as often as iron ore, so asking for the
        # same count of each would make one task trivial and the other a grind.
        self.assertLess(job_quantity(20, "coal"), job_quantity(20, "iron_ore"))


class JobRewardTests(unittest.TestCase):
    def test_the_reward_is_the_base_ceiling_price_of_what_was_sold(self):
        self.assertAlmostEqual(
            job_reward("iron_ore", 100), RAW_MATERIALS["iron_ore"]["market_ceiling_price"] * 100
        )

    def test_the_bonus_never_exceeds_the_goods_full_value(self):
        """What keeps this a bounded faucet. A normal sale pays between half
        and one times the ceiling price per unit, so the bonus is at most a
        100% top-up and never more than the goods were worth to begin with."""
        for material_id in JOB_BOARD_MATERIALS:
            with self.subTest(material=material_id):
                quantity = job_quantity(20, material_id)
                ceiling = job_reward(material_id, 1)
                self.assertLessEqual(job_reward(material_id, quantity), ceiling * quantity + 1e-9)


class MaterialSelectionTests(unittest.TestCase):
    def test_the_material_the_server_needs_most_dominates(self):
        deficits = {material_id: 0.0 for material_id in JOB_BOARD_MATERIALS}
        deficits["steel"] = 1.0
        rng = random.Random(20260803)
        picks = [pick_job_material(deficits, rng) for _ in range(2000)]
        self.assertGreater(picks.count("steel"), 1500)

    def test_a_fully_stocked_material_is_still_reachable(self):
        """Why the floor exists. A deterministic "biggest deficit wins" rule
        parks the board on one material until the server catches up, and a
        server that can't produce that material yet gets the same impossible
        task every single day."""
        deficits = {material_id: 0.0 for material_id in JOB_BOARD_MATERIALS}
        deficits["iron_ore"] = 1.0
        rng = random.Random(1)
        picks = {pick_job_material(deficits, rng) for _ in range(4000)}
        self.assertEqual(picks, set(JOB_BOARD_MATERIALS))

    def test_a_server_that_needs_nothing_still_gets_a_task(self):
        # Without the floor every weight would be zero and random.choices
        # raises on a total weight of zero.
        deficits = {material_id: 0.0 for material_id in JOB_BOARD_MATERIALS}
        self.assertIn(pick_job_material(deficits, random.Random(7)), JOB_BOARD_MATERIALS)

    def test_missing_deficits_are_treated_as_zero(self):
        self.assertIn(pick_job_material({}, random.Random(7)), JOB_BOARD_MATERIALS)

    def test_the_floor_is_small_enough_to_be_a_floor(self):
        # It has to keep every material reachable without drowning out the
        # deficit that's supposed to be steering the choice.
        self.assertLess(JOB_BOARD_SELECTION_FLOOR, 0.25)
        self.assertGreater(JOB_BOARD_SELECTION_FLOOR, 0.0)


if __name__ == "__main__":
    unittest.main()
