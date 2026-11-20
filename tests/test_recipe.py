"""
Tests for the /recipe book's display formatting.

Pure functions over data/materials.py - no database, no discord.py.
"""
import unittest

from cogs.recipe import _container_lines, _drill_upgrade_lines
from data.materials import DRILLS, STORAGE_CONTAINERS, effective_rate
from utils.formatting import format_rate


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
