"""
Tests for 1.2's mining changes: the pool's per-material composition and the
gemstone guarantee that rides on it, and the mining focus.

The invariant worth stating up front, because several tests here exist only to
defend it: a count is stored twice. drills.stored_amount must equal the sum of
that drill's drill_contents, and server_config.mining_pool_remaining must equal
the sum of that guild's server_mining_pool. Both are denormalised on purpose
(capacity, is_full and the pool cap are all counts), and both are one bug away
from a player's drill handing them the wrong amount.
"""
import random
import tempfile
import unittest
from pathlib import Path

from database.db import Database
from data.materials import (
    DEFAULT_MINING_FOCUS,
    GEMSTONES,
    MINING_POOL_BAG_SIZE,
    MINING_FOCUSES,
    ORES,
    RAW_MATERIALS,
    accrue,
    apply_mining_focus,
    draw_from_pool,
    focus_conversion_rate,
    pool_bag_contents,
)
from utils.db_helpers import ensure_server_row, ensure_user_row, get_user_quantity
from utils.drills import add_drill_contents, take_drill_contents
from utils.mining_focus import convert_haul, get_focus, set_focus
from utils.mining_pool import pool_contents, pool_display_lines, refill_pool, take_from_pool

GUILD = 4242
USER = 77


class AccrueTests(unittest.TestCase):
    """The one primitive behind every fractional accumulation in the game."""

    def test_it_hands_over_whole_units_and_keeps_the_rest(self):
        for carry_in, amount, whole, carry_out in (
            (0.0, 2.7, 2, 0.7),
            (0.7, 0.4, 1, 0.1),
            (0.0, 0.9, 0, 0.9),
        ):
            with self.subTest(carry=carry_in, amount=amount):
                got_whole, got_carry = accrue(carry_in, amount)
                self.assertEqual(got_whole, whole)
                self.assertAlmostEqual(got_carry, carry_out, places=9)

    def test_the_carry_stays_a_genuine_fraction(self):
        carry = 0.0
        for _ in range(500):
            _, carry = accrue(carry, 0.1)
            self.assertGreaterEqual(carry, 0.0)
            self.assertLess(carry, 1.0)

    def test_nothing_is_lost_over_many_small_steps(self):
        # The reason it exists at all. A tenth added a thousand times is a
        # hundred, and rounding each step in isolation gives zero.
        carry, total = 0.0, 0
        for _ in range(1000):
            whole, carry = accrue(carry, 0.1)
            total += whole
        self.assertEqual(total, 100)


class DrawFromPoolTests(unittest.TestCase):
    def test_it_never_draws_more_than_is_there(self):
        rng = random.Random(1)
        self.assertEqual(draw_from_pool({"coal": 3}, 10, rng), {"coal": 3})

    def test_it_empties_a_pool_exactly(self):
        rng = random.Random(2)
        pool = {"iron_ore": 10, "coal": 2, "diamond": 1}
        self.assertEqual(draw_from_pool(pool, 13, rng), pool)

    def test_it_draws_without_replacement(self):
        # The property that makes a gemstone guaranteed rather than merely
        # likely: a single diamond in the pool can come out at most once, and
        # over enough draws of the whole pool it always comes out.
        rng = random.Random(3)
        for _ in range(50):
            drawn = draw_from_pool({"iron_ore": 5, "diamond": 1}, 6, rng)
            self.assertEqual(drawn["diamond"], 1)

    def test_an_empty_pool_yields_nothing(self):
        self.assertEqual(draw_from_pool({}, 5), {})
        self.assertEqual(draw_from_pool({"coal": 0}, 5), {})

    def test_drawing_nothing_is_harmless(self):
        self.assertEqual(draw_from_pool({"coal": 5}, 0), {})


class FocusArithmeticTests(unittest.TestCase):
    def test_a_copper_is_worth_two_iron(self):
        # The headline rule, and it lands exactly: iron ore's drop chance is
        # precisely twice copper ore's.
        self.assertEqual(focus_conversion_rate("copper_ore", "iron_ore"), 2.0)
        self.assertEqual(focus_conversion_rate("iron_ore", "copper_ore"), 0.5)

    def test_balanced_changes_nothing(self):
        haul = {"iron_ore": 57, "copper_ore": 28, "coal": 15}
        converted, carry = apply_mining_focus(DEFAULT_MINING_FOCUS, haul)
        self.assertEqual(converted, haul)
        self.assertEqual(carry, 0.0)

    def test_iron_focus_turns_copper_into_iron_and_keeps_coal(self):
        converted, _ = apply_mining_focus("iron", {"iron_ore": 100, "copper_ore": 50, "coal": 20})
        self.assertEqual(converted, {"iron_ore": 200, "coal": 20})

    def test_copper_focus_leaves_no_iron_ore_at_all(self):
        # Which is why it can't make steel - the note the /focus menu leads on.
        converted, _ = apply_mining_focus("copper", {"iron_ore": 100, "copper_ore": 50, "coal": 20})
        self.assertNotIn("iron_ore", converted)
        self.assertEqual(converted["copper_ore"], 100)

    def test_coal_focus_converts_everything(self):
        converted, _ = apply_mining_focus("coal", {"iron_ore": 100, "copper_ore": 50, "coal": 20})
        self.assertEqual(set(converted), {"coal"})

    def test_gemstones_are_never_touched(self):
        # The promise of the whole feature: what you choose changes which ore
        # you get, never your odds on the things worth having.
        haul = {"iron_ore": 100, "copper_ore": 50, "ruby": 1, "obsidian": 2, "diamond": 3}
        for focus_id in MINING_FOCUSES:
            with self.subTest(focus=focus_id):
                converted, _ = apply_mining_focus(focus_id, haul)
                for gem in ("ruby", "obsidian", "diamond"):
                    self.assertEqual(converted[gem], haul[gem])

    def test_a_focus_never_mints_more_than_the_rarity_ratio_allows(self):
        # The safety property. Converting at drop-chance ratio can raise the
        # item COUNT (an iron focus does), but never the total rarity-weighted
        # value, which is what "an even trade for the digging you did" means.
        haul = {"iron_ore": 5667, "copper_ore": 2834, "coal": 1499}
        weight = lambda b: sum(q / RAW_MATERIALS[m]["drop_chance"] for m, q in b.items())
        for focus_id in MINING_FOCUSES:
            with self.subTest(focus=focus_id):
                converted, carry = apply_mining_focus(focus_id, haul)
                self.assertLessEqual(weight(converted), weight(haul) + 1e-6)

    def test_the_carry_makes_small_collections_add_up(self):
        # The exploit this closes: a coal focus turns one iron ore into 0.264
        # coal. Rounding up would let somebody collect one item at a time and
        # get 3.8x their material; rounding down would destroy it. Twenty
        # single-item collections have to equal one collection of twenty.
        carry, piecemeal = 0.0, 0
        for _ in range(20):
            converted, carry = apply_mining_focus("coal", {"iron_ore": 1}, carry)
            piecemeal += converted.get("coal", 0)
        at_once, _ = apply_mining_focus("coal", {"iron_ore": 20})
        self.assertEqual(piecemeal, at_once["coal"])

    def test_the_carry_never_runs_away(self):
        carry = 0.0
        for _ in range(200):
            _, carry = apply_mining_focus("coal", {"iron_ore": 1}, carry)
            self.assertGreaterEqual(carry, 0.0)
            self.assertLess(carry, 1.0)


class MiningDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def refill(self):
        async with self.db.transaction() as tx:
            return await refill_pool(tx, GUILD)

    async def take(self, count):
        async with self.db.transaction() as tx:
            return await take_from_pool(tx, GUILD, count)

    async def pool_total(self):
        row = await self.db.fetchone(
            "SELECT mining_pool_remaining FROM server_config WHERE guild_id = ?", (GUILD,)
        )
        return row["mining_pool_remaining"]

    async def assert_pool_agrees(self):
        """The denormalisation invariant: the total and the composition are one
        fact stored twice, and every write has to move both."""
        contents = await pool_contents(self.db, GUILD)
        self.assertEqual(sum(contents.values()), await self.pool_total())


class BagContentsTests(unittest.TestCase):
    """The bag is the gemstone guarantee. Its exact contents ARE the promise."""

    def test_a_bag_holds_exactly_its_stated_size(self):
        # mining_pool_remaining is kept as this total, so a bag that doesn't sum
        # is a pool that invents or destroys items on every refill.
        self.assertEqual(sum(pool_bag_contents().values()), MINING_POOL_BAG_SIZE)

    def test_it_holds_the_published_gemstone_counts(self):
        bag = pool_bag_contents()
        self.assertEqual(bag["ruby"], 90)
        self.assertEqual(bag["obsidian"], 9)
        self.assertEqual(bag["diamond"], 1)

    def test_gem_density_matches_the_published_drop_rates(self):
        # The bag removes VARIANCE, not the average. A player reading the drop
        # table should find it still true over a bag.
        bag = pool_bag_contents()
        for material_id, info in RAW_MATERIALS.items():
            with self.subTest(material=material_id):
                self.assertAlmostEqual(
                    bag[material_id] / MINING_POOL_BAG_SIZE, info["drop_chance"], places=6
                )

    def test_an_odd_bag_size_still_sums_exactly(self):
        # The remainder has to land somewhere; the largest material absorbs it.
        for size in (7, 999, 123_457):
            with self.subTest(size=size):
                self.assertEqual(sum(pool_bag_contents(size).values()), size)


class PoolBagTests(MiningDatabaseTestCase):
    async def test_a_refill_puts_a_whole_bag_in(self):
        await self.refill()
        self.assertEqual(await self.pool_total(), MINING_POOL_BAG_SIZE)
        await self.assert_pool_agrees()

    async def test_refilling_adds_rather_than_replacing(self):
        # Only matters at the bag boundary, but the failure is silent deletion
        # of whatever the old bag had left.
        await self.refill()
        await self.take(400)
        await self.refill()
        self.assertEqual(await self.pool_total(), MINING_POOL_BAG_SIZE * 2 - 400)
        await self.assert_pool_agrees()

    async def test_drawing_moves_the_total_and_the_composition_together(self):
        await self.refill()
        drawn = await self.take(50)
        self.assertEqual(sum(drawn.values()), 50)
        await self.assert_pool_agrees()

    async def test_an_empty_pool_refills_itself_rather_than_giving_nothing(self):
        # The headline of the change. There is no daily allowance any more, so
        # a drill must never be told the server has run out.
        self.assertEqual(await self.pool_total(), 0)
        drawn = await self.take(10)
        self.assertEqual(sum(drawn.values()), 10)
        await self.assert_pool_agrees()

    async def test_a_draw_spanning_the_end_of_a_bag_is_served_in_full(self):
        await self.refill()
        await self.take(MINING_POOL_BAG_SIZE - 3)
        drawn = await self.take(40)
        self.assertEqual(sum(drawn.values()), 40)
        await self.assert_pool_agrees()

    async def test_mining_is_never_throttled_however_much_is_taken(self):
        # Two full bags' worth in a day would once have been impossible - the
        # cap was three days of production and a day was 200 per member.
        for _ in range(5):
            drawn = await self.take(500_000)
            self.assertEqual(sum(drawn.values()), 500_000)
        await self.assert_pool_agrees()

    async def test_draining_a_bag_always_yields_the_diamond(self):
        # The guarantee, end to end: no timers, no floor, no luck. Drain a bag,
        # get a diamond, on every server, every time.
        await self.refill()
        total = 0
        taken = {}
        while total < MINING_POOL_BAG_SIZE:
            chunk = await self.take(min(50_000, MINING_POOL_BAG_SIZE - total))
            for material_id, quantity in chunk.items():
                taken[material_id] = taken.get(material_id, 0) + quantity
            total += sum(chunk.values())
        self.assertEqual(taken["diamond"], 1)
        self.assertEqual(taken["obsidian"], 9)
        self.assertEqual(taken["ruby"], 90)


class PoolDisplayTests(MiningDatabaseTestCase):
    """What /mine status renders. Every line must be a fact - there are no
    estimates left in this embed, which is the point of the bag."""

    async def test_it_names_the_gemstones_actually_in_the_bag(self):
        await self.refill()
        contents = await pool_contents(self.db, GUILD)
        rendered = " ".join(pool_display_lines(await self.pool_total(), contents))
        self.assertIn("Gemstones remaining", rendered)

    async def test_it_says_so_plainly_when_the_gems_are_gone(self):
        await self.refill()
        for gem in GEMSTONES:
            await self.db.execute(
                "UPDATE server_mining_pool SET quantity = 0 WHERE guild_id = ? AND material_id = ?",
                (GUILD, gem),
            )
        contents = await pool_contents(self.db, GUILD)
        rendered = " ".join(pool_display_lines(1000, contents))
        self.assertIn("No gemstones left", rendered)

    async def test_it_does_not_quote_a_fraction_of_the_bag_size(self):
        # A refill ADDS a bag to whatever was left, so the remaining count can
        # legitimately exceed MINING_POOL_BAG_SIZE - it does on every server the
        # moment the 1.2 migration runs. "1,002,786 / 1,000,000" is a lie about
        # what the number means, so there is no denominator at all.
        await self.refill()
        await self.refill()
        total = await self.pool_total()
        self.assertGreater(total, MINING_POOL_BAG_SIZE)
        rendered = " ".join(pool_display_lines(total, await pool_contents(self.db, GUILD)))
        self.assertIn(f"{total:,}", rendered)
        self.assertNotIn(f"/ {MINING_POOL_BAG_SIZE:,}", rendered)

    async def test_it_never_claims_a_gem_that_is_not_there(self):
        await self.refill()
        await self.db.execute(
            "UPDATE server_mining_pool SET quantity = 0 WHERE guild_id = ? AND material_id = 'diamond'",
            (GUILD,),
        )
        contents = await pool_contents(self.db, GUILD)
        rendered = " ".join(pool_display_lines(await self.pool_total(), contents))
        self.assertNotIn(RAW_MATERIALS["diamond"]["emoji"], rendered)


class FocusPersistenceTests(MiningDatabaseTestCase):
    async def test_someone_who_never_unlocked_it_reads_as_default(self):
        focus_id, carry, last_changed, unlocked = await get_focus(self.db, USER)
        self.assertEqual(focus_id, DEFAULT_MINING_FOCUS)
        self.assertEqual(carry, 0.0)
        self.assertFalse(unlocked)

    async def test_the_row_is_the_unlock(self):
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "iron", "2026-08-10")
        _, _, _, unlocked = await get_focus(self.db, USER)
        self.assertTrue(unlocked)

    async def test_an_unknown_focus_falls_back_rather_than_stranding_anyone(self):
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "retired_focus", "2026-08-10")
        focus_id, _, _, unlocked = await get_focus(self.db, USER)
        self.assertEqual(focus_id, DEFAULT_MINING_FOCUS)
        self.assertTrue(unlocked)

    async def test_changing_focus_resets_the_carry(self):
        # A fraction of a copper owed under one focus must not be paid out as
        # iron under the next.
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "coal", "2026-08-10")
            await convert_haul(tx, USER, {"iron_ore": 1})
        _, carry, _, _ = await get_focus(self.db, USER)
        self.assertGreater(carry, 0.0)

        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "iron", "2026-08-11")
        _, carry, _, _ = await get_focus(self.db, USER)
        self.assertEqual(carry, 0.0)


class ConvertHaulTests(MiningDatabaseTestCase):
    async def test_a_locked_player_gets_their_haul_untouched(self):
        haul = {"iron_ore": 100, "copper_ore": 50}
        async with self.db.transaction() as tx:
            self.assertEqual(await convert_haul(tx, USER, haul), haul)

    async def test_it_converts_and_banks_the_remainder(self):
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "coal", "2026-08-10")
            converted = await convert_haul(tx, USER, {"iron_ore": 10})
        self.assertEqual(set(converted), {"coal"})
        _, carry, _, _ = await get_focus(self.db, USER)
        self.assertGreater(carry, 0.0)

    async def test_piecemeal_collection_earns_the_same_as_one_go(self):
        # The exploit closure, end to end through the database this time.
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "coal", "2026-08-10")
        for _ in range(20):
            async with self.db.transaction() as tx:
                converted = await convert_haul(tx, USER, {"iron_ore": 1})
                for material_id, quantity in converted.items():
                    await tx.execute(
                        "INSERT INTO user_materials (user_id, material_id, quantity) "
                        "VALUES (?, ?, ?) ON CONFLICT(user_id, material_id) DO UPDATE "
                        "SET quantity = quantity + excluded.quantity",
                        (USER, material_id, quantity),
                    )
        at_once, _ = apply_mining_focus("coal", {"iron_ore": 20})
        self.assertEqual(await get_user_quantity(self.db, USER, "coal"), at_once["coal"])

    async def test_an_empty_haul_is_left_alone(self):
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "iron", "2026-08-10")
            self.assertEqual(await convert_haul(tx, USER, {}), {})


class HarvestToCollectTests(MiningDatabaseTestCase):
    """Pool -> drill -> inventory, the way the harvest loop and /collect
    actually run it. The unit tests above each cover one hop; this is the one
    that would catch the two halves disagreeing about what a drill holds."""

    async def add_drill(self):
        return await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (?, ?, 'iron_drill')",
            (GUILD, USER),
        )

    async def harvest(self, drill_id, count):
        """One tick, as cogs/mining.py runs it: draw from the pool, then move
        stored_amount and drill_contents in the same transaction."""
        async with self.db.transaction() as tx:
            drawn = await take_from_pool(tx, GUILD, count)
            await tx.execute(
                "UPDATE drills SET stored_amount = stored_amount + ? WHERE drill_id = ?",
                (sum(drawn.values()), drill_id),
            )
            await add_drill_contents(tx, drill_id, drawn)
        return drawn

    async def collect(self, drill_id):
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM drills WHERE drill_id = ?", (drill_id,))
            await tx.execute(
                "UPDATE drills SET stored_amount = 0, is_full = 0 WHERE drill_id = ?", (drill_id,)
            )
            haul = await take_drill_contents(tx, row)
            return await convert_haul(tx, USER, haul)

    async def test_what_a_drill_holds_is_what_it_hands_over(self):
        drill_id = await self.add_drill()
        mined: dict[str, int] = {}
        for _ in range(5):
            for material_id, quantity in (await self.harvest(drill_id, 12)).items():
                mined[material_id] = mined.get(material_id, 0) + quantity

        row = await self.db.fetchone(
            "SELECT stored_amount FROM drills WHERE drill_id = ?", (drill_id,)
        )
        self.assertEqual(row["stored_amount"], sum(mined.values()))
        self.assertEqual(await self.collect(drill_id), mined)
        await self.assert_pool_agrees()

    async def test_collecting_clears_the_contents(self):
        drill_id = await self.add_drill()
        await self.harvest(drill_id, 30)
        await self.collect(drill_id)
        rows = await self.db.fetchall(
            "SELECT * FROM drill_contents WHERE drill_id = ?", (drill_id,)
        )
        self.assertEqual(rows, [])

    async def test_the_focus_applies_to_what_was_already_in_the_drill(self):
        # Conversion happens at collection, so choosing a focus re-aims ore a
        # drill was already holding. Harmless - the rule is fixed, so there is
        # nothing to time - and it spares emptying every drill before switching.
        drill_id = await self.add_drill()
        await self.harvest(drill_id, 200)
        async with self.db.transaction() as tx:
            await set_focus(tx, USER, "coal", "2026-08-10")
        self.assertEqual(set(await self.collect(drill_id)) - set(GEMSTONES), {"coal"})

    async def test_a_drill_never_hands_over_more_than_the_pool_gave_it(self):
        # The conservation property across the whole chain: nothing is minted
        # between the pool and the inventory.
        drill_id = await self.add_drill()
        drawn = await self.harvest(drill_id, 100)
        collected = await self.collect(drill_id)
        self.assertEqual(sum(collected.values()), sum(drawn.values()))


class FocusMenuTests(MiningDatabaseTestCase):
    """The two places a focus is named: Discord's own picker, which is plain
    text and the same for everybody, and the /focus embed, which is rendered
    per player and carries both the icon and the "(selected)" mark."""

    def picker(self):
        from cogs.mining import MiningCog

        cog = MiningCog.__new__(MiningCog)
        cog.db = self.db
        return cog

    def options(self):
        """The choices Discord is handed for /focus <focus>, read off the
        command itself rather than rebuilt here, so this can't agree with a
        copy of the list while disagreeing with what ships."""
        from cogs.mining import MiningCog

        parameter = next(p for p in MiningCog.focus.parameters if p.name == "focus")
        return parameter.choices

    def test_it_offers_every_focus(self):
        self.assertEqual([choice.value for choice in self.options()], list(MINING_FOCUSES))

    def test_no_option_carries_an_emoji(self):
        # A choice name is rendered as plain text: a unicode glyph doesn't come
        # through and a custom <:Name:ID> arrives as its own literal markup.
        for choice in self.options():
            with self.subTest(choice=choice.value):
                self.assertTrue(
                    choice.name.isascii(), f"{choice.name!r} carries a non-ASCII glyph"
                )
                self.assertNotIn("<:", choice.name)

    def test_no_option_is_marked_as_selected(self):
        # This list is built once at class definition and served identically to
        # every player, so a "(selected)" in it would be a claim about somebody
        # that is wrong for everybody else. The embed does that job.
        for choice in self.options():
            with self.subTest(choice=choice.value):
                self.assertNotIn("(selected)", choice.name)

    async def test_the_embed_puts_the_icon_in_the_heading(self):
        embed = self.picker()._focus_embed("iron", True)
        for field, info in zip(embed.fields, MINING_FOCUSES.values()):
            with self.subTest(field=field.name):
                self.assertTrue(field.name.startswith(info["emoji"]))

    async def test_the_embed_marks_the_selected_focus(self):
        embed = self.picker()._focus_embed("coal", True)
        info = MINING_FOCUSES["coal"]
        selected = [f.name for f in embed.fields if f.name.endswith(" (selected)")]
        self.assertEqual(selected, [f"{info['emoji']} {info['name']} (selected)"])

    async def test_the_embed_marks_balance_before_anything_is_unlocked(self):
        # A player who has never paid the ruby is mining Balance, not mining
        # nothing, and the menu should say so - it's the only place that
        # answers "what am I on right now?".
        embed = self.picker()._focus_embed(DEFAULT_MINING_FOCUS, False)
        marked = [f.name for f in embed.fields if "(selected)" in f.name]
        self.assertEqual(len(marked), 1)
        self.assertIn(MINING_FOCUSES[DEFAULT_MINING_FOCUS]["name"], marked[0])


class FocusDataTests(unittest.TestCase):
    def test_every_focus_keeps_something_and_names_it(self):
        for focus_id, info in MINING_FOCUSES.items():
            with self.subTest(focus=focus_id):
                self.assertTrue(info["name"])
                self.assertTrue(info["emoji"])
                self.assertTrue(info["blurb"])
                self.assertTrue(info["keep"])

    def test_a_primary_is_always_something_the_focus_keeps(self):
        for focus_id, info in MINING_FOCUSES.items():
            with self.subTest(focus=focus_id):
                if info["primary"] is not None:
                    self.assertIn(info["primary"], info["keep"])

    def test_every_kept_material_is_an_ore(self):
        # Gemstones must never appear in `keep` or `primary` - a focus that
        # converted ore INTO a gemstone would be a gem faucet.
        for focus_id, info in MINING_FOCUSES.items():
            with self.subTest(focus=focus_id):
                for material_id in info["keep"]:
                    self.assertIn(material_id, ORES)

    def test_the_default_focus_exists_and_converts_nothing(self):
        self.assertIn(DEFAULT_MINING_FOCUS, MINING_FOCUSES)
        self.assertIsNone(MINING_FOCUSES[DEFAULT_MINING_FOCUS]["primary"])


if __name__ == "__main__":
    unittest.main()
