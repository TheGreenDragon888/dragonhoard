"""
Tests for bot.py's extension list, which is what decides whether the
beta-only devtools cog (cogs/devtools.py) is ever registered as a slash
command at all - see that module's docstring for the full defense-in-depth
picture, of which this is layer 1.
"""
import unittest

from bot import build_initial_extensions


class BuildInitialExtensionsTests(unittest.TestCase):
    def test_devtools_is_included_on_beta(self):
        self.assertIn("cogs.devtools", build_initial_extensions(True))

    def test_devtools_is_excluded_off_beta(self):
        self.assertNotIn("cogs.devtools", build_initial_extensions(False))

    def test_every_player_facing_cog_is_included_either_way(self):
        # The beta-only conditional must only ever ADD to the list, never
        # remove anything a live server depends on.
        live = set(build_initial_extensions(False))
        beta = set(build_initial_extensions(True))
        self.assertTrue(live.issubset(beta))
        self.assertEqual(beta - live, {"cogs.devtools"})


if __name__ == "__main__":
    unittest.main()
