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
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

import config
from database.db import Database, InsufficientQuantity, _Executor
from data.materials import (
    BONANZA_SPEED_MULTIPLIER,
    PLAYER_PRICE_SCALE,
    enhancement_speed,
    get_material_info,
    effective_level,
    effective_max_queue,
    mining_slot_level,
    mining_slot_threshold,
    mining_slots,
    upgrade_threshold,
)
from utils.formatting import format_currency, plural
from utils.notifications import post_server_notification, post_user_notification
from data.notifications import GEM_UNLOCK_NOTICES

# Every machine whose per-server settings live in <machine>_level,
# _fees_collected, _max_queue, _fee_multiplier and _enhancement_level columns on
# server_config, and whose queued work shares the production_jobs table. That
# uniform naming is what lets /setup max_queue, /treasurer fee and queue_room
# below all be one implementation instead of five - adding a sixth machine
# means adding it here, its default fee to MACHINE_DEFAULT_FEES, and nowhere
# else. The blast furnace, added in 1.3, is what proved that: it needed no
# change to any function in this module beyond this tuple and its fee.
#
# What a machine counts in is NOT uniform, though. Everything here is denominated
# in whatever unit that machine charges and queues by, which is one item for four
# of them and one BATCH of data.materials.BLAST_FURNACE_BATCH_SIZE items for the
# blast furnace - hence the `unit` argument on queue_full_message below.
MACHINES = ("furnace", "blast_furnace", "factory", "press", "scrapper")

# What each machine charges at a fee multiplier of x1, in the unit it counts in
# (an item, a batch for the blast furnace, a press-day for the press). The base
# every server's fee is a multiple of: there is no per-server fee, only the
# Treasurer's multiplier on this (utils/government.py: FEE_MULTIPLIERS). So
# retuning one of these in config.py changes it on every server at once.
MACHINE_DEFAULT_FEES: dict[str, float] = {
    "furnace": config.DEFAULT_FURNACE_FEE,
    "blast_furnace": config.DEFAULT_BLAST_FURNACE_FEE,
    "factory": config.DEFAULT_FACTORY_FEE,
    "press": config.DEFAULT_PRESS_FEE,
    "scrapper": config.DEFAULT_SCRAPPER_FEE,
}


def machine_fee(machine: str, multiplier: float) -> float:
    """What `machine` charges per unit on a server whose Treasurer has set its
    fee multiplier to `multiplier`."""
    return MACHINE_DEFAULT_FEES[machine] * multiplier


async def machine_fee_rate(db: _Executor, guild_id: int, machine: str) -> float:
    """machine_fee for one server, read from its multiplier. A server with no
    row yet is on x1, the default every row starts with."""
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    row = await db.fetchone(
        f"SELECT {machine}_fee_multiplier AS multiplier FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    return machine_fee(machine, row["multiplier"] if row else 1.0)


# Every column a machine's lifetime fees are banked in. Built from MACHINES
# rather than written out, so a sixth machine's fees count by being added to
# that tuple and nowhere else, the same property that makes queue_room and
# apply_machine_upgrades single implementations.
FEES_COLLECTED_COLUMNS: tuple[str, ...] = tuple(
    f"{machine}_fees_collected" for machine in MACHINES
)

# Everything the mining slot ladder is priced in: every machine's banked fees,
# plus mining_slot_credit - what the government has bought for slots directly
# (every government burn once, Mining Slot Enhancement five times over; see
# utils/government.py).
#
# This tuple and slot_progress() below are the one definition of the total.
# Its two consumers have to agree - the "Mining slot progress" figure /economy
# status shows and the mining slot ladder priced in that same figure - so
# neither adds the columns up on its own. It was "Fees collected" until 1.4,
# and was renamed because the credit column made it more than fees.
#
# There is deliberately no stored column holding the total. Every figure in it
# is already banked in a column that only ever grows, so a separate accumulator
# would be a second copy of the same number with its own opportunities to
# drift - and summing on read is what makes mining slots retroactive to fees a
# server paid before the feature existed.
SLOT_PROGRESS_COLUMNS: tuple[str, ...] = FEES_COLLECTED_COLUMNS + ("mining_slot_credit",)

# The same columns as a SELECT list, for the queries that fetch a row purely to
# hand it to slot_progress.
_SLOT_PROGRESS_SQL = ", ".join(SLOT_PROGRESS_COLUMNS)


def machine_label(machine: str) -> str:
    """A machine's name as prose rather than as a column prefix
    ("blast_furnace" -> "blast furnace"). Every other machine's id is already
    one word, so this only shows up on the newest one."""
    return machine.replace("_", " ")


def slot_progress(cfg) -> float:
    """This server's mining slot progress: every fee its five machines have
    ever banked plus everything the government has bought toward slots, added
    up (SLOT_PROGRESS_COLUMNS).

    One function rather than a sum written out at each call site, because the
    figure /economy status shows and the figure the mining slot ladder is
    priced in have to be the same number (mining_slot_status).

    Takes an already-fetched row rather than querying, since every caller has
    one in hand - and the callers inside a transaction need the row that
    transaction just wrote, not a second read of their own.
    """
    return sum(cfg[column] for column in SLOT_PROGRESS_COLUMNS)


async def ensure_user_row(db: _Executor, user_id: int):
    await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


async def ensure_server_row(db: _Executor, guild_id: int):
    await db.execute("INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)", (guild_id,))


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
        f"You can only queue up to {room.effective:,} {plural(unit)} worth of "
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
    """Tells a player about the command their first ruby, obsidian or diamond
    unlocks, once. Returns whether this raised the notice.

    All three gems unlock something a player has no other way to discover -
    /focus, /efficiency and /affinity do not appear anywhere until you hold the
    gem that opens them - so finding one and not being told is finding nothing. The wording
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


async def get_server_stocks(db: _Executor, guild_id: int) -> dict[str, int]:
    """Everything the server's market is holding, by material, in one query.

    For any surface that reads more than one material - /market status reads
    every tradeable one, the furnace's auto-smelt reads five - this replaces a
    get_server_stock per material. A material with no row is simply absent,
    so read it with .get(material_id, 0)."""
    rows = await db.fetchall(
        "SELECT material_id, quantity FROM server_material_storage WHERE guild_id = ?",
        (guild_id,),
    )
    return {row["material_id"]: row["quantity"] for row in rows}


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


# One guild's balances and its escrowed order currency, as SQL. Shared as
# strings because the two readers hold different handles: the bot has an async
# Database, web/queries.py has its own synchronous sqlite3 connection, and
# neither can call the other's accessor. What they must not do is disagree
# about the query.
CIRCULATING_BALANCE_SQL = (
    "SELECT COALESCE(SUM(balance), 0) AS total FROM server_currency_balances "
    "WHERE guild_id = ?"
)
ESCROWED_UNITS_SQL = (
    "SELECT COALESCE(SUM(quantity * price_units), 0) AS units FROM market_orders "
    "WHERE guild_id = ?"
)
# The same total for every guild at once, which is how the dashboard reads it -
# it renders every server in one pass and would otherwise issue one query per
# server. Spelled out rather than derived from the string above: a GROUP BY
# has to select the column it groups on, so the two are not the same query with
# a different tail.
ESCROWED_UNITS_BY_GUILD_SQL = (
    "SELECT guild_id, COALESCE(SUM(quantity * price_units), 0) AS units "
    "FROM market_orders GROUP BY guild_id"
)
# The other escrow: currency staked on a prediction bet that has not settled
# (1.4). Held on prediction_wagers exactly as a bid's is held on market_orders,
# and just as much not a burn - a cancelled bet hands every cent back. Denoted
# in CENTS, which is what prediction_wagers.stake_cents stores; the order
# escrow above is in PLAYER_PRICE_SCALE units. The two scales are why
# circulating_currency takes them as separate arguments rather than one total.
#
# Spelled with the literal `status IN ('open', 'closed')` that
# idx_prediction_bets_live is partial on - see utils/betting.py: LIVE_BETS_SQL,
# which is the same predicate for the same reason.
BET_ESCROW_CENTS_SQL = (
    "SELECT COALESCE(SUM(w.stake_cents), 0) AS cents "
    "FROM prediction_wagers w JOIN prediction_bets b ON b.bet_id = w.bet_id "
    "WHERE b.guild_id = ? AND b.status IN ('open', 'closed')"
)
BET_ESCROW_CENTS_BY_GUILD_SQL = (
    "SELECT b.guild_id, COALESCE(SUM(w.stake_cents), 0) AS cents "
    "FROM prediction_wagers w JOIN prediction_bets b ON b.bet_id = w.bet_id "
    "WHERE b.status IN ('open', 'closed') GROUP BY b.guild_id"
)
# What the server government is holding (1.4): the treasury and the bond
# repayment pool. Both are server_config columns, so a reader that already has
# the row (web/queries.py) takes them off it with government_held() instead.
GOVERNMENT_HELD_SQL = (
    "SELECT treasury + repayment_pool AS held FROM server_config WHERE guild_id = ?"
)


def government_held(cfg) -> float:
    """The treasury and the repayment pool of an already-fetched server_config
    row - the currency circulating_currency's `government_held` means."""
    return cfg["treasury"] + cfg["repayment_pool"]


def circulating_currency(
    balance_total: float,
    escrowed_units: int,
    escrowed_bet_cents: int = 0,
    government_held: float = 0.0,
) -> float:
    """Every unit of this server's currency that still belongs to somebody:
    what is sitting in balances, plus what open buy orders and running bets are
    holding, plus what the server government holds.

    One function rather than the sum written out per call site, for the same
    reason slot_progress is one - /economy status and the Ops dashboard both
    report this figure and have to agree about it.

    The escrow is the part that is easy to get wrong. Placing a /market order
    deducts the currency from the buyer's balance (the order cannot promise
    money that has since been spent), but that is NOT a burn: nothing was
    destroyed, and cancelling the order hands every unit back. It has left
    server_currency_balances and not the economy. A plain SUM(balance) would
    therefore report a server's money supply shrinking every time somebody
    placed a bid, and recovering when they withdrew it - see docs/market.md
    section 4.

    A prediction bet's stakes (1.4) are the same case in every respect: the
    stake leaves the better's balance when the wager is placed, the pot is paid
    out in full when the bet resolves, and cancelling hands back every cent. A
    server whose members had a large bet running would otherwise look like one
    that had just burned the stake.

    So is the government's money (1.4). Tax and bond sales leave players'
    balances for the treasury or the repayment pool, and nothing is burned
    until the Mayor spends it on a project - repayments go back to players
    untouched (docs/government.md, Money flow).

    escrowed_units is in PLAYER_PRICE_SCALE units, because that is how
    market_orders stores a price; escrowed_bet_cents is in cents, because that
    is how prediction_wagers stores a stake; balance_total is already in
    currency. Three arguments rather than one pre-summed total precisely
    because the scales differ - adding them up is the step that has to happen
    in one place.
    """
    return (
        balance_total
        + escrowed_units / PLAYER_PRICE_SCALE
        + escrowed_bet_cents / 100
        + government_held
    )


async def circulating_currency_for(db: _Executor, guild_id: int) -> float:
    """circulating_currency for one guild, for callers holding a Database."""
    balances = await db.fetchone(CIRCULATING_BALANCE_SQL, (guild_id,))
    escrow = await db.fetchone(ESCROWED_UNITS_SQL, (guild_id,))
    bets = await db.fetchone(BET_ESCROW_CENTS_SQL, (guild_id,))
    held = await db.fetchone(GOVERNMENT_HELD_SQL, (guild_id,))
    return circulating_currency(
        balances["total"], escrow["units"], bets["cents"], held["held"] if held else 0.0
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
    progress behind that number - everything a caller needs to state the cap
    and explain where it came from."""

    level: int             # 1 on a server that has never paid a fee
    slots: int             # drills one player may place here
    progress: float        # mining slot progress (slot_progress)
    next_threshold: float  # `progress` needed for one more slot


async def mining_slot_status(db: _Executor, guild_id: int) -> MiningSlots:
    """This server's mining slot cap, derived from its mining slot progress
    (slot_progress).

    Reads that figure through the same function /economy status displays rather
    than adding the columns up again here, so the progress a player is shown
    on one surface is the number the cap is actually derived from on the other.

    Read rather than stored, so it is correct the instant a fee is banked and
    for fees banked before the feature shipped - there is no marker to migrate
    and no way for the cap to disagree with the money that paid for it. Call it
    inside the transaction that enforces the cap, for the same read-then-write
    reason queue_room documents.

    A guild with no server_config row has paid nothing, which is level 1 rather
    than an error - ensure_server_row has simply not run for it yet.
    """
    row = await db.fetchone(
        f"SELECT {_SLOT_PROGRESS_SQL} FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    progress = slot_progress(row) if row else 0.0
    level = mining_slot_level(progress)
    return MiningSlots(
        level=level,
        slots=mining_slots(level),
        progress=progress,
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
        f"{format_currency(slots.next_threshold, currency_emoji)} of mining slot progress - "
        f"this server has {format_currency(slots.progress, currency_emoji)} so far, "
        f"and every fee its machines charge and every project its Mayor funds adds to that."
    )


async def announce_mining_slot_unlocks(db: _Executor, guild_id: int) -> int:
    """Posts a server notice if this server's mining slot progress has bought it
    a mining slot nobody has been told about yet, and returns its slot level.

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
        f"SELECT {_SLOT_PROGRESS_SQL}, mining_slots_announced AS announced, "
        f"currency_emoji FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    if cfg is None:
        return 1

    level = mining_slot_level(slot_progress(cfg))
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
        f"This server's mining slot progress has passed "
        f"**{format_currency(mining_slot_threshold(level), cfg['currency_emoji'])}**, "
        f"and every player here can now keep **{slots_now:,} drills** in the ground "
        f"instead of {mining_slots(cfg['announced']):,}.\n\n"
        f"Fees from every machine count toward this, and so does every project the "
        f"Mayor funds, so anything smelted, crafted, pressed, scrapped or donated "
        f"paid for it. Use `/mine place` to fill it.",
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
    through utils/government.py: charge_machine_fee (the untaxed share of a
    fee, burned), through /donate or through a Mayor's machine funding (burns
    recorded by their callers), and folding those together would mean the
    callers passing a flag to skip half the function.
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


# ---------------------------------------------------------------------------
# Production jobs
# ---------------------------------------------------------------------------

# How long a finished job's row is kept before it is deleted. A job is marked
# complete rather than deleted when its machine finishes it, because the row is
# the record that the work happened; but nothing reads a finished job back
# except the Ops dashboard's "last activity" heuristic (web/queries.py), which
# looks at most this far back. Before 1.4 completed rows were kept forever,
# and every live-job lookup - the processing loops each tick, queue_room on
# every queue command - scanned the lot; the partial index in schema.sql is
# what makes those lookups cheap, and this is what keeps the table itself from
# growing without bound. The same 90 days the production ledger keeps
# (utils/production_ledger.py: LEDGER_HISTORY_DAYS), for the same reason.
COMPLETED_JOB_HISTORY_DAYS = 90

# SQLite's own datetime('now') layout, which is what queued_at's DEFAULT
# writes; a cutoff compared against it as text has to match it exactly. Same
# constant utils/production_ledger.py keeps, which can't be imported from here
# without a cycle.
_SQLITE_TIMESTAMP = "%Y-%m-%d %H:%M:%S"

# The last date a prune ran, so the DELETE happens about once a day rather
# than every time a job finishes. Process-local on purpose, exactly like the
# ledger's: a restart prunes once more than it strictly had to.
_jobs_last_pruned: str | None = None


async def guilds_with_queued_work(db: _Executor, machine: str):
    """The servers that have a live job on `machine` - the work list a
    processing loop walks each tick. Each row carries everything that loop
    needs to know how much work it may do: the machine's `level` and `collected`
    fees, its `enhancement` level and the server's `bonanza_until` (together,
    run_level), and `work_started`, the queued_at of its oldest live job (for
    ProductionClock).

    Only those servers, rather than every server_config row: a machine with an
    empty queue has nothing to do, and until 1.4 every loop visited every
    server every tick anyway, running a job lookup (and, for the furnace, the
    whole auto-smelt check) against servers that had been idle for months or
    that the bot had been removed from. The grouped subquery is what the
    partial index on production_jobs exists for, and it is the same one read
    that found the live jobs at all, so the extra columns cost no extra query.
    """
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    return await db.fetchall(
        f"SELECT sc.guild_id, sc.{machine}_level AS level, "
        f"sc.{machine}_fees_collected AS collected, "
        f"sc.{machine}_enhancement_level AS enhancement, sc.bonanza_until, "
        f"live.work_started "
        f"FROM server_config sc JOIN ("
        f"SELECT guild_id, MIN(queued_at) AS work_started FROM production_jobs "
        f"WHERE job_type = ? AND status != 'complete' GROUP BY guild_id"
        f") live ON live.guild_id = sc.guild_id",
        (machine,),
    )


def bonanza_active(bonanza_until: str | None, now: datetime | None = None) -> bool:
    """Whether a Server Bonanza ending at `bonanza_until` (server_config's
    column of that name) is running at `now`."""
    if bonanza_until is None:
        return False
    return sqlite_timestamp(now or clock_now()) < bonanza_until


def speed_multiplier(enhancement: int, bonanza_until: str | None, now: datetime | None = None) -> float:
    """How many times its levelled speed a machine runs at: doubled per
    Infrastructure Enhancement, and doubled again while a Bonanza runs."""
    bonanza = BONANZA_SPEED_MULTIPLIER if bonanza_active(bonanza_until, now) else 1
    return enhancement_speed(enhancement) * bonanza


def run_level(cfg, now: datetime | None = None) -> float:
    """The level a machine's rate function is called at: effective_level times
    speed_multiplier. Every rate function is linear in its level
    (data/materials.py), so multiplying the level is multiplying the speed.

    Takes a row with `level`, `collected`, `enhancement` and `bonanza_until` -
    guilds_with_queued_work's row, or machine_speed_level's."""
    return effective_level(cfg["level"], cfg["collected"]) * speed_multiplier(
        cfg["enhancement"], cfg["bonanza_until"], now
    )


async def machine_speed_level(db: _Executor, guild_id: int, machine: str) -> float:
    """The level (run_level) `machine` runs at in this server right now, for a
    status embed or a receipt's quoted wait. A receipt calls it after the job's
    fee is banked, inside the same transaction, because that fee has just moved
    the machine's speed - by a fraction of a level every time now, not only when
    it crosses a threshold."""
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    cfg = await db.fetchone(
        f"SELECT {machine}_level AS level, {machine}_fees_collected AS collected, "
        f"{machine}_enhancement_level AS enhancement, bonanza_until "
        f"FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    return run_level(cfg)


# int() of a sum of per-tick fractions can land a hair under the whole unit it
# should have reached; same tolerance and same reasoning as cogs/press.py:
# PROGRESS_EPSILON.
_PRODUCTION_EPSILON = 1e-9


def clock_now() -> datetime:
    """The clock every tick loop measures elapsed time on - the five machines'
    and the drills'. UTC, matching the datetime('now') the timestamps it is
    compared against were written with. The loops hold a reference to it rather
    than calling it by name so their tests can hand them a clock of their own."""
    return datetime.now(timezone.utc)


def sqlite_timestamp(when: datetime) -> str:
    """`when` in the layout datetime('now') writes, for a timestamp column that
    elapsed_work_hours will later read back."""
    return when.strftime(_SQLITE_TIMESTAMP)


def elapsed_work_hours(since: str | None, now: datetime, tick_minutes: float) -> float:
    """How many hours of work a tick may credit, given `since` - the moment
    this work was last credited up to, or began, as a datetime('now') string.

    Never more than one tick: that is what bounds a restart, a late tick, or a
    `since` from before a long outage to what a tick always credited. Never
    less than zero, for a timestamp written a moment after `now` was read. And
    a whole tick when `since` is unknown (NULL), which is exactly what a tick
    credited before anything was timestamped.

    The one rule behind ProductionClock, the press loop and the drill harvest:
    work is paid for by time that has actually passed since it could start,
    never by the tick merely arriving."""
    tick_hours = tick_minutes / 60
    if since is None:
        return tick_hours
    started = datetime.strptime(since, _SQLITE_TIMESTAMP).replace(tzinfo=timezone.utc)
    return min(tick_hours, max(0.0, (now - started).total_seconds() / 3600))


class ProductionClock:
    """How much work each server's machine has earned since it last worked -
    the per-guild accumulator the four item-counting machine loops share.

    Work is earned from elapsed time, not counted in ticks, and never from
    before the work existed. Before this each tick simply credited an hour's
    rate over the ticks in an hour to whatever job was at the head of the
    queue, however recently it had been queued - so any machine fast enough to
    make one unit per tick (a level 3 furnace, which 25 collected fees buys)
    handed a newly queued job its first unit on the very next tick, seconds
    after the command if the tick was due, against a receipt that had quoted
    minutes. And the fraction left over when a queue emptied sat in memory
    untouched until the next job was queued, however much later, and was spent
    on it; at level 2 that alone could finish a one-item job on its first tick.

    So, per server:

    * A run of work begins when the queue goes from empty to not. It is
      detected by the oldest live job having been queued at or after this
      clock last worked the server - an empty queue was never visited in
      between, which is exactly the case. A new run starts from nothing: no
      leftover fraction, and time counted from when its work was queued.
    * Within a run, time is counted from the last tick.
    * Either way, no tick earns more than one tick's worth. That bounds what a
      restart can hand out (this state is in memory, and a restarted bot sees
      every run as new) and what a late tick can, to what a tick always
      earned.

    What this deliberately keeps from before: a restart still loses the
    fraction in flight, under one unit of work, and time the bot was down is
    not worked. The press persists its own accumulator instead
    (server_config.press_progress) because one unit of ITS work is days, and
    applies the same two rules its own way - see cogs/press.py.

    `now` is injectable because the loops are tested a tick at a time, far
    faster than real time; production passes nothing and gets UTC.
    """

    def __init__(self, tick_minutes: float, now: Callable[[], datetime] | None = None):
        self._tick_minutes = tick_minutes
        self._now = now or clock_now
        # guild_id -> (fraction of a unit carried, when this clock last worked it)
        self._state: dict[int, tuple[float, datetime]] = {}

    def now(self) -> datetime:
        return self._now()

    def earn(self, guild_id: int, rate_per_hour: float, work_started: str, now: datetime) -> int:
        """The whole units `guild_id`'s machine has earned by `now`, at
        `rate_per_hour`, given `work_started` (guilds_with_queued_work's column
        of that name). Whatever fraction is left over carries to the next tick
        of the same run."""
        started = datetime.strptime(work_started, _SQLITE_TIMESTAMP).replace(tzinfo=timezone.utc)
        carry, worked_at = self._state.get(guild_id, (0.0, None))
        # queued_at is whole seconds, so worked_at is compared at whole seconds
        # too: a job queued in the same second a tick ran still reads as new.
        if worked_at is None or started >= worked_at:
            carry, since = 0.0, work_started
        else:
            since = sqlite_timestamp(worked_at)
        progress = carry + rate_per_hour * elapsed_work_hours(since, now, self._tick_minutes)
        units = int(progress + _PRODUCTION_EPSILON)
        self._state[guild_id] = (max(0.0, progress - units), now.replace(microsecond=0))
        return units


async def complete_job(db: _Executor, job_id: int) -> None:
    """Marks a job finished: its quantity is zeroed, because quantity is what's
    LEFT to produce, and the row stays as the record that it ran (see
    COMPLETED_JOB_HISTORY_DAYS). Every machine's loop finishes a job through
    here, which is also where the once-a-day prune hangs."""
    await db.execute(
        "UPDATE production_jobs SET status = 'complete', quantity = 0 WHERE job_id = ?",
        (job_id,),
    )
    await prune_completed_jobs(db)


async def advance_job(db: _Executor, job_id: int, remaining: int) -> None:
    """Records that a job produced part of its quantity this tick and has
    `remaining` still to go."""
    await db.execute(
        "UPDATE production_jobs SET quantity = ?, status = 'in_progress' WHERE job_id = ?",
        (remaining, job_id),
    )


async def prune_completed_jobs(db: _Executor, now: datetime | None = None) -> None:
    """Drops finished jobs queued more than COMPLETED_JOB_HISTORY_DAYS ago, at
    most once a day per process. Called from complete_job rather than from a
    loop of its own, on the same reasoning the ledger and the job board prune
    from their write paths: the moment a row is written is when the table is
    worth trimming, and a loop would be one more thing to keep running.

    Compares queued_at, the only timestamp a job carries - a job queued that
    long ago and now complete is long past anything that reads it."""
    global _jobs_last_pruned
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    if _jobs_last_pruned == today:
        return
    _jobs_last_pruned = today
    cutoff = (now - timedelta(days=COMPLETED_JOB_HISTORY_DAYS)).strftime(_SQLITE_TIMESTAMP)
    await db.execute(
        "DELETE FROM production_jobs WHERE status = 'complete' AND queued_at < ?",
        (cutoff,),
    )


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
