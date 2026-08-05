"""
data/changelog.py

Every version's release notes, in the same shape data/manual.py holds the
manual: this file is all the text, and cogs/changelog.py only decides how it
gets on screen.

Adding a version is one _add(ChangelogVersion(...)) call at the TOP of the
list below, plus bumping config.VERSION. Nothing in the cog, the dropdown or
the choice list needs touching - VERSIONS' insertion order IS display order,
newest first, so the newest release is what /changelog opens on.

Notes start at 1.1. Version 1.0 shipped before there was anywhere to write
them down, and reconstructing it after the fact would be inventing a record
rather than keeping one.
"""
from dataclasses import dataclass

import discord

from utils.embeds import make_embed, add_multi_field, SCRAPPER_COLOR


@dataclass(frozen=True)
class ChangelogEntry:
    """One change. `heading` groups entries into embed fields, so a release
    with a lot in it reads as a set of areas rather than one wall of text."""
    heading: str
    text: str


@dataclass(frozen=True)
class ChangelogVersion:
    version: str                  # "1.1" - matches config.VERSION for the newest
    released: str                 # ISO date
    emoji: str
    color: discord.Color          # a docs/stylization.md palette color
    summary: str                  # one line, shown under the dropdown option (<= 100 chars)
    headline: str                 # the embed description
    entries: tuple[ChangelogEntry, ...] = ()


VERSIONS: dict[str, ChangelogVersion] = {}


def _add(version: ChangelogVersion):
    VERSIONS[version.version] = version


_add(ChangelogVersion(
    version="1.1",
    released="2026-08-03",
    emoji="♻️",
    color=SCRAPPER_COLOR,
    summary="The scrapper, a daily job board, and a bot channel setting",
    headline=(
        "A new machine, a new reason to log in daily, and a handful of things that should "
        "always have worked the way they do now."
    ),
    entries=(
        ChangelogEntry(
            "♻️ The Scrapper",
            "A fourth machine, and the factory in reverse. Feed it components, containers, "
            "upgrade packs or whole drills and it gives back half of what they were made "
            "from, one tier at a time. It never destroys a gemstone, and a scrapped drill "
            "loses its levels - see `/recipe scrapper` for exactly what everything returns.",
        ),
        ChangelogEntry(
            "📋 Job Board",
            "Every day your server posts one job asking for a material it's short of. Sell it "
            "that material with `/market sell` and you're paid a bonus on top of the sale. "
            "Everyone can claim it, once each, and progress adds up across as many sales as "
            "you like. A new job is posted at midnight Arizona time. `/jobboard` shows "
            "today's.",
        ),
        ChangelogEntry(
            "📺 Bot Channel",
            "`/setup channel` restricts Dragonhoard to a single channel and the threads inside "
            "it, for servers that would rather keep the traffic in one place. `/setup` and the "
            "manual always work anywhere, so it can't lock anyone out, and the restriction "
            "lifts itself if the channel is deleted.",
        ),
        ChangelogEntry(
            "⚙️ Queue Limits Scale",
            "A machine's per-player queue limit is now multiplied by its level, so a furnace "
            "levelled from 5 items/hour to 50 has ten times the room to match. Admins still "
            "set the base with `/setup max_queue`.",
        ),
        ChangelogEntry(
            "📦 Collecting",
            "`/collect` now shows what you hold of each material altogether, not just what "
            "this haul brought in, the same way a queue receipt shows what's left after it "
            "takes something. No more running `/inventory` straight afterwards to find out.",
        ),
        ChangelogEntry(
            "⛏️ Drills",
            "`/factory upgrade` now offers drills placed in the server you're in, pulling them "
            "out of the ground for you and handing back whatever they were holding. Storage "
            "containers, in exchange, can only be fitted to or pulled off drills that are in "
            "your inventory or placed in the server you're actually in.",
        ),
        ChangelogEntry(
            "💰 Market Order",
            "`/market status` and both market commands now list materials from raw to smelted "
            "to gemstones, commonest to rarest, instead of mixing gemstones in among the ores.",
        ),
        ChangelogEntry(
            "🐛 Fixes",
            "`/mine remove` could pay out a drill's contents twice if a `/collect` landed at "
            "exactly the wrong moment. It can't now.",
        ),
    ),
))


# The newest release, which is what /changelog opens on. VERSIONS is ordered
# newest first, so this is simply the first key.
LATEST_VERSION = next(iter(VERSIONS))

# Discord allows at most 25 options in a select menu. Twenty-five releases is a
# long way off, but the failure mode is a dropdown that silently drops the
# oldest entries, so the cog slices to this and a test pins it.
MAX_SELECTABLE_VERSIONS = 25


def build_version_embed(version: ChangelogVersion) -> discord.Embed:
    """Renders one release's notes. Entries go through add_multi_field so an
    unusually large release spills into a continuation field rather than being
    silently truncated at Discord's 1024-character field limit."""
    embed = make_embed(
        f"{version.emoji} Dragonhoard {version.version}",
        version.color,
        description=version.headline,
    )
    embed.set_author(name=f"Released {version.released}")
    for entry in version.entries:
        add_multi_field(embed, entry.heading, [entry.text])
    return embed
