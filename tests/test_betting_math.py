"""
Tests for the pot arithmetic behind /bet (utils/betting.py).

No database and no Discord: everything here is a pure function, and the one
property the whole feature rests on - that a pot pays out exactly what was
staked into it - is a property of apportion() alone. ApportionTests is
therefore the class to read first and the one not to weaken.
"""
import random
import unittest
from datetime import datetime, timedelta, timezone

from utils.betting import (
    AGAINST,
    FOR,
    Pools,
    apportion,
    closes_at_text,
    from_cents,
    hours_until,
    return_multiple,
    to_cents,
)


class ApportionTests(unittest.TestCase):
    def test_a_lone_winner_takes_the_whole_pot(self):
        self.assertEqual(apportion([(1, 1_000)], 11_000), {1: 11_000})

    def test_the_readme_example_pays_eleven_times(self):
        """10 staked for, 100 against: the for side is a tenth of the pot, so
        it comes back multiplied by eleven - the stake plus the ten times it
        that was bet against it. The figure quoted in docs/betting.md."""
        pools = Pools(for_cents=1_000, against_cents=10_000, for_backers=1, against_backers=1)
        self.assertAlmostEqual(return_multiple(pools, FOR), 11.0)
        self.assertEqual(apportion([(1, 1_000)], pools.pot_cents), {1: 11_000})

    def test_equal_stakes_split_evenly(self):
        self.assertEqual(
            apportion([(1, 500), (2, 500)], 2_000), {1: 1_000, 2: 1_000}
        )

    def test_shares_are_proportional_to_stake(self):
        # 300 and 100 staked out of a 1,200 pot: three quarters and one.
        self.assertEqual(apportion([(1, 300), (2, 100)], 1_200), {1: 900, 2: 300})

    def test_an_indivisible_pot_goes_to_the_largest_remainder(self):
        """Three equal stakes cannot split ten cents evenly. Each takes the
        floor of 10/3 and the odd cent goes to the lowest wager id, since the
        remainders and the stakes are all equal."""
        self.assertEqual(apportion([(1, 1), (2, 1), (3, 1)], 10), {1: 4, 2: 3, 3: 3})

    def test_the_odd_cent_follows_the_bigger_fraction_not_the_bigger_stake(self):
        """2 and 1 staked out of a 10 cent pot: exact shares are 6.67 and 3.33,
        so the floors are 6 and 3 and the leftover cent belongs to the .67."""
        self.assertEqual(apportion([(1, 2), (2, 1)], 10), {1: 7, 2: 3})

    def test_no_winners_pays_nothing(self):
        self.assertEqual(apportion([], 5_000), {})

    def test_it_is_exact_for_arbitrary_pools(self):
        """THE INVARIANT. A thousand randomly shaped bets, none of which may
        pay out a cent more or less than was staked into it."""
        rng = random.Random(20260918)
        for trial in range(1_000):
            winners = [
                (wager_id, rng.randint(1, 5_000_000))
                for wager_id in range(1, rng.randint(1, 12) + 1)
            ]
            losers = sum(rng.randint(0, 5_000_000) for _ in range(rng.randint(0, 12)))
            pot = sum(stake for _, stake in winners) + losers
            with self.subTest(trial=trial):
                payouts = apportion(winners, pot)
                self.assertEqual(sum(payouts.values()), pot)
                self.assertEqual(set(payouts), {wager_id for wager_id, _ in winners})
                # Nobody is paid a negative amount, and nobody is short-changed
                # by more than the one cent rounding can cost them.
                for wager_id, stake in winners:
                    exact = stake * pot / sum(s for _, s in winners)
                    self.assertGreaterEqual(payouts[wager_id], 0)
                    self.assertLess(abs(payouts[wager_id] - exact), 1)

    def test_it_does_not_depend_on_the_order_the_wagers_are_read_in(self):
        """Rows come back in whatever order SQLite hands them over, so a payout
        that moved with that order would be a payout nobody could reproduce."""
        rng = random.Random(4)
        entries = [(wager_id, rng.randint(1, 10_000)) for wager_id in range(1, 9)]
        expected = apportion(entries, 123_457)
        for _ in range(20):
            shuffled = entries[:]
            rng.shuffle(shuffled)
            self.assertEqual(apportion(shuffled, 123_457), expected)

    def test_a_bigger_stake_is_never_paid_less_than_a_smaller_one(self):
        rng = random.Random(11)
        for _ in range(200):
            entries = [(i, rng.randint(1, 100_000)) for i in range(1, 7)]
            payouts = apportion(entries, sum(s for _, s in entries) * 3)
            ranked = sorted(entries, key=lambda e: e[1])
            for (lo_id, _), (hi_id, _) in zip(ranked, ranked[1:]):
                self.assertLessEqual(payouts[lo_id], payouts[hi_id])


class ReturnMultipleTests(unittest.TestCase):
    def test_an_unbacked_side_has_no_multiple(self):
        pools = Pools(for_cents=0, against_cents=500, for_backers=0, against_backers=1)
        self.assertIsNone(return_multiple(pools, FOR))

    def test_an_unopposed_side_returns_exactly_the_stake(self):
        """Nothing staked against it means the pot is just that side's own
        money, so the multiple is 1.0 - the reason the embed says "nothing
        staked against this yet" instead of printing it."""
        pools = Pools(for_cents=500, against_cents=0, for_backers=1, against_backers=0)
        self.assertAlmostEqual(return_multiple(pools, FOR), 1.0)

    def test_the_crowded_side_pays_less(self):
        pools = Pools(
            for_cents=9_000, against_cents=1_000, for_backers=9, against_backers=1
        )
        self.assertLess(return_multiple(pools, FOR), return_multiple(pools, AGAINST))

    def test_both_multiples_share_the_same_pot(self):
        pools = Pools(for_cents=2_500, against_cents=7_500, for_backers=3, against_backers=2)
        self.assertAlmostEqual(
            pools.for_cents * return_multiple(pools, FOR), pools.pot_cents
        )
        self.assertAlmostEqual(
            pools.against_cents * return_multiple(pools, AGAINST), pools.pot_cents
        )


class CentsTests(unittest.TestCase):
    def test_a_currency_amount_becomes_whole_cents(self):
        self.assertEqual(to_cents(12.34), 1_234)
        self.assertEqual(to_cents(0.01), 1)
        self.assertEqual(to_cents(1_000), 100_000)

    def test_it_survives_the_floats_that_usually_go_wrong(self):
        # 0.29 * 100 is 28.999999999999996 in IEEE-754, which is the case
        # utils/formatting.py: format_price also has to nudge around.
        for amount, expected in ((0.29, 29), (1.15, 115), (8.16, 816), (33.33, 3_333)):
            with self.subTest(amount=amount):
                self.assertEqual(to_cents(amount), expected)

    def test_it_round_trips(self):
        for cents in (1, 7, 100, 12_345, 9_999_999):
            self.assertEqual(to_cents(from_cents(cents)), cents)


class ClockTests(unittest.TestCase):
    def test_closing_time_is_stored_in_sqlites_own_layout(self):
        now = datetime(2026, 9, 18, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(closes_at_text(6, now), "2026-09-18 20:30:00")

    def test_it_sorts_as_text(self):
        """closes_at is compared against datetime('now') as a string, so the
        layout has to keep chronological order under a plain text compare."""
        now = datetime(2026, 9, 18, 14, 30, 0, tzinfo=timezone.utc)
        stamps = [closes_at_text(h, now) for h in (1, 5, 24, 200, 336)]
        self.assertEqual(stamps, sorted(stamps))

    def test_hours_until_counts_down(self):
        now = datetime(2026, 9, 18, 14, 30, 0, tzinfo=timezone.utc)
        closes = closes_at_text(6, now)
        self.assertAlmostEqual(hours_until(closes, now), 6.0, places=6)
        self.assertAlmostEqual(
            hours_until(closes, now + timedelta(hours=2)), 4.0, places=6
        )

    def test_a_passed_deadline_clamps_at_zero(self):
        """format_relative_timestamp is given this figure and renders a point
        in the future; a negative would put the countdown in the past."""
        now = datetime(2026, 9, 18, 14, 30, 0, tzinfo=timezone.utc)
        closes = closes_at_text(1, now)
        self.assertEqual(hours_until(closes, now + timedelta(days=3)), 0.0)


if __name__ == "__main__":
    unittest.main()
