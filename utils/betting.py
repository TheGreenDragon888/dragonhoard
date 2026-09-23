"""
utils/betting.py

Server prediction bets (1.4): the pot arithmetic, and every database operation
that moves currency into or out of one. See docs/betting.md for the design.

cogs/betting.py owns the slash commands and the embeds; everything that decides
who is owed what lives here, so it can be tested against a temporary SQLite
database without a gateway connection.

THE ONE RULE THIS MODULE EXISTS TO KEEP: no currency is created or destroyed by
a bet. Everything below follows from it.

  A stake is ESCROWED, not burned. It leaves server_currency_balances when the
  wager is placed - a bet cannot promise money the better has since spent at
  /market buy - and sits on the wager row until the bet settles. Nothing calls
  record_minted or record_burned anywhere in this feature, and
  utils/db_helpers.py: circulating_currency adds live escrow back so a server's
  money supply does not appear to shrink while a bet is running. That is the
  same arrangement 1.4's market_orders has, for the same reason.

  The pot is split EXACTLY. Winners share the whole pot - their own stakes back
  plus the losing side's - in proportion to what each staked. "Exactly" is the
  hard part and it is why stakes are integer cents rather than floats: see
  apportion() below, which is the single place the split is computed and the
  single place the invariant is asserted.

  There is no house cut. A rake would make this a currency sink of the kind
  docs/market.md section 1 argues for, and it would also mean the pot paid out
  is smaller than the pot staked. The second consideration won; docs/betting.md
  records the trade.
"""
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from database.db import _Executor
from utils.db_helpers import (
    adjust_currency_balance,
    deduct_currency_balance,
    ensure_user_row,
)

# The two sides of a prediction, and how to get from one to the other. `for`
# means "this will happen" - the side the person who opened the bet is always
# on, because proposing an outcome and staking against it is not a prediction.
FOR = "for"
AGAINST = "against"
SIDES = (FOR, AGAINST)
OPPOSITE = {FOR: AGAINST, AGAINST: FOR}

# Human-facing labels for the two sides. Here rather than in the cog because
# the wording has to match between a button, an embed field and a receipt.
SIDE_LABELS = {FOR: "For", AGAINST: "Against"}

# Every live-bet query spells this predicate literally, because that is the
# only form idx_prediction_bets_live (schema.sql) applies to. Kept as a string
# so the rule is enforced by every caller interpolating the same one rather
# than by everyone remembering to type it the same way.
LIVE_BETS_SQL = "status IN ('open', 'closed')"

# The smallest stake worth taking. format_price shows two decimals, so anything
# below a cent would round to nothing on every embed reporting it - the same
# figure and the same reasoning as cogs/donate.py: MIN_DONATION.
MIN_STAKE_CENTS = 1

# How many bets one server may have running at once. A cap rather than no cap
# because every open bet is currency sitting in escrow and a row in the
# autocomplete a player picks from, and Discord shows at most 25 choices there.
MAX_OPEN_BETS = 10

# The window a bet may take wagers for, in hours. The floor stops a bet that is
# closed before anyone could see the notice announcing it; the ceiling is two
# weeks, past which an unresolved bet is holding somebody's currency hostage
# over a prediction they have forgotten making.
MIN_CLOSES_IN_HOURS = 1
MAX_CLOSES_IN_HOURS = 336

# Discord allows 256 characters in an embed title and 1,024 in a field value.
# The prediction is rendered into a description and quoted in notices and
# receipts, so it is held well inside the smallest of those.
MAX_PREDICTION_LENGTH = 200

# SQLite's own datetime('now') layout, which is what created_at's DEFAULT
# writes and what closes_at is compared against as text. The same constant
# utils/db_helpers.py and utils/production_ledger.py each keep privately, for
# the same reason they do not import it from each other.
_SQLITE_TIMESTAMP = "%Y-%m-%d %H:%M:%S"


class BetUnavailable(Exception):
    """A bet cannot take this action, with a message written for the player.

    Raised from inside the caller's transaction, which aborts it - so a refusal
    discovered halfway through placing a wager cannot leave the stake deducted.
    The cog catches it and sends str(exc) ephemerally.

    Validation deliberately happens INSIDE the transaction rather than before
    it. "Is this bet still open" is a read that authorises a write, and two
    players pressing a button at the same moment as an admin resolves the bet
    is exactly the interleaving Database.transaction's docstring describes.
    """


def to_cents(amount: float) -> int:
    """A currency amount as a whole number of cents.

    The one place a player's float crosses into the integer accounting, so it
    is the one place a rounding decision is made. Everything downstream - the
    pools, the pot, the payouts - is integers from here on.
    """
    return round(amount * 100)


def from_cents(cents: int) -> float:
    """Cents back to a currency amount, for display and for crediting a
    balance (server_currency_balances.balance is a REAL)."""
    return cents / 100


def now_text(now: datetime | None = None) -> str:
    """The current UTC time in the layout closes_at is compared against."""
    return (now or datetime.now(timezone.utc)).strftime(_SQLITE_TIMESTAMP)


def closes_at_text(hours: float, now: datetime | None = None) -> str:
    """When a bet opened `now` and running for `hours` stops taking wagers.

    Resolved to an absolute instant here, at the moment the bet is opened,
    rather than stored as a duration and added on read - the same choice
    daily_jobs makes about its quantity and reward. A duration would move the
    deadline every time anything about the clock changed underneath it.
    """
    return ((now or datetime.now(timezone.utc)) + timedelta(hours=hours)).strftime(
        _SQLITE_TIMESTAMP
    )


def hours_until(closes_at: str, now: datetime | None = None) -> float:
    """Hours from now until `closes_at`, clamped at 0 once it has passed.

    Feeds utils/formatting.py: format_relative_timestamp, which renders a point
    in time the reader's own client counts down - so an embed that sits in a
    channel keeps telling the truth about a deadline it was written before.
    """
    deadline = datetime.strptime(closes_at, _SQLITE_TIMESTAMP).replace(tzinfo=timezone.utc)
    return max(0.0, (deadline - (now or datetime.now(timezone.utc))).total_seconds() / 3600)


class Pools(NamedTuple):
    """What is staked on each side of one bet, in cents, and by how many
    players."""
    for_cents: int
    against_cents: int
    for_backers: int
    against_backers: int

    @property
    def pot_cents(self) -> int:
        """Everything staked on the bet. What the winning side splits, in
        full - see apportion()."""
        return self.for_cents + self.against_cents

    def side_cents(self, side: str) -> int:
        return self.for_cents if side == FOR else self.against_cents

    def backers(self, side: str) -> int:
        return self.for_backers if side == FOR else self.against_backers


def return_multiple(pools: Pools, side: str) -> float | None:
    """What one unit staked on `side` comes back as if that side wins: the pot
    divided by that side's pool.

    A TOTAL RETURN, not a profit. Staking 10 into a for-pool of 10 against an
    against-pool of 100 returns 110/10 = 11.00x - the original 10 back plus the
    100 that was bet against it. Quoted this way everywhere, in one number, so
    nothing on screen has to say whether a figure includes the stake.

    None when nothing is staked on that side yet: there is no multiple, because
    there is nobody to multiply. Odds shown before both sides have money on
    them are not odds, and the embeds say so rather than printing a 0.
    """
    staked = pools.side_cents(side)
    if staked <= 0:
        return None
    return pools.pot_cents / staked


def apportion(entries: list[tuple[int, int]], pot_cents: int) -> dict[int, int]:
    """Split `pot_cents` between the winning wagers in proportion to their
    stakes, in whole cents, summing to exactly `pot_cents`.

    `entries` is (wager_id, stake_cents) for the winning side only. Returns
    wager_id -> payout_cents.

    THIS IS WHERE "no currency is created or destroyed" IS EITHER TRUE OR NOT.
    A proportional split does not generally land on whole cents, so something
    has to be done with the remainder, and the two obvious options are both
    wrong: rounding each share independently can pay out more or less than the
    pot, and truncating every share destroys the leftover.

    So: each wager takes the floor of its exact share, and the cents left over
    are handed out one each, to the wagers with the largest fractional part
    first. That is the largest-remainder method, and it is exact by
    construction - the leftover is the sum of the discarded fractions, each
    below one cent, so there are strictly fewer leftover cents than there are
    winners and no wager is ever owed two of them.

    Ties are broken by larger stake first, then by lower wager_id, so the
    result depends only on the wagers and not on the order they were read out
    of the database. Two players who staked the same amount on the same side
    can still be separated - somebody has to get the odd cent - and the earlier
    wager gets it.

    The assert is not decoration. Every other guarantee in this feature rests
    on this function, and a silent drift of a cent per resolution is exactly
    the kind of thing that is only noticed once a server's books are a long way
    out.
    """
    total_staked = sum(stake for _, stake in entries)
    if total_staked <= 0:
        # No winners to pay. The caller is responsible for not reaching here
        # with a pot to distribute; resolve_bet voids such a bet instead.
        return {}

    payouts: dict[int, int] = {}
    remainders: list[tuple[int, int, int]] = []
    for wager_id, stake in entries:
        exact = stake * pot_cents
        payouts[wager_id] = exact // total_staked
        remainders.append((exact % total_staked, stake, wager_id))

    leftover = pot_cents - sum(payouts.values())
    # Largest remainder first; then larger stake; then the earlier wager. The
    # negated stake keeps the whole key ascending, so wager_id breaks the tie
    # in the direction it reads.
    remainders.sort(key=lambda r: (-r[0], -r[1], r[2]))
    for _, _, wager_id in remainders[:leftover]:
        payouts[wager_id] += 1

    assert sum(payouts.values()) == pot_cents, (
        f"apportioned {sum(payouts.values())} of a {pot_cents} cent pot"
    )
    return payouts


# ---------------------------------------------------------------------------
# Database operations
#
# Every one of these reads a value and then writes based on it, so every one
# must be called inside `async with db.transaction()`. They take a _Executor so
# the tests can drive them directly, but passing a bare Database would reopen
# each race the transaction exists to close.
# ---------------------------------------------------------------------------


async def fetch_bet(db: _Executor, bet_id: int, guild_id: int | None = None):
    """One bet's row, or None. `guild_id` scopes the lookup to one server -
    a bet id typed into another server's /bet resolve must not resolve it."""
    if guild_id is None:
        return await db.fetchone(
            "SELECT * FROM prediction_bets WHERE bet_id = ?", (bet_id,)
        )
    return await db.fetchone(
        "SELECT * FROM prediction_bets WHERE bet_id = ? AND guild_id = ?",
        (bet_id, guild_id),
    )


async def pools_for(db: _Executor, bet_id: int) -> Pools:
    """What is staked on each side of a bet, in one query."""
    rows = await db.fetchall(
        "SELECT side, COALESCE(SUM(stake_cents), 0) AS staked, COUNT(*) AS backers "
        "FROM prediction_wagers WHERE bet_id = ? GROUP BY side",
        (bet_id,),
    )
    by_side = {row["side"]: row for row in rows}
    return Pools(
        for_cents=by_side[FOR]["staked"] if FOR in by_side else 0,
        against_cents=by_side[AGAINST]["staked"] if AGAINST in by_side else 0,
        for_backers=by_side[FOR]["backers"] if FOR in by_side else 0,
        against_backers=by_side[AGAINST]["backers"] if AGAINST in by_side else 0,
    )


async def live_bets(db: _Executor, guild_id: int):
    """One server's open and closed bets, newest first. What the autocompletes
    and /bet status read."""
    return await db.fetchall(
        f"SELECT * FROM prediction_bets WHERE guild_id = ? AND {LIVE_BETS_SQL} "
        f"ORDER BY bet_id DESC",
        (guild_id,),
    )


async def refresh_status(db: _Executor, bet, now: datetime | None = None) -> str:
    """Moves an open bet whose deadline has passed to 'closed', and returns the
    status it should now be treated as having.

    Lazy rather than swept by a background loop, which is the same choice the
    job board makes about posting the day's task (schema.sql, daily_jobs): a
    bet nobody is looking at has nothing to accrue, so a sixth loop would only
    be one more thing to keep running. Every path that touches a bet calls this
    first, so the transition happens the moment it could possibly matter to
    anybody.
    """
    if bet["status"] != "open" or bet["closes_at"] > now_text(now):
        return bet["status"]
    await db.execute(
        "UPDATE prediction_bets SET status = 'closed' WHERE bet_id = ? AND status = 'open'",
        (bet["bet_id"],),
    )
    return "closed"


async def open_bet(
    db: _Executor,
    guild_id: int,
    creator_id: int,
    prediction: str,
    closes_at: str,
) -> int:
    """Creates a bet and returns its id. The creator's opening wager is placed
    separately, by the cog, in the same transaction."""
    open_count = await db.fetchone(
        f"SELECT COUNT(*) AS live FROM prediction_bets WHERE guild_id = ? AND {LIVE_BETS_SQL}",
        (guild_id,),
    )
    if open_count["live"] >= MAX_OPEN_BETS:
        raise BetUnavailable(
            f"This server already has {MAX_OPEN_BETS} bets running, which is the limit. "
            f"One of them has to be resolved or cancelled before another can open."
        )
    return await db.execute(
        "INSERT INTO prediction_bets (guild_id, creator_id, prediction, closes_at) "
        "VALUES (?, ?, ?, ?)",
        (guild_id, creator_id, prediction, closes_at),
    )


async def place_wager(
    db: _Executor,
    bet,
    user_id: int,
    side: str,
    stake_cents: int,
    now: datetime | None = None,
) -> int:
    """Takes `stake_cents` out of the player's balance and stakes it on `side`.
    Returns their total stake on this bet afterwards.

    Adding to a position the player already holds is the normal case, not a
    special one - the row is upserted. Taking the OTHER side is refused: see
    the UNIQUE (bet_id, user_id) constraint in schema.sql for why a player gets
    one side per bet.
    """
    status = await refresh_status(db, bet, now)
    if status == "cancelled":
        raise BetUnavailable("That bet was cancelled - it is not taking wagers.")
    if status == "resolved":
        raise BetUnavailable("That bet has already been resolved.")
    if status == "closed":
        raise BetUnavailable(
            "That bet has closed and is waiting to be resolved - no more wagers."
        )

    existing = await db.fetchone(
        "SELECT side, stake_cents FROM prediction_wagers WHERE bet_id = ? AND user_id = ?",
        (bet["bet_id"], user_id),
    )
    if existing is not None and existing["side"] != side:
        raise BetUnavailable(
            f"You are already **{SIDE_LABELS[existing['side']].lower()}** this one, and a side "
            f"is locked in until the bet resolves. You can add to that side, but not switch."
        )

    await ensure_user_row(db, user_id)
    # Raises InsufficientQuantity, which aborts the transaction, so a stake
    # that cannot be covered never reaches the wager row.
    await deduct_currency_balance(db, bet["guild_id"], user_id, from_cents(stake_cents))
    await db.execute(
        "INSERT INTO prediction_wagers (bet_id, user_id, side, stake_cents) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (bet_id, user_id) DO UPDATE SET stake_cents = stake_cents + excluded.stake_cents",
        (bet["bet_id"], user_id, side, stake_cents),
    )
    return (existing["stake_cents"] if existing else 0) + stake_cents


class Settlement(NamedTuple):
    """What settling a bet paid out.

    `payouts` is user_id -> cents received, and it is what the cog reports and
    what the tests add up. `voided` says the pot was handed back rather than
    won: either nobody backed the winning side, or the bet was cancelled.
    """
    payouts: dict[int, int]
    pot_cents: int
    voided: bool


async def _refund(db: _Executor, bet, wagers) -> dict[int, int]:
    """Hands every stake back to whoever put it up, and records it as the
    payout. Conserving by construction - each player gets exactly their own
    money - which is why a void needs no apportionment."""
    payouts: dict[int, int] = {}
    for wager in wagers:
        await adjust_currency_balance(
            db, bet["guild_id"], wager["user_id"], from_cents(wager["stake_cents"])
        )
        await db.execute(
            "UPDATE prediction_wagers SET payout_cents = ? WHERE wager_id = ?",
            (wager["stake_cents"], wager["wager_id"]),
        )
        payouts[wager["user_id"]] = wager["stake_cents"]
    return payouts


async def resolve_bet(
    db: _Executor, bet, outcome: str, resolved_by: int, now: datetime | None = None
) -> Settlement:
    """Pays the pot out to `outcome`'s backers and closes the bet.

    A bet the winning side never backed is VOIDED and refunded in full rather
    than resolved. There is no honest alternative: the losing stakes cannot be
    paid to a side nobody took, and keeping them would destroy currency this
    feature promises not to destroy. It is rare and it reads as a let-down, so
    the cog says plainly that it happened and why.
    """
    status = await refresh_status(db, bet, now)
    if status == "resolved":
        raise BetUnavailable("That bet has already been resolved.")
    if status == "cancelled":
        raise BetUnavailable("That bet was cancelled - there is nothing to resolve.")

    wagers = await db.fetchall(
        "SELECT wager_id, user_id, side, stake_cents FROM prediction_wagers WHERE bet_id = ?",
        (bet["bet_id"],),
    )
    pot_cents = sum(wager["stake_cents"] for wager in wagers)
    winners = [w for w in wagers if w["side"] == outcome]

    if not winners:
        payouts = await _refund(db, bet, wagers)
        voided = True
    else:
        by_wager = apportion([(w["wager_id"], w["stake_cents"]) for w in winners], pot_cents)
        payouts = {}
        for wager in winners:
            paid = by_wager[wager["wager_id"]]
            await adjust_currency_balance(
                db, bet["guild_id"], wager["user_id"], from_cents(paid)
            )
            await db.execute(
                "UPDATE prediction_wagers SET payout_cents = ? WHERE wager_id = ?",
                (paid, wager["wager_id"]),
            )
            payouts[wager["user_id"]] = paid
        # A losing wager is paid 0 rather than left NULL: NULL means "this bet
        # has not settled yet", and this one has.
        await db.execute(
            "UPDATE prediction_wagers SET payout_cents = 0 WHERE bet_id = ? AND side != ?",
            (bet["bet_id"], outcome),
        )
        voided = False

    await db.execute(
        "UPDATE prediction_bets SET status = 'resolved', outcome = ?, resolved_by = ?, "
        "resolved_at = datetime('now') WHERE bet_id = ?",
        (outcome, resolved_by, bet["bet_id"]),
    )
    return Settlement(payouts=payouts, pot_cents=pot_cents, voided=voided)


async def cancel_bet(
    db: _Executor, bet, now: datetime | None = None
) -> Settlement:
    """Voids a bet and hands every stake back untouched."""
    status = await refresh_status(db, bet, now)
    if status == "resolved":
        raise BetUnavailable(
            "That bet has already been resolved - the pot has been paid out and cannot be "
            "unwound."
        )
    if status == "cancelled":
        raise BetUnavailable("That bet has already been cancelled.")

    wagers = await db.fetchall(
        "SELECT wager_id, user_id, stake_cents FROM prediction_wagers WHERE bet_id = ?",
        (bet["bet_id"],),
    )
    payouts = await _refund(db, bet, wagers)
    await db.execute(
        "UPDATE prediction_bets SET status = 'cancelled', resolved_at = datetime('now') "
        "WHERE bet_id = ?",
        (bet["bet_id"],),
    )
    return Settlement(
        payouts=payouts,
        pot_cents=sum(w["stake_cents"] for w in wagers),
        voided=True,
    )


__all__ = [
    "AGAINST",
    "FOR",
    "LIVE_BETS_SQL",
    "MAX_CLOSES_IN_HOURS",
    "MAX_OPEN_BETS",
    "MAX_PREDICTION_LENGTH",
    "MIN_CLOSES_IN_HOURS",
    "MIN_STAKE_CENTS",
    "OPPOSITE",
    "SIDES",
    "SIDE_LABELS",
    "BetUnavailable",
    "Pools",
    "Settlement",
    "apportion",
    "cancel_bet",
    "closes_at_text",
    "fetch_bet",
    "from_cents",
    "hours_until",
    "live_bets",
    "now_text",
    "open_bet",
    "place_wager",
    "pools_for",
    "refresh_status",
    "resolve_bet",
    "return_multiple",
    "to_cents",
]
