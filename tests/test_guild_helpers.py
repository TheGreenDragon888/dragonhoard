"""
tests/test_guild_helpers.py

human_member_count counts a guild's humans by asking the gateway for its
members and keeping only the number (utils/guild_helpers.py). The two things
worth pinning: bots are excluded, and the answer is remembered for
MEMBER_COUNT_CACHE_SECONDS rather than requested again on every command -
the request is a burst of gateway traffic, and it replaced a member cache
that held every member of every server in memory for the sake of this one
figure. And that a member request that never comes back gives up rather than
hanging every caller after it (LostMemberRequestTests).
"""
import asyncio
import inspect
import unittest
from unittest.mock import patch

import discord.state

from utils import guild_helpers
from utils.guild_helpers import human_member_count


class _Member:
    def __init__(self, bot):
        self.bot = bot


class _Guild:
    """Just enough of discord.Guild: an id, and a chunk request that returns
    the members and counts how often it was asked."""

    def __init__(self, guild_id, members):
        self.id = guild_id
        self._members = members
        self.chunk_calls = 0

    async def chunk(self, *, cache=True):
        # cache=False is the whole point: the members are counted, not kept.
        assert cache is False
        self.chunk_calls += 1
        return list(self._members)


class HumanMemberCountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guild_helpers._member_counts.clear()

    async def test_bots_are_not_counted(self):
        guild = _Guild(1, [_Member(False), _Member(False), _Member(True)])
        self.assertEqual(await human_member_count(guild), 2)

    async def test_the_count_is_remembered_rather_than_requested_again(self):
        guild = _Guild(2, [_Member(False)])
        await human_member_count(guild)
        await human_member_count(guild)
        self.assertEqual(guild.chunk_calls, 1)

    async def test_a_stale_count_is_requested_again(self):
        guild = _Guild(3, [_Member(False)])
        await human_member_count(guild)
        count, _ = guild_helpers._member_counts[guild.id]
        guild_helpers._member_counts[guild.id] = (count, 0.0)   # already expired
        guild._members.append(_Member(False))
        self.assertEqual(await human_member_count(guild), 2)
        self.assertEqual(guild.chunk_calls, 2)

    async def test_servers_are_cached_separately(self):
        big = _Guild(4, [_Member(False)] * 5)
        small = _Guild(5, [_Member(False)])
        self.assertEqual(await human_member_count(big), 5)
        self.assertEqual(await human_member_count(small), 1)


class _State:
    """The one piece of discord.py's ConnectionState the timeout touches: its
    table of in-flight member requests, keyed by guild id."""

    def __init__(self):
        self._chunk_requests = {}


class _HungGuild(_Guild):
    """A guild whose member request was lost: chunk() never returns, the way
    discord.py's does when the reply to an outstanding request never arrives
    (2026-11-04 - see human_member_count's docstring). member_count is the
    bot-inclusive figure discord.Guild carries for free."""

    def __init__(self, guild_id, members, member_count):
        super().__init__(guild_id, members)
        self.member_count = member_count
        self._state = _State()
        self._state._chunk_requests[guild_id] = object()
        self.hung = True

    async def chunk(self, *, cache=True):
        if not self.hung:
            return await super().chunk(cache=cache)
        self.chunk_calls += 1
        await asyncio.Event().wait()


class LostMemberRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guild_helpers._member_counts.clear()
        patcher = patch.object(guild_helpers, "CHUNK_TIMEOUT_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_request_that_never_returns_does_not_hang_the_caller(self):
        guild = _HungGuild(10, [], member_count=7)
        with self.assertLogs("dragonhoard", "WARNING"):
            self.assertEqual(await human_member_count(guild), 7)

    async def test_the_last_known_count_beats_the_bot_inclusive_one(self):
        guild = _HungGuild(11, [_Member(False)] * 3, member_count=9)
        guild.hung = False
        self.assertEqual(await human_member_count(guild), 3)
        guild_helpers._member_counts[guild.id] = (3, 0.0)   # expired
        guild.hung = True
        with self.assertLogs("dragonhoard", "WARNING"):
            self.assertEqual(await human_member_count(guild), 3)

    async def test_the_lost_request_is_dropped_so_the_next_call_asks_again(self):
        guild = _HungGuild(12, [_Member(False)] * 2, member_count=5)
        with self.assertLogs("dragonhoard", "WARNING"):
            await human_member_count(guild)
        self.assertNotIn(guild.id, guild._state._chunk_requests)

        # The gateway answers again once a fresh request is sent.
        guild.hung = False
        guild_helpers._member_counts[guild.id] = (5, 0.0)   # expired
        self.assertEqual(await human_member_count(guild), 2)

    async def test_the_fallback_is_remembered_rather_than_timed_out_every_command(self):
        guild = _HungGuild(13, [], member_count=4)
        with self.assertLogs("dragonhoard", "WARNING"):
            await human_member_count(guild)
        await human_member_count(guild)
        self.assertEqual(guild.chunk_calls, 1)

    def test_discord_py_still_keeps_member_requests_where_the_timeout_clears_them(self):
        # The timeout reaches into a private attribute. If an upgrade renames it
        # or re-keys it, the pop silently stops doing anything and a lost
        # request would again outlive the timeout, so pin the shape it relies on.
        source = inspect.getsource(discord.state.ConnectionState.chunk_guild)
        self.assertIn("self._chunk_requests.get(guild.id)", source)
        self.assertIn("self._chunk_requests[guild.id] = ", source)


if __name__ == "__main__":
    unittest.main()
