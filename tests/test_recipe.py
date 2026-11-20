"""
Tests for the /recipe book's display formatting.

Pure functions over data/materials.py - no database, no discord.py.
"""
import unittest

from cogs.recipe import _container_lines
from data.materials import STORAGE_CONTAINERS


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
