"""
Coverage tests for the in-Discord manual (data/manual.py, cogs/manual.py).

The manual's text is hand-written, which means it can drift out of date the
moment someone adds a command and forgets to document it. These tests walk the
real command tree and fail if that happens.

Cog classes expose their app commands as a class attribute, so the tree can be
walked without a bot, a token or a database - like the other suites here, this
runs on its own.
"""
import unittest

import discord

from cogs.setup import SetupCog
from cogs.economy import EconomyCog
from cogs.mining import MiningCog
from cogs.furnace import FurnaceCog
from cogs.factory import FactoryCog
from cogs.press import PressCog
from cogs.scrapper import ScrapperCog
from cogs.jobboard import JobBoardCog
from cogs.donate import DonateCog
from cogs.recipe import RecipeCog
from cogs.manual import ManualCog
from cogs.changelog import ChangelogCog
from cogs.fun import FunCog

from data.manual import SECTIONS, DEFAULT_SECTION, build_section_embed

import utils.embeds as embeds

COGS = (
    SetupCog,
    EconomyCog,
    MiningCog,
    FurnaceCog,
    FactoryCog,
    PressCog,
    ScrapperCog,
    JobBoardCog,
    DonateCog,
    RecipeCog,
    ManualCog,
    ChangelogCog,
    FunCog,
)

# Discord's own hard limits on a single embed.
MAX_EMBED_LENGTH = 6000
MAX_EMBED_FIELDS = 25
MAX_SELECT_DESCRIPTION = 100

PALETTE = {
    value for name, value in vars(embeds).items()
    if name.endswith("_COLOR") and isinstance(value, discord.Color)
}


def registered_commands() -> set[str]:
    """Every leaf slash command the bot registers, as its canonical invocation
    ("/mine place"). Groups are walked; top-level commands stand alone."""
    found = set()
    for cog_cls in COGS:
        for command in cog_cls.__cog_app_commands__:
            if isinstance(command, discord.app_commands.Group):
                for sub in command.walk_commands():
                    found.add(f"/{command.name} {sub.name}")
            else:
                found.add(f"/{command.name}")
    return found


def documented_commands() -> list[str]:
    """Every command name the manual claims to document, in order, including
    duplicates so they can be caught."""
    return [cmd.name for section in SECTIONS.values() for cmd in section.commands]


class ManualCoverageTests(unittest.TestCase):
    def test_every_loaded_cog_is_checked_here(self):
        """COGS above is hand-written, so a new cog is documented only if
        somebody remembers to add it here too - and a coverage test that
        silently skips the thing you just added is worse than none.

        Caught exactly that: /donate shipped its cog and its manual entry while
        COGS still listed twelve, so the suite went green without ever looking
        at it.
        """
        import bot

        loaded = {name.removeprefix("cogs.") for name in bot.INITIAL_EXTENSIONS}
        checked = {cog_cls.__module__.removeprefix("cogs.") for cog_cls in COGS}
        self.assertEqual(loaded, checked)

    def test_every_registered_command_is_documented(self):
        missing = registered_commands() - set(documented_commands())
        self.assertEqual(
            missing, set(),
            f"These commands have no entry in data/manual.py: {sorted(missing)}",
        )

    def test_manual_documents_no_commands_that_do_not_exist(self):
        orphans = set(documented_commands()) - registered_commands()
        self.assertEqual(
            orphans, set(),
            f"data/manual.py documents commands that aren't registered: {sorted(orphans)}",
        )

    def test_no_command_is_documented_twice(self):
        documented = documented_commands()
        duplicates = {name for name in documented if documented.count(name) > 1}
        self.assertEqual(duplicates, set(), f"Documented in more than one section: {sorted(duplicates)}")


class ManualSectionTests(unittest.TestCase):
    def test_default_section_exists(self):
        self.assertIn(DEFAULT_SECTION, SECTIONS)

    def test_keys_match_their_sections(self):
        for key, section in SECTIONS.items():
            self.assertEqual(key, section.key)

    def test_every_section_uses_a_palette_color(self):
        """Guards docs/stylization.md - a section must claim one of the
        bot's colors rather than inventing its own."""
        for section in SECTIONS.values():
            self.assertIn(
                section.color, PALETTE,
                f"Section '{section.key}' uses a color that isn't in utils/embeds.py",
            )

    def test_summaries_fit_a_select_option(self):
        for section in SECTIONS.values():
            self.assertLessEqual(
                len(section.summary), MAX_SELECT_DESCRIPTION,
                f"Section '{section.key}' summary is too long for the dropdown",
            )

    def test_section_count_fits_a_select_menu(self):
        # A select menu holds 25 options; so does a command's choice list.
        self.assertLessEqual(len(SECTIONS), 25)


class ManualEmbedTests(unittest.TestCase):
    def test_every_section_renders_within_discord_limits(self):
        for section in SECTIONS.values():
            with self.subTest(section=section.key):
                embed = build_section_embed(section)
                self.assertLessEqual(
                    len(embed), MAX_EMBED_LENGTH,
                    f"Section '{section.key}' renders to {len(embed)} characters",
                )
                self.assertLessEqual(len(embed.fields), MAX_EMBED_FIELDS)

    def test_every_section_carries_the_standard_footer(self):
        for section in SECTIONS.values():
            self.assertEqual(build_section_embed(section).footer.text, embeds.FOOTER_TEXT)


if __name__ == "__main__":
    unittest.main()
