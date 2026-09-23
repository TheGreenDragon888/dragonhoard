"""
utils/production_ledger.py

The record of goods PRODUCED, and the one place the rule for valuing them
lives. server_config has held the currency side of the economy since 1.1 -
minted, burned, and each machine's lifetime fees - but nothing recorded output,
so /economy gdp's figures had no data to sum until this table existed. The model
it serves (value added, attributed by location) is argued in docs/market.md
section 5; this module is only its implementation.

Three things about it are easy to get wrong and are therefore fixed here rather
than at the eleven call sites:

  * VALUE ADDED IS DERIVED, NEVER STORED. A row carries output_value and
    input_value; every read subtracts them. Storing the difference too would be
    a second copy of one number, free to drift the moment prices are retuned -
    the same reasoning that keeps collected fees summed on read rather than
    banked in a column (utils/db_helpers.py: FEES_COLLECTED_COLUMNS).

  * ONLY WHAT THE MARKET PRICES IS VALUED. Ores and smelted materials have a
    market_price; components, drills, containers and ultra dense matter
    deliberately do not (docs/market.md section 3), so they contribute 0 to
    whichever side of a row they land on rather than an invented figure. That
    is why the factory records real input and no output, and the scrapper the
    reverse, and why neither is in GDP_SOURCES.

  * EVERY WRITE BELONGS INSIDE ITS EVENT'S OWN TRANSACTION. A ledger row that
    can commit without the inventory change it describes is a ledger that
    drifts, so every function here takes a Transaction and none of them opens
    one.
"""
from datetime import datetime, timedelta, timezone

from typing import NamedTuple

from database.db import _Executor
from data.materials import (
    GEMSTONES,
    get_material_info,
    recipe_true_inputs,
)
from utils.db_helpers import MACHINES

# Every kind of work a ledger row can describe. Mining plus the five machines,
# built from MACHINES for the same reason FEES_COLLECTED_COLUMNS is - a sixth machine
# starts being recorded by being added to that tuple. Kept in step with the
# CHECK constraint on production_ledger.source.
LEDGER_SOURCES: tuple[str, ...] = ("mining",) + MACHINES

# Which of those count toward GDP, and it is deliberately not all of them.
#
# GDP is the sum of value ADDED, which needs a price for the output as well as
# for the inputs - and only ores and smelted materials have one. Mining, the
# furnace and the blast furnace produce exactly those; the factory, press and
# scrapper produce components, drills, containers and gemstones, none of which
# the market prices. Valuing those would mean inventing a number and putting it
# in the headline figure, and the property that makes GDP worth quoting at all
# is that every figure in it is a price the market will actually honour, which
# is what makes two servers' GDP comparable (docs/market.md section 5).
#
# Their rows are still WRITTEN, and read back for the import/export line below.
# The decision is about what is summed, not about what is recorded, so changing
# it later is a change to this tuple and needs no backfill.
GDP_SOURCES: tuple[str, ...] = ("mining", "furnace", "blast_furnace")

# The two windows /economy gdp reports, in hours, and the ops dashboard reads the
# same two (web/queries.py) so the figure on the dashboard is the figure a
# player sees rather than a lookalike computed twice.
#
# Rolling from now rather than aligned to the job board's Arizona midnight.
# The board's day is deliberately in a timezone that means something to the
# people playing (docs/market.md section 1); a rolling window has no such
# reason, and an aligned "today" would read as almost nothing for the first
# hours after every reset - which is exactly the "this number looks wrong"
# reaction the dashboard's tracked-since note exists to head off.
GDP_DAY_HOURS = 24
GDP_WEEK_HOURS = 24 * 7

# How long a row is kept. Comfortably past the longest window /economy gdp shows
# (7 days), leaving three months for a future graph, and bounded because this
# table is the only one in the schema that grows with PLAY rather than with
# players: each machine's loop can append a row per job it touches per
# PROCESS_TICK_MINUTES tick, on top of a row group per /collect.
LEDGER_HISTORY_DAYS = 90

# SQLite's own datetime('now') format, which is what occurred_at's DEFAULT
# writes. Cutoffs are compared against that column as TEXT, so they have to be
# formatted identically - and this layout sorts chronologically as a plain
# string, which is what makes the comparison valid at all. Same property
# job_date relies on (utils/job_board.py).
_SQLITE_TIMESTAMP = "%Y-%m-%d %H:%M:%S"

# The last date a prune ran, so the DELETE below happens about once a day
# rather than on every single production event. Process-local on purpose: it is
# an optimisation, not a correctness guard - a restart simply prunes once more
# than it strictly had to, and a bot that never restarts still prunes daily.
_last_pruned: str | None = None


def utc_now() -> datetime:
    """The clock every timestamp in this table is on.

    UTC rather than the job board's Arizona clock, matching occurred_at's
    datetime('now') DEFAULT. The two are deliberately different: the board's
    day is a thing players experience and so belongs in their timezone, while
    a rolling 24-hour window is the same 24 hours wherever it is read from."""
    return datetime.now(timezone.utc)


def window_cutoff(hours: float, now: datetime | None = None) -> str:
    """The occurred_at value a row must be at or after to fall inside a window
    of the last `hours` hours.

    Takes `now` so a test can pin the boundary at a chosen instant rather than
    only at whatever time the suite happens to run - the same reason
    hours_until_reset does."""
    now = now or utc_now()
    return (now - timedelta(hours=hours)).strftime(_SQLITE_TIMESTAMP)


def market_value(material_id: str, quantity: int) -> float:
    """What `quantity` of a material is worth at the market's own price.

    0 for anything the market does not price. That is the honest answer rather
    than a placeholder: components, drills, containers and ultra dense matter
    are excluded from the market on purpose (docs/market.md section 3), so
    there is no figure to quote and inventing one would put a made-up number
    into a headline statistic.
    """
    info = get_material_info(material_id)
    if info is None:
        return 0.0
    return (info.get("market_price") or 0.0) * quantity


def inputs_value(inputs: dict[str, int]) -> float:
    """The market value of a whole set of consumed inputs."""
    return sum(market_value(material_id, quantity) for material_id, quantity in inputs.items())


def smelting_inputs(material_id: str, items: int) -> dict[str, int]:
    """What smelting `items` units of a material actually consumes: the recipe,
    plus the flat coal the furnace burns per item.

    recipe_true_inputs rather than SMELTED_MATERIALS[...]["inputs"] because
    that fuel coal is genuinely burned, and leaving it out overstates smelting's
    value added by 0.03 an item - which on Iron is more than the value added
    itself (0.02 against 0.05). tests/test_production_ledger.py pins all three.

    Correct for the blast furnace as well as the furnace, which is why it takes
    ITEMS rather than a job's quantity: a blast furnace batch is every furnace
    figure multiplied by BLAST_FURNACE_BATCH_SIZE, inputs and fuel coal alike
    (data/materials.py), so the per-item ratio is identical at both machines.
    """
    return {
        input_id: per_unit * items
        for input_id, per_unit in recipe_true_inputs(material_id).items()
    }


async def prune_ledger(db: _Executor, now: datetime | None = None) -> None:
    """Drops rows older than LEDGER_HISTORY_DAYS, at most once a day per
    process (see _last_pruned).

    Called from the write path rather than from a background loop, on the same
    reasoning the job board prunes inside ensure_todays_job: rows only appear
    when something produced them, so the moment one is written is exactly when
    the table is worth trimming, and a loop would be one more thing to keep
    running.
    """
    global _last_pruned
    now = now or utc_now()
    today = now.date().isoformat()
    if _last_pruned == today:
        return
    _last_pruned = today
    cutoff = (now - timedelta(days=LEDGER_HISTORY_DAYS)).strftime(_SQLITE_TIMESTAMP)
    await db.execute("DELETE FROM production_ledger WHERE occurred_at < ?", (cutoff,))


async def record_output(
    db: _Executor,
    guild_id: int,
    source: str,
    material_id: str,
    quantity: int,
    inputs: dict[str, int] | None = None,
) -> None:
    """Records one production event: `quantity` of `material_id` came out of
    `source` in `guild_id`, having consumed `inputs`.

    `guild_id` is where the WORK happened. For a machine that is the guild
    whose machine ran the job; for mining it is drills.guild_id, the pool the
    ore was drawn from, which is not necessarily where /collect was typed - see
    the note on the table in database/schema.sql.

    Quantities are in ITEMS throughout, including for the blast furnace, whose
    production_jobs rows count batches. What a machine queues in and what it
    produced are different questions and only the second one belongs here.

    Hand this a Transaction, always: the row and the inventory change it
    describes have to commit together.
    """
    if source not in LEDGER_SOURCES:
        raise ValueError(f"unknown production source {source!r}")
    if quantity <= 0:
        return
    await db.execute(
        "INSERT INTO production_ledger "
        "(guild_id, source, material_id, quantity, output_value, input_value, is_gemstone) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            source,
            material_id,
            quantity,
            market_value(material_id, quantity),
            inputs_value(inputs) if inputs else 0.0,
            1 if material_id in GEMSTONES else 0,
        ),
    )
    await prune_ledger(db)


async def record_mined(db: _Executor, guild_id: int, breakdown: dict[str, int]) -> None:
    """Records a haul drawn from one server's mining pool - one row per
    material, no inputs, because mining consumes nothing.

    Gemstone rows go in like any other and are marked is_gemstone; what
    excludes them is the GDP query, not the write. They are counted and
    displayed, just never summed into GDP (docs/market.md section 5).
    """
    for material_id, quantity in breakdown.items():
        await record_output(db, guild_id, "mining", material_id, quantity)


def split_by_guild(
    haul: dict[str, int], weights: dict[int, int]
) -> dict[int, dict[str, int]]:
    """Divides one aggregated haul between the guilds it came from, in
    proportion to `weights`, with every part a whole number and the parts
    summing exactly to the whole.

    This exists because /collect converts a player's WHOLE haul at once - the
    focus's rounding carry is per player, so converting drill by drill would
    give a different answer depending on how many drills someone had going
    (cogs/mining.py). By the time the haul is what landed in the inventory, the
    per-drill guild attribution has been mixed away, and there is no exact
    answer to recover: a coal focus turns three servers' ore into one pile of
    coal. Weighting by each guild's share of the raw ore it contributed is the
    honest reconstruction of that, and it is EXACT in the single-server case,
    which is all but the rarest /collect.

    Largest remainder rather than plain rounding: floor everything, then hand
    the leftover units to the largest fractional parts. Rounding each share
    independently would let the parts sum to more or less than the haul, which
    would make a server's GDP quietly disagree with the ore that produced it.

    Gemstones are routed through here only for a player who has a mining
    affinity. They pass through the focus and the efficiency untouched
    (data/materials.py: apply_mining_focus), so without an affinity each one is
    still attributable to the exact drill it came out of and the caller
    credits it there instead. An affinity pools them exactly as the focus pools
    ore, and the caller then weights by what each guild's gems were WORTH
    rather than by how many there were - one diamond and one ruby are not
    interchangeable the way two iron ore are (cogs/mining.py: collect).
    """
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {}

    guild_ids = list(weights)
    split: dict[int, dict[str, int]] = {guild_id: {} for guild_id in guild_ids}
    for material_id, quantity in haul.items():
        if quantity <= 0:
            continue
        exact = [quantity * weights[guild_id] / total_weight for guild_id in guild_ids]
        floors = [int(share) for share in exact]
        # Hand out what flooring left over, largest fractional part first. The
        # index is the tie-break so the result never depends on dict ordering
        # between two guilds owed the same fraction.
        leftover = quantity - sum(floors)
        order = sorted(
            range(len(guild_ids)),
            key=lambda i: (exact[i] - floors[i], -i),
            reverse=True,
        )
        for i in order[:leftover]:
            floors[i] += 1
        for guild_id, amount in zip(guild_ids, floors):
            if amount:
                split[guild_id][material_id] = amount
    return split


class WindowTotals(NamedTuple):
    """Everything /economy reports about one guild over one window, in one
    query's worth of rows."""

    gdp: float                      # value added, GDP_SOURCES only, gemstones excluded
    added_by_source: dict[str, float]   # the same, broken down - GDP_SOURCES only
    mined_output: float             # market value of what was mined here
    machine_input: float            # market value of what this server's machines consumed
    rows: int                       # non-gemstone rows in the window, so "no data" is knowable


async def window_totals(db: _Executor, guild_id: int, since: str) -> WindowTotals:
    """Sums one guild's ledger over the window starting at `since` (a
    window_cutoff value).

    Grouped by source in a single query rather than one query per figure: the
    breakdown, the headline GDP and both halves of the import/export comparison
    are all the same rows added up differently.

    Gemstones are excluded from every figure here. A single diamond is valued
    at 500,000 against iron ore's 0.01, so one drop would drown a month of
    everybody else's mining and make the number meaningless - they get their
    own field on the embed instead (gem_counts below).
    """
    rows = await db.fetchall(
        "SELECT source, "
        "       SUM(output_value - input_value) AS added, "
        "       SUM(output_value) AS output_value, "
        "       SUM(input_value) AS input_value, "
        "       COUNT(*) AS entries "
        "FROM production_ledger "
        "WHERE guild_id = ? AND is_gemstone = 0 AND occurred_at >= ? "
        "GROUP BY source",
        (guild_id, since),
    )

    added_by_source: dict[str, float] = {}
    mined_output = 0.0
    machine_input = 0.0
    entries = 0
    for row in rows:
        source = row["source"]
        entries += row["entries"]
        if source in GDP_SOURCES:
            added_by_source[source] = row["added"] or 0.0
        if source == "mining":
            mined_output = row["output_value"] or 0.0
        else:
            # Every machine's consumption, not just the ones in GDP. Whether a
            # factory's output can be priced has no bearing on whether the ore
            # it ate was really eaten, and the import/export line is asking
            # about the ore.
            machine_input += row["input_value"] or 0.0

    return WindowTotals(
        gdp=sum(added_by_source.values()),
        added_by_source=added_by_source,
        mined_output=mined_output,
        machine_input=machine_input,
        rows=entries,
    )


async def gem_counts(db: _Executor, guild_id: int, since: str) -> dict[str, int]:
    """How many of each gemstone this server produced in the window.

    Counted rather than valued, which is the whole point of keeping them out of
    GDP: the interesting fact about a diamond is that somebody found one, not
    that it notionally moved the economy by half a million.
    """
    rows = await db.fetchall(
        "SELECT material_id, SUM(quantity) AS quantity FROM production_ledger "
        "WHERE guild_id = ? AND is_gemstone = 1 AND occurred_at >= ? "
        "GROUP BY material_id",
        (guild_id, since),
    )
    return {row["material_id"]: row["quantity"] for row in rows}


async def tracked_since(db: _Executor, guild_id: int) -> str | None:
    """The oldest ledger row this server has, or None if it has never produced
    anything since the table existed.

    Nothing produced before 1.4 was ever recorded and none of it can be
    recovered, so a server that has been running for months reads exactly like
    one installed this morning (docs/market.md section 5). The Ops dashboard
    reports this date for that reason; /economy reads it only as a flag, to
    tell a server with no history at all that there is nothing wrong.
    """
    row = await db.fetchone(
        "SELECT MIN(occurred_at) AS first_seen FROM production_ledger WHERE guild_id = ?",
        (guild_id,),
    )
    return row["first_seen"] if row and row["first_seen"] else None
