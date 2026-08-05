"""
Tests for the live/beta custom emoji resolver in data/emoji.py.

"Dragonhoard" (live) and "Dragonhoard Beta" are separate Discord
applications with their own separately-uploaded copy of every icon
(docs/testing.md), so custom_emoji() has to pick the id that matches
whichever one this process logged in as. Pure string logic - no database, no
discord.py - so the two applications are simulated by patching
config.IS_BETA directly rather than actually running two processes.
"""
import unittest
from unittest.mock import patch

from data.emoji import MISSING_EMOJI, custom_emoji
from data.materials import ALL_MATERIALS


class CustomEmojiTests(unittest.TestCase):
    def test_live_resolves_to_the_live_id(self):
        with patch("config.IS_BETA", False):
            self.assertEqual(custom_emoji("IronOre", 111, 222), "<:IronOre:111>")

    def test_beta_resolves_to_the_beta_id(self):
        with patch("config.IS_BETA", True):
            self.assertEqual(custom_emoji("IronOre", 111, 222), "<:IronOre:222>")

    def test_beta_id_is_irrelevant_when_not_beta(self):
        with patch("config.IS_BETA", False):
            self.assertEqual(custom_emoji("IronOre", 111, None), "<:IronOre:111>")

    def test_missing_beta_id_falls_back_to_the_placeholder(self):
        with patch("config.IS_BETA", True):
            self.assertEqual(custom_emoji("IronOre", 111, None), MISSING_EMOJI)


class MaterialEmojiResolutionTests(unittest.TestCase):
    """data/materials.py resolves every material's "emoji" field once, at
    import time, through custom_emoji() - these check that resolution
    actually happened rather than a raw string or an unresolved call
    hanging around."""

    def test_every_material_emoji_is_a_plain_string(self):
        for material_id, info in ALL_MATERIALS.items():
            self.assertIsInstance(info["emoji"], str, material_id)


if __name__ == "__main__":
    unittest.main()
