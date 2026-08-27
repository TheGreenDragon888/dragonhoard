"""
Tests for the /recipe book's display formatting.

Pure functions over data/materials.py - no database, no discord.py.
"""
import unittest

from cogs.recipe import _container_lines
from data.materials import STORAGE_CONTAINERS


class ContainerLinesTests(unittest.TestCase):
    def test_a_four_figure_capacity_is_comma_separated(self):
        lines = dict(zip(STORAGE_CONTAINERS, _container_lines()))
        self.assertIn("[holds 32,000]", lines["diamond_container"])

    def test_a_three_figure_capacity_has_no_stray_comma(self):
        lines = dict(zip(STORAGE_CONTAINERS, _container_lines()))
        self.assertIn("[holds 250]", lines["iron_container"])


if __name__ == "__main__":
    unittest.main()
