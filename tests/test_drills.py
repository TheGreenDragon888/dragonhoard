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

# Matches HARVEST_TICK_MINUTES = 5 in cogs/mining.py.
TICKS_PER_HOUR = 12


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
        self.assertEqual(effective_rate("obsidian_drill", 2), 144)
        self.assertEqual(effective_rate("diamond_drill", 4), 768)


class EffectiveCapacityTests(unittest.TestCase):
    def test_bare_drill_holds_the_flat_base(self):
        self.assertEqual(effective_capacity(None), BASE_STORAGE_CAPACITY)

    def test_container_bonus_is_additive(self):
        expected = {
            "iron_container": 250,
            "steel_container": 500,
            "ruby_container": 2000,
            "obsidian_container": 8000,
            "diamond_container": 32000,
        }
        self.assertEqual(
            {c: effective_capacity(c) for c in STORAGE_CONTAINERS}, expected
        )

    def test_each_step_scales_by_the_matched_drills_speed_factor(self):
        # As of 1.3 every step from Steel up is a uniform x4 (matching each
        # step's drill speed jump: 7.5->30->120->480) - only Iron->Steel is
        # still x2. See the rationale comment above STORAGE_CONTAINERS in
        # data/materials.py for why, and for the 1.2.1 history where
        # Ruby->Obsidian and Obsidian->Diamond were only x2.
        totals = [effective_capacity(c) for c in STORAGE_CONTAINERS]
        ratios = [higher / lower for lower, higher in zip(totals, totals[1:])]
        self.assertEqual(ratios, [2, 4, 4, 4])

    def test_a_dearer_container_buys_at_least_as_much_runtime_as_a_cheaper_one(self):
        # The failure this guards is subtle and shipped once: the old ladder's
        # totals were in the same ratio as the matched drills' rates, so steel
        # through diamond all held exactly 40 hours and a ruby bought less
        # autonomy than iron did. Since 1.2.1, container capacity is
        # deliberately scaled by the same factor as the matched drill's speed
        # (see data/materials.py's STORAGE_CONTAINERS comment), which
        # reintroduces a flat tie from Steel through Diamond - accepted as the
        # cost of that design, not a bug. 1.3 changed which factors produce
        # that tie (Ruby->Obsidian->Diamond moved from x2/x2 to x4/x4) but not
        # the tie itself. Iron is the one tier that still buys strictly less.
        matched = (
            ("iron_container", "iron_drill"),
            ("steel_container", "steel_drill"),
            ("ruby_container", "ruby_drill"),
            ("obsidian_container", "obsidian_drill"),
            ("diamond_container", "diamond_drill"),
        )
        hours = [
            effective_capacity(container) / effective_rate(drill, 1)
            for container, drill in matched
        ]
        self.assertLess(hours[0], hours[1])
        self.assertTrue(all(h == hours[1] for h in hours[1:]))


class UpgradeCostTests(unittest.TestCase):
    def test_first_upgrade_costs_one_pack_plus_tier_material(self):
        self.assertEqual(upgrade_cost("iron_drill", 1), {"drill_upgrade_pack": 1, "iron": 10})
        self.assertEqual(upgrade_cost("steel_drill", 1), {"drill_upgrade_pack": 1, "steel": 10})

    def test_gem_drills_cost_one_of_their_gem(self):
        for drill_type, gem in (
            ("ruby_drill", "ruby"),
            ("obsidian_drill", "obsidian"),
            ("diamond_drill", "diamond"),
        ):
            self.assertEqual(upgrade_cost(drill_type, 1), {"drill_upgrade_pack": 1, gem: 1})

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


class BuyingTheNewTierIsCheaperThanUpgradingTests(unittest.TestCase):
    """The 1.2.1 drill speed buff was chosen so that buying a fresh drill of
    the new tier is cheaper than levelling the previous tier's drill up to
    match its speed - otherwise the buff would make the new tier a worse deal
    than just grinding the old one. "Cheaper" is measured with raw_input_cost,
    the codebase's existing "how hard to obtain" metric (also used by
    /inventory's ordering and scrap_yield's keystone selection), not an
    invented one.

    This was a close call for Ruby specifically: at the originally proposed
    15/30/60, buying a new Ruby Drill cost about 123x MORE than levelling a
    Steel Drill to match, because raw_input_cost prices a ruby at 5,500 and a
    Ruby Drill bit alone needs three. 30/60/120 was chosen so this reverses."""

    def _upgrade_path_value(self, drill_type: str, target_rate: float) -> float:
        level = 1
        while effective_rate(drill_type, level) < target_rate:
            level += 1
        total: dict[str, int] = {}
        for lvl in range(1, level):
            for material_id, qty in upgrade_cost(drill_type, lvl).items():
                total[material_id] = total.get(material_id, 0) + qty
        return sum(qty * raw_input_cost(material_id) for material_id, qty in total.items())

    def _new_drill_value(self, drill_type: str) -> float:
        return sum(
            qty * raw_input_cost(material_id)
            for material_id, qty in DRILLS[drill_type]["inputs"].items()
        )

    def test_each_gem_tier_drill_is_cheaper_than_levelling_the_previous_tier_to_match(self):
        precedents = {
            "ruby_drill": "steel_drill",
            "obsidian_drill": "ruby_drill",
            "diamond_drill": "obsidian_drill",
        }
        for new_tier, precedent in precedents.items():
            with self.subTest(new_tier=new_tier):
                target_rate = effective_rate(new_tier, 1)
                upgrade_value = self._upgrade_path_value(precedent, target_rate)
                new_drill_value = self._new_drill_value(new_tier)
                self.assertLess(new_drill_value, upgrade_value)


class AdvanceHarvestTests(unittest.TestCase):
    def _mine(self, rate_per_hour, ticks):
        total, carry = 0, 0.0
        for _ in range(ticks):
            amount, carry = advance_harvest(carry, rate_per_hour, TICKS_PER_HOUR)
            total += amount
        return total

    def test_level_two_iron_drill_earns_its_full_bonus(self):
        # The regression this whole mechanism exists for. A level 2 iron drill
        # mines 6/hour = 0.5 items/tick; rounding each tick in isolation gives
        # 0/tick forever, i.e. nothing over 4 hours (48 ticks) - not even what
        # level 1 produces, making the upgrade worthless.
        self.assertEqual(self._mine(6, 48), 24)

    def test_base_rates_are_unchanged(self):
        self.assertEqual(self._mine(5, 48), 20)     # iron, level 1
        self.assertEqual(self._mine(7.5, 48), 30)   # steel, level 1
        self.assertEqual(self._mine(120, 48), 480)  # diamond, level 1

    def test_odd_levels_land_exactly_too(self):
        self.assertEqual(self._mine(7, 48), 28)     # iron level 3
        self.assertEqual(self._mine(9, 48), 36)     # iron level 5

    def test_half_item_rates_land_exactly(self):
        # Percentage levelling puts drills on fractional rates whenever a
        # type's base isn't a multiple of LEVEL_RATE_ANCHOR (5) - steel (7.5)
        # is on a half at every odd level. As of 1.2.1 every gem-tier base
        # (30/60/120) is a multiple of 5, so steel is now the only drill type
        # this can happen to; see test_every_drill_and_level_averages_to_its_stated_rate
        # below for the generic (fractional-or-not) correctness check.
        self.assertEqual(self._mine(10.5, 48), 42)   # steel level 3

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
        # 2/hour is 0.167 items/tick at 12 ticks/hour - it must never floor to
        # zero forever.
        self.assertEqual(self._mine(2, 48), 8)


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
