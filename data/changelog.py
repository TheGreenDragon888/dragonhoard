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

from utils.embeds import (
    make_embed,
    add_multi_field,
    MINING_COLOR,
    JOBBOARD_COLOR,
    SCRAPPER_COLOR,
    BLAST_FURNACE_COLOR,
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
    version="1.3.1",
    released="2026-08-28",
    emoji="⛏️",
    color=MINING_COLOR,
    summary="Drills sorted by material in /mine status, and a cleaner /collect receipt",
    headline=(
        "A small readability patch: `/mine status` and `/collect` are easier to scan, "
        "nothing about how mining works has changed."
    ),
    entries=(
        ChangelogEntry(
            "⛏️ Drills Sorted By Material",
            "`/mine status` now lists **Your Drills**, and the drill-type breakdown under "
            "**Server Mining Speed**, from Diamond down to Iron, and within a material the "
            "highest-leveled drill leads - instead of whatever order they happened to be "
            "placed in.",
        ),
        ChangelogEntry(
            "📦 A Cleaner /collect Receipt",
            "`/collect`'s summary now reads over a few short lines instead of one long "
            "sentence, and leaves room for however many focus- or efficiency-style options "
            "end up affecting your haul at once.",
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
        "Market prices have stopped moving. Every material is now worth one fixed, round "
        "number, whatever your server is holding - and the job board pays out every single "
        "time you finish it instead of once a day. Underneath that, a fifth machine: the "
        "blast furnace smelts a hundred at a time, Mining Efficiency joins Mining Focus as a "
        "second obsidian-gated way to shape your haul, and every fee your server has ever "
        "paid now counts toward extra mining slots, for everybody."
    ),
    entries=(
        ChangelogEntry(
            "🏷️ Fixed Prices",
            "The market used to pay less for something the more of it your server already "
            "had. It doesn't any more - **every price is fixed**, and the same on every "
            "server. Iron Ore is 0.01, Copper Ore 0.02, Coal 0.03, Iron 0.15, Copper 0.30 "
            "and Steel 0.48. Buying back costs exactly double.\n\nThey're round numbers of "
            "cents on purpose: what a pile of ore is worth is now something you can work out "
            "in your head before you sell it, instead of something you find out afterwards.",
        ),
        ChangelogEntry(
            "⛏️ Obsidian and Diamond Drills, Faster Again",
            "Following 1.2.1's pass on all three gem drills, the top two jump again: "
            "Obsidian **60 → 120** and Diamond **120 → 480** raw materials an hour, "
            "containers grown to match (Obsidian **8,000**, Diamond **32,000**) so a full "
            "one still buys about the same running time. Ruby, Iron and Steel are "
            "unchanged.",
        ),
        ChangelogEntry(
            "📦 Sell a Million at a Time",
            "`/market sell` and `/market buy` now take up to **1,000,000** in one command, "
            "up from 1,000. A drill wearing a Diamond Container holds 32,000 items - that "
            "used to be thirty-two sales. `/market buy` also gets a full receipt now, "
            "matching `/market sell`'s since 1.2.1, instead of one line of plain text.",
        ),
        ChangelogEntry(
            "📋 The Job Board Pays Every Time",
            "The daily job used to pay its bonus once per player per day. Now it pays **every "
            "time you finish it** - bring twice what it asks for and you're paid twice, bring "
            "fifty times it and you're paid fifty times, all in the one sale. Whatever the "
            "board is asking for is worth about double its market price all day.\n\n"
            "Anything left over after a finish counts toward the next one, and `/jobboard` "
            "shows how many times you've finished it today.",
        ),
        ChangelogEntry(
            "♨️ The Blast Furnace",
            "`/blast smelt <material> <batches>` queues bulk smelting in batches of **100**. "
            "One batch of Iron takes 1,000 Iron Ore and hands back 100 Iron - the exact same "
            "recipe the furnace runs, multiplied by a hundred. `/blast status` and "
            "`/blast queue` show its level and what's waiting on it.",
        ),
        ChangelogEntry(
            "⚖️ The Same Price, Not a Cheaper One",
            "The ore per bar, the coal per bar and the fee per bar are all identical to the "
            "furnace's - the default fee is **1.00 per batch**, which is the furnace's 0.01 "
            "an item times the hundred items in a batch. Bulk smelting buys speed and elbow "
            "room, not a discount.",
        ),
        ChangelogEntry(
            "⚡ Twenty Times the Throughput",
            "A blast furnace smelts **100 items an hour per level** against the furnace's 5. "
            "The 27,000 Steel behind one pressed diamond took a level 5 furnace 45 days of "
            "smelting nothing else; a level 1 blast furnace gets through it in 11.25.",
        ),
        ChangelogEntry(
            "⚙️ Server Settings",
            "`/setup fee`, `/setup max_queue` and `/donate infrastructure` all list the blast "
            "furnace alongside the other four machines. Its fee and its queue limit are "
            "counted in **batches**, and it starts at 5 batches per user per level.",
        ),
        ChangelogEntry(
            "⛏️ Mining Slots",
            "The 3-drills-per-server limit is no longer a fixed rule - it's the **starting "
            "point of a ladder your server unlocks**. Every fee anyone pays to the furnace, "
            "blast furnace, factory, press or scrapper adds to one shared total, and so does "
            "anything given with `/donate infrastructure`. Cross **25** and everybody here "
            "can place a 4th drill; **125** buys a 5th, **625** a 6th, and so on at five "
            "times the last each time.",
        ),
        ChangelogEntry(
            "🤝 Slots Are Server-Wide",
            "A slot isn't bought per player - when the server crosses a threshold, *every* "
            "member gets the extra slot at once, and a notice goes out saying so. It's the "
            "server's investment paying off, not an individual purchase. `/mine status` now "
            "shows how many of your slots are filled and how far along the server is toward "
            "the next one.\n\nNothing is spent twice: fees still level the machine they "
            "were paid to. Servers that had already paid their way past a threshold before "
            "this release had the slots the moment it shipped.",
        ),
        ChangelogEntry(
            "🧠 Mining Efficiency",
            "A second lever next to `/focus`, unlocked with one Obsidian instead of a Ruby. "
            "`/efficiency` commits you to one smelted recipe - Iron, Copper or Steel - and "
            "every collection **doubles** the raw materials it needs, then converts a little "
            "of whichever one you have too much of into the one you're short. Stacked on a "
            "matching focus it's the biggest jump in the game: at least double the focus's "
            "own output, up to +180% for Steel over Iron & Coal.\n\nIt stacks independently "
            "of `/focus` - a mismatched pair still helps, just less. Run it alone to see "
            "what each option needs before you spend the gem.",
        ),
        ChangelogEntry(
            "♻️ Scrapping a Drill Pays Out More",
            "Scrapping a drill used to hand back one whole part - an Iron Drill became a "
            "Drill Chassis - since a drill's recipe is one of each part and half of one is "
            "nothing. Now it skips straight past the component step: the bit comes back "
            "whole, plus the iron and copper its wiring and chassis were built from "
            "(**10 iron, 12 copper**, any drill), instead of a spare part you could build "
            "another drill from for free. `/recipe scrapper` has the numbers.",
        ),
        ChangelogEntry(
            "💎 A Nudge on Your First Gem",
            "The first Ruby or Obsidian you ever get now comes with a private note "
            "explaining what it unlocks. `/focus` and `/efficiency` don't show up "
            "anywhere until you own the gem that opens them, so it was entirely "
            "possible to sit on a ruby for weeks without knowing it did anything.",
        ),
        ChangelogEntry(
            "📖 Documentation",
            "`/recipe furnace` now shows both smelters' recipes side by side, `/recipe "
            "factory` shows every section on one page instead of asking you to pick one, "
            "`/help blast` is the new manual page, and `/help mining` covers mining slots "
            "and mining efficiency. `/help market` and `/help jobboard` have been rewritten "
            "for the fixed prices and the repeatable bonus.",
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
        "A small hotfix on top of 1.2.1: gem-tier drill upgrades cost a lot less, the job "
        "board's material selection is smoother, and a few commands that used to default "
        "their quantity now ask for it explicitly."
    ),
    entries=(
        ChangelogEntry(
            "💎 Cheaper Gem Drill Upgrades",
            "Levelling up a Ruby, Obsidian or Diamond Drill now costs **1** of its gem per "
            "level instead of 3. The drill bits themselves are unchanged - still 3 gems to "
            "craft.",
        ),
        ChangelogEntry(
            "📋 Job Board Selection Smoothed Out",
            "The daily job board's odds of landing on a particular material now taper off "
            "gradually as that material gets more overstocked, instead of levelling off at a "
            "fixed floor the moment it hit target stock. A material sitting right at target "
            "stock now has a real shot at coming up; one well past it barely does.",
        ),
        ChangelogEntry(
            "🔢 Quantities Required Again",
            "`/market buy`, `/market sell` and `/furnace smelt` now require a quantity - it's "
            "no longer optional for these three. `/factory craft`, `/press craft` and "
            "`/scrapper scrap` are unchanged and still default to 1 if left blank.",
        ),
        ChangelogEntry(
            "🧾 Job Board Bonus Shown First",
            "`/market sell`'s receipt now shows the job board bonus (if the sale finished "
            "today's task) above the new totals instead of below them.",
        ),
    ),
))

_add(ChangelogVersion(
    version="1.2.1",
    released="2026-08-17",
    emoji="⛏️",
    color=MINING_COLOR,
    summary="Faster gem drills, bigger gem containers, and optional quantities",
    headline=(
        "A focused follow-up to 1.2. The three gemstone drills mine a lot faster, their "
        "containers grew to match, mining progress updates in smaller steps, and every "
        "quantity field you fill in by hand is now optional."
    ),
    entries=(
        ChangelogEntry(
            "⛏️ Gem Drills Mine Faster",
            "Ruby, Obsidian and Diamond Drills all mine much faster at level 1 — Ruby **10 → "
            "30**, Obsidian **12.5 → 60**, Diamond **15 → 120** raw materials an hour. Their "
            "matching containers grew by the same factor so a full one still buys about as "
            "much running time as before: Ruby now holds **2,000** (was 1,000), Obsidian "
            "**4,000** (was 2,000), Diamond **8,000** (was 4,000). Iron and Steel drills and "
            "containers are unchanged. `/recipe factory` shows the new rates and capacities.",
        ),
        ChangelogEntry(
            "⏱️ Smoother Mining Progress",
            "Drills now check in every **5 minutes** instead of every 24, matching how often "
            "the other three machines already did. With gem drills running so much faster, "
            "the old 24-minute tick would fill a drill's own 100-item bay in one jump far "
            "sooner than it used to; the shorter tick keeps `/mine status` showing real "
            "progress in between instead of an occasional leap straight to full.",
        ),
        ChangelogEntry(
            "🧾 Sale Receipts",
            "`/market sell` now hands back a proper receipt: what you sold, what it sold for, "
            "how much of that material you have left, and your new balance — all in one "
            "embed, the same way `/collect` already shows post-haul totals. If the sale "
            "finished today's job board task, the bonus shows up right alongside it.",
        ),
        ChangelogEntry(
            "🔢 Optional Quantities",
            "Every command that asks how many of an item — `/market buy`, `/market sell`, "
            "`/factory craft`, `/furnace smelt`, `/press craft`, `/scrapper scrap` — now lets "
            "you leave the quantity blank and defaults to **1**. Currency amounts still have "
            "to be typed out.",
        ),
        ChangelogEntry(
            "📖 Recipe Book Shows Drill Speed",
            "`/recipe factory` → Drills now lists each drill's mining speed right next to its "
            "crafting recipe, so you don't have to leave the recipe book to check what a drill "
            "you're about to build actually does.",
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
            "leveled from 5 items/hour to 50 has ten times the room to match. Admins still "
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
