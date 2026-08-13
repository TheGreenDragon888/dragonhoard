"""
utils/job_board.py

The daily job board: once per day, a server posts one task asking players to
sell it a material it's short of, and pays a bonus to everyone who does. The
day rolls over at midnight Arizona time (see JOB_BOARD_TIMEZONE).

The task is sized to pay just over one unit of the server's currency, whatever
it picks and whatever size the server is (data/materials.py:
JOB_BOARD_TARGET_PAYOUT). The bonus is what the server itself would pay for
those goods at the moment the job was posted, and it stacks on top of what
selling them earned in the first place - so completing the job is worth about
twice a plain sale of the same materials.

Pricing the bonus off the server's own rate rather than the flat ceiling price
is what stops the board being printable: once a server holds a real amount of
the material, buying the goods back afterwards costs more than the sale and the
bonus together paid out. On a thinly stocked server a few tenths still leak,
worst on the smallest ones. See data/materials.py: job_reward for the shape of
it and what closing it outright would cost.

Two design decisions worth knowing before changing anything here:

  * The job is posted LAZILY - the first time anyone looks at the board or
    sells into it - rather than by a background loop. The mining pool needs a
    loop because it accrues whether or not anyone is playing; a task nobody has
    looked at has nothing to accrue, so a loop would only be a fourth thing to
    keep running.

  * quantity and reward are frozen into the daily_jobs row at posting time
    rather than recomputed on read. Both derive from how well stocked the
    server is, and the day's own selling moves that constantly - recomputing
    would grow the task under someone already partway through it, every time
    anybody sold anything.

Everything that decides WHAT the task is lives in data/materials.py
(JOB_BOARD_MATERIALS, pick_job_material, job_quantity, job_reward) so it can be
tested without a database.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from utils.db_helpers import adjust_currency_balance, record_minted
from data.materials import (
    JOB_BOARD_MATERIALS,
    job_quantity,
    job_reward,
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
# Deliberately NOT shared with the mining pool top-up, which still rolls over
# at UTC midnight (utils/formatting.py: utc_today). The two were one function
# until the board moved; they are separate schedules now.
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

    # How far below target stock the server is on each eligible material, as a
    # fraction of that target - so the shortfalls of a hundred-member server
    # and a five-member one are on the same scale.
    #
    # The stock and target themselves are kept, not just the deficit they
    # produce: the task's size and its bonus are both priced off the chosen
    # material's stock level, and re-reading it after the pick would be a
    # second look at a number that can have moved in between.
    deficits: dict[str, float] = {}
    stocks: dict[str, int] = {}
    targets: dict[str, int] = {}
    for material_id in JOB_BOARD_MATERIALS:
        target = target_stock(member_count, material_id)
        row = await tx.fetchone(
            "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
            (guild_id, material_id),
        )
        stock = row["quantity"] if row else 0
        deficits[material_id] = max(0.0, target - stock) / target
        stocks[material_id] = stock
        targets[material_id] = target

    material_id = pick_job_material(deficits)
    stock, target = stocks[material_id], targets[material_id]
    quantity = job_quantity(material_id, stock, target)
    await tx.execute(
        "INSERT OR IGNORE INTO daily_jobs (guild_id, job_date, material_id, quantity, reward) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, today, material_id, quantity, job_reward(material_id, quantity, stock, target)),
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
        "SELECT sold, claimed_at FROM daily_job_progress "
        "WHERE guild_id = ? AND job_date = ? AND user_id = ?",
        (guild_id, job_date, user_id),
    )


async def credit_job_progress(tx, guild_id: int, user_id: int, material_id: str, quantity: int, member_count: int) -> float:
    """Records a sale against today's job and pays the reward if it completes
    it. Returns the reward paid, or 0.0 - so a caller only has to mention the
    board when there is something to mention.

    Called from inside /market sell's own transaction, so the sale and the
    bonus commit together.

    Progress accumulates rather than needing one big sale: selling ten, then
    ten, then thirty against a fifty-unit task completes it just as a single
    fifty does.

    The claim is one guarded UPDATE, which is both the once-per-user-per-day
    rule and the concurrency guard. claimed_at IS NULL in the WHERE clause
    means two sells racing to finish the task can't both pay it out - exactly
    one of them changes a row, and only that one credits anything.
    """
    job = await ensure_todays_job(tx, guild_id, member_count)
    if job is None or job["material_id"] != material_id:
        return 0.0

    await tx.execute(
        "INSERT INTO daily_job_progress (guild_id, job_date, user_id, sold) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (guild_id, job_date, user_id) DO UPDATE SET sold = sold + excluded.sold",
        (guild_id, job["job_date"], user_id, quantity),
    )

    claimed = await tx.execute_changes(
        "UPDATE daily_job_progress SET claimed_at = datetime('now') "
        "WHERE guild_id = ? AND job_date = ? AND user_id = ? "
        "AND claimed_at IS NULL AND sold >= ?",
        (guild_id, job["job_date"], user_id, job["quantity"]),
    )
    if not claimed:
        return 0.0

    reward = job["reward"]
    await adjust_currency_balance(tx, guild_id, user_id, reward)
    # The board is a currency faucet, and the second one the bot has ever had -
    # docs/market.md section 4's accounting has to see it or the server's
    # minted total quietly stops matching the currency in circulation.
    await record_minted(tx, guild_id, reward)
    return reward
