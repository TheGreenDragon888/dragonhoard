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
    FACTORY_COLOR,
    RECIPE_COLOR,
    PRESS_COLOR,
)

from data.materials import (
    BASE_STORAGE_CAPACITY,
    FURNACE_COAL_COST_PER_UNIT,
    MAX_DRILLS_PER_USER_PER_SERVER,
)


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
        "Dragon Assistant is a game you play a few commands at a time. This server has its "
        "own currency and its own economy, and everything you own here is yours alone.\n\n"
        "**Your first five minutes**\n"
        "1. `/mine place` - puts a drill in the ground. If you don't own one yet you'll be "
        "given a free Iron Drill.\n"
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
            "**Mining** produces raw materials. The **Furnace** smelts them into metals, the "
            "**Factory** turns metals into components, drills and gear, and the **Hydraulic "
            "Press** crushes huge piles of ore into gemstones. The **Market** is where all of "
            "it turns into money.",
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
        "The whole server shares one pool of raw material, which tops up every day and can "
        "bank a few days' worth if nobody is drawing on it. There's no dedicated mining "
        "channel - drills belong to the server, not to a room in it.\n\n"
        f"You can have up to **{MAX_DRILLS_PER_USER_PER_SERVER} drills** placed in any one "
        f"server. Every drill holds **{BASE_STORAGE_CAPACITY}** items by itself, and once it "
        "fills up it stops and waits for you - so a bigger container, or simply collecting "
        "more often, is the difference between a drill that works all day and one that "
        "spends the afternoon idle.\n\n"
        "`/collect` reaches every server you have drills in, so one call from anywhere "
        "empties the lot. Your materials are yours wherever you earned them - only the "
        "drills, the pool and the currency belong to a particular server.\n\n"
        "Better drill types mine faster, and every level you add to a drill makes it faster "
        "still. Drills are crafted and upgraded in the Factory."
    ),
    commands=(
        ManualCommand(
            "/mine place", "/mine place [drill]",
            "Puts one of your drills to work in this server. Leave the drill blank if you "
            "only have one spare. If you own no drills at all, you'll be given a free Iron "
            "Drill to start with.",
        ),
        ManualCommand(
            "/mine status", "/mine status",
            "Shows every drill you have here - its level, how full it is, what it's fitted "
            "with - plus how much raw material the server pool has left.",
        ),
        ManualCommand(
            "/collect", "/collect [here]",
            "Empties every drill you have placed - in every server, not just this one - into "
            "your inventory at once, and tells you what came from where. This is the command "
            "you'll run most. Set `here` to True to collect only from your drills in this "
            "server.",
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
    ),
    notes=(
        (
            "Gemstones",
            "Rubies, obsidian and diamonds come out of the ground on their own, but rarely "
            "enough that you shouldn't count on them. If you want a gem on a schedule rather "
            "than by luck, that's what the Hydraulic Press is for.",
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
        "The furnace levels up as the server pays fees into it, and a higher level smelts "
        "more per hour for everyone."
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
        "late ones are a project.\n\n"
        "The factory levels up on the fees paid into it and crafts faster at higher levels."
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
        ("Recipes", "`/recipe factory` lists every factory recipe, including container capacities."),
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
        "takes a level 1 press nine real days and a level 3 press three. Queue it and forget "
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
        "The server itself is who you trade with. It keeps its own warehouse of materials, "
        "buys from you, and sells back to you at a markup.\n\n"
        "**Selling is the only way currency comes into existence.** There's no payout for "
        "chatting and no daily handout - if you want money, you mine and you sell. Money "
        "leaves again through the fees you pay to the furnace, factory and press, and when "
        "you buy materials back off the server.\n\n"
        "Prices move with what the server is holding. The less of something it has, the more "
        "it will pay you for it; as its shelves fill up, the price it offers drops. So a "
        "material nobody has bothered mining is usually the one worth mining."
    ),
    commands=(
        ManualCommand(
            "/market sell", "/market sell <material> <quantity>",
            "Sells raw or smelted materials from your inventory to the server. This is how "
            "you earn.",
        ),
        ManualCommand(
            "/market buy", "/market buy <material> <quantity>",
            "Buys materials back out of the server's own stock. You can only buy what it "
            "actually has on hand.",
        ),
        ManualCommand(
            "/market status", "/market status",
            "Shows what the server will pay, what it charges, and how much of each material "
            "it's holding. Check it before a big sale.",
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
            "Everything you own - your balance, your materials grouped by kind, and every "
            "drill you have, placed or not.",
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
            "Every factory recipe: components, drill bits, drills, storage containers with "
            "the capacity each one adds, and upgrade materials.",
        ),
        ManualCommand(
            "/recipe furnace", "/recipe furnace",
            "Every furnace recipe and the raw materials it takes.",
        ),
        ManualCommand(
            "/recipe press", "/recipe press",
            "Every press recipe, with how many press-days each one occupies the press for.",
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
        "admins chose, and the fees and queue limits on its machines are set locally too. "
        "Machines level up on the fees they collect, so a server that charges nothing has "
        "machines that never improve, and one that charges too much prices its players out - "
        "the fee is the main dial an admin has.\n\n"
        "Replies from the bot are private by default so it stays out of the way. A server "
        "with a dedicated bot channel may prefer to make them public."
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
            "/setup fee", "/setup fee <infrastructure> <amount>",
            "Sets what the furnace, factory or press charges. Per item produced - except the "
            "press, which charges per press-day.",
        ),
        ManualCommand(
            "/setup max_queue", "/setup max_queue <infrastructure> <amount>",
            "Limits how many items one player can have queued on a machine at a time, so no "
            "one person can tie it up.",
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
