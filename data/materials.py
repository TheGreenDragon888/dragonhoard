"""
data/materials.py

Static definitions for every material, recipe, and drill in the game. This
is plain Python data (dicts), not database rows, because it never changes
at runtime - it's balance data you'll tune by editing this file and
restarting the bot, not something users modify.
"""
import random

from data.emoji import custom_emoji

# Raw materials and their drop chance when a drill pulls one item from its
# server's mining pool. Chances are expressed as fractions of 1.0 and
# should sum to 1.0. market_ceiling_price is the most the server market will
# ever pay to acquire one unit (see cogs/economy.py and docs/market.md) -
# denominated in the buying server's own currency, not DragonCoin.
# iron_ore/copper_ore/coal were rebalanced together: coal's drop_chance is
# +50% over its original 0.0999, with iron_ore and copper_ore absorbing that
# increase proportionally (their original 2:1 ratio is preserved). Each of
# the three's market_ceiling_price was then rescaled so ceiling_price *
# drop_chance stays exactly what it was before the rebalance - the expected
# currency value of mining one item of that material is unchanged, only its
# frequency and per-unit price shifted. Gemstone rates/prices are untouched.
RAW_MATERIALS = {
    "iron_ore":    {"name": "Iron Ore",    "emoji": custom_emoji("IronOre", 1523432328028885034, 1533714268560691281),    "drop_chance": 0.5667,   "market_ceiling_price": 0.010588},
    "copper_ore":  {"name": "Copper Ore",  "emoji": custom_emoji("CopperOre", 1523432342813933699, 1533714267478560818),  "drop_chance": 0.28335,  "market_ceiling_price": 0.017681},
    "coal":        {"name": "Coal",        "emoji": custom_emoji("Coal", 1523432352318099456, 1533714266551484516),       "drop_chance": 0.14985,  "market_ceiling_price": 0.033333},
    "ruby":        {"name": "Ruby",        "emoji": custom_emoji("Ruby", 1532897325238980680, 1533714310134497404),       "drop_chance": 0.00009,  "market_ceiling_price": 5500.00},
    "obsidian":    {"name": "Obsidian",    "emoji": custom_emoji("Obsidian", 1532899466687021268, 1533714309119737946),   "drop_chance": 0.000009, "market_ceiling_price": 52500.00},
    "diamond":     {"name": "Diamond",     "emoji": custom_emoji("Diamond", 1523433355708858612, 1533714307911778446),    "drop_chance": 0.000001, "market_ceiling_price": 500000.00},
}

# Smelted materials: produced by the furnace from raw materials.
# "inputs" maps material_id -> quantity required to produce ONE output unit.
# Balance rule: each ceiling price is 150% of the combined ceiling price of
# its recipe's raw inputs (that raw total is the trailing comment per line).
SMELTED_MATERIALS = {
    "iron":   {"name": "Iron",   "emoji": custom_emoji("Iron", 1523433412805918820, 1533714352232988722),   "inputs": {"iron_ore": 10},            "market_ceiling_price": 0.15882},   # raw: 0.10588
    "copper": {"name": "Copper", "emoji": custom_emoji("Copper", 1523433425220927498, 1533714350416728167), "inputs": {"copper_ore": 10},          "market_ceiling_price": 0.265215},  # raw: 0.17681
    "steel":  {"name": "Steel",  "emoji": custom_emoji("Steel", 1523433463150149692, 1533714353428107264),  "inputs": {"iron_ore": 20, "coal": 4}, "market_ceiling_price": 0.517638},  # raw: 0.345092
}

# Component materials: produced by the factory from smelted materials.
COMPONENT_MATERIALS = {
    "wiring":              {"name": "Wiring",              "emoji": custom_emoji("Wiring", 1523433594004049971, 1533714399456526356),        "inputs": {"copper": 12}},
    "drill_chassis":       {"name": "Drill Chassis",        "emoji": custom_emoji("DrillChassis", 1523433620566446150, 1533714400639320084),  "inputs": {"iron": 20, "copper": 12}},
    "iron_drill_bit":      {"name": "Iron Drill Bit",       "emoji": custom_emoji("IronDrillBit", 1523433731799519403, 1533714519258562670),  "inputs": {"iron": 20}},
    "steel_drill_bit":     {"name": "Steel Drill Bit",      "emoji": custom_emoji("SteelDrillBit", 1523433738950807592, 1533714522500497541), "inputs": {"steel": 20}},
    "ruby_drill_bit":      {"name": "Ruby Drill Bit",       "emoji": custom_emoji("RubyDrillBit", 1523433749893742752, 1533714521523359804),  "inputs": {"steel": 10, "ruby": 3}},
    "obsidian_drill_bit":  {"name": "Obsidian Drill Bit",   "emoji": custom_emoji("ObsidianDrillBit", 1523433758139748372, 1533714520340566187), "inputs": {"steel": 10, "obsidian": 3}},
    "diamond_drill_bit":   {"name": "Diamond Drill Bit",    "emoji": custom_emoji("DiamondDrillBit", 1523433768076050551, 1533714517932900432), "inputs": {"steel": 10, "diamond": 3}},
}

# Drills: also crafted in the factory, from a chassis + wiring + a bit type.
# A drill's type fixes only its base mining rate - storage comes from
# BASE_STORAGE_CAPACITY plus whatever container is attached, and the rate
# grows with the drill's level. There is deliberately no per-type
# storage_capacity here: every drill type has the same base, and a per-type
# field that happens to hold the same number five times invites someone to
# "fix" it back into a per-tier ladder.
DRILLS = {
    "iron_drill": {
        "name": "Iron Drill", "emoji": custom_emoji("IronMiningDrill", 1523433637398450347, 1533714428627910726),
        "inputs": {"wiring": 1, "drill_chassis": 1, "iron_drill_bit": 1},
        "mines_per_hour": 5,
    },
    "steel_drill": {
        "name": "Steel Drill", "emoji": custom_emoji("SteelMiningDrill", 1523433646613069824, 1533714431739953192),
        "inputs": {"wiring": 1, "drill_chassis": 1, "steel_drill_bit": 1},
        "mines_per_hour": 7.5,
    },
    "ruby_drill": {
        "name": "Ruby Drill", "emoji": custom_emoji("RubyMiningDrill", 1523433666800517262, 1533714430704222288),
        "inputs": {"wiring": 1, "drill_chassis": 1, "ruby_drill_bit": 1},
        "mines_per_hour": 10,
    },
    "obsidian_drill": {
        "name": "Obsidian Drill", "emoji": custom_emoji("ObsidianMiningDrill", 1523433678825459893, 1533714429655646358),
        "inputs": {"wiring": 1, "drill_chassis": 1, "obsidian_drill_bit": 1},
        "mines_per_hour": 12.5,
    },
    "diamond_drill": {
        "name": "Diamond Drill", "emoji": custom_emoji("DiamondMiningDrill", 1523433688656908408, 1533714427679997952),
        "inputs": {"wiring": 1, "drill_chassis": 1, "diamond_drill_bit": 1},
        "mines_per_hour": 15,
    },
}

# Consumed by /factory upgrade to raise a drill's level. An ordinary fungible
# item in user_materials, unlike the drills themselves.
UPGRADE_MATERIALS = {
    "drill_upgrade_pack": {"name": "Drill Upgrade Pack", "emoji": custom_emoji("DrillUpgradePack", 1533713579948114090, 1533714744157016134), "inputs": {"copper": 5}},
}

# Storage containers: attach one to a drill (/mine attach) for an ADDITIVE
# bonus on top of BASE_STORAGE_CAPACITY. At most one per drill, freely
# swappable, and any container fits any drill - the tier names describe the
# container's own cost and bonus, not which drill it fits.
STORAGE_CONTAINERS = {
    "iron_container":     {"name": "Iron Container",     "emoji": custom_emoji("IronContainer", 1533713574977994793, 1533714646551363684), "inputs": {"iron": 10, "copper": 5},      "storage_bonus": 150},
    "steel_container":    {"name": "Steel Container",    "emoji": custom_emoji("SteelContainer", 1533713578811461642, 1533714649990824036), "inputs": {"steel": 10, "copper": 10},    "storage_bonus": 200},
    "ruby_container":     {"name": "Ruby Container",     "emoji": custom_emoji("RubyContainer", 1533713577607954542, 1533714648736469033), "inputs": {"ruby": 1, "copper": 20},      "storage_bonus": 300},
    "obsidian_container": {"name": "Obsidian Container", "emoji": custom_emoji("ObsidianContainer", 1533713576261587107, 1533714647687893093), "inputs": {"obsidian": 1, "copper": 40},  "storage_bonus": 400},
    "diamond_container":  {"name": "Diamond Container",  "emoji": custom_emoji("DiamondContainer", 1533713574076219502, 1533714644965916805), "inputs": {"diamond": 1, "copper": 80},   "storage_bonus": 500},
}

# Made only by the hydraulic press. Deliberately has no market_ceiling_price:
# like drills and components it's a finished good, and docs/market.md section 3
# keeps those out of the market entirely. Nothing consumes it yet - it's
# reserved for a later feature, so it accumulates rather than being spent.
#
# TODO(emoji): the only item left with no icon designed at all yet, on
# either Discord application - hence the plain unicode placeholder. Once
# one's designed, replace it with custom_emoji("Name", live_id, beta_id)
# (see data/emoji.py); it'll need uploading to BOTH the live and beta
# Dragonhoard applications, since they don't share emoji, so get both ids
# before filling this in. Nothing else needs to change, since every display
# path reads the emoji through get_material_info().
PRESS_MATERIALS = {
    "ultra_dense_matter": {"name": "Ultra Dense Matter", "emoji": custom_emoji("UltraDenseMatter", 1533722773418016868, 1533722431024529408)},
}


def _mining_equivalent(gem_id: str, material_id: str) -> float:
    """How much of a smelted material a player receives, on average, over the
    stretch of mining it takes to turn up one of that gemstone.

    Whichever of the recipe's raw inputs is scarcest relative to how much the
    recipe needs is what limits how much can actually be smelted - for steel
    that's iron ore, not coal.

    Exact, and verified by simulation: the expected number of units of a
    material mined before the first gem works out to simply its drop chance
    divided by the gem's."""
    items_per_gem = 1.0 / RAW_MATERIALS[gem_id]["drop_chance"]
    return min(
        items_per_gem * RAW_MATERIALS[raw_id]["drop_chance"] / per_unit
        for raw_id, per_unit in SMELTED_MATERIALS[material_id]["inputs"].items()
    )


# What a pressed ruby costs, and through it every other press recipe. The exact
# mining-equivalent for a ruby is 629.67 Iron; the press charges a round 600.
#
# This is the one number to turn to retune the press. Every other recipe is
# scaled from it by the exact ratio between their mining-equivalents, so the
# ~4.7% discount it represents applies identically to all of them. That
# uniformity is the point: those ratios are what keep the recipes worth the
# same as each other per unit of mining effort, and discounting one gem more
# than another would quietly make it the only one worth pressing.
PRESS_COST_BASE = 600


def _press_cost(gem_id: str, material_id: str) -> int:
    """A recipe's cost: PRESS_COST_BASE, scaled by how this gem's
    mining-equivalent compares to a ruby's.

    Those ratios come out to exactly 1 : 5 : 45 for ruby, obsidian and diamond
    - gem rarities are 90:9:1, copper ore drops at half iron ore's rate, and
    steel takes 20 ore per unit against iron's 10. Deriving them rather than
    writing the numbers in keeps that true if drop chances are ever retuned."""
    ratio = _mining_equivalent(gem_id, material_id) / _mining_equivalent("ruby", "iron")
    return round(PRESS_COST_BASE * ratio)


# The hydraulic press: turns bulk smelted material into gemstones, trading a
# lot of ore for the certainty of a gem instead of the one-in-a-million chance
# of mining one.
#
# Each recipe costs a little under what a player would have received alongside
# that gem on average before finding one - the stretch between two rubies
# yields about 6,297 iron ore, which smelts into 630 Iron, and a pressed ruby
# costs 600. That near-equivalence is the whole design: the press isn't a
# source of gems so much as a way to trade the variance of mining for
# certainty, with a small fixed edge for having built the thing.
#
# press_days is what a recipe costs in press time, measured against a ruby. A
# press produces press_rate_per_day(level) ruby-equivalents each day, so a
# level 1 press spends nine days on a diamond and a level 3 press spends three.
# Ultra dense matter is priced in diamonds rather than ore: at 27x a ruby's
# press time it sits one full tier beyond diamond, and costing it in ore would
# put it out of reach of any server that will ever exist.
PRESS_RECIPES = {
    "ruby":     {"inputs": {"iron":   _press_cost("ruby", "iron")},       "press_days": 1},
    "obsidian": {"inputs": {"copper": _press_cost("obsidian", "copper")}, "press_days": 3},
    "diamond":  {"inputs": {"steel":  _press_cost("diamond", "steel")},   "press_days": 9},
    "ultra_dense_matter": {"inputs": {"diamond": 10}, "press_days": 27},
}

# Ultra dense matter is the only press output that isn't already a material in
# its own right, so it carries its recipe on its entry too - that's what
# raw_input_cost and the /inventory ordering read.
PRESS_MATERIALS["ultra_dense_matter"]["inputs"] = PRESS_RECIPES["ultra_dense_matter"]["inputs"]

# Infrastructure throughput, per level. Furnace, factory and scrapper are in
# items per hour; the press is in ruby-equivalents per day (see PRESS_RECIPES).
# All four scale linearly and have no maximum level - the cost of the next
# upgrade is the only ceiling.
FURNACE_RATE_PER_LEVEL = 5
FACTORY_RATE_PER_LEVEL = 1
PRESS_RATE_PER_LEVEL = 1
# Twice the factory's, because the scrapper undoes exactly what the factory
# makes and pulling something apart is quicker than assembling it. The 50%
# material loss (see scrap_yield) is already what the operation costs; a
# machine that was both the slowest in the game and the least rewarding would
# simply never be used.
SCRAPPER_RATE_PER_LEVEL = 2

# Fee total (in server currency) a server must have collected from a machine
# to take it to the next level. Every level costs ten times the last, so the
# ladder is 5, 50, 500, 5000... and climbs out of reach on its own rather than
# stopping at a hard cap.
UPGRADE_THRESHOLD_BASE = 5.00
UPGRADE_THRESHOLD_STEP = 10


def furnace_rate(level: int) -> int:
    """Smelted items per hour at this furnace level."""
    return FURNACE_RATE_PER_LEVEL * level


def factory_rate(level: int) -> int:
    """Crafted items per hour at this factory level."""
    return FACTORY_RATE_PER_LEVEL * level


def press_rate_per_day(level: int) -> int:
    """Ruby-equivalents per day at this press level. A recipe's press_days is
    what it costs against this budget, so a level 3 press gets through one
    diamond (9 press-days) in three days."""
    return PRESS_RATE_PER_LEVEL * level


def scrapper_rate(level: int) -> int:
    """Items recycled per hour at this scrapper level."""
    return SCRAPPER_RATE_PER_LEVEL * level


def upgrade_threshold(level: int) -> float:
    """Fees a machine must have collected to reach `level`. Levels are
    unbounded, so this always returns a number - there is no "max level"."""
    return UPGRADE_THRESHOLD_BASE * UPGRADE_THRESHOLD_STEP ** (level - 2)


def effective_max_queue(base: int, level: int) -> int:
    """The per-user queue cap a machine actually enforces: the base a server
    manager sets with /setup max_queue, multiplied by the machine's level.

    Every machine's throughput is linear in its level (furnace_rate and its
    siblings above), so its queue grows by the same factor. Without this a
    server that has taken its furnace from 5 items/hour to 50 is still hitting
    the same ceiling it had on day one, and the cap stops describing "how much
    work you may have outstanding" and starts describing "how long you must
    wait between commands".

    The level floor is defensive: server_config.<machine>_level is NOT NULL
    DEFAULT 1 and nothing decrements it, but a zero here would silently make
    the machine unusable rather than merely stingy."""
    return base * max(1, level)

FURNACE_COAL_COST_PER_UNIT = 1  # extra coal burned per item smelted, on top of the recipe's own inputs
MAX_DRILLS_PER_USER_PER_SERVER = 3

# Every drill type starts here; a container adds its storage_bonus on top.
BASE_STORAGE_CAPACITY = 100

# Every drill starts at level 1 with its type's base mines_per_hour, and each
# level above that adds a fixed FRACTION of that base rather than a flat amount.
# There is no maximum level - the doubling upgrade cost below is the only brake.
#
# The fraction is one fifth, and this number is where that comes from: the Iron
# Drill's ladder has always been 5 -> 6 -> 7 -> 8, which is +1 per level on a
# base of 5. Reading that as "a level is worth a fifth of the drill's OWN base"
# rather than "a level is worth +1" is what makes the same ladder mean the same
# thing at every tier. Under the old flat +1 a level made an Iron Drill 20%
# faster and a Diamond Drill 6.7% faster, so the upgrade path got worse the
# better your drill was - exactly backwards.
#
# The player-facing diminishing returns fall out of this on their own, with no
# second mechanism: each level adds a constant amount to a growing total, so the
# gain reads as +1/5, +1/6, +1/7 ... (20%, 16.7%, 14.3%) as the levels climb.
LEVEL_RATE_ANCHOR = 5

# What one level-up consumes, on top of the upgrade packs. Ore-tier drills pay
# in their smelted material; gem-tier drills pay in 3 of their gem, matching
# the 3 gems their drill bit already costs - 10 of a gem whose drop chance is
# one in a million would put those drills permanently out of reach.
_UPGRADE_TIER_MATERIAL = {
    "iron_drill":     {"iron": 10},
    "steel_drill":    {"steel": 10},
    "ruby_drill":     {"ruby": 3},
    "obsidian_drill": {"obsidian": 3},
    "diamond_drill":  {"diamond": 3},
}

# Not a material - the production_jobs.target_id sentinel marking a factory job
# as a drill level-up rather than a craft. Deliberately absent from
# ALL_MATERIALS: it's a job kind, not an item, and registering it would leak it
# into the factory's craftable list and the recipe book.
DRILL_UPGRADE_JOB_TARGET = "drill_upgrade"

# The same idea for the scrapper: a scrapper job whose target_id is this is
# breaking down the drill named by target_drill_id, rather than a stack of some
# material. Also deliberately absent from ALL_MATERIALS.
DRILL_SCRAP_JOB_TARGET = "drill_scrap"

# The server-wide raw material pool: how much is added per member each day,
# and how many days' worth (at that daily rate) it can bank up to before the
# top-up stops growing it further.
MINING_POOL_DAILY_PER_MEMBER = 200
MINING_POOL_CAP_MULTIPLIER = 3

# The server market's per-material "target stock" - the equilibrium point its
# buy-price curve is built around - scales with server size: target_stock =
# member_count * MATERIAL_TARGET_STOCK_PER_MEMBER[material_id]. The server
# pays a material's full market_ceiling_price at zero stock, half price at
# target stock, and progressively less (but never nothing) beyond it - target
# stock is not a maximum. See EconomyCog._buy_price in cogs/economy.py.
# member_count here should already exclude bots (see utils/guild_helpers.py)
# - the target stock is meant to reflect the server's actual player base.

# Each raw material's per-member target stock is anchored to iron_ore = 100
# and scaled from there by its ORIGINAL (pre coal-rebalance) drop chance -
# deliberately not the live drop_chance values above, so future rate tuning
# doesn't also reshuffle every material's market equilibrium. This is why
# iron_ore -> 100 and copper_ore (originally half iron_ore's chance) -> 50.
_BASELINE_RAW_DROP_CHANCE = {
    "iron_ore": 0.60,
    "copper_ore": 0.30,
    "coal": 0.0999,
    "ruby": 0.00009,
    "obsidian": 0.000009,
    "diamond": 0.000001,
}
_IRON_ORE_TARGET_STOCK_PER_MEMBER = 100

MATERIAL_TARGET_STOCK_PER_MEMBER = {
    material_id: chance / _BASELINE_RAW_DROP_CHANCE["iron_ore"] * _IRON_ORE_TARGET_STOCK_PER_MEMBER
    for material_id, chance in _BASELINE_RAW_DROP_CHANCE.items()
}
# Smelted materials have no drop chance of their own, so their target stock
# instead derives from whichever raw input constrains it most tightly -
# target_stock(input) / quantity needed per unit - so the server is never
# aiming to hold more smelted stock than its raw-input target could
# realistically feed.
MATERIAL_TARGET_STOCK_PER_MEMBER["iron"] = (
    MATERIAL_TARGET_STOCK_PER_MEMBER["iron_ore"] / SMELTED_MATERIALS["iron"]["inputs"]["iron_ore"]
)
MATERIAL_TARGET_STOCK_PER_MEMBER["copper"] = (
    MATERIAL_TARGET_STOCK_PER_MEMBER["copper_ore"] / SMELTED_MATERIALS["copper"]["inputs"]["copper_ore"]
)
MATERIAL_TARGET_STOCK_PER_MEMBER["steel"] = min(
    MATERIAL_TARGET_STOCK_PER_MEMBER["iron_ore"] / SMELTED_MATERIALS["steel"]["inputs"]["iron_ore"],
    MATERIAL_TARGET_STOCK_PER_MEMBER["coal"] / SMELTED_MATERIALS["steel"]["inputs"]["coal"],
)


def target_stock(member_count: int, material_id: str) -> int:
    """The equilibrium stock level (docs/market.md section 3) a server's
    market pricing curve is built around for `material_id` - the point
    where the server's buy price is half that material's ceiling price.
    `member_count` should already be bot-excluded (utils/guild_helpers.py:
    human_member_count)."""
    return max(1, round(member_count * MATERIAL_TARGET_STOCK_PER_MEMBER[material_id]))


_MATERIAL_TABLES = (
    RAW_MATERIALS, SMELTED_MATERIALS, COMPONENT_MATERIALS,
    DRILLS, UPGRADE_MATERIALS, STORAGE_CONTAINERS, PRESS_MATERIALS,
)

# Flattened once at import, so get_material_info is a plain dict lookup and a
# new tier can't be forgotten here. Forgetting one used to fail silently and
# far away: receipts, /factory status, /inventory and the recipe book all
# render "❓" for an unknown id, and raw_input_cost quietly returns 0.0, which
# would sort the missing items first in every /inventory category.
ALL_MATERIALS: dict[str, dict] = {}
for _table in _MATERIAL_TABLES:
    _duplicates = ALL_MATERIALS.keys() & _table.keys()
    assert not _duplicates, f"duplicate material ids across tiers: {_duplicates}"
    ALL_MATERIALS.update(_table)


def get_material_info(material_id: str) -> dict | None:
    """Looks up a material regardless of which tier (raw/smelted/component/
    drill/container) it belongs to. Returns None if the ID doesn't exist."""
    return ALL_MATERIALS.get(material_id)


def effective_capacity(container_type: str | None) -> int:
    """How many raw materials a drill can hold: its flat base, plus the bonus
    of whatever container is attached. Any container fits any drill."""
    if container_type is None:
        return BASE_STORAGE_CAPACITY
    return BASE_STORAGE_CAPACITY + STORAGE_CONTAINERS[container_type]["storage_bonus"]


def effective_rate(drill_type: str, level: int) -> float:
    """A drill's mining rate in items per hour at the given level - its base
    rate scaled by LEVEL_RATE_ANCHOR's ladder, so every type gains the same
    proportion per level that the Iron Drill always has.

    Written as one rational expression rather than `base * (1 + 0.2 * (level -
    1))` because 0.2 has no exact binary representation: the multiply-then-add
    form makes a level 2 Steel Drill 9.000000000000002, and that figure ends up
    in an embed and in harvest arithmetic. Dividing by the anchor last keeps
    every rate here exact."""
    base = DRILLS[drill_type]["mines_per_hour"]
    return base * (LEVEL_RATE_ANCHOR + level - 1) / LEVEL_RATE_ANCHOR


def upgrade_cost(drill_type: str, level: int) -> dict[str, int]:
    """What it costs to take a drill from `level` to the next one. Every part
    of the recipe doubles per level, so each upgrade costs as much as all the
    previous ones put together."""
    multiplier = 2 ** (level - 1)
    cost = {"drill_upgrade_pack": multiplier}
    for material_id, quantity in _UPGRADE_TIER_MATERIAL[drill_type].items():
        cost[material_id] = quantity * multiplier
    return cost


def advance_harvest(progress: float, rate_per_hour: float, ticks_per_hour: float) -> tuple[int, float]:
    """Splits a tick's worth of mining into whole items now and a fraction to
    carry into the next tick.

    The carry is what makes a level worth exactly its stated rate. At 2.5
    ticks/hour an iron drill's level is +0.4 items/tick, so rounding each tick
    in isolation would throw the bonus away entirely - a level 2 iron drill
    mines 2.4/tick, which rounds to the same 2 as level 1. Since a level is now
    a fraction of the drill's own base rate, hardly any drill lands on a whole
    number of items per tick and the carry matters at every tier.
    The tiny nudge stops accumulated float error from turning a carry that
    should be exactly 1.0 into 0.999... and losing an item to truncation; the
    clamp then keeps the returned carry a genuine fraction, since that same
    nudge can round up past the true total and leave it a hair below zero."""
    total = progress + rate_per_hour / ticks_per_hour
    whole = int(total + 1e-9)
    return whole, max(0.0, total - whole)


# Gemstones drop from mining like the ores do, so they live in RAW_MATERIALS,
# but they're displayed as their own group. Drill bits likewise sit in
# COMPONENT_MATERIALS next to the true components.
GEMSTONES = tuple(m for m in RAW_MATERIALS if "drop_chance" in RAW_MATERIALS[m] and m not in ("iron_ore", "copper_ore", "coal"))
ORES = tuple(m for m in RAW_MATERIALS if m not in GEMSTONES)
DRILL_BITS = tuple(m for m in COMPONENT_MATERIALS if m.endswith("_drill_bit"))
DRILL_COMPONENTS = tuple(m for m in COMPONENT_MATERIALS if m not in DRILL_BITS)


def raw_input_cost(material_id: str) -> float:
    """What one unit ultimately costs in raw materials, following recipes all
    the way down and totalling the raw inputs' ceiling prices. Crafted items
    have no drop chance, so this stands in as "how hard it is to obtain" when
    ordering them for display."""
    if material_id in RAW_MATERIALS:
        return RAW_MATERIALS[material_id]["market_ceiling_price"]
    info = get_material_info(material_id)
    if info is None:
        return 0.0
    return sum(raw_input_cost(input_id) * qty for input_id, qty in info.get("inputs", {}).items())


def _by_drop_chance(material_ids) -> list[str]:
    return sorted(material_ids, key=lambda m: RAW_MATERIALS[m]["drop_chance"], reverse=True)


def _by_cost(material_ids) -> list[str]:
    return sorted(material_ids, key=raw_input_cost)


def roll_raw_material(rng=random) -> str:
    """Picks one raw material at random, weighted by drop chance - one item's
    worth of a drill's harvest.

    A pure function of RAW_MATERIALS, so the mining loop's drop distribution can
    be tested without a bot or a database. `rng` is injectable for exactly that.
    """
    roll = rng.random()
    cumulative = 0.0
    for material_id, info in RAW_MATERIALS.items():
        cumulative += info["drop_chance"]
        if roll <= cumulative:
            return material_id
    # RAW_MATERIALS' drop_chance values sum to exactly 1.0, so this only
    # triggers on float rounding at the roll==1.0 edge - falls back to the
    # first (most common) material rather than ever returning nothing.
    return next(iter(RAW_MATERIALS))


# What fraction of a recipe the scrapper hands back (see scrap_yield).
SCRAP_RETURN_RATE = 0.5


def scrap_yield(material_id: str) -> dict[str, int]:
    """What the scrapper returns for one unit of `material_id`: half of its
    recipe's DIRECT inputs, rounded down, and never less than one of the
    recipe's single most valuable input. Returns {} for anything with no recipe.

    Only one tier is undone per scrap, so a drill yields components and those
    components have to be scrapped again to reach smelted metal. That keeps
    every step legible - a player can read the recipe book and know what they
    will get - and it means intermediate goods stay recoverable.

    The guaranteed unit is not a special case bolted on for drills so much as
    what "half a recipe" has to mean when the recipe is one of each part. Every
    drill costs a chassis, some wiring and a bit, and half of that isn't
    expressible in whole items of any one of them, so a plain floor would return
    literally nothing. The same rule is what stops a Ruby Container's single
    ruby being incinerated in exchange for ten copper.

    Choosing the most valuable input to guarantee (by raw_input_cost of the
    whole line, not per unit) is what keeps that concession honest: for an Iron
    Drill it lands on the chassis, which is 50.0% of the drill's value, and for
    a Steel Drill on the steel bit at 52.0%. Gem-tier items come out near 100%,
    which is unavoidable rather than generous - the gem IS essentially all of a
    gem-tier item's value, so there is no subset of the recipe worth half of it.

    The invariant that makes all of this safe: max(1, floor(0.5 * q)) <= q for
    every q >= 1, so a yield never exceeds its own recipe and no craft-then-scrap
    cycle can produce material out of nothing. tests/test_scrapper.py asserts it
    across every entry in ALL_MATERIALS.
    """
    inputs = (get_material_info(material_id) or {}).get("inputs", {})
    if not inputs:
        return {}
    out = {i: int(q * SCRAP_RETURN_RATE) for i, q in inputs.items()}
    keystone = max(inputs, key=lambda i: raw_input_cost(i) * inputs[i])
    out[keystone] = max(1, out[keystone])
    return {i: q for i, q in out.items() if q > 0}


# Display order for /inventory: one entry per embed field, each listing its
# materials most-common-to-obtain first. Raw materials go by drop chance;
# everything crafted goes by raw input cost, cheapest first. Both orderings
# derive from the data above, so retuning drop rates or recipes reorders the
# inventory automatically.
# Drills are absent on purpose: they aren't stacks in user_materials any more
# but individually tracked rows in the drills table, each with its own level
# and container, so EconomyCog.inventory renders them in its own field.
INVENTORY_CATEGORIES = (
    ("Raw Materials", tuple(_by_drop_chance(ORES))),
    ("Smelted Materials", tuple(_by_cost(SMELTED_MATERIALS))),
    ("Gemstones", tuple(_by_drop_chance(GEMSTONES))),
    ("Components", tuple(_by_cost(DRILL_COMPONENTS) + _by_cost(DRILL_BITS) + _by_cost(UPGRADE_MATERIALS))),
    ("Containers", tuple(_by_cost(STORAGE_CONTAINERS))),
    ("Exotic Matter", tuple(_by_cost(PRESS_MATERIALS))),
)

# Display and choice order for the market: raw ores first (commonest first),
# then smelted (cheapest to obtain first), then gemstones (commonest first), so
# the list reads top to bottom from most common to rarest. Only raw and smelted
# materials are tradeable at all (docs/market.md section 3), so this is every
# tradeable id exactly once.
#
# Derived from the same helpers INVENTORY_CATEGORIES uses rather than written
# out by hand, so retuning a drop chance or a recipe reorders /market status,
# /market sell and /market buy automatically instead of leaving them behind.
# Previously the market took whatever order RAW_MATERIALS and SMELTED_MATERIALS
# happened to be declared in, which interleaved the three gemstones among the
# ores - a 500,000-value diamond sat two lines below iron ore at 0.01.
TRADEABLE_ORDER: tuple[str, ...] = tuple(
    _by_drop_chance(ORES) + _by_cost(SMELTED_MATERIALS) + _by_drop_chance(GEMSTONES)
)


# ---------------------------------------------------------------------------
# Daily job board
# ---------------------------------------------------------------------------

# What the daily job board can ask a server to sell it. Gemstones are excluded
# by construction, and that exclusion is a balance decision rather than an
# oversight: a gemstone's market_ceiling_price runs from 5,500 to 500,000, so
# since the reward is ceiling_price * quantity, a single one-diamond task would
# pay every player who completed it more than every other source of currency in
# the game combined.
JOB_BOARD_MATERIALS: tuple[str, ...] = tuple(
    _by_drop_chance(ORES) + _by_cost(SMELTED_MATERIALS)
)

# The day's task asks for this fraction of the server's target stock in the
# chosen material. Scaling off target_stock rather than a flat number is what
# makes one constant work for every server and every material at once: it
# already accounts for member count AND for how common the material is, so a
# five-member server is asked for 50 iron ore where a twenty-member one is
# asked for 200, and coal - which drops a quarter as often as iron ore - is
# asked for proportionally less.
#
# The reward this produces is bounded by construction. A normal sale pays
# between half and one times the ceiling price per unit (cogs/economy.py:
# _buy_price), so the bonus is at most a 100% top-up on the day's sale and
# never more than the goods' full ceiling value - and earning it means putting
# real materials into the market, which pushes the buy price down and partly
# pays for itself.
#
# The dial to reach for if this ever needs reining in is a maximum quantity,
# not a smaller fraction. Above roughly fifty members the ore tasks pass what
# one player can mine in a day (three level 1 diamond drills yield about 1,080
# items daily, some 612 of them iron ore), so they become stockpile-only while
# the reward keeps scaling - clamping the quantity at around 600 would cap both
# together. No server is near that yet, so this ships unclamped.
JOB_BOARD_TARGET_STOCK_FRACTION = 0.10

# Every eligible material carries at least this much selection weight, on top
# of however far below target stock the server is (see pick_job_material).
JOB_BOARD_SELECTION_FLOOR = 0.05


def job_quantity(member_count: int, material_id: str) -> int:
    """How many units the day's task asks for. Floors at 1, so the smallest
    server still gets an achievable task rather than one asking for zero."""
    return max(1, int(target_stock(member_count, material_id) * JOB_BOARD_TARGET_STOCK_FRACTION))


def job_reward(material_id: str, quantity: int) -> float:
    """What completing the task pays, on top of what selling the goods earned
    in the first place: the material's base ceiling price per unit."""
    return ALL_MATERIALS[material_id]["market_ceiling_price"] * quantity


def pick_job_material(deficits: dict[str, float], rng=random) -> str:
    """Chooses the day's task from how far below target stock the server is on
    each eligible material, as a fraction of that target (so a hundred-member
    server's shortfall is comparable to a five-member one's).

    Weighted rather than simply picking the largest deficit, because a
    deterministic maximum parks the board on one material until the server
    catches up - and a server that cannot produce that material at all yet (a
    brand new one and steel, say) would get the same impossible task every day
    forever, which is the one failure mode a DAILY task must not have.

    JOB_BOARD_SELECTION_FLOOR keeps a fully-stocked material in the running, so
    there is always somewhere for the weight to go even when the server needs
    nothing - without it a server at or above target on everything would have
    zero total weight and no task to post.
    """
    weights = [JOB_BOARD_SELECTION_FLOOR + max(0.0, deficits.get(m, 0.0)) for m in JOB_BOARD_MATERIALS]
    return rng.choices(JOB_BOARD_MATERIALS, weights=weights, k=1)[0]
