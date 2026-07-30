"""
utils/drills.py

Shared helpers for working with drill instances. Since drills became
individually tracked rows rather than fungible stacks, both cogs/mining.py and
cogs/factory.py need the same three things: read a drill's effective stats off
its row, describe a drill in one line, and let the user pick one of their
drills by ID from an autocomplete list.

On trusting the autocomplete value: Discord does NOT enforce that the value a
user submits came from the choices we offered, and drill_ids are small
sequential integers, so anyone can type someone else's. Every command that
takes a drill must therefore re-check ownership server-side via fetch_drill()
rather than trusting that the ID came from its own autocomplete.
"""
from discord import app_commands

from database.db import Database
from data.materials import (
    DRILLS,
    STORAGE_CONTAINERS,
    effective_capacity,
    effective_rate,
    get_material_info,
)

# Discord truncates a choice name past 100 characters; leave room for the
# server name, which is the only unbounded part of a label.
_MAX_CHOICE_NAME = 100
# Autocomplete accepts at most 25 results, so a player with more drills than
# this sees the most valuable ones and narrows down by typing.
MAX_AUTOCOMPLETE_RESULTS = 25
# How many servers /collect names individually before collapsing the rest into
# an "and N more" line - the haul itself is credited in full either way.
COLLECT_SERVER_DISPLAY_LIMIT = 10


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


def drill_label(drill_row, location: str | None = None, *, with_emoji: bool = False) -> str:
    """One line describing a drill: which one it is, how far it's been
    upgraded, what it's carrying and where it lives. `location` is the server
    name for a placed drill; pass None to have it read as inventory."""
    parts = [f"#{drill_row['drill_id']} {drill_name(drill_row)}", f"Lv{drill_row['level']}"]

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


async def drill_choices(
    db: Database,
    owner_id: int,
    current: str,
    *,
    guild_id: int | None = None,
    unplaced_only: bool = False,
    exclude_locked: bool = True,
    require_container: bool = False,
    guild_names: dict[int, str] | None = None,
) -> list[app_commands.Choice[int]]:
    """Builds the drill list for an autocomplete callback.

    guild_id restricts to drills placed in that one server; unplaced_only
    restricts to drills sitting in inventory. Neither means "any drill this
    user owns, wherever it is".
    """
    conditions = ["owner_id = ?"]
    params: list = [owner_id]

    if unplaced_only:
        conditions.append("guild_id IS NULL")
    elif guild_id is not None:
        conditions.append("guild_id = ?")
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
        # Matching the ID as well as the label means typing a bare number
        # jumps straight to that drill, which is how the labels read anyway.
        if search and search not in label.lower() and not str(row["drill_id"]).startswith(search):
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


def collection_summary_lines(
    hauls: list[tuple[int, int]],
    guild_names: dict[int, str],
    *,
    current_guild_id: int | None = None,
    limit: int = COLLECT_SERVER_DISPLAY_LIMIT,
) -> list[str]:
    """Summarises a cross-server haul, one line per server: how many of that
    server's drills were emptied and how much came out of them.

    `hauls` is one (guild_id, items) pair per emptied drill, so a server with
    three drills appears three times and is tallied here. The server the
    command was run in comes first even if it wasn't the biggest haul - it's
    the one the player is looking at - and the rest follow by total descending,
    the same ordering /balance uses for currencies.
    """
    tallies: dict[int, list[int]] = {}
    for guild_id, items in hauls:
        entry = tallies.setdefault(guild_id, [0, 0])
        entry[0] += 1
        entry[1] += items

    ordered = []
    current = tallies.pop(current_guild_id, None) if current_guild_id is not None else None
    if current is not None:
        ordered.append((current_guild_id, current))
    ordered.extend(sorted(tallies.items(), key=lambda item: item[1][1], reverse=True))

    lines = []
    for guild_id, (drill_count, items) in ordered[:limit]:
        # Same fallback as guild_name_map: a server the bot can no longer see
        # still gets named, because its drills kept mining regardless.
        name = guild_names.get(guild_id, f"server {guild_id}")
        plural = "s" if drill_count != 1 else ""
        lines.append(f"**{name}** - {drill_count} drill{plural} · {items:,}")

    if len(ordered) > limit:
        lines.append(f"... and {len(ordered) - limit} more")
    return lines


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
