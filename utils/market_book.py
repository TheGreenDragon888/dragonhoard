"""
utils/market_book.py

The player market's two order books and the routing that reads them.

The server has been the counterparty on every market transaction since 1.0
(docs/market.md section 3). As of 1.4 it is one participant among several:
players place their own asks (`market_listings`) and bids (`market_orders`),
and /market buy and /market sell fill against whichever source is cheapest or
dearest before falling back to the server. The design note this implements is
the "Looking ahead - user-driven orders" paragraph in that same section.

TWO THINGS ABOUT THE ECONOMICS ARE LOAD-BEARING, and both are easier to break
here than anywhere else in the codebase:

  A PLAYER-TO-PLAYER FILL MINTS AND BURNS NOTHING. Goods move one way, currency
  the other, and the player sector holds the same total of both afterwards -
  exactly the status a /donate player transfer has (cogs/donate.py). Only the
  SERVER leg touches record_minted/record_burned. Calling either for a player
  leg would report a server printing money it merely moved.

  ONLY THE SERVER LEG CREDITS THE JOB BOARD. This is the one that would
  actually break the economy rather than just misreport it. The board pays
  JOB_BOARD_TARGET_PAYOUT per completion with no daily cap, and what bounds it
  is that goods only re-enter the player sector by being bought back from the
  server at MARKET_BUY_MARKUP times what selling them paid
  (data/materials.py, the bullets under JOB_BOARD_TARGET_PAYOUT). A P2P sale
  never removes goods from the sector at all, so if it credited the board, two
  players could pass one stack back and forth and mint the bonus on every leg
  at no cost to either. plan_sell reports the server's share separately for
  that reason, and cogs/economy.py credits progress with that figure alone.

  As of 1.4.1 the board's materials cannot reach a player book at all
  (SERVER_ONLY_MATERIALS, server_only_error below), so a sale of one is always
  filled by the server in full. The guard above stays: it is what keeps the
  rule true if a row for one of them ever gets onto a book some other way.
"""
from typing import NamedTuple

from data.materials import (
    PERMANENT_MATERIALS,
    PLAYER_PRICE_SCALE,
    SERVER_ONLY_MATERIALS,
    TRADEABLE_ORDER,
    get_material_info,
    player_price_total,
    purchase_unit_price,
    sale_unit_price,
)
from database.db import InsufficientQuantity
from utils.db_helpers import adjust_currency_balance, adjust_user_quantity, get_server_stock

# What a fill came from. The server is a source like any other here, which is
# the whole point of the 1.4 model - it just happens to be the one with an
# unlimited appetite on the bid side and a stock constraint on the ask side.
SERVER = "server"
PLAYER = "player"


class Fill(NamedTuple):
    """One leg of a trade: `quantity` units at `price_units` each, against
    `source`.

    `row_id` is the market_listings/market_orders row this came from, or None
    for the server's own leg. `counterparty` is the other player's user id, or
    None likewise - a receipt names who a player traded with, and the server
    needs no name.

    The SERVER's price is carried in PLAYER_PRICE_SCALE units too, even though
    it is always a whole number of cents. Mixing units here was the obvious
    alternative and a bad one: a trade can span both books, and summing a float
    cents price against an integer sub-cent one is how a total ends up a hair
    off the sum of the legs a receipt just printed.
    """
    source: str
    row_id: int | None
    counterparty: int | None
    quantity: int
    price_units: int

    @property
    def total(self) -> float:
        return player_price_total(self.price_units, self.quantity)


def server_price_units(material_id: str, *, buying: bool) -> int:
    """The server's own quote in PLAYER_PRICE_SCALE units. `buying` is from the
    PLAYER's side: True is what they pay to buy, False what they receive."""
    price = purchase_unit_price(material_id) if buying else sale_unit_price(material_id)
    # Back through cents rather than multiplying the float by the scale: the
    # prices are whole cents by construction (MARKET_PRICE_CENTS), and
    # round() on the cent figure is exact where 0.48 * 10000 is not.
    return round(price * 100) * (PLAYER_PRICE_SCALE // 100)


def fills_total(fills: list[Fill]) -> float:
    """What a plan costs or pays, summed leg by leg. Integer units throughout,
    so this equals the sum of the figures each leg's receipt line shows."""
    return player_price_total(
        sum(f.price_units * f.quantity for f in fills), 1
    )


def server_quantity(fills: list[Fill]) -> int:
    """How much of a plan cleared against the SERVER.

    This is the figure the job board is credited with, and the module docstring
    explains why it can never be the full quantity: a player-to-player sale
    does not remove goods from the player sector, so paying the board for one
    would let two players wash-trade the bonus out of nothing.
    """
    return sum(f.quantity for f in fills if f.source is SERVER)


async def plan_buy(db, guild_id: int, buyer_id: int, material_id: str, quantity: int):
    """How to buy `quantity` units, cheapest source first, as (fills, short).

    `short` is how many units could not be sourced at all. Callers reject on a
    shortfall rather than filling what they can: /market buy has always been
    all-or-nothing ("The server only has N of that in stock"), and a command
    that quotes a price should charge that price or decline.

    Ordered by price explicitly rather than assuming which source is cheaper.
    As of 1.4.1 a material has either a server leg or player legs, never both
    (SERVER_ONLY_MATERIALS), but the ordering has to be right on its own terms
    rather than as a side effect of validation elsewhere.

    A seller's own listings are skipped. Self-trading moves nothing and would
    only put wash volume into the figures docs/market.md section 4 asks to
    track.

    Drill listings carry material_id NULL, so they can never match here - a
    drill is bought by naming the listing, not by asking for a quantity of
    "steel_drill".
    """
    fills: list[Fill] = []
    remaining = quantity

    rows = await db.fetchall(
        "SELECT listing_id, seller_id, quantity, price_units FROM market_listings "
        f"WHERE guild_id = ? AND material_id = ? AND seller_id != ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY price_units ASC, created_at ASC, listing_id ASC",
        (guild_id, material_id, buyer_id),
    )
    for row in rows:
        if remaining <= 0:
            break
        take = min(remaining, row["quantity"])
        fills.append(Fill(PLAYER, row["listing_id"], row["seller_id"], take, row["price_units"]))
        remaining -= take

    if remaining > 0 and material_id in TRADEABLE_ORDER:
        # The server can only sell what it actually holds (docs/market.md
        # section 3), so this leg is bounded by stock where the player legs
        # were bounded by their own quantities.
        stock = await get_server_stock(db, guild_id, material_id)
        take = min(remaining, stock)
        if take > 0:
            fills.append(
                Fill(SERVER, None, None, take, server_price_units(material_id, buying=True))
            )
            remaining -= take

    return fills, remaining


async def plan_sell(db, guild_id: int, seller_id: int, material_id: str, quantity: int):
    """How to sell `quantity` units, dearest bid first, as (fills, short).

    The mirror of plan_buy, with one asymmetry: the server's appetite is
    unlimited on this side. It always accepts a sale at a flat rate however
    much it already holds (docs/market.md section 3), so a tradeable material
    never comes back short - only gemstones, components and containers can,
    and only when no player happens to be bidding for them.
    """
    fills: list[Fill] = []
    remaining = quantity

    rows = await db.fetchall(
        "SELECT order_id, buyer_id, quantity, price_units FROM market_orders "
        f"WHERE guild_id = ? AND material_id = ? AND buyer_id != ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY price_units DESC, created_at ASC, order_id ASC",
        (guild_id, material_id, seller_id),
    )
    for row in rows:
        if remaining <= 0:
            break
        take = min(remaining, row["quantity"])
        fills.append(Fill(PLAYER, row["order_id"], row["buyer_id"], take, row["price_units"]))
        remaining -= take

    if remaining > 0 and material_id in TRADEABLE_ORDER:
        fills.append(
            Fill(SERVER, None, None, remaining, server_price_units(material_id, buying=False))
        )
        remaining = 0

    return fills, remaining


def server_only_error(material_id: str) -> str | None:
    """Why this material can't go on a player book, or None if it can.

    Everything the job board can ask for is traded only with the server
    (data/materials.py: SERVER_ONLY_MATERIALS says why). Both commands check
    this, as they do permanent_material_error, and before the "Unknown item"
    check, so a player sees the rule rather than something that reads as a typo.
    """
    if material_id not in SERVER_ONLY_MATERIALS:
        return None
    name = get_material_info(material_id)["name"]
    return (
        f"{name} is traded only with the server, so every sale can count toward "
        "the job board. Use `/market sell` or `/market buy`."
    )


def permanent_material_error(material_id: str) -> str | None:
    """Why this material can never be traded, or None if it can.

    Exotic Matter accrues and is never disposed of (data/materials.py:
    PERMANENT_MATERIALS). Both commands check this, not just /market list: an
    order nobody is permitted to fill would escrow a buyer's currency against a
    trade that cannot happen.
    """
    if material_id not in PERMANENT_MATERIALS:
        return None
    return (
        "Exotic Matter can't be traded. It accrues and is never spent - it's "
        "reserved for a feature that hasn't been built yet, so there's no "
        "price anyone is in a position to put on it."
    )


# How long a listing or an order stays on the book (1.4.1). Past it, the sweep
# in cogs/economy.py hands back whatever is left, exactly as /market cancel
# would. Stored per row as an absolute expires_at, written with
# ENTRY_LIFETIME_MODIFIER by the command that places the entry.
MARKET_ENTRY_LIFETIME_DAYS = 7
ENTRY_LIFETIME_MODIFIER = f"+{MARKET_ENTRY_LIFETIME_DAYS} days"

# The condition that makes a row part of the book. Every read of either book
# carries it, spelled this way (prefix it with the table's alias where the
# query has one), so an entry that has expired but not yet been swept can be
# neither bought, filled, counted nor shown - the sweep runs hourly, and
# nothing may happen to an entry in the hour after it expired. The same idea
# as utils/drills.py: DRILL_AVAILABLE_SQL.
LIVE_ENTRY_SQL = "expires_at > datetime('now')"

# How many open entries one player may have on each of a server's books at
# once (1.4.1). Per server, like the books themselves, and one limit per book
# rather than one shared between them. An entry stops counting when it fills
# completely or is cancelled - either way its row is deleted - so a partly
# filled one still counts as one.
MAX_OPEN_LISTINGS = 5
MAX_OPEN_ORDERS = 5

LISTINGS = "listings"
ORDERS = "orders"

_ENTRY_LIMITS = {
    LISTINGS: (MAX_OPEN_LISTINGS, "market_listings", "seller_id"),
    ORDERS: (MAX_OPEN_ORDERS, "market_orders", "buyer_id"),
}


class EntryLimitReached(Exception):
    """Raised by check_entry_limit, carrying the refusal to show the player.

    An exception rather than a return value because the check has to run
    inside the transaction that inserts the entry - that is what stops two
    commands sent at once both getting in as the fifth - and the refusal has to
    be sent after it closes, since a Discord call must never be awaited while
    the write lock is held (database/db.py: Database.transaction).
    """


async def open_entry_count(db, guild_id: int, user_id: int, book: str) -> int:
    """How many entries this player has on one of this server's books."""
    _, table, owner = _ENTRY_LIMITS[book]
    row = await db.fetchone(
        f"SELECT COUNT(*) AS n FROM {table} WHERE guild_id = ? AND {owner} = ? "
        f"AND {LIVE_ENTRY_SQL}",
        (guild_id, user_id),
    )
    return row["n"]


async def check_entry_limit(tx, guild_id: int, user_id: int, book: str) -> None:
    """Raises EntryLimitReached if this player already has as many entries on
    `book` as it allows. Call it inside the transaction that inserts the new
    one, before any escrow is taken.

    A player who had more than the limit before it existed keeps what they
    have; they simply can't add another until they are under it.
    """
    limit, _, _ = _ENTRY_LIMITS[book]
    if await open_entry_count(tx, guild_id, user_id, book) >= limit:
        raise EntryLimitReached(
            f"You already have {limit} {book} on this server, which is the limit. "
            f"`/market entries` shows them; `/market cancel` takes one back."
        )


async def consume_listing(tx, listing_id: int, quantity: int) -> None:
    """Takes `quantity` off a listing, deleting the row once it is empty.

    The guard is in the WHERE clause, the way deduct_user_quantity's is: "is
    there enough left" and "take it" are one statement, so two buyers hitting
    the same listing inside one write lock cannot both pass a check that only
    one of them can satisfy. A caller that has already planned against this row
    should never trip it - it is the backstop that turns a future regression
    into a rolled-back transaction rather than a listing that has sold more
    than it held.
    """
    # A fill that takes the whole listing DELETEs it rather than decrementing
    # to zero. The table CHECKs quantity > 0, so "update then delete the empty
    # row" fails on the update - the constraint fires before the second
    # statement can run. Deleting on the exact-match case keeps each branch a
    # single guarded statement, which is what makes it atomic.
    deleted = await tx.execute_changes(
        "DELETE FROM market_listings WHERE listing_id = ? AND quantity = ?",
        (listing_id, quantity),
    )
    if deleted:
        return
    changed = await tx.execute_changes(
        "UPDATE market_listings SET quantity = quantity - ? "
        "WHERE listing_id = ? AND quantity > ?",
        (quantity, listing_id, quantity),
    )
    if not changed:
        raise InsufficientQuantity(f"listing {listing_id} no longer has {quantity}")


async def consume_order(tx, order_id: int, quantity: int) -> None:
    """Takes `quantity` off a standing bid, deleting the row once it is filled.

    The buyer's currency was escrowed when the order was placed, so nothing is
    charged here - the escrow shrinking with the order IS the payment, and the
    seller is credited from it by the caller.
    """
    # Delete-on-exact-match, then decrement - see consume_listing on why the
    # CHECK makes this two branches rather than an update followed by a tidy-up.
    deleted = await tx.execute_changes(
        "DELETE FROM market_orders WHERE order_id = ? AND quantity = ?",
        (order_id, quantity),
    )
    if deleted:
        return
    changed = await tx.execute_changes(
        "UPDATE market_orders SET quantity = quantity - ? "
        "WHERE order_id = ? AND quantity > ?",
        (quantity, order_id, quantity),
    )
    if not changed:
        raise InsufficientQuantity(f"order {order_id} no longer wants {quantity}")


async def listing_depth(db, guild_id: int):
    """The sell side aggregated per material: {material_id: (quantity,
    best_price_units, sellers)}, where "best" is the CHEAPEST ask.

    Aggregated rather than listed row by row because only the best price is
    actionable. plan_buy fills cheapest-first, so on a book where six people
    have undercut each other only the cheapest can be bought at all until it is
    exhausted - the five rows behind it tell a reader nothing they can act on,
    while crowding out every other material in a field that has to end
    somewhere.

    It also bounds the field by the number of MATERIALS rather than the number
    of rows: at most one line per listable material however many people are
    trading, where an un-aggregated book grew without limit and hid whole
    materials behind an "... and N more".

    Depth and seller count are what survive aggregation, and they are the two
    things a buyer actually wants next to the price: how much is on offer at
    all, and whether it is one person or a market.

    Drill listings are excluded (material_id IS NULL) - each is unique, so
    there is nothing to aggregate and they are rendered individually.
    """
    rows = await db.fetchall(
        "SELECT material_id, SUM(quantity) AS quantity, MIN(price_units) AS price_units, "
        "       COUNT(DISTINCT seller_id) AS participants "
        "FROM market_listings WHERE guild_id = ? AND material_id IS NOT NULL "
        f"AND {LIVE_ENTRY_SQL} GROUP BY material_id",
        (guild_id,),
    )
    return {
        r["material_id"]: (r["quantity"], r["price_units"], r["participants"]) for r in rows
    }


async def order_depth(db, guild_id: int):
    """The buy side aggregated per material, where "best" is the DEAREST bid -
    the one plan_sell fills into first. The mirror of listing_depth."""
    rows = await db.fetchall(
        "SELECT material_id, SUM(quantity) AS quantity, MAX(price_units) AS price_units, "
        "       COUNT(DISTINCT buyer_id) AS participants "
        f"FROM market_orders WHERE guild_id = ? AND {LIVE_ENTRY_SQL} GROUP BY material_id",
        (guild_id,),
    )
    return {
        r["material_id"]: (r["quantity"], r["price_units"], r["participants"]) for r in rows
    }


async def listed_drills(db, guild_id: int):
    """Every drill on this server's book, cheapest first, carrying the drill's
    own columns so it can be named by what it is. Each is unique, so these are
    never aggregated."""
    return await db.fetchall(
        "SELECT l.listing_id, l.seller_id, l.price_units, d.drill_type, d.level, "
        "       d.container_type "
        "FROM market_listings l JOIN drills d ON d.drill_id = l.drill_id "
        f"WHERE l.guild_id = ? AND l.{LIVE_ENTRY_SQL} "
        "ORDER BY l.price_units ASC, l.listing_id ASC",
        (guild_id,),
    )


async def own_entries(db, guild_id: int, user_id: int):
    """This player's own open listings and orders, as (kind, id, material_id,
    drill_type, quantity, price_units) rows.

    Feeds /market entries and /market cancel's autocomplete. Aggregating the
    books by material (listing_depth) removed the last place a player could
    see their own entries one by one, so this puts them back.
    """
    listings = await db.fetchall(
        "SELECT l.listing_id AS id, l.material_id, l.quantity, l.price_units, "
        "       l.expires_at, d.drill_type, d.level, d.container_type "
        "FROM market_listings l LEFT JOIN drills d ON d.drill_id = l.drill_id "
        f"WHERE l.guild_id = ? AND l.seller_id = ? AND l.{LIVE_ENTRY_SQL} "
        "ORDER BY l.listing_id",
        (guild_id, user_id),
    )
    orders = await db.fetchall(
        "SELECT order_id AS id, material_id, quantity, price_units, expires_at "
        f"FROM market_orders WHERE guild_id = ? AND buyer_id = ? AND {LIVE_ENTRY_SQL} "
        "ORDER BY order_id",
        (guild_id, user_id),
    )
    return listings, orders


async def listed_quantities(db, guild_id: int, exclude_user: int):
    """Per material, how much OTHER players have listed here and the cheapest
    ask among those listings: {material_id: (quantity, price_units)}.

    One grouped query rather than a plan per material. This feeds
    /market buy's autocomplete, which fires on every keystroke and would
    otherwise walk both books once per candidate material.

    `exclude_user` drops the caller's own listings, because plan_buy will not
    fill against them and offering a material that turns out to be entirely
    your own stock is offering something you cannot buy.

    Drill listings carry material_id NULL and are excluded by the WHERE - they
    are offered individually, by listing id, not as a quantity of a type.
    """
    rows = await db.fetchall(
        "SELECT material_id, SUM(quantity) AS quantity, MIN(price_units) AS price_units "
        "FROM market_listings "
        "WHERE guild_id = ? AND seller_id != ? AND material_id IS NOT NULL "
        f"AND {LIVE_ENTRY_SQL} GROUP BY material_id",
        (guild_id, exclude_user),
    )
    return {r["material_id"]: (r["quantity"], r["price_units"]) for r in rows}


async def bid_quantities(db, guild_id: int, exclude_user: int):
    """Per material, how much OTHER players are bidding for here and the
    DEAREST bid among those orders: {material_id: (quantity, price_units)}.

    The mirror of listed_quantities, for /market sell's autocomplete, and
    excluding the caller's own bids for the same reason.
    """
    rows = await db.fetchall(
        "SELECT material_id, SUM(quantity) AS quantity, MAX(price_units) AS price_units "
        "FROM market_orders WHERE guild_id = ? AND buyer_id != ? "
        f"AND {LIVE_ENTRY_SQL} GROUP BY material_id",
        (guild_id, exclude_user),
    )
    return {r["material_id"]: (r["quantity"], r["price_units"]) for r in rows}


async def return_listing(tx, listing) -> None:
    """Hands a listing's escrowed goods back to its seller and deletes it.

    The one way a listing leaves the book other than by filling: /market
    cancel, the expiry sweep and guild removal all come here. A material goes
    back into the seller's inventory; a drill has its listed_id cleared, which
    is all it takes to put it back in their inventory, since the drill never
    left the drills table.
    """
    await tx.execute("DELETE FROM market_listings WHERE listing_id = ?", (listing["listing_id"],))
    if listing["drill_id"] is not None:
        # Matching on listed_id makes this idempotent, the same property
        # cogs/factory.py relies on when it releases an upgraded drill.
        await tx.execute(
            "UPDATE drills SET listed_id = NULL WHERE drill_id = ? AND listed_id = ?",
            (listing["drill_id"], listing["listing_id"]),
        )
        return
    await adjust_user_quantity(tx, listing["seller_id"], listing["material_id"], listing["quantity"])


async def refund_order(tx, order) -> float:
    """Hands an order's escrowed currency back to its buyer, deletes it, and
    returns the amount.

    The order-side twin of return_listing, with the same three callers. Only
    what is left of the order is refunded - a part that filled was paid out of
    the escrow as it filled. adjust_currency_balance rather than record_minted:
    this currency was never burned when it was escrowed, so returning it mints
    nothing.
    """
    await tx.execute("DELETE FROM market_orders WHERE order_id = ?", (order["order_id"],))
    refund = player_price_total(order["price_units"], order["quantity"])
    await adjust_currency_balance(tx, order["guild_id"], order["buyer_id"], refund)
    return refund


async def guilds_with_expired_entries(db) -> list[int]:
    """Every server with at least one listing or order past its expires_at -
    the only servers the hourly sweep visits. Both halves are lookups on the
    expires_at indexes, so a sweep with nothing to do reads almost nothing."""
    rows = await db.fetchall(
        "SELECT guild_id FROM market_listings WHERE expires_at <= datetime('now') "
        "UNION "
        "SELECT guild_id FROM market_orders WHERE expires_at <= datetime('now')"
    )
    return [row["guild_id"] for row in rows]


async def expire_entries(tx, guild_id: int):
    """Returns every expired listing and order on one server's books, as
    return_listing and refund_order do, and reports what was handed back as
    (listings, orders): the expired rows, each order paired with its refund.

    The caller tells each player, in the same transaction, so a notice can
    never describe a return that rolled back.
    """
    listings = await tx.fetchall(
        "SELECT * FROM market_listings WHERE guild_id = ? AND expires_at <= datetime('now') "
        "ORDER BY listing_id",
        (guild_id,),
    )
    for listing in listings:
        await return_listing(tx, listing)

    orders = await tx.fetchall(
        "SELECT * FROM market_orders WHERE guild_id = ? AND expires_at <= datetime('now') "
        "ORDER BY order_id",
        (guild_id,),
    )
    refunded = [(order, await refund_order(tx, order)) for order in orders]
    return listings, refunded
