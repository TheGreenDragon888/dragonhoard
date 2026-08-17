"""
utils/guild_helpers.py

Small discord.Guild helpers shared across cogs.
"""
import discord


async def human_member_count(guild: discord.Guild) -> int:
    """Member count used for server-size-scaled formulas (market target
    stock and the furnace auto-smelt thresholds built on it) - excludes
    bots so a server stuffed with other bots doesn't inflate its own
    economy's targets. The mining bag is deliberately NOT one of these; it
    is the same size on every server, whatever its membership.

    Requires the members intent (enabled in bot.py) so
    guild.members can actually be populated. Explicitly chunks the guild if
    it hasn't finished caching members yet, rather than trusting on_ready to
    have already done it - discord.py dispatches interactions as soon as
    the gateway session is up, which can be before a large guild's member
    chunk finishes, and reading guild.members early silently undercounts
    (an empty/partial cache reads as a near-empty server, tanking every
    formula built on this count)."""
    if not guild.chunked:
        await guild.chunk()
    return sum(1 for member in guild.members if not member.bot)
