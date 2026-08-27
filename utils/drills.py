"""
utils/drills.py

Shared helpers for working with drill instances. Since drills became
individually tracked rows rather than fungible stacks, both cogs/mining.py and
cogs/factory.py need the same three things: read a drill's effective stats off
its row, describe a drill in one line, and let the user pick one of their
drills by ID from an autocomplete list.

On trusting the autocomplete value: Discord does NOT enforce that the value a
user submits came from the choices we offered, and drill_ids are small
sequential integers, so anyone can guess someone else's. Every command that
takes a drill must therefore re-check ownership server-side via fetch_drill()
rather than trusting that the ID came from its own autocomplete. Note that
drill_ids are no longer shown to players anywhere - they're an internal
identifier that rides along as the autocomplete's hidden value - but that is a
presentation choice and NOT what makes any of this safe. fetch_drill is.
"""
import enum
import logging

from discord import app_commands

from database.db import Database
from data.materials import (
    DRILLS,
    RAW_MATERIAL_ORDER,
    STORAGE_CONTAINERS,
    effective_capacity,
    effective_rate,
    get_material_info,
    roll_raw_material,
)
from utils.db_helpers import ensure_user_row, adjust_user_quantity
from utils.mining_focus import convert_haul

log = logging.getLogger("dragonhoard")

# Discord truncates a choice name past 100 characters; leave room for the
# server name, which is the only unbounded part of a label.
_MAX_CHOICE_NAME = 100
# Autocomplete accepts at most 25 results, so a player with more drills than
# this sees the most valuable ones and narrows down by typing.
MAX_AUTOCOMPLETE_RESULTS = 25


def capacity_of(drill_row) -> int:
    """How much this drill can hold, container included."""
    return effective_capacity(drill_row["container_type"])


def rate_of(drill_row) -> float:
    """How fast this drill mines, in items per hour, at its current level."""
    return effective_rate(drill_row["drill_type"], drill_row["level"])


def drill_name(drill_row) -> str:
    return DRILLS[drill_row["drill_type"]]["name"]


def drill_emoji(drill_row) -> str:
    return DRILLS[drill_row["drill_type"]]["emoji"]


def container_name(container_type: str | None) -> str:
    return STORAGE_CONTAINERS[container_type]["name"] if container_type else "No Container"


def container_emoji(container_type: str | None) -> str:
    """The container's own glyph, or nothing at all when none is fitted - so a
    bare drill's cell is just the drill, not a gap or a placeholder."""
    return STORAGE_CONTAINERS[container_type]["emoji"] if container_type else ""


def drill_cell(drill_row) -> str:
    """The compact form used in grids: the drill's emoji, its container's emoji
    if one is fitted, and its level. Sized to sit alongside other drills in an
    /inventory row rather than to be read on a line of its own, which is why it
    carries no name, no fill and no location."""
    return f"{drill_emoji(drill_row)}{container_emoji(drill_row['container_type'])} Lv.{drill_row['level']}"


def drill_short_label(drill_row) -> str:
    """Just which drill it is and how far it's leveled - no container, no
    location, no fill.

    For sentences that are ABOUT one of those things, or that describe a change
    that has already been committed. The row a command is holding was read
    before its own UPDATE, so a full drill_label there would name the container
    that was just swapped out, or call a drill "inventory" in the same breath as
    announcing it was placed."""
    return f"{drill_name(drill_row)} Lv.{drill_row['level']}"


def drill_label(drill_row, location: str | None = None, *, with_emoji: bool = False) -> str:
    """One line describing a drill: which one it is, how far it's been
    upgraded, what it's carrying and where it lives. `location` is the server
    name for a placed drill; pass None to have it read as inventory.

    Deliberately carries no drill_id. Two drills that match on every part of
    this label are interchangeable - same type, same level, same container,
    both unplaced - so telling them apart buys the player nothing, and a raw
    row id in a game menu is noise."""
    parts = [drill_name(drill_row), f"Lv.{drill_row['level']}"]

    if drill_row["container_type"]:
        parts.append(container_name(drill_row["container_type"]))

    if drill_row["guild_id"] is None:
        parts.append("inventory")
    else:
        parts.append(f"{drill_row['stored_amount']}/{capacity_of(drill_row)}")
        if location:
            parts.append(location)

    if drill_row["locked_job_id"] is not None:
        parts.append("upgrading")

    label = " · ".join(parts)
    if with_emoji:
        label = f"{drill_emoji(drill_row)} {label}"
    return label[:_MAX_CHOICE_NAME]


async def fetch_drill(db: Database, drill_id: int, owner_id: int):
    """Loads a drill only if it really belongs to this user. Every command
    that accepts a drill ID goes through here - see the module docstring on
    why the autocomplete list can't be trusted to have produced it."""
    return await db.fetchone(
        "SELECT * FROM drills WHERE drill_id = ? AND owner_id = ?",
        (drill_id, owner_id),
    )


class DrillScope(enum.Enum):
    """Which of a player's drills a command is willing to act on.

    This replaced a pair of booleans (unplaced_only / guild_id) that were read
    as mutually exclusive branches, and so between them could not express LOCAL
    - the "in your inventory, or placed right here" set that /factory upgrade
    and /mine attach both need. Stacking a third flag onto two that already
    interacted was the alternative, and one enum says what each caller means.
    """

    ANY = "any"            # every drill this user owns, wherever it is
    UNPLACED = "unplaced"  # sitting in inventory
    PLACED_HERE = "here"   # placed in guild_id
    LOCAL = "local"        # unplaced OR placed in guild_id


def is_local_drill(drill_row, guild_id: int | None) -> bool:
    """A drill this player can act on from this server: sitting in their
    inventory, or placed in the server the command was run in.

    The predicate DrillScope.LOCAL expresses in SQL, for use after fetch_drill.
    Narrowing an autocomplete list is presentation; this is the enforcement,
    because the value a user submits need never have come from the list we
    offered (see the module docstring).
    """
    return drill_row["guild_id"] is None or drill_row["guild_id"] == guild_id


async def drill_choices(
    db: Database,
    owner_id: int,
    current: str,
    *,
    scope: DrillScope = DrillScope.ANY,
    guild_id: int | None = None,
    exclude_locked: bool = True,
    require_container: bool = False,
    guild_names: dict[int, str] | None = None,
) -> list[app_commands.Choice[int]]:
    """Builds the drill list for an autocomplete callback, restricted to
    `scope` (see DrillScope)."""
    if scope in (DrillScope.PLACED_HERE, DrillScope.LOCAL) and guild_id is None:
        raise ValueError(f"DrillScope.{scope.name} needs a guild_id")

    conditions = ["owner_id = ?"]
    params: list = [owner_id]

    if scope is DrillScope.UNPLACED:
        conditions.append("guild_id IS NULL")
    elif scope is DrillScope.PLACED_HERE:
        conditions.append("guild_id = ?")
        params.append(guild_id)
    elif scope is DrillScope.LOCAL:
        conditions.append("(guild_id IS NULL OR guild_id = ?)")
        params.append(guild_id)

    if exclude_locked:
        conditions.append("locked_job_id IS NULL")
    if require_container:
        conditions.append("container_type IS NOT NULL")

    rows = await db.fetchall(
        f"SELECT * FROM drills WHERE {' AND '.join(conditions)} "
        f"ORDER BY level DESC, drill_id ASC",
        tuple(params),
    )

    search = current.strip().lower()
    choices = []
    for row in rows:
        location = (guild_names or {}).get(row["guild_id"])
        label = drill_label(row, location)
        if search and search not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label, value=row["drill_id"]))
        if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
            break
    return choices


def guild_name_map(bot, drill_rows) -> dict[int, str]:
    """Server names for whichever servers a set of drills is placed in, so
    labels can say where a drill lives. Servers the bot can no longer see fall
    back to their ID rather than dropping the location entirely."""
    names = {}
    for row in drill_rows:
        guild_id = row["guild_id"]
        if guild_id is None or guild_id in names:
            continue
        guild = bot.get_guild(guild_id)
        names[guild_id] = guild.name if guild else f"server {guild_id}"
    return names




def build_material_breakdown(total_items: int, roll_material=roll_raw_material) -> dict[str, int]:
    """Rolls `total_items` individual drops and tallies them per material.

    This used to be how a haul was decided: a drill banked a plain count while
    it mined and only worked out WHAT it had mined when the count was handed
    over. As of 1.2 a drill knows what it holds the moment it mines it
    (drill_contents), because the server pool it draws from has a real finite
    composition and a guaranteed diamond in a shared pool cannot be drawn
    per-player at collection time.

    What survives here is the repair path in take_drill_contents, plus the 1.2
    migration that gave existing drills their composition - both of which need
    exactly this: turn a bare count into a plausible haul under the old
    independent-roll rules."""
    breakdown: dict[str, int] = {}
    for _ in range(max(0, total_items)):
        material_id = roll_material()
        breakdown[material_id] = breakdown.get(material_id, 0) + 1
    return breakdown


async def add_drill_contents(tx, drill_id: int, drawn: dict[str, int]) -> None:
    """Adds a tick's harvest to what a drill is holding. Must run in the same
    transaction as the matching drills.stored_amount update - the two are one
    fact stored twice (see drill_contents in schema.sql)."""
    for material_id, quantity in drawn.items():
        if quantity <= 0:
            continue
        await tx.execute(
            "INSERT INTO drill_contents (drill_id, material_id, quantity) VALUES (?, ?, ?) "
            "ON CONFLICT(drill_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity",
            (drill_id, material_id, quantity),
        )


async def take_drill_contents(tx, drill_row) -> dict[str, int]:
    """Reads what a drill is holding and clears it, in one transaction with
    whatever is about to credit it.

    Falls back to rolling the difference if the rows come to LESS than
    stored_amount. That shortfall shouldn't happen - every write to one goes in
    the same transaction as the write to the other - but the failure it guards
    is a player watching a full drill hand them nothing, and material the drill
    genuinely mined is not something to lose to a bookkeeping disagreement. It
    is also the path a drill that predates 1.2 takes if it somehow reaches here
    without the migration having filled it in.

    A surplus is left alone deliberately: stored_amount is what capacity and
    is_full are enforced against, so crediting more than it says would let a
    drill hand over more than it was ever allowed to hold.
    """
    rows = await tx.fetchall(
        "SELECT material_id, quantity FROM drill_contents WHERE drill_id = ? AND quantity > 0",
        (drill_row["drill_id"],),
    )
    contents = {row["material_id"]: row["quantity"] for row in rows}

    shortfall = drill_row["stored_amount"] - sum(contents.values())
    if shortfall > 0:
        log.warning(
            "Drill %s holds %d items but only %d are itemised - rolling the difference.",
            drill_row["drill_id"], drill_row["stored_amount"], sum(contents.values()),
        )
        for material_id, quantity in build_material_breakdown(shortfall).items():
            contents[material_id] = contents.get(material_id, 0) + quantity

    await tx.execute("DELETE FROM drill_contents WHERE drill_id = ?", (drill_row["drill_id"],))
    return contents


def material_breakdown_lines(breakdown: dict[str, int], totals: dict[str, int] | None = None) -> list[str]:
    """One line per material in a haul, commonest first (data/materials.py:
    RAW_MATERIAL_ORDER), so the same haul always renders in the same order.
    Shared by /mine remove, /collect and the /factory upgrade receipt - all
    three only ever show what a drill produces, which is exactly what that
    order covers.

    Pass `totals` to append what the player now holds of each, in the same
    "(N total)" shape the queue receipts use for what's left after a deduction
    (utils/receipts.py: _material_line). A haul is the one moment a player most
    wants that number, and without it /collect was reliably followed by
    /inventory to find out.

    A material missing from `totals` falls back to the plain form rather than
    claiming a total of zero - the player just received some of it, so zero is
    the one answer that can't be true."""
    order = {material_id: rank for rank, material_id in enumerate(RAW_MATERIAL_ORDER)}
    lines = []
    for material_id, quantity in sorted(breakdown.items(), key=lambda item: order.get(item[0], len(order))):
        info = get_material_info(material_id)
        if not info:
            continue
        line = f"{info['emoji']} **{quantity:,} {info['name']}**"
        if totals and material_id in totals:
            line += f" ({totals[material_id]:,} total)"
        lines.append(line)
    return lines


async def retract_drill(tx, drill_row) -> dict[str, int] | None:
    """Pulls a placed drill back to its owner's inventory and credits whatever
    it was holding. Returns the breakdown credited (possibly empty, if the
    drill was empty), or None if a racing command emptied it first.

    The unplace goes FIRST and carries stored_amount in its WHERE clause. If
    another command - a /collect, another retraction - already emptied this
    drill, the guarded UPDATE matches nothing, this returns None and the credit
    never runs. Crediting first would hand out a second copy of the same haul,
    which is exactly the bug /mine remove used to have: it read the drill
    outside its transaction and unplaced it with no stored_amount guard, so a
    /collect committing in between paid the haul out twice.

    All three columns move in one statement because the drills table CHECKs
    that an unplaced drill holds nothing - setting guild_id on its own fails
    the constraint and rolls the caller's whole transaction back.

    Level, container and harvest_progress ride along untouched: they're
    properties of the drill, and clearing them would only ever cost the player.

    Must be handed a Transaction rather than the Database. The unplace and the
    credit are one operation, and the materials in a drill exist nowhere but its
    own rows - a failure between the two either duplicates the haul or destroys
    it.

    The unplace runs BEFORE the contents are read, so a racing command that
    already emptied this drill leaves nothing to read and nothing to credit.
    The player's mining focus is applied on the way out, exactly as /collect
    applies it - pulling a drill early is a collection, and shouldn't be a way
    to receive ore the focus says you no longer mine.
    """
    changed = await tx.execute_changes(
        "UPDATE drills SET guild_id = NULL, stored_amount = 0, is_full = 0 "
        "WHERE drill_id = ? AND guild_id = ? AND stored_amount = ?",
        (drill_row["drill_id"], drill_row["guild_id"], drill_row["stored_amount"]),
    )
    if not changed:
        return None

    breakdown = await take_drill_contents(tx, drill_row)
    breakdown = await convert_haul(tx, drill_row["owner_id"], breakdown)
    if breakdown:
        await ensure_user_row(tx, drill_row["owner_id"])
        for material_id, quantity in breakdown.items():
            await adjust_user_quantity(tx, drill_row["owner_id"], material_id, quantity)
    return breakdown


async def set_container(db, drill_row, container_type: str | None):
    """Fits or removes a container and re-derives is_full against the new
    capacity in the same statement.

    That recompute is not optional. The harvest loop only ever selects drills
    with is_full = 0, so a full drill that gains a container would never mine
    again; and a drill that loses one while holding more than its bare capacity
    would sit in the loop's result set forever, doing nothing while /mine status
    called it "mining".

    The container_type guard makes the swap safe to race: if another command
    changed the container first, this matches nothing and its transaction rolls
    back rather than returning the wrong item."""
    return await db.execute_changes(
        "UPDATE drills SET container_type = ?, "
        "is_full = CASE WHEN stored_amount >= ? THEN 1 ELSE 0 END "
        "WHERE drill_id = ? AND container_type IS ?",
        (
            container_type,
            effective_capacity(container_type),
            drill_row["drill_id"],
            drill_row["container_type"],
        ),
    )


async def release_stale_drill_locks(db):
    """Frees any drill still pointing at a job that has already finished or no
    longer exists, which would otherwise lock that drill out of every command
    forever.

    Queueing and completing a lock-taking job - a /factory upgrade or a
    /scrapper drill - are each one transaction, so this shouldn't find
    anything. It stays as a cheap backstop for rows left behind by the
    pre-transaction code, and for anything that manages to desync the two in
    future. Deliberately job-type-agnostic: it reads locked_job_id against
    production_jobs and doesn't care which machine holds the lock."""
    await db.execute(
        "UPDATE drills SET locked_job_id = NULL WHERE locked_job_id IS NOT NULL "
        "AND locked_job_id NOT IN (SELECT job_id FROM production_jobs WHERE status != 'complete')"
    )


def describe_cost(cost: dict[str, int]) -> str:
    """Renders an upgrade recipe as one inline string, e.g. "🧰 2 , ⛏ 20".
    Thousands separators because the cost doubles every level and reaches
    six figures well before the exponent makes it unaffordable."""
    parts = []
    for material_id, quantity in cost.items():
        info = get_material_info(material_id)
        emoji = info["emoji"] if info else "❓"
        parts.append(f"{emoji} {quantity:,}")
    return " , ".join(parts)
