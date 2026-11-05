"""
utils/guild_helpers.py

Small discord.Guild helpers shared across cogs.
"""
import asyncio
import logging
import time

import discord

log = logging.getLogger("dragonhoard")

# How long one member request may take before the count falls back (see
# human_member_count). /jobboard, /market sell and /economy status wait for it
# before they reply, and Discord drops an interaction that hasn't been answered
# within three seconds, so it has to leave them room inside that.
CHUNK_TIMEOUT_SECONDS = 2.0

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
    free with the guild and counts everybody.

    THE REQUEST IS GIVEN UP ON after CHUNK_TIMEOUT_SECONDS. discord.py (2.7.1)
    keeps at most one member request per guild, waits on it with no timeout,
    and clears unanswered ones only on a full reconnect, never on a resume. A
    reply lost when the gateway drops mid-request therefore leaves a request
    that never finishes, and every later chunk() for that guild waits on it
    instead of asking again. On 2026-11-04 one did: the gateway resumed in the
    same second the furnace's auto-smelt asked for a server's members, and
    from then until a restart that server's /jobboard, /market sell and
    /economy status never answered, and the furnace loop - stuck on the same
    wait - stopped for every server. So a request that times out is also
    removed from discord.py's table, letting the next call send a fresh one,
    and the count falls back to the last one known, or failing that to
    guild.member_count, bots and all."""
    now = time.monotonic()
    cached = _member_counts.get(guild.id)
    if cached is not None and cached[1] > now:
        return cached[0]

    try:
        members = await asyncio.wait_for(guild.chunk(cache=False), timeout=CHUNK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # A private attribute, reached only because nothing public clears it.
        # getattr, so a discord.py that renames it degrades to the timeout
        # alone rather than raising here.
        state = getattr(guild, "_state", None)
        getattr(state, "_chunk_requests", {}).pop(guild.id, None)
        count = cached[0] if cached is not None else (guild.member_count or 0)
        log.warning(
            "Member request for guild %s timed out after %ss; using %s members until it is counted again.",
            guild.id, CHUNK_TIMEOUT_SECONDS, count,
        )
        # Remembered like a real count, so a gateway that stays slow costs
        # each server one timeout per window rather than one per command.
        _member_counts[guild.id] = (count, now + MEMBER_COUNT_CACHE_SECONDS)
        return count

    count = sum(1 for member in members if not member.bot)
    _member_counts[guild.id] = (count, now + MEMBER_COUNT_CACHE_SECONDS)
    return count
