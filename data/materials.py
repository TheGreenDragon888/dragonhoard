"""
data/materials.py

Static definitions for every material, recipe, and drill in the game. This
is plain Python data (dicts), not database rows, because it never changes
at runtime - it's balance data you'll tune by editing this file and
restarting the bot, not something users modify.
"""
import math
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
# the three's market_ceiling_price was then rescaled to hold ceiling_price *
# drop_chance at what it was before the rebalance - the expected currency value
# of mining one item of that material is unchanged, only its frequency and
# per-unit price shifted. (The rescaled prices are rounded to six places, so
# the products match to about five significant figures rather than exactly.)
# Gemstone rates/prices are untouched.
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
        "mines_per_hour": 30,
    },
    "obsidian_drill": {
        "name": "Obsidian Drill", "emoji": custom_emoji("ObsidianMiningDrill", 1523433678825459893, 1533714429655646358),
        "inputs": {"wiring": 1, "drill_chassis": 1, "obsidian_drill_bit": 1},
        "mines_per_hour": 60,
    },
    "diamond_drill": {
        "name": "Diamond Drill", "emoji": custom_emoji("DiamondMiningDrill", 1523433688656908408, 1533714427679997952),
        "inputs": {"wiring": 1, "drill_chassis": 1, "diamond_drill_bit": 1},
        "mines_per_hour": 120,
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
#
# The ladder is set by TOTAL capacity, not by the bonus, and the bonuses are
# simply those totals minus BASE_STORAGE_CAPACITY - that's why they read as
# 150/400/1,900/3,900/7,900 rather than round numbers; the round number is the
# total, which is also the only one a player ever sees (/recipe factory
# renders effective_capacity, not the bonus).
#
# As of 1.2.1, Ruby/Obsidian/Diamond's totals are set by scaling each one by
# the exact same factor that tier's drill speed was scaled by that release
# (Steel->Ruby is a 4x jump on both the drill, 7.5->30, and the container,
# 500->2,000; Ruby->Obsidian and Obsidian->Diamond are both 2x jumps on drill
# and container alike). That keeps a container proportionally as useful
# relative to its own tier's drill as it always was - but because capacity and
# rate now scale by an identical factor at each of those three steps, runtime
# (capacity / effective_rate) is flat at 66.67 hours from Steel through
# Diamond (500/7.5 = 2,000/30 = 4,000/60 = 8,000/120). Iron is the only tier
# that still buys strictly less runtime (250/5 = 50 hours). See
# EffectiveCapacityTests.test_a_dearer_container_buys_more_runtime_than_a_cheaper_one
# in tests/test_drills.py, where this is pinned.
#
# The previous ladder was 150/200/300/400/500, and its problem was not that the
# gem tiers were low but that they were exactly cancelled out: totals of
# 300/400/500/600 sat in the same 3:4:5:6 ratio as the matched drills' rates at
# the time, so every container from steel up bought precisely 40 hours of
# runtime and the iron one bought 50. A ruby container cost a thousand times a
# steel one and bought no more autonomy than it - and less than the iron one.
# That check was run again for 1.2.1's drill speed buff, and this time the
# result was accepted rather than redesigned around: Steel through Diamond
# tying at 66.67 hours (see above) is the same flat-runtime shape this
# paragraph describes fixing once already, reintroduced here deliberately as
# the accepted cost of scaling containers proportionally to drill speed - not
# the same bug recurring by accident.
#
# No attempt is made to price these against what the gems cost - the usefulness
# of storage saturates within about a week of autonomy, so proportionality to a
# gemstone's value is not reachable and chasing it would put absurd numbers
# here. Note also that the gem is not really spent: scrap_yield's keystone
# guarantee returns it (along with half the copper), so a gem container costs
# half its copper, the factory time, and having the gem parked.
STORAGE_CONTAINERS = {
    "iron_container":     {"name": "Iron Container",     "emoji": custom_emoji("IronContainer", 1533713574977994793, 1533714646551363684), "inputs": {"iron": 10, "copper": 5},      "storage_bonus": 150},   # holds 250
    "steel_container":    {"name": "Steel Container",    "emoji": custom_emoji("SteelContainer", 1533713578811461642, 1533714649990824036), "inputs": {"steel": 10, "copper": 10},    "storage_bonus": 400},   # holds 500
    "ruby_container":     {"name": "Ruby Container",     "emoji": custom_emoji("RubyContainer", 1533713577607954542, 1533714648736469033), "inputs": {"ruby": 1, "copper": 20},      "storage_bonus": 1900},  # holds 2,000
    "obsidian_container": {"name": "Obsidian Container", "emoji": custom_emoji("ObsidianContainer", 1533713576261587107, 1533714647687893093), "inputs": {"obsidian": 1, "copper": 40},  "storage_bonus": 3900},  # holds 4,000
    "diamond_container":  {"name": "Diamond Container",  "emoji": custom_emoji("DiamondContainer", 1533713574076219502, 1533714644965916805), "inputs": {"diamond": 1, "copper": 80},   "storage_bonus": 7900},  # holds 8,000
}

# Made only by the hydraulic press. Deliberately has no market_ceiling_price:
# like drills and components it's a finished good, and docs/market.md section 3
# keeps those out of the market entirely. Nothing consumes it yet - it's
# reserved for a later feature, so it accumulates rather than being spent.
PRESS_MATERIALS = {
    "ultra_dense_matter": {"name": "Ultra Dense Matter", "emoji": custom_emoji("UltraDenseMatter", 1533722773418016868, 1533722431024529408)},
}


def _mining_equivalent(gem_id: str, material_id: str) -> float:
    """How much of a smelted material a player receives, on average, over the
    stretch of mining it takes to turn up one of that gemstone.

    Whichever of the recipe's raw inputs is scarcest relative to how much the
    recipe needs is what limits how much can actually be smelted - for steel
    that's iron ore, not coal.

    Exact rather than approximate: the expected number of units of a material
    mined before the first gem works out to simply its drop chance divided by
    the gem's."""
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
# to take it to the next level. Every level costs STEP times the last, so the
# ladder is 5, 25, 125, 625... and climbs out of reach on its own rather than
# stopping at a hard cap.
#
# The step was 10 until 1.2, and the ladder it produced was not a slow climb so
# much as a wall two rungs up. Fees are tiny by design - the furnace's default
# is 0.01 an item - and the market pays roughly one currency unit for a hundred
# iron ore, so 500 for a level 4 furnace is on the order of fifty thousand items
# smelted, which no server had come close to.
#
# Halving the step does far more than halve the cost, because the cost compounds
# - what changes is the exponent, not a coefficient. Cumulative fees to reach a
# level, old against new:
#
#     level 3      55  ->      30     1.8x cheaper
#     level 4     555  ->     155     3.6x
#     level 6  55,555  ->   3,905    14.2x
#     level 8   5.56M  ->  97,655    56.9x
#
# That is the intended size of the change: level 3 becomes an early goal rather
# than a milestone, and level 6 becomes reachable by a real server rather than
# theoretical. It is worth being clear-eyed that these fees are the game's
# primary currency SINK (docs/market.md section 1), so this loosens the money
# supply as well as the progression - accepted because a sink nobody can afford
# to use isn't draining anything either.
#
# Note the ladder is shared by all four machines but their fees are not: the
# press charges 5.00 per press-day against the furnace's 0.01 per item, so the
# press climbs this ladder hundreds of times faster than the furnace does. That
# asymmetry predates this change and is unaffected by it.
UPGRADE_THRESHOLD_BASE = 5.00
UPGRADE_THRESHOLD_STEP = 5


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

# The server-wide raw material pool, which as of 1.2 is a BAG of exactly this
# many items rather than a daily allowance.
#
# What it replaced, and why: the pool used to be topped up by 200 items per
# member per day and capped at three days of that. Both are gone. They made
# mining a rate limit dressed up as a resource - a server's output was fixed by
# its member count, so building better drills bought you nothing once you were
# already draining the daily allowance, and the answer to "how do we mine more?"
# was "recruit". Now the only limits are how many drills you have, how good they
# are, and how often you empty them.
#
# The bag holds exactly one million items in the drop_chance proportions above,
# which works out to:
#
#     iron_ore   566,700     ruby         90
#     copper_ore 283,350     obsidian      9
#     coal       149,850     diamond       1
#
# Those gemstone counts are the whole point. A drill draws from what is actually
# in the bag (draw_from_pool, without replacement), so a diamond is not a
# one-in-a-million chance re-rolled forever - it is a single object sitting in
# there that somebody WILL dig up before the bag is empty. Drain a bag, get a
# diamond. Every time, on every server.
#
# The bag refills the instant it empties. Any pause would just be the daily
# allowance again with extra steps, and the point of removing that was to make
# mining limited by what a player builds rather than by the clock.
#
# One million is chosen so the per-item gemstone density is identical to the
# published drop rates - what changes is that a bag holds exactly its stated
# gems rather than a random draw of them, not what it holds on average. Where
# within a bag a gem turns up is still chance; how many are in there is not.
# It is deliberately NOT scaled to server size: a smaller bag would
# make a small server's diamonds commoner per item mined, which is a different
# game rather than the same one at a different pace.
#
# The consequence worth being clear-eyed about is that gem timing is now purely
# a function of investment, with no floor under it. Days to drain a bag:
#
#     5 members, 3x Iron Drill Lv.1          1,800/day     556 days
#     5 members, 3x Diamond Drill Lv.10    120,960/day       8 days
#     200 members, 3x Iron Drill Lv.1       72,000/day      14 days
#     1 member, 3x Iron Drill Lv.1             360/day   2,778 days
#
# A lone player on starter drills waits years, and that is the accepted trade
# for making the ceiling unlimited: it is now their choice how fast to go, where
# before it was the server's member count's choice. An earlier design floored
# this at one diamond a year by injecting gems on a timer; it was dropped
# because the only recurring event it could hang off was the daily top-up that
# this replaced.
MINING_POOL_BAG_SIZE = 1_000_000


def pool_bag_contents(bag_size: int = MINING_POOL_BAG_SIZE) -> dict[str, int]:
    """One full bag, as exact whole counts of each raw material.

    Derived from drop_chance rather than written out, so retuning a rate
    reshapes the bag automatically. The largest material absorbs any rounding
    remainder, so the counts always sum to exactly `bag_size` - which matters
    because mining_pool_remaining is kept as their total and the two disagreeing
    is how a pool starts inventing or destroying items.
    """
    counts = {
        material_id: int(info["drop_chance"] * bag_size)
        for material_id, info in RAW_MATERIALS.items()
    }
    remainder = bag_size - sum(counts.values())
    if remainder:
        counts[max(counts, key=counts.get)] += remainder
    return counts

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


def sale_unit_price(ceiling_price: float, current_stock: int, target: int) -> float:
    """What a player RECEIVES per unit for selling into a server holding
    `current_stock` - the curve target_stock is the midpoint of. Full ceiling
    price at zero stock, half at target stock, tapering toward (but never
    reaching) zero beyond it.

    Named for what the player does rather than what the server does, because
    the two read as opposites and the difference is worth money. This is the
    rate EconomyCog._buy_price is built on - "buy" there is the SERVER buying,
    so _buy_price is what a player is paid, and the method named _sell_price is
    the one that costs them. Anything reaching for "the sell price" wants this.

    Takes a price rather than a material id so the market and the job board can
    share one definition of the curve without the job board's arithmetic
    reaching into a cog.
    """
    if target <= 0:
        return 0.0
    return ceiling_price * target / (target + current_stock)


def _sale_unit_price_of(material_id: str, current_stock: int, target: int) -> float:
    """sale_unit_price for a material id - what the job board works in."""
    return sale_unit_price(
        ALL_MATERIALS[material_id]["market_ceiling_price"], current_stock, target
    )


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
    1))` because 0.2 has no exact binary representation: on many type/level
    pairs the multiply-then-add form lands a fraction off, and that figure
    reaches an embed verbatim and feeds harvest arithmetic. Multiplying first
    and dividing by the anchor last keeps every rate here exact -
    tests/test_drills.py: test_rates_stay_exact is what pins that."""
    base = DRILLS[drill_type]["mines_per_hour"]
    return base * (LEVEL_RATE_ANCHOR + level - 1) / LEVEL_RATE_ANCHOR


def upgrade_cost(drill_type: str, level: int) -> dict[str, int]:
    """What it costs to take a drill from `level` to the next one. Every part
    of the recipe doubles per level, so the cost of a ladder is dominated by
    its last rung."""
    multiplier = 2 ** (level - 1)
    cost = {"drill_upgrade_pack": multiplier}
    for material_id, quantity in _UPGRADE_TIER_MATERIAL[drill_type].items():
        cost[material_id] = quantity * multiplier
    return cost


def accrue(carry: float, amount: float) -> tuple[int, float]:
    """Adds `amount` to a running fractional total and splits the result into
    whole units to hand over now and a remainder to carry forward.

    The one primitive behind every place the game accumulates fractions of an
    item: a drill's per-tick mining rate (advance_harvest) and a mining focus
    converting one ore into another (apply_mining_focus). Both have the same
    failure mode without it - rounding each step in isolation either destroys
    the fraction (a level 2 iron drill mines exactly what a level 1 does) or,
    if rounded up instead, mints material from nothing by repeating the step in
    small pieces.

    The tiny nudge stops accumulated float error from turning a total that
    should be exactly 1.0 into 0.999... and losing a unit to truncation; the
    clamp then keeps the returned carry a genuine fraction, since that same
    nudge can round up past the true total and leave it a hair below zero.
    """
    total = carry + amount
    whole = int(total + 1e-9)
    return whole, max(0.0, total - whole)


def advance_harvest(progress: float, rate_per_hour: float, ticks_per_hour: float) -> tuple[int, float]:
    """Splits a tick's worth of mining into whole items now and a fraction to
    carry into the next tick.

    The carry is what makes a level worth exactly its stated rate. At 12
    ticks/hour an iron drill's level is +0.083 items/tick, so rounding each
    tick in isolation would throw the bonus away entirely - levels 1 through 7
    (0.417 to 0.917 items/tick) would all floor to 0/tick and mine nothing at
    all, indistinguishable from one another. A type whose rate happens to
    divide evenly into ticks would survive without this; most don't, and which
    ones do changes with any retune, so the carry is unconditional rather than
    something to reason about per tier."""
    return accrue(progress, rate_per_hour / ticks_per_hour)


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


def draw_from_pool(available: dict[str, int], count: int, rng=random) -> dict[str, int]:
    """Takes `count` items out of a pool holding exactly `available`, drawing
    WITHOUT replacement, and reports what came out. Never draws more of anything
    than is there, and returns fewer than `count` items in total if the pool runs
    out partway.

    This is what makes the gemstone guarantee a guarantee rather than a second
    lottery on top of the first. Rolling each item independently (which is what
    roll_raw_material did, and still does for display and simulation) means a
    diamond sitting in the pool might never be drawn at all; drawing from the
    pool's real contents means that once one is in there, somebody gets it.

    Sequential rather than a closed-form multivariate hypergeometric because
    `count` is a single drill's share of one 5-minute tick - a handful of
    items - over six materials. The loop is cheaper than the arithmetic that
    would replace it.
    """
    remaining = {m: q for m, q in available.items() if q > 0}
    drawn: dict[str, int] = {}
    for _ in range(max(0, count)):
        total = sum(remaining.values())
        if total <= 0:
            break
        roll = rng.randrange(total)
        cumulative = 0
        for material_id, quantity in remaining.items():
            cumulative += quantity
            if roll < cumulative:
                drawn[material_id] = drawn.get(material_id, 0) + 1
                remaining[material_id] -= 1
                if remaining[material_id] == 0:
                    del remaining[material_id]
                break
    return drawn


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


# ---------------------------------------------------------------------------
# Mining focus
# ---------------------------------------------------------------------------

# A player who has reached the ruby stage can commit their mining to one ore.
# Everything they would have mined of the other ores arrives as their chosen one
# instead, converted at the RARITY ratio - a copper ore is worth two iron ore
# because iron drops twice as often, so the trade is even in mining effort.
# Gemstones are never touched by any of this.
#
# `keep` is what passes through unconverted and `primary` is what everything
# else becomes. Coal is kept by every focus that has one because it is fuel: the
# furnace burns a coal per item smelted whatever the recipe, so a focus that
# converted coal away would stop the player smelting the very ore they chose.
#
# The conversion is deliberately not value-neutral, and it is worth knowing
# which way it leans before retuning anything. The three ores' drop_chance x
# market_ceiling_price come to 0.0060 for iron ore against 0.0050 for the other
# two, so converting at the rarity ratio moves market value by about +6% for
# iron, -6% for copper and -6.4% for coal. Converting at the PRICE ratio instead
# would be exactly neutral (a copper would become 1.67 iron rather than 2.0),
# and was not chosen: 6% is noise beside everything else a focus changes, and
# "iron drops twice as often, so a copper is worth two of it" is a rule a player
# can hold in their head, which 1.67 is not.
#
# Three consequences that are not obvious from the table and should be stated
# plainly wherever a player is choosing:
#
#   - COPPER and COAL focus make steel impossible to self-supply. Steel needs
#     iron ore, and neither produces any. That rules out every steel component,
#     both steel drill bits, the Steel Container, and diamonds via the press.
#   - IRON focus is a weaker help to steel than it looks. It takes the stream to
#     7.56 iron ore per coal, and steel wants 5:1, so COAL becomes the binding
#     input rather than ore - steel per item mined improves by ~32%, not 100%.
#   - It roughly halves the mining behind a pressed ruby or obsidian. The press
#     is priced at ~95% of what a player would have mined alongside finding that
#     gem naturally (see PRESS_COST_BASE), and doubling the relevant ore halves
#     that. Intended - accelerating the gem economy is the point of the feature -
#     but it is the largest downstream effect of anything here.
MINING_FOCUSES = {
    "balanced": {
        "name": "Balance", "emoji": "⚖️", "primary": None, "keep": ORES,
        "blurb": "All resources are mined in their natural proportions.",
    },
    "iron": {
        "name": "Iron & Coal", "emoji": custom_emoji("IronOre", 1523432328028885034, 1533714268560691281), "primary": "iron_ore", "keep": ("iron_ore", "coal"),
        "blurb": "Copper ore is converted to iron ore.",
    },
    "copper": {
        "name": "Copper & Coal", "emoji": custom_emoji("CopperOre", 1523432342813933699, 1533714267478560818), "primary": "copper_ore", "keep": ("copper_ore", "coal"),
        "blurb": "Iron ore is converted to copper ore.",
    },
    "coal": {
        "name": "Coal", "emoji": custom_emoji("Coal", 1523432352318099456, 1533714266551484516), "primary": "coal", "keep": ("coal",),
        "blurb": "All raw ores are converted to coal.",
    },
}

DEFAULT_MINING_FOCUS = "balanced"

# What unlocking the feature costs, once. Deliberately a one-off rather than a
# per-switch charge: a ruby is about 11,000 items mined, which is a month of a
# starting player's entire output, while the benefit of CHANGING focus in
# response to your server's prices is fractions of a coin. Charging it per
# switch would price the choice so far above its own value that nobody would
# ever revise it, which is the opposite of the point - a market only diversifies
# if people can respond to it. Switching is instead free and rate-limited to
# once a day (see MINING_FOCUS_SWITCH_PER_DAY).
MINING_FOCUS_UNLOCK_COST = {"ruby": 1}
MINING_FOCUS_SWITCH_PER_DAY = 1


def focus_conversion_rate(source_id: str, target_id: str) -> float:
    """How many of `target_id` one `source_id` becomes: the ratio of their drop
    chances. Iron ore drops exactly twice as often as copper ore, so a copper
    becomes two iron and an iron becomes half a copper - even trades measured in
    how long each took to dig up."""
    return RAW_MATERIALS[target_id]["drop_chance"] / RAW_MATERIALS[source_id]["drop_chance"]


def apply_mining_focus(
    focus_id: str, breakdown: dict[str, int], carry: float = 0.0
) -> tuple[dict[str, int], float]:
    """Converts a haul according to a mining focus. Returns the new breakdown
    and the fraction of the focus's primary ore still owed.

    The carry is not a nicety, it is what stops the conversion being a money
    printer. A coal focus turns one iron ore into 0.264 coal: rounding that up
    would let somebody run /mine remove on a one-item drill over and over and
    get 3.8x their material, and rounding it down would destroy the haul of
    anyone collecting in small amounts. Banking the remainder against the
    player's next collection is the only option that is neither.

    One carry value covers every source ore because a focus has exactly one
    primary - all conversions land on the same material, so their fractions are
    summed before being split. It has to be RESET when the focus changes, or a
    fraction of a copper owed under one focus would be paid out as iron under
    the next.

    Gemstones pass through untouched, whatever the focus. That is the whole
    promise of the feature: what you choose changes which ore you get, never
    your odds on the things worth having.
    """
    focus = MINING_FOCUSES[focus_id]
    primary = focus["primary"]
    if primary is None:
        return dict(breakdown), carry

    converted = dict(breakdown)
    owed = 0.0
    for material_id, quantity in breakdown.items():
        if material_id in focus["keep"] or material_id not in ORES:
            continue
        owed += quantity * focus_conversion_rate(material_id, primary)
        del converted[material_id]

    whole, new_carry = accrue(carry, owed)
    if whole:
        converted[primary] = converted.get(primary, 0) + whole
    return converted, new_carry


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
# then smelted (cheapest to obtain first). This is every tradeable id exactly
# once - the market's whole vocabulary, driving /market status's lines and both
# /market sell's and /market buy's choice lists.
#
# GEMSTONES ARE DELIBERATELY ABSENT, as of 1.2, and this is a balance decision
# rather than a display one. A ruby's ceiling price is 5,500 against iron ore at
# 0.01, so one gem sale minted more currency than a server's entire population
# could earn by playing the game - and because a gemstone's target stock stays
# at 1 on any server of realistic size, the curve that is meant to damp repeated
# sales barely engaged: the first four ruby sales alone paid 5,500 + 2,750 +
# 1,833 + 1,375. scripts/revert_gem_sales.py is the one-time repair.
#
# Gemstones now sit alongside components and drills in docs/market.md section 3's
# non-tradeable category. They are removed from BUYING too, not just selling:
# once no server can acquire one, leaving them in the buy list would offer
# players something no server will ever have in stock.
#
# Derived from the same helpers INVENTORY_CATEGORIES uses rather than written
# out by hand, so retuning a drop chance or a recipe reorders /market status,
# /market sell and /market buy automatically instead of leaving them behind.
# Previously the market took whatever order RAW_MATERIALS and SMELTED_MATERIALS
# happened to be declared in, which interleaved the three gemstones among the
# ores - a 500,000-value diamond sat two lines below iron ore at 0.01.
TRADEABLE_ORDER: tuple[str, ...] = tuple(
    _by_drop_chance(ORES) + _by_cost(SMELTED_MATERIALS)
)


# ---------------------------------------------------------------------------
# Daily job board
# ---------------------------------------------------------------------------

# What the daily job board can ask a server to sell it: exactly what the market
# trades, because the task is completed BY selling and a job for something the
# market won't buy is unfinishable by construction.
#
# This used to be its own tuple that happened to equal TRADEABLE_ORDER minus
# gemstones - the board excluded gems while the market still bought them. As of
# 1.2 the market doesn't either, so the two lists became the same list, and
# aliasing rather than restating it is what stops them drifting apart again.
#
# The gem exclusion is a balance decision either way, and worth restating here
# because it is the board that would pay for it: a gemstone's ceiling price runs
# from 5,500 to 500,000, so one gem task would pay every player who completed it
# more than every other source of currency in the game combined.
#
# Note that JOB_BOARD_TARGET_PAYOUT does NOT protect against this on its own,
# and re-admitting a material on the assumption that it does would be an
# expensive mistake. The payout is met by lowering the quantity, and the
# quantity floors at one unit - so for anything worth more than the target
# payout per unit, the task is one unit and the payout is whatever that unit is
# worth. Every material here is under 0.52 a unit, which is what makes the rule
# work; a diamond would simply pay 500,000.
JOB_BOARD_MATERIALS: tuple[str, ...] = TRADEABLE_ORDER

# What the day's task aims to pay. The quantity is worked backwards from this:
# the smallest number of units whose reward clears it. One task, one dollar,
# whoever you are and whatever the board picked.
#
# Pinning the PAYOUT rather than the quantity is what makes the task the same
# size for everybody. Both terms below derive from member count and cancel:
#
#     quantity = TARGET_PAYOUT / sale_unit_price
#              = TARGET_PAYOUT / (ceiling * target / (target + stock))
#              = TARGET_PAYOUT / ceiling * (1 + stock / target)
#
# leaving only the ratio stock/target, which is a statement about how well
# supplied a server is and not about how many people are in it. A five-member
# server and a five-hundred-member one sitting at the same fraction of target
# stock are asked for exactly the same amount. That is the whole point: the
# task is completed per player, so sizing it off a server-wide total (as the
# old TARGET_STOCK_FRACTION did) grew the personal quota with the member list
# while nobody's mining rate grew with it.
#
# The rarity scaling the old fraction gave for free survives, because 1/ceiling
# tracks how common a material is: iron ore comes out at 95 units and steel at
# 2, without a per-material table to keep in step.
JOB_BOARD_TARGET_PAYOUT = 1.00

# Ceiling on the quantity, whatever the arithmetic above asks for. Roughly a
# day's output from three level 1 diamond drills (about 1,080 items, some 612
# of them iron ore), so it is "more than a heavily invested player mines in a
# day" rather than a round number.
#
# It exists because quantity has no natural bound as stock climbs: the price
# a server pays decays toward zero past target stock, so the units needed to
# clear a fixed payout grow without limit. How far past target stock it starts
# binding depends on the material - the cheaper one is per unit, the sooner -
# so it reaches the common ores long before the smelted ones. Once it does
# bind the payout falls under JOB_BOARD_TARGET_PAYOUT, which is the intended
# trade: a task nobody can finish pays nothing at all.
JOB_BOARD_MAX_QUANTITY = 600

# Every eligible material carries at least this much selection weight, on top
# of however far below target stock the server is (see pick_job_material).
JOB_BOARD_SELECTION_FLOOR = 0.05


def job_quantity(material_id: str, current_stock: int, target: int) -> int:
    """How many units the day's task asks for: the fewest that pay
    JOB_BOARD_TARGET_PAYOUT, capped at JOB_BOARD_MAX_QUANTITY.

    Deliberately takes no member count. Only the stock/target ratio survives
    the algebra (see JOB_BOARD_TARGET_PAYOUT), so server size cannot reach the
    answer - which is the fix for the task growing past what one player could
    mine on a server that merely had a lot of people in it.

    Floors at 1 so there is always something to sell, and rises as the server
    fills up, because each unit is worth less to a server that already has
    plenty and it takes more of them to clear the same payout.
    """
    unit_price = _sale_unit_price_of(material_id, current_stock, target)
    if unit_price <= 0:
        return 1
    return max(1, min(JOB_BOARD_MAX_QUANTITY, math.ceil(JOB_BOARD_TARGET_PAYOUT / unit_price)))


def job_reward(material_id: str, quantity: int, current_stock: int, target: int) -> float:
    """What completing the task pays, on top of what selling the goods earned
    in the first place: what the server itself would pay for that many units,
    priced at the stock level when the job was posted.

    The ceiling price used to stand here, and it made the board printable at
    every stock level. Sell the task quantity, claim the bonus, buy the same
    goods back: the buyback is priced at the higher stock your own sale just
    created, so it cost less than the sale plus the bonus paid you, and you
    ended the day with your materials and a little free currency. Paying the
    server's own rate instead closes that once a server holds a real amount of
    the material, and the further past target stock it is the wider the loss -
    at target stock the round trip costs about a currency unit more than the
    job paid, whatever the server's size.

    It does NOT close below that, and that is the part to know before touching
    anything here. The leak is driven by quantity/target_stock - how far your
    own sale moves the price you then buy back at - and quantity is
    member-independent by design while target stock is not, so it is worst on
    the smallest servers and shrinks toward nothing as one grows. Any retune of
    JOB_BOARD_TARGET_PAYOUT or the target-stock constants should re-measure it
    rather than assume it stayed small.

    The extreme is a lone player: target stock for one member is 17 coal
    against a task of 31, so the task is nearly twice the entire equilibrium
    and a single sale swings the price hard. It is also the case worth caring
    least about - every server's economy is its own, so a solo player printing
    currency is only doing it to themselves.

    Closing it outright means pricing the bonus at the stock level the task
    itself creates (current_stock + quantity) rather than at posting stock,
    which makes the round trip break even at worst. That was left alone
    deliberately: it needs the quantity solved self-consistently against its own
    reward, and it lands the payout under JOB_BOARD_TARGET_PAYOUT unless the
    quantity grows about a quarter to compensate. A capped leak on an
    under-stocked material was judged the better trade against a bigger daily
    task for everyone.

    Must be passed the CAPPED quantity - a reward for more units than the task
    actually asks for would hand back the printable margin by another door.
    """
    return _sale_unit_price_of(material_id, current_stock, target) * quantity


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
