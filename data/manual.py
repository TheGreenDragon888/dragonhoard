"""
data/manual.py

The text of the in-Discord manual served by /help, /manual and /man
(cogs/manual.py). Kept here rather than in the cog so it's pure data with no
bot or database attached - tests/test_manual.py imports it directly and checks
every registered command has an entry.

Numbers that come from balance data are interpolated from the modules that
own them (data/materials.py, utils/government.py, ...) rather than typed out,
so tuning the game doesn't quietly make the manual lie.

Every page has the same shape: a sentence or two on what this part of the
game is, one line per command, and at most three tips. What earns a place on
it is in CLAUDE.md, "Writing text players read", and tests/test_manual.py
caps every page's word count.

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
    BLAST_FURNACE_RECIPES,
    FURNACE_COAL_COST_PER_UNIT,
    BASE_MINING_SLOTS,
    BONANZA_HOURS,
    MINING_SLOT_ENHANCEMENT_MULTIPLIER,
    drill_scrap_yield,
)
from utils.government import MAX_BOND_RATE_PERCENT, VOTER_DRILL_DAYS
from utils.market_book import MARKET_ENTRY_LIFETIME_DAYS, MAX_OPEN_LISTINGS, MAX_OPEN_ORDERS
from utils.production_ledger import BALANCED_ORE_GAP

# What one batch of Steel actually costs, for the blast furnace page below.
# Read off the recipe rather than typed out, so retuning Steel can't leave the
# manual quoting the old figure - the same rule the rest of this file follows.
_STEEL_BATCH = BLAST_FURNACE_RECIPES["steel"]["inputs"]
_STEEL_BATCH_COAL = _STEEL_BATCH.get("coal", 0) + BLAST_FURNACE_COAL_COST_PER_BATCH

# What a scrapped drill returns besides its bit (and, for a levelled gem drill,
# the refunded gems), for the scrapper page. Every drill is built from the
# same wiring and chassis, so a level 1 Iron Drill's yield stands for all of
# them.
_DRILL_SCRAP = drill_scrap_yield("iron_drill")


@dataclass(frozen=True)
class ManualCommand:
    """One command's line. `name` is the canonical invocation with no
    parameters ("/mine place") and is what tests/test_manual.py matches against
    the live command tree; `usage` is the prettier form shown to the reader.
    `aliases` are other commands that do exactly the same thing ("/furnace
    queue" for "/furnace status"); they share this line instead of taking one
    each, and the coverage tests count them as documented."""
    name: str
    usage: str
    description: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManualSection:
    key: str                                   # the /help topic value
    label: str                                 # dropdown option + embed title
    emoji: str
    color: discord.Color                       # this feature's docs/stylization.md color
    summary: str                               # one line, shown under the dropdown option
    body: str                                  # a sentence or two: what this part of the game is
    commands: tuple[ManualCommand, ...] = ()
    tips: tuple[str, ...] = ()                 # at most three, shown under "Good to know"


DEFAULT_SECTION = "start"

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
        "Dragonhoard is a game you play a few commands at a time. Each server has its own "
        "currency and economy.\n\n"
        "**Your first five minutes**\n"
        "1. `/mine place` - put a drill to work. Your first Iron Drill is free and already full.\n"
        "2. `/collect` - empty your drills into your inventory. They keep mining while you're away.\n"
        "3. `/market sell` - sell what you collected.\n"
        "4. Spend it: smelt ore in the `/furnace`, build and upgrade drills in the `/factory`."
    ),
    commands=(
        ManualCommand(
            "/help", "/help [topic]",
            "Open this manual, or jump straight to a topic.",
            aliases=("/manual", "/man"),
        ),
    ),
    tips=(
        "Only you see the bot's replies, unless an admin runs `/setup messages public`.",
    ),
))


_add(ManualSection(
    key="mining",
    label="Mining & Drills",
    emoji="⛏️",
    color=MINING_COLOR,
    summary="Place drills, let them run, collect what they dig up",
    body=(
        "Drills mine on their own from the moment you place them until they fill up. Better "
        "drills and higher levels mine faster; both come from the `/factory`."
    ),
    commands=(
        ManualCommand(
            "/mine place", "/mine place [drill] [container]",
            "Put a drill to work here, fitting a container if you name one.",
        ),
        ManualCommand(
            "/mine status", "/mine status",
            "Your drills here, the server's mining slots, and which gems are left to find.",
        ),
        ManualCommand(
            "/collect", "/collect [here]",
            "Empty your drills in every server. `here` limits it to this one.",
        ),
        ManualCommand(
            "/mine remove", "/mine remove <drill> [keep_container]",
            "Take a drill out, with what it mined. It keeps its level.",
        ),
        ManualCommand(
            "/mine attach", "/mine attach <drill> <container>",
            "Fit a container so a drill holds more. Any old one comes back to you.",
        ),
        ManualCommand(
            "/mine detach", "/mine detach <drill>",
            "Take a container off a drill. It costs nothing.",
        ),
        ManualCommand(
            "/focus", "/focus [focus]",
            "Turn every ore you mine into one kind. Unlock: 1 Ruby.",
        ),
        ManualCommand(
            "/efficiency", "/efficiency [efficiency]",
            "Mine double the ore one metal's recipe uses, mixed to waste less. Unlock: 1 Obsidian.",
        ),
        ManualCommand(
            "/affinity", "/affinity [affinity]",
            "Turn every gem you mine into one kind, at better than even. Unlock: 1 Diamond.",
        ),
    ),
    tips=(
        f"You start with {BASE_MINING_SLOTS} drill slots on each server. Machine fees and "
        "donations unlock more, for everyone here.",
        f"A drill holds {BASE_STORAGE_CAPACITY} items, then stops. Collect often, or fit a "
        "container.",
        "After unlocking `/focus`, `/efficiency` or `/affinity`, you can change it free once a "
        "day. Run one with no option to compare the choices.",
    ),
))


_add(ManualSection(
    key="furnace",
    label="Furnace",
    emoji="🔥",
    color=FURNACE_COLOR,
    summary="Smelt raw ore into usable metal",
    body=(
        "Smelts ore into the metals almost everything else is built from. Each server has one "
        "furnace, shared by everyone, and jobs take time. `/recipe furnace` lists what it makes."
    ),
    commands=(
        ManualCommand(
            "/furnace smelt", "/furnace smelt <material> <quantity>",
            "Queue ore to be smelted.",
        ),
        ManualCommand(
            "/furnace status", "/furnace status",
            "Level, queue, when each job finishes, and progress to the next level.",
            aliases=("/furnace queue",),
        ),
    ),
    tips=(
        "Materials and the fee are taken when you queue, not when the job finishes.",
        f"Every item smelted burns {FURNACE_COAL_COST_PER_UNIT} extra coal as fuel, so don't "
        "sell all your coal.",
        f"Smelting thousands of ore? The Blast Furnace does {BLAST_FURNACE_BATCH_SIZE} at a "
        "time, much faster (`/help blast`).",
    ),
))


_add(ManualSection(
    key="blast",
    label="Blast Furnace",
    emoji="♨️",
    color=BLAST_FURNACE_COLOR,
    summary=f"Smelt in batches of {BLAST_FURNACE_BATCH_SIZE}, for when the furnace can't keep up",
    body=(
        f"Runs the furnace's recipes {BLAST_FURNACE_BATCH_SIZE} at a time, much faster. The "
        "cost per bar is the same, so it isn't a discount."
    ),
    commands=(
        ManualCommand(
            "/blast smelt", "/blast smelt <material> <batches>",
            "Queue bulk smelting.",
        ),
        ManualCommand(
            "/blast status", "/blast status",
            "Level, queue, when it clears, and progress to the next level.",
            aliases=("/blast queue",),
        ),
    ),
    tips=(
        "Quantities, fees and queue limits all count batches, not items.",
        f"One Steel batch needs {_STEEL_BATCH['iron_ore']:,} Iron Ore and "
        f"{_STEEL_BATCH_COAL:,} Coal at once. There are no part batches.",
        "Otherwise it works like the furnace: shared, and paid for when you queue.",
    ),
))


_add(ManualSection(
    key="factory",
    label="Factory",
    emoji="🏭",
    color=FACTORY_COLOR,
    summary="Craft components, drills, containers and upgrades",
    body=(
        "Builds components, drills, containers and upgrade packs from metal. Like the furnace, "
        "it's paid for when you queue. `/recipe factory` lists everything it makes."
    ),
    commands=(
        ManualCommand(
            "/factory craft", "/factory craft <item> <quantity>",
            "Queue something to be built.",
        ),
        ManualCommand(
            "/factory upgrade", "/factory upgrade <drill>",
            "Queue a level-up for a drill. Run it to see what the next level costs.",
        ),
        ManualCommand(
            "/factory status", "/factory status",
            "Level, queue, when each job finishes, and progress to the next level.",
            aliases=("/factory queue",),
        ),
    ),
    tips=(
        "To upgrade a drill, `/mine remove` it first. It stays in the factory until the job "
        "finishes.",
        "Each level costs an Upgrade Pack plus the drill's own tier material, and the cost "
        "doubles every level.",
    ),
))


_add(ManualSection(
    key="press",
    label="Hydraulic Press",
    emoji="⚙️",
    color=PRESS_COLOR,
    summary="Crush bulk ore into guaranteed gemstones",
    body=(
        "Turns a huge pile of ordinary ore into a guaranteed gemstone, slowly. `/recipe press` "
        "lists each recipe and how many press-days it takes."
    ),
    commands=(
        ManualCommand(
            "/press craft", "/press craft <product> <quantity>",
            "Queue a gemstone to be pressed.",
        ),
        ManualCommand(
            "/press status", "/press status",
            "Level, current jobs, when each one lands, and progress to the next level.",
            aliases=("/press queue",),
        ),
    ),
    tips=(
        "A press does one press-day of work per day for each level it has, so jobs take real "
        "days.",
        "The fee is charged per press-day, which makes pressing the most expensive thing you "
        "can queue.",
    ),
))


_add(ManualSection(
    key="market",
    label="Market & Currency",
    emoji="💰",
    color=MARKET_COLOR,
    summary="Buy and sell with the server and with other players",
    body=(
        "`/market buy` and `/market sell` trade with players and the server, and take the best "
        "price for you. The server's prices are fixed, and buying back costs double what "
        "selling paid."
    ),
    commands=(
        ManualCommand(
            "/market sell", "/market sell <item> <quantity>",
            "Sell to the highest bidder, then the server. The list shows only what someone will buy.",
        ),
        ManualCommand(
            "/market buy", "/market buy <item> [quantity]",
            "Buy from the cheapest source: player listings, then the server.",
        ),
        ManualCommand(
            "/market list", "/market list <item> <price> [quantity]",
            "Put something up for sale at your price. It's held until sold or cancelled.",
        ),
        ManualCommand(
            "/market order", "/market order <item> <quantity> <price>",
            "Offer to buy something at your price. The money is held until it fills or you cancel.",
        ),
        ManualCommand(
            "/market entries", "/market entries",
            "Your own listings and orders, and the money your orders hold.",
        ),
        ManualCommand(
            "/market cancel", "/market cancel <entry>",
            "Take back a listing or order, and whatever it held.",
        ),
        ManualCommand(
            "/market status", "/market status",
            "Server prices and stock, plus the best player offers on each item.",
        ),
        ManualCommand(
            "/donate infrastructure", "/donate infrastructure <machine> <amount>",
            "Pay into a machine. Like a fee, it speeds the machine up and counts toward mining slots.",
        ),
        ManualCommand(
            "/donate player", "/donate player <member> <amount>",
            "Give currency to another member. No fee.",
        ),
    ),
    tips=(
        "Ores and smelted metals trade only with the server. Gemstones, components, containers "
        "and drills trade only between players. Exotic Matter can't be traded at all.",
        f"You can have {MAX_OPEN_LISTINGS} listings and {MAX_OPEN_ORDERS} orders per server, "
        f"each lasting {MARKET_ENTRY_LIFETIME_DAYS} days.",
        "Trades between players may carry the Market Tax, paid by the seller "
        "(`/help government`).",
    ),
))


_add(ManualSection(
    key="economy",
    label="Economy",
    emoji="📊",
    # The market's yellow: /economy reads the market's own ledger from another
    # angle rather than being a feature of its own (docs/stylization.md).
    color=MARKET_COLOR,
    summary="The server's wealth, what it produced, and what GDP means",
    body=(
        "`/economy status` is this server's economy on one page, and `/economy gdp` breaks "
        "down what it produced."
    ),
    commands=(
        ManualCommand(
            "/economy status", "/economy status",
            "Players' wealth, mining slot progress, this week's output, gems mined, queues and "
            "today's job.",
        ),
        ManualCommand(
            "/economy gdp", "/economy gdp",
            "Output over 24 hours and 7 days, which stage added it, and the import/export line.",
        ),
    ),
    tips=(
        "**GDP** is what this server produced, at market prices - not how much money it has. "
        "Each stage counts once, for the value it added, in the server where the work happened.",
        "A **net importer** smelted more ore than it mined; a **net exporter** mined more than "
        f"it smelted. Within {BALANCED_ORE_GAP:.0%} reads as **balanced**, with the gap: + means "
        "more was smelted.",
        "Gemstones are left out of GDP. `/economy status` counts them on their own.",
    ),
))


_add(ManualSection(
    key="jobboard",
    label="Job Board",
    emoji="📋",
    color=JOBBOARD_COLOR,
    summary="A daily task the server pays a bonus for",
    body=(
        "Each day the server posts one job: sell it a set amount of one material. Finish it and "
        "you earn a bonus about as big as the sale itself."
    ),
    commands=(
        ManualCommand(
            "/jobboard", "/jobboard",
            "Today's job, what it pays, and your progress.",
        ),
    ),
    tips=(
        "Sell through `/market sell` as usual. Progress adds up across sales.",
        "It pays every time you finish it: bring five times the amount and you're paid five "
        "times. Leftovers count toward the next one.",
        "It isn't a race - everyone can do it. A new job is posted at midnight Arizona time, "
        "and unfinished progress is lost.",
    ),
))


_add(ManualSection(
    key="inventory",
    label="Inventory & Balance",
    emoji="🎒",
    color=INVENTORY_COLOR,
    summary="See what you own and what you're worth",
    body="Your materials are yours in every server; your balance is separate in each.",
    commands=(
        ManualCommand(
            "/inventory", "/inventory",
            "Your balance, materials and spare drills. Placed drills are on `/mine status`.",
        ),
        ManualCommand(
            "/balance", "/balance",
            "Your balance in every server you play in.",
        ),
    ),
))


_add(ManualSection(
    key="recipes",
    label="Recipe Book",
    emoji="📜",
    color=RECIPE_COLOR,
    summary="Look up what anything costs to make",
    body="What everything costs to make.",
    commands=(
        ManualCommand(
            "/recipe factory", "/recipe factory",
            "Components, drills, upgrade packs, and containers with their capacity.",
        ),
        ManualCommand(
            "/recipe furnace", "/recipe furnace",
            "Furnace recipes, with the blast furnace's batch version of each.",
        ),
        ManualCommand(
            "/recipe press", "/recipe press",
            "Press recipes and how many press-days each takes.",
        ),
        ManualCommand(
            "/recipe scrapper", "/recipe scrapper",
            "What the scrapper gives back for each item.",
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
        "Breaks a component, container, upgrade pack or drill back into **half** of what it "
        "was made from, one step at a time. `/recipe scrapper` shows what each item returns."
    ),
    commands=(
        ManualCommand(
            "/scrapper scrap", "/scrapper scrap <item> <quantity>",
            "Queue components, containers or upgrade packs to be broken down.",
        ),
        ManualCommand(
            "/scrapper drill", "/scrapper drill <drill>",
            "Break down a drill. `/mine remove` it first; its container comes back to you.",
        ),
        ManualCommand(
            "/scrapper status", "/scrapper status",
            "Level, speed, fee, and what's waiting.",
            aliases=("/scrapper queue",),
        ),
    ),
    tips=(
        f"A drill returns its bit plus {_DRILL_SCRAP['iron']} iron and "
        f"{_DRILL_SCRAP['copper']} copper.",
        "**Scrapping a drill is permanent.** Iron and Steel levels are lost; a Ruby, Obsidian or "
        "Diamond Drill returns half the gems its levels cost, at least one.",
        "You never lose a gemstone: at least one of an item's most valuable part always comes "
        "back.",
    ),
))


_add(ManualSection(
    key="betting",
    label="Bets",
    emoji="🎲",
    color=BET_COLOR,
    summary="Bet the server's currency on what happens next",
    body=(
        "Open a bet on something happening, and others back it or bet against it. An admin "
        "settles it, and the winning side splits the whole pot by stake. There's no house cut."
    ),
    commands=(
        ManualCommand(
            "/bet open", "/bet open <prediction> <amount> <closes_in>",
            "Open a bet and stake that it happens. `closes_in` is in hours.",
        ),
        ManualCommand(
            "/bet place", "/bet place <bet> <side> <amount>",
            "Back a bet or take the other side. The bet's buttons do the same.",
        ),
        ManualCommand(
            "/bet status", "/bet status [bet]",
            "One bet's pools and odds, or every bet running here.",
        ),
        ManualCommand(
            "/bet close", "/bet close <bet>",
            "Stop taking wagers early. Its opener or Manage Server.",
        ),
        ManualCommand(
            "/bet resolve", "/bet resolve <bet> <outcome>",
            "Settle it and pay out. Manage Server.",
        ),
        ManualCommand(
            "/bet cancel", "/bet cancel <bet>",
            "Call it off and refund every stake. Manage Server.",
        ),
    ),
    tips=(
        "Your stake leaves your balance at once. You pick a side once: you can add to it, but "
        "not switch or back out.",
        "Odds change with every wager until the bet closes. Only your stake and side are "
        "locked in.",
        "If nobody backed the winning side, everyone gets their stake back.",
    ),
))


_add(ManualSection(
    key="government",
    label="Government",
    emoji="🏛️",
    color=GOVERNMENT_COLOR,
    summary="Elect a Mayor and a Treasurer, lend the server money, fund big projects",
    body=(
        "The server elects a **Mayor** and a **Treasurer** every week. The Treasurer sets fees "
        "and taxes; the Mayor spends the treasury."
    ),
    commands=(
        ManualCommand(
            "/government status", "/government status",
            "Who holds office, fees, rates, treasury, debt and project prices.",
        ),
        ManualCommand("/vote mayor", "/vote mayor <member>", "Vote for a Mayor."),
        ManualCommand("/vote treasurer", "/vote treasurer <member>", "Vote for a Treasurer."),
        ManualCommand(
            "/treasurer fee", "/treasurer fee <machine> <multiplier>",
            "Set a machine's fee as a multiple of its default.",
        ),
        ManualCommand(
            "/treasurer rate", "/treasurer rate <fee-share|market-tax> <percent>",
            "Set the Fee Share or the Market Tax.",
        ),
        ManualCommand(
            "/treasurer bondrate", "/treasurer bondrate <percent>",
            f"Set the premium new bonds repay, up to {MAX_BOND_RATE_PERCENT}%.",
        ),
        ManualCommand(
            "/mayor fund", "/mayor fund <machine> <amount>",
            "Pay treasury money into a machine's level.",
        ),
        ManualCommand(
            "/mayor enhance", "/mayor enhance <machine>",
            "Double one machine's speed with an Infrastructure Enhancement.",
        ),
        ManualCommand(
            "/mayor slots", "/mayor slots <amount>",
            f"Spend toward mining slots, each 1 counting as {MINING_SLOT_ENHANCEMENT_MULTIPLIER}.",
        ),
        ManualCommand(
            "/mayor bonanza", "/mayor bonanza",
            f"Start a Server Bonanza: {BONANZA_HOURS} hours of double-speed drills and machines.",
        ),
        ManualCommand(
            "/mayor bonds", "/mayor bonds <amount>",
            "Put bonds up for sale, or 0 to withdraw them.",
        ),
        ManualCommand(
            "/bonds buy", "/bonds buy <denomination>",
            "Lend the server money by buying a bond.",
        ),
        ManualCommand(
            "/bonds holdings", "/bonds holdings",
            "What the server owes you, and any bonds for sale.",
        ),
    ),
    tips=(
        f"**Voting** is Thursdays, Arizona time. You need a drill placed here for at least "
        f"{VOTER_DRILL_DAYS} days, and can't vote for yourself.",
        "**Fee Share**: the part of each machine fee the government keeps - your fee is the "
        "same either way. **Market Tax**: a cut of player trades, paid by the seller.",
        "**Bonds** repay their price plus the premium fixed when you buy, hourly from the "
        "government's revenue. If you leave, they freeze until you return.",
    ),
))


_add(ManualSection(
    key="setup",
    label="Server Setup",
    emoji="🛠️",
    color=DEFAULT_COLOR,
    summary="Admin settings — requires Manage Server",
    body=(
        "These need the Manage Server permission. Machine fees are set by the elected "
        "Treasurer, not admins (`/help government`)."
    ),
    commands=(
        ManualCommand(
            "/setup messages", "/setup messages <visibility>",
            "Show the bot's replies to everyone, or only to whoever ran the command (the default).",
        ),
        ManualCommand(
            "/setup currency", "/setup currency <name> <emoji>",
            "Name this server's currency and pick its emoji.",
        ),
        ManualCommand(
            "/setup channel", "/setup channel [channel]",
            "Keep the bot to one channel and its threads. Leave it blank to allow every channel.",
        ),
        ManualCommand(
            "/setup max_queue", "/setup max_queue <infrastructure> <amount>",
            "Cap how much one player can queue on a machine. The cap grows with the machine's level.",
        ),
    ),
    tips=(
        "`/setup` and `/help` work in every channel, so `/setup channel` can't lock you out.",
    ),
))


_add(ManualSection(
    key="extras",
    label="Extras",
    emoji="🪿",
    color=DEFAULT_COLOR,
    summary="Commands that aren't part of the game at all",
    body="Just for fun. Nothing here costs or earns anything.",
    commands=(
        ManualCommand(
            "/honk", "/honk",
            "Honk. Press play on the clip.",
        ),
        ManualCommand(
            "/changelog", "/changelog [version]",
            "What changed in each version, newest first.",
        ),
    ),
))


def command_line(command: ManualCommand) -> str:
    """One command as the manual shows it: its usage, any aliases, then what
    it does - all on one line, split with the ` · ` docs/stylization.md uses
    inside a line."""
    names = f"`{command.usage}`"
    if command.aliases:
        names += " (or " + ", ".join(f"`{alias}`" for alias in command.aliases) + ")"
    return f"{names} · {command.description}"


def build_section_embed(section: ManualSection) -> discord.Embed:
    """Renders one manual page. Lines go through add_multi_field so a section
    that outgrows Discord's 1024-character field limit spills into a
    continuation field instead of being silently truncated."""
    embed = make_embed(f"{section.emoji} {section.label}", section.color, description=section.body)
    if section.commands:
        add_multi_field(embed, "Commands", [command_line(cmd) for cmd in section.commands])
    if section.tips:
        add_multi_field(embed, "Good to know", [f"- {tip}" for tip in section.tips])
    return embed
