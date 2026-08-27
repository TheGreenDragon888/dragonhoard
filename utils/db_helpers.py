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
from data.materials import (
    get_material_info,
    effective_max_queue,
    mining_slot_level,
    mining_slot_threshold,
    mining_slots,
    upgrade_threshold,
)
from utils.formatting import format_currency
from utils.notifications import post_server_notification, post_user_notification
from data.notifications import GEM_UNLOCK_NOTICES

# Every machine whose per-server settings live in <machine>_level, _fee,
# _fees_collected and _max_queue columns on server_config, and whose queued
# work shares the production_jobs table. That uniform naming is what lets
# /setup fee, /setup max_queue and queue_room below all be one implementation
# instead of five - adding a sixth machine means adding it here and nowhere
# else. The blast furnace, added in 1.3, is what proved that: it needed no
# change to any function in this module beyond this tuple and the fee inserted
# by ensure_server_row.
#
# What a machine counts in is NOT uniform, though. Everything here is denominated
# in whatever unit that machine charges and queues by, which is one item for four
# of them and one BATCH of data.materials.BLAST_FURNACE_BATCH_SIZE items for the
# blast furnace - hence the `unit` argument on queue_full_message below.
MACHINES = ("furnace", "blast_furnace", "factory", "press", "scrapper")

# Every fee a server has ever paid into its infrastructure, added up across all
# five machines, as a SQL expression. Built from MACHINES rather than written
# out, so a sixth machine starts counting toward mining slots by being added to
# that tuple and nowhere else - the same property that makes queue_room and
# apply_machine_upgrades single implementations.
#
# There is deliberately no stored column holding this total. Every figure in it
# is already banked in a <machine>_fees_collected column that only ever grows,
# so a separate accumulator would be a second copy of the same number with its
# own opportunities to drift - and summing on read is what makes mining slots
# retroactive to fees a server paid before the feature existed.
_INVESTED_SQL = " + ".join(f"{machine}_fees_collected" for machine in MACHINES)


def machine_label(machine: str) -> str:
    """A machine's name as prose rather than as a column prefix
    ("blast_furnace" -> "blast furnace"). Every other machine's id is already
    one word, so this only shows up on the newest one."""
    return machine.replace("_", " ")


async def ensure_user_row(db: _Executor, user_id: int):
    await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


async def ensure_server_row(db: _Executor, guild_id: int):
    # Fees are inserted explicitly rather than left to the schema DEFAULTs:
    # a database created before a default changed keeps its old column
    # DEFAULT forever, so relying on it would give new servers stale fees.
    await db.execute(
        "INSERT OR IGNORE INTO server_config "
        "(guild_id, furnace_fee, blast_furnace_fee, factory_fee, press_fee, scrapper_fee) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            config.DEFAULT_FURNACE_FEE,
            config.DEFAULT_BLAST_FURNACE_FEE,
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
    of something occupies ten of the cap. "Item" means one unit of whatever that
    machine produces per unit of fee, so a blast furnace job queueing ten
    BATCHES occupies ten, not a thousand (see MACHINES).

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


def queue_full_message(machine: str, room: QueueRoom, unit: str = "item") -> str:
    """The rejection when a queue is full. Names the effective cap and where it
    came from, because a player who read "5 items" in /setup and is being
    refused at 15 needs to see the level multiplier to believe the number.

    `unit` is what this machine counts in, which is an item everywhere except
    the blast furnace - quoting a bulk queue in items would understate it by a
    factor of BLAST_FURNACE_BATCH_SIZE and send the player looking for 500
    missing items."""
    return (
        f"You can only queue up to {room.effective:,} {unit}s worth of "
        f"{machine_label(machine)} recipes per user at once ({room.base:,} per level, "
        f"at level {room.level:,}), and you already have {room.queued:,}. "
        f"Complete some jobs first."
    )


async def get_user_quantity(db: _Executor, user_id: int, material_id: str) -> int:
    row = await db.fetchone(
        "SELECT quantity FROM user_materials WHERE user_id = ? AND material_id = ?",
        (user_id, material_id),
    )
    return row["quantity"] if row else 0


async def announce_first_gem(db: _Executor, user_id: int, material_id: str) -> bool:
    """Tells a player about the command their first ruby or obsidian unlocks,
    once. Returns whether this raised the notice.

    Both gems unlock something a player has no other way to discover - /focus
    and /efficiency do not appear anywhere until you hold the gem that opens
    them - so finding one and not being told is finding nothing. The wording
    lives in data/notifications.py: GEM_UNLOCK_NOTICES.

    "First" is not derived from the quantity going 0 -> 1, which would fire
    again for somebody who spent their ruby and later mined another. It is the
    (user_id, notice_key) primary key on user_notifications: the row that
    notified them IS the record that they have been notified, so there is no
    separate marker to keep in step and nothing to migrate for a player who
    already owns a gem - they get the notice on their next one, or never, which
    is the harmless direction.

    Anything not in GEM_UNLOCK_NOTICES is a plain no-op, so this stays a dict
    lookup on the overwhelming majority of calls.
    """
    notice = GEM_UNLOCK_NOTICES.get(material_id)
    if notice is None:
        return False
    return await post_user_notification(db, user_id, notice.key, notice.title, notice.body)


async def adjust_user_quantity(db: _Executor, user_id: int, material_id: str, delta: int):
    """Credits materials to an inventory, and raises anything that first
    arrival is supposed to announce.

    The announcement hangs off here for the same reason fees are banked in one
    place: this is the single funnel every credit passes through - /collect,
    the press, the factory, the scrapper, a market buy, a devtools grant - so a
    gem arriving by a route nobody thought of still tells the player what it
    unlocked. Hooking the four or five call sites instead would mean the sixth
    one silently doesn't.
    """
    await db.execute(
        """
        INSERT INTO user_materials (user_id, material_id, quantity) VALUES (?, ?, ?)
        ON CONFLICT (user_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity
        """,
        (user_id, material_id, delta),
    )
    # Only on a credit. Nothing calls this with a negative delta today
    # (deduct_user_quantity is the guarded way to take materials away), but
    # "you found your first ruby" fired by something removing one would be an
    # odd way to learn that.
    if delta > 0:
        await announce_first_gem(db, user_id, material_id)


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

    One implementation for every machine, which their uniform column naming is
    what allows (see MACHINES). It was four identical private methods until
    /donate needed a fifth, and a rule about levelling that is written down five
    times is a rule that eventually differs in one of them - the blast furnace
    then arrived and leveled correctly without this function being touched.
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


class MiningSlots(NamedTuple):
    """How many drills one player may have placed in one server, and the
    investment behind that number - everything a caller needs to state the cap
    and explain where it came from."""

    level: int             # 1 on a server that has never paid a fee
    slots: int             # drills one player may place here
    invested: float        # lifetime infrastructure fees, all machines summed
    next_threshold: float  # `invested` needed for one more slot


async def mining_slot_status(db: _Executor, guild_id: int) -> MiningSlots:
    """This server's mining slot cap, derived from its lifetime infrastructure
    fees (see _INVESTED_SQL).

    Read rather than stored, so it is correct the instant a fee is banked and
    for fees banked before the feature shipped - there is no marker to migrate
    and no way for the cap to disagree with the money that paid for it. Call it
    inside the transaction that enforces the cap, for the same read-then-write
    reason queue_room documents.

    A guild with no server_config row has paid nothing, which is level 1 rather
    than an error - ensure_server_row has simply not run for it yet.
    """
    row = await db.fetchone(
        f"SELECT {_INVESTED_SQL} AS invested FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    invested = row["invested"] if row else 0.0
    level = mining_slot_level(invested)
    return MiningSlots(
        level=level,
        slots=mining_slots(level),
        invested=invested,
        next_threshold=mining_slot_threshold(level + 1),
    )


def mining_slots_full_message(slots: MiningSlots, currency_emoji: str | None) -> str:
    """The rejection when a player's drills already fill this server's slots.

    Names what the next slot costs and how far along the server is, because the
    cap is a SERVER-wide unlock that the refused player may have no other reason
    to know exists - "you already have 3" alone reads as a hard rule of the game
    rather than as something their server can buy its way out of."""
    return (
        f"You already have all {slots.slots:,} of this server's mining slots filled. "
        f"The next one unlocks at "
        f"{format_currency(slots.next_threshold, currency_emoji)} in total "
        f"infrastructure fees - this server has invested "
        f"{format_currency(slots.invested, currency_emoji)} so far."
    )


async def announce_mining_slot_unlocks(db: _Executor, guild_id: int) -> int:
    """Posts a server notice if this server's lifetime fees have bought it a
    mining slot nobody has been told about yet, and returns its slot level.

    server_config.mining_slots_announced is a record of what has been ANNOUNCED,
    not of what has been unlocked - mining_slot_status derives the live cap and
    never consults it. Its whole job is dedupe: post_server_notification refuses
    to guess whether two calls mean the same event, so the guard belongs here,
    and without it every fee paid after a threshold would repost the same notice.

    Announcing lags the unlock by one fee on a server that crossed a threshold
    before this shipped, or that crossed it on a fee paid through some future
    path that forgets to call this. That is the deliberate failure direction:
    the slot itself is derived and already usable either way, so the worst case
    is a quiet unlock rather than an unusable one.

    Call it inside the fee's own transaction. It reads the total that
    transaction just wrote - passing the bare Database would announce against
    the figure from before the fee that paid for the slot.
    """
    cfg = await db.fetchone(
        f"SELECT {_INVESTED_SQL} AS invested, mining_slots_announced AS announced, "
        f"currency_emoji FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    if cfg is None:
        return 1

    level = mining_slot_level(cfg["invested"])
    if level <= cfg["announced"]:
        return level

    # Quotes the threshold actually reached rather than the fee that tipped it,
    # and the whole new total rather than "+1", because a server crossing more
    # than one threshold at once - a large donation, or the first fee paid after
    # this shipped - would otherwise announce the wrong number.
    slots_now = mining_slots(level)
    await post_server_notification(
        db, guild_id,
        "⛏️ New Mining Slot" if slots_now - mining_slots(cfg["announced"]) == 1 else "⛏️ New Mining Slots",
        f"This server's infrastructure investment has passed "
        f"**{format_currency(mining_slot_threshold(level), cfg['currency_emoji'])}**, "
        f"and every player here can now keep **{slots_now:,} drills** in the ground "
        f"instead of {mining_slots(cfg['announced']):,}.\n\n"
        f"Fees from every machine count toward this, so anything smelted, crafted, "
        f"pressed, scrapped or donated paid for it. Use `/mine place` to fill it.",
    )
    await db.execute(
        "UPDATE server_config SET mining_slots_announced = ? WHERE guild_id = ?",
        (level, guild_id),
    )
    return level


async def bank_infrastructure_fee(
    db: _Executor, guild_id: int, machine: str, amount: float
) -> int:
    """Credits `amount` to one machine's lifetime fee total, then applies
    everything that total now pays for - the machine's own level, and the
    server's mining slots - and returns the machine's level.

    The one place a fee becomes progress, which is the point of it. Every cog
    that charges a fee used to write the same UPDATE and the same
    apply_machine_upgrades call itself, seven times over; mining slots would
    have made that eight copies of a rule that has to be identical in all of
    them, and the release that adds a ninth thing fees unlock should not have to
    find every one of them again.

    Charging the player is deliberately NOT part of this. A fee reaches here
    through charge_user_fee (a burn) or through /donate (a burn recorded
    separately), and folding those together would mean one of the two callers
    passing a flag to skip half the function.
    """
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    await db.execute(
        f"UPDATE server_config SET {machine}_fees_collected = "
        f"{machine}_fees_collected + ? WHERE guild_id = ?",
        (amount, guild_id),
    )
    level = await apply_machine_upgrades(db, guild_id, machine)
    await announce_mining_slot_unlocks(db, guild_id)
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
