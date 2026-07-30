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
    press_rate_per_day(level) ruby-equivalents each day and every recipe costs
    a whole number of those, so one machine speed drives four very different
    recipe durations.
  * Its progress accumulator is persisted in server_config.press_progress
    rather than held in memory. Jobs here run for days, so an in-memory total
    reset by every restart would mean a diamond never finishes on a bot that
    gets restarted weekly.
  * It banks no progress while its queue is empty. Otherwise an idle press
    would store up weeks of press-days and finish a newly queued diamond the
    instant it was submitted.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import make_embed, add_multi_field, PRESS_COLOR
from utils.responses import respond
from utils.formatting import format_currency
from utils.receipts import build_receipt_embed
from database.db import InsufficientQuantity
from utils.db_helpers import (
    ensure_server_row,
    get_user_quantity,
    adjust_user_quantity,
    deduct_user_quantity,
    get_currency_balance,
    charge_user_fee,
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

# Progress is accumulated a tick at a time, and a day's worth of those
# fractions sums to a hair under 1.0 rather than exactly 1.0 (48 additions of
# 1/48 lands on 0.99999999999999989). Without this tolerance every job would
# finish one tick late, and a level 27 press would never finish anything in a
# day at all. Same reasoning as the nudge in data.materials.advance_harvest.
PROGRESS_EPSILON = 1e-9


class PressCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    press_group = app_commands.Group(name="press", description="Press bulk materials into gemstones")

    @press_group.command(name="craft", description="Queue a gemstone to be pressed")
    @app_commands.describe(product="What to press", quantity="How many to produce")
    @app_commands.choices(product=[
        app_commands.Choice(name=get_material_info(key)["name"], value=key)
        for key in PRESS_RECIPES
    ])
    async def press_craft(
        self,
        interaction: discord.Interaction,
        product: app_commands.Choice[str],
        quantity: app_commands.Range[int, 1, 50],
    ):
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
                    "SELECT press_fee, press_max_queue, press_level, currency_emoji "
                    "FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_rate = cfg["press_fee"]
                currency_emoji = cfg["currency_emoji"]
                max_queue = cfg["press_max_queue"]

                user_queue_row = await tx.fetchone(
                    "SELECT COALESCE(SUM(quantity), 0) as queued_items FROM production_jobs "
                    "WHERE guild_id = ? AND user_id = ? AND job_type = 'press' AND status != 'complete'",
                    (interaction.guild_id, interaction.user.id),
                )
                queued_items = user_queue_row["queued_items"] if user_queue_row else 0
                if queued_items + quantity > max_queue:
                    await interaction.response.send_message(
                        f"You can only have {max_queue} item(s) queued at the press at once. "
                        f"Wait for your current job to finish.",
                        ephemeral=True,
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
                            f"This would cost {format_currency(fee_total, currency_emoji)} up front, "
                            f"but you only have {format_currency(balance, currency_emoji)}.",
                            ephemeral=True,
                        )
                        return
                    balance_after = balance - fee_total

                for input_id, needed in needs.items():
                    await deduct_user_quantity(tx, interaction.user.id, input_id, needed)

                if fee_total > 0:
                    await charge_user_fee(tx, interaction.guild_id, interaction.user.id, fee_total)
                    await tx.execute(
                        "UPDATE server_config SET press_fees_collected = press_fees_collected + ? WHERE guild_id = ?",
                        (fee_total, interaction.guild_id),
                    )
                    await self._maybe_upgrade_press(tx, interaction.guild_id)

                await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                    "VALUES (?, ?, 'press', ?, ?)",
                    (interaction.guild_id, interaction.user.id, product.value, quantity),
                )
                level = cfg["press_level"]
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your materials or balance changed while that was going through - "
                "nothing was queued or spent. Try again.",
                ephemeral=True,
            )
            return

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
        )
        days = press_days * quantity / press_rate_per_day(level)
        embed.add_field(
            name="Press Time",
            value=(
                f"**{days:,.1f}** days at level {level} "
                f"({press_days} press-day{'s' if press_days != 1 else ''} each, "
                f"{press_rate_per_day(level)}/day)"
            ),
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)

    async def _press_status_impl(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        cfg = await self.db.fetchone(
            "SELECT press_level, press_fee, press_fees_collected, press_max_queue, "
            "press_progress, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["press_level"]
        fee_rate = cfg["press_fee"]
        currency_emoji = cfg["currency_emoji"]

        jobs = await self.db.fetchall(
            "SELECT job_id, user_id, target_id, quantity, status FROM production_jobs "
            "WHERE guild_id = ? AND job_type = 'press' AND status != 'complete' ORDER BY queued_at ASC",
            (interaction.guild_id,),
        )
        pending_items = sum(job["quantity"] for job in jobs)

        embed = make_embed("⚙️ Hydraulic Press Status", PRESS_COLOR)
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Speed", value=f"**{press_rate_per_day(level)}** press-days/day", inline=True)
        embed.add_field(name="Queue Limit", value=f"**{cfg['press_max_queue']}** item(s) per user", inline=True)
        embed.add_field(
            name="Fee",
            value=f"{format_currency(fee_rate, currency_emoji)} per press-day",
            inline=True,
        )
        embed.add_field(
            name="Pending", value=f"**{pending_items}** item(s) across **{len(jobs)}** job(s)", inline=True
        )

        # Levels are unbounded, so there is always a next one to show.
        next_level = level + 1
        cost = upgrade_threshold(next_level)
        progress = min(cfg["press_fees_collected"], cost)
        embed.add_field(
            name=f"Progress to Level {next_level}",
            value=f"{format_currency(progress, currency_emoji)} / {format_currency(cost, currency_emoji)} collected",
            inline=False,
        )

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
            for job in jobs[:10]:
                info = get_material_info(job["target_id"])
                emoji = info["emoji"] if info else "❓"
                name = info["name"] if info else job["target_id"]
                status_str = "In Progress" if job["status"] == "in_progress" else "Queued"
                lines.append(f"{emoji} {job['quantity']}x {name} - {status_str}")
            if len(jobs) > 10:
                lines.append(f"... and {len(jobs) - 10} more")
            add_multi_field(embed, "Pending Jobs", lines)

        await respond(interaction, self.db, embed=embed)

    @press_group.command(name="status", description="Show press level, queue, and upgrade progress")
    async def press_status(self, interaction: discord.Interaction):
        await self._press_status_impl(interaction)

    @press_group.command(name="queue", description="Alias for /press status")
    async def press_queue_alias(self, interaction: discord.Interaction):
        await self._press_status_impl(interaction)

    @tasks.loop(minutes=PRESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's press earns a slice of a day's press-days
        and spends them on the oldest job in its queue."""
        ticks_per_day = (60 * 24) / PRESS_TICK_MINUTES
        configs = await self.db.fetchall("SELECT guild_id, press_level FROM server_config")

        for cfg in configs:
            async with self.db.transaction() as tx:
                job = await self._oldest_job(tx, cfg["guild_id"])
                # An idle press banks nothing. Without this it would store up
                # weeks of press-days and finish a diamond the moment one was
                # queued, which is the whole thing the timings exist to prevent.
                if job is None:
                    continue

                row = await tx.fetchone(
                    "SELECT press_progress FROM server_config WHERE guild_id = ?", (cfg["guild_id"],)
                )
                progress = row["press_progress"] + press_rate_per_day(cfg["press_level"]) / ticks_per_day

                while job is not None:
                    cost = PRESS_RECIPES[job["target_id"]]["press_days"]
                    if progress + PROGRESS_EPSILON < cost:
                        break
                    # max(0.0, ...) because the tolerance above can let a
                    # fractionally-short total through, which would otherwise
                    # leave a negative carry behind.
                    progress = max(0.0, progress - cost)

                    await adjust_user_quantity(tx, job["user_id"], job["target_id"], 1)
                    remaining = job["quantity"] - 1
                    if remaining <= 0:
                        await tx.execute(
                            "UPDATE production_jobs SET status = 'complete', quantity = 0 WHERE job_id = ?",
                            (job["job_id"],),
                        )
                        job = await self._oldest_job(tx, cfg["guild_id"])
                    else:
                        await tx.execute(
                            "UPDATE production_jobs SET quantity = ?, status = 'in_progress' WHERE job_id = ?",
                            (remaining, job["job_id"]),
                        )
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

    async def _maybe_upgrade_press(self, db, guild_id: int):
        """Takes an executor rather than using self.db, so it reads the fee
        total its caller just wrote rather than the pre-transaction value.
        Loops because a single expensive job can cross more than one threshold."""
        cfg = await db.fetchone(
            "SELECT press_level, press_fees_collected FROM server_config WHERE guild_id = ?",
            (guild_id,),
        )
        level, collected = cfg["press_level"], cfg["press_fees_collected"]
        while collected >= upgrade_threshold(level + 1):
            level += 1
        if level != cfg["press_level"]:
            await db.execute(
                "UPDATE server_config SET press_level = ? WHERE guild_id = ?", (level, guild_id)
            )

    @process_loop.before_loop
    async def before_process_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the press_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(PressCog(bot))
