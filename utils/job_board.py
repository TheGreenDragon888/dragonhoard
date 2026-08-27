"""
utils/job_board.py

The daily job board: once per day, a server posts one task asking players to
sell it a material it's short of, and pays a bonus to everyone who does. The
day rolls over at midnight Arizona time (see JOB_BOARD_TIMEZONE).

The task is sized to pay just over one unit of the server's currency, whatever
it picks and whatever size the server is (data/materials.py:
JOB_BOARD_TARGET_PAYOUT). The bonus stacks on top of what selling the goods
earned in the first place, so completing the task is worth about twice a plain
sale of the same materials.

THE BONUS IS PAID PER COMPLETION as of 1.3, not once per player per day. Sell
three times the task quantity and it pays three times - in one command, if that
is how it was sold. What stops that printing currency is the market's buy
markup rather than a daily cap: a completion pays at most what the goods sold
for (the quantity is the fewest units clearing the target payout), while buying
those same goods back costs exactly twice the sale price, so the round trip
breaks even at its very best. See data/materials.py: JOB_BOARD_TARGET_PAYOUT
and MARKET_BUY_MARKUP - the two numbers only work as a pair.

Two design decisions worth knowing before changing anything here:

  * The job is posted LAZILY - the first time anyone looks at the board or
    sells into it - rather than by a background loop. The mining pool needs a
    loop because it accrues whether or not anyone is playing; a task nobody has
    looked at has nothing to accrue, so a loop would only be a fourth thing to
    keep running.

  * quantity and reward are frozen into the daily_jobs row at posting time
    rather than recomputed on read. Neither derives from the server's stock any
    more (1.3), so the day's own selling can no longer move them - but a
    balance retune between two of a player's sales still could, and the task
    someone is partway through should be the task they started.

Everything that decides WHAT the task is lives in data/materials.py
(JOB_BOARD_MATERIALS, pick_job_material, job_quantity) so it can be tested
without a database.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from utils.db_helpers import adjust_currency_balance, record_minted
from data.materials import (
    JOB_BOARD_MATERIALS,
    JOB_BOARD_TARGET_PAYOUT,
    job_quantity,
    pick_job_material,
    target_stock,
)

# The clock the job board's day runs on. Arizona rather than UTC, so the board
# turns over at a time that means something to the people playing rather than
# at 5pm local.
#
# America/Phoenix specifically, not a fixed -07:00 offset, even though Arizona
# has not observed daylight saving since 1968 and the two are identical today.
# Naming the place rather than the offset means the answer stays right if that
# ever changes, and it says WHICH -07:00 this is - Phoenix and, say, Denver in
# winter are the same offset and different calendars.
#
# This is now the only date in the game. The mining pool used to keep its own
# on UTC midnight; the bag replaced it and there is nothing left to keep in
# step with (utils/mining_pool.py).
JOB_BOARD_TIMEZONE = ZoneInfo("America/Phoenix")

# Finished job rows are kept for a while so a player can see they completed
# yesterday's, but not forever - nothing reads further back than that.
JOB_HISTORY_DAYS = 30


def job_board_now() -> datetime:
    """The current time on the job board's clock."""
    return datetime.now(JOB_BOARD_TIMEZONE)


def job_board_today() -> str:
    """Today's date on the job board's clock, ISO formatted.

    This is the value stored in daily_jobs.job_date and compared against, so it
    is the single definition of which day a job belongs to. Stored as text and
    compared as text, which is why ISO matters: it sorts chronologically as a
    plain string."""
    return job_board_now().date().isoformat()


def hours_until_reset(now: datetime | None = None) -> float:
    """How long until the next job is posted, in hours - what the countdown on
    the /jobboard embed is built from.

    Takes `now` so it can be tested at a chosen instant rather than only at
    whatever time the suite happens to run."""
    now = now or job_board_now()
    now = now.astimezone(JOB_BOARD_TIMEZONE)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds() / 3600


async def ensure_todays_job(tx, guild_id: int, member_count: int):
    """Posts today's job if it hasn't been posted yet, and returns the row.

    Safe to call from anywhere and as often as you like: the (guild_id,
    job_date) primary key plus INSERT OR IGNORE make it idempotent, and
    Database.transaction's write lock serialises callers, so two /market sell
    commands racing across midnight UTC still post exactly one job.

    Must be handed a Transaction. `member_count` has to be computed by the
    caller BEFORE opening it - human_member_count can chunk a guild over the
    gateway, and awaiting Discord while holding the write lock stalls every
    other command in the bot.
    """
    today = job_board_today()
    existing = await tx.fetchone(
        "SELECT * FROM daily_jobs WHERE guild_id = ? AND job_date = ?", (guild_id, today)
    )
    if existing is not None:
        return existing

    # Each eligible material's selection weight: target / (stock + target).
    # Emptied out (stock=0) gives the maximum weight of 1.0 - the same
    # maximum the old formula's clamped deficit gave a fully-drained
    # material. Sitting exactly at target stock gives 0.5, not the 0.0 a
    # hard floor at target used to; the weight keeps falling continuously
    # toward (but never reaching) 0 as stock climbs past target, so nothing
    # is ever fully out of the running. Dividing by target is what puts a
    # hundred-member server's numbers on the same scale as a five-member
    # one's - the role target played in the formula this replaced.
    #
    # Stock is now read for this weighting and nothing else. It used to also
    # size the task and price its bonus, which is why the stock and target were
    # kept per material rather than just the weight they produce; as of 1.3
    # both of those are constants of the material, so only the weight survives
    # the loop.
    deficits: dict[str, float] = {}
    for material_id in JOB_BOARD_MATERIALS:
        target = target_stock(member_count, material_id)
        row = await tx.fetchone(
            "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
            (guild_id, material_id),
        )
        stock = row["quantity"] if row else 0
        deficits[material_id] = target / (stock + target)

    material_id = pick_job_material(deficits)
    # Both frozen into the row rather than recomputed on read - see the module
    # docstring. The reward is a flat JOB_BOARD_TARGET_PAYOUT per completion,
    # stored here so a retune can't change what a task in progress pays.
    await tx.execute(
        "INSERT OR IGNORE INTO daily_jobs (guild_id, job_date, material_id, quantity, reward) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, today, material_id, job_quantity(material_id), JOB_BOARD_TARGET_PAYOUT),
    )

    # Once per guild per day, on the one branch that isn't a plain read. The
    # cutoff is computed on the board's own clock rather than with SQLite's
    # date('now'), which is UTC - job_date is no longer a UTC date, so letting
    # the two drift apart would mean the window quietly wasn't the number of
    # days it says it is.
    cutoff = (job_board_now() - timedelta(days=JOB_HISTORY_DAYS)).date().isoformat()
    await tx.execute("DELETE FROM daily_jobs WHERE job_date < ?", (cutoff,))
    await tx.execute("DELETE FROM daily_job_progress WHERE job_date < ?", (cutoff,))

    # Re-read rather than returning what was just built: INSERT OR IGNORE may
    # have lost to a concurrent insert, and the row that won is the real job.
    return await tx.fetchone(
        "SELECT * FROM daily_jobs WHERE guild_id = ? AND job_date = ?", (guild_id, today)
    )


async def get_progress(db, guild_id: int, user_id: int, job_date: str):
    return await db.fetchone(
        "SELECT sold, claims_paid, claimed_at FROM daily_job_progress "
        "WHERE guild_id = ? AND job_date = ? AND user_id = ?",
        (guild_id, job_date, user_id),
    )


async def credit_job_progress(
    tx, guild_id: int, user_id: int, material_id: str, quantity: int, member_count: int
) -> tuple[float, int]:
    """Records a sale against today's job and pays for every completion it
    finished. Returns (total reward paid, completions paid for) - (0.0, 0) if
    this sale didn't finish one, so a caller only has to mention the board when
    there is something to mention.

    Called from inside /market sell's own transaction, so the sale and the
    bonus commit together.

    Progress accumulates rather than needing one big sale: selling ten, then
    ten, then thirty against a fifty-unit task completes it just as a single
    fifty does. Since 1.3 it also keeps going past the first completion -
    a hundred and twenty against that fifty-unit task is two completions and
    forty units of progress toward a third, whether that arrived as one sale
    or as twelve.

    claims_paid is how many completions this player has already been paid for
    today, and every completion is paid exactly once because the payout is the
    difference between that and sold/quantity. The read and the UPDATE that
    banks it are safe as a pair because callers are inside a transaction, which
    takes SQLite's write lock up front (Database.transaction) - the guard in the
    UPDATE's WHERE clause is what keeps that true rather than assumed, and would
    decline to pay twice rather than double-pay if this were ever called outside
    one.
    """
    job = await ensure_todays_job(tx, guild_id, member_count)
    if job is None or job["material_id"] != material_id:
        return 0.0, 0

    await tx.execute(
        "INSERT INTO daily_job_progress (guild_id, job_date, user_id, sold) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (guild_id, job_date, user_id) DO UPDATE SET sold = sold + excluded.sold",
        (guild_id, job["job_date"], user_id, quantity),
    )

    # sold / quantity is integer division - both operands are INTEGER columns
    # or integer parameters, and SQLite's / follows its operands' types.
    before = await tx.fetchone(
        "SELECT sold / ? - claims_paid AS owed FROM daily_job_progress "
        "WHERE guild_id = ? AND job_date = ? AND user_id = ?",
        (job["quantity"], guild_id, job["job_date"], user_id),
    )
    completions = max(0, before["owed"]) if before else 0
    if not completions:
        return 0.0, 0

    claimed = await tx.execute_changes(
        "UPDATE daily_job_progress SET claims_paid = claims_paid + ?, claimed_at = datetime('now') "
        "WHERE guild_id = ? AND job_date = ? AND user_id = ? "
        "AND sold / ? - claims_paid = ?",
        (completions, guild_id, job["job_date"], user_id, job["quantity"], completions),
    )
    if not claimed:
        return 0.0, 0

    reward = job["reward"] * completions
    await adjust_currency_balance(tx, guild_id, user_id, reward)
    # The board is a currency faucet, and the second one the bot has ever had -
    # docs/market.md section 4's accounting has to see it or the server's
    # minted total quietly stops matching the currency in circulation.
    await record_minted(tx, guild_id, reward)
    return reward, completions
