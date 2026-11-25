"""
utils/shares.py

Server shares (1.6): a server's government sells shares of the server for
DragonCoin, and each share is owed SHARE_REVENUE_PERCENT of the server's
government revenue forever. cogs/shares.py owns /shares; the Treasurer's and
Mayor's commands are in cogs/government.py. Every rule and every currency
movement is here, so it can be tested against a temporary database without a
gateway connection. The design, and the reasoning behind every number, is
docs/shares.md.

WHERE THE RETURN COMES FROM is the question the design turns on. Not GDP:
GDP is a measurement of what was produced, nobody holds it, and paying a share
"x% of GDP" would print DragonCoin out of nothing - which, since DragonCoin
converts into every server's currency at DRAGONCOIN_RATE, would inflate every
server at once. The return is government REVENUE (the Fee Share, Market Tax
and Exchange Tax): real currency that rises when the server is busier.

THE MONEY RULES:

  A GOVERNMENT SALE: the buyer's DragonCoin is destroyed and DRAGONCOIN_RATE of
  this server's currency per DragonCoin is minted into the treasury - the
  server's capital. Not revenue: no slice of it goes to shareholders and it
  does not count toward the debt cap. No Exchange Tax, which would only be paid
  to the same government.

  DIVIDENDS: utils/government.py: collect_revenue sets the shareholders' slice
  aside in server_config.dividend_pool before the government sees the rest.
  pay_dividends shares the pool out hourly in proportion to shares held, into
  each holder's `accrued`, and converts every whole hundredth of a DragonCoin
  accrued into DragonCoin: the currency is burned, the DragonCoin minted, as
  /exchange does. Accrued exactly, as a REAL, rather than split in whole cents
  by apportion(): a pool of a cent or two an hour split by largest remainder
  would give the odd cent to the biggest holder every time and a small
  holder nothing, ever. The pool and every holder's accrual are this server's
  currency still owed to somebody, so circulating_currency counts them.

  A BUYBACK: the Mayor pays a listing's price out of the treasury
  (spend_treasury - a burn) and the seller receives that much DragonCoin. The
  shares are retired, freeing room under the ceiling. Shares pay forever, so
  this is the only way a server ever gets its revenue back.

  A TRADE BETWEEN PLAYERS moves shares one way and DragonCoin the other, and
  mints and burns nothing. No tax: DragonCoin belongs to no server, so there is
  no obvious government to collect one.

THE GUARD: a Mayor could sell the server's revenue to their own alt for next to
nothing. So a sale may not ask less than price_floor_cents - one year of the
dividend a share would have earned at the last week's revenue - and a buyback
may not pay more than that same figure. Both officers have a hand in any
sale (the Treasurer sets the ceiling, the Mayor sells under it), and
MAX_SHARES caps what can ever be promised.

MEMBERSHIP: every command that trades runs in the server whose shares it
trades - there is no server parameter - and Discord lets only members run
commands in a server, so that is the membership check. A holder who leaves
keeps their shares and keeps earning; they cannot trade until they are back.

Prices and DragonCoin amounts are whole hundredths of a DragonCoin
(users.dragoncoin_cents' unit), called "cents" below.
"""
from datetime import datetime, timedelta
from typing import NamedTuple

from database.db import InsufficientQuantity, _Executor
from utils.db_helpers import ensure_server_row, ensure_user_row, record_burned, record_minted
from utils.exchange import DRAGONCOIN_RATE, get_dragoncoin_cents
from utils.formatting import format_dragoncoin
from utils.government import (
    MAYOR,
    SHARE_REVENUE_PERCENT,
    TREASURER,
    GovernmentError,
    _refuse_second_change,
    game_date,
    game_now,
    has_active_debt,
    outstanding_shares,
    require_office,
    spend_treasury,
)
from utils.market_book import ENTRY_LIFETIME_MODIFIER, LIVE_ENTRY_SQL

# The most shares a server may ever have: at SHARE_REVENUE_PERCENT each, this
# is the most of its revenue a server can promise away. The Treasurer's
# ceiling runs from 0 to this.
MAX_SHARES = 40

# The price floor is this many weeks of the dividend a share would have earned
# at the revenue of the last SHARE_FLOOR_WINDOW_DAYS complete game days.
SHARE_FLOOR_WEEKS = 52
SHARE_FLOOR_WINDOW_DAYS = 7

# How many open listings and orders one player may have on one server's share
# books at once - the player market's limits (utils/market_book.py).
MAX_OPEN_SHARE_LISTINGS = 5
MAX_OPEN_SHARE_ORDERS = 5

# A hundredth of a DragonCoin in this server's currency. The two hundredths
# cancel (utils/exchange.py: quote): 0.01 DragonCoin is DRAGONCOIN_RATE / 100
# currency, which is DRAGONCOIN_RATE cents.
CURRENCY_PER_CENT = DRAGONCOIN_RATE / 100

GOVERNMENT = "government"
PLAYER = "player"

LISTING = "listing"
ORDER = "order"

_EPSILON = 1e-9


class ShareError(GovernmentError):
    """A share trade that cannot happen, with a message written for the
    player. A GovernmentError, so the Mayor's and Treasurer's handlers in
    cogs/government.py catch it too. Raised inside the caller's transaction,
    which aborts it."""


def currency_for(cents: int) -> float:
    """`cents` hundredths of a DragonCoin in this server's currency."""
    return cents * CURRENCY_PER_CENT


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------

async def trailing_revenue(db: _Executor, guild_id: int, now: datetime | None = None) -> float:
    """All of the revenue this server collected over the last
    SHARE_FLOOR_WINDOW_DAYS complete game days, shareholders' slice included.
    Today is left out, as utils/government.py: revenue_collected leaves it out."""
    today = game_now(now).date()
    first = (today - timedelta(days=SHARE_FLOOR_WINDOW_DAYS)).isoformat()
    row = await db.fetchone(
        "SELECT COALESCE(SUM(amount + dividends), 0) AS total FROM government_tax_daily "
        "WHERE guild_id = ? AND day >= ? AND day < ?",
        (guild_id, first, today.isoformat()),
    )
    return row["total"]


def price_floor_cents(week_revenue: float) -> int:
    """The least a share may sell for and the most a buyback may pay: what one
    share would earn in SHARE_FLOOR_WEEKS at `week_revenue` a week, rounded up
    to the next hundredth of a DragonCoin, and never under one. Integer
    throughout: revenue in cents times the weeks times the share's basis
    points, over basis points times the cents in a hundredth of a DragonCoin."""
    revenue_cents = int(week_revenue * 100 + _EPSILON)
    numerator = revenue_cents * SHARE_FLOOR_WEEKS * round(SHARE_REVENUE_PERCENT * 100)
    denominator = 10_000 * DRAGONCOIN_RATE
    return max(1, -(-numerator // denominator))


async def share_price_floor(db: _Executor, guild_id: int, now: datetime | None = None) -> int:
    return price_floor_cents(await trailing_revenue(db, guild_id, now))


async def available_shares(db: _Executor, guild_id: int, holder_id: int) -> int:
    """The shares a player holds here that are not reserved by one of their
    live listings - what they may sell or list. The one place the reservation
    is subtracted (schema.sql, share_holdings)."""
    held = await db.fetchone(
        "SELECT shares FROM share_holdings WHERE guild_id = ? AND holder_id = ?",
        (guild_id, holder_id),
    )
    listed = await db.fetchone(
        f"SELECT COALESCE(SUM(quantity), 0) AS n FROM share_listings "
        f"WHERE guild_id = ? AND seller_id = ? AND {LIVE_ENTRY_SQL}",
        (guild_id, holder_id),
    )
    return (held["shares"] if held else 0) - listed["n"]


async def held_shares(db: _Executor, guild_id: int, holder_id: int) -> int:
    row = await db.fetchone(
        "SELECT shares FROM share_holdings WHERE guild_id = ? AND holder_id = ?",
        (guild_id, holder_id),
    )
    return row["shares"] if row else 0


async def government_sale(db: _Executor, guild_id: int) -> tuple[int, int]:
    """(quantity, price) of the Mayor's sale as it can actually fill right now:
    never more than the room left under the ceiling, which the Treasurer may
    have lowered since the sale opened."""
    cfg = await db.fetchone(
        "SELECT share_ceiling, share_sale_quantity, share_sale_price_cents FROM server_config "
        "WHERE guild_id = ?",
        (guild_id,),
    )
    if cfg is None or cfg["share_sale_quantity"] <= 0:
        return 0, 0
    room = cfg["share_ceiling"] - await outstanding_shares(db, guild_id)
    return max(0, min(cfg["share_sale_quantity"], room)), cfg["share_sale_price_cents"]


class ShareStatus(NamedTuple):
    ceiling: int
    outstanding: int
    sale_quantity: int
    sale_price_cents: int
    floor_cents: int
    week_revenue: float
    best_ask_cents: int | None
    best_bid_cents: int | None
    currency_name: str | None
    currency_emoji: str | None


async def share_status(db: _Executor, guild_id: int, now: datetime | None = None) -> ShareStatus:
    cfg = await db.fetchone(
        "SELECT share_ceiling, currency_name, currency_emoji FROM server_config WHERE guild_id = ?",
        (guild_id,),
    )
    sale_quantity, sale_price = await government_sale(db, guild_id)
    ask = await db.fetchone(
        f"SELECT MIN(price_cents) AS p FROM share_listings WHERE guild_id = ? AND {LIVE_ENTRY_SQL}",
        (guild_id,),
    )
    bid = await db.fetchone(
        f"SELECT MAX(price_cents) AS p FROM share_orders WHERE guild_id = ? AND {LIVE_ENTRY_SQL}",
        (guild_id,),
    )
    asks = [p for p in (ask["p"], sale_price if sale_quantity else None) if p is not None]
    week = await trailing_revenue(db, guild_id, now)
    return ShareStatus(
        ceiling=cfg["share_ceiling"] if cfg else 0,
        outstanding=await outstanding_shares(db, guild_id),
        sale_quantity=sale_quantity,
        sale_price_cents=sale_price,
        floor_cents=price_floor_cents(week),
        week_revenue=week,
        best_ask_cents=min(asks) if asks else None,
        best_bid_cents=bid["p"],
        currency_name=cfg["currency_name"] if cfg else None,
        currency_emoji=cfg["currency_emoji"] if cfg else None,
    )


async def holdings_of(db: _Executor, holder_id: int):
    """Every server a player holds shares in, or has dividends waiting in, with
    what /shares holdings shows for each - largest holding first."""
    return await db.fetchall(
        "SELECT h.guild_id, h.shares, h.accrued, h.dividends_paid_cents, "
        "sc.currency_name, sc.currency_emoji "
        "FROM share_holdings h LEFT JOIN server_config sc ON sc.guild_id = h.guild_id "
        "WHERE h.holder_id = ? AND (h.shares > 0 OR h.accrued > 0) "
        "ORDER BY h.shares DESC, h.guild_id",
        (holder_id,),
    )


async def own_share_entries(db: _Executor, guild_id: int, user_id: int):
    """A player's live listings and orders on this server's share books, as
    (listings, orders), oldest first."""
    listings = await db.fetchall(
        f"SELECT * FROM share_listings WHERE guild_id = ? AND seller_id = ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY listing_id",
        (guild_id, user_id),
    )
    orders = await db.fetchall(
        f"SELECT * FROM share_orders WHERE guild_id = ? AND buyer_id = ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY order_id",
        (guild_id, user_id),
    )
    return listings, orders


# ---------------------------------------------------------------------------
# Moving shares and DragonCoin
# ---------------------------------------------------------------------------

async def _add_shares(tx: _Executor, guild_id: int, holder_id: int, quantity: int) -> None:
    await tx.execute(
        "INSERT INTO share_holdings (guild_id, holder_id, shares) VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id, holder_id) DO UPDATE SET shares = shares + excluded.shares",
        (guild_id, holder_id, quantity),
    )


async def _remove_shares(tx: _Executor, guild_id: int, holder_id: int, quantity: int) -> None:
    """Takes shares off a holding, refusing to take more than it has. A holding
    left with no shares and nothing accrued is deleted; one with dividends
    still waiting stays, so they are paid when they reach a hundredth."""
    changed = await tx.execute_changes(
        "UPDATE share_holdings SET shares = shares - ? "
        "WHERE guild_id = ? AND holder_id = ? AND shares >= ?",
        (quantity, guild_id, holder_id, quantity),
    )
    if not changed:
        raise InsufficientQuantity(f"holder {holder_id} does not have {quantity} shares in {guild_id}")
    await tx.execute(
        "DELETE FROM share_holdings WHERE guild_id = ? AND holder_id = ? AND shares = 0 AND accrued <= 0",
        (guild_id, holder_id),
    )


async def _pay_dragoncoin(tx: _Executor, user_id: int, cents: int) -> None:
    await ensure_user_row(tx, user_id)
    await tx.execute(
        "UPDATE users SET dragoncoin_cents = dragoncoin_cents + ? WHERE user_id = ?",
        (cents, user_id),
    )


async def _take_dragoncoin(tx: _Executor, user_id: int, cents: int) -> None:
    """Charges a player DragonCoin, refusing to overdraw them."""
    await ensure_user_row(tx, user_id)
    changed = await tx.execute_changes(
        "UPDATE users SET dragoncoin_cents = dragoncoin_cents - ? "
        "WHERE user_id = ? AND dragoncoin_cents >= ?",
        (cents, user_id, cents),
    )
    if not changed:
        have = await get_dragoncoin_cents(tx, user_id)
        raise ShareError(f"That costs {format_dragoncoin(cents)}, but you have {format_dragoncoin(have)}.")


async def _consume(tx: _Executor, table: str, key: str, row_id: int, quantity: int) -> None:
    """Takes `quantity` off a listing or order, deleting it once it is empty -
    the two guarded branches utils/market_book.py: consume_listing explains."""
    deleted = await tx.execute_changes(
        f"DELETE FROM {table} WHERE {key} = ? AND quantity = ?", (row_id, quantity)
    )
    if deleted:
        return
    changed = await tx.execute_changes(
        f"UPDATE {table} SET quantity = quantity - ? WHERE {key} = ? AND quantity > ?",
        (quantity, row_id, quantity),
    )
    if not changed:
        raise InsufficientQuantity(f"{table} {row_id} no longer has {quantity}")


class ShareFill(NamedTuple):
    """One leg of a share trade: `quantity` shares at `price_cents` each,
    against a player's listing or order (`row_id`, `counterparty`) or the
    government's sale (both None)."""

    source: str
    row_id: int | None
    counterparty: int | None
    quantity: int
    price_cents: int

    @property
    def total_cents(self) -> int:
        return self.quantity * self.price_cents


def fills_total_cents(fills: list[ShareFill]) -> int:
    return sum(fill.total_cents for fill in fills)


async def plan_share_buy(db: _Executor, guild_id: int, buyer_id: int, quantity: int):
    """How to buy `quantity` shares, cheapest first across players' listings
    and the government's sale, as (fills, short). At one price players' listings
    go first, oldest first. The buyer's own listings are skipped."""
    rows = await db.fetchall(
        f"SELECT listing_id, seller_id, quantity, price_cents FROM share_listings "
        f"WHERE guild_id = ? AND seller_id != ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY price_cents ASC, created_at ASC, listing_id ASC",
        (guild_id, buyer_id),
    )
    sources = [
        (row["price_cents"], 0, ShareFill(PLAYER, row["listing_id"], row["seller_id"], row["quantity"], row["price_cents"]))
        for row in rows
    ]
    sale_quantity, sale_price = await government_sale(db, guild_id)
    if sale_quantity:
        sources.append((sale_price, 1, ShareFill(GOVERNMENT, None, None, sale_quantity, sale_price)))
    # A stable sort, so listings at one price keep their oldest-first order.
    sources.sort(key=lambda source: (source[0], source[1]))

    fills, remaining = [], quantity
    for _, _, source in sources:
        if remaining <= 0:
            break
        take = min(remaining, source.quantity)
        fills.append(source._replace(quantity=take))
        remaining -= take
    return fills, remaining


async def plan_share_sell(db: _Executor, guild_id: int, seller_id: int, quantity: int):
    """How to sell `quantity` shares, dearest bid first, as (fills, short). The
    government does not bid; its buybacks take listings."""
    rows = await db.fetchall(
        f"SELECT order_id, buyer_id, quantity, price_cents FROM share_orders "
        f"WHERE guild_id = ? AND buyer_id != ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY price_cents DESC, created_at ASC, order_id ASC",
        (guild_id, seller_id),
    )
    fills, remaining = [], quantity
    for row in rows:
        if remaining <= 0:
            break
        take = min(remaining, row["quantity"])
        fills.append(ShareFill(PLAYER, row["order_id"], row["buyer_id"], take, row["price_cents"]))
        remaining -= take
    return fills, remaining


class ShareTrade(NamedTuple):
    fills: list[ShareFill]
    total_cents: int
    dragoncoin_after: int
    shares_after: int


async def buy_shares(tx: _Executor, guild_id: int, buyer_id: int, quantity: int) -> ShareTrade:
    """Buys `quantity` of this server's shares, all of them or none - a command
    that quotes a price charges it or declines, as /market buy does."""
    if quantity <= 0:
        raise ValueError("buy at least one share")
    await ensure_server_row(tx, guild_id)
    fills, short = await plan_share_buy(tx, guild_id, buyer_id, quantity)
    if short:
        on_sale = quantity - short
        raise ShareError(
            f"Only {on_sale:,} shares are for sale here." if on_sale else "No shares are for sale here."
        )
    total = fills_total_cents(fills)
    await _take_dragoncoin(tx, buyer_id, total)

    for fill in fills:
        if fill.source == GOVERNMENT:
            changed = await tx.execute_changes(
                "UPDATE server_config SET share_sale_quantity = share_sale_quantity - ? "
                "WHERE guild_id = ? AND share_sale_quantity >= ?",
                (fill.quantity, guild_id, fill.quantity),
            )
            if not changed:
                raise InsufficientQuantity(f"the government's sale in {guild_id} ran out")
            proceeds = currency_for(fill.total_cents)
            await tx.execute(
                "UPDATE server_config SET treasury = treasury + ? WHERE guild_id = ?",
                (proceeds, guild_id),
            )
            await record_minted(tx, guild_id, proceeds)
        else:
            await _consume(tx, "share_listings", "listing_id", fill.row_id, fill.quantity)
            await _remove_shares(tx, guild_id, fill.counterparty, fill.quantity)
            await _pay_dragoncoin(tx, fill.counterparty, fill.total_cents)
    await _add_shares(tx, guild_id, buyer_id, quantity)
    return ShareTrade(
        fills, total, await get_dragoncoin_cents(tx, buyer_id), await held_shares(tx, guild_id, buyer_id)
    )


async def sell_shares(tx: _Executor, guild_id: int, seller_id: int, quantity: int) -> ShareTrade:
    """Sells `quantity` of the player's shares here into players' bids, all of
    them or none. The bids' escrowed DragonCoin is the payment."""
    if quantity <= 0:
        raise ValueError("sell at least one share")
    available = await available_shares(tx, guild_id, seller_id)
    if available < quantity:
        raise ShareError(_not_enough_shares(available))
    fills, short = await plan_share_sell(tx, guild_id, seller_id, quantity)
    if short:
        bid_for = quantity - short
        raise ShareError(
            f"Only {bid_for:,} shares are bid for here." if bid_for
            else "Nobody is bidding for shares here. `/shares list` offers yours."
        )
    for fill in fills:
        await _consume(tx, "share_orders", "order_id", fill.row_id, fill.quantity)
        await _add_shares(tx, guild_id, fill.counterparty, fill.quantity)
    total = fills_total_cents(fills)
    await _remove_shares(tx, guild_id, seller_id, quantity)
    await _pay_dragoncoin(tx, seller_id, total)
    return ShareTrade(
        fills, total, await get_dragoncoin_cents(tx, seller_id), await held_shares(tx, guild_id, seller_id)
    )


def _not_enough_shares(available: int) -> str:
    if available <= 0:
        return "You have no shares here to sell. Listed shares can't be sold twice."
    return f"You have {available:,} shares here to sell. Listed shares can't be sold twice."


async def _check_entry_limit(tx: _Executor, guild_id: int, user_id: int, kind: str) -> None:
    table, owner, limit = (
        ("share_listings", "seller_id", MAX_OPEN_SHARE_LISTINGS) if kind == LISTING
        else ("share_orders", "buyer_id", MAX_OPEN_SHARE_ORDERS)
    )
    row = await tx.fetchone(
        f"SELECT COUNT(*) AS n FROM {table} WHERE guild_id = ? AND {owner} = ? AND {LIVE_ENTRY_SQL}",
        (guild_id, user_id),
    )
    if row["n"] >= limit:
        raise ShareError(
            f"You already have {limit} share {kind}s here, the limit. `/shares cancel` takes one back."
        )


async def list_shares(tx: _Executor, guild_id: int, seller_id: int, quantity: int, price_cents: int) -> int:
    """Offers `quantity` of the player's shares at `price_cents` each. They stay
    in the holding, reserved, so they keep earning until sold. Returns the
    listing's id."""
    if quantity <= 0 or price_cents <= 0:
        raise ValueError("a listing needs a quantity and a price")
    await _check_entry_limit(tx, guild_id, seller_id, LISTING)
    available = await available_shares(tx, guild_id, seller_id)
    if available < quantity:
        raise ShareError(_not_enough_shares(available).replace("to sell", "to list"))
    return await tx.execute(
        "INSERT INTO share_listings (guild_id, seller_id, quantity, price_cents, expires_at) "
        "VALUES (?, ?, ?, ?, datetime('now', ?))",
        (guild_id, seller_id, quantity, price_cents, ENTRY_LIFETIME_MODIFIER),
    )


async def order_shares(tx: _Executor, guild_id: int, buyer_id: int, quantity: int, price_cents: int) -> int:
    """Bids for `quantity` shares at `price_cents` each, escrowing the
    DragonCoin. Returns the order's id."""
    if quantity <= 0 or price_cents <= 0:
        raise ValueError("an order needs a quantity and a price")
    await _check_entry_limit(tx, guild_id, buyer_id, ORDER)
    await _take_dragoncoin(tx, buyer_id, quantity * price_cents)
    return await tx.execute(
        "INSERT INTO share_orders (guild_id, buyer_id, quantity, price_cents, expires_at) "
        "VALUES (?, ?, ?, ?, datetime('now', ?))",
        (guild_id, buyer_id, quantity, price_cents, ENTRY_LIFETIME_MODIFIER),
    )


async def refund_share_order(tx: _Executor, order) -> int:
    """Hands what is left of an order's escrow back and deletes it; returns the
    refund. The one place an order's DragonCoin goes back: cancel, expiry and
    guild removal all come here."""
    await tx.execute("DELETE FROM share_orders WHERE order_id = ?", (order["order_id"],))
    refund = order["quantity"] * order["price_cents"]
    await _pay_dragoncoin(tx, order["buyer_id"], refund)
    return refund


async def cancel_share_entry(tx: _Executor, guild_id: int, user_id: int, kind: str, entry_id: int):
    """Withdraws one of the player's entries here. Returns (row, refund_cents):
    a listing reserved its shares in place, so it has nothing to hand back."""
    if kind == LISTING:
        row = await tx.fetchone(
            "SELECT * FROM share_listings WHERE listing_id = ? AND guild_id = ? AND seller_id = ?",
            (entry_id, guild_id, user_id),
        )
        if row is None:
            raise ShareError("That isn't one of your share listings here. `/shares status` shows yours.")
        await tx.execute("DELETE FROM share_listings WHERE listing_id = ?", (entry_id,))
        return row, 0
    row = await tx.fetchone(
        "SELECT * FROM share_orders WHERE order_id = ? AND guild_id = ? AND buyer_id = ?",
        (entry_id, guild_id, user_id),
    )
    if row is None:
        raise ShareError("That isn't one of your share orders here. `/shares status` shows yours.")
    return row, await refund_share_order(tx, row)


async def guilds_with_expired_share_entries(db: _Executor) -> list[int]:
    rows = await db.fetchall(
        "SELECT guild_id FROM share_listings WHERE expires_at <= datetime('now') "
        "UNION "
        "SELECT guild_id FROM share_orders WHERE expires_at <= datetime('now')"
    )
    return [row["guild_id"] for row in rows]


async def expire_share_entries(tx: _Executor, guild_id: int):
    """Clears every expired entry on one server's share books, as (listings,
    [(order, refund)]). Expired rows are already off the book - every read
    spells LIVE_ENTRY_SQL - so this only tidies them away and hands bids'
    DragonCoin back."""
    listings = await tx.fetchall(
        "SELECT * FROM share_listings WHERE guild_id = ? AND expires_at <= datetime('now') "
        "ORDER BY listing_id",
        (guild_id,),
    )
    await tx.execute(
        "DELETE FROM share_listings WHERE guild_id = ? AND expires_at <= datetime('now')", (guild_id,)
    )
    orders = await tx.fetchall(
        "SELECT * FROM share_orders WHERE guild_id = ? AND expires_at <= datetime('now') "
        "ORDER BY order_id",
        (guild_id,),
    )
    return listings, [(order, await refund_share_order(tx, order)) for order in orders]


async def withdraw_share_books(tx: _Executor, guild_id: int) -> int:
    """Takes every entry off a server's share books when the bot is removed,
    handing bids' DragonCoin back - the reason cogs/mining.py:
    _withdraw_guild_market gives for the player market. The shares themselves
    stay with their holders. Returns how many rows were withdrawn."""
    withdrawn = await tx.execute_changes("DELETE FROM share_listings WHERE guild_id = ?", (guild_id,))
    orders = await tx.fetchall("SELECT * FROM share_orders WHERE guild_id = ?", (guild_id,))
    for order in orders:
        await refund_share_order(tx, order)
    return withdrawn + len(orders)


# ---------------------------------------------------------------------------
# The government's side
# ---------------------------------------------------------------------------

async def set_share_ceiling(
    tx: _Executor, guild_id: int, actor_id: int, ceiling: int, now: datetime | None = None,
) -> None:
    """The Treasurer's setting: how many of the server's shares may exist.
    Lowering it below what exists cancels nothing - a share is owed forever -
    it only stops new sales."""
    if not 0 <= ceiling <= MAX_SHARES:
        raise GovernmentError(f"The share ceiling must be between 0 and {MAX_SHARES}.")
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, TREASURER)
    row = await tx.fetchone("SELECT share_ceiling_changed FROM server_config WHERE guild_id = ?", (guild_id,))
    _refuse_second_change(row["share_ceiling_changed"], "share ceiling", now)
    await tx.execute(
        "UPDATE server_config SET share_ceiling = ?, share_ceiling_changed = ? WHERE guild_id = ?",
        (ceiling, game_date(now), guild_id),
    )


async def open_share_sale(
    tx: _Executor, guild_id: int, actor_id: int, quantity: int, price_cents: int,
    now: datetime | None = None,
) -> None:
    """The Mayor's sale: `quantity` new shares at `price_cents` each, replacing
    any sale open. 0 withdraws it. The ceiling is checked here so the Mayor
    hears straight away, and again at every purchase (government_sale)."""
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, MAYOR)
    if quantity < 0:
        raise GovernmentError("A share sale can't be negative.")
    if quantity:
        cfg = await tx.fetchone("SELECT share_ceiling FROM server_config WHERE guild_id = ?", (guild_id,))
        outstanding = await outstanding_shares(tx, guild_id)
        room = cfg["share_ceiling"] - outstanding
        if room <= 0:
            raise GovernmentError(
                f"{outstanding:,} of {cfg['share_ceiling']:,} shares allowed already exist. "
                "The Treasurer sets the limit with `/treasurer shares`."
            )
        if quantity > room:
            raise GovernmentError(f"Only {room:,} more shares are allowed. `/treasurer shares` sets the limit.")
        floor = await share_price_floor(tx, guild_id, now)
        if price_cents < floor:
            raise GovernmentError(
                f"A share can't sell for less than {format_dragoncoin(floor)}: a year of its "
                "dividends at last week's revenue."
            )
    await tx.execute(
        "UPDATE server_config SET share_sale_quantity = ?, share_sale_price_cents = ? WHERE guild_id = ?",
        (quantity, price_cents if quantity else 0, guild_id),
    )


class Buyback(NamedTuple):
    fills: list[ShareFill]
    cost: float            # this server's currency, out of the treasury
    max_price_cents: int   # the price limit actually applied


async def buy_back_shares(
    tx: _Executor, guild_id: int, actor_id: int, quantity: int, max_price_cents: int,
    now: datetime | None = None,
) -> Buyback:
    """The Mayor buys up to `quantity` listed shares, cheapest first, at no more
    than `max_price_cents` each and never more than the price floor - the same
    figure a sale may not go under - and retires them. Paid out of the treasury
    through spend_treasury, which burns it; the sellers receive DragonCoin."""
    await ensure_server_row(tx, guild_id)
    await require_office(tx, guild_id, actor_id, MAYOR)
    if quantity <= 0:
        raise GovernmentError("Buy back at least one share.")
    floor = await share_price_floor(tx, guild_id, now)
    limit = min(max_price_cents, floor)
    rows = await tx.fetchall(
        f"SELECT listing_id, seller_id, quantity, price_cents FROM share_listings "
        f"WHERE guild_id = ? AND price_cents <= ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY price_cents ASC, created_at ASC, listing_id ASC",
        (guild_id, limit),
    )
    fills, remaining = [], quantity
    for row in rows:
        if remaining <= 0:
            break
        take = min(remaining, row["quantity"])
        fills.append(ShareFill(PLAYER, row["listing_id"], row["seller_id"], take, row["price_cents"]))
        remaining -= take
    if not fills:
        capped = " (the most the government may pay)" if limit < max_price_cents else ""
        raise GovernmentError(f"No shares are listed at {format_dragoncoin(limit)}{capped} or less.")

    cost = currency_for(fills_total_cents(fills))
    await spend_treasury(tx, guild_id, cost, slot_credit=0.0)
    for fill in fills:
        await _consume(tx, "share_listings", "listing_id", fill.row_id, fill.quantity)
        await _remove_shares(tx, guild_id, fill.counterparty, fill.quantity)
        await _pay_dragoncoin(tx, fill.counterparty, fill.total_cents)
    return Buyback(fills, cost, limit)


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------

async def guilds_with_dividends(db: _Executor) -> list[int]:
    """The servers with revenue set aside for shareholders - the work list for
    the hourly payout. A server with no shares never has any."""
    rows = await db.fetchall("SELECT guild_id FROM server_config WHERE dividend_pool > 0")
    return [row["guild_id"] for row in rows]


async def pay_dividends(tx: _Executor, guild_id: int) -> dict[int, int]:
    """Shares the dividend pool out to this server's holders in proportion to
    their shares, then turns every whole hundredth of a DragonCoin each holder
    has accrued into DragonCoin. Returns holder_id -> hundredths paid.

    Accrued exactly rather than split in whole cents (see the module
    docstring). If nobody holds a share any more - all bought back - what was
    set aside goes back to the government, where it would have gone without
    them.
    """
    cfg = await tx.fetchone("SELECT dividend_pool FROM server_config WHERE guild_id = ?", (guild_id,))
    pool = cfg["dividend_pool"] if cfg else 0.0
    if pool <= 0:
        return {}
    holders = await tx.fetchall(
        "SELECT holder_id, shares FROM share_holdings WHERE guild_id = ? AND shares > 0 ORDER BY holder_id",
        (guild_id,),
    )
    total = sum(row["shares"] for row in holders)
    if not total:
        column = "repayment_pool" if await has_active_debt(tx, guild_id) else "treasury"
        await tx.execute(
            f"UPDATE server_config SET {column} = {column} + dividend_pool, dividend_pool = 0 "
            "WHERE guild_id = ?",
            (guild_id,),
        )
        return {}
    for row in holders:
        await tx.execute(
            "UPDATE share_holdings SET accrued = accrued + ? WHERE guild_id = ? AND holder_id = ?",
            (pool * row["shares"] / total, guild_id, row["holder_id"]),
        )
    await tx.execute("UPDATE server_config SET dividend_pool = 0 WHERE guild_id = ?", (guild_id,))

    paid = {}
    ready = await tx.fetchall(
        "SELECT holder_id, accrued FROM share_holdings WHERE guild_id = ? AND accrued >= ?",
        (guild_id, CURRENCY_PER_CENT - _EPSILON),
    )
    for row in ready:
        cents = int(row["accrued"] / CURRENCY_PER_CENT + _EPSILON)
        if cents <= 0:
            continue
        converted = currency_for(cents)
        await tx.execute(
            "UPDATE share_holdings SET accrued = MAX(0, accrued - ?), "
            "dividends_paid_cents = dividends_paid_cents + ? WHERE guild_id = ? AND holder_id = ?",
            (converted, cents, guild_id, row["holder_id"]),
        )
        await record_burned(tx, guild_id, converted)
        await _pay_dragoncoin(tx, row["holder_id"], cents)
        paid[row["holder_id"]] = cents
    return paid
