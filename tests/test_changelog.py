"""
Tests for the changelog's data and rendering.

Shaped after tests/test_manual.py, since data/changelog.py is deliberately the
same pattern: text in a data module, one embed per entry, a dropdown to browse
them. These pin the things that break silently - a release that renders past
Discord's embed limits, a summary too long for a select option, or a version
number that has drifted away from config.VERSION.
"""
import unittest

import discord

import config
import utils.embeds as embeds
from cogs.changelog import VERSION_CHOICES, _SELECTABLE, _version_options
from data.changelog import (
    VERSIONS,
    LATEST_VERSION,
    MAX_SELECTABLE_VERSIONS,
    build_version_embed,
)

# Discord's own hard limits on a single embed and select menu.
MAX_EMBED_LENGTH = 6000
MAX_EMBED_FIELDS = 25
MAX_SELECT_OPTIONS = 25
MAX_SELECT_DESCRIPTION = 100

PALETTE = {
    value for name, value in vars(embeds).items()
    if name.endswith("_COLOR") and isinstance(value, discord.Color)
}


class ChangelogDataTests(unittest.TestCase):
    def test_there_is_at_least_one_release(self):
        self.assertTrue(VERSIONS)

    def test_keys_match_their_versions(self):
        for key, version in VERSIONS.items():
            self.assertEqual(key, version.version)

    def test_the_newest_release_is_the_version_the_bot_reports(self):
        """VERSIONS is ordered newest first and /changelog opens on the first
        entry, so this drifting means the bot's footer says one thing and its
        own release notes say another."""
        self.assertEqual(LATEST_VERSION, config.VERSION)

    def test_the_newest_release_is_first(self):
        self.assertEqual(next(iter(VERSIONS)), LATEST_VERSION)

    def test_notes_start_at_1_1(self):
        # 1.0 shipped before there was anywhere to write release notes down;
        # reconstructing it after the fact would be inventing a record.
        self.assertNotIn("1.0", VERSIONS)

    def test_every_release_says_something(self):
        for version in VERSIONS.values():
            with self.subTest(version=version.version):
                self.assertTrue(version.entries)
                self.assertTrue(version.headline)

    def test_every_release_uses_a_palette_color(self):
        """Guards docs/stylization.md - a release must claim one of the bot's
        colors rather than inventing its own."""
        for version in VERSIONS.values():
            with self.subTest(version=version.version):
                self.assertIn(version.color, PALETTE)

    def test_released_dates_are_iso_formatted(self):
        for version in VERSIONS.values():
            with self.subTest(version=version.version):
                year, month, day = version.released.split("-")
                self.assertEqual((len(year), len(month), len(day)), (4, 2, 2))


class ChangelogRenderingTests(unittest.TestCase):
    def test_every_release_fits_in_one_embed(self):
        for version in VERSIONS.values():
            with self.subTest(version=version.version):
                embed = build_version_embed(version)
                self.assertLessEqual(len(embed), MAX_EMBED_LENGTH)
                self.assertLessEqual(len(embed.fields), MAX_EMBED_FIELDS)

    def test_every_release_carries_the_standard_footer(self):
        for version in VERSIONS.values():
            with self.subTest(version=version.version):
                self.assertEqual(build_version_embed(version).footer.text, embeds.FOOTER_TEXT)

    def test_every_release_names_itself_in_its_title(self):
        for version in VERSIONS.values():
            with self.subTest(version=version.version):
                self.assertIn(version.version, build_version_embed(version).title)


class ChangelogDropdownTests(unittest.TestCase):
    def test_summaries_fit_a_select_option(self):
        for version in VERSIONS.values():
            with self.subTest(version=version.version):
                self.assertLessEqual(len(version.summary), MAX_SELECT_DESCRIPTION)

    def test_the_dropdown_never_exceeds_discords_option_limit(self):
        """A select menu holds 25 options and drops the rest silently, so the
        cog slices - this is what makes that slice load-bearing rather than
        decorative once there are 26 releases."""
        self.assertLessEqual(MAX_SELECTABLE_VERSIONS, MAX_SELECT_OPTIONS)
        self.assertLessEqual(len(_SELECTABLE), MAX_SELECT_OPTIONS)
        self.assertLessEqual(len(VERSION_CHOICES), MAX_SELECT_OPTIONS)

    def test_the_release_being_read_is_marked_as_selected(self):
        options = _version_options(LATEST_VERSION)
        selected = [option for option in options if option.default]
        self.assertEqual([option.value for option in selected], [LATEST_VERSION])

    def test_every_choice_names_a_real_release(self):
        for choice in VERSION_CHOICES:
            self.assertIn(choice.value, VERSIONS)


if __name__ == "__main__":
    unittest.main()
