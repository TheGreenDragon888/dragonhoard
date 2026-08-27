"""
Balance-data tests for the hydraulic press and for uncapped infrastructure
levels. Pure arithmetic over data/materials.py - no database, no discord.py.
"""
import unittest

from data.materials import (
    ALL_MATERIALS,
    PRESS_MATERIALS,
    PRESS_RECIPES,
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    UPGRADE_THRESHOLD_BASE,
    UPGRADE_THRESHOLD_STEP,
    factory_rate,
    furnace_rate,
    get_material_info,
    press_rate_per_day,
    raw_input_cost,
    upgrade_threshold,
)


GEM_MATERIAL = (("ruby", "iron"), ("obsidian", "copper"), ("diamond", "steel"))


def mining_equivalent(gem, material):
    """What a player accumulates of `material` while mining one `gem`,
    recomputed here from the raw tables so the test doesn't just re-run the
    implementation it's checking."""
    items_per_gem = 1.0 / RAW_MATERIALS[gem]["drop_chance"]
    return min(
        items_per_gem * RAW_MATERIALS[raw]["drop_chance"] / per_unit
        for raw, per_unit in SMELTED_MATERIALS[material]["inputs"].items()
    )


class PressRecipeTests(unittest.TestCase):
    def test_costs_sit_just_under_what_mining_that_gem_would_have_yielded(self):
        # The design rule: a pressed gem costs very nearly the smelted material
        # you'd have accumulated mining one the hard way, minus a small edge
        # for owning a press.
        for gem, material in GEM_MATERIAL:
            charged = PRESS_RECIPES[gem]["inputs"][material]
            exact = mining_equivalent(gem, material)
            self.assertLess(charged, exact)
            self.assertGreater(charged, exact * 0.9)

    def test_the_discount_is_identical_for_every_recipe(self):
        # The load-bearing property. Discounting one gem more than another
        # would make it the only one worth pressing per unit of mining effort,
        # which is what the equal ratios exist to prevent.
        discounts = [
            PRESS_RECIPES[gem]["inputs"][material] / mining_equivalent(gem, material)
            for gem, material in GEM_MATERIAL
        ]
        for discount in discounts:
            self.assertAlmostEqual(discount, discounts[0], places=6)
        self.assertAlmostEqual(discounts[0], 1 - 0.0471, places=3)

    def test_costs_keep_the_one_five_fortyfive_ratio(self):
        # Ruby : obsidian : diamond, straight out of the drop rates. Both the
        # exact equivalents and the charged costs have to share it.
        ruby_cost = PRESS_RECIPES["ruby"]["inputs"]["iron"]
        ruby_exact = mining_equivalent("ruby", "iron")
        for gem, material, ratio in (("ruby", "iron", 1), ("obsidian", "copper", 5), ("diamond", "steel", 45)):
            self.assertEqual(PRESS_RECIPES[gem]["inputs"][material], ruby_cost * ratio)
            self.assertAlmostEqual(mining_equivalent(gem, material) / ruby_exact, ratio, places=6)

    def test_the_derived_costs_are_the_expected_figures(self):
        # Pinned so a drop-rate change that moves these is noticed rather than
        # silently rebalancing the press.
        self.assertEqual(PRESS_RECIPES["ruby"]["inputs"], {"iron": 600})
        self.assertEqual(PRESS_RECIPES["obsidian"]["inputs"], {"copper": 3000})
        self.assertEqual(PRESS_RECIPES["diamond"]["inputs"], {"steel": 27000})
        self.assertEqual(PRESS_RECIPES["ultra_dense_matter"]["inputs"], {"diamond": 10})

    def test_every_recipe_is_worth_about_the_same_per_unit_mined(self):
        # Gem ceiling prices are set so mining one item is worth the same
        # whichever gem it turns up (materials.py). The press has to preserve
        # that or one recipe becomes the only rational choice.
        values = []
        for gem, material in GEM_MATERIAL:
            cost = PRESS_RECIPES[gem]["inputs"][material]
            items = max(
                cost * per_unit / RAW_MATERIALS[raw]["drop_chance"]
                for raw, per_unit in SMELTED_MATERIALS[material]["inputs"].items()
            )
            values.append(RAW_MATERIALS[gem]["market_price"] / items)
        self.assertLess(max(values) / min(values), 1.10)

    def test_steel_is_limited_by_iron_ore_not_coal(self):
        # If coal ever became the binding input the diamond recipe would jump,
        # so the assumption is worth pinning.
        items = 1.0 / RAW_MATERIALS["diamond"]["drop_chance"]
        recipe = SMELTED_MATERIALS["steel"]["inputs"]
        by_iron = items * RAW_MATERIALS["iron_ore"]["drop_chance"] / recipe["iron_ore"]
        by_coal = items * RAW_MATERIALS["coal"]["drop_chance"] / recipe["coal"]
        self.assertLess(by_iron, by_coal)

    def test_press_days_trebles_each_tier(self):
        self.assertEqual(
            [PRESS_RECIPES[p]["press_days"] for p in ("ruby", "obsidian", "diamond", "ultra_dense_matter")],
            [1, 3, 9, 27],
        )

    def test_every_recipe_output_and_input_resolves(self):
        for product_id, recipe in PRESS_RECIPES.items():
            self.assertIsNotNone(get_material_info(product_id), product_id)
            for input_id in recipe["inputs"]:
                self.assertIsNotNone(get_material_info(input_id), input_id)

    def test_ultra_dense_matter_is_a_registered_material(self):
        info = get_material_info("ultra_dense_matter")
        self.assertIsNotNone(info)
        self.assertTrue(info["name"] and info["emoji"])
        self.assertGreater(raw_input_cost("ultra_dense_matter"), 0.0)

    def test_ultra_dense_matter_is_not_tradeable(self):
        # docs/market.md section 3: crafted and finished goods never trade.
        from cogs.economy import TRADEABLE_MATERIALS

        for material_id in PRESS_MATERIALS:
            self.assertNotIn(material_id, TRADEABLE_MATERIALS)
            self.assertNotIn("market_price", ALL_MATERIALS[material_id])

    def test_pressed_gems_are_ordinary_raw_materials(self):
        # The press outputs the same rubies mining does - no synthetic twin.
        for gem in ("ruby", "obsidian", "diamond"):
            self.assertIn(gem, RAW_MATERIALS)


class UncappedLevelTests(unittest.TestCase):
    def test_rates_are_linear_and_match_the_old_tables(self):
        self.assertEqual([furnace_rate(l) for l in (1, 2, 3)], [5, 10, 15])
        self.assertEqual([factory_rate(l) for l in (1, 2, 3)], [1, 2, 3])
        self.assertEqual([press_rate_per_day(l) for l in (1, 2, 3)], [1, 2, 3])

    def test_rates_keep_going_past_the_old_cap(self):
        self.assertEqual(furnace_rate(10), 50)
        self.assertEqual(factory_rate(10), 10)
        self.assertEqual(press_rate_per_day(10), 10)

    def test_thresholds_match_the_established_first_two(self):
        self.assertEqual(upgrade_threshold(2), 5.00)
        self.assertEqual(upgrade_threshold(3), 25.00)

    def test_each_level_costs_the_step_times_the_last(self):
        for level in range(2, 12):
            self.assertAlmostEqual(
                upgrade_threshold(level + 1),
                upgrade_threshold(level) * UPGRADE_THRESHOLD_STEP,
                places=4,
            )

    def test_the_ladder_is_reachable_by_a_real_server(self):
        # The point of the 1.2 change. Under the old x10 step the cumulative fee
        # to reach level 6 was 55,555 of a currency that the market mints about
        # one unit of per hundred iron ore sold - no server was ever going to
        # get there. Pinned as cumulative rather than per-level because the
        # per-level figure understates what a step change does to a ladder that
        # compounds.
        cumulative = [
            sum(upgrade_threshold(l) for l in range(2, level + 1))
            for level in range(2, 9)
        ]
        self.assertEqual(cumulative, [5, 30, 155, 780, 3905, 19530, 97655])

    def test_there_is_always_a_next_threshold(self):
        # Nothing returns None any more, so no caller needs a max-level branch.
        for level in range(2, 40):
            self.assertGreater(upgrade_threshold(level), 0)

    def test_level_two_costs_the_documented_base(self):
        self.assertEqual(upgrade_threshold(2), UPGRADE_THRESHOLD_BASE)


class PressTimingTests(unittest.TestCase):
    def days_for(self, product, level, quantity=1):
        return PRESS_RECIPES[product]["press_days"] * quantity / press_rate_per_day(level)

    def test_level_one_durations_are_one_three_nine_and_twentyseven_days(self):
        self.assertEqual(
            [self.days_for(p, 1) for p in ("ruby", "obsidian", "diamond", "ultra_dense_matter")],
            [1, 3, 9, 27],
        )

    def test_levels_divide_the_time(self):
        self.assertEqual(self.days_for("diamond", 3), 3)
        self.assertEqual(self.days_for("obsidian", 3), 1)
        self.assertEqual(self.days_for("ultra_dense_matter", 27), 1)

    def test_quantity_scales_the_time(self):
        self.assertEqual(self.days_for("ruby", 1, quantity=5), 5)


if __name__ == "__main__":
    unittest.main()
