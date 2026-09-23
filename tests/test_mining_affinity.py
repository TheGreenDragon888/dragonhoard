"""
Tests for the mining affinity: the gem tier's mirror of a mining focus.

Two properties carry most of this file. The first is that NOTHING HERE MINTS
GEM VALUE except the one bonus that is supposed to - the conversion rate is the
rarity ratio times MINING_AFFINITY_CONVERSION_BONUS and nothing else, and in
particular moving a part-finished carry between targets is bonus-free, because
the alternative is a player pumping a fraction of a diamond into a whole one by
switching back and forth.

The second is that a carry here is worth thousands of times what the focus's
carry is worth - up to 89 rubies of mining sits in it - so every test that
would be a nicety over there is load-bearing here.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cogs.mining import MiningCog
from database.db import Database
from data.materials import (
    DEFAULT_MINING_AFFINITY,
    GEMSTONES,
    MINING_AFFINITIES,
    MINING_AFFINITY_CONVERSION_BONUS,
    ORES,
    RAW_MATERIALS,
    apply_mining_affinity,
    focus_conversion_rate,
    pool_bag_contents,
    affinity_conversion_rate,
    affinity_ratio,
)
from utils.db_helpers import ensure_server_row, ensure_user_row, get_user_quantity
from utils.mining_affinity import (
    convert_gems,
    get_affinity,
    affinity_progress,
    set_affinity,
)

GUILD = 4242
USER = 77


class ConversionRateTests(unittest.TestCase):
    """The trades the picker quotes, and the arithmetic behind them."""

    def test_the_headline_trade_is_forty_five_rubies_to_a_diamond(self):
        self.assertEqual(affinity_ratio("ruby", "diamond"), (45, 1))

    def test_every_live_pair_is_a_whole_number_of_each(self):
        # The picker renders these directly, so a pair that needed a fraction
        # to express would reach a player as one.
        for source in GEMSTONES:
            for target in GEMSTONES:
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    per_source, per_target = affinity_ratio(source, target)
                    self.assertGreaterEqual(per_source, 1)
                    self.assertGreaterEqual(per_target, 1)
                    self.assertAlmostEqual(
                        per_target / per_source,
                        affinity_conversion_rate(source, target),
                        places=9,
                    )

    def test_the_rate_is_exactly_rarity_times_the_bonus(self):
        for source in GEMSTONES:
            for target in GEMSTONES:
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    self.assertAlmostEqual(
                        affinity_conversion_rate(source, target),
                        focus_conversion_rate(source, target)
                        * MINING_AFFINITY_CONVERSION_BONUS,
                        places=12,
                    )

    def test_a_bag_is_worth_the_same_whichever_gem_is_chosen(self):
        """The identity data/materials.py quotes: a bag's gems are exactly
        thirds by value, so two thirds of them get converted and the chosen
        third passes through unbonused - which comes to (2*BONUS + 1)/3 of a
        bag, the same for all three choices."""
        bag = pool_bag_contents()
        gems = {gem: bag[gem] for gem in GEMSTONES}
        expected = (2 * MINING_AFFINITY_CONVERSION_BONUS + 1) / 3

        for target in GEMSTONES:
            with self.subTest(target=target):
                converted, _ = apply_mining_affinity(target, gems)
                # Valued in diamond-equivalents, the unit the identity is
                # stated in.
                got = converted[target] * focus_conversion_rate(target, "diamond")
                self.assertAlmostEqual(got / 3.0, expected, places=9)

    def test_the_total_is_one_and_two_thirds_at_the_live_bonus(self):
        # The figure data/materials.py states. Pinned separately from the
        # identity above so retuning the bonus fails here and nowhere else.
        self.assertEqual(MINING_AFFINITY_CONVERSION_BONUS, 2.0)
        self.assertAlmostEqual((2 * MINING_AFFINITY_CONVERSION_BONUS + 1) / 3, 5 / 3, places=9)


class ApplyAffinityTests(unittest.TestCase):
    def test_ore_is_never_touched(self):
        haul = {material_id: 100 for material_id in ORES}
        for affinity_id in MINING_AFFINITIES:
            with self.subTest(affinity=affinity_id):
                converted, carry = apply_mining_affinity(affinity_id, haul)
                self.assertEqual(converted, haul)
                self.assertEqual(carry, 0.0)

    def test_the_chosen_gem_passes_through_unbonused(self):
        # Bonusing it would mean a diamond affinity doubled the diamonds a bag
        # holds, which is the pool's decision alone.
        converted, carry = apply_mining_affinity("diamond", {"diamond": 3})
        self.assertEqual(converted, {"diamond": 3})
        self.assertEqual(carry, 0.0)

    def test_the_default_converts_nothing(self):
        haul = {"ruby": 90, "obsidian": 9}
        converted, carry = apply_mining_affinity(DEFAULT_MINING_AFFINITY, haul)
        self.assertEqual(converted, haul)
        self.assertEqual(carry, 0.0)

    def test_forty_five_rubies_make_exactly_one_diamond(self):
        converted, carry = apply_mining_affinity("diamond", {"ruby": 45})
        self.assertEqual(converted, {"diamond": 1})
        self.assertAlmostEqual(carry, 0.0, places=9)

    def test_a_short_haul_banks_the_remainder_rather_than_rounding(self):
        converted, carry = apply_mining_affinity("diamond", {"ruby": 44})
        self.assertEqual(converted, {})
        self.assertGreater(carry, 0.0)
        self.assertLess(carry, 1.0)

    def test_the_source_gems_are_consumed(self):
        converted, _ = apply_mining_affinity("diamond", {"ruby": 45, "obsidian": 9})
        self.assertNotIn("ruby", converted)
        self.assertNotIn("obsidian", converted)

    def test_collecting_in_pieces_comes_to_the_same_as_collecting_at_once(self):
        carry = 0.0
        total = 0
        for _ in range(45):
            converted, carry = apply_mining_affinity("diamond", {"ruby": 1}, carry)
            total += converted.get("diamond", 0)
        at_once, _ = apply_mining_affinity("diamond", {"ruby": 45})
        self.assertEqual(total, at_once["diamond"])


class ProgressRenderingTests(unittest.TestCase):
    """Progress is a PERCENTAGE, and the reason is a real report rather than a
    preference.

    A player mined 38 rubies and 3 obsidian on a diamond affinity and was told
    "23 of 45 Rubies". That was a true rendering of the 0.511 they had banked,
    and it still read as though the obsidian had done nothing - it had in fact
    done 0.667 of that diamond against the rubies' 0.844. One carry fed by
    several gems cannot be honestly quoted in any one of them.
    """

    def test_nothing_to_report_reads_as_nothing(self):
        self.assertIsNone(affinity_progress("diamond", 0.0))
        self.assertIsNone(affinity_progress(DEFAULT_MINING_AFFINITY, 0.5))

    def test_the_mixed_haul_that_prompted_this_reads_as_one_number(self):
        _, carry = apply_mining_affinity("diamond", {"ruby": 38, "obsidian": 3})
        line = affinity_progress("diamond", carry)
        self.assertIn("51%", line)
        self.assertIn(RAW_MATERIALS["diamond"]["name"], line)
        # The unit that caused the confusion is gone entirely.
        self.assertNotIn(RAW_MATERIALS["ruby"]["name"], line)

    def test_a_ruby_affinity_never_has_progress_to_report(self):
        # Every rate into a ruby makes at least one whole ruby, so there is
        # never a fraction owing and a percentage would be reporting on a carry
        # that cannot exist.
        self.assertIsNone(affinity_progress("ruby", 0.5))

    def test_it_floors_rather_than_rounding_up(self):
        # 99.9% must not read as 100%, which is a line claiming a whole gem the
        # player has not earned.
        self.assertIn("99%", affinity_progress("diamond", 0.999))

    def test_a_rounding_crumb_gets_no_line_at_all(self):
        self.assertIsNone(affinity_progress("diamond", 0.004))


class AffinityDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def set_to(self, affinity_id, day="2026-09-20"):
        async with self.db.transaction() as tx:
            return await set_affinity(tx, USER, affinity_id, day)

    async def collect(self, haul):
        async with self.db.transaction() as tx:
            return await convert_gems(tx, USER, haul)


class AffinityPersistenceTests(AffinityDatabaseTestCase):
    async def test_someone_who_never_unlocked_it_reads_as_default(self):
        affinity_id, carry, _, unlocked = await get_affinity(self.db, USER)
        self.assertEqual(affinity_id, DEFAULT_MINING_AFFINITY)
        self.assertEqual(carry, 0.0)
        self.assertFalse(unlocked)

    async def test_the_row_is_the_unlock(self):
        await self.set_to("diamond")
        _, _, _, unlocked = await get_affinity(self.db, USER)
        self.assertTrue(unlocked)

    async def test_an_unknown_affinity_falls_back_rather_than_stranding_anyone(self):
        await self.set_to("retired_affinity")
        affinity_id, _, _, unlocked = await get_affinity(self.db, USER)
        self.assertEqual(affinity_id, DEFAULT_MINING_AFFINITY)
        self.assertTrue(unlocked)

    async def test_a_locked_player_keeps_their_gems(self):
        haul = {"ruby": 90}
        self.assertEqual(await self.collect(haul), haul)

    async def test_the_carry_is_persisted_across_collections(self):
        await self.set_to("diamond")
        for _ in range(45):
            await self.collect({"ruby": 1})
        self.assertEqual(await get_user_quantity(self.db, USER, "diamond"), 0)
        # convert_gems returns the haul; the credit is the caller's job, so
        # what this asserts is that the 45th call produced the diamond.
        _, carry, _, _ = await get_affinity(self.db, USER)
        self.assertAlmostEqual(carry, 0.0, places=9)


class CarryOnChangeTests(AffinityDatabaseTestCase):
    """The rules that differ from a focus, all of them because a fraction of a
    gem is worth real money."""

    async def test_a_change_converts_the_carry_instead_of_resetting_it(self):
        await self.set_to("diamond")
        await self.collect({"ruby": 44})
        _, before, _, _ = await get_affinity(self.db, USER)
        self.assertGreater(before, 0.0)

        await self.set_to("obsidian", "2026-09-21")
        _, after, _, _ = await get_affinity(self.db, USER)
        # 44 rubies is 0.978 of a diamond and 8.8 obsidian: 8 fall out as whole
        # gems and 0.8 stays behind.
        self.assertGreater(after, 0.0)
        self.assertEqual(await get_user_quantity(self.db, USER, "obsidian"), 8)

    async def test_a_change_pays_out_the_whole_gems_it_frees(self):
        await self.set_to("diamond")
        await self.collect({"ruby": 44})
        paid = await self.set_to("ruby", "2026-09-21")
        # 0.978 of a diamond is 88 rubies.
        self.assertEqual(paid, {"ruby": 88})
        self.assertEqual(await get_user_quantity(self.db, USER, "ruby"), 88)

    async def test_switching_back_and_forth_mints_nothing(self):
        """The reason the carry moves at the RARITY ratio rather than the
        bonused one: re-bonusing already-earned value on every change would be
        a printer driven by a free daily action."""
        await self.set_to("diamond")
        await self.collect({"ruby": 44})
        _, start, _, _ = await get_affinity(self.db, USER)

        for day, target in enumerate(["ruby", "diamond", "obsidian", "diamond"], start=21):
            await self.set_to(target, f"2026-09-{day}")

        _, end, _, _ = await get_affinity(self.db, USER)
        held = {gem: await get_user_quantity(self.db, USER, gem) for gem in GEMSTONES}
        total = end + sum(
            held[gem] * focus_conversion_rate(gem, "diamond") for gem in GEMSTONES
        )
        self.assertAlmostEqual(total, start, places=6)

    async def test_turning_it_off_banks_the_progress_rather_than_burning_it(self):
        await self.set_to("diamond")
        await self.collect({"ruby": 44})
        paid = await self.set_to("none", "2026-09-21")
        self.assertEqual(paid, {"ruby": 88})
        _, carry, _, _ = await get_affinity(self.db, USER)
        self.assertEqual(carry, 0.0)

    async def test_the_first_unlock_has_no_carry_to_move(self):
        paid = await self.set_to("diamond")
        self.assertEqual(paid, {})
        _, carry, _, _ = await get_affinity(self.db, USER)
        self.assertEqual(carry, 0.0)


class IndependenceTests(AffinityDatabaseTestCase):
    async def test_a_affinity_leaves_a_hauls_ore_exactly_as_it_found_it(self):
        await self.set_to("diamond")
        haul = {"iron_ore": 500, "copper_ore": 250, "coal": 125, "ruby": 45}
        converted = await self.collect(dict(haul))
        for material_id in ORES:
            self.assertEqual(converted[material_id], haul[material_id])
        self.assertEqual(converted["diamond"], 1)


if __name__ == "__main__":
    unittest.main()


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = "Tester"


class FakeInteraction:
    def __init__(self, guild_id, user_id):
        self.guild_id = guild_id
        self.guild = None
        self.user = FakeUser(user_id)
        self.response = AsyncMock()


class CollectReceiptTests(AffinityDatabaseTestCase):
    """The receipt field that stops a gem find reading as an empty haul.

    A player aiming at a diamond who collects a ruby receives nothing at all in
    the materials list - 45 of them make one - so without this field the rarest
    event in the game looks like a bug.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog = MiningCog.__new__(MiningCog)
        self.cog.db = self.db

    async def drill_holding(self, **contents):
        total = sum(contents.values())
        drill_id = await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level, is_full, "
            "stored_amount) VALUES (?, ?, 'iron_drill', 1, 0, ?)",
            (GUILD, USER, total),
        )
        for material_id, quantity in contents.items():
            await self.db.execute(
                "INSERT INTO drill_contents (drill_id, material_id, quantity) VALUES (?, ?, ?)",
                (drill_id, material_id, quantity),
            )

    async def collect_embed(self):
        interaction = FakeInteraction(GUILD, USER)
        await MiningCog.collect.callback(self.cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        return kwargs["embeds"][0] if "embeds" in kwargs else kwargs["embed"]

    def field(self, embed, needle):
        return next((f.value for f in embed.fields if needle in f.name), None)

    async def test_a_single_ruby_does_not_read_as_an_empty_haul(self):
        await self.set_to("diamond")
        await self.drill_holding(ruby=1)
        embed = await self.collect_embed()
        field = self.field(embed, "Affinity")
        self.assertIsNotNone(field, "a converted gem must be reported somewhere")
        self.assertIn(RAW_MATERIALS["ruby"]["name"], field)
        self.assertIn("2%", field)

    async def test_it_says_how_many_gems_the_affinity_made(self):
        # The materials list cannot answer this: a diamond in it is just a
        # diamond, whether the pool produced it or 45 rubies were melted down.
        await self.set_to("diamond")
        await self.drill_holding(ruby=45)
        embed = await self.collect_embed()
        field = self.field(embed, "Affinity")
        self.assertIn(f"**1 {RAW_MATERIALS['diamond']['name']}** was created this collection", field)
        self.assertIn(f"**45 {RAW_MATERIALS['ruby']['plural']}**", field)

    async def test_more_than_one_created_gem_reads_as_a_plural(self):
        await self.set_to("diamond")
        await self.drill_holding(ruby=90)
        embed = await self.collect_embed()
        field = self.field(embed, "Affinity")
        self.assertIn(f"**2 {RAW_MATERIALS['diamond']['plural']}** were created this collection", field)

    async def test_a_mixed_haul_counts_every_gem_that_fed_it(self):
        # The reported case: 38 rubies and 3 obsidian make one diamond between
        # them, and neither line may be left out of the explanation.
        await self.set_to("diamond")
        await self.drill_holding(ruby=38, obsidian=3)
        embed = await self.collect_embed()
        field = self.field(embed, "Affinity")
        self.assertIn(f"**38 {RAW_MATERIALS['ruby']['plural']}**", field)
        self.assertIn(f"**3 {RAW_MATERIALS['obsidian']['name']}**", field)
        self.assertIn("was created this collection", field)
        self.assertIn("51%", field)

    async def test_a_gem_drawn_from_the_pool_is_not_claimed_as_created(self):
        await self.set_to("diamond")
        await self.drill_holding(diamond=1)
        embed = await self.collect_embed()
        field = self.field(embed, "Affinity")
        self.assertIsNone(field)

    async def test_it_names_what_the_gems_became(self):
        await self.set_to("diamond")
        await self.drill_holding(ruby=45)
        embed = await self.collect_embed()
        field = self.field(embed, "Affinity")
        self.assertIn(RAW_MATERIALS["diamond"]["name"], field)

    async def test_a_player_without_a_affinity_gets_no_such_field(self):
        await self.drill_holding(ruby=1)
        embed = await self.collect_embed()
        self.assertIsNone(self.field(embed, "Affinity"))

    async def test_an_ore_only_haul_gets_no_such_field(self):
        await self.set_to("diamond")
        await self.drill_holding(iron_ore=50)
        embed = await self.collect_embed()
        self.assertIsNone(self.field(embed, "Affinity"))
