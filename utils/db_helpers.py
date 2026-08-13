"""
utils/db_helpers.py

Shared database accessors used by multiple cogs. Every cog was carrying its
own identical copies of these (get/adjust inventory quantities, server stock,
currency balances, fee burning) - they live here once instead.

Every function here takes an `_Executor`, which is either a Database (each
statement standing alone) or a Transaction (all of them committing together).
Pass a Transaction whenever the caller reads a value and then writes based on
it - see Database.transaction for why that matters.
"""
from typing import NamedTuple

import config
from database.db import Database, InsufficientQuantity, _Executor
from data.materials import get_material_info, effective_max_queue, upgrade_threshold

# Every machine whose per-server settings live in <machine>_level, _fee,
# _fees_collected and _max_queue columns on server_config, and whose queued
# work shares the production_jobs table. That uniform naming is what lets
# /setup fee, /setup max_queue and queue_room below all be one implementation
# instead of four - adding a fifth machine means adding it here and nowhere
# else.
MACHINES = ("furnace", "factory", "press", "scrapper")


async def ensure_user_row(db: _Executor, user_id: int):
    await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


async def ensure_server_row(db: _Executor, guild_id: int):
    # Fees are inserted explicitly rather than left to the schema DEFAULTs:
    # a database created before a default changed keeps its old column
    # DEFAULT forever, so relying on it would give new servers stale fees.
    await db.execute(
        "INSERT OR IGNORE INTO server_config "
        "(guild_id, furnace_fee, factory_fee, press_fee, scrapper_fee) VALUES (?, ?, ?, ?, ?)",
        (
            guild_id,
            config.DEFAULT_FURNACE_FEE,
            config.DEFAULT_FACTORY_FEE,
            config.DEFAULT_PRESS_FEE,
            config.DEFAULT_SCRAPPER_FEE,
        ),
    )


class QueueRoom(NamedTuple):
    """The answer to "may this user queue `adding` more items here?", along with
    every number a caller needs to explain the answer."""

    fits: bool
    queued: int      # items this user already has outstanding on this machine
    effective: int   # the cap actually enforced: base * level
    base: int        # what /setup max_queue is set to
    level: int


async def queue_room(db: _Executor, guild_id: int, user_id: int, machine: str, adding: int) -> QueueRoom:
    """Whether a user has room for `adding` more items on one of this server's
    machines, counted in ITEMS outstanding rather than jobs - a job queueing ten
    of something occupies ten of the cap.

    The cap is per user, per guild, per machine, and it scales with the
    machine's level (see effective_max_queue). status != 'complete' is the
    liveness filter; the server's own auto-smelt jobs bypass this entirely
    because they're inserted directly rather than through a command.

    Call this inside the transaction that will do the queueing. It reads that
    transaction's own writes, and a read-then-write across an await is exactly
    the race Database.transaction exists to prevent - without it, two commands
    fired at once both see the same outstanding total and both pass.
    """
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")

    cfg = await db.fetchone(
        f"SELECT {machine}_max_queue AS base, {machine}_level AS level "
        f"FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    base, level = (cfg["base"], cfg["level"]) if cfg else (0, 1)

    row = await db.fetchone(
        "SELECT COALESCE(SUM(quantity), 0) AS queued FROM production_jobs "
        "WHERE guild_id = ? AND user_id = ? AND job_type = ? AND status != 'complete'",
        (guild_id, user_id, machine),
    )
    queued = row["queued"] if row else 0

    effective = effective_max_queue(base, level)
    return QueueRoom(
        fits=queued + adding <= effective,
        queued=queued,
        effective=effective,
        base=base,
        level=level,
    )


def queue_full_message(machine: str, room: QueueRoom) -> str:
    """The rejection when a queue is full. Names the effective cap and where it
    came from, because a player who read "5 items" in /setup and is being
    refused at 15 needs to see the level multiplier to believe the number."""
    return (
        f"You can only queue up to {room.effective:,} items worth of {machine} recipes "
        f"per user at once ({room.base:,} per level, at level {room.level:,}), and you "
        f"already have {room.queued:,}. Complete some jobs first."
    )


async def get_user_quantity(db: _Executor, user_id: int, material_id: str) -> int:
    row = await db.fetchone(
        "SELECT quantity FROM user_materials WHERE user_id = ? AND material_id = ?",
        (user_id, material_id),
    )
    return row["quantity"] if row else 0


async def adjust_user_quantity(db: _Executor, user_id: int, material_id: str, delta: int):
    await db.execute(
        """
        INSERT INTO user_materials (user_id, material_id, quantity) VALUES (?, ?, ?)
        ON CONFLICT (user_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity
        """,
        (user_id, material_id, delta),
    )


async def deduct_user_quantity(db: _Executor, user_id: int, material_id: str, amount: int):
    """Takes materials out of an inventory, refusing to take more than is
    there. Use this rather than a negative adjust_user_quantity anywhere the
    amount came from a validated read.

    The guard is in the WHERE clause, so "do they have enough" and "take it"
    are one statement and nothing can change the quantity in between. It's a
    backstop, not the primary defence - the caller should already have checked
    inside the same transaction - but it means a future regression aborts the
    operation instead of quietly minting materials out of a negative balance.
    """
    if amount <= 0:
        return
    changed = await db.execute_changes(
        "UPDATE user_materials SET quantity = quantity - ? "
        "WHERE user_id = ? AND material_id = ? AND quantity >= ?",
        (amount, user_id, material_id, amount),
    )
    if not changed:
        raise InsufficientQuantity(
            f"user {user_id} does not have {amount}x {material_id}"
        )


async def deduct_server_stock(db: _Executor, guild_id: int, material_id: str, amount: int):
    """The server-side counterpart to deduct_user_quantity - stops the market
    selling stock it doesn't actually hold."""
    if amount <= 0:
        return
    changed = await db.execute_changes(
        "UPDATE server_material_storage SET quantity = quantity - ? "
        "WHERE guild_id = ? AND material_id = ? AND quantity >= ?",
        (amount, guild_id, material_id, amount),
    )
    if not changed:
        raise InsufficientQuantity(
            f"guild {guild_id} does not have {amount}x {material_id} in stock"
        )


async def deduct_currency_balance(db: _Executor, guild_id: int, user_id: int, amount: float):
    """Charges a user, refusing to overdraw them."""
    if amount <= 0:
        return
    changed = await db.execute_changes(
        "UPDATE server_currency_balances SET balance = balance - ? "
        "WHERE guild_id = ? AND user_id = ? AND balance >= ?",
        (amount, guild_id, user_id, amount),
    )
    if not changed:
        raise InsufficientQuantity(
            f"user {user_id} cannot afford {amount} in guild {guild_id}"
        )


async def get_server_stock(db: _Executor, guild_id: int, material_id: str) -> int:
    row = await db.fetchone(
        "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
        (guild_id, material_id),
    )
    return row["quantity"] if row else 0


async def adjust_server_stock(db: _Executor, guild_id: int, material_id: str, delta: int):
    await db.execute(
        """
        INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)
        ON CONFLICT (guild_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity
        """,
        (guild_id, material_id, delta),
    )


async def get_currency_balance(db: _Executor, guild_id: int, user_id: int) -> float:
    row = await db.fetchone(
        "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return row["balance"] if row else 0.0


async def adjust_currency_balance(db: _Executor, guild_id: int, user_id: int, delta: float):
    await db.execute(
        """
        INSERT INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, ?)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET balance = balance + excluded.balance
        """,
        (guild_id, user_id, delta),
    )


async def record_minted(db: _Executor, guild_id: int, amount: float):
    await db.execute(
        "UPDATE server_config SET currency_minted_total = currency_minted_total + ? WHERE guild_id = ?",
        (amount, guild_id),
    )


async def apply_machine_upgrades(db: _Executor, guild_id: int, machine: str) -> int:
    """Raises a machine's level as far as its collected fees now reach, and
    returns the level it ended on.

    Loops rather than incrementing once because a single expensive job - or a
    donation - can cross more than one threshold at a time, and there is no cap
    to stop at.

    Takes an executor rather than a Database so it reads the fee total its
    caller just wrote, inside the same transaction, rather than the value from
    before it. Passing the bare Database here would let a machine miss an
    upgrade the fee it just banked had paid for.

    One implementation for all four machines, which their uniform column naming
    is what allows (see MACHINES). It was four identical private methods until
    /donate needed a fifth, and a rule about levelling that is written down five
    times is a rule that eventually differs in one of them.
    """
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    cfg = await db.fetchone(
        f"SELECT {machine}_level AS level, {machine}_fees_collected AS collected "
        f"FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    if cfg is None:
        return 1

    level = cfg["level"]
    while cfg["collected"] >= upgrade_threshold(level + 1):
        level += 1
    if level != cfg["level"]:
        await db.execute(
            f"UPDATE server_config SET {machine}_level = ? WHERE guild_id = ?",
            (level, guild_id),
        )
    return level


async def record_burned(db: _Executor, guild_id: int, amount: float):
    await db.execute(
        "UPDATE server_config SET currency_burned_total = currency_burned_total + ? WHERE guild_id = ?",
        (amount, guild_id),
    )


async def charge_user_fee(db: _Executor, guild_id: int, user_id: int, amount: float):
    """Deducts an infrastructure fee from a user's balance. Fees are a currency
    sink (docs/market.md section 1/4) - the amount leaves circulation entirely
    rather than moving to another balance.

    Raises InsufficientQuantity if the user can't cover it, which aborts the
    surrounding transaction. It used to clamp the balance to zero instead, so a
    fee charged against too small a balance silently burned less than it
    recorded, drifting currency_burned_total away from the currency that
    actually left circulation."""
    if amount <= 0:
        return
    await db.execute(
        "INSERT OR IGNORE INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, 0.0)",
        (guild_id, user_id),
    )
    await deduct_currency_balance(db, guild_id, user_id, amount)
    await record_burned(db, guild_id, amount)


def build_recipe_lines(recipes: dict) -> list[str]:
    """One display line per recipe: the product's emoji and name, followed by
    each input's emoji and quantity. Shared by /furnace status and /factory
    status."""
    lines = []
    for material_id, recipe in recipes.items():
        info = get_material_info(material_id)
        emoji = info["emoji"] if info else "❓"
        name = info["name"] if info else material_id
        costs = []
        for input_id, qty in recipe.get("inputs", {}).items():
            input_info = get_material_info(input_id)
            input_emoji = input_info["emoji"] if input_info else "❓"
            costs.append(f"{input_emoji} {qty}")
        lines.append(f"{emoji} {name} - {' , '.join(costs)}")
    return lines
