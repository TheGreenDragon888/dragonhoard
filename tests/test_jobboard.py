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
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    JOB_BOARD_MATERIALS,
    JOB_BOARD_MAX_QUANTITY,
    JOB_BOARD_TARGET_PAYOUT,
    job_quantity,
    job_reward,
    pick_job_material,
    sale_unit_price,
    target_stock,
)


def ceiling(material_id: str) -> float:
    return ALL_MATERIALS[material_id]["market_ceiling_price"]


def buyback_cost(material_id: str, quantity: int, stock: int, target: int) -> float:
    """What it costs to buy `quantity` back out of the server afterwards, at
    the stock level the sale itself just created - cogs/economy.py:
    _sell_price. The other half of the round trip job_reward has to lose."""
    return ceiling(material_id) * (1 + target / (target + stock + quantity)) * quantity


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
    def test_it_does_not_scale_with_member_count(self):
        """The whole point of pricing the task off a payout rather than off a
        share of target stock. The task is completed per player, so sizing it
        from a server-wide total grew one person's quota every time somebody
        joined, while nobody's mining rate grew to match."""
        for material_id in JOB_BOARD_MATERIALS:
            for stock_fraction in (0.0, 0.5, 1.0):
                with self.subTest(material=material_id, stock=stock_fraction):
                    quantities = set()
                    for members in (1, 5, 20, 100, 500):
                        target = target_stock(members, material_id)
                        stock = int(target * stock_fraction)
                        quantities.add(job_quantity(material_id, stock, target))
                    self.assertEqual(len(quantities), 1)

    def test_the_task_pays_just_over_the_target(self):
        """"Just over" is the contract: the fewest units that clear the target,
        so the payout lands within one unit's worth of it and never under."""
        for material_id in JOB_BOARD_MATERIALS:
            for stock_fraction in (0.0, 0.25, 1.0, 3.0):
                with self.subTest(material=material_id, stock=stock_fraction):
                    target = target_stock(20, material_id)
                    stock = int(target * stock_fraction)
                    quantity = job_quantity(material_id, stock, target)
                    if quantity == JOB_BOARD_MAX_QUANTITY:
                        continue  # capped, so the payout is allowed to sag
                    unit = sale_unit_price(ceiling(material_id), stock, target)
                    reward = job_reward(material_id, quantity, stock, target)
                    self.assertGreaterEqual(reward, JOB_BOARD_TARGET_PAYOUT - 1e-9)
                    self.assertLess(reward, JOB_BOARD_TARGET_PAYOUT + unit)

    def test_the_smallest_server_still_gets_an_achievable_task(self):
        # "Sell 0 of something" would be either unclaimable or free.
        for material_id in JOB_BOARD_MATERIALS:
            with self.subTest(material=material_id):
                target = target_stock(1, material_id)
                self.assertGreaterEqual(job_quantity(material_id, 0, target), 1)

    def test_a_rarer_material_is_asked_for_in_smaller_amounts(self):
        # Coal drops about a quarter as often as iron ore and is worth more per
        # unit, so it takes fewer of them to make the same payout. The rarity
        # scaling the old target-stock fraction gave for free has to survive.
        target_coal = target_stock(20, "coal")
        target_iron = target_stock(20, "iron_ore")
        self.assertLess(
            job_quantity("coal", 0, target_coal), job_quantity("iron_ore", 0, target_iron)
        )

    def test_the_task_grows_as_the_server_fills_up(self):
        """Each unit is worth less to a server that already has plenty, so it
        takes more of them to clear the same payout. This is what replaces the
        multiplier that was considered for the over-target case - it falls out
        of the price curve instead of being bolted on top of it."""
        target = target_stock(20, "iron_ore")
        quantities = [
            job_quantity("iron_ore", int(target * f), target) for f in (0.0, 0.5, 1.0, 2.0)
        ]
        self.assertEqual(quantities, sorted(quantities))
        self.assertLess(quantities[0], quantities[-1])

    def test_the_quantity_is_capped(self):
        """Quantity has no natural bound as stock climbs - the price decays
        toward zero, so the units needed to clear a fixed payout grow without
        one. A task nobody can finish pays nothing at all."""
        target = target_stock(20, "iron_ore")
        self.assertEqual(job_quantity("iron_ore", target * 10_000, target), JOB_BOARD_MAX_QUANTITY)

    def test_the_cap_is_the_only_thing_that_lowers_the_payout(self):
        target = target_stock(20, "iron_ore")
        stock = target * 10_000
        quantity = job_quantity("iron_ore", stock, target)
        self.assertEqual(quantity, JOB_BOARD_MAX_QUANTITY)
        self.assertLess(job_reward("iron_ore", quantity, stock, target), JOB_BOARD_TARGET_PAYOUT)


class JobRewardTests(unittest.TestCase):
    def test_the_reward_is_what_the_server_pays_for_the_goods(self):
        target = target_stock(20, "iron_ore")
        for stock in (0, target // 2, target, target * 4):
            with self.subTest(stock=stock):
                unit = sale_unit_price(ceiling("iron_ore"), stock, target)
                self.assertAlmostEqual(job_reward("iron_ore", 100, stock, target), unit * 100)

    def test_the_reward_is_never_the_flat_ceiling_price(self):
        """What it used to be, and the reason the round trip below paid. A
        server holding any stock at all pays under the ceiling."""
        target = target_stock(20, "iron_ore")
        self.assertLess(job_reward("iron_ore", 100, target, target), ceiling("iron_ore") * 100)

    def _round_trip(self, material_id, members, stock_fraction):
        """The money loop, run the way it would actually be run: sell the task
        quantity, claim the bonus, buy the same goods straight back out. The
        buyback is priced at the stock your own sale just created, so it is
        cheaper than the sale was - and the bonus used to more than cover the
        difference. Returns the best a player could do across a day's worth of
        claimants, each selling into the stock the ones before them left; the
        bonus is frozen at posting, so a later seller claims a figure priced at
        a fuller warehouse than the one they sold into."""
        target = target_stock(members, material_id)
        posted_at = int(target * stock_fraction)
        quantity = job_quantity(material_id, posted_at, target)
        bonus = job_reward(material_id, quantity, posted_at, target)
        return max(
            sale_unit_price(ceiling(material_id), posted_at + k * quantity, target) * quantity
            + bonus
            - buyback_cost(material_id, quantity, posted_at + k * quantity, target)
            for k in range(12)
        )

    def test_buying_the_goods_back_costs_more_than_the_job_paid(self):
        """Closed at every server size once the server holds a real amount of
        the material. 70% of target is the threshold that holds even for a
        one-member server, where the task is nearly twice the whole target
        stock and a single sale swings the price hardest."""
        for material_id in JOB_BOARD_MATERIALS:
            for members in (1, 5, 20, 100):
                for stock_fraction in (0.7, 1.0, 3.0):
                    with self.subTest(material=material_id, members=members, stock=stock_fraction):
                        self.assertLess(self._round_trip(material_id, members, stock_fraction), 0)

    def test_it_closes_far_sooner_on_a_server_with_players_in_it(self):
        """The leak scales with quantity/target_stock, and quantity is
        member-independent while target stock is not - so it shrinks as a
        server fills up. A twenty-member server is closed by a quarter of
        target stock, long before the 70% a lone player needs."""
        for material_id in JOB_BOARD_MATERIALS:
            for members in (20, 100):
                with self.subTest(material=material_id, members=members):
                    self.assertLess(self._round_trip(material_id, members, 0.25), 0)

    def test_the_leak_on_a_thin_server_stays_bounded(self):
        """Below those thresholds the round trip still pays. This is the guard
        on how much: if a change to the price curve or the payout target widens
        it, this is what should fail. The worst case is a one-member server,
        which is also the one that matters least - each server's economy is its
        own, so a solo player prints only for themselves."""
        worst = max(
            self._round_trip(material_id, members, stock_fraction)
            for material_id in JOB_BOARD_MATERIALS
            for members in (1, 5, 20, 100)
            for stock_fraction in (0.0, 0.05, 0.1, 0.25, 0.5)
        )
        self.assertLess(worst, 0.70)


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
