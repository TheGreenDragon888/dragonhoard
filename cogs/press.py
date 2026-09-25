"""
cogs/press.py

Implements the hydraulic press: /press craft <product> <quantity>, plus
/press status and /press queue.

The press turns bulk smelted material into gemstones. Its recipes cost a little
under what a player would have mined alongside that gem on average before
finding one (see PRESS_RECIPES), so the press doesn't create gems out of
nothing - it trades a very large pile of ore for the certainty of a gem
instead of the one-in-a-million chance of digging one up, with a small fixed
discount as the reward for having built one.

Structurally it follows cogs/factory.py - same shared production_jobs queue,
same up-front fee, same FIFO drain - with three differences worth knowing:

  * Its budget is press-days, not items. The press produces
    press_rate_per_day(run_level) ruby-equivalents each day and every
    recipe costs a whole number of those, so one machine speed drives four
    very different recipe durations.
  * Its progress accumulator is persisted in server_config.press_progress
    rather than held in memory. Jobs here run for days, so an in-memory total
    reset by every restart would mean a diamond never finishes on a bot that
    gets restarted weekly.
  * It banks no progress while its queue is empty. Otherwise an idle press
    would store up weeks of press-days and finish a newly queued diamond the
    instant it was submitted. Nor does a new run inherit anything: a job
    queued onto an empty press clears whatever fraction the last run left
    behind, and its first tick credits only the time since it was queued
    (utils/db_helpers.py: elapsed_work_hours) - the same two rules the other
    machines' ProductionClock applies, kept here in the persisted column
    instead of in memory.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import (
    add_multi_field,
    job_owner_label,
    make_infrastructure_embed,
    queue_field_name,
    queue_limit_field_value,
    PRESS_COLOR,
)
from utils.responses import respond
from utils.formatting import format_currency, format_exact_currency, format_rate
from utils.receipts import build_receipt_embed
from utils.production_ledger import record_output
from database.db import InsufficientQuantity
from utils.government import charge_machine_fee
from utils.db_helpers import (
    advance_job,
    complete_job,
    ensure_server_row,
    clock_now,
    elapsed_work_hours,
    guilds_with_queued_work,
    get_user_quantity,
    adjust_user_quantity,
    deduct_user_quantity,
    get_currency_balance,
    machine_fee,
    run_level,
    machine_speed_level,
    queue_room,
    queue_full_message,
)

from data.materials import (
    PRESS_RECIPES,
    get_material_info,
    press_rate_per_day,
    upgrade_threshold,
)

# Half-hourly. Fine enough that a level 1 press visibly inches through a ruby
# over its 24 hours, coarse enough that persisting progress every tick stays
# cheap.
PRESS_TICK_MINUTES = 30

# How many queued jobs a status embed names individually before collapsing the
# rest into an "and N more" line.
JOB_DISPLAY_LIMIT = 10

# Progress is accumulated a tick at a time, and press_rate_per_day(level)
# divided by the tick count is not generally representable in binary - so at
# many levels a day's worth of those fractions sums to a hair under a whole
# press-day rather than to exactly one. Without this tolerance those levels
# would finish every job a tick late, indefinitely. Same reasoning as the nudge
# in data.materials.advance_harvest.
PROGRESS_EPSILON = 1e-9


class PressCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._now = clock_now
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    press_group = app_commands.Group(name="press", description="Press bulk materials into gemstones")

    @press_group.command(name="craft", description="Queue a gemstone to be pressed")
    @app_commands.describe(product="What to press", quantity="How many to produce — leave blank for 1")
    @app_commands.choices(product=[
        app_commands.Choice(name=get_material_info(key)["name"], value=key)
        for key in PRESS_RECIPES
    ])
    async def press_craft(
        self,
        interaction: discord.Interaction,
        product: app_commands.Choice[str],
        quantity: app_commands.Range[int, 1, 50] | None = None,
    ):
        quantity = quantity or 1
        recipe = PRESS_RECIPES[product.value]
        press_days = recipe["press_days"]
        needs = {input_id: per_unit * quantity for input_id, per_unit in recipe["inputs"].items()}

        have: dict[str, int] = {}
        try:
            async with self.db.transaction() as tx:
                # Kept for the receipt: subtracting what's deducted from these
                # pre-deduction reads gives the remaining amounts for free.
                for input_id, needed in needs.items():
                    have[input_id] = await get_user_quantity(tx, interaction.user.id, input_id)
                    if have[input_id] < needed:
                        info = get_material_info(input_id)
                        label = f"{info['emoji']} {info['name']}" if info else f"`{input_id}`"
                        await interaction.response.send_message(
                            f"Pressing {quantity}x {product.name} needs {needed:,}x {label}, "
                            f"but you only have {have[input_id]:,}.",
                            ephemeral=True,
                        )
                        return

                await ensure_server_row(tx, interaction.guild_id)
                cfg = await tx.fetchone(
                    "SELECT press_fee_multiplier, press_level, press_progress, currency_emoji "
                    "FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_rate = machine_fee("press", cfg["press_fee_multiplier"])
                currency_emoji = cfg["currency_emoji"]

                room = await queue_room(tx, interaction.guild_id, interaction.user.id, "press", quantity)
                if not room.fits:
                    await interaction.response.send_message(
                        queue_full_message("press", room), ephemeral=True
                    )
                    return

                # The fee scales with press time, so a diamond costs nine times
                # a ruby - the machine is tied up nine times as long for it.
                fee_total = fee_rate * press_days * quantity
                balance_after = 0.0
                if fee_total > 0:
                    balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
                    if balance < fee_total:
                        await interaction.response.send_message(
                            f"This would cost {format_currency(fee_total, currency_emoji, round_up=True)} up front, "
                            f"but you only have {format_currency(balance, currency_emoji)}.",
                            ephemeral=True,
                        )
                        return
                    balance_after = balance - fee_total

                for input_id, needed in needs.items():
                    await deduct_user_quantity(tx, interaction.user.id, input_id, needed)

                if fee_total > 0:
                    await charge_machine_fee(tx, interaction.guild_id, interaction.user.id, "press", fee_total)

                # Press-days already spoken for by the jobs in front of this
                # one. Recipes cost wildly different amounts of press time, so
                # unlike the furnace and factory this can't be counted in items.
                queued = await tx.fetchall(
                    "SELECT target_id, quantity FROM production_jobs "
                    "WHERE guild_id = ? AND job_type = 'press' AND status != 'complete'",
                    (interaction.guild_id,),
                )
                press_days_ahead = sum(
                    PRESS_RECIPES[job["target_id"]]["press_days"] * job["quantity"]
                    for job in queued
                )
                # An empty press starts this job from nothing. What's left in
                # press_progress is the fraction of a tick the last run ended on,
                # earned by work that is finished, and spending it here would
                # hand this job time it never waited.
                if not queued:
                    await tx.execute(
                        "UPDATE server_config SET press_progress = 0.0 WHERE guild_id = ?",
                        (interaction.guild_id,),
                    )

                await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                    "VALUES (?, ?, 'press', ?, ?)",
                    (interaction.guild_id, interaction.user.id, product.value, quantity),
                )

                # Re-read rather than reuse cfg's level: this job's own fee has
                # just made the press faster (and may have levelled it), and
                # both the wait quoted below and the press time shown should
                # use the speed it will run at.
                level_row = await tx.fetchone(
                    "SELECT press_level FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                level = level_row["press_level"]
                rate_per_day = press_rate_per_day(
                    await machine_speed_level(tx, interaction.guild_id, "press")
                )
                progress = cfg["press_progress"] if queued else 0.0
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your materials or balance changed while that was going through - "
                "nothing was queued or spent. Try again.",
                ephemeral=True,
            )
            return

        # Whatever press-days the machine has already banked come off the front
        # of the queue, so they shorten this job's wait too.
        days_until_ready = max(
            0.0, press_days_ahead + press_days * quantity - progress
        ) / rate_per_day

        embed = build_receipt_embed(
            title="⚙️ Pressing Receipt",
            color=PRESS_COLOR,
            action="pressing",
            product_id=product.value,
            quantity=quantity,
            consumed=[
                (input_id, needed, have[input_id] - needed)
                for input_id, needed in needs.items()
            ],
            fee_total=fee_total,
            balance_after=balance_after,
            currency_emoji=currency_emoji,
            eta_hours=days_until_ready * 24,
        )
        days = press_days * quantity / rate_per_day
        embed.add_field(
            name="Press Time",
            value=(
                f"This job alone needs **{days:,.1f}** days on the press at level {level} "
                f"({press_days} press-day{'s' if press_days != 1 else ''} each, "
                f"{format_rate(rate_per_day)}/day)"
            ),
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)

    async def _press_status_impl(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        cfg = await self.db.fetchone(
            "SELECT press_level, press_fee_multiplier, press_fees_collected, press_max_queue, "
            "press_progress, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["press_level"]
        fee_rate = machine_fee("press", cfg["press_fee_multiplier"])
        currency_emoji = cfg["currency_emoji"]

        jobs = await self.db.fetchall(
            "SELECT job_id, user_id, target_id, quantity FROM production_jobs "
            "WHERE guild_id = ? AND job_type = 'press' AND status != 'complete' ORDER BY queued_at ASC",
            (interaction.guild_id,),
        )
        pending_items = sum(job["quantity"] for job in jobs)

        # The press is FIFO like the other machines, but its queue is measured
        # in press-days rather than items, and whatever it has already banked
        # (press_progress) comes off the front of it.
        rate_per_day = press_rate_per_day(await machine_speed_level(self.db, interaction.guild_id, "press"))
        press_days_queued = sum(
            PRESS_RECIPES[job["target_id"]]["press_days"] * job["quantity"] for job in jobs
        )
        queue_hours = max(0.0, press_days_queued - cfg["press_progress"]) / rate_per_day * 24

        embed = make_infrastructure_embed(
            emoji="⚙️",
            name="Hydraulic Press",
            color=PRESS_COLOR,
            level=level,
            # The one machine whose speed isn't items per hour: a press-day is
            # what a recipe costs, and one ruby is one of them.
            speed_text=f"{format_rate(rate_per_day, 'press-day')}/day",
            fees_collected=cfg["press_fees_collected"],
            upgrade_cost=upgrade_threshold(level + 1),
            currency_emoji=currency_emoji,
        )
        embed.add_field(
            name="Fee",
            value=f"{format_exact_currency(fee_rate, currency_emoji)} per press-day",
            inline=True,
        )
        embed.add_field(
            name="Queue Limit", value=queue_limit_field_value(cfg["press_max_queue"], level), inline=True
        )

        # Kept where the other two machines have nothing equivalent: a press job
        # runs for days, so "how far into the current one am I" is a genuinely
        # different question from "how long until the queue clears".
        if jobs:
            current = jobs[0]
            cost_days = PRESS_RECIPES[current["target_id"]]["press_days"]
            info = get_material_info(current["target_id"])
            embed.add_field(
                name="Currently Pressing",
                value=(
                    f"{info['emoji']} {info['name']} - "
                    f"{cfg['press_progress']:.2f} / {cost_days} press-days"
                ),
                inline=False,
            )

        lines = []
        for job in jobs[:JOB_DISPLAY_LIMIT]:
            info = get_material_info(job["target_id"])
            emoji = info["emoji"] if info else "❓"
            name = info["name"] if info else job["target_id"]
            lines.append(
                f"{job['quantity']}x {emoji} {name} • {job_owner_label(job['user_id'])}"
            )
        if len(jobs) > JOB_DISPLAY_LIMIT:
            lines.append(f"... and {len(jobs) - JOB_DISPLAY_LIMIT} more")

        add_multi_field(
            embed,
            # An estimate at the current speed: it moves out if anyone queues
            # more behind this, and in if the press levels up on their fees.
            queue_field_name(pending_items, len(jobs), queue_hours),
            lines,
            empty_text="Nothing queued.",
        )

        await respond(interaction, self.db, embed=embed)

    @press_group.command(name="status", description="Show press level, queue, and upgrade progress")
    async def press_status(self, interaction: discord.Interaction):
        await self._press_status_impl(interaction)

    @press_group.command(name="queue", description="Alias for /press status")
    async def press_queue_alias(self, interaction: discord.Interaction):
        await self._press_status_impl(interaction)

    @tasks.loop(minutes=PRESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's press that has work queued earns the
        press-days the time since the tick before pays for - or, if its queue
        was empty then, the time since the work was queued - and spends them on
        the oldest job in its queue."""
        now = self._now()

        # An idle press banks nothing. Without this it would store up weeks of
        # press-days and finish a diamond the moment one was queued, which is
        # the whole thing the timings exist to prevent. The work list already
        # holds only servers with a live press job, so an idle press is never
        # visited - and never has a write transaction opened just to find out
        # it was idle, which every server got every tick until 1.4. The check
        # inside the transaction stays as the race guard: the job could finish
        # or vanish between the list and the lock.
        for cfg in await guilds_with_queued_work(self.db, "press"):
            async with self.db.transaction() as tx:
                job = await self._oldest_job(tx, cfg["guild_id"])
                if job is None:
                    continue

                row = await tx.fetchone(
                    "SELECT press_progress FROM server_config WHERE guild_id = ?", (cfg["guild_id"],)
                )
                speed = press_rate_per_day(run_level(cfg, now))
                # work_started is the oldest live job, so a run that has been
                # going since before the last tick gets a whole tick, and one
                # whose first job arrived since gets only what it waited.
                hours = elapsed_work_hours(cfg["work_started"], now, PRESS_TICK_MINUTES)
                progress = row["press_progress"] + speed * hours / 24

                while job is not None:
                    cost = PRESS_RECIPES[job["target_id"]]["press_days"]
                    if progress + PROGRESS_EPSILON < cost:
                        break
                    # max(0.0, ...) because the tolerance above can let a
                    # fractionally-short total through, which would otherwise
                    # leave a negative carry behind.
                    progress = max(0.0, progress - cost)

                    await adjust_user_quantity(tx, job["user_id"], job["target_id"], 1)
                    # One press job, one gem (or one ultra dense matter), and
                    # one row. Its input side is real - hundreds of Iron or
                    # thousands of Copper leaving circulation - which is what
                    # the import/export line wants; its output side is a
                    # gemstone, which is excluded from GDP wherever it came
                    # from, so 'press' has no place in GDP_SOURCES either.
                    await record_output(
                        tx, job["guild_id"], "press", job["target_id"], 1,
                        PRESS_RECIPES[job["target_id"]]["inputs"],
                    )
                    remaining = job["quantity"] - 1
                    if remaining <= 0:
                        await complete_job(tx, job["job_id"])
                    else:
                        await advance_job(tx, job["job_id"], remaining)
                    job = await self._oldest_job(tx, cfg["guild_id"])

                await tx.execute(
                    "UPDATE server_config SET press_progress = ? WHERE guild_id = ?",
                    (progress, cfg["guild_id"]),
                )

    @staticmethod
    async def _oldest_job(db, guild_id: int):
        return await db.fetchone(
            """
            SELECT * FROM production_jobs
            WHERE guild_id = ? AND job_type = 'press' AND status != 'complete'
            ORDER BY queued_at ASC, job_id ASC LIMIT 1
            """,
            (guild_id,),
        )

    @process_loop.before_loop
    async def before_process_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the press_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(PressCog(bot))
