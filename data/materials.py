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
# should sum to 1.0. market_price is what the server market pays to acquire
# one unit (see cogs/economy.py and docs/market.md) - denominated in the
# buying server's own currency, not DragonCoin.
#
# EVERY PRICE IN THIS FILE IS A WHOLE NUMBER OF CENTS, and that is a hard
# rule rather than a coincidence (1.3). The market's prices no longer move
# with the server's stock, so a price is now a figure a player reads off
# /market status and multiplies in their head - and a price of 0.010588 that
# displays as "0.01" and charges something else is exactly the kind of
# arithmetic a static price is supposed to make possible. Anything added here
# rounds to a cent; MARKET_PRICE_CENTS below is the assertion that keeps it
# that way.
#
# The three common ores are the rounded form of the values the 1.2 coal
# rebalance left behind (0.010588 / 0.017681 / 0.033333). Rounding to the cent
# cannot preserve their ratios at this magnitude, because the grid is as coarse
# as the prices are: one cent was 94.4% of iron ore's whole price, so copper
# ore's 0.0177 had to become either 0.01 or 0.02 with nothing in between. It
# went to 0.02 (+13.1%), iron ore to 0.01 (-5.6%) and coal to 0.03 (-10.0%),
# taking their ratios to iron ore from 1.67 and 3.15 to a flat 2 and 3.
#
# The one balance figure that shifted with it is stated where it is read, in
# the mining focus block below: drop_chance * market_price, which was 0.0060
# for iron ore against 0.0050 for the other two, is now 0.005667 for iron ore,
# 0.005667 for copper ore and 0.0044955 for coal.
#
# Gemstone prices are untouched, and were already whole currency units. They
# are not tradeable (see TRADEABLE_ORDER) - the price is what raw_input_cost
# values a gem at when comparing recipes.
RAW_MATERIALS = {
    "iron_ore":    {"name": "Iron Ore",    "emoji": custom_emoji("IronOre", 1523432328028885034, 1533714268560691281),    "drop_chance": 0.5667,   "market_price": 0.01},
    "copper_ore":  {"name": "Copper Ore",  "emoji": custom_emoji("CopperOre", 1523432342813933699, 1533714267478560818),  "drop_chance": 0.28335,  "market_price": 0.02},
    "coal":        {"name": "Coal",        "emoji": custom_emoji("Coal", 1523432352318099456, 1533714266551484516),       "drop_chance": 0.14985,  "market_price": 0.03},
    "ruby":        {"name": "Ruby",        "emoji": custom_emoji("Ruby", 1532897325238980680, 1533714310134497404),       "drop_chance": 0.00009,  "market_price": 5500.00},
    "obsidian":    {"name": "Obsidian",    "emoji": custom_emoji("Obsidian", 1532899466687021268, 1533714309119737946),   "drop_chance": 0.000009, "market_price": 52500.00},
    "diamond":     {"name": "Diamond",     "emoji": custom_emoji("Diamond", 1523433355708858612, 1533714307911778446),    "drop_chance": 0.000001, "market_price": 500000.00},
}

# Smelted materials: produced by the furnace from raw materials.
# "inputs" maps material_id -> quantity required to produce ONE output unit.
#
# Balance rule: each price is 150% of the combined price of its recipe's raw
# inputs. That is now DERIVED rather than written out and checked by hand -
# _smelted_market_price does the arithmetic in integer cents below - because
# the rule and the whole-cent rule have to hold simultaneously, and a pair of
# constraints maintained by hand across three lines is a pair of constraints
# that drifts. Retuning an ore's price now moves the bars that smelt from it.
SMELTED_MARKUP = 1.5

SMELTED_MATERIALS = {
    "iron":   {"name": "Iron",   "emoji": custom_emoji("Iron", 1523433412805918820, 1533714352232988722),   "inputs": {"iron_ore": 10}},
    "copper": {"name": "Copper", "emoji": custom_emoji("Copper", 1523433425220927498, 1533714350416728167), "inputs": {"copper_ore": 10}},
    "steel":  {"name": "Steel",  "emoji": custom_emoji("Steel", 1523433463150149692, 1533714353428107264),  "inputs": {"iron_ore": 20, "coal": 4}},
}


def _market_price_cents(price: float) -> int:
    """A price as a whole number of cents, refusing anything that isn't one.

    Prices are held as floats because balances are, but every arithmetic
    decision about them (the smelting markup, the buy markup, the job board's
    quantity) is made in cents so that "rounded to the cent" is a property of
    the numbers rather than of how they happen to be displayed.
    """
    cents = round(price * 100)
    assert abs(price * 100 - cents) < 1e-9, f"price is not a whole number of cents: {price}"
    return cents


def _smelted_market_price(inputs: dict[str, int]) -> float:
    """SMELTED_MARKUP over the combined price of a recipe's raw inputs,
    rounded to the cent. The three live recipes all land exactly on one (10,
    20 and 32 cents of input, times 1.5), so the rounding is a guard for a
    future retune rather than something any current material relies on."""
    input_cents = sum(
        _market_price_cents(RAW_MATERIALS[material_id]["market_price"]) * quantity
        for material_id, quantity in inputs.items()
    )
    return round(input_cents * SMELTED_MARKUP) / 100


for _info in SMELTED_MATERIALS.values():
    _info["market_price"] = _smelted_market_price(_info["inputs"])

# Every price the market quotes, in cents - the single place the whole-cent
# rule is enforced rather than merely intended. Built at import so a price
# that isn't a whole cent fails on startup, next to the table it was typed
# into, rather than as a receipt that charges 0.0106 and says 0.01.
MARKET_PRICE_CENTS: dict[str, int] = {
    material_id: _market_price_cents(info["market_price"])
    for table in (RAW_MATERIALS, SMELTED_MATERIALS)
    for material_id, info in table.items()
}

# The blast furnace: the same three smelting recipes, in batches of 100.
#
# One blast furnace "item" is one batch, so its inputs and its output are every
# furnace figure multiplied by BLAST_FURNACE_BATCH_SIZE - derived here rather
# than written out, so retuning a furnace recipe can never leave the bulk one
# quoting the old ratio. That identity is the whole point of the machine: it is
# an auxiliary furnace for players moving thousands of ore at a time, not a
# better exchange rate. Every cost it charges per item is the furnace's own
# scaled by that same 100 (config.DEFAULT_BLAST_FURNACE_FEE,
# BLAST_FURNACE_COAL_COST_PER_BATCH), so the price of one smelted unit is the
# same at either machine.
#
# Deliberately NOT added to _MATERIAL_TABLES: these recipes produce the same
# three material_ids the furnace does, so a lookup table holding both would
# have to pick one, and get_material_info would start reporting a batch's
# quantities for an ordinary Iron.
BLAST_FURNACE_BATCH_SIZE = 100

BLAST_FURNACE_RECIPES = {
    material_id: {
        "inputs": {
            input_id: per_unit * BLAST_FURNACE_BATCH_SIZE
            for input_id, per_unit in recipe["inputs"].items()
        },
        "output": BLAST_FURNACE_BATCH_SIZE,
    }
    for material_id, recipe in SMELTED_MATERIALS.items()
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
        "mines_per_hour": 120,
    },
    "diamond_drill": {
        "name": "Diamond Drill", "emoji": custom_emoji("DiamondMiningDrill", 1523433688656908408, 1533714427679997952),
        "inputs": {"wiring": 1, "drill_chassis": 1, "diamond_drill_bit": 1},
        "mines_per_hour": 480,
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
# 150/400/1,900/7,900/31,900 rather than round numbers; the round number is
# the total, which is also the only one a player ever sees (/recipe factory
# renders effective_capacity, not the bonus).
#
# Every container from Steel up is set by scaling that tier's ladder-mate
# total by the exact same factor its drill's speed was scaled by (Steel->Ruby
# is a 4x jump on both the drill, 7.5->30, and the container, 500->2,000; as
# of 1.3, Ruby->Obsidian and Obsidian->Diamond are 4x jumps too, on both drill
# and container alike - 30->120->480 and 2,000->8,000->32,000). That keeps a
# container proportionally as useful relative to its own tier's drill as it
# always was - but because capacity and rate scale by an identical factor at
# every one of those three steps, runtime (capacity / effective_rate) is flat
# at 66.67 hours from Steel through Diamond (500/7.5 = 2,000/30 = 8,000/120 =
# 32,000/480). Iron is the only tier that still buys strictly less runtime
# (250/5 = 50 hours). See
# EffectiveCapacityTests.test_a_dearer_container_buys_more_runtime_than_a_cheaper_one
# in tests/test_drills.py, where this is pinned.
#
# The previous ladder was 150/200/300/400/500, and its problem was not that the
# gem tiers were low but that they were exactly cancelled out: totals of
# 300/400/500/600 sat in the same 3:4:5:6 ratio as the matched drills' rates at
# the time, so every container from steel up bought precisely 40 hours of
# runtime and the iron one bought 50. A ruby container cost a thousand times a
# steel one and bought no more autonomy than it - and less than the iron one.
# That check was run again for 1.2.1's drill speed buff (which introduced the
# 66.67-hour tie for Steel through Ruby only, Obsidian and Diamond still
# scaling at 2x per step) and again for 1.3's, which extended the same 4x
# factor through Obsidian and Diamond - and each time the result was accepted
# rather than redesigned around: the flat-runtime shape this paragraph
# describes fixing once is reintroduced here deliberately, as the accepted
# cost of scaling containers proportionally to drill speed, not the same bug
# recurring by accident.
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
    "obsidian_container": {"name": "Obsidian Container", "emoji": custom_emoji("ObsidianContainer", 1533713576261587107, 1533714647687893093), "inputs": {"obsidian": 1, "copper": 40},  "storage_bonus": 7900},  # holds 8,000
    "diamond_container":  {"name": "Diamond Container",  "emoji": custom_emoji("DiamondContainer", 1533713574076219502, 1533714644965916805), "inputs": {"diamond": 1, "copper": 80},   "storage_bonus": 31900},  # holds 32,000
}

# Made only by the hydraulic press. Deliberately has no market_price:
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
# items per hour and the blast furnace in batches per hour (see
# BLAST_FURNACE_BATCH_SIZE); the press is in ruby-equivalents per day (see
# PRESS_RECIPES). All five scale linearly and have no maximum level - the cost
# of the next upgrade is the only ceiling.
FURNACE_RATE_PER_LEVEL = 5
FACTORY_RATE_PER_LEVEL = 1
PRESS_RATE_PER_LEVEL = 1
# One batch an hour per level, so 100 smelted units an hour per level: twenty
# times the furnace at the same level, and the one figure about the blast
# furnace that is NOT simply the furnace's times a hundred.
#
# The furnace's problem is that mining scales with a server's player count and
# a single shared furnace does not. Working from this file's own numbers - a
# player may place BASE_MINING_SLOTS drills, a Diamond Drill mines
# 120/hour, iron ore is 0.5667 of a balanced haul, and Iron takes 10 ore - one
# player running three of them generates 20.4 Iron/hour of smelting demand,
# against the 25/hour a level 5 furnace can actually smelt. One such player
# saturates the machine the whole server shares.
#
# Nor can the furnace be leveled out of that. Levels are a high-water mark on
# lifetime fees (utils/db_helpers.py: apply_machine_upgrades), so at the 0.01
# default the 3,125 that level 6 costs is 13,467 hours - 561 days - of nonstop
# smelting. Level 5 to 6 is where a real server stops.
#
# Twenty times is what that gap costs to close, measured against the thing the
# machine exists for: a pressed diamond's 27,000 Steel takes a level 5 furnace
# 45 days, and a level 1 blast furnace 11.25. Ten players running three Diamond
# Drills each mine that much ore in about 11 days, so at 20x the ore supply is
# the constraint again rather than the machine - which is the intended shape.
# It is deliberately not 100x: that would take one level 1 machine past two
# dozen such players' entire output and leave the press as the only pacing in
# the gem loop at all. See tests/test_blast_furnace.py: BlastFurnaceRateTests.
BLAST_FURNACE_RATE_PER_LEVEL = 1
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


def blast_furnace_rate(level: int) -> int:
    """Batches per hour at this blast furnace level. Multiply by
    BLAST_FURNACE_BATCH_SIZE for the smelted items that actually lands."""
    return BLAST_FURNACE_RATE_PER_LEVEL * level


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
# The blast furnace's fuel, scaled like everything else it charges: a batch of
# 100 burns what 100 items would at the furnace, so fuelling a smelted unit
# costs the same at either machine.
BLAST_FURNACE_COAL_COST_PER_BATCH = FURNACE_COAL_COST_PER_UNIT * BLAST_FURNACE_BATCH_SIZE

# How many drills one player may have placed in one server before the server
# has unlocked anything. Every server starts here; mining slot levels above 1
# add one each (see mining_slots below).
#
# Renamed from MAX_DRILLS_PER_USER_PER_SERVER in 1.3, when it stopped being a
# maximum. It is now the floor of a ladder rather than a ceiling, and a name
# saying "MAX" would have been wrong on every server that had unlocked a slot.
BASE_MINING_SLOTS = 3

# Cumulative infrastructure fees - every machine's, added together - a server
# must have paid to unlock its FIRST extra mining slot. Each slot after it
# costs UPGRADE_THRESHOLD_STEP times the last, exactly as machine levels do, so
# the ladder is 25 / 125 / 625 / 3,125 and climbs out of reach on its own
# rather than stopping at a cap.
#
# The base is five times UPGRADE_THRESHOLD_BASE, which puts the first slot at
# the same cost as taking one machine to level 3. That is deliberate: a slot is
# a permanent multiplier on everything the server mines, so it should not be
# cheaper than the machine levels it is bought alongside. It reads from the
# SUM of all five machines' fees rather than any one of them, so it is the
# whole server's investment that unlocks it and no single machine's ladder is
# made steeper by it.
#
# What that costs in practice, from this file's own numbers: at the 0.01
# default furnace fee, 25 is 2,500 items smelted; at the 5.00 press fee, which
# a ruby pays once (press_days 1), it is five ruby-presses. Because
# upgrade_threshold(3) is also 25, a server that has taken any single machine
# to level 3 has necessarily paid enough for its first slot as well.
MINING_SLOT_THRESHOLD_BASE = 25.00


def mining_slot_threshold(level: int) -> float:
    """Cumulative infrastructure fees a server must have paid to reach mining
    slot `level`. Level 1 is what every server starts with and costs nothing,
    so this is only meaningful from 2 up; like machine levels there is no
    maximum, so it always returns a number."""
    return MINING_SLOT_THRESHOLD_BASE * UPGRADE_THRESHOLD_STEP ** (level - 2)


def mining_slot_level(invested: float) -> int:
    """The highest mining slot level `invested` in lifetime infrastructure fees
    pays for.

    Loops rather than inverting the exponential, for the same reason
    apply_machine_upgrades does: floating point at a threshold boundary is the
    one place this must not be off by one, and a server that has paid exactly
    625.00 has earned the level it just bought. `invested` is finite, so the
    loop is too.
    """
    level = 1
    while invested >= mining_slot_threshold(level + 1):
        level += 1
    return level


def mining_slots(level: int) -> int:
    """How many drills one player may have placed in a server at this mining
    slot level: the base every server starts with, plus one per level above 1.

    The floor is defensive, matching effective_max_queue - a zero reaching here
    would strand every drill in the server rather than merely being stingy."""
    return BASE_MINING_SLOTS + max(1, level) - 1

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
# in their smelted material; gem-tier drills pay in their gem - 1 as of
# 1.2.2, down from 3. This is the upgrade cost only: the drill bit's own
# crafting recipe (COMPONENT_MATERIALS's *_drill_bit entries) is unchanged,
# still 3 gems.
_UPGRADE_TIER_MATERIAL = {
    "iron_drill":     {"iron": 10},
    "steel_drill":    {"steel": 10},
    "ruby_drill":     {"ruby": 1},
    "obsidian_drill": {"obsidian": 1},
    "diamond_drill":  {"diamond": 1},
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

# The server market's per-material "target stock" - how much of it a server of
# a given size is expected to hold - scales with server size: target_stock =
# member_count * MATERIAL_TARGET_STOCK_PER_MEMBER[material_id]. member_count
# here should already exclude bots (see utils/guild_helpers.py) - the target
# stock is meant to reflect the server's actual player base.
#
# This USED to be a price parameter: the market paid full price at zero stock,
# half at target stock and less beyond it, and target stock was the midpoint of
# that curve. As of 1.3 prices are static (see MARKET_PRICE_CENTS above and
# docs/market.md section 3) and target stock is purely an inventory figure. Two
# things still read it, and they are why it survived the curve:
#
#   * the furnace's auto-smelt, which only smelts a server's surplus ore -
#     the portion above that ore's target stock (cogs/furnace.py).
#   * the job board's choice of material, which weights each candidate by
#     target / (stock + target), so "what is this server short of" is answered
#     on the same scale for a five-member server and a five-hundred-member one
#     (pick_job_material below).

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
    """How much of `material_id` a server this size is expected to hold
    (docs/market.md section 3) - the furnace auto-smelt's surplus threshold and
    the job board's "short of what?" weighting, no longer a price parameter.
    `member_count` should already be bot-excluded (utils/guild_helpers.py:
    human_member_count)."""
    return max(1, round(member_count * MATERIAL_TARGET_STOCK_PER_MEMBER[material_id]))


# What a player PAYS per unit buying out of the server's stock, as a multiple
# of what they are paid for selling one. Two rather than any other number, and
# it is doing more work than a markup normally would:
#
#   * It is the old rule's fixed point. The resale price used to be the
#     acquisition rate plus one full ceiling price, which was double at zero
#     stock and tapered toward the ceiling as the shelves filled. With the
#     acquisition rate now flat AT the old ceiling, "plus one ceiling" IS
#     doubling, at every stock level.
#   * It is what keeps the job board honest now that its bonus is paid per
#     completion rather than once a day (see JOB_BOARD_TARGET_PAYOUT). Sell q
#     units and complete the task: the sale pays q*price and the bonus pays at
#     most another q*price, while buying those same q units back costs exactly
#     2*q*price. The round trip cannot come out ahead. At a markup of 1.5 it
#     could, indefinitely, for as long as someone cared to repeat it.
MARKET_BUY_MARKUP = 2


def sale_unit_price(material_id: str) -> float:
    """What a player RECEIVES per unit for selling `material_id` to the server.

    Named for what the player does rather than what the server does, because
    the two read as opposites and the difference is worth money: this is the
    SERVER buying. purchase_unit_price is the one that costs the player.

    Flat as of 1.3 - it does not depend on the server's stock, its size, or
    anything else. It used to decay from this figure toward zero as the
    server's shelves filled (docs/market.md section 3), which is why so much
    of this module used to thread a stock level and a target through to reach
    a price.
    """
    return MARKET_PRICE_CENTS[material_id] / 100


def purchase_unit_price(material_id: str) -> float:
    """What a player PAYS per unit to buy `material_id` out of the server's
    stock: MARKET_BUY_MARKUP times what selling one pays. Flat, like the sale
    price, and always strictly above it - so selling something and immediately
    buying it back is never profitable."""
    return MARKET_PRICE_CENTS[material_id] * MARKET_BUY_MARKUP / 100


def sale_total(material_id: str, quantity: int) -> float:
    """What the server pays for `quantity` units.

    Multiplied in cents and divided once at the end, rather than by scaling a
    float unit price. Neither 0.15 nor 0.01 is itself in binary, so the
    per-unit form drifts off a whole cent at ordinary quantities: 0.15 * 3 is
    0.44999999999999996 and 0.15 * 99999 is 14999.849999999999, against 0.45
    and 14999.85 here. Both display the same, but a balance that is a hair
    under a cent is one that can quote "you can afford 2" for something you can
    afford 3 of, which is the class of bug max_affordable's 1e-9 nudge exists
    to paper over. Cent counts up to 2^53 are exact, so there is nothing to
    paper over at this end.
    """
    return MARKET_PRICE_CENTS[material_id] * quantity / 100


def purchase_total(material_id: str, quantity: int) -> float:
    """What a player pays for `quantity` units out of the server's stock -
    sale_total times MARKET_BUY_MARKUP, in cents for the same reason."""
    return MARKET_PRICE_CENTS[material_id] * MARKET_BUY_MARKUP * quantity / 100


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
    the way down and totalling the raw inputs' market prices. Crafted items
    have no drop chance, so this stands in as "how hard it is to obtain" when
    ordering them for display."""
    if material_id in RAW_MATERIALS:
        return RAW_MATERIALS[material_id]["market_price"]
    info = get_material_info(material_id)
    if info is None:
        return 0.0
    return sum(raw_input_cost(input_id) * qty for input_id, qty in info.get("inputs", {}).items())


def _by_drop_chance(material_ids) -> list[str]:
    return sorted(material_ids, key=lambda m: RAW_MATERIALS[m]["drop_chance"], reverse=True)


def _by_cost(material_ids) -> list[str]:
    return sorted(material_ids, key=raw_input_cost)


# Everything a drill can produce, commonest first - ores, then gemstones. A
# drill only ever mines raw materials, so this is the canonical order for any
# breakdown of what one produced (utils/drills.py: material_breakdown_lines),
# the same "commonest first" rule INVENTORY_CATEGORIES applies to the raw and
# gemstone sections of /inventory.
RAW_MATERIAL_ORDER: tuple[str, ...] = tuple(_by_drop_chance(ORES) + _by_drop_chance(GEMSTONES))


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
# Whether the conversion is value-neutral depends on which focus, and 1.3's
# static prices changed the answer. The three ores' drop_chance x market_price
# now come to 0.005667 for iron ore, 0.005667 for copper ore and 0.0044955 for
# coal, so:
#
#   - IRON and COPPER focus are now exactly value-neutral. Rounding the prices
#     to the cent put copper ore at twice iron ore, which is also the rarity
#     ratio the conversion uses, so the two ratios coincide and the trade is
#     even in effort AND in money. Before 1.3 this leaned about +6% for iron
#     and -6% for copper.
#   - COAL focus loses 14.8% of the stream's market value. Coal's price ratio
#     to iron ore is 3 while its rarity ratio is 3.78, so converting at rarity
#     hands back less than it takes. This is the one focus where the two
#     disagree, and it is a wider gap than any focus had before 1.3.
#
# Left as the rarity ratio rather than retuned to the price ratio: "coal drops
# a bit under four times less often, so four iron ore become one coal" is a
# rule a player can hold in their head, and a coal focus is chosen for fuel
# rather than for resale value anyway.
#
# Three consequences that are not obvious from the table and should be stated
# plainly wherever a player is choosing:
#
#   - COPPER and COAL focus make steel impossible to self-supply. Steel needs
#     iron ore, and neither produces any. That rules out every steel component,
#     both steel drill bits, the Steel Container, and diamonds via the press.
#   - IRON focus is a weaker help to steel than it looks. It takes the stream to
#     7.56 iron ore per coal, and steel wants 4:1 once the furnace's own fuel
#     coal is counted (20 ore + 4 coal + FURNACE_COAL_COST_PER_UNIT), so COAL
#     becomes the binding input rather than ore - steel per item mined improves
#     by 5.8%, not 100%. Mining Efficiency is what unlocks the rest of that
#     doubling; see docs/mining-efficiency.md.
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


# ---------------------------------------------------------------------------
# Mining efficiency
# ---------------------------------------------------------------------------

# A player who has reached the obsidian stage can commit to one SMELTED
# material. Where a focus decides which ore arrives, an efficiency decides how
# much of it arrives and in what proportion - see docs/mining-efficiency.md.
#
# It is a SEPARATE feature from the focus, not a second half of it: it does not
# require one, is not gated behind one, and its options (three smelted
# materials) do not correspond to the focus options (four ores). The two
# multiply - Iron efficiency on an Iron & Coal focus is +335% over a player
# with neither - but each works on its own.
#
# Two steps, in order, applied to the raw materials the chosen recipe uses:
#
#   BOOST every one of them by MINING_EFFICIENCY_BOOST, then CORRECT by
#   converting up to MINING_EFFICIENCY_CORRECTION_CAP of whichever is in
#   surplus into whichever is short, at the same rarity ratio a focus uses,
#   stopping the moment the recipe's exact ratio is reached.
#
# They are deliberately two knobs rather than one multiplier. The boost sets
# the floor on what the feature is worth and the cap decides how much of a haul
# is left over; neither can break the other.

# The floor the boost buys: the weakest live combination (Steel efficiency on a
# Balance focus) comes out at +105.6% over the same focus without efficiency,
# and every other combination beats it. "At least double, whatever you picked"
# is the whole promise of the feature and this is the number that keeps it.
MINING_EFFICIENCY_BOOST = 1.0

# Why the correction is CAPPED, and why this cap is not a tuning nicety.
#
# Correcting all the way to the exact ratio every time would give 100% of the
# haul consumed in every combination, which looks strictly better. It is not.
# A rarity-ratio conversion is even in mining effort, so a full correction
# erases the difference between focuses - Coal focus, which converts everything
# into the densest ore, becomes a universal wildcard equal to the matched focus
# for every recipe (Iron on Coal focus and Iron on Iron & Coal both land on
# 2467.2 units per 10,000 mined). Mining Focus stops being a choice.
#
# At 20% that collapse does not happen: Iron on a Coal focus is 680.0 against
# Iron & Coal's 2467.2. The cap is what keeps the other feature alive, so treat
# it as load-bearing rather than as a spare dial.
#
# It is not an idle limit either - it binds in four of the six live
# combinations. The two that reach the exact ratio first stop there: Iron on
# Iron & Coal at 17.68% and Steel on Balance at 2.8%.
MINING_EFFICIENCY_CORRECTION_CAP = 0.20

# Ten times what a focus costs (a ruby is one per 11,111 items mined, an
# obsidian one per 111,111). Deliberately a tier above rather than beside it:
# this is a mid-to-late game feature, and doubling a player's output is worth
# more than re-aiming it.
MINING_EFFICIENCY_UNLOCK_COST = {"obsidian": 1}
MINING_EFFICIENCY_SWITCH_PER_DAY = 1

DEFAULT_MINING_EFFICIENCY = "none"

def recipe_true_inputs(material_id: str) -> dict[str, int]:
    """What one unit of a smelted material ACTUALLY costs at the furnace: its
    recipe plus the flat coal the furnace burns per item smelted.

    Every ratio in this feature is measured against this rather than against
    SMELTED_MATERIALS[...]["inputs"], because that fuel coal is the only reason
    Iron and Copper need coal at all - without it two of the three efficiencies
    would have no second material and no ratio to correct."""
    inputs = dict(SMELTED_MATERIALS[material_id]["inputs"])
    inputs["coal"] = inputs.get("coal", 0) + FURNACE_COAL_COST_PER_UNIT
    return inputs


# `produces` is the smelted material whose recipe decides which raw materials
# are boosted and which way the correction runs. Names and icons are taken from
# SMELTED_MATERIALS rather than written out again, so retuning a recipe can
# never leave this table describing the old one.
MINING_EFFICIENCIES = {
    "none": {
        "name": "None", "emoji": "⚖️", "produces": None,
        "blurb": "No boost. Everything you mine arrives exactly as your focus leaves it.",
    },
    **{
        material_id: {
            "name": SMELTED_MATERIALS[material_id]["name"],
            "emoji": SMELTED_MATERIALS[material_id]["emoji"],
            "produces": material_id,
            # Derived, not written out: the ratio a player is told to expect is
            # read from the recipe itself, so retuning one can't leave the
            # picker quoting the old numbers.
            "blurb": (
                "Doubles the "
                + " and ".join(
                    RAW_MATERIALS[raw_id]["name"] for raw_id in recipe_true_inputs(material_id)
                )
                + " you collect, then converts a little of whichever you have too much of "
                + "into the other - toward the "
                + ":".join(str(q) for q in recipe_true_inputs(material_id).values())
                + f" the furnace wants for {SMELTED_MATERIALS[material_id]['name']}."
            ),
        }
        for material_id in ("iron", "copper", "steel")
    },
}


def efficiency_correction(
    amounts: dict[str, float], needed: dict[str, int]
) -> tuple[str, str, float]:
    """Which material is short, which is in surplus, and what fraction of the
    surplus to convert: the exact ratio, or the cap, whichever is smaller.

    Capping at the exact ratio rather than always converting a flat percentage
    is what removes the cliff an earlier draft had. Converting a fixed 20%
    overshot for Iron and put a hard ceiling at 24.36%, past which the feature
    produced LESS Iron than not having it - it was draining the fuel coal the
    furnace needed. Stopping on arrival means raising the cap can never reduce
    output; it can only stop helping sooner.
    """
    short, surplus = sorted(needed, key=lambda k: amounts[k] / needed[k])
    if amounts[surplus] <= 0:
        return short, surplus, 0.0
    # Solving amounts[short] + moved * rate == target_ratio * (amounts[surplus]
    # - moved) for moved, as a fraction of amounts[surplus].
    rate = focus_conversion_rate(surplus, short)
    target_ratio = needed[short] / needed[surplus]
    numerator = target_ratio * amounts[surplus] - amounts[short]
    denominator = amounts[surplus] * (rate + target_ratio)
    exact = 0.0 if denominator <= 0 else numerator / denominator
    return short, surplus, max(0.0, min(MINING_EFFICIENCY_CORRECTION_CAP, exact))


def apply_mining_efficiency(
    efficiency_id: str, breakdown: dict[str, int], carries: dict[str, float] | None = None
) -> tuple[dict[str, int], dict[str, float]]:
    """Boosts and re-proportions a haul according to a mining efficiency.
    Returns the new breakdown and the fraction of each material still owed.

    Only the chosen recipe's own raw inputs are touched. A player on an Iron
    efficiency still mines copper ore at exactly the normal rate, and gemstones
    are never affected by any of this - the same promise a focus makes.

    THE CARRIES ARE PER MATERIAL, unlike the single float user_mining_focus
    keeps. A focus has exactly one primary, so every fraction it owes is a
    fraction of the same ore; here the correction produces fractions on both
    sides at once, and WHICH materials those are depends on the player's focus.
    One shared carry would pay a fraction of a coal out as iron ore the next
    time the direction flipped.

    Keyed that way they are direction-agnostic - a fraction of an iron ore is
    owed as an iron ore whichever way the correction later runs - so unlike a
    focus's carry they would survive a change correctly and are cleared anyway,
    as a clean slate on a paid, once-a-day action. That costs a player at most
    a fraction of one item and cannot be gamed in either direction, since a
    carry never holds a whole one.
    """
    produces = MINING_EFFICIENCIES[efficiency_id]["produces"]
    carries = dict(carries or {})
    if produces is None or not breakdown:
        return dict(breakdown), carries

    needed = recipe_true_inputs(produces)
    amounts = {k: float(breakdown.get(k, 0)) for k in needed}
    if not any(amounts.values()):
        return dict(breakdown), carries

    for material_id in amounts:
        amounts[material_id] *= 1 + MINING_EFFICIENCY_BOOST

    short, surplus, fraction = efficiency_correction(amounts, needed)
    if fraction > 0:
        moved = amounts[surplus] * fraction
        amounts[surplus] -= moved
        amounts[short] += moved * focus_conversion_rate(surplus, short)

    # Each material is accrued against its own carry, so twenty single-item
    # collections come to exactly what one twenty-item collection does.
    converted = dict(breakdown)
    for material_id, amount in amounts.items():
        whole, carries[material_id] = accrue(carries.get(material_id, 0.0), amount)
        if whole:
            converted[material_id] = whole
        else:
            converted.pop(material_id, None)
    return converted, carries


# What fraction of a recipe the scrapper hands back (see scrap_yield).
SCRAP_RETURN_RATE = 0.5


def scrap_yield(material_id: str) -> dict[str, int]:
    """What the scrapper returns for one unit of `material_id`: half of its
    recipe's DIRECT inputs, rounded down, and never less than one of the
    recipe's single most valuable input. Returns {} for anything with no recipe.

    Only one tier is undone per scrap, so a Drill Chassis yields iron and
    copper, and those have to be scrapped again to reach ore. That keeps every
    step legible - a player can read the recipe book and know what they will
    get - and it means intermediate goods stay recoverable. (Drills themselves
    are the one exception: see drill_scrap_yield, which /scrapper drill uses
    instead of calling this directly on a drill.)

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


def drill_scrap_yield(drill_id: str) -> dict[str, int]:
    """What /scrapper drill actually pays out for one drill - different from,
    and used instead of, plain scrap_yield(drill_id).

    A drill's recipe is one wiring, one chassis and one bit, so
    scrap_yield(drill_id) itself can only apply its guaranteed-unit rule and
    hand back exactly one of those three whole (see ScrapYieldShapeTests).
    Handing back a whole chassis or wiring is a problem specific to drills:
    unlike every other guaranteed-unit case, that item slots straight into
    another drill's recipe at no further cost, so scrapping one drill and
    crafting the missing two parts would rebuild it at a discount.
    Wiring and chassis are decomposed one tier further instead, via
    scrap_yield on each of THEM, so what comes back is their own raw inputs
    (still halved and rounded down) rather than a reusable component. The bit
    is kept whole and added on top - it isn't a shared drill part, so there is
    no reuse to prevent - which is also what makes this return strictly more
    than scrap_yield(drill_id)'s single guaranteed unit did.
    """
    inputs = DRILLS[drill_id]["inputs"]
    bit_id = next(i for i in inputs if i not in ("wiring", "drill_chassis"))

    out: dict[str, int] = {}
    for component_id in ("wiring", "drill_chassis"):
        for material_id, quantity in scrap_yield(component_id).items():
            out[material_id] = out.get(material_id, 0) + quantity * inputs[component_id]
    out[bit_id] = out.get(bit_id, 0) + inputs[bit_id]
    return out


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
# rather than a display one. A ruby's price is 5,500 against iron ore at 0.01,
# so one gem sale minted more currency than a server's entire population could
# earn by playing the game - and because a gemstone's target stock stays at 1 on
# any server of realistic size, the decaying curve that was then meant to damp
# repeated sales barely engaged: the first four ruby sales alone paid 5,500 +
# 2,750 + 1,833 + 1,375. scripts/revert_gem_sales.py is the one-time repair.
#
# 1.3's static prices REMOVE that damping entirely rather than restore it: four
# ruby sales would now pay 5,500 apiece. The exclusion is the only thing
# standing between a gemstone and a server's economy, so treat this list as
# load-bearing rather than cosmetic.
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
# because it is the board that would pay for it: a gemstone's price runs from
# 5,500 to 500,000, so one gem task would pay more than every other source of
# currency in the game combined - and as of 1.3 it would pay that per
# completion, without a daily limit.
#
# Note that JOB_BOARD_TARGET_PAYOUT does NOT protect against this on its own,
# and re-admitting a material on the assumption that it does would be an
# expensive mistake. The payout is met by lowering the quantity, and the
# quantity floors at one unit - so anything worth more than the target payout
# per unit becomes a one-unit task, and one unit of a diamond sale is 500,000
# of goods against a 1.00 bonus. Nothing here is worth more than steel's 0.48
# a unit, which is what makes the rule work.
JOB_BOARD_MATERIALS: tuple[str, ...] = TRADEABLE_ORDER

# What one completion of the day's task pays, on top of what selling the goods
# earned in the first place. The quantity is worked backwards from it: the
# fewest units whose sale clears it. One completion, one dollar, whoever you
# are and whatever the board picked.
#
# As of 1.3 this is paid PER COMPLETION rather than once per player per day
# (utils/job_board.py: credit_job_progress). Selling ten times the task
# quantity pays it ten times, in one command if that is how it was sold. Three
# things bound what that can become:
#
#   * A completion never pays more than the goods are worth. quantity is the
#     fewest units clearing 1.00, so quantity * price >= 1.00 by construction
#     and the bonus is at most a second copy of the sale.
#   * Buying the goods back always costs more than the pair paid out. The buy
#     price is exactly twice the sale price (MARKET_BUY_MARKUP), so a round
#     trip of q units collects q*price + 1.00 <= 2*q*price and spends exactly
#     2*q*price. It breaks even at best - on iron ore and copper ore, where
#     quantity * price is exactly 1.00 - and loses on everything else.
#   * It still requires real production. The only way to claim it is to put
#     materials into the market, which is the same work a plain sale is.
#
# What it is no longer bounded by is a per-day cap, and that is the deliberate
# change: the day's material is now worth roughly twice its market price to
# whoever wants to mine it, with no ceiling on how much of it they bring.
JOB_BOARD_TARGET_PAYOUT = 1.00


def job_quantity(material_id: str) -> int:
    """How many units one completion of the day's task asks for: the fewest
    whose sale pays JOB_BOARD_TARGET_PAYOUT.

    Takes nothing but the material as of 1.3. Both of the arguments it used to
    need - the server's stock and its target stock - were there only to price
    the sale, and the price no longer moves. The task is now the same size for
    a given material on every server on every day, which is also what makes it
    safe to complete repeatedly: nothing about how much has already been sold
    today can change what the next completion costs.

    Worked in cents so the division is exact: a task of 34 coal is
    ceil(100/3), not ceil(1.00/0.029999999999999998).
    """
    price_cents = MARKET_PRICE_CENTS[material_id]
    if price_cents <= 0:
        return 1
    target_cents = round(JOB_BOARD_TARGET_PAYOUT * 100)
    return max(1, -(-target_cents // price_cents))


def pick_job_material(deficits: dict[str, float], rng=random) -> str:
    """Chooses the day's task from each eligible material's weight - target /
    (stock + target), so a hundred-member server's numbers are on the same
    scale as a five-member one's. Emptied out (stock=0) is the maximum weight
    of 1.0; sitting exactly at target is 0.5, not a hard 0.0; weight keeps
    falling toward (but mathematically never reaches) 0 as stock climbs past
    target, so nothing is ever fully out of the running.

    Weighted rather than simply picking the largest weight, because a
    deterministic maximum parks the board on one material until the server
    catches up - and a server that cannot produce that material at all yet (a
    brand new one and steel, say) would get the same impossible task every day
    forever, which is the one failure mode a DAILY task must not have.
    """
    weights = [deficits.get(m, 0.0) for m in JOB_BOARD_MATERIALS]
    return rng.choices(JOB_BOARD_MATERIALS, weights=weights, k=1)[0]
