"""
Balance-data tests for drill levelling and storage containers.

Everything here is pure arithmetic over data/materials.py - no database and no
discord.py - so it runs without a bot or a fixture.
"""
import unittest

from data.materials import (
    ALL_MATERIALS,
    BASE_STORAGE_CAPACITY,
    DRILLS,
    DRILL_UPGRADE_JOB_TARGET,
    LEVEL_RATE_ANCHOR,
    STORAGE_CONTAINERS,
    UPGRADE_MATERIALS,
    advance_harvest,
    effective_capacity,
    effective_rate,
    get_material_info,
    raw_input_cost,
    upgrade_cost,
)
from utils.drills import drill_cell, drill_label

# Matches HARVEST_TICK_MINUTES = 24 in cogs/mining.py.
TICKS_PER_HOUR = 2.5


class EffectiveRateTests(unittest.TestCase):
    def test_level_one_is_the_types_base_rate(self):
        for drill_type, info in DRILLS.items():
            self.assertEqual(effective_rate(drill_type, 1), info["mines_per_hour"])

    def test_each_level_adds_a_fifth_of_the_types_base_rate(self):
        for drill_type in DRILLS:
            base = effective_rate(drill_type, 1)
            for level in range(1, 12):
                self.assertEqual(
                    effective_rate(drill_type, level),
                    base * (LEVEL_RATE_ANCHOR + level - 1) / LEVEL_RATE_ANCHOR,
                )

    def test_the_iron_ladder_is_unchanged(self):
        # The anchor the whole scheme is derived from: the Iron Drill's
        # 5 -> 6 -> 7 -> 8 predates percentage levelling and has to survive it,
        # because "a level is a fifth of base" is just that ladder generalised.
        self.assertEqual(
            [effective_rate("iron_drill", level) for level in range(1, 5)],
            [5, 6, 7, 8],
        )

    def test_a_level_is_worth_the_same_proportion_at_every_tier(self):
        # The point of the change. Under the old flat +1 a level made an Iron
        # Drill 20% faster and a Diamond Drill 6.7% faster, so the upgrade path
        # got worse the better your drill was.
        for level in range(1, 8):
            gains = {
                effective_rate(t, level + 1) / effective_rate(t, level) for t in DRILLS
            }
            self.assertEqual(len(gains), 1, f"tiers diverge at level {level}: {gains}")

    def test_rates_stay_exact(self):
        # Written as one rational expression precisely so these land on the nose
        # - a 9.000000000000002 items/hour reaches an embed as "9.000000000000002".
        self.assertEqual(effective_rate("steel_drill", 2), 9)
        self.assertEqual(effective_rate("steel_drill", 3), 10.5)
        self.assertEqual(effective_rate("obsidian_drill", 2), 15)
        self.assertEqual(effective_rate("diamond_drill", 4), 24)


class EffectiveCapacityTests(unittest.TestCase):
    def test_bare_drill_holds_the_flat_base(self):
        self.assertEqual(effective_capacity(None), BASE_STORAGE_CAPACITY)

    def test_container_bonus_is_additive(self):
        expected = {
            "iron_container": 250,
            "steel_container": 300,
            "ruby_container": 400,
            "obsidian_container": 500,
            "diamond_container": 600,
        }
        self.assertEqual(
            {c: effective_capacity(c) for c in STORAGE_CONTAINERS}, expected
        )


class UpgradeCostTests(unittest.TestCase):
    def test_first_upgrade_costs_one_pack_plus_tier_material(self):
        self.assertEqual(upgrade_cost("iron_drill", 1), {"drill_upgrade_pack": 1, "iron": 10})
        self.assertEqual(upgrade_cost("steel_drill", 1), {"drill_upgrade_pack": 1, "steel": 10})

    def test_gem_drills_cost_three_of_their_gem(self):
        for drill_type, gem in (
            ("ruby_drill", "ruby"),
            ("obsidian_drill", "obsidian"),
            ("diamond_drill", "diamond"),
        ):
            self.assertEqual(upgrade_cost(drill_type, 1), {"drill_upgrade_pack": 1, gem: 3})

    def test_every_part_of_the_cost_doubles_per_level(self):
        for level in range(1, 8):
            multiplier = 2 ** (level - 1)
            self.assertEqual(
                upgrade_cost("iron_drill", level),
                {"drill_upgrade_pack": multiplier, "iron": 10 * multiplier},
            )

    def test_every_drill_type_has_an_upgrade_path(self):
        for drill_type in DRILLS:
            cost = upgrade_cost(drill_type, 1)
            self.assertIn("drill_upgrade_pack", cost)
            for material_id in cost:
                self.assertIsNotNone(get_material_info(material_id))


class AdvanceHarvestTests(unittest.TestCase):
    def _mine(self, rate_per_hour, ticks):
        total, carry = 0, 0.0
        for _ in range(ticks):
            amount, carry = advance_harvest(carry, rate_per_hour, TICKS_PER_HOUR)
            total += amount
        return total

    def test_level_two_iron_drill_earns_its_full_bonus(self):
        # The regression this whole mechanism exists for. A level 2 iron drill
        # mines 6/hour = 2.4 items/tick; rounding each tick in isolation gives
        # 2/tick, i.e. 20 items over 4 hours - exactly what level 1 produces,
        # making the upgrade worthless.
        self.assertEqual(self._mine(6, 10), 24)

    def test_base_rates_are_unchanged(self):
        self.assertEqual(self._mine(5, 10), 20)     # iron, level 1
        self.assertEqual(self._mine(7.5, 10), 30)   # steel, level 1
        self.assertEqual(self._mine(15, 10), 60)    # diamond, level 1

    def test_odd_levels_land_exactly_too(self):
        self.assertEqual(self._mine(7, 10), 28)     # iron level 3
        self.assertEqual(self._mine(9, 10), 36)     # iron level 5

    def test_half_item_rates_land_exactly(self):
        # Percentage levelling puts far more drills on fractional rates than
        # the old flat +1 did - a steel drill is on a half at every odd level.
        self.assertEqual(self._mine(10.5, 10), 42)   # steel level 3
        self.assertEqual(self._mine(17.5, 10), 70)   # obsidian level 3

    def test_every_drill_and_level_averages_to_its_stated_rate(self):
        for drill_type in DRILLS:
            # Past level 5, where the fractional rates the carry exists for are
            # thickest - a steel drill alternates whole and half from level 2 on.
            for level in range(1, 12):
                rate = effective_rate(drill_type, level)
                # 100 hours, so any per-tick drift would be obvious.
                self.assertEqual(self._mine(rate, int(100 * TICKS_PER_HOUR)), rate * 100)

    def test_carry_stays_a_fraction(self):
        carry = 0.0
        for _ in range(50):
            _, carry = advance_harvest(carry, 6, TICKS_PER_HOUR)
            self.assertGreaterEqual(carry, 0.0)
            self.assertLess(carry, 1.0)

    def test_a_rate_below_one_item_per_tick_still_accumulates(self):
        # 2/hour is 0.8 items/tick - it must never floor to zero forever.
        self.assertEqual(self._mine(2, 10), 8)


class DrillDisplayTests(unittest.TestCase):
    """The compact form /inventory and /mine status render drills as."""

    @staticmethod
    def row(**columns):
        base = {
            "drill_id": 7, "guild_id": None, "owner_id": 1, "drill_type": "iron_drill",
            "level": 1, "container_type": None, "stored_amount": 0,
            "locked_job_id": None,
        }
        base.update(columns)
        return base

    def test_a_bare_drill_is_its_emoji_and_level(self):
        self.assertEqual(
            drill_cell(self.row()), f"{DRILLS['iron_drill']['emoji']} Lv.1"
        )

    def test_a_container_adds_its_own_glyph(self):
        cell = drill_cell(self.row(container_type="steel_container", level=3))
        self.assertEqual(
            cell,
            f"{DRILLS['iron_drill']['emoji']}{STORAGE_CONTAINERS['steel_container']['emoji']} Lv.3",
        )

    def test_no_container_leaves_no_gap_or_placeholder(self):
        # The cells sit side by side in a grid, so a stand-in glyph for "no
        # container" would read as a container the player doesn't have.
        self.assertNotIn(" ", drill_cell(self.row()).split(" Lv.")[0])

    def test_no_drill_id_is_shown(self):
        for row in (self.row(drill_id=1234), self.row(drill_id=1234, container_type="iron_container")):
            self.assertNotIn("1234", drill_cell(row))
            self.assertNotIn("#", drill_cell(row))

    def test_the_label_carries_no_id_either(self):
        label = drill_label(self.row(drill_id=1234, level=2))
        self.assertNotIn("1234", label)
        self.assertNotIn("#", label)
        self.assertIn("Iron Drill", label)
        self.assertIn("Lv.2", label)


class MaterialRegistryTests(unittest.TestCase):
    def test_every_tier_is_registered(self):
        for table in (DRILLS, STORAGE_CONTAINERS, UPGRADE_MATERIALS):
            for material_id in table:
                self.assertIsNotNone(
                    get_material_info(material_id), f"{material_id} is not in ALL_MATERIALS"
                )

    def test_the_upgrade_job_sentinel_is_not_a_material(self):
        # It's a job kind, not an item; registering it would leak it into the
        # factory's craftable list and the recipe book.
        self.assertIsNone(get_material_info(DRILL_UPGRADE_JOB_TARGET))

    def test_every_recipe_input_resolves(self):
        for material_id, info in ALL_MATERIALS.items():
            for input_id in info.get("inputs", {}):
                self.assertIsNotNone(
                    get_material_info(input_id),
                    f"{material_id} needs unknown input {input_id}",
                )

    def test_every_material_has_a_name_and_emoji(self):
        for material_id, info in ALL_MATERIALS.items():
            self.assertTrue(info.get("name"), material_id)
            self.assertTrue(info.get("emoji"), material_id)

    def test_raw_input_cost_terminates_and_is_positive(self):
        for material_id in ALL_MATERIALS:
            self.assertGreater(raw_input_cost(material_id), 0.0, material_id)

    def test_containers_are_ordered_by_cost(self):
        costs = [raw_input_cost(c) for c in STORAGE_CONTAINERS]
        self.assertEqual(costs, sorted(costs))

    def test_drills_no_longer_carry_a_per_type_capacity(self):
        # Capacity is a flat base plus a container; a leftover per-type field
        # would be read by nothing and drift out of sync.
        for drill_type, info in DRILLS.items():
            self.assertNotIn("storage_capacity", info, drill_type)


if __name__ == "__main__":
    unittest.main()
