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
"""
from typing import NamedTuple

from data.materials import (
    PERMANENT_MATERIALS,
    PLAYER_PRICE_SCALE,
    TRADEABLE_ORDER,
    player_price_bounds,
    player_price_total,
    purchase_unit_price,
    sale_unit_price,
)
from database.db import InsufficientQuantity
from utils.db_helpers import get_server_stock

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

    Ordered by price explicitly rather than assuming the band rule has put
    players first. It does for the six materials the server trades, but the
    other sixteen have no server leg at all, and the ordering has to be right
    on its own terms rather than as a side effect of validation elsewhere.

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
        "WHERE guild_id = ? AND material_id = ? AND seller_id != ? "
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
        "WHERE guild_id = ? AND material_id = ? AND buyer_id != ? "
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


def listing_price_error(material_id: str, price_units: int) -> str | None:
    """Why this ask is not worth putting on the book, or None if it is fine.

    The rule is the server's own quotes (data/materials.py:
    player_price_bounds): an ask at or above what the server charges is one
    nobody would take when the server sells the same thing cheaper, and an ask
    at or below what the server PAYS is one the seller should have taken to the
    server instead. Neither is a trade anyone benefits from, so neither belongs
    on a book people read to find one.

    Untraded materials - gemstones, components, containers, drills - have no
    server quote to be measured against and are unbounded.
    """
    low, high = player_price_bounds(material_id)
    if low is None:
        return None
    if price_units >= high:
        return (
            f"The server already sells that for {_p(high)} - nobody would buy "
            f"it from you at {_p(price_units)} or more. Ask under {_p(high)}."
        )
    if price_units <= low:
        return (
            f"The server already pays {_p(low)} for that, so you would be "
            f"better off with `/market sell`. Ask over {_p(low)}."
        )
    return None


def order_price_error(material_id: str, price_units: int) -> str | None:
    """Why this bid is not worth putting on the book, or None if it is fine.

    The mirror of listing_price_error, and the same rule read from the other
    side: a bid at or below what the server pays is one nobody would fill when
    the server pays the same or more, and a bid at or above what the server
    CHARGES is one the buyer should have taken to the server instead.
    """
    low, high = player_price_bounds(material_id)
    if low is None:
        return None
    if price_units <= low:
        return (
            f"The server already pays {_p(low)} for that - nobody would sell "
            f"to you at {_p(price_units)} or less. Bid over {_p(low)}."
        )
    if price_units >= high:
        return (
            f"The server already sells that for {_p(high)}, so you would be "
            f"better off with `/market buy`. Bid under {_p(high)}."
        )
    return None


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


def _p(price_units: int) -> str:
    """A bound as it appears in a rejection. Local to this module because it is
    only ever used inside these messages; format_compact_price is for columns
    that have to line up, which prose does not."""
    from utils.formatting import format_price
    from data.materials import PLAYER_PRICE_SCALE

    return format_price(price_units / PLAYER_PRICE_SCALE)


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
    of rows: at most twenty-two lines however many people are trading, where an
    un-aggregated book grew without limit and hid whole materials behind an
    "... and N more".

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
        "GROUP BY material_id",
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
        "FROM market_orders WHERE guild_id = ? GROUP BY material_id",
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
        "WHERE l.guild_id = ? ORDER BY l.price_units ASC, l.listing_id ASC",
        (guild_id,),
    )


async def own_entries(db, guild_id: int, user_id: int):
    """This player's own open listings and orders, as (kind, id, material_id,
    drill_type, quantity, price_units) rows.

    /market cancel takes an id, and the only other place an id appears is the
    receipt from when the entry was made - which scrolls away. Aggregating the
    books by material (listing_depth) removed the last place a player could
    look one up, so this puts their own back where they can see them.
    """
    listings = await db.fetchall(
        "SELECT l.listing_id AS id, l.material_id, l.quantity, l.price_units, "
        "       d.drill_type, d.level, d.container_type "
        "FROM market_listings l LEFT JOIN drills d ON d.drill_id = l.drill_id "
        "WHERE l.guild_id = ? AND l.seller_id = ? ORDER BY l.listing_id",
        (guild_id, user_id),
    )
    orders = await db.fetchall(
        "SELECT order_id AS id, material_id, quantity, price_units "
        "FROM market_orders WHERE guild_id = ? AND buyer_id = ? ORDER BY order_id",
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
        "GROUP BY material_id",
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
        "GROUP BY material_id",
        (guild_id, exclude_user),
    )
    return {r["material_id"]: (r["quantity"], r["price_units"]) for r in rows}
