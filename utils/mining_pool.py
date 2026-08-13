"""
utils/mining_pool.py

The server mining pool: a bag of MINING_POOL_BAG_SIZE raw materials that drills
draw from, refilled the moment it empties.

There is no rate limit here and no clock. Before 1.2 the pool was topped up by
200 items per member per day and capped at three days of that, which made mining
a rate limit dressed up as a resource: a server's output was fixed by its member
count, better drills bought nothing once you were already draining the daily
allowance, and the answer to "how do we mine more" was "recruit". Both the
top-up and the cap are gone. What a server produces is now decided by how many
drills it has running, how good they are, and how often somebody empties them.

What the bag buys, beyond removing the ceiling, is certainty about gemstones. It
holds exactly 90 rubies, 9 obsidian and 1 diamond, and a drill draws from what
is genuinely in it (draw_from_pool, without replacement). So a diamond is not a
one-in-a-million chance rolled afresh on every item forever - it is a single
object in the bag that somebody will dig up before the bag is empty. Drain a
bag, get a diamond. The average is unchanged from the published drop rates; the
variance is gone.

server_config.mining_pool_remaining stays the authoritative TOTAL and must equal
SUM(quantity) here for that guild. Every function below writes both.
"""
import logging

from database.db import _Executor
from data.materials import (
    GEMSTONES,
    MINING_POOL_BAG_SIZE,
    ORES,
    draw_from_pool,
    pool_bag_contents,
)

log = logging.getLogger("dragonhoard")


async def pool_contents(db: _Executor, guild_id: int) -> dict[str, int]:
    """What the bag is holding, by material. Materials at zero are omitted -
    every caller either draws from this or displays it, and neither wants a row
    saying a server has no diamonds."""
    rows = await db.fetchall(
        "SELECT material_id, quantity FROM server_mining_pool "
        "WHERE guild_id = ? AND quantity > 0",
        (guild_id,),
    )
    return {row["material_id"]: row["quantity"] for row in rows}


async def refill_pool(tx: _Executor, guild_id: int) -> dict[str, int]:
    """Puts a fresh bag in, and returns what went into it.

    ADDS to whatever is left rather than replacing it, which only matters at the
    boundary: a bag with three items in it and a drill wanting ten would
    otherwise have those three quietly deleted. Adding means no item is ever
    destroyed by a refill, and the invariant that the total equals the sum of
    the parts survives a partial bag.

    Must run inside the caller's transaction. It writes an absolute figure
    derived from what it read, so a drill harvesting in between would have its
    take restored - minting raw materials out of nothing.
    """
    bag = pool_bag_contents()
    for material_id, quantity in bag.items():
        await tx.execute(
            "INSERT INTO server_mining_pool (guild_id, material_id, quantity) "
            "VALUES (?, ?, ?) ON CONFLICT(guild_id, material_id) DO UPDATE "
            "SET quantity = quantity + excluded.quantity",
            (guild_id, material_id, quantity),
        )
    await tx.execute(
        "UPDATE server_config SET mining_pool_remaining = mining_pool_remaining + ? "
        "WHERE guild_id = ?",
        (sum(bag.values()), guild_id),
    )
    log.info("Guild %s opened a fresh mining bag.", guild_id)
    return bag


async def take_from_pool(tx: _Executor, guild_id: int, count: int, rng=None) -> dict[str, int]:
    """Draws `count` items out of a server's bag and reports what came out,
    having already removed them. Refills first if the bag is empty or too thin
    to cover the draw.

    Always returns exactly `count` items for any sane count, because the bag
    refills on demand - this is what "no daily limit" means in practice. A drill
    is never told to wait for a clock; the only thing that stops it is its own
    storage filling up.

    Must run inside the caller's transaction. The bag is shared by every drill
    in the server, so reading what is left and taking a share of it has to be
    one atomic step, or two drills can both read the same remainder and between
    them mine more than it held.
    """
    if count <= 0:
        return {}

    available = await pool_contents(tx, guild_id)
    if sum(available.values()) < count:
        # Drawing across a bag boundary. Refilling first (rather than drawing
        # the remnant and refilling after) keeps this one draw against one
        # combined bag, so the boundary can't skew what a tick produces.
        await refill_pool(tx, guild_id)
        available = await pool_contents(tx, guild_id)

    drawn = draw_from_pool(available, count) if rng is None else draw_from_pool(available, count, rng)
    if not drawn:
        return {}

    for material_id, quantity in drawn.items():
        # Guarded on there being enough left, so a concurrent draw that somehow
        # got in first can't push a material negative and invent items.
        changed = await tx.execute_changes(
            "UPDATE server_mining_pool SET quantity = quantity - ? "
            "WHERE guild_id = ? AND material_id = ? AND quantity >= ?",
            (quantity, guild_id, material_id, quantity),
        )
        if not changed:
            raise RuntimeError(
                f"pool for guild {guild_id} lost {quantity} {material_id} mid-draw"
            )

    await tx.execute(
        "UPDATE server_config SET mining_pool_remaining = mining_pool_remaining - ? "
        "WHERE guild_id = ?",
        (sum(drawn.values()), guild_id),
    )
    return drawn


def pool_display_lines(remaining: int, contents: dict[str, int]) -> list[str]:
    """How /mine status renders the bag. EVERY line here is a fact read from the
    database - there are no estimates left anywhere in this embed.

    That is worth stating because the previous design had both, side by side and
    unlabelled: ore counts were real while gemstone lines were a projection from
    an accrual rate, and the gemstone line silently changed from one to the other
    depending on whether a gem happened to be in the pool. With a real bag there
    is nothing to project - the gems are either in there or they are not, and
    the count is simply the count.
    """
    # Deliberately NO "/ 1,000,000" denominator. The bag is not a fixed-size
    # container being drained from full - a refill ADDS a bag to whatever was
    # left, so the remaining count can legitimately sit above the bag size (it
    # does on every server the moment the 1.2 migration runs, and by a few items
    # after any refill that crosses a tick). A fraction reading 1,002,786 /
    # 1,000,000 is simply a lie about what the number means.
    #
    # Nothing is lost by dropping it: the gemstone counts below are the progress
    # indicator that actually matters, and they are exact.
    lines = [f"**{remaining:,}** raw materials left"]

    # ore_cells = [
    #     f"{_emoji(ore)} {contents[ore]:,}" for ore in ORES if contents.get(ore)
    # ]
    # if ore_cells:
    #     lines.append(" ".join(ore_cells))

    gem_cells = [
        f"{_emoji(gem)} **{contents[gem]}**" for gem in GEMSTONES if contents.get(gem)
    ]
    lines.append(
        "Gemstones still in the batch: **(** " + " ".join(gem_cells) + " **)**" if gem_cells
        else "No gemstones left in this batch - a fresh one starts when it runs out."
    )
    return lines


def _emoji(material_id: str) -> str:
    # Imported lazily to keep this module's import graph free of the display
    # layer; get_material_info lives with the balance data.
    from data.materials import get_material_info

    return get_material_info(material_id)["emoji"]
