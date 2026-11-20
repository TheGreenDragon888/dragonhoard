"""
Tests for the /recipe book's display formatting.

Pure functions over data/materials.py - no database, no discord.py.
"""
import unittest

from cogs.recipe import _container_lines, _drill_upgrade_lines, _press_lines
from data.materials import (
    DRILLS,
    PRESS_RECIPES,
    STORAGE_CONTAINERS,
    effective_rate,
    get_material_info,
)
from utils.formatting import format_rate


class PressLinesTests(unittest.TestCase):
    """/recipe press names each recipe by what it makes, so the two that make
    obsidian (and the two that make diamonds) read as two ways to the same gem."""

    def lines(self):
        return dict(zip(PRESS_RECIPES, _press_lines()))

    def test_a_fusion_reads_as_the_gem_it_makes(self):
        obsidian = get_material_info("obsidian")
        ruby = get_material_info("ruby")
        line = self.lines()["obsidian_from_ruby"]
        self.assertTrue(line.startswith(f"{obsidian['emoji']} Obsidian - {ruby['emoji']} 10"), line)
        self.assertTrue(line.endswith("[3 press-days]"), line)

    def test_large_inputs_are_comma_separated(self):
        self.assertIn("27,000", self.lines()["diamond"])

    def test_a_one_day_recipe_is_singular(self):
        self.assertTrue(self.lines()["ruby"].endswith("[1 press-day]"))

    def test_nothing_renders_as_unknown(self):
        self.assertNotIn("❓", "".join(self.lines().values()))


class DrillUpgradeLinesTests(unittest.TestCase):
    """1.5: each Drill Upgrades line says what a level adds to that drill's
    mining, so the cost sits next to what it buys."""

    def lines(self):
        return dict(zip(DRILLS, _drill_upgrade_lines()))

    def test_every_line_ends_with_the_gain_a_level_adds(self):
        for drill_id, line in self.lines().items():
            with self.subTest(drill=drill_id):
                gain = effective_rate(drill_id, 2) - effective_rate(drill_id, 1)
                self.assertTrue(
                    line.endswith(f" — [+{format_rate(gain)}/hour per level]"), line
                )

    def test_the_gain_is_the_same_at_every_level(self):
        # What makes "per level" true rather than true of the first one only.
        for drill_id in DRILLS:
            first = effective_rate(drill_id, 2) - effective_rate(drill_id, 1)
            for level in range(2, 30):
                with self.subTest(drill=drill_id, level=level):
                    self.assertEqual(
                        effective_rate(drill_id, level + 1) - effective_rate(drill_id, level), first
                    )

    def test_the_figures_as_of_1_4_3(self):
        # Iron and Steel levels add a fifth of the base rate, gem drills the
        # whole of it (data/materials.py: LEVEL_RATE_ANCHOR).
        lines = self.lines()
        self.assertIn("[+1/hour per level]", lines["iron_drill"])
        self.assertIn("[+1.5/hour per level]", lines["steel_drill"])
        self.assertIn("[+30/hour per level]", lines["ruby_drill"])
        self.assertIn("[+120/hour per level]", lines["obsidian_drill"])
        self.assertIn("[+480/hour per level]", lines["diamond_drill"])


class ContainerLinesTests(unittest.TestCase):
    def test_a_six_figure_capacity_is_comma_separated(self):
        lines = dict(zip(STORAGE_CONTAINERS, _container_lines()))
        self.assertIn("[holds 128,000]", lines["diamond_container"])

    def test_the_smallest_container_reads_as_a_whole_thousand(self):
        # Every container holds four figures or more since 1.5 quadrupled them.
        lines = dict(zip(STORAGE_CONTAINERS, _container_lines()))
        self.assertIn("[holds 1,000]", lines["iron_container"])


if __name__ == "__main__":
    unittest.main()
