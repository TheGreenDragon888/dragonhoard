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

from utils.embeds import make_embed, add_multi_field, JOBBOARD_COLOR, SCRAPPER_COLOR


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
    version="1.2",
    released="2026-08-06",
    emoji="📋",
    color=JOBBOARD_COLOR,
    summary="Mining Focus, guaranteed gemstones, cheaper machines, and a market fix",
    headline=(
        "The biggest update yet. **Mining Focus** lets you commit to one ore and get more of "
        "it, gemstones are now guaranteed to turn up rather than merely likely to, machines "
        "level up far sooner, and a hole in the market that was wrecking server economies is "
        "closed - and repaired."
    ),
    entries=(
        ChangelogEntry(
            "⛏️ Mining Focus",
            "Spend one Ruby, once, and `/focus` lets you commit your mining to a single ore - "
            "**Iron & Coal**, **Copper & Coal**, or **Coal**. Everything else you dig up "
            "arrives as your chosen ore instead. A copper ore is worth **two** iron ore, "
            "because iron drops twice as often, so the trade is even for the digging you "
            "actually did; you just stop receiving the thing you didn't want. Changing your "
            "mind is free, once a day. Gemstone odds are identical whatever you pick.\n\n"
            "Two warnings, because both are easy to find out too late: Copper and Coal focus "
            "**can't make steel at all** (steel needs iron ore, and neither produces any), and "
            "Iron focus helps steel less than you'd think - you end up rich in ore and short "
            "of coal instead. `/focus` on its own explains all of this before you spend "
            "anything.",
        ),
        ChangelogEntry(
            "⛏️ No More Daily Mining Limit",
            "The daily top-up is **gone**, and so is the cap. Your server's pool used to gain "
            "200 items per member per day and stop there, which meant a better drill bought you "
            "nothing once you were already draining the day's allowance - the only real way to "
            "mine more was to recruit more people. Now the pool is a batch of a million items "
            "that refills the instant it runs out, and how much your server produces is decided "
            "by how many drills you have running, how good they are, and how often you empty "
            "them. Mine as fast as you can build.",
        ),
        ChangelogEntry(
            "💎 Every Batch Holds A Diamond",
            "That batch contains exactly **90 Rubies, 9 Obsidian and 1 Diamond**, and drills "
            "pull from what's genuinely in it. So a gemstone isn't a one-in-a-million chance "
            "rolled fresh on every single item forever - it's a real thing sitting in the batch "
            "that somebody's drill **will** find before it runs out. Mine through a batch, get "
            "a Diamond. Every time, on every server. Your odds per item are exactly what they "
            "always were; what's gone is the luck. `/mine status` shows precisely which gems "
            "are still in there.",
        ),
        ChangelogEntry(
            "📋 One Job, One Payout",
            "Every job board task is now sized to pay a little over **1** of your server's "
            "currency, whatever it asks for. The amount asked for used to be a share of the "
            "whole server's stockpile, so it grew every time somebody new joined - on a large "
            "server the job could ask for more than anyone could mine in a day. It no longer "
            "depends on how many people are in the server at all.",
        ),
        ChangelogEntry(
            "📋 Jobs Read The Warehouse",
            "A job on something your server already has plenty of asks for more of it, because "
            "each one is worth less to them - and still pays about the same at the end. The "
            "bonus is now what the server would actually pay for the goods rather than a flat "
            "rate, so it moves with the price like everything else in the market does.",
        ),
        ChangelogEntry(
            "📦 Containers Hold More",
            "Every container above the Iron one now holds a good deal more, and each tier holds "
            "**twice** what the one below it does - 250, 500, 1,000, 2,000, 4,000. The gemstone "
            "containers were the reason: they cost a gem and used to buy less running time than "
            "the Iron Container did, because a faster drill filled them just as quickly. A "
            "Diamond Container now keeps even a heavily upgraded drill going for days. Nothing "
            "needs re-crafting: containers you already own are bigger as of right now, and a "
            "drill sitting full takes up its extra room on your next `/collect`.",
        ),
        ChangelogEntry(
            "🏗️ Machines Level Up Sooner",
            "Every machine level now costs **five** times the fees the last one did instead of "
            "ten, so the ladder runs 5, 25, 125 rather than 5, 50, 500. Because the cost "
            "compounds, that is a much bigger change than it sounds: getting a machine to level "
            "6 used to take 55,555 in fees and now takes 3,905. The old ladder was priced for an "
            "economy nobody has - fees are fractions of a coin, and the market pays about 1.00 "
            "for a hundred iron ore - so level 4 and beyond were theoretical. Fees you have "
            "already paid all still count.",
        ),
        ChangelogEntry(
            "💎 Gemstones Left The Market",
            "Rubies, obsidian and diamonds can no longer be sold to or bought from the server "
            "market. A ruby was worth 5,500 to the market against iron ore at about a "
            "hundredth of a coin, so a single sale minted more currency than a whole server "
            "could earn playing - and on any server under about thirty members the price "
            "barely dropped for the second, third or fourth one either. Sales that had "
            "already happened have been reversed: the currency is gone and the gem is back "
            "with whoever sold it. Gemstones are crafting materials now and only that.",
        ),
        ChangelogEntry(
            "💸 Donations",
            "`/donate infrastructure` pays your own currency into one of the server's machines "
            "and levels it up for everybody - the first way to push a machine along "
            "deliberately rather than waiting for use to do it, and it stacks with the fees "
            "already paid in. `/donate player` hands currency to someone else, with no cut "
            "taken. Both are per-server, like the currency itself.",
        ),
        ChangelogEntry(
            "👋 A Welcome For New Servers",
            "Dragonhoard now says hello when it joins a server and points whoever runs it at "
            "the three settings worth touching - naming the currency above all, since until "
            "that's done every price shows a placeholder. Existing servers aren't pestered.",
        ),
        ChangelogEntry(
            "📣 Notifications",
            "Dragonhoard can now tell you something once, the next time you use it. "
            "Announcements from the bot and notices from your server are separate, and you "
            "only ever get the latest of each - being away for a week doesn't mean scrolling "
            "through a backlog. There's nothing to turn on and no command; they arrive "
            "alongside whatever you were already doing and don't come back.",
        ),
    ),
))

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
