"""
data/changelog.py

Every version's release notes, in the same shape data/manual.py holds the
manual: this file is all the text, and cogs/changelog.py only decides how it
gets on screen.

Adding a version is one _add(ChangelogVersion(...)) call at the TOP of the
list below, plus bumping config.VERSION. Nothing in the cog, the dropdown or
the choice list needs touching - VERSIONS' insertion order IS display order,
newest first, so the newest release is what /changelog opens on.

A release note says what changed for players and nothing else (the rules are
in CLAUDE.md, "Writing text players read"):
- The headline is one sentence naming the main changes.
- One entry per change: what changed, with old -> new numbers when a number
  changed, and what happened to things players already had (refunds, resets,
  reversals). Why it changed goes in the commit message, not here.
- Minor changes and fixes are one-line bullets in a single entry.
- Condense, don't correct: a release describes the game on the day it
  shipped, so its numbers are typed out rather than read from data/materials.py,
  and later releases never rewrite its facts.
tests/test_changelog.py caps the headline, every entry and the whole page.

Notes start at 1.1. Version 1.0 shipped before there was anywhere to write
them down, and reconstructing it after the fact would be inventing a record
rather than keeping one.
"""
from dataclasses import dataclass

import discord

from utils.embeds import (
    make_embed,
    add_multi_field,
    MINING_COLOR,
    JOBBOARD_COLOR,
    SCRAPPER_COLOR,
    BLAST_FURNACE_COLOR,
    GOVERNMENT_COLOR,
    MARKET_COLOR,
)


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
    version="1.4.3",
    released="2026-11-10",
    emoji="💎",
    color=MINING_COLOR,
    summary="Gem drill levels add the drill's whole base speed, and scrapping one returns upgrade gems",
    headline=(
        "Gem drill levels now add the drill's whole base speed, and scrapping a levelled gem "
        "drill returns gems."
    ),
    entries=(
        ChangelogEntry(
            "💎 Gem Drill Levels Add The Whole Base Speed",
            "Each Ruby, Obsidian or Diamond Drill level now adds the drill's whole base speed, "
            "not half: a Ruby Drill goes 30, 60, 90 an hour (was 30, 45, 60). Drills already "
            "levelled speed up too. Iron and Steel Drills and upgrade costs are unchanged.",
        ),
        ChangelogEntry(
            "♻️ Scrapping A Gem Drill Returns Upgrade Gems",
            "`/scrapper drill` on a levelled Ruby, Obsidian or Diamond Drill now also returns "
            "**half the gems its levels cost**, rounded down but at least one. Upgrade packs "
            "aren't returned, and Iron and Steel Drills are unchanged.",
        ),
    ),
))


_add(ChangelogVersion(
    version="1.4.2",
    released="2026-10-11",
    emoji="💎",
    color=MINING_COLOR,
    summary="Stronger gem drills, a Market Tax, /bet close, bigger bonds, easier containers, a fairer GDP line",
    headline=(
        "Gem drill levels count for more, the Treasurer can set a Market Tax, and bets can "
        "close early."
    ),
    entries=(
        ChangelogEntry(
            "💎 Gem Drill Levels Count For More",
            "Each Ruby, Obsidian or Diamond Drill level now adds half the drill's base speed, "
            "not a fifth: a Ruby Drill goes 30, 45, 60 an hour (was 30, 36, 42). Drills already "
            "levelled speed up too. Iron and Steel Drills and upgrade costs are unchanged.",
        ),
        ChangelogEntry(
            "🏛️ The Fee Share And The Market Tax",
            "The government's tax is now the **Fee Share**: the part of each fee it keeps, so "
            "your fee doesn't change. The Treasurer can also set a **Market Tax** on player "
            "trades, paid by the seller. `/treasurer rate` sets both, replacing `/treasurer tax`.",
        ),
        ChangelogEntry(
            "📜 Bonds",
            "Bonds now come in sizes from 1 to 1,000, the larger ones opening up as revenue "
            "grows. Each payout finishes your oldest bond first, and repaid bonds arrive as one "
            "notice per server.",
        ),
        ChangelogEntry(
            "📦 Containers Go With The Drill",
            "`/mine place` can fit a container as the drill goes in, and `/mine remove` now "
            "hands the container back unless you set `keep_container`.",
        ),
        ChangelogEntry(
            "📊 A Fairer Import/Export Line",
            "`/economy gdp` now compares ore smelted with ore mined, so servers that make "
            "everything themselves no longer read as importers. Past figures were corrected, "
            "and balanced means within 15%, shown with the gap.",
        ),
        ChangelogEntry(
            "🔧 Smaller Changes And Fixes",
            "- `/bet close` stops a bet taking wagers early; its opener or Manage Server can use it.\n"
            "- A Fee Share between two steps was rounded down to the step below.\n"
            "- `/bonds holdings` is laid out like a status page.\n"
            "- Players with many unread notices get replies again; extra notices come later.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.4.1",
    released="2026-09-30",
    emoji="💰",
    color=MARKET_COLOR,
    summary="Ores and metals trade with the server only, 5+5 market entries, a week's expiry, tidier menus",
    headline=(
        "Ores and metals trade only with the server, market entries last a week, and 1.4's "
        "pages are shorter."
    ),
    entries=(
        ChangelogEntry(
            "🪙 Ores And Metals Trade With The Server Only",
            "Iron ore, copper ore, coal, iron, copper and steel can still be bought and sold with "
            "the server, but no longer listed or bid for. Listings and orders you had for them "
            "were returned.",
        ),
        ChangelogEntry(
            "📋 Five Of Each, For A Week",
            "You can have 5 listings and 5 orders per server, each lasting 7 days; entries "
            "already up got theirs from this update. Anything unsold or unfilled comes back with "
            "a notice. `/market entries` shows the time left.",
        ),
        ChangelogEntry(
            "🧾 Receipts And Status Pages",
            "Market, government and bond commands answer with a receipt instead of a paragraph, "
            "and `/government status`, `/economy`, `/bet` and `/market` are shorter.",
        ),
        ChangelogEntry(
            "📊 GDP Explained In /help",
            "What GDP, net importer and net exporter mean is now on the Economy page of `/help`. "
            "`/economy status` lists only gems actually mined.",
        ),
        ChangelogEntry(
            "🔧 Fixes",
            "- Losing bets no longer say \"returned to this side\".\n"
            "- Government refusals use your server's own currency.\n"
            "- Opening or settling a bet no longer shows you its own notice.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.4",
    released="2026-09-22",
    emoji="🏛️",
    color=GOVERNMENT_COLOR,
    summary="An elected government, the player market, prediction bets, /economy and Mining Affinity",
    headline=(
        "Servers elect a government, players trade and bet with each other, and a Diamond "
        "unlocks Mining Affinity."
    ),
    entries=(
        ChangelogEntry(
            "🏛️ Mayors and Treasurers",
            "Every Thursday your server elects a Mayor and a Treasurer with `/vote`; voting needs "
            "a drill placed here for 7 days. The Treasurer sets machine fees (x0.25 to x4) and a "
            "tax on them, and the Mayor spends it. `/setup fee` is gone and fees reset.",
        ),
        ChangelogEntry(
            "🏗️ The Mayor's Projects",
            "The Mayor can fund a machine's level, double its speed with an Infrastructure "
            "Enhancement, buy toward mining slots, or start a 48-hour Server Bonanza.",
        ),
        ChangelogEntry(
            "📜 Bonds",
            "The Mayor can sell bonds: `/bonds buy` lends the server money, repaid hourly from "
            "its tax with the Treasurer's premium. If you leave, they wait for you.",
        ),
        ChangelogEntry(
            "🏷️ The Player Market",
            "`/market list` sells anything you own at your price, even a drill, and "
            "`/market order` places a standing bid; `/market entries` and `/market cancel` "
            "manage them. Buying and selling try the best player price first. "
            "Exotic Matter can't be traded.",
        ),
        ChangelogEntry(
            "🎲 Prediction Bets",
            "`/bet open` stakes your currency on something happening; others back it or bet "
            "against it. The winners split the whole pot, with no house cut.",
        ),
        ChangelogEntry(
            "📊 /economy",
            "`/economy status` shows your server on one page, and `/economy gdp` what it "
            "produced over a day and a week.",
        ),
        ChangelogEntry(
            "💎 Mining Affinity",
            "A Diamond unlocks `/affinity`: every gem you mine arrives as one kind, at better than "
            "an even trade.",
        ),
        ChangelogEntry(
            "🧹 Smaller Changes and Fixes",
            "- Every fee now speeds a machine up, not only each level.\n"
            "- \"Fees collected\" is now **Mining slot progress**.\n"
            "- `/mine status` shows your enhancements and any Bonanza.\n"
            "- Fresh jobs and drills are no longer paid for time before they started.\n"
            "- `/mine remove` applies your Mining Efficiency.\n"
            "- Your first Diamond comes with a note.\n"
            "- \"batchs\" now reads \"batches\", and commands are faster.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.3.1",
    released="2026-08-28",
    emoji="⛏️",
    color=MINING_COLOR,
    summary="Drills sorted by material in /mine status, and a cleaner /collect receipt",
    headline="`/mine status` and `/collect` are easier to read. Mining itself is unchanged.",
    entries=(
        ChangelogEntry(
            "⛏️ Drills Sorted By Material",
            "`/mine status` lists drills from Diamond down to Iron, highest level first.",
        ),
        ChangelogEntry(
            "📦 A Cleaner /collect Receipt",
            "`/collect`'s summary is now a few short lines instead of one long sentence.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.3",
    released="2026-08-24",
    emoji="♨️",
    color=BLAST_FURNACE_COLOR,
    summary="Fixed prices, a repeatable job board, the blast furnace, mining slots, and efficiency",
    headline=(
        "Fixed prices, a job board that pays every time, the blast furnace, mining slots and "
        "Mining Efficiency."
    ),
    entries=(
        ChangelogEntry(
            "🏷️ Fixed Prices",
            "Prices no longer drop as your server's stock grows. Every price is fixed and the same "
            "on every server: Iron Ore 0.01, Copper Ore 0.02, Coal 0.03, Iron 0.15, Copper 0.30 "
            "and Steel 0.48. Buying back costs double.",
        ),
        ChangelogEntry(
            "📋 The Job Board Pays Every Time",
            "The bonus now pays every time you finish the job, not once a day, and leftovers count "
            "toward the next one. `/jobboard` counts your finishes.",
        ),
        ChangelogEntry(
            "♨️ The Blast Furnace",
            "`/blast smelt` runs the furnace's recipes in batches of 100, at the same cost per "
            "bar, smelting 100 items an hour per level against the furnace's 5. Its fee and "
            "queue limit count batches.",
        ),
        ChangelogEntry(
            "⛏️ Mining Slots",
            "Every machine fee and donation now counts toward extra drill slots for everyone here: "
            "25 unlocks a 4th drill, 125 a 5th, 625 a 6th. Servers already past a threshold got "
            "their slots at once.",
        ),
        ChangelogEntry(
            "🧠 Mining Efficiency",
            "One Obsidian unlocks `/efficiency`: double the raw materials one smelted recipe "
            "needs, in a mix that wastes less. It stacks with `/focus`.",
        ),
        ChangelogEntry(
            "🧹 Smaller Changes",
            "- Obsidian Drills mine 120 an hour (was 60) and Diamond 480 (was 120); containers 8,000 and 32,000.\n"
            "- A scrapped drill returns its bit plus 10 iron and 12 copper.\n"
            "- Buy or sell up to 1,000,000 at once (was 1,000); `/market buy` gets a receipt.\n"
            "- Your first Ruby or Obsidian comes with a note.\n"
            "- `/recipe` and `/help` cover the new features.",
        ),
    ),
))


_add(ChangelogVersion(
    version="1.2.2",
    released="2026-08-18",
    emoji="💎",
    color=MINING_COLOR,
    summary="Cheaper gem drill upgrades, a smoother job board, and required quantities",
    headline=(
        "Gem drill upgrades cost less, the job board picks materials more smoothly, and some "
        "quantities are required again."
    ),
    entries=(
        ChangelogEntry(
            "💎 Cheaper Gem Drill Upgrades",
            "Levelling a Ruby, Obsidian or Diamond Drill costs 1 gem per level instead of 3. "
            "Drill bits still cost 3.",
        ),
        ChangelogEntry(
            "📋 Job Board Selection Smoothed Out",
            "The more overstocked a material is, the less often the job board asks for it, "
            "tapering off gradually instead of dropping to a fixed floor.",
        ),
        ChangelogEntry(
            "🔢 Quantities Required Again",
            "`/market buy`, `/market sell` and `/furnace smelt` need a quantity again. "
            "`/factory craft`, `/press craft` and `/scrapper scrap` still default to 1.",
        ),
        ChangelogEntry(
            "🧾 Job Board Bonus Shown First",
            "`/market sell`'s receipt shows the job board bonus above your new totals.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.2.1",
    released="2026-08-17",
    emoji="⛏️",
    color=MINING_COLOR,
    summary="Faster gem drills, bigger gem containers, and optional quantities",
    headline="Gem drills mine much faster, their containers grew to match, and quantities are optional.",
    entries=(
        ChangelogEntry(
            "⛏️ Gem Drills Mine Faster",
            "At level 1, Ruby Drills mine 30 an hour (was 10), Obsidian 60 (was 12.5) and Diamond "
            "120 (was 15). Their containers grew to match: 2,000, 4,000 and 8,000 (were 1,000, "
            "2,000 and 4,000).",
        ),
        ChangelogEntry(
            "⏱️ Smoother Mining Progress",
            "Drills now update every 5 minutes instead of every 24.",
        ),
        ChangelogEntry(
            "🧾 Sale Receipts",
            "`/market sell` answers with a receipt: what sold, for how much, what you have left, "
            "your balance and any job board bonus.",
        ),
        ChangelogEntry(
            "🔢 Optional Quantities",
            "Item quantities on `/market buy`, `/market sell`, `/factory craft`, `/furnace smelt`, "
            "`/press craft` and `/scrapper scrap` now default to 1.",
        ),
        ChangelogEntry(
            "📖 Recipe Book Shows Drill Speed",
            "`/recipe factory` lists each drill's mining speed beside its recipe.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.2",
    released="2026-08-06",
    emoji="📋",
    color=JOBBOARD_COLOR,
    summary="Mining Focus, guaranteed gemstones, cheaper machines, and a market fix",
    headline=(
        "Mining Focus, no daily mining limit, guaranteed gemstones, and machines that level up "
        "sooner."
    ),
    entries=(
        ChangelogEntry(
            "⛏️ Mining Focus",
            "One Ruby unlocks `/focus`: everything you mine arrives as Iron & Coal, Copper & Coal, "
            "or Coal, traded evenly by rarity. Changing it is free once a day, and gem odds don't "
            "change. Copper and Coal focus can't make steel.",
        ),
        ChangelogEntry(
            "⛏️ No More Daily Mining Limit",
            "The daily cap is gone. Your server's pool is now a batch of a million items that "
            "refills as soon as it runs out.",
        ),
        ChangelogEntry(
            "💎 Every Batch Holds A Diamond",
            "Each batch holds exactly 90 Rubies, 9 Obsidian and 1 Diamond, so mining through a "
            "batch always finds them. `/mine status` shows which are left.",
        ),
        ChangelogEntry(
            "📋 Job Board Payouts",
            "Every job now pays a little over 1 of your currency, whatever your server's size, "
            "and the bonus follows market prices.",
        ),
        ChangelogEntry(
            "📦 Containers Hold More",
            "Each container tier holds twice the one below: 250, 500, 1,000, 2,000, 4,000. "
            "Containers you own grew straight away.",
        ),
        ChangelogEntry(
            "🏗️ Machines Level Up Sooner",
            "Each machine level costs five times the fees of the last instead of ten: 5, 25, 125. "
            "Fees already paid still count.",
        ),
        ChangelogEntry(
            "💎 Gemstones Left The Market",
            "Gems can no longer be sold to or bought from the server. Past gem sales were "
            "reversed: the currency was removed and the gems returned.",
        ),
        ChangelogEntry(
            "💸 Donations",
            "`/donate infrastructure` pays your currency into a machine to level it up for "
            "everyone; `/donate player` gives currency to another member, with no cut.",
        ),
        ChangelogEntry(
            "🧹 Smaller Changes",
            "- The bot greets new servers and points admins at the settings worth setting.\n"
            "- Notices: the bot can tell you something once, with your next command.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.1",
    released="2026-08-03",
    emoji="♻️",
    color=SCRAPPER_COLOR,
    summary="The scrapper, a daily job board, and a bot channel setting",
    headline="The scrapper, a daily job board, a bot channel setting, and some fixes.",
    entries=(
        ChangelogEntry(
            "♻️ The Scrapper",
            "A fourth machine that breaks components, containers, upgrade packs and drills back "
            "into half of what they were made from, one tier at a time. It never destroys a "
            "gemstone, and scrapped drills lose their levels.",
        ),
        ChangelogEntry(
            "📋 Job Board",
            "Each day your server posts one job: sell it a material with `/market sell` for a "
            "bonus on top of the sale. Everyone can claim it once. A new job comes at midnight "
            "Arizona time.",
        ),
        ChangelogEntry(
            "📺 Bot Channel",
            "`/setup channel` keeps Dragonhoard to one channel and its threads. `/setup` and "
            "`/help` still work anywhere, and the limit lifts if the channel is deleted.",
        ),
        ChangelogEntry(
            "🧹 Smaller Changes And Fixes",
            "- Queue limits now scale with a machine's level.\n"
            "- `/collect` shows your totals, not just the haul.\n"
            "- `/factory upgrade` can pull a drill straight out of the ground.\n"
            "- Containers can only be swapped on drills in your inventory or this server.\n"
            "- Market lists run from raw materials to gemstones.\n"
            "- `/mine remove` can no longer pay out a drill's contents twice.",
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
