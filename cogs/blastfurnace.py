"""
cogs/blastfurnace.py

Implements /blast smelt <material> <batches>, plus /blast status and
/blast queue.

The blast furnace is an auxiliary furnace for bulk work. It runs the furnace's
own three recipes at exactly BLAST_FURNACE_BATCH_SIZE times the scale
(data/materials.py: BLAST_FURNACE_RECIPES), charges the furnace's fee times
that same batch size, and burns the same fuel per smelted unit - so a bulk
smelter pays exactly what a furnace user pays per Iron. What it buys is
throughput: BLAST_FURNACE_RATE_PER_LEVEL is one batch an hour per level,
twenty times the furnace's units per hour, which is what makes a pressed
diamond's 27,000 Steel a project rather than a season. Keeping that traffic on
its own machine is also what stops one player's ten-thousand-ore job filling
the shared furnace queue for everyone else.

Structurally it follows cogs/furnace.py - same shared production_jobs queue,
same up-front fee, same FIFO drain, same in-memory progress accumulator - with
two differences worth knowing:

  * EVERYTHING HERE IS COUNTED IN BATCHES. production_jobs.quantity, the fee,
    the queue cap and this machine's rate are all denominated in batches of
    BLAST_FURNACE_BATCH_SIZE items, and only the credit at the far end of the
    drain loop (and what the receipt quotes to the player) is in items. The one
    conversion point is deliberate: a batch is the unit the machine actually
    processes, so making it the stored unit keeps queue_room, the fee and the
    drain arithmetic all speaking the same language as each other.
  * It has no auto-smelt. The furnace's server-owned jobs (cogs/furnace.py:
    _try_auto_smelt) steer the market's own stock one item at a time precisely
    so they can't overshoot, and one item is not something this machine can
    do: a twenty-member server's whole target stock is 83 steel or 200 iron
    (data/materials.py: target_stock), which a single batch would sail past.
    The market's stockpile management stays on the furnace, which is also why
    this cog has no SERVER_JOB_USER_ID sentinel and its drain loop is a plain
    FIFO.
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
    BLAST_FURNACE_COLOR,
)
from utils.responses import respond
from utils.formatting import format_currency
from utils.receipts import build_receipt_embed
from database.db import InsufficientQuantity
from utils.db_helpers import (
    bank_infrastructure_fee,
    ensure_server_row,
    get_user_quantity,
    adjust_user_quantity,
    deduct_user_quantity,
    get_currency_balance,
    charge_user_fee,
    queue_room,
    queue_full_message,
)

from data.materials import (
    SMELTED_MATERIALS,
    BLAST_FURNACE_BATCH_SIZE,
    BLAST_FURNACE_COAL_COST_PER_BATCH,
    BLAST_FURNACE_RECIPES,
    blast_furnace_rate,
    upgrade_threshold,
    get_material_info,
)

PROCESS_TICK_MINUTES = 5

# How many queued jobs a status embed names individually before collapsing the
# rest into an "and N more" line.
JOB_DISPLAY_LIMIT = 10

# The most batches one command may queue. 1,000 batches is 100,000 smelted items -
# a thousand hours of a level 1 machine - and the per-user queue cap
# (server_config.blast_furnace_max_queue, 5 per level by default) is what
# normally bites first. This is the backstop on a single command, matching the
# 1,000 ceiling /furnace smelt puts on its own quantity.
MAX_BATCHES_PER_JOB = 1000


class BlastFurnaceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._production_progress: dict[int, float] = {}
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    blast_group = app_commands.Group(name="blast", description="Smelt raw materials in bulk")

    @blast_group.command(name="smelt", description="Queue a bulk batch of raw materials to be smelted")
    @app_commands.describe(
        material="What to smelt",
        quantity=f"How many batches of {BLAST_FURNACE_BATCH_SIZE} to produce",
    )
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in SMELTED_MATERIALS.items()
    ])
    async def blast_smelt(
        self,
        interaction: discord.Interaction,
        material: app_commands.Choice[str],
        quantity: app_commands.Range[int, 1, MAX_BATCHES_PER_JOB],
    ):
        recipe = BLAST_FURNACE_RECIPES[material.value]
        produced_items = quantity * BLAST_FURNACE_BATCH_SIZE

        # As at the furnace: the recipe's own inputs and the flat fuel coal are
        # tracked separately so the receipt can show each one, but they're
        # summed into `needs` to deduct and to check the user can cover both at
        # once. Both figures are already per-batch, so multiplying by the batch
        # count is all that's left to do here.
        recipe_needs: dict[str, int] = {}
        for input_id, per_batch in recipe["inputs"].items():
            recipe_needs[input_id] = recipe_needs.get(input_id, 0) + per_batch * quantity
        fuel_coal = BLAST_FURNACE_COAL_COST_PER_BATCH * quantity
        needs = dict(recipe_needs)
        needs["coal"] = needs.get("coal", 0) + fuel_coal

        # Validating and deducting share one transaction: the inputs, the fuel
        # coal, the fee and the job row all land together or not at all, and a
        # second invocation can't spend the same materials in the gap between
        # the check and the deduction.
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
                            f"You need {needed:,}x {label} for {quantity:,} batch"
                            f"{'es' if quantity != 1 else ''} but only have {have[input_id]:,}.",
                            ephemeral=True,
                        )
                        return

                await ensure_server_row(tx, interaction.guild_id)
                cfg = await tx.fetchone(
                    "SELECT blast_furnace_fee, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_rate = cfg["blast_furnace_fee"]
                currency_emoji = cfg["currency_emoji"]

                room = await queue_room(
                    tx, interaction.guild_id, interaction.user.id, "blast_furnace", quantity
                )
                if not room.fits:
                    await interaction.response.send_message(
                        queue_full_message("blast_furnace", room, unit="batch"), ephemeral=True
                    )
                    return

                # The fee is charged up front at queue time, so affordability
                # must be checked before any inventory is deducted.
                fee_total = fee_rate * quantity
                balance_after = 0.0
                if fee_total > 0:
                    balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
                    if balance < fee_total:
                        await interaction.response.send_message(
                            f"This would cost {format_currency(fee_total, currency_emoji)} up front, but you only have {format_currency(balance, currency_emoji)}.",
                            ephemeral=True,
                        )
                        return
                    balance_after = balance - fee_total

                for input_id, needed in needs.items():
                    await deduct_user_quantity(tx, interaction.user.id, input_id, needed)

                if fee_total > 0:
                    await charge_user_fee(tx, interaction.guild_id, interaction.user.id, fee_total)
                    await bank_infrastructure_fee(
                        tx, interaction.guild_id, "blast_furnace", fee_total
                    )

                # Everything already waiting that will be smelted before this
                # job, in batches - the unit the wait below is computed in.
                ahead_row = await tx.fetchone(
                    "SELECT COALESCE(SUM(quantity), 0) AS batches FROM production_jobs "
                    "WHERE guild_id = ? AND job_type = 'blast_furnace' AND status != 'complete'",
                    (interaction.guild_id,),
                )
                batches_ahead = ahead_row["batches"]

                await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'blast_furnace', ?, ?)",
                    (interaction.guild_id, interaction.user.id, material.value, quantity),
                )

                # Re-read rather than reuse the level from cfg above: this
                # job's own fee may have just upgraded the machine, and the
                # quoted wait should use the speed it will actually run at.
                level_row = await tx.fetchone(
                    "SELECT blast_furnace_level FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                level = level_row["blast_furnace_level"]
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your materials or balance changed while that was going through - "
                "nothing was queued or spent. Try again.",
                ephemeral=True,
            )
            return

        # Quoted in ITEMS rather than batches, because items are what lands in
        # the inventory - the batch count is the machine's unit, not the
        # player's. The fuel coal's remainder is taken after the recipe's own
        # coal, so the two receipt lines read as consecutive draws on the same
        # stack.
        embed = build_receipt_embed(
            title="♨️ Bulk Smelting Receipt",
            color=BLAST_FURNACE_COLOR,
            action="bulk smelting",
            product_id=material.value,
            quantity=produced_items,
            consumed=[
                (input_id, needed, have[input_id] - needed)
                for input_id, needed in recipe_needs.items()
            ],
            fuel=("coal", fuel_coal, have["coal"] - needs["coal"]),
            fuel_label="Blast Furnace Fuel",
            fee_total=fee_total,
            balance_after=balance_after,
            currency_emoji=currency_emoji,
            eta_hours=(batches_ahead + quantity) / blast_furnace_rate(level),
        )
        await respond(interaction, self.db, embed=embed)

    async def _blast_status_impl(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        cfg = await self.db.fetchone(
            "SELECT blast_furnace_level, blast_furnace_fee, blast_furnace_fees_collected, "
            "blast_furnace_max_queue, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["blast_furnace_level"]
        fee_rate = cfg["blast_furnace_fee"]
        max_queue = cfg["blast_furnace_max_queue"]
        fees_collected = cfg["blast_furnace_fees_collected"]
        currency_emoji = cfg["currency_emoji"]

        rate = blast_furnace_rate(level)
        upgrade_cost = upgrade_threshold(level + 1)

        jobs = await self.db.fetchall(
            "SELECT job_id, user_id, target_id, quantity FROM production_jobs WHERE guild_id = ? "
            "AND job_type = 'blast_furnace' AND status != 'complete' ORDER BY queued_at ASC",
            (interaction.guild_id,),
        )
        pending_batches = sum(job["quantity"] for job in jobs)

        # Both figures in the title: batches are what the queue and the fee are
        # counted in, items are what the player is waiting for, and quoting one
        # without the other invites reading the wrong one.
        embed = make_infrastructure_embed(
            emoji="♨️",
            name="Blast Furnace",
            color=BLAST_FURNACE_COLOR,
            level=level,
            speed_text=(
                f"{rate:,} batch{'es' if rate != 1 else ''}/hour "
                f"({rate * BLAST_FURNACE_BATCH_SIZE:,} items/hour)"
            ),
            fees_collected=fees_collected,
            upgrade_cost=upgrade_cost,
            currency_emoji=currency_emoji,
        )
        embed.add_field(
            name="Fee",
            value=f"{format_currency(fee_rate, currency_emoji)} per batch of {BLAST_FURNACE_BATCH_SIZE}",
            inline=True,
        )
        embed.add_field(
            name="Queue Limit",
            value=queue_limit_field_value(max_queue, level, unit="batch"),
            inline=True,
        )

        lines = []
        for job in jobs[:JOB_DISPLAY_LIMIT]:
            info = get_material_info(job["target_id"])
            emoji = info["emoji"] if info else "❓"
            name = info["name"] if info else job["target_id"]
            items = job["quantity"] * BLAST_FURNACE_BATCH_SIZE
            lines.append(f"{items:,}x {emoji} {name} • {job_owner_label(job['user_id'])}")
        if len(jobs) > JOB_DISPLAY_LIMIT:
            lines.append(f"... and {len(jobs) - JOB_DISPLAY_LIMIT} more")

        add_multi_field(
            embed,
            # An estimate at the current speed: it moves out if anyone queues
            # more behind this, and in if the machine levels up on their fees.
            queue_field_name(pending_batches, len(jobs), pending_batches / rate, unit="batch"),
            lines,
            empty_text="Nothing queued.",
        )

        await respond(interaction, self.db, embed=embed)

    @blast_group.command(name="status", description="Show blast furnace level, queue, and upgrade progress")
    async def blast_status(self, interaction: discord.Interaction):
        await self._blast_status_impl(interaction)

    @blast_group.command(name="queue", description="Alias for /blast status")
    async def blast_queue_alias(self, interaction: discord.Interaction):
        await self._blast_status_impl(interaction)

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's blast furnace processes its hourly rate
        spread over time. The loop keeps a fractional accumulator per guild so
        rates that don't divide evenly into whole batches per tick never over-
        or under-produce.

        In memory rather than persisted, exactly as the furnace's is. A restart
        can cost at most one unfinished batch's progress, which at a level 1
        machine is under an hour - proportionally the same as the furnace
        losing part of a twelve-minute item, because both are a fraction of one
        unit of work. The press persists its own accumulator instead
        (server_config.press_progress) because one unit of ITS work is days.
        """
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall(
            "SELECT guild_id, blast_furnace_level FROM server_config"
        )
        for cfg in configs:
            rate = blast_furnace_rate(cfg["blast_furnace_level"])
            progress = self._production_progress.get(cfg["guild_id"], 0.0) + (rate / ticks_per_hour)
            produced_batches = int(progress)
            self._production_progress[cfg["guild_id"]] = progress - produced_batches

            remaining_capacity = produced_batches
            while remaining_capacity > 0:
                # One transaction per job: claiming the job, crediting its
                # output and updating its row commit together, so a failure
                # can't credit goods for a job that stays queued (or retire a
                # job whose output never arrived).
                async with self.db.transaction() as tx:
                    job = await tx.fetchone(
                        """
                        SELECT * FROM production_jobs
                        WHERE guild_id = ? AND job_type = 'blast_furnace' AND status != 'complete'
                        ORDER BY queued_at ASC LIMIT 1
                        """,
                        (cfg["guild_id"],),
                    )
                    if job is None:
                        break  # no jobs waiting for this server

                    produced = min(remaining_capacity, job["quantity"])
                    new_quantity = job["quantity"] - produced
                    remaining_capacity -= produced

                    # The one place batches become items. Everything above this
                    # line - the fee, the queue cap, this loop's own capacity -
                    # is counted in batches.
                    await adjust_user_quantity(
                        tx, job["user_id"], job["target_id"],
                        produced * BLAST_FURNACE_BATCH_SIZE,
                    )

                    if new_quantity <= 0:
                        await tx.execute(
                            "UPDATE production_jobs SET status = 'complete', quantity = 0 WHERE job_id = ?",
                            (job["job_id"],),
                        )
                    else:
                        await tx.execute(
                            "UPDATE production_jobs SET quantity = ?, status = 'in_progress' WHERE job_id = ?",
                            (new_quantity, job["job_id"]),
                        )

    @process_loop.before_loop
    async def before_process_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the blast_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(BlastFurnaceCog(bot))
