"""
utils/exchange.py

The DragonCoin exchange (1.5.1): every server buys and sells DragonCoin at a
fixed DRAGONCOIN_RATE of its own currency, plus or minus the Exchange Tax its
Treasurer sets. cogs/exchange.py owns /exchange and its embeds; every rule and
every currency movement is here, so it can be tested against a temporary
database without a gateway connection. The design is docs/market.md section 2.

DragonCoin is global - users.dragoncoin_cents, one balance per player rather
than one per server - which is what makes it the one thing that carries
between servers. It is held in whole hundredths, for the reason a bet's stake
is held in cents (see the column's comment in schema.sql).

THE MONEY RULES, for d hundredths of a DragonCoin at an Exchange Tax of t:

  base = d * DRAGONCOIN_RATE cents of the server's currency, always whole.
  tax = rate_cut(base, t), rounded down (utils/government.py).

  BUYING: the player pays base + tax. The base is BURNED - it leaves this
  server's economy, and the DragonCoin is what it became. It is not a fee, so
  it levels no machine and counts toward no mining slot (record_burned, never
  bank_infrastructure_fee). The tax is government revenue, held exactly as
  the Market Tax is (collect_revenue).

  SELLING: the player gives up d. The base is MINTED into this server's
  economy; the player receives base - tax and the government holds the tax.

  So a round trip in one server creates nothing: at 0% the base burned on the
  way in is the base minted on the way out, and at any other rate the player
  has paid the tax twice. Across servers, currency moves at par - what one
  server burns, another mints - and a server's only say in that is its
  Exchange Tax (docs/market.md section 2).

  The sell side's cut is valued in the server's currency rather than taken in
  DragonCoin hundredths. Counted in hundredths it would round away on small
  sales - 0.09 DragonCoin at 10% is 0.009, which rounds down to nothing - so
  selling in small pieces would dodge it. Counted in currency cents, rounding
  never costs the government a whole cent on an exchange.

What a player types is DragonCoin, rounded to the nearest hundredth (half up,
on the number as typed), and the cost is worked out from the rounded amount -
the owner's specification for 1.5.1.
"""
import math
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import NamedTuple

from database.db import InsufficientQuantity, _Executor
from utils.db_helpers import (
    adjust_currency_balance,
    deduct_currency_balance,
    ensure_server_row,
    ensure_user_row,
    get_currency_balance,
    record_burned,
    record_minted,
)
from utils.government import collect_revenue, rate_cut

# How many units of any server's currency one DragonCoin costs before tax, and
# pays before tax. The same on every server.
DRAGONCOIN_RATE = 1000

BUY = "buy"
SELL = "sell"


def dragoncoin_cents(amount: float) -> int:
    """`amount` DragonCoin, rounded to the nearest hundredth, in hundredths.

    Rounded half up on the decimal the player typed: discord.py hands over a
    float, and repr() is the shortest text that reads back as that float, so
    2.675 rounds to 2.68. Rounding the float itself would not: 2.675 is stored
    a hair under 2.675, so round(2.675, 2) is 2.67 (tests/test_exchange.py).
    Anything that is not a finite number is 0, which callers refuse.
    """
    if not math.isfinite(amount):
        return 0
    try:
        hundredths = Decimal(repr(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return 0
    return int(hundredths * 100)


class ExchangeQuote(NamedTuple):
    """What exchanging `dragoncoin_cents` hundredths costs or pays, in cents of
    the server's currency."""

    dragoncoin_cents: int
    base_cents: int
    tax_cents: int

    @property
    def cost_cents(self) -> int:
        """What buying it costs: the price plus the Exchange Tax."""
        return self.base_cents + self.tax_cents

    @property
    def proceeds_cents(self) -> int:
        """What selling it pays: the price less the Exchange Tax."""
        return self.base_cents - self.tax_cents


def quote(dragoncoin_cents: int, percent: float) -> ExchangeQuote:
    """The price of `dragoncoin_cents` hundredths of a DragonCoin at an
    Exchange Tax of `percent`, either way. A hundredth of a DragonCoin is a
    hundredth of DRAGONCOIN_RATE currency, which is DRAGONCOIN_RATE cents - the
    two hundredths cancel, so the base is always a whole number of cents."""
    base = dragoncoin_cents * DRAGONCOIN_RATE
    return ExchangeQuote(dragoncoin_cents, base, rate_cut(base, percent))


def max_buyable_cents(balance: float, percent: float) -> int:
    """The most DragonCoin, in hundredths, that `balance` of the server's
    currency can buy at an Exchange Tax of `percent`.

    The division gives the answer if the tax were never rounded; rounding it
    down can only make a purchase cheaper, by under a cent, so at most one more
    hundredth fits and the loop checks for it. The 1e-9 is the nudge
    cogs/economy.py: max_affordable uses, so a balance that is a whole number
    of cents on paper but a hair under it as a float still counts as that many.
    """
    balance_cents = math.floor(balance * 100 + 1e-9)
    if balance_cents <= 0:
        return 0
    basis_points = 10_000 if percent >= 100 else round(percent * 100)
    count = balance_cents * 10_000 // (DRAGONCOIN_RATE * (10_000 + basis_points))
    while quote(count + 1, percent).cost_cents <= balance_cents:
        count += 1
    while count > 0 and quote(count, percent).cost_cents > balance_cents:
        count -= 1
    return count


async def exchange_tax_rate(db: _Executor, guild_id: int) -> float:
    """The Exchange Tax in force. Read inside the exchange's own transaction,
    so a Treasurer's change lands either wholly before it or wholly after."""
    row = await db.fetchone(
        "SELECT exchange_tax_percent FROM server_config WHERE guild_id = ?", (guild_id,)
    )
    return row["exchange_tax_percent"] if row else 0.0


async def get_dragoncoin_cents(db: _Executor, user_id: int) -> int:
    row = await db.fetchone("SELECT dragoncoin_cents FROM users WHERE user_id = ?", (user_id,))
    return row["dragoncoin_cents"] if row else 0


class ExchangeResult(NamedTuple):
    """One exchange, and where it left the player - read in the same
    transaction, for the receipt."""

    quote: ExchangeQuote
    percent: float
    balance_after: float     # the server's currency
    dragoncoin_after: int    # hundredths


async def buy_dragoncoin(
    tx: _Executor, guild_id: int, user_id: int, dragoncoin_cents: int,
    now: datetime | None = None,
) -> ExchangeResult:
    """Sells a player `dragoncoin_cents` hundredths of a DragonCoin for this
    server's currency: the price is burned and the Exchange Tax held (see the
    module docstring). Raises InsufficientQuantity, which aborts the
    surrounding transaction, if they cannot cover the price plus the tax."""
    if dragoncoin_cents <= 0:
        raise ValueError("an exchange needs at least a hundredth of a DragonCoin")
    await ensure_user_row(tx, user_id)
    await ensure_server_row(tx, guild_id)
    percent = await exchange_tax_rate(tx, guild_id)
    priced = quote(dragoncoin_cents, percent)

    await deduct_currency_balance(tx, guild_id, user_id, priced.cost_cents / 100)
    await record_burned(tx, guild_id, priced.base_cents / 100)
    await collect_revenue(tx, guild_id, priced.tax_cents / 100, now)
    await tx.execute(
        "UPDATE users SET dragoncoin_cents = dragoncoin_cents + ? WHERE user_id = ?",
        (dragoncoin_cents, user_id),
    )
    return ExchangeResult(
        priced,
        percent,
        await get_currency_balance(tx, guild_id, user_id),
        await get_dragoncoin_cents(tx, user_id),
    )


async def sell_dragoncoin(
    tx: _Executor, guild_id: int, user_id: int, dragoncoin_cents: int,
    now: datetime | None = None,
) -> ExchangeResult:
    """Buys `dragoncoin_cents` hundredths of a DragonCoin back from a player:
    the price is minted, the player paid it less the Exchange Tax, and the tax
    held (see the module docstring). Raises InsufficientQuantity, which aborts
    the surrounding transaction, if they do not hold that much."""
    if dragoncoin_cents <= 0:
        raise ValueError("an exchange needs at least a hundredth of a DragonCoin")
    await ensure_user_row(tx, user_id)
    await ensure_server_row(tx, guild_id)
    changed = await tx.execute_changes(
        "UPDATE users SET dragoncoin_cents = dragoncoin_cents - ? "
        "WHERE user_id = ? AND dragoncoin_cents >= ?",
        (dragoncoin_cents, user_id, dragoncoin_cents),
    )
    if not changed:
        raise InsufficientQuantity(
            f"user {user_id} does not hold {dragoncoin_cents} hundredths of a DragonCoin"
        )
    percent = await exchange_tax_rate(tx, guild_id)
    priced = quote(dragoncoin_cents, percent)

    await record_minted(tx, guild_id, priced.base_cents / 100)
    await adjust_currency_balance(tx, guild_id, user_id, priced.proceeds_cents / 100)
    await collect_revenue(tx, guild_id, priced.tax_cents / 100, now)
    return ExchangeResult(
        priced,
        percent,
        await get_currency_balance(tx, guild_id, user_id),
        await get_dragoncoin_cents(tx, user_id),
    )
