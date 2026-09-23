"""
data/manual.py

The text of the in-Discord manual served by /help, /manual and /man
(cogs/manual.py). Kept here rather than in the cog so it's pure data with no
bot or database attached - tests/test_manual.py imports it directly and checks
every registered command has an entry.

Numbers that come from balance data are interpolated from data/materials.py
rather than typed out, so tuning the game doesn't quietly make the manual lie.
Prose that only describes *how* something works is written out longhand.

Adding a game later means adding a ManualSection here (and claiming a color in
docs/stylization.md) - the /help topic choices and the section dropdown are
both built from SECTIONS, so nothing in the cog needs touching.
"""
from dataclasses import dataclass

import discord

from utils.embeds import (
    make_embed,
    add_multi_field,
    DEFAULT_COLOR,
    MINING_COLOR,
    INVENTORY_COLOR,
    MARKET_COLOR,
    FURNACE_COLOR,
    BLAST_FURNACE_COLOR,
    FACTORY_COLOR,
    RECIPE_COLOR,
    PRESS_COLOR,
    SCRAPPER_COLOR,
    JOBBOARD_COLOR,
    BET_COLOR,
    GOVERNMENT_COLOR,
)

from data.materials import (
    BASE_STORAGE_CAPACITY,
    BLAST_FURNACE_BATCH_SIZE,
    BLAST_FURNACE_COAL_COST_PER_BATCH,
    BLAST_FURNACE_RATE_PER_LEVEL,
    BLAST_FURNACE_RECIPES,
    FURNACE_COAL_COST_PER_UNIT,
    FURNACE_RATE_PER_LEVEL,
    BASE_MINING_SLOTS,
    MINING_SLOT_THRESHOLD_BASE,
    UPGRADE_THRESHOLD_STEP,
    BONANZA_HOURS,
    BONANZA_MINIMUM_PRICE,
    ENHANCEMENT_PRICE_BASE,
    ENHANCEMENT_PRICE_STEP,
    MINING_SLOT_ENHANCEMENT_MULTIPLIER,
)
from utils.government import (
    BOND_DENOMINATIONS_CENTS,
    DEBT_CAP_DAYS,
    FEE_MULTIPLIERS,
    MAX_BOND_RATE_PERCENT,
    VOTER_DRILL_DAYS,
)

# What one batch of Steel actually costs, for the blast furnace page below.
# Read off the recipe rather than typed out, so retuning Steel can't leave the
# manual quoting the old figure - the same rule the rest of this file follows.
_STEEL_BATCH = BLAST_FURNACE_RECIPES["steel"]["inputs"]
_STEEL_BATCH_COAL = _STEEL_BATCH.get("coal", 0) + BLAST_FURNACE_COAL_COST_PER_BATCH


@dataclass(frozen=True)
class ManualCommand:
    """One command's entry. `name` is the canonical invocation with no
    parameters ("/mine place") and is what tests/test_manual.py matches against
    the live command tree; `usage` is the prettier form shown to the reader."""
    name: str
    usage: str
    description: str


@dataclass(frozen=True)
class ManualSection:
    key: str                                   # the /help topic value
    label: str                                 # dropdown option + embed title
    emoji: str
    color: discord.Color                       # this feature's docs/stylization.md color
    summary: str                               # one line, shown under the dropdown option
    body: str                                  # how this part of the game works
    commands: tuple[ManualCommand, ...] = ()
    notes: tuple[tuple[str, str], ...] = ()    # extra (field name, text) pairs


DEFAULT_SECTION = "start"


def format_bond(cents: int) -> str:
    """A bond denomination as the manual quotes it - a whole number of the
    server's currency, whatever it is called."""
    return f"{cents // 100:,}"


SECTIONS: dict[str, ManualSection] = {}


def _add(section: ManualSection):
    SECTIONS[section.key] = section


_add(ManualSection(
    key="start",
    label="Getting Started",
    emoji="📖",
    color=DEFAULT_COLOR,
    summary="What this bot is and your first five minutes",
    body=(
        "Dragonhoard is a game you play a few commands at a time. This server has its "
        "own currency and its own economy, and everything you own here is yours alone.\n\n"
        "**Your first five minutes**\n"
        "1. `/mine place` - puts a drill in the ground. If you don't own one yet you'll be "
        "given a free Iron Drill, already full.\n"
        "2. Go do something else. Your drill mines on its own, whether or not you're online.\n"
        "3. `/collect` - empties everything your drills have mined into your inventory.\n"
        "4. `/market sell` - sells those materials to the server for currency.\n"
        "5. Spend it. Smelt ore in the `/furnace`, craft better gear in the `/factory`, and "
        "upgrade your drills so the whole loop runs faster.\n\n"
        "Pick a section from the dropdown below to read about any part of the game."
    ),
    commands=(
        ManualCommand(
            "/help", "/help [topic]",
            "Opens this manual. Give it a topic to jump straight to that section.",
        ),
        ManualCommand(
            "/manual", "/manual [topic]",
            "Exactly the same as `/help` - use whichever name you remember.",
        ),
        ManualCommand(
            "/man", "/man [topic]",
            "The short name for `/help`, for when you're in a hurry.",
        ),
    ),
    notes=(
        (
            "The games",
            "**Mining** produces raw materials. The **Furnace** smelts them into metals (and "
            "the **Blast Furnace** does the same in batches of "
            f"{BLAST_FURNACE_BATCH_SIZE}), the **Factory** turns metals into components, "
            "drills and gear, and the **Hydraulic Press** crushes huge piles of ore into "
            "gemstones. The **Market** is where all of it turns into money.",
        ),
        (
            "Who can see your replies",
            "By default only you can see what the bot says back, so it never clutters the "
            "channel. A server admin can make replies public with `/setup messages public`.",
        ),
    ),
))


_add(ManualSection(
    key="mining",
    label="Mining & Drills",
    emoji="⛏️",
    color=MINING_COLOR,
    summary="Place drills, let them run, collect what they dig up",
    body=(
        "Mining is the source of every raw material in the game, and it runs without you. "
        "Place a drill and it digs on its own from that moment on.\n\n"
        "The whole server shares one pool of raw material - a batch of a million items that "
        "refills the moment it runs out. There's no daily limit and nothing to wait for: how "
        "much your server produces is decided by how many drills are running, how good they "
        "are, and how often somebody empties them.\n\n"
        "**Every batch contains exactly 90 Rubies, 9 Obsidian and 1 Diamond.** Drills pull from "
        "what's genuinely in it, so a gemstone isn't a long shot re-rolled forever - it's a "
        "real thing sitting in the batch that somebody will dig up before it runs out. Mine "
        "through a batch, find a Diamond. `/mine status` shows exactly which gems are left.\n\n"
        f"Every server starts you with **{BASE_MINING_SLOTS} mining slots** - {BASE_MINING_SLOTS} "
        "drills you can have in the ground here at once - and unlocks more as the server "
        "makes mining slot progress. Every fee anyone pays to the furnace, blast furnace, "
        "factory, press or scrapper counts toward the same total, and so does anything given "
        "with `/donate infrastructure` and every project the Mayor funds (see `/help "
        "government`). The first extra slot costs "
        f"**{MINING_SLOT_THRESHOLD_BASE:,.0f}** in progress and each one after that costs "
        f"{UPGRADE_THRESHOLD_STEP} times the last. `/mine status` shows how far along your "
        "server is. Slots are server-wide: when one unlocks, *everybody* here gets it.\n\n"
        f"Every drill holds **{BASE_STORAGE_CAPACITY}** items by itself, and once it "
        "fills up it stops and waits for you - so a bigger container, or simply collecting "
        "more often, is the difference between a drill that works all day and one that "
        "spends the afternoon idle.\n\n"
        "`/collect` reaches every server you have drills in, so one call from anywhere "
        "empties the lot. Your materials are yours wherever you earned them - only the "
        "drills, the pool and the currency belong to a particular server.\n\n"
        "Better drill types mine faster, and every level you add to a drill makes it faster "
        "still - a level is worth a fifth of that drill's own base speed, so an upgrade "
        "counts for as much on a Diamond Drill as it does on an Iron one. Drills are "
        "crafted and upgraded in the Factory."
    ),
    commands=(
        ManualCommand(
            "/mine place", "/mine place [drill]",
            "Puts one of your drills to work in this server. Leave the drill blank if you "
            "only have one spare. If you own no drills at all, you'll be given a free Iron "
            "Drill, already full, to start with.",
        ),
        ManualCommand(
            "/mine status", "/mine status",
            "Shows every drill you have here - its level, how full it is, what it's fitted "
            "with - plus what's left in the server's batch, including which gemstones are still "
            "in it.",
        ),
        ManualCommand(
            "/collect", "/collect [here]",
            "Empties every drill you have placed - in every server, not just this one - into "
            "your inventory at once, and shows what you now hold of each material "
            "altogether. This is the command you'll run most. Set `here` to True to collect "
            "only from your drills in this server.",
        ),
        ManualCommand(
            "/mine remove", "/mine remove <drill>",
            "Pulls a drill back out of the ground and into your inventory, handing you "
            "whatever it was holding. It keeps its level and its container.",
        ),
        ManualCommand(
            "/mine attach", "/mine attach <drill> <container>",
            "Fits a storage container to a drill so it can hold more before filling up. "
            "Swapping in a new container returns the old one to you.",
        ),
        ManualCommand(
            "/mine detach", "/mine detach <drill>",
            "Takes the container back off a drill. The container returns to your inventory "
            "undamaged - fitting and removing them costs nothing.",
        ),
        ManualCommand(
            "/focus", "/focus [focus]",
            "Commits your mining to one ore: everything else you dig up arrives as that "
            "instead. Costs one Ruby to unlock, then changing is free once a day. Run it "
            "with no option to see what each one does before you spend anything.",
        ),
        ManualCommand(
            "/efficiency", "/efficiency [efficiency]",
            "Doubles the raw materials one smelted recipe needs, then converts a little of "
            "whichever one you end up with too much of into the other. Costs one Obsidian "
            "to unlock, then changing is free once a day. Run it with no option to see what "
            "each one does, and whether your focus can feed it.",
        ),
        ManualCommand(
            "/affinity", "/affinity [affinity]",
            "Commits your gemstones to one kind: every other gem you mine arrives as that "
            "one instead, at better than an even trade. Costs one Diamond to unlock, then "
            "changing is free once a day and your progress comes with you. Run it with no "
            "option to see what each gem costs in the others.",
        ),
    ),
    notes=(
        (
            "Mining Focus",
            "A copper ore is worth two iron ore because iron drops twice as often, so a "
            "focus trades evenly for the digging you did - you just get more of what you "
            "actually want. Gemstone odds never change, whatever you choose.",
        ),
        (
            "Mining Efficiency",
            "Separate from your focus - you can have either, both or neither, and they "
            "stack. A focus decides which ore you dig up; an efficiency decides how much of "
            "it you get and in what proportion, so a haul smelts down with less left over. "
            "Pick the one matching what your focus actually mines: Steel wants iron ore, so "
            "it does very little on a Copper & Coal focus.",
        ),
        (
            "Mining Affinity",
            "The gem tier's version of a focus, and separate again from both of the others. "
            "45 rubies become a diamond, so the gems you were never going to spend turn "
            "into the one you were. It only ever changes which gem you get - how many you "
            "find is the server's pool and nothing here touches it. Part-finished progress "
            "is remembered and shown on /mine status, /collect and /affinity.",
        ),
        (
            "Gemstones",
            "Rubies, obsidian and diamonds come out of the ground on their own, but rarely "
            "enough that you shouldn't count on them. If you want a gem on a schedule rather "
            "than by luck, that's what the Hydraulic Press is for. They can't be sold to the "
            "server - a gem is worth what you build with it, not what it fetches.",
        ),
    ),
))


_add(ManualSection(
    key="furnace",
    label="Furnace",
    emoji="🔥",
    color=FURNACE_COLOR,
    summary="Smelt raw ore into usable metal",
    body=(
        "The furnace turns the ore your drills dig up into the metals almost everything else "
        "is built from. There is one furnace per server and everyone shares it.\n\n"
        "Smelting is not instant. You queue a job and the furnace works through it over time, "
        "so it's worth starting a batch before you log off. The materials and the fee are "
        "taken **when you queue the job**, not when it finishes.\n\n"
        "Your receipt tells you when that job will be ready, counting everything already in "
        "front of it - the furnace is shared, so a full queue means a longer wait for whoever "
        "joins the back of it.\n\n"
        f"Every item smelted also burns **{FURNACE_COAL_COST_PER_UNIT} extra coal** as fuel, "
        "on top of whatever its recipe already calls for - so keep coal in reserve, and don't "
        "sell all of it.\n\n"
        "Every fee paid into the furnace makes it smelt faster for everyone - not only when "
        "it levels up. Halfway to its next level, it runs halfway between the two levels' "
        "speeds."
    ),
    commands=(
        ManualCommand(
            "/furnace smelt", "/furnace smelt <material> <quantity>",
            "Queues raw materials to be smelted. You'll get a receipt showing what was "
            "consumed, the fuel burned, and the fee charged.",
        ),
        ManualCommand(
            "/furnace status", "/furnace status",
            "Shows the furnace's level, everything queued on it and when each job will be "
            "done, when the queue clears entirely, and how close it is to its next level.",
        ),
        ManualCommand(
            "/furnace queue", "/furnace queue",
            "The same screen as `/furnace status`, under the name you probably reached for.",
        ),
    ),
    notes=(
        (
            "Queue limits",
            "There's a cap on how many items you can have queued at once, so one player "
            "can't monopolise the furnace. Your server's admin sets it with `/setup max_queue`.",
        ),
        ("Recipes", "`/recipe furnace` lists everything the furnace can make and what it costs."),
        (
            "Smelting in bulk",
            f"Once you're moving thousands of ore at a time, the **Blast Furnace** smelts the "
            f"same recipes {BLAST_FURNACE_BATCH_SIZE} at a time and a great deal faster. Same "
            f"cost per bar, same fee per bar - see `/help blast`.",
        ),
    ),
))


_add(ManualSection(
    key="blast",
    label="Blast Furnace",
    emoji="♨️",
    color=BLAST_FURNACE_COLOR,
    summary=f"Smelt in batches of {BLAST_FURNACE_BATCH_SIZE}, for when the furnace can't keep up",
    body=(
        f"The blast furnace is a second, bulk-only smelter. It runs exactly the same recipes "
        f"the furnace does, {BLAST_FURNACE_BATCH_SIZE} at a time: one **batch** of Iron costs "
        f"{BLAST_FURNACE_BATCH_SIZE} times what one Iron costs at the furnace, and hands back "
        f"{BLAST_FURNACE_BATCH_SIZE} Iron.\n\n"
        f"It is not a discount. The ore per bar, the coal per bar and the fee per bar are all "
        f"identical to the furnace's - what you are buying is speed and elbow room. A blast "
        f"furnace smelts {BLAST_FURNACE_RATE_PER_LEVEL * BLAST_FURNACE_BATCH_SIZE:,} items an "
        f"hour per level against the furnace's {FURNACE_RATE_PER_LEVEL}, and the enormous jobs "
        f"that used to sit in the shared furnace queue for a day now happen out of everyone "
        f"else's way.\n\n"
        f"**Everything here is counted in batches.** The quantity you give `/blast smelt` is "
        f"how many batches of {BLAST_FURNACE_BATCH_SIZE} to make, the fee is charged per "
        f"batch, and your queue limit is a number of batches. Your receipt shows the real "
        f"totals: what came out of your inventory, and how many items are coming back.\n\n"
        f"Like the furnace, it takes the materials and the fee **when you queue the job**, it "
        f"gets faster with every fee paid into it, and it's shared with everyone in the server."
    ),
    commands=(
        ManualCommand(
            "/blast smelt", "/blast smelt <material> <batches>",
            f"Queues bulk smelting, in batches of {BLAST_FURNACE_BATCH_SIZE}. You'll get a "
            f"receipt showing what was consumed, the fuel burned, and the fee charged.",
        ),
        ManualCommand(
            "/blast status", "/blast status",
            "Shows the blast furnace's level, everything queued on it, when the queue clears, "
            "and how close it is to its next level.",
        ),
        ManualCommand(
            "/blast queue", "/blast queue",
            "The same screen as `/blast status`, under the name you probably reached for.",
        ),
    ),
    notes=(
        (
            "Is it worth it?",
            f"Only if you have the ore. One batch of Steel needs "
            f"{_STEEL_BATCH['iron_ore']:,} Iron Ore and {_STEEL_BATCH_COAL:,} Coal in one go, "
            f"and there's no part-batch - if you can't fill one, the furnace is still your "
            f"machine.",
        ),
        (
            "Recipes",
            "`/recipe furnace` lists both smelters' recipes side by side, so you can see the "
            "ratios are the same.",
        ),
    ),
))


_add(ManualSection(
    key="factory",
    label="Factory",
    emoji="🏭",
    color=FACTORY_COLOR,
    summary="Craft components, drills, containers and upgrades",
    body=(
        "The factory is where smelted metal becomes equipment: the components and drill bits "
        "that go into drills, the drills themselves, storage containers, and the upgrade "
        "packs that make a drill you already own better.\n\n"
        "Like the furnace, the factory works through a queue over time, and takes your "
        "materials and the fee up front when you place the job. The receipt says when the "
        "job will be ready, queue included.\n\n"
        "**Upgrading a drill** is a factory job too. The drill has to be in your inventory "
        "rather than in the ground, and it stays locked in the factory until the job "
        "finishes - so pull it out with `/mine remove` first, and expect it to be out of "
        "action for a while. Each level costs an Upgrade Pack plus that drill's own tier "
        "material, and the cost doubles with every level, so early levels are cheap and "
        "late ones are a project. Each one adds a fifth of the drill's base speed.\n\n"
        "The factory crafts faster with every fee paid into it, and levels up as they add up."
    ),
    commands=(
        ManualCommand(
            "/factory craft", "/factory craft <item> <quantity>",
            "Queues a component, drill, container or upgrade pack to be built.",
        ),
        ManualCommand(
            "/factory upgrade", "/factory upgrade <drill>",
            "Queues a level-up for one of your drills, raising how fast it mines. Run it to "
            "see exactly what that drill's next level will cost.",
        ),
        ManualCommand(
            "/factory status", "/factory status",
            "Shows the factory's level, every queued job and when it'll be done, and progress "
            "toward the next level.",
        ),
        ManualCommand(
            "/factory queue", "/factory queue",
            "The same screen as `/factory status`.",
        ),
    ),
    notes=(
        ("Recipes", "`/recipe factory` lists every factory recipe: drill components, drills and containers."),
    ),
))


_add(ManualSection(
    key="press",
    label="Hydraulic Press",
    emoji="⚙️",
    color=PRESS_COLOR,
    summary="Crush bulk ore into guaranteed gemstones",
    body=(
        "The hydraulic press is the patient way to get gemstones. Instead of waiting on a "
        "very unlikely drop, you feed it an enormous pile of ordinary ore and it gives you "
        "the gem outright.\n\n"
        "Each recipe costs a little less than you'd expect to mine alongside that gem before "
        "finding one naturally - so the press isn't a shortcut so much as a way of trading "
        "luck for certainty.\n\n"
        "Press jobs are measured in **press-days**, and they mean it: a press produces one "
        "press-day of work per day for each level it has, so a diamond worth nine press-days "
        "takes a level 1 press nine real days and a level 3 press three - and every fee paid "
        "toward its next level shaves a little more off. Queue it and forget "
        "about it. The fee is charged per press-day rather than per item, which is why "
        "pressing is the most expensive thing you can queue.\n\n"
        "A press earns nothing while it sits idle, so on a busy server it's worth keeping "
        "something in it."
    ),
    commands=(
        ManualCommand(
            "/press craft", "/press craft <product> <quantity>",
            "Queues a gemstone to be pressed. The receipt shows the bulk materials consumed, "
            "the total fee for the press-days involved, and the day it'll be ready.",
        ),
        ManualCommand(
            "/press status", "/press status",
            "Shows the press's level, what it's working on and when each job lands, and "
            "progress toward its next level.",
        ),
        ManualCommand(
            "/press queue", "/press queue",
            "The same screen as `/press status`.",
        ),
    ),
    notes=(
        ("Recipes", "`/recipe press` lists every press recipe along with how many press-days each takes."),
    ),
))


_add(ManualSection(
    key="market",
    label="Market & Currency",
    emoji="💰",
    color=MARKET_COLOR,
    summary="Sell to the server, buy from its stock, and how money works",
    body=(
        "Every server has its own currency with its own name and emoji, set by its admins. "
        "Currency is not shared between servers - what you earn here stays here.\n\n"
        "The server is one of the people you trade with, not the only one. It keeps its own "
        "warehouse, buys from you, and sells back at a markup - and you can trade with "
        "everyone else here too, through `/market list` and `/market order`. Buying and "
        "selling take the better deal automatically.\n\n"
        "**A player price has to beat the server's.** A listing must undercut what the "
        "server charges and a bid must beat what it pays, or it's an offer nobody has a "
        "reason to take. Things the server doesn't trade - gemstones, components, "
        "containers, drills - have no such limit.\n\n"
        "**Selling is the only way currency comes into existence.** There's no payout for "
        "chatting and no daily handout - if you want money, you mine and you sell. Money "
        "leaves again through the fees you pay to the machines you use, and when "
        "you buy materials back off the server.\n\n"
        "**Prices are fixed.** Every material is worth the same whether the server's "
        "warehouse is empty or overflowing, and every price is a round number of cents - so "
        "what a pile of ore is worth is something you can work out before you sell it. "
        "Buying back always costs exactly double what selling paid, which is where the "
        "server makes its margin.\n\n"
        "You can move up to **1,000,000** at a time in either direction.\n\n"
        "**Gemstones can't be sold to the server.** Rubies, obsidian and diamonds are worth "
        "so much more than anything else that one sale used to be worth more than a whole "
        "server could earn. The server won't touch them - but other players will.\n\n"
        "**Exotic Matter can never be traded at all.** Not to the server, not to another "
        "player, not for any price. It accrues and it stays yours - it's being held back "
        "for something that hasn't been built yet."
    ),
    commands=(
        ManualCommand(
            "/market sell", "/market sell <item> <quantity>",
            "Sells out of your inventory, to whichever player bid highest for it and then "
            "to the server. This is how you earn. The list only shows what you actually "
            "hold AND somebody here will buy - so if something you own isn't on it, "
            "nobody is currently buying it and `/market list` is how you find someone.",
        ),
        ManualCommand(
            "/market buy", "/market buy <item> [quantity]",
            "Buys from the cheapest source going - player listings first, since they have "
            "to undercut the server, then the server's own stock. The list shows only "
            "what's genuinely for sale right now, how much of it there is and what the "
            "best price is, including individual drills somebody has put up.",
        ),
        ManualCommand(
            "/market list", "/market list <item> <price> [quantity]",
            "Puts something of yours up for sale to the rest of the server at a price you "
            "set. Anything you own works - ore, gemstones, components, containers, even a "
            "specific drill with its level and container intact. What you list is held "
            "aside until somebody buys it or you take it back, so it leaves your inventory "
            "straight away.",
        ),
        ManualCommand(
            "/market order", "/market order <item> <quantity> <price>",
            "Puts up a standing offer to BUY something, at your price, from whoever wants "
            "to fill it. The money is held aside the moment you place it, so the order can "
            "always pay out - you get it back in full if you withdraw.",
        ),
        ManualCommand(
            "/market entries", "/market entries",
            "Everything you personally have on the market - what you're selling, what "
            "you're bidding for, and how much of your balance those bids are holding.",
        ),
        ManualCommand(
            "/market cancel", "/market cancel <entry>",
            "Takes back one of your own listings or orders and returns whatever it was "
            "holding - the goods, or the money. Pick which one from the list.",
        ),
        ManualCommand(
            "/market status", "/market status",
            "Shows what the server will pay, what it charges, and how much of each material "
            "it's holding - plus what the players are selling and bidding for, with the "
            "best price on each and how much is behind it. For your own entries, see "
            "`/market entries`.",
        ),
        ManualCommand(
            "/economy status", "/economy status",
            "The whole server's economy on one page: the wealth its players hold, its mining "
            "slot progress, what it produced this week, what's queued, and today's job. "
            "Read-only - it costs nothing to look.",
        ),
        ManualCommand(
            "/economy gdp", "/economy gdp",
            "What the server actually produced, in detail: the last 24 hours and the last 7 "
            "days, which stage of production added the value, and whether its machines ran "
            "on more than it dug up here.",
        ),
        ManualCommand(
            "/donate infrastructure", "/donate infrastructure <machine> <amount>",
            "Pays your own currency into one of the server's machines. It counts exactly "
            "like a fee, so it speeds the machine up for everyone - the only way to push a "
            "machine along deliberately instead of waiting for use to do it. It also counts "
            "toward the progress that unlocks mining slots, which every machine feeds together, "
            "so a donation buys progress on two ladders at once.",
        ),
        ManualCommand(
            "/donate player", "/donate player <member> <amount>",
            "Hands some of your currency to another member of this server. No fee is taken "
            "and nothing is lost on the way.",
        ),
    ),
    notes=(
        (
            "Where money goes",
            "Currency is created when the server buys materials from you, and destroyed when "
            "you pay a machine's fee, donate to one, or buy something back. A donation to "
            "another player is the one exception - that money just changes hands. A server "
            "that mints far more than it burns ends up with money that doesn't buy much, "
            "which is what the fees are quietly there to prevent.",
        ),
        (
            "What server GDP means",
            "`/economy gdp` reports what this server **produced**, which is a different thing "
            "from how much money it has. Mining counts what you dug out of this server's "
            "pool; smelting counts what the bars are worth **minus** the ore and coal they "
            "ate, so nothing is counted twice.\n\n"
            "It's credited to the server the work happened in, not the one you typed the "
            "command in - so if you mine here and smelt somewhere else, each server gets "
            "its own half. Gemstones are left out entirely: one Diamond is worth more than "
            "a month of everyone's mining, and a number that swings that far isn't telling "
            "you anything.\n\n"
            "It counts from when this was added, so a long-running server starts at nothing "
            "like everyone else - none of it was written down before.",
        ),
    ),
))


_add(ManualSection(
    key="jobboard",
    label="Job Board",
    emoji="📋",
    color=JOBBOARD_COLOR,
    summary="A daily task the server pays a bonus for",
    body=(
        "Every day, your server posts one job: sell it a certain amount of a material it's "
        "running short of. Finish it and you're paid a bonus on top of what the sale itself "
        "earned you.\n\n"
        "Every job is worth about the same: the amount asked for is whatever it takes to pay a "
        "little over **1** of your server's currency, and the bonus is that same **1** again. "
        "So finishing a job is worth roughly the sale all over again - a job you were going to "
        "do anyway is worth doing today instead.\n\n"
        "**It pays every time you finish it, not once a day.** Bring twice the amount asked "
        "for and you're paid twice; bring fifty times it and you're paid fifty times, all in "
        "the one command. Whatever the board asks for today is worth about double its market "
        "price for as long as you keep bringing it.\n\n"
        "You complete it through `/market sell` like any other sale; there's no separate "
        "hand-in. Progress adds up across as many sales as you like, so ten now and forty "
        "later counts the same as fifty at once - and anything left over after a finish counts "
        "toward the next one.\n\n"
        "It isn't a race: the job doesn't run out, and one player finishing it doesn't use it "
        "up for anyone else. A new one is posted at midnight Arizona time, and unfinished "
        "progress doesn't carry over.\n\n"
        "Which material gets asked for leans towards whatever the server's warehouse is "
        "shortest on."
    ),
    commands=(
        ManualCommand(
            "/jobboard", "/jobboard",
            "Today's job, what it pays, and how far into it you are.",
        ),
    ),
))


_add(ManualSection(
    key="inventory",
    label="Inventory & Balance",
    emoji="🎒",
    color=INVENTORY_COLOR,
    summary="See what you own and what you're worth",
    body=(
        "Your materials and drills are yours across the whole server, and your currency "
        "balance is tracked separately for each server you play in."
    ),
    commands=(
        ManualCommand(
            "/inventory", "/inventory",
            "Everything you own - your balance, your materials grouped by kind, and the "
            "drills you have spare. Drills already placed in a server aren't listed here; "
            "`/mine status` shows those, in the server they're working in.",
        ),
        ManualCommand(
            "/balance", "/balance",
            "Just the money: what you're holding in every server you play in, with this one "
            "listed first.",
        ),
    ),
))


_add(ManualSection(
    key="recipes",
    label="Recipe Book",
    emoji="📜",
    color=RECIPE_COLOR,
    summary="Look up what anything costs to make",
    body=(
        "The recipe book is the reference for every craftable thing in the game. It's worth "
        "reading before a big purchase - a lot of what looks expensive is cheaper to build "
        "than to buy."
    ),
    commands=(
        ManualCommand(
            "/recipe factory", "/recipe factory",
            "Every factory recipe at once: components and drill bits, drills and upgrade "
            "packs (with how upgrading works), and containers (with the capacity each one "
            "adds).",
        ),
        ManualCommand(
            "/recipe furnace", "/recipe furnace",
            "Every furnace recipe and the raw materials it takes, with the blast furnace's "
            "bulk version of each one beside it.",
        ),
        ManualCommand(
            "/recipe press", "/recipe press",
            "Every press recipe, with how many press-days each one occupies the press for.",
        ),
        ManualCommand(
            "/recipe scrapper", "/recipe scrapper",
            "What the scrapper hands back for every item it accepts. Worth a look before "
            "scrapping anything - some things come back better than others.",
        ),
    ),
))


_add(ManualSection(
    key="scrapper",
    label="Scrapper",
    emoji="♻️",
    color=SCRAPPER_COLOR,
    summary="Break components and drills back down into materials",
    body=(
        "The scrapper is the factory in reverse. Feed it a component, a container, an upgrade "
        "pack or a whole drill, and it gives you back **half** of what that thing was made "
        "from.\n\n"
        "It exists because components and drills can't be sold. The market only deals in raw "
        "and smelted materials, so before the scrapper a mis-planned batch of drill bits or a "
        "drill you'd outgrown just sat in your inventory forever. Now it's metal again.\n\n"
        "It only undoes **one step** at a time - scrapping a Drill Chassis gives you iron and "
        "copper, and scrapping that iron gives you ore. Chain it as far down as you need to "
        "go.\n\n"
        "**Drills are the exception.** Scrapping one skips the component step entirely: you get "
        "the drill's bit back whole, plus the iron and copper its wiring and chassis were made "
        "from (10 iron, 12 copper, whichever drill it is) - never a spare chassis or wiring you "
        "could put toward another drill for free.\n\n"
        "Two things are worth knowing before you commit. **A drill loses its levels** - a "
        "Level 8 drill comes back as exactly the same parts as a Level 1, so upgrade the drill "
        "you mean to keep. And **you never lose a gemstone to it**: the scrapper always returns "
        "at least one of a recipe's most valuable part, so a Ruby Container gives its ruby "
        "back.\n\n"
        "Like the other machines it takes a fee, works through a queue, and gets faster on "
        "the fees it collects - a level 1 scrapper gets through 2 items an hour, a level 2 "
        "gets through 4, and one halfway between the two gets through 3."
    ),
    commands=(
        ManualCommand(
            "/scrapper scrap", "/scrapper scrap <item> <quantity>",
            "Queues components, containers or upgrade packs to be broken down. They leave your "
            "inventory when you queue them, like anything else at a machine.",
        ),
        ManualCommand(
            "/scrapper drill", "/scrapper drill <drill>",
            "Breaks down one of your drills. It has to be in your inventory, not placed - "
            "`/mine remove` first. Any container on it is pulled off and handed back intact.",
        ),
        ManualCommand(
            "/scrapper status", "/scrapper status",
            "The scrapper's level, speed, fee and everything currently waiting in it.",
        ),
        ManualCommand(
            "/scrapper queue", "/scrapper queue",
            "The same page as `/scrapper status`, under the name you'll reach for when you "
            "just want to know what's in the machine.",
        ),
    ),
    notes=(
        (
            "This is permanent",
            "A scrapped drill is gone, along with every level you put into it. The scrapper "
            "will not stop you.",
        ),
    ),
))



_add(ManualSection(
    key="betting",
    label="Bets",
    emoji="🎲",
    color=BET_COLOR,
    summary="Bet the server's currency on what happens next",
    body=(
        "Anyone can propose something that might happen - `/bet open` - and stake their own "
        "currency saying it will. Everyone else can back them or take the other side. When "
        "the time comes, a server admin says which way it went and the whole pot is split "
        "between the people who were right.\n\n"
        "**Your winnings depend on the odds, and the odds are just who bet what.** All the "
        "money staked goes into one pot. If the winning side put in a tenth of that pot, "
        "everyone on it gets ten times what they staked back - their own money plus the "
        "money that was bet against them. If the winning side was the crowded one, the "
        "payout is small, because there was less to win.\n\n"
        "**Nothing is created and nothing is taken.** The pot pays out exactly what went "
        "into it - there's no house cut and the bot keeps nothing. Every unit somebody wins "
        "is a unit somebody else lost.\n\n"
        "Your stake leaves your balance the moment you place it and is held until the bet "
        "settles, so you can't stake money and spend it too. **You pick a side once** - you "
        "can add more to it later, but you can't switch sides or back out.\n\n"
        "You'll see new bets on your next command, with buttons to take either side. "
        "`/bet status` shows you everything running here whenever you want it."
    ),
    commands=(
        ManualCommand(
            "/bet open", "/bet open <prediction> <amount> <closes_in>",
            "Proposes something and stakes that it happens. `closes_in` is how many hours "
            "people have to join in.",
        ),
        ManualCommand(
            "/bet place", "/bet place <bet> <side> <amount>",
            "Backs a bet or takes the other side. The buttons do the same thing.",
        ),
        ManualCommand(
            "/bet status", "/bet status [bet]",
            "One bet's pools and odds, or a list of everything running here.",
        ),
        ManualCommand(
            "/bet resolve", "/bet resolve <bet> <outcome>",
            "Says which way it went and pays the pot out. Needs Manage Server.",
        ),
        ManualCommand(
            "/bet cancel", "/bet cancel <bet>",
            "Calls the whole thing off and hands every stake back. Needs Manage Server.",
        ),
    ),
    notes=(
        (
            "If nobody takes the other side",
            "A bet only pays out if somebody was on the winning side. If an admin resolves a "
            "bet the way nobody backed, there's nothing to pay and no honest way to keep the "
            "losing stakes - so the bet is voided and everyone gets their own money back. The "
            "same happens if it's cancelled.",
        ),
        (
            "Odds move until the bet closes",
            "The multiple you see is what the bet pays **right now**. Every wager after yours "
            "changes it - money arriving on your side shares the pot more ways, money arriving "
            "against you makes the pot bigger. What's locked in is your stake and your side, "
            "never the odds you saw when you placed it.",
        ),
    ),
))

_add(ManualSection(
    key="government",
    label="Government",
    emoji="🏛️",
    color=GOVERNMENT_COLOR,
    summary="Elect a Mayor and a Treasurer, lend the server money, fund big projects",
    body=(
        "Every server elects a **Mayor** and a **Treasurer** each week, and nobody can hold "
        "both. Admins have no say in any of it.\n\n"
        "**Voting is on Thursdays** (the job board's clock), and the votes are counted at "
        "midnight. You can't vote for yourself, and to vote at all you need a drill that has "
        f"been placed in this server for at least {VOTER_DRILL_DAYS} days - or one that was "
        "already in the ground when elections arrived. Vote again and only "
        "your latest counts. An office nobody votes on keeps its holder, so a Mayor stays "
        "Mayor until somebody votes for somebody else. The Mayor is decided first; if the "
        "Treasurer ballot's winner just became Mayor, the next one down gets it.\n\n"
        "**The Treasurer sets the money.** Each machine's fee is its default times "
        + ", ".join(f"x{m:g}" for m in FEE_MULTIPLIERS) + ", and a **tax** of 0-100% of every "
        "fee goes to the government instead of being destroyed. Each setting can change once "
        "a day.\n\n"
        "**The Mayor spends it.** Tax lands in the treasury, and the Mayor spends it on "
        "projects: funding a machine's level, an **Infrastructure Enhancement** (doubles one "
        f"machine's speed on top of its level - {ENHANCEMENT_PRICE_BASE:,.0f} for the first, "
        f"{ENHANCEMENT_PRICE_STEP} times as much for each after), a **Mining Slot "
        f"Enhancement** (every 1 spent counts {MINING_SLOT_ENHANCEMENT_MULTIPLIER} toward the "
        f"next mining slot), or a **Server Bonanza** ({BONANZA_HOURS} hours of double-speed "
        "drills and machines, priced at half the server's weekly GDP and never under "
        f"{BONANZA_MINIMUM_PRICE:,.0f}). Every project spend is destroyed, just as the fee "
        "would have been - and it all counts toward mining slots."
    ),
    commands=(
        ManualCommand("/government status", "/government status",
                      "Who holds office, the fees and tax, the treasury and the debt, and what "
                      "each project costs right now."),
        ManualCommand("/vote mayor", "/vote mayor <member>", "Votes for a Mayor. Thursdays only."),
        ManualCommand("/vote treasurer", "/vote treasurer <member>", "Votes for a Treasurer. Thursdays only."),
        ManualCommand("/treasurer fee", "/treasurer fee <machine> <multiplier>",
                      "Sets one machine's fee as a multiple of its default. Treasurer only."),
        ManualCommand("/treasurer tax", "/treasurer tax <percent>",
                      "Sets the tax on every machine fee. Treasurer only. It can't drop below the "
                      "rate bonds were last sold at while the server owes on them."),
        ManualCommand("/treasurer bondrate", "/treasurer bondrate <percent>",
                      f"Sets the premium new bonds repay, 0-{MAX_BOND_RATE_PERCENT}%. Treasurer only."),
        ManualCommand("/mayor fund", "/mayor fund <machine> <amount>",
                      "Pays treasury money into a machine's level, like `/donate infrastructure`. "
                      "Mayor only."),
        ManualCommand("/mayor enhance", "/mayor enhance <machine>",
                      "Buys a machine an Infrastructure Enhancement. Mayor only."),
        ManualCommand("/mayor slots", "/mayor slots <amount>",
                      "Spends treasury money on a Mining Slot Enhancement. Mayor only."),
        ManualCommand("/mayor bonanza", "/mayor bonanza", "Starts a Server Bonanza. Mayor only."),
        ManualCommand("/mayor bonds", "/mayor bonds <amount>",
                      "Puts bonds up for sale, or withdraws the sale with 0. Mayor only."),
        ManualCommand("/bonds buy", "/bonds buy <denomination>",
                      "Lends the server currency from the Mayor's bond sale."),
        ManualCommand("/bonds holdings", "/bonds holdings", "What the server still owes you."),
    ),
    notes=(
        (
            "Bonds",
            "A bond lends the server money now and is repaid out of tax: "
            + ", ".join(format_bond(c) for c in BOND_DENOMINATIONS_CENTS) + ", plus the "
            "Treasurer's premium, fixed when you buy. While the server owes anything, **all** "
            "of its tax repays bondholders - every hour, split in proportion to what each is "
            f"still owed. The server can't owe more than its last {DEBT_CAP_DAYS} days of tax. "
            "Leave the server and your bonds are frozen, not lost: they pick up again when you "
            "come back.",
        ),
    ),
))

_add(ManualSection(
    key="setup",
    label="Server Setup",
    emoji="🛠️",
    color=DEFAULT_COLOR,
    summary="Admin settings — requires Manage Server",
    body=(
        "**These commands require the Manage Server permission.** They're listed here so "
        "everyone can see how their server is configured, but only admins can change "
        "anything.\n\n"
        "Each server is configured on its own: its currency has whatever name and emoji its "
        "admins chose, and the queue limits on its machines are set locally too. What the "
        "machines **charge** isn't an admin setting: the Treasurer your server elects sets "
        "it (`/help government`).\n\n"
        "Replies from the bot are private by default so it stays out of the way. A server "
        "with a dedicated bot channel may prefer to make them public.\n\n"
        "Dragonhoard answers in every channel unless an admin points it at one with "
        "`/setup channel`. That's a tidiness setting rather than a security one - it keeps the "
        "bot's traffic in one place - and `/setup` and the manual always work anywhere, so "
        "it's never possible to lock yourself out with it."
    ),
    commands=(
        ManualCommand(
            "/setup messages", "/setup messages <visibility>",
            "Sets whether the bot's replies are visible to everyone or only to the person who "
            "ran the command. Private by default.",
        ),
        ManualCommand(
            "/setup currency", "/setup currency <name> <emoji>",
            "Names this server's currency and picks the emoji shown beside it.",
        ),
        ManualCommand(
            "/setup channel", "/setup channel [channel]",
            "Restricts the bot to one channel, and to threads inside it. Leave the channel "
            "blank to lift the restriction and allow every channel again.",
        ),
        ManualCommand(
            "/setup max_queue", "/setup max_queue <infrastructure> <amount>",
            "Limits how many items one player can have queued on a machine at a time (batches, "
            "for the blast furnace), so no one person can tie it up. Multiplied by the "
            "machine's level, so a machine that gets faster gets roomier too.",
        ),
    ),
))


_add(ManualSection(
    key="extras",
    label="Extras",
    emoji="🪿",
    color=DEFAULT_COLOR,
    summary="Commands that aren't part of the game at all",
    body=(
        "Not everything the bot does is a game. Nothing on this page costs anything, earns "
        "anything, or touches your inventory - it's here because it's funny."
    ),
    commands=(
        ManualCommand(
            "/honk", "/honk",
            "Honks. The bot sends the sound as an audio clip, so hit play on it.",
        ),
        ManualCommand(
            "/changelog", "/changelog [version]",
            "What changed in each version of Dragonhoard, newest first. Worth a look if "
            "something works differently to how you remember it.",
        ),
    ),
))


def build_section_embed(section: ManualSection) -> discord.Embed:
    """Renders one manual page. Command lines go through add_multi_field so a
    section that outgrows Discord's 1024-character field limit spills into a
    continuation field instead of being silently truncated."""
    embed = make_embed(f"{section.emoji} {section.label}", section.color, description=section.body)
    if section.commands:
        add_multi_field(
            embed,
            "Commands",
            [f"`{cmd.usage}`\n{cmd.description}" for cmd in section.commands],
        )
    for name, text in section.notes:
        embed.add_field(name=name, value=text, inline=False)
    return embed
