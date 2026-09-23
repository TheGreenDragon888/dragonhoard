"""
tests/test_guild_helpers.py

human_member_count counts a guild's humans by asking the gateway for its
members and keeping only the number (utils/guild_helpers.py). The two things
worth pinning: bots are excluded, and the answer is remembered for
MEMBER_COUNT_CACHE_SECONDS rather than requested again on every command -
the request is a burst of gateway traffic, and it replaced a member cache
that held every member of every server in memory for the sake of this one
figure.
"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
