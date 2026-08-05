"""
Tests for what the scrapper gives back.

scrap_yield is pure balance arithmetic over data/materials.py, so all of this
runs without a bot or a database. The one test that matters more than the rest
is test_a_yield_never_exceeds_its_own_recipe: that invariant is what makes the
"always return at least one of the most valuable input" rule safe, and without
it some recipe somewhere would let a craft-then-scrap loop print materials.
"""
import unittest

from data.materials import (
    ALL_MATERIALS,
    COMPONENT_MATERIALS,
    DRILLS,
    GEMSTONES,
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    STORAGE_CONTAINERS,
    UPGRADE_MATERIALS,
    SCRAP_RETURN_RATE,
    raw_input_cost,
    scrap_yield,
    scrapper_rate,
)
from cogs.scrapper import SCRAPPABLE

# Everything the scrapper accepts, in either of its two commands.
SCRAPPABLE_EVERYTHING = {**SCRAPPABLE, **DRILLS}


class ScrapYieldInvariantTests(unittest.TestCase):
    def test_a_yield_never_exceeds_its_own_recipe(self):
        """The invariant the guaranteed-unit rule rests on: max(1, floor(0.5q))
        <= q for every q >= 1. Break this anywhere and crafting a thing and
        scrapping it becomes a way to make materials out of nothing."""
        for material_id, info in ALL_MATERIALS.items():
            inputs = info.get("inputs", {})
            for return_id, quantity in scrap_yield(material_id).items():
                with self.subTest(material=material_id, returns=return_id):
                    self.assertIn(return_id, inputs)
                    self.assertLessEqual(quantity, inputs[return_id])

    def test_a_yield_never_exceeds_its_recipe_in_value_either(self):
        # The per-input check above allows a recipe to return everything it
        # took; this pins that a scrap is never actually PROFITABLE in the
        # currency the market prices things in.
        for material_id in SCRAPPABLE_EVERYTHING:
            with self.subTest(material=material_id):
                back = sum(raw_input_cost(m) * q for m, q in scrap_yield(material_id).items())
                self.assertLessEqual(back, raw_input_cost(material_id) + 1e-9)

    def test_everything_the_scrapper_accepts_returns_something(self):
        # The whole reason the guaranteed unit exists. Under a plain floor
        # every drill returns {} - one of each part, halved and rounded down.
        for material_id in SCRAPPABLE_EVERYTHING:
            with self.subTest(material=material_id):
                self.assertTrue(scrap_yield(material_id))

    def test_raw_materials_have_nothing_to_give_back(self):
        for material_id in RAW_MATERIALS:
            self.assertEqual(scrap_yield(material_id), {})

    def test_an_unknown_id_yields_nothing_rather_than_raising(self):
        self.assertEqual(scrap_yield("not_a_material"), {})


class ScrapYieldShapeTests(unittest.TestCase):
    def test_a_drill_returns_exactly_one_component(self):
        # Every drill's recipe is one of each of three parts, so the only whole
        # thing half of it can be is a single part - the most valuable one.
        for drill_id in DRILLS:
            with self.subTest(drill=drill_id):
                yields = scrap_yield(drill_id)
                self.assertEqual(len(yields), 1)
                (component_id, quantity), = yields.items()
                self.assertEqual(quantity, 1)
                self.assertIn(component_id, COMPONENT_MATERIALS)

    def test_a_gemstone_is_never_destroyed_by_scrapping(self):
        """A Ruby Container costs one ruby; floor(0.5) is zero. Incinerating a
        5,500-value gem in exchange for ten copper is the failure the
        guaranteed unit exists to prevent."""
        for material_id, info in SCRAPPABLE_EVERYTHING.items():
            gems_in = {g: q for g, q in info.get("inputs", {}).items() if g in GEMSTONES}
            if not gems_in:
                continue
            with self.subTest(material=material_id):
                yields = scrap_yield(material_id)
                for gem_id in gems_in:
                    self.assertGreaterEqual(yields.get(gem_id, 0), 1)

    def test_ore_tier_items_come_back_at_about_half_their_value(self):
        # The rule as stated. Gem-tier items are excluded because the gem IS
        # essentially all of their value, so no half-subset of the recipe
        # exists - that's a known and accepted consequence, covered below.
        for material_id in ("wiring", "drill_chassis", "iron_drill_bit",
                            "steel_drill_bit", "iron_drill", "steel_drill",
                            "steel_container"):
            with self.subTest(material=material_id):
                back = sum(raw_input_cost(m) * q for m, q in scrap_yield(material_id).items())
                self.assertAlmostEqual(back / raw_input_cost(material_id), SCRAP_RETURN_RATE, delta=0.05)

    def test_scrapping_chains_down_to_raw_materials(self):
        # An Iron Drill has to be scrappable all the way to ore, one tier at a
        # time, or the "chain it as far as you need" promise is empty.
        seen = set()
        frontier = ["iron_drill"]
        while frontier:
            material_id = frontier.pop()
            if material_id in seen:
                continue
            seen.add(material_id)
            frontier.extend(scrap_yield(material_id))
        self.assertIn("drill_chassis", seen)
        self.assertIn("iron", seen)
        self.assertIn("iron_ore", seen)
        # And it terminates: ore has no recipe, so the walk above can't loop.
        self.assertEqual(scrap_yield("iron_ore"), {})


class ScrappableListTests(unittest.TestCase):
    def test_the_static_choice_list_fits_discords_limit(self):
        # app_commands.choices refuses more than 25 entries outright.
        self.assertLessEqual(len(SCRAPPABLE), 25)

    def test_it_covers_components_containers_and_upgrade_packs(self):
        self.assertEqual(
            set(SCRAPPABLE),
            set(COMPONENT_MATERIALS) | set(STORAGE_CONTAINERS) | set(UPGRADE_MATERIALS),
        )

    def test_ultra_dense_matter_is_not_scrappable(self):
        # Nothing consumes it yet, so scrapping it would only ever turn ten
        # diamonds into five.
        self.assertNotIn("ultra_dense_matter", SCRAPPABLE)
        self.assertNotIn("ultra_dense_matter", SCRAPPABLE_EVERYTHING)

    def test_raw_and_smelted_materials_are_not_scrappable(self):
        # Smelted metal is sellable, so it has an exit already; ore has no
        # recipe to undo at all.
        for material_id in set(RAW_MATERIALS) | set(SMELTED_MATERIALS):
            self.assertNotIn(material_id, SCRAPPABLE)


class ScrapperRateTests(unittest.TestCase):
    def test_the_rate_is_linear_and_uncapped(self):
        self.assertEqual(scrapper_rate(1), 2)
        self.assertEqual(scrapper_rate(2), 4)
        self.assertEqual(scrapper_rate(1000), 2000)


if __name__ == "__main__":
    unittest.main()
