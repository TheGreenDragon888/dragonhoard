"""
Tests for the designated bot channel predicate and its exemption list.

is_allowed_channel is deliberately a pure function so every case it has to get
right is checkable without a gateway connection. The parts that aren't pure -
reading bot_channel_id, refusing autocompletes silently, failing open on a
deleted channel - live in DragonhoardTree.interaction_check and are covered by
the end-to-end pass in docs/testing.md.
"""
import unittest

from cogs.setup import SetupCog
from utils.channel_guard import EXEMPT_ROOT_COMMANDS, is_allowed_channel

BOT_CHANNEL = 111
OTHER_CHANNEL = 222
THREAD = 333


class AllowedChannelTests(unittest.TestCase):
    def test_no_channel_configured_allows_everywhere(self):
        # The default every server starts on, and what every server was doing
        # before this setting existed.
        self.assertTrue(is_allowed_channel(None, OTHER_CHANNEL, None))
        self.assertTrue(is_allowed_channel(None, None, None))

    def test_the_bot_channel_itself_is_allowed(self):
        self.assertTrue(is_allowed_channel(BOT_CHANNEL, BOT_CHANNEL, None))

    def test_a_thread_inside_the_bot_channel_is_allowed(self):
        # Threads are how a conversation happens without burying the channel,
        # so a thread started in the bot channel must not be a dead zone.
        self.assertTrue(is_allowed_channel(BOT_CHANNEL, THREAD, BOT_CHANNEL))

    def test_another_channel_is_refused(self):
        self.assertFalse(is_allowed_channel(BOT_CHANNEL, OTHER_CHANNEL, None))

    def test_a_thread_in_another_channel_is_refused(self):
        self.assertFalse(is_allowed_channel(BOT_CHANNEL, THREAD, OTHER_CHANNEL))

    def test_an_unknown_channel_is_refused(self):
        # channel_id can be None on a malformed payload; that must not read as
        # "allowed" just because it isn't the bot channel's id.
        self.assertFalse(is_allowed_channel(BOT_CHANNEL, None, None))


class ExemptionTests(unittest.TestCase):
    def test_setup_is_exempt(self):
        """Without this a manager who sets a bot channel and then deletes it,
        or picks one nobody can post in, has no way back - the only command
        that could fix it would be locked behind the thing it fixes."""
        self.assertIn("setup", EXEMPT_ROOT_COMMANDS)

    def test_every_name_the_manual_answers_to_is_exempt(self):
        for name in ("help", "manual", "man"):
            self.assertIn(name, EXEMPT_ROOT_COMMANDS)

    def test_nothing_else_is_exempt(self):
        self.assertEqual(EXEMPT_ROOT_COMMANDS, {"setup", "help", "manual", "man"})

    def test_the_exempt_names_are_real_top_level_command_names(self):
        """interaction_check matches against interaction.data["name"], which is
        the TOP-LEVEL name even for a subcommand - so /setup fee is matched by
        "setup". A typo here would silently exempt nothing."""
        registered = {command.name for command in SetupCog.__cog_app_commands__}
        self.assertIn("setup", registered)


if __name__ == "__main__":
    unittest.main()
