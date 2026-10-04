"""
utils/government.py

The server government (1.4): an elected Mayor and Treasurer, the revenue
that funds them, the bonds that let them spend ahead of it, and the projects they
spend on. See docs/government.md for the design and the reasoning behind every
number here; cogs/government.py owns the slash commands and the embeds.

Everything that moves currency or changes who holds office lives here, so it
can be tested against a temporary SQLite database without a gateway
connection.

THE ONE RULE THIS MODULE EXISTS TO KEEP: currency leaves the government only as
a burn or as a bond repayment.

  A fee used to be burned outright. Now the Treasurer's Fee Share diverts part
  of it, and that part is HELD - in the treasury, or in the repayment pool while
  the server owes bondholders - rather than burned. utils/db_helpers.py:
  circulating_currency counts both, the way it counts order and bet escrow.

  The Market Tax is the same arrangement on player-to-player trades: a share
  of what the seller would have received is held instead (pay_market_seller).

  Money leaves the treasury through spend_treasury and nothing else, and every
  caller of that is a project, so every such payment is a burn. Money leaves
  the repayment pool through pay_bondholders and nothing else. So over a whole
  bond cycle the server burns exactly what its fees would have burned without a
  government; it only burns it sooner. The one leak is the bond premium, which
  is currency that would have been burned and is paid to a player instead -
  capped by MAX_BOND_RATE_PERCENT at 5/105 of every repayment.

Game dates are the job board's America/Phoenix day (utils/job_board.py:
JOB_BOARD_TIMEZONE), so "once per day" and "Thursday" mean the same thing here
as everywhere else in the game. Timestamps stored in the database are UTC in
datetime('now')'s layout, as they are everywhere else.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Awaitable, Callable, NamedTuple

from database.db import Database, InsufficientQuantity, _Executor
from data.materials import (
    BONANZA_HISTORY_DAYS,
    BONANZA_HOURS,
    MINING_SLOT_ENHANCEMENT_MULTIPLIER,
    bonanza_price,
    enhancement_price,
    player_price_total,
)
from utils.betting import apportion, from_cents
from utils.db_helpers import (
    MACHINES,
    adjust_currency_balance,
    announce_mining_slot_unlocks,
    bank_infrastructure_fee,
    bonanza_active,
    clock_now,
    deduct_currency_balance,
    ensure_server_row,
    machine_label,
    record_burned,
    sqlite_timestamp,
)
from utils.formatting import format_currency
from utils.job_board import JOB_BOARD_TIMEZONE
from utils.notifications import post_server_notification, set_user_notification
from utils.production_ledger import GDP_WEEK_HOURS, tracked_since, window_cutoff, window_totals

MAYOR = "mayor"
TREASURER = "treasurer"
OFFICES = (MAYOR, TREASURER)
OFFICE_LABELS = {MAYOR: "Mayor", TREASURER: "Treasurer"}

# The multipliers the Treasurer may put on a machine's default fee
# (utils/db_helpers.py: MACHINE_DEFAULT_FEES). Fixed steps rather than any
# number, which is what the design specified.
#
# Between x0.5 and x2 each doubling is split in three (x1.25 then x1.6, since
# 1.25 * 1.6 == 2), because going straight from x1 to x2 was judged too
# extreme a move for most servers. Every raise keeps an exact inverse cut
# (1.25 * 0.8 == 1, 1.6 * 0.625 == 1), which is what the ladder was chosen
# for. Only the middle is fine-grained; the extremes keep whole doublings.
# x0.625 is the one step with three decimals, so a fee can carry five, which
# utils/formatting.py: format_exact_price shows in full.
FEE_MULTIPLIERS = (0.25, 0.5, 0.625, 0.8, 1.0, 1.25, 1.6, 2.0, 4.0)

# The government's two rates - its revenue. Neither is a charge on top of
# anything:
#
#   FEE SHARE - the government's share of every machine fee, held instead of
#   burned. The player pays the same fee at any rate; the rate only decides how
#   much of it the government keeps. Stored in server_config.tax_percent, the
#   column's name from 1.4, when it was called the tax.
#
#   MARKET TAX - a transaction tax on every player-to-player trade, taken out
#   of what the seller receives; the buyer pays the quoted price at any rate.
#   Only trades between players carry it, because those are
#   the ones the server facilitates rather than takes part in; a sale to or a
#   purchase from the server is untouched. Charged at whatever rate is in force
#   when the trade happens - a listing does not lock in the rate it was posted
#   under.
FEE_SHARE = "fee-share"
MARKET_TAX = "market-tax"
RATE_KINDS = (FEE_SHARE, MARKET_TAX)
RATE_LABELS = {FEE_SHARE: "Fee Share", MARKET_TAX: "Market Tax"}
# Each kind's server_config rate column, the column that stamps the day it
# last changed, and the government_bonds column recording it at each sale.
_RATE_COLUMNS = {
    FEE_SHARE: ("tax_percent", "tax_changed", "tax_percent_at_sale"),
    MARKET_TAX: ("market_tax_percent", "market_tax_changed", "market_tax_percent_at_sale"),
}

# The steps either rate may be set to - one list for both, so /treasurer rate
# offers one dropdown whichever it sets. Fixed steps chosen the way
# FEE_MULTIPLIERS are, and in fact built from them: the fee ladder times ten,
# so 10% sits where x1 does, 5% to 20% splits each doubling in three with an
# exact inverse for every raise, and 2.5% and 40% are whole doublings outside
# that. 0% switches a rate off, and 100% is the top of the range.
# tests/test_government.py checks the ladder against the fee one.
#
# Fee Share was any whole percent until it moved onto this list, and a rate that
# was not on it was rounded down to the step below (database/db.py:
# _migrate_fee_share_to_steps). Some steps are not whole, which the INTEGER
# columns that hold Fee Share store as REAL - SQLite's INTEGER affinity keeps a
# value that would not convert losslessly (tests/test_migrations.py pins it).
RATE_PERCENTS = (0.0, 2.5, 5.0, 6.25, 8.0, 10.0, 12.5, 16.0, 20.0, 40.0, 100.0)
MAX_RATE_PERCENT = 100.0


def rate_step_at_or_below(percent: float) -> float:
    """The highest step in RATE_PERCENTS that is not above `percent`."""
    return max(step for step in RATE_PERCENTS if step <= percent)


# The ceiling on the bond premium. It is the only currency this whole feature
# lets escape being burned, so it is what bounds the leak (see the module
# docstring).
MAX_BOND_RATE_PERCENT = 5

# What a bond may be bought in, in cents: 1 to 1,000 units of the server's
# currency. The debt cap decides which of them a server can actually sell - a
# 1,000 bond needs at least 1,000 of revenue over the last DEBT_CAP_DAYS - so
# the large ones only open up as a server grows (docs/government.md).
BOND_DENOMINATIONS_CENTS = (100, 200, 500, 1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000)

# Outstanding debt may not exceed the revenue collected over this many previous
# game days - "a server can repay everything it owes within a week".
DEBT_CAP_DAYS = 7

# How long a day's revenue total is kept. Only the last DEBT_CAP_DAYS are ever
# read; the rest is margin.
REVENUE_HISTORY_DAYS = 30

# Voting happens on this weekday (Monday is 0), all day on the game clock, and
# is counted at the midnight that ends it.
VOTING_WEEKDAY = 3  # Thursday

# To vote, an account must have had a drill placed in the server for this long
# when voting opens. A drill placed at all is not enough: a player who owns no
# drill gets a free one on their first /mine place, so an alt account could
# qualify with one command (docs/government.md, Q31). A drill that was already
# placed when the government shipped has no placed_at and qualifies outright,
# so existing players can vote in the first election (see can_vote).
VOTER_DRILL_DAYS = 7

# Every query for bonds still owed spells this predicate literally, because it
# is the only form idx_government_bonds_owed (schema.sql) applies to.
OWED_BONDS_SQL = "remaining_cents > 0"

# Rounding slack for comparing float currency against the cent it is meant to
# be. The treasury accumulates revenue a fraction of a cent at a time.
_EPSILON = 1e-9


class GovernmentError(Exception):
    """An action the government cannot take, with a message written for the
    player. Raised inside the caller's transaction, which aborts it - the same
    arrangement utils/betting.py: BetUnavailable has, for the same reason."""


# ---------------------------------------------------------------------------
# The game clock
# ---------------------------------------------------------------------------

def game_now(now: datetime | None = None) -> datetime:
    """`now` (UTC by default) on the game clock."""
    return (now or clock_now()).astimezone(JOB_BOARD_TIMEZONE)


def game_date(now: datetime | None = None) -> str:
    """Today's game date, ISO formatted, which sorts as plain text."""
    return game_now(now).date().isoformat()


def game_midnight(day: str) -> datetime:
    """The instant `day` (a game date) begins, in UTC."""
    local = datetime.combine(date.fromisoformat(day), time(0), tzinfo=JOB_BOARD_TIMEZONE)
    return local.astimezone(timezone.utc)


def voting_open(now: datetime | None = None) -> bool:
    return game_now(now).weekday() == VOTING_WEEKDAY


def current_voting_day(now: datetime | None = None) -> str:
    """The voting day a vote cast `now` belongs to. Only meaningful while
    voting_open."""
    return game_date(now)


def due_voting_day(now: datetime | None = None) -> str:
    """The most recent voting day that has finished, whose votes are due to be
    counted. On a Thursday that is the one a week ago; today's is still
    open."""
    today = game_now(now).date()
    days_back = (today.weekday() - VOTING_WEEKDAY) % 7 or 7
    return (today - timedelta(days=days_back)).isoformat()


def next_voting_day(now: datetime | None = None) -> str:
    """The voting day that is open now, or the next one to open."""
    today = game_now(now).date()
    return (today + timedelta(days=(VOTING_WEEKDAY - today.weekday()) % 7)).isoformat()


def next_game_midnight(now: datetime | None = None) -> datetime:
    """When the next game day starts, in UTC - when a setting changed today may
    change again."""
    return game_midnight((game_now(now).date() + timedelta(days=1)).isoformat())


# ---------------------------------------------------------------------------
# Revenue: the Fee Share and the Market Tax
# ---------------------------------------------------------------------------

async def has_active_debt(db: _Executor, guild_id: int) -> bool:
    """Whether the server owes anything to a creditor who is still here.
    While it does, every unit of revenue goes to repaying them."""
    row = await db.fetchone(
        f"SELECT 1 FROM government_bonds WHERE guild_id = ? AND {OWED_BONDS_SQL} "
        f"AND frozen = 0 LIMIT 1",
        (guild_id,),
    )
    return row is not None


async def collect_revenue(db: _Executor, guild_id: int, amount: float, now: datetime | None = None):
    """Takes `amount` of revenue - either rate's - into the government: into
    the repayment pool while the server owes active creditors, the treasury
    otherwise. Also adds it to today's revenue total, which the debt cap is
    measured against."""
    if amount <= 0:
        return
    column = "repayment_pool" if await has_active_debt(db, guild_id) else "treasury"
    await db.execute(
        f"UPDATE server_config SET {column} = {column} + ? WHERE guild_id = ?",
        (amount, guild_id),
    )
    await db.execute(
        "INSERT INTO government_tax_daily (guild_id, day, amount) VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id, day) DO UPDATE SET amount = amount + excluded.amount",
        (guild_id, game_date(now), amount),
    )


async def charge_machine_fee(
    db: _Executor, guild_id: int, user_id: int, machine: str, amount: float,
    now: datetime | None = None,
) -> None:
    """Charges a player a machine's fee, and sends every part of it where it
    belongs. The one funnel every machine fee passes through.

    The Fee Share is held by the government (collect_revenue). The rest is what a
    whole fee was before 1.4: burned, and banked to the machine's level and the
    server's mining slots (bank_infrastructure_fee). The share reaches mining
    slots later, when the Mayor spends it - counting it now as well would count
    it twice.

    Raises InsufficientQuantity if the player can't cover it, which aborts the
    surrounding transaction - the fee is never clamped to what they have (see
    the history of that on deduct_currency_balance's callers).
    """
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    if amount <= 0:
        return
    await db.execute(
        "INSERT OR IGNORE INTO server_currency_balances (guild_id, user_id, balance) "
        "VALUES (?, ?, 0.0)",
        (guild_id, user_id),
    )
    await deduct_currency_balance(db, guild_id, user_id, amount)

    cfg = await db.fetchone("SELECT tax_percent FROM server_config WHERE guild_id = ?", (guild_id,))
    percent = cfg["tax_percent"] if cfg else 0
    # 100% is special-cased so the burned part is exactly zero rather than
    # whatever amount - amount * 100 / 100 rounds to.
    share = amount if percent >= MAX_RATE_PERCENT else amount * percent / 100
    kept = amount - share

    await record_burned(db, guild_id, kept)
    await bank_infrastructure_fee(db, guild_id, machine, kept)
    await collect_revenue(db, guild_id, share, now)


def format_percent(percent: float) -> str:
    """A government rate for a player to read: 10%, 6.25%."""
    return f"{percent:g}%"


def market_tax_units(gross_units: int, percent: float) -> int:
    """The Market Tax on a trade worth `gross_units` (PLAYER_PRICE_SCALE units).

    Integer throughout, in basis points, so the seller's share and the tax are
    both whole units and add back up to exactly what the buyer paid. Rounded
    down, so a fraction of a unit stays with the seller. 100% is special-cased
    for the reason charge_machine_fee's is.
    """
    if percent >= MAX_RATE_PERCENT:
        return gross_units
    return gross_units * round(percent * 100) // 10_000


async def market_tax_rate(db: _Executor, guild_id: int) -> float:
    """The Market Tax in force. Read once per trade, inside its transaction,
    so every leg of one trade is charged the same rate."""
    row = await db.fetchone("SELECT market_tax_percent FROM server_config WHERE guild_id = ?", (guild_id,))
    return row["market_tax_percent"] if row else 0.0


async def pay_market_seller(
    db: _Executor, guild_id: int, seller_id: int, gross_units: int, percent: float,
    now: datetime | None = None,
) -> float:
    """Pays a player the proceeds of a player-to-player trade worth
    `gross_units`, less the Market Tax, and holds the tax through
    collect_revenue - into the repayment pool or the treasury, and toward the
    debt cap, exactly as the Fee Share is. The one funnel every
    player-to-player payment passes through. Returns the tax taken, in
    currency.

    Nothing is minted or burned: the buyer's currency is split between the
    seller and the government, and circulating_currency counts both.
    """
    tax_units = market_tax_units(gross_units, percent)
    await adjust_currency_balance(
        db, guild_id, seller_id, player_price_total(gross_units - tax_units, 1)
    )
    tax = player_price_total(tax_units, 1)
    await collect_revenue(db, guild_id, tax, now)
    return tax


async def revenue_collected(db: _Executor, guild_id: int, days: int, now: datetime | None = None) -> float:
    """The revenue this server collected over the `days` game days before today.
    Today is left out: it is not over, and a cap that grew through the day
    would let a sale land on revenue that has only just arrived."""
    today = game_now(now).date()
    first = (today - timedelta(days=days)).isoformat()
    row = await db.fetchone(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM government_tax_daily "
        "WHERE guild_id = ? AND day >= ? AND day < ?",
        (guild_id, first, today.isoformat()),
    )
    return row["total"]


async def prune_revenue_history(db: _Executor, now: datetime | None = None) -> None:
    cutoff = (game_now(now).date() - timedelta(days=REVENUE_HISTORY_DAYS)).isoformat()
    await db.execute("DELETE FROM government_tax_daily WHERE day < ?", (cutoff,))


# ---------------------------------------------------------------------------
# Offices
# ---------------------------------------------------------------------------

async def officeholders(db: _Executor, guild_id: int) -> dict[str, int | None]:
    row = await db.fetchone(
        "SELECT mayor_id, treasurer_id FROM server_config WHERE guild_id = ?", (guild_id,)
    )
    if row is None:
        return {MAYOR: None, TREASURER: None}
    return {MAYOR: row["mayor_id"], TREASURER: row["treasurer_id"]}


async def require_office(db: _Executor, guild_id: int, user_id: int, office: str) -> None:
    """Refuses unless `user_id` holds `office`. Read inside the caller's
    transaction, so an election counted a moment earlier is honoured."""
    holders = await officeholders(db, guild_id)
    if holders[office] != user_id:
        holder = holders[office]
        who = f"<@{holder}> is" if holder else "Nobody is"
        raise GovernmentError(
            f"Only the {OFFICE_LABELS[office]} can do that. {who} {OFFICE_LABELS[office]} "
            f"right now - elections are every Thursday (`/vote`)."
        )


# ---------------------------------------------------------------------------
# Treasurer settings
# ---------------------------------------------------------------------------

def _refuse_second_change(changed: str | None, what: str, now: datetime | None) -> None:
    if changed == game_date(now):
        stamp = int(next_game_midnight(now).timestamp())
        raise GovernmentError(
            f"The {what} has already been changed today. It can change again <t:{stamp}:R>."
        )


async def set_fee_multiplier(
    tx: _Executor, guild_id: int, actor_id: int, machine: str, multiplier: float,
    now: datetime | None = None,
) -> None:
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    if multiplier not in FEE_MULTIPLIERS:
        raise GovernmentError(
            "The fee multiplier must be one of "
            + ", ".join(f"x{m:g}" for m in FEE_MULTIPLIERS) + "."
        )
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, TREASURER)
    row = await tx.fetchone(
        f"SELECT {machine}_fee_changed AS changed FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    _refuse_second_change(row["changed"], f"{machine_label(machine)} fee", now)
    await tx.execute(
        f"UPDATE server_config SET {machine}_fee_multiplier = ?, {machine}_fee_changed = ? "
        f"WHERE guild_id = ?",
        (multiplier, game_date(now), guild_id),
    )


async def rate_floor(db: _Executor, guild_id: int, kind: str) -> float:
    """The lowest a rate may be set while the server owes active creditors:
    the rate in force when the most recent bond still owed was sold. Both rates
    repay bondholders, so they lent against both, and they are the only thing
    that repays them."""
    at_sale = _RATE_COLUMNS[kind][2]
    row = await db.fetchone(
        f"SELECT {at_sale} FROM government_bonds WHERE guild_id = ? "
        f"AND {OWED_BONDS_SQL} AND frozen = 0 ORDER BY bond_id DESC LIMIT 1",
        (guild_id,),
    )
    return row[at_sale] if row else 0.0


async def set_rate(
    tx: _Executor, guild_id: int, actor_id: int, kind: str, percent: float,
    now: datetime | None = None,
) -> None:
    if kind not in RATE_KINDS:
        raise ValueError(f"unknown rate {kind!r}")
    label = RATE_LABELS[kind]
    if percent not in RATE_PERCENTS:
        raise GovernmentError(
            f"The {label} must be one of " + ", ".join(format_percent(p) for p in RATE_PERCENTS) + "."
        )
    rate, changed, _ = _RATE_COLUMNS[kind]
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, TREASURER)
    row = await tx.fetchone(f"SELECT {changed} FROM server_config WHERE guild_id = ?", (guild_id,))
    _refuse_second_change(row[changed], label, now)
    floor = await rate_floor(tx, guild_id, kind)
    if percent < floor:
        raise GovernmentError(
            f"The server owes bondholders who lent at a {format_percent(floor)} {label}, so the "
            f"{label} can't go below {format_percent(floor)} until they are repaid."
        )
    await tx.execute(
        f"UPDATE server_config SET {rate} = ?, {changed} = ? WHERE guild_id = ?",
        (percent, game_date(now), guild_id),
    )


async def set_bond_rate(tx: _Executor, guild_id: int, actor_id: int, percent: int, now: datetime | None = None) -> None:
    if not 0 <= percent <= MAX_BOND_RATE_PERCENT:
        raise GovernmentError(f"The bond rate must be between 0% and {MAX_BOND_RATE_PERCENT}%.")
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, TREASURER)
    row = await tx.fetchone("SELECT bond_rate_changed FROM server_config WHERE guild_id = ?", (guild_id,))
    _refuse_second_change(row["bond_rate_changed"], "bond rate", now)
    await tx.execute(
        "UPDATE server_config SET bond_rate_percent = ?, bond_rate_changed = ? WHERE guild_id = ?",
        (percent, game_date(now), guild_id),
    )


# ---------------------------------------------------------------------------
# Bonds
# ---------------------------------------------------------------------------

def bond_owed_cents(principal_cents: int, rate_percent: int) -> int:
    """What a bond bought for `principal_cents` at `rate_percent` is repaid in
    total. The premium is floored to a whole cent; every denomination is a
    whole number of currency units and every rate a whole percent, so it never
    has anything to floor."""
    return principal_cents + principal_cents * rate_percent // 100


async def outstanding_debt_cents(db: _Executor, guild_id: int) -> int:
    """What the server still owes creditors who are here. Frozen debt - owed to
    somebody who has left - is not counted: it is not being repaid, so it
    should not use up room under the cap (docs/government.md, Q28)."""
    row = await db.fetchone(
        f"SELECT COALESCE(SUM(remaining_cents), 0) AS owed FROM government_bonds "
        f"WHERE guild_id = ? AND {OWED_BONDS_SQL} AND frozen = 0",
        (guild_id,),
    )
    return row["owed"]


async def frozen_debt_cents(db: _Executor, guild_id: int) -> int:
    row = await db.fetchone(
        f"SELECT COALESCE(SUM(remaining_cents), 0) AS owed FROM government_bonds "
        f"WHERE guild_id = ? AND {OWED_BONDS_SQL} AND frozen = 1",
        (guild_id,),
    )
    return row["owed"]


async def debt_cap_cents(db: _Executor, guild_id: int, now: datetime | None = None) -> int:
    """The most the server may owe: its revenue over the previous DEBT_CAP_DAYS,
    in whole cents, rounded down."""
    return int(await revenue_collected(db, guild_id, DEBT_CAP_DAYS, now) * 100 + _EPSILON)


async def _money(tx: _Executor, guild_id: int, amount: float) -> str:
    """An amount in this server's own currency, for a refusal. Every
    GovernmentError that names a sum goes through this: they used to call
    format_currency with no emoji, so a server with its own currency saw its
    refusals quoted in the default 💰."""
    row = await tx.fetchone("SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,))
    return format_currency(amount, row["currency_emoji"] if row else None)


async def open_bond_sale(
    tx: _Executor, guild_id: int, actor_id: int, total_cents: int, now: datetime | None = None,
) -> None:
    """Puts `total_cents` of bonds up for sale, replacing any sale already
    open; 0 withdraws the sale. The cap is checked here so the Mayor learns
    straight away that a sale can't fill, and again at every purchase, which
    is where it is actually enforced."""
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, MAYOR)
    smallest = min(BOND_DENOMINATIONS_CENTS)
    if total_cents < 0 or total_cents % smallest:
        raise GovernmentError(
            f"A bond sale has to be a whole number of {await _money(tx, guild_id, smallest / 100)} bonds."
        )
    if total_cents:
        cfg = await tx.fetchone(
            "SELECT bond_rate_percent FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        owed_if_sold = bond_owed_cents(total_cents, cfg["bond_rate_percent"])
        room = await debt_cap_cents(tx, guild_id, now) - await outstanding_debt_cents(tx, guild_id)
        if owed_if_sold > room:
            raise GovernmentError(
                f"Selling that much would leave the server owing more than it collected in "
                f"revenue over the last {DEBT_CAP_DAYS} days. It has room for "
                f"{await _money(tx, guild_id, max(0, room) / 100)} more debt, interest included."
            )
    await tx.execute(
        "UPDATE server_config SET bond_sale_cents = ? WHERE guild_id = ?", (total_cents, guild_id)
    )


class BondPurchase(NamedTuple):
    bond_id: int
    principal_cents: int
    owed_cents: int
    rate_percent: int


async def buy_bond(
    tx: _Executor, guild_id: int, buyer_id: int, denomination_cents: int,
    now: datetime | None = None,
) -> BondPurchase:
    if denomination_cents not in BOND_DENOMINATIONS_CENTS:
        raise GovernmentError("That isn't a bond denomination.")
    await ensure_server_row(tx, guild_id)
    cfg = await tx.fetchone(
        "SELECT bond_sale_cents, bond_rate_percent, tax_percent, market_tax_percent FROM server_config "
        "WHERE guild_id = ?",
        (guild_id,),
    )
    if cfg["bond_sale_cents"] < denomination_cents:
        if cfg["bond_sale_cents"] == 0:
            raise GovernmentError("The Mayor isn't selling any bonds right now.")
        raise GovernmentError(
            f"Only {await _money(tx, guild_id, cfg['bond_sale_cents'] / 100)} of bonds are left for sale."
        )
    owed = bond_owed_cents(denomination_cents, cfg["bond_rate_percent"])
    room = await debt_cap_cents(tx, guild_id, now) - await outstanding_debt_cents(tx, guild_id)
    if owed > room:
        raise GovernmentError(
            f"The server can't take on that much debt: it may owe no more than its revenue over "
            f"the last {DEBT_CAP_DAYS} days, and has room for {await _money(tx, guild_id, max(0, room) / 100)}, "
            f"interest included."
        )
    try:
        await deduct_currency_balance(tx, guild_id, buyer_id, denomination_cents / 100)
    except InsufficientQuantity:
        raise GovernmentError(
            f"A {await _money(tx, guild_id, denomination_cents / 100)} bond costs more than your balance."
        )
    await tx.execute(
        "UPDATE server_config SET treasury = treasury + ?, bond_sale_cents = bond_sale_cents - ? "
        "WHERE guild_id = ?",
        (denomination_cents / 100, denomination_cents, guild_id),
    )
    bond_id = await tx.execute(
        "INSERT INTO government_bonds (guild_id, holder_id, principal_cents, rate_percent, "
        "owed_cents, remaining_cents, tax_percent_at_sale, market_tax_percent_at_sale, sold_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, buyer_id, denomination_cents, cfg["bond_rate_percent"], owed, owed,
         cfg["tax_percent"], cfg["market_tax_percent"], sqlite_timestamp(now or clock_now())),
    )
    return BondPurchase(bond_id, denomination_cents, owed, cfg["bond_rate_percent"])


async def bonds_held(db: _Executor, guild_id: int, holder_id: int):
    return await db.fetchall(
        f"SELECT * FROM government_bonds WHERE guild_id = ? AND holder_id = ? "
        f"AND {OWED_BONDS_SQL} ORDER BY bond_id",
        (guild_id, holder_id),
    )


async def guilds_with_repayments(db: _Executor) -> list[int]:
    """The servers whose repayment pool has something in it - the work list
    for the hourly payout."""
    rows = await db.fetchall("SELECT guild_id FROM server_config WHERE repayment_pool > 0")
    return [row["guild_id"] for row in rows]


async def pay_bondholders(tx: _Executor, guild_id: int) -> dict[int, int]:
    """Pays the repayment pool out to the server's active creditors and
    returns bond_id -> cents paid.

    Between creditors, in proportion to what each is still owed in total -
    on what is still owed rather than on what was lent, so every active
    creditor is repaid by the same final payout. Within one creditor's share,
    oldest bond first: it pays off their earliest bond before the next one
    gets anything, so somebody holding several bonds sees them finish one at
    a time. Until it did, every bond was paid pro-rata on its own, and a
    creditor's bonds all crept along together and finished only at the final
    payout.

    Whole cents, split by utils/betting.py: apportion, so what is paid adds up
    to exactly what leaves the pool; the fraction of a cent left over stays in
    it for the next payout.

    Once every active creditor is repaid, whatever is left in the pool moves to
    the treasury - and collect_revenue sends new revenue there too - until somebody is
    owed again.
    """
    cfg = await tx.fetchone("SELECT repayment_pool FROM server_config WHERE guild_id = ?", (guild_id,))
    pool = cfg["repayment_pool"] if cfg else 0.0
    if pool <= 0:
        return {}
    # bond_id order is the order they were sold in, so each creditor's
    # oldest bond comes first in the walk below.
    bonds = await tx.fetchall(
        f"SELECT bond_id, holder_id, remaining_cents FROM government_bonds "
        f"WHERE guild_id = ? AND {OWED_BONDS_SQL} AND frozen = 0 ORDER BY bond_id",
        (guild_id,),
    )
    owed_by_holder: dict[int, int] = {}
    for bond in bonds:
        owed_by_holder[bond["holder_id"]] = owed_by_holder.get(bond["holder_id"], 0) + bond["remaining_cents"]
    total_owed = sum(owed_by_holder.values())
    pay = min(int(pool * 100 + _EPSILON), total_owed)

    unspent = apportion(list(owed_by_holder.items()), pay) if pay else {}
    shares: dict[int, int] = {}
    repaid: dict[int, list[int]] = {}   # holder_id -> the bonds this payout finished
    for bond in bonds:
        share = min(unspent.get(bond["holder_id"], 0), bond["remaining_cents"])
        if not share:
            continue
        unspent[bond["holder_id"]] -= share
        shares[bond["bond_id"]] = share
        await tx.execute(
            "UPDATE government_bonds SET remaining_cents = remaining_cents - ? WHERE bond_id = ?",
            (share, bond["bond_id"]),
        )
        await adjust_currency_balance(tx, guild_id, bond["holder_id"], share / 100)
        if share == bond["remaining_cents"]:
            repaid.setdefault(bond["holder_id"], []).append(bond["bond_id"])
    # A creditor's share is never more than they are owed, so the walk spends
    # all of it; anything unspent would leave the pool without reaching anyone.
    assert not any(unspent.values()), f"unspent bond shares {unspent}"
    for holder_id, bond_ids in repaid.items():
        await announce_repaid_bonds(tx, guild_id, holder_id, bond_ids)

    left = max(0.0, pool - pay / 100)
    if pay == total_owed:
        # Everybody active is repaid. The fraction of a cent the split could
        # not use, and anything beyond the debt, belongs to the treasury now.
        await tx.execute(
            "UPDATE server_config SET repayment_pool = 0, treasury = treasury + ? WHERE guild_id = ?",
            (left, guild_id),
        )
    else:
        await tx.execute(
            "UPDATE server_config SET repayment_pool = ? WHERE guild_id = ?", (left, guild_id)
        )
    return shares


# The prefix of a repaid-bonds notice's key, which is
# "bonds_repaid:<guild_id>:<bond_id>". Not the "bond_repaid:<bond_id>" each bond
# used to be announced under, so a notice raised the old way is never mistaken
# for an open one of these.
BONDS_REPAID_NOTICE = "bonds_repaid"


def repaid_bonds_notice(count: int, returned_cents: int, currency_emoji: str | None) -> tuple[str, str]:
    """The title and body of the notice for `count` repaid bonds that returned
    `returned_cents` between them - what was owed on them, interest included."""
    returned = format_currency(from_cents(returned_cents), currency_emoji)
    if count == 1:
        title = "🏛️ Bond Repaid"
        opening = f"The server has finished repaying one of your bonds, returning **{returned}** with interest."
    else:
        title = "🏛️ Bonds Repaid"
        opening = (
            f"The server has finished repaying **{count}** of your bonds, "
            f"returning **{returned}** in total with interest."
        )
    return title, (
        f"{opening} Every payment went straight to your balance; "
        "`/bonds holdings` shows any you still hold."
    )


async def announce_repaid_bonds(tx: _Executor, guild_id: int, holder_id: int, bond_ids: list[int]) -> None:
    """Tells a holder that `bond_ids` have been repaid, in ONE notice for
    every bond this server has repaid them since they last saw one.

    A payout that finishes several bonds, or several payouts before the holder
    next runs a command, used to raise a notice per bond, each saying the same
    thing. Now the holder's unread notice for this server, if there is one,
    takes the new bonds and is rewritten with the new count and total; if they
    have read it, or there was none, these start a new one. Its figures are
    summed from the bonds carrying its key rather than kept on the notice, so
    they always match what was repaid.

    Shown only in this server: the total is in its currency.
    """
    # At most one unread notice per holder per server ever exists, because
    # this is the only thing that raises one and it reuses any that is open.
    open_notice = await tx.fetchone(
        "SELECT notice_key FROM user_notifications "
        "WHERE user_id = ? AND guild_id = ? AND seen_at IS NULL AND notice_key LIKE ?",
        (holder_id, guild_id, f"{BONDS_REPAID_NOTICE}:%"),
    )
    if open_notice:
        key = open_notice["notice_key"]
    else:
        # Bond ids are never reused, so neither is a key made from one.
        key = f"{BONDS_REPAID_NOTICE}:{guild_id}:{min(bond_ids)}"
    placeholders = ", ".join("?" * len(bond_ids))
    await tx.execute(
        f"UPDATE government_bonds SET repaid_notice_key = ? WHERE bond_id IN ({placeholders})",
        (key, *bond_ids),
    )
    batch = await tx.fetchone(
        "SELECT COUNT(*) AS bonds, SUM(owed_cents) AS returned_cents FROM government_bonds "
        "WHERE guild_id = ? AND repaid_notice_key = ?",
        (guild_id, key),
    )
    cfg = await tx.fetchone("SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,))
    title, body = repaid_bonds_notice(
        batch["bonds"], batch["returned_cents"], cfg["currency_emoji"] if cfg else None
    )
    await set_user_notification(tx, holder_id, key, title, body, guild_id=guild_id)


# ---------------------------------------------------------------------------
# Members leaving and returning
# ---------------------------------------------------------------------------

async def member_left(tx: _Executor, guild_id: int, user_id: int) -> list[str]:
    """Everything that follows from a member leaving the server, returning the
    offices they vacated.

    Their bonds are FROZEN, not voided: payouts skip them and the debt cap
    ignores them until they return (member_returned). Voiding would let an
    admin wipe the server's debt by kicking its creditors (docs/government.md).
    Any office they held is vacated, and votes for or by them are withdrawn -
    a departed member can neither win nor decide an election.
    """
    await tx.execute(
        f"UPDATE government_bonds SET frozen = 1 WHERE guild_id = ? AND holder_id = ? "
        f"AND {OWED_BONDS_SQL}",
        (guild_id, user_id),
    )
    await tx.execute(
        "DELETE FROM government_votes WHERE guild_id = ? AND (voter_id = ? OR candidate_id = ?)",
        (guild_id, user_id, user_id),
    )
    holders = await officeholders(tx, guild_id)
    vacated = [office for office in OFFICES if holders[office] == user_id]
    for office in vacated:
        await tx.execute(f"UPDATE server_config SET {office}_id = NULL WHERE guild_id = ?", (guild_id,))
        if office == MAYOR:
            # A sale is the Mayor's; it does not outlive them.
            await tx.execute(
                "UPDATE server_config SET bond_sale_cents = 0 WHERE guild_id = ?", (guild_id,)
            )
        await post_server_notification(
            tx, guild_id, f"🏛️ The {OFFICE_LABELS[office]} Has Left",
            f"<@{user_id}> has left the server, so the office of {OFFICE_LABELS[office]} is "
            f"vacant until the next election. Voting is every Thursday - `/vote`.",
        )
    return vacated


async def member_returned(tx: _Executor, guild_id: int, user_id: int) -> int:
    """Unfreezes a returning member's bonds. Returns how many were frozen."""
    return await tx.execute_changes(
        f"UPDATE government_bonds SET frozen = 0 WHERE guild_id = ? AND holder_id = ? "
        f"AND frozen = 1 AND {OWED_BONDS_SQL}",
        (guild_id, user_id),
    )


async def frozen_holders(db: _Executor) -> list[tuple[int, int]]:
    """(guild_id, holder_id) for every frozen creditor, so the hourly loop can
    check whether any came back while the bot wasn't watching."""
    rows = await db.fetchall(
        f"SELECT DISTINCT guild_id, holder_id FROM government_bonds "
        f"WHERE frozen = 1 AND {OWED_BONDS_SQL}"
    )
    return [(row["guild_id"], row["holder_id"]) for row in rows]


# ---------------------------------------------------------------------------
# Elections
# ---------------------------------------------------------------------------

def voter_drill_cutoff(voting_day: str) -> str:
    """A drill placed at or before this instant (UTC, datetime('now') layout)
    qualifies its owner to vote on `voting_day`."""
    return sqlite_timestamp(game_midnight(voting_day) - timedelta(days=VOTER_DRILL_DAYS))


async def can_vote(db: _Executor, guild_id: int, voter_id: int, voting_day: str) -> bool:
    """Whether the voter has a drill here that was placed at least
    VOTER_DRILL_DAYS before voting opened - or was placed before the update
    that added elections, which is what a NULL placed_at on a placed drill
    means. Those are grandfathered: nobody could have placed a drill to win an
    election that did not exist, and without them the first election would
    have had no voters."""
    row = await db.fetchone(
        "SELECT 1 FROM drills WHERE guild_id = ? AND owner_id = ? "
        "AND (placed_at IS NULL OR placed_at <= ?) LIMIT 1",
        (guild_id, voter_id, voter_drill_cutoff(voting_day)),
    )
    return row is not None


async def has_played(db: _Executor, guild_id: int, user_id: int) -> bool:
    """Whether a member has ever taken part in this server's economy - held its
    currency or placed a drill here. What a candidate needs."""
    row = await db.fetchone(
        "SELECT 1 FROM server_currency_balances WHERE guild_id = ? AND user_id = ? "
        "UNION ALL SELECT 1 FROM drills WHERE guild_id = ? AND owner_id = ? LIMIT 1",
        (guild_id, user_id, guild_id, user_id),
    )
    return row is not None


async def cast_vote(
    tx: _Executor, guild_id: int, office: str, voter_id: int, candidate_id: int,
    candidate_is_bot: bool = False, now: datetime | None = None,
) -> str:
    """Records a vote, replacing the voter's earlier vote for the same office
    this week. Returns the voting day it counts toward."""
    if office not in OFFICES:
        raise ValueError(f"unknown office {office!r}")
    if not voting_open(now):
        opens = int(game_midnight(next_voting_day(now)).timestamp())
        raise GovernmentError(f"Voting is only open on Thursdays. The next vote opens <t:{opens}:R>.")
    if candidate_id == voter_id:
        raise GovernmentError("You can't vote for yourself.")
    if candidate_is_bot:
        raise GovernmentError("Bots can't hold office.")
    voting_day = current_voting_day(now)
    if not await can_vote(tx, guild_id, voter_id, voting_day):
        raise GovernmentError(
            f"To vote you need a drill that has been placed in this server for at least "
            f"{VOTER_DRILL_DAYS} days when voting opens (or one that was already placed "
            f"when elections were added)."
        )
    if not await has_played(tx, guild_id, candidate_id):
        raise GovernmentError(
            "That member has never played Dragonhoard in this server, so they can't hold office here."
        )
    await tx.execute(
        "INSERT INTO government_votes (guild_id, voting_day, office, voter_id, candidate_id, cast_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (guild_id, voting_day, office, voter_id) "
        "DO UPDATE SET candidate_id = excluded.candidate_id, cast_at = excluded.cast_at",
        (guild_id, voting_day, office, voter_id, candidate_id, sqlite_timestamp(now or clock_now())),
    )
    return voting_day


class Standing(NamedTuple):
    candidate_id: int
    votes: int
    reached_at: str   # when their last vote arrived - when they reached `votes`


async def tally(db: _Executor, guild_id: int, voting_day: str, office: str) -> list[Standing]:
    rows = await db.fetchall(
        "SELECT candidate_id, COUNT(*) AS votes, MAX(cast_at) AS reached_at "
        "FROM government_votes WHERE guild_id = ? AND voting_day = ? AND office = ? "
        "GROUP BY candidate_id",
        (guild_id, voting_day, office),
    )
    return [Standing(row["candidate_id"], row["votes"], row["reached_at"]) for row in rows]


def rank(standings: list[Standing], incumbent: int | None) -> list[int]:
    """Candidates in the order they would take the office: most votes first; on
    a tie, the incumbent, then whoever reached that count first, then the lower
    id so the order never depends on how rows came back."""
    ordered = sorted(
        standings,
        key=lambda s: (-s.votes, s.candidate_id != incumbent, s.reached_at, s.candidate_id),
    )
    return [s.candidate_id for s in ordered]


def decide(
    mayor_ranking: list[int], treasurer_ranking: list[int],
    mayor: int | None, treasurer: int | None,
) -> tuple[int | None, int | None]:
    """Who holds each office after a count, from each ballot's ranking (already
    stripped of anybody who has left) and the current holders.

    The Mayor is decided first. An office nobody voted on keeps its holder. The
    Treasurer ballot skips the new Mayor, since nobody holds both; if that
    leaves nobody, the sitting Treasurer stays - unless the sitting Treasurer
    IS the new Mayor, in which case the seat is empty (docs/government.md,
    Q17 and Q18)."""
    new_mayor = mayor_ranking[0] if mayor_ranking else mayor
    eligible = [c for c in treasurer_ranking if c != new_mayor]
    if eligible:
        new_treasurer = eligible[0]
    elif treasurer == new_mayor:
        new_treasurer = None
    else:
        new_treasurer = treasurer
    return new_mayor, new_treasurer


class ElectionResult(NamedTuple):
    voting_day: str
    mayor: int | None
    treasurer: int | None
    previous_mayor: int | None
    previous_treasurer: int | None


async def guilds_with_due_votes(db: _Executor, now: datetime | None = None) -> list[int]:
    rows = await db.fetchall(
        "SELECT DISTINCT guild_id FROM government_votes WHERE voting_day <= ?",
        (due_voting_day(now),),
    )
    return [row["guild_id"] for row in rows]


async def count_election(
    db: Database, guild_id: int, now: datetime | None = None,
    is_member: Callable[[int], Awaitable[bool]] | None = None,
) -> ElectionResult | None:
    """Counts the oldest finished vote that has not been counted, and returns
    what it decided - or None if there was nothing to count. Normally there is
    one at most; a bot that was down across a whole week leaves two, and the
    next call counts the second.

    Takes the Database rather than a transaction because `is_member` asks
    Discord whether a winner is still in the server, and nothing may await
    the network inside a transaction. That is safe here: the voting day is
    over, so the votes being read cannot change, and the transaction below
    re-checks that they are still there to count before it writes anything -
    two callers racing to count the same election count it once.
    """
    pending = await db.fetchone(
        "SELECT MIN(voting_day) AS day FROM government_votes WHERE guild_id = ? AND voting_day <= ?",
        (guild_id, due_voting_day(now)),
    )
    if pending is None or pending["day"] is None:
        return None
    voting_day = pending["day"]
    holders = await officeholders(db, guild_id)
    rankings = {}
    for office in OFFICES:
        ranking = rank(await tally(db, guild_id, voting_day, office), holders[office])
        if is_member is not None:
            # Only as far down as the first candidate who is still here - the
            # rest could never have won this office.
            present = []
            for candidate in ranking:
                if await is_member(candidate):
                    present.append(candidate)
                    if office == MAYOR or len(present) == 2:
                        break
            ranking = present
        rankings[office] = ranking

    async with db.transaction() as tx:
        still_there = await tx.fetchone(
            "SELECT COUNT(*) AS votes FROM government_votes WHERE guild_id = ? AND voting_day = ?",
            (guild_id, voting_day),
        )
        if not still_there["votes"]:
            return None
        holders = await officeholders(tx, guild_id)
        mayor, treasurer = decide(rankings[MAYOR], rankings[TREASURER], holders[MAYOR], holders[TREASURER])
        await tx.execute(
            "UPDATE server_config SET mayor_id = ?, treasurer_id = ?, election_counted = ? "
            "WHERE guild_id = ?",
            (mayor, treasurer, voting_day, guild_id),
        )
        if mayor != holders[MAYOR]:
            await tx.execute("UPDATE server_config SET bond_sale_cents = 0 WHERE guild_id = ?", (guild_id,))
        await tx.execute(
            "DELETE FROM government_votes WHERE guild_id = ? AND voting_day = ?", (guild_id, voting_day)
        )
        result = ElectionResult(voting_day, mayor, treasurer, holders[MAYOR], holders[TREASURER])
        await post_server_notification(tx, guild_id, "🗳️ Election Results", election_summary(result))
    return result


async def assign_office(tx: _Executor, guild_id: int, office: str, user_id: int | None) -> list[str]:
    """Puts `user_id` in `office` directly, or vacates it with None - the beta
    devtools' way to skip a week of elections (cogs/devtools.py). Returns what
    else it had to change, for the confirmation.

    It keeps the two rules a count keeps, so a tester can never reach a state
    the real game can't: nobody holds both offices (appointing the other
    office's holder vacates that one), and a Mayor's bond sale ends with the
    Mayor.
    """
    if office not in OFFICES:
        raise ValueError(f"unknown office {office!r}")
    await ensure_server_row(tx, guild_id)
    holders = await officeholders(tx, guild_id)
    notes = []
    other = TREASURER if office == MAYOR else MAYOR
    if user_id is not None and holders[other] == user_id:
        await tx.execute(f"UPDATE server_config SET {other}_id = NULL WHERE guild_id = ?", (guild_id,))
        notes.append(f"vacated {OFFICE_LABELS[other]}, since nobody holds both")
    if office == MAYOR and holders[MAYOR] != user_id:
        cancelled = await tx.execute_changes(
            "UPDATE server_config SET bond_sale_cents = 0 WHERE guild_id = ? AND bond_sale_cents > 0",
            (guild_id,),
        )
        if cancelled:
            notes.append("ended the previous Mayor's bond sale")
    await tx.execute(f"UPDATE server_config SET {office}_id = ? WHERE guild_id = ?", (user_id, guild_id))
    return notes


def election_summary(result: ElectionResult) -> str:
    def line(label: str, new: int | None, old: int | None) -> str:
        if new == old:
            return f"**{label}:** " + (f"<@{new}> stays in office" if new else "still vacant")
        if new is None:
            return f"**{label}:** vacant (was <@{old}>)"
        return f"**{label}:** <@{new}>" + (f" (was <@{old}>)" if old else "")

    return (
        line("Mayor", result.mayor, result.previous_mayor) + "\n"
        + line("Treasurer", result.treasurer, result.previous_treasurer) + "\n\n"
        "`/government status` shows what they can do. The next vote is next Thursday."
    )


async def announce_voting(tx: _Executor, guild_id: int, now: datetime | None = None) -> bool:
    """Posts the "polls are open" notice, once per voting day. Returns whether
    it posted."""
    if not voting_open(now):
        return False
    today = current_voting_day(now)
    row = await tx.fetchone("SELECT election_announced FROM server_config WHERE guild_id = ?", (guild_id,))
    if row is None or row["election_announced"] == today:
        return False
    closes = int(game_midnight((date.fromisoformat(today) + timedelta(days=1)).isoformat()).timestamp())
    await post_server_notification(
        tx, guild_id, "🗳️ Election Day",
        f"Voting for this server's Mayor and Treasurer is open until <t:{closes}:t>.\n\n"
        f"Use `/vote mayor` and `/vote treasurer` to back somebody - you can't vote for "
        f"yourself, and you need a drill that has been placed here for at least "
        f"{VOTER_DRILL_DAYS} days. An office nobody votes on keeps its holder.",
    )
    await tx.execute(
        "UPDATE server_config SET election_announced = ? WHERE guild_id = ?", (today, guild_id)
    )
    return True


# ---------------------------------------------------------------------------
# Mayoral projects
# ---------------------------------------------------------------------------

async def spend_treasury(tx: _Executor, guild_id: int, amount: float, slot_credit: float) -> None:
    """THE way currency leaves the treasury: burned, with `slot_credit` added to
    the server's mining slot progress. Every project calls this and nothing
    else takes money out of the treasury, which is what makes every treasury
    payment a burn (see the module docstring)."""
    if amount <= 0:
        raise ValueError("a project must cost something")
    changed = await tx.execute_changes(
        "UPDATE server_config SET treasury = MAX(0, treasury - ?), "
        "mining_slot_credit = mining_slot_credit + ? WHERE guild_id = ? AND treasury >= ?",
        (amount, slot_credit, guild_id, amount - _EPSILON),
    )
    if not changed:
        row = await tx.fetchone("SELECT treasury FROM server_config WHERE guild_id = ?", (guild_id,))
        raise GovernmentError(
            f"That costs {await _money(tx, guild_id, amount)}, and the treasury holds "
            f"{await _money(tx, guild_id, row['treasury'] if row else 0.0)}."
        )
    await record_burned(tx, guild_id, amount)
    if slot_credit:
        await announce_mining_slot_unlocks(tx, guild_id)


async def fund_machine(
    tx: _Executor, guild_id: int, actor_id: int, machine: str, amount: float,
) -> int:
    """Pays treasury money into a machine's upgrade fund, exactly as /donate
    infrastructure does from a player's balance. Returns the machine's level.

    The slot credit is zero here only because bank_infrastructure_fee already
    counts the money toward slots through the machine's own fee column -
    crediting it again would count it twice."""
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, MAYOR)
    await spend_treasury(tx, guild_id, amount, slot_credit=0.0)
    return await bank_infrastructure_fee(tx, guild_id, machine, amount)


async def buy_enhancement(tx: _Executor, guild_id: int, actor_id: int, machine: str) -> tuple[int, float]:
    """Buys a machine its next Infrastructure Enhancement. Returns the level it
    now has and what it cost."""
    if machine not in MACHINES:
        raise ValueError(f"unknown machine {machine!r}")
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, MAYOR)
    row = await tx.fetchone(
        f"SELECT {machine}_enhancement_level AS level FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    price = enhancement_price(row["level"])
    await spend_treasury(tx, guild_id, price, slot_credit=price)
    level = row["level"] + 1
    await tx.execute(
        f"UPDATE server_config SET {machine}_enhancement_level = ? WHERE guild_id = ?",
        (level, guild_id),
    )
    await post_server_notification(
        tx, guild_id, "🏗️ Infrastructure Enhanced",
        f"The Mayor has enhanced the **{machine_label(machine)}** to level {level}. It now runs "
        f"at **{2 ** level:,}x** the speed its own level gives it.",
    )
    return level, price


async def fund_mining_slots(tx: _Executor, guild_id: int, actor_id: int, amount: float) -> float:
    """Mining Slot Enhancement: spends `amount` for MINING_SLOT_ENHANCEMENT_MULTIPLIER
    times as much mining slot progress. Returns the progress it bought."""
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, MAYOR)
    credit = amount * MINING_SLOT_ENHANCEMENT_MULTIPLIER
    await spend_treasury(tx, guild_id, amount, slot_credit=credit)
    return credit


class BonanzaQuote(NamedTuple):
    price: float
    week_gdp: float
    running_until: str | None       # set while one is running
    available_from: datetime | None  # set while the ledger is too young


async def bonanza_quote(db: _Executor, guild_id: int, now: datetime | None = None) -> BonanzaQuote:
    now = now or clock_now()
    week = await window_totals(db, guild_id, window_cutoff(GDP_WEEK_HOURS, now))
    price = bonanza_price(week.gdp)
    row = await db.fetchone("SELECT bonanza_until FROM server_config WHERE guild_id = ?", (guild_id,))
    until = row["bonanza_until"] if row and bonanza_active(row["bonanza_until"], now) else None
    first = await tracked_since(db, guild_id)
    ready_at = (
        datetime.strptime(first, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        + timedelta(days=BONANZA_HISTORY_DAYS)
        if first else None
    )
    available_from = None
    if ready_at is None or ready_at > now:
        # A server with no history at all can't be quoted a date: its ledger
        # starts whenever it first produces something.
        available_from = ready_at or datetime.max.replace(tzinfo=timezone.utc)
    return BonanzaQuote(price, week.gdp, until, available_from)


async def start_bonanza(tx: _Executor, guild_id: int, actor_id: int, now: datetime | None = None) -> BonanzaQuote:
    now = now or clock_now()
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, MAYOR)
    quote = await bonanza_quote(tx, guild_id, now)
    if quote.running_until:
        raise GovernmentError("A Server Bonanza is already running.")
    if quote.available_from is not None:
        raise GovernmentError(
            f"A Bonanza is priced on the last {BONANZA_HISTORY_DAYS} days of production, and "
            f"this server hasn't been producing for {BONANZA_HISTORY_DAYS} days yet."
        )
    await spend_treasury(tx, guild_id, quote.price, slot_credit=quote.price)
    until = sqlite_timestamp(now + timedelta(hours=BONANZA_HOURS))
    await tx.execute("UPDATE server_config SET bonanza_until = ? WHERE guild_id = ?", (until, guild_id))
    ends = int((now + timedelta(hours=BONANZA_HOURS)).timestamp())
    await post_server_notification(
        tx, guild_id, "🎉 Server Bonanza!",
        f"The Mayor has started a Server Bonanza. Until <t:{ends}:f>, every drill in this server "
        f"mines at **double** speed and every machine runs at **double** speed. Drills fill up "
        f"twice as fast too, so `/collect` often.",
    )
    return quote._replace(running_until=until)


async def guilds_with_bonanza(db: _Executor, now: datetime | None = None) -> set[int]:
    """The servers with a Bonanza running - read once per harvest tick."""
    rows = await db.fetchall(
        "SELECT guild_id FROM server_config WHERE bonanza_until > ?",
        (sqlite_timestamp(now or clock_now()),),
    )
    return {row["guild_id"] for row in rows}


# ---------------------------------------------------------------------------
# The status page
# ---------------------------------------------------------------------------

class GovernmentStatus(NamedTuple):
    mayor: int | None
    treasurer: int | None
    fee_share_percent: float
    market_tax_percent: float
    bond_rate_percent: int
    multipliers: dict[str, float]
    enhancements: dict[str, int]
    treasury: float
    repayment_pool: float
    debt_cents: int
    frozen_debt_cents: int
    cap_cents: int
    sale_cents: int
    bonanza_until: str | None
    currency_emoji: str | None


async def government_status(db: _Executor, guild_id: int, now: datetime | None = None) -> GovernmentStatus:
    cfg = await db.fetchone("SELECT * FROM server_config WHERE guild_id = ?", (guild_id,))
    return GovernmentStatus(
        mayor=cfg["mayor_id"],
        treasurer=cfg["treasurer_id"],
        fee_share_percent=cfg["tax_percent"],
        market_tax_percent=cfg["market_tax_percent"],
        bond_rate_percent=cfg["bond_rate_percent"],
        multipliers={m: cfg[f"{m}_fee_multiplier"] for m in MACHINES},
        enhancements={m: cfg[f"{m}_enhancement_level"] for m in MACHINES},
        treasury=cfg["treasury"],
        repayment_pool=cfg["repayment_pool"],
        debt_cents=await outstanding_debt_cents(db, guild_id),
        frozen_debt_cents=await frozen_debt_cents(db, guild_id),
        cap_cents=await debt_cap_cents(db, guild_id, now),
        sale_cents=cfg["bond_sale_cents"],
        bonanza_until=cfg["bonanza_until"] if bonanza_active(cfg["bonanza_until"], now) else None,
        currency_emoji=cfg["currency_emoji"],
    )
