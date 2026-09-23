"""
utils/guild_helpers.py

Small discord.Guild helpers shared across cogs.
"""
import time

import discord

# How long a guild's human member count is trusted before it is counted again.
# What it feeds - the market's target stock and the job board's choice of
# material (data/materials.py: target_stock) - moves with a server's size over
# weeks, so a count a few minutes stale changes nothing a player can see, and
# counting is the expensive part: see human_member_count.
MEMBER_COUNT_CACHE_SECONDS = 600

# guild_id -> (count, monotonic time it expires). Process-local on purpose;
# a restart simply counts once more.
_member_counts: dict[int, tuple[int, float]] = {}


async def human_member_count(guild: discord.Guild) -> int:
    """Member count used for server-size-scaled formulas (market target
    stock and the furnace auto-smelt thresholds built on it) - excludes
    bots so a server stuffed with other bots doesn't inflate its own
    economy's targets. The mining bag is deliberately NOT one of these; it
    is the same size on every server, whatever its membership.

    Requires the members intent (enabled in bot.py) so guild.chunk() is
    allowed at all. The members are requested over the gateway and COUNTED,
    NOT CACHED: bot.py turns the member cache off, because holding every
    member of every server in memory - which is what the intent does by
    default, chunking all of them at startup - was the largest thing the
    process kept in RAM, and this count is the only thing that ever read it.
    Requesting them costs a burst of gateway traffic per call, which is why
    the answer is remembered for MEMBER_COUNT_CACHE_SECONDS.

    Excluding bots is what stops this being guild.member_count, which arrives
    free with the guild and counts everybody."""
    now = time.monotonic()
    cached = _member_counts.get(guild.id)
    if cached is not None and cached[1] > now:
        return cached[0]

    members = await guild.chunk(cache=False)
    count = sum(1 for member in members if not member.bot)
    _member_counts[guild.id] = (count, now + MEMBER_COUNT_CACHE_SECONDS)
    return count
