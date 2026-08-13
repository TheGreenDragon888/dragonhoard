"""
cogs/furnace.py

Implements /furnace smelt <material> <quantity>, which:
  1. Checks the user has enough raw materials and can afford the fee
  2. Charges the fee and deducts the raw materials immediately, then queues
     a production job
  3. A background loop processes queued jobs at the server's furnace_level
     rate (5/10/15 per hour), crediting completed items to the user.
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
    FURNACE_COLOR,
)
from utils.responses import respond
from utils.formatting import format_currency
from utils.receipts import build_receipt_embed
from utils.guild_helpers import human_member_count
from database.db import InsufficientQuantity
from utils.db_helpers import (
    apply_machine_upgrades,
    ensure_server_row,
    get_user_quantity,
    adjust_user_quantity,
    deduct_user_quantity,
    get_server_stock,
    adjust_server_stock,
    deduct_server_stock,
    get_currency_balance,
    charge_user_fee,
    queue_room,
    queue_full_message,
)

from data.materials import (
    SMELTED_MATERIALS,
    furnace_rate,
    upgrade_threshold,
    get_material_info,
    FURNACE_COAL_COST_PER_UNIT,
    target_stock,
)

PROCESS_TICK_MINUTES = 5

# How many queued jobs a status embed names individually before collapsing the
# rest into an "and N more" line.
JOB_DISPLAY_LIMIT = 10

# Discord snowflake IDs are always large positive integers, so 0 is safe to
# reserve as a sentinel marking a production_jobs row as owned by the server
# itself (the auto-smelt feature below) rather than a real user.
SERVER_JOB_USER_ID = 0

# Target ratio of iron:steel the server's auto-smelt steers its own stockpile
# towards when both recipes draw from the same iron_ore supply.
SERVER_IRON_TO_STEEL_RATIO = 4


class FurnaceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._production_progress: dict[int, float] = {}
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    furnace_group = app_commands.Group(name="furnace", description="Smelt raw materials")

    @furnace_group.command(name="smelt", description="Queue raw materials to be smelted")
    @app_commands.describe(material="What to smelt", quantity="How many to produce")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in SMELTED_MATERIALS.items()
    ])
    async def furnace_smelt(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        recipe = SMELTED_MATERIALS[material.value]

        # The recipe's own inputs and the flat per-item coal cost of running
        # the furnace at all are tracked separately so the receipt can show
        # each one, but they're summed into `needs` to deduct and to check the
        # user can cover both at once (e.g. steel's 4 coal per unit plus fuel).
        recipe_needs: dict[str, int] = {}
        for input_id, per_unit in recipe["inputs"].items():
            recipe_needs[input_id] = recipe_needs.get(input_id, 0) + per_unit * quantity
        fuel_coal = FURNACE_COAL_COST_PER_UNIT * quantity
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
                            f"You need {needed}x {label} but only have {have[input_id]}.", ephemeral=True
                        )
                        return

                await ensure_server_row(tx, interaction.guild_id)
                cfg = await tx.fetchone(
                    "SELECT furnace_fee, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_rate = cfg["furnace_fee"]
                currency_emoji = cfg["currency_emoji"]

                room = await queue_room(tx, interaction.guild_id, interaction.user.id, "furnace", quantity)
                if not room.fits:
                    await interaction.response.send_message(
                        queue_full_message("furnace", room), ephemeral=True
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
                    await tx.execute(
                        "UPDATE server_config SET furnace_fees_collected = furnace_fees_collected + ? WHERE guild_id = ?",
                        (fee_total, interaction.guild_id),
                    )
                    await apply_machine_upgrades(tx, interaction.guild_id, "furnace")

                # Everything already waiting that will be smelted before this
                # job. The server's own auto-smelt jobs are excluded because
                # the drain loop always sorts them behind real players', so
                # they never actually hold anyone up.
                ahead_row = await tx.fetchone(
                    "SELECT COALESCE(SUM(quantity), 0) AS items FROM production_jobs "
                    "WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete' "
                    "AND user_id != ?",
                    (interaction.guild_id, SERVER_JOB_USER_ID),
                )
                items_ahead = ahead_row["items"]

                await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'furnace', ?, ?)",
                    (interaction.guild_id, interaction.user.id, material.value, quantity),
                )

                # Re-read rather than reuse the level from cfg above: this
                # job's own fee may have just upgraded the furnace, and the
                # quoted wait should use the speed it will actually run at.
                level_row = await tx.fetchone(
                    "SELECT furnace_level FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                level = level_row["furnace_level"]
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your materials or balance changed while that was going through - "
                "nothing was queued or spent. Try again.",
                ephemeral=True,
            )
            return

        # The fuel coal's remainder is taken after the recipe's own coal, so
        # the two receipt lines read as consecutive draws on the same stack.
        embed = build_receipt_embed(
            title="🔥 Smelting Receipt",
            color=FURNACE_COLOR,
            action="smelting",
            product_id=material.value,
            quantity=quantity,
            consumed=[
                (input_id, needed, have[input_id] - needed)
                for input_id, needed in recipe_needs.items()
            ],
            fuel=("coal", fuel_coal, have["coal"] - needs["coal"]),
            fee_total=fee_total,
            balance_after=balance_after,
            currency_emoji=currency_emoji,
            eta_hours=(items_ahead + quantity) / furnace_rate(level),
        )
        await respond(interaction, self.db, embed=embed)

    async def _furnace_status_impl(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        cfg = await self.db.fetchone(
            "SELECT furnace_level, furnace_fee, furnace_fees_collected, furnace_max_queue, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["furnace_level"]
        fee_rate = cfg["furnace_fee"]
        max_queue = cfg["furnace_max_queue"]
        fees_collected = cfg["furnace_fees_collected"]
        currency_emoji = cfg["currency_emoji"]

        rate = furnace_rate(level)
        upgrade_cost = upgrade_threshold(level + 1)

        jobs = await self.db.fetchall(
            "SELECT job_id, user_id, target_id, quantity FROM production_jobs WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete' ORDER BY queued_at ASC",
            (interaction.guild_id,),
        )
        # Listed in the order the furnace will actually work through them: the
        # drain loop puts the server's own auto-smelt jobs last, and a stable
        # sort keeps everything else in queued_at order.
        jobs = sorted(jobs, key=lambda job: job["user_id"] == SERVER_JOB_USER_ID)
        pending_items = sum(job["quantity"] for job in jobs)

        embed = make_infrastructure_embed(
            emoji="🔥",
            name="Furnace",
            color=FURNACE_COLOR,
            level=level,
            speed_text=f"{rate} items/hour",
            fees_collected=fees_collected,
            upgrade_cost=upgrade_cost,
            currency_emoji=currency_emoji,
        )
        embed.add_field(name="Fee", value=f"{format_currency(fee_rate, currency_emoji)} per item", inline=True)
        embed.add_field(name="Queue Limit", value=queue_limit_field_value(max_queue, level), inline=True)

        lines = []
        for job in jobs[:JOB_DISPLAY_LIMIT]:
            info = get_material_info(job["target_id"])
            emoji = info["emoji"] if info else "❓"
            name = info["name"] if info else job["target_id"]
            # The server's own auto-smelt jobs have no member to mention.
            owner = (
                "🏛️ Server" if job["user_id"] == SERVER_JOB_USER_ID
                else job_owner_label(job["user_id"])
            )
            lines.append(f"{job['quantity']}x {emoji} {name} • {owner}")
        if len(jobs) > JOB_DISPLAY_LIMIT:
            lines.append(f"... and {len(jobs) - JOB_DISPLAY_LIMIT} more")

        add_multi_field(
            embed,
            # An estimate at the current speed: it moves out if anyone queues
            # more behind this, and in if the furnace levels up on their fees.
            queue_field_name(pending_items, len(jobs), pending_items / rate),
            lines,
            empty_text="Nothing queued.",
        )

        await respond(interaction, self.db, embed=embed)

    @furnace_group.command(name="status", description="Show furnace level, queue, and upgrade progress")
    async def furnace_status(self, interaction: discord.Interaction):
        await self._furnace_status_impl(interaction)

    @furnace_group.command(name="queue", description="Alias for /furnace status")
    async def furnace_queue_alias(self, interaction: discord.Interaction):
        await self._furnace_status_impl(interaction)

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's furnace processes its hourly rate spread
        over time. The loop keeps a fractional accumulator per guild so rates
        that don't divide evenly into whole items per tick never over- or
        under-produce."""
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall(
            "SELECT guild_id, furnace_level FROM server_config"
        )
        for cfg in configs:
            rate = furnace_rate(cfg["furnace_level"])
            progress = self._production_progress.get(cfg["guild_id"], 0.0) + (rate / ticks_per_hour)
            produced_units = int(progress)
            self._production_progress[cfg["guild_id"]] = progress - produced_units

            remaining_capacity = produced_units
            while remaining_capacity > 0:
                # One transaction per job: claiming the job, crediting its
                # output and updating its row commit together, so a failure
                # can't credit goods for a job that stays queued (or retire a
                # job whose output never arrived).
                async with self.db.transaction() as tx:
                    # Real users' jobs always process ahead of the server's own
                    # auto-smelt job (see _try_auto_smelt) so the server never
                    # hogs the furnace from the people actually playing.
                    job = await tx.fetchone(
                        """
                        SELECT * FROM production_jobs
                        WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete'
                        ORDER BY (user_id = ?) ASC, queued_at ASC LIMIT 1
                        """,
                        (cfg["guild_id"], SERVER_JOB_USER_ID),
                    )
                    if job is None:
                        break  # no jobs waiting for this server

                    produced = min(remaining_capacity, job["quantity"])
                    new_quantity = job["quantity"] - produced
                    remaining_capacity -= produced

                    # Credit the produced items - to the server's own market
                    # storage for its auto-smelt jobs, otherwise to the user.
                    if job["user_id"] == SERVER_JOB_USER_ID:
                        await adjust_server_stock(tx, job["guild_id"], job["target_id"], produced)
                    else:
                        await adjust_user_quantity(tx, job["user_id"], job["target_id"], produced)

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

            # If nothing at all is queued for this guild's furnace, let the
            # server consider queuing its own auto-smelt job(s).
            pending = await self.db.fetchone(
                "SELECT 1 FROM production_jobs WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete' LIMIT 1",
                (cfg["guild_id"],),
            )
            if pending is None:
                await self._try_auto_smelt(cfg["guild_id"])

    async def _pending_server_job(self, guild_id: int, target_ids: tuple[str, ...]):
        """Gates the one-item-at-a-time auto-smelt jobs below: looks for any
        still-incomplete furnace job against target_ids. Producing a batch
        sized off the whole ore surplus in one shot can badly overshoot -
        e.g. it once turned a 500-ore surplus into 25 steel in a single job
        against a near-empty steel stock. Queuing one item at a time, and
        never a second one before the first is confirmed done, keeps each
        step small.

        Returns (job, blocked_by_user, servers_item_still_in_flight):
        blocked_by_user is True when a real player has one of target_ids
        queued (user_id isn't the server's - yield to them);
        servers_item_still_in_flight is True when the server's own prior
        one-item job (user_id is the server's, quantity == 1) hasn't
        completed yet. Callers should only queue a new job when `job` is
        None - if a job exists but matches neither shape (e.g. a stale
        multi-item job from before one-item-per-job was introduced), leave
        it alone rather than risk queuing a competing job on top of it."""
        placeholders = ",".join("?" * len(target_ids))
        job = await self.db.fetchone(
            f"""
            SELECT * FROM production_jobs
            WHERE guild_id = ? AND job_type = 'furnace' AND target_id IN ({placeholders})
              AND status != 'complete'
            LIMIT 1
            """,
            (guild_id, *target_ids),
        )
        blocked_by_user = job is not None and job["user_id"] != SERVER_JOB_USER_ID
        servers_item_still_in_flight = (
            job is not None and job["user_id"] == SERVER_JOB_USER_ID and job["quantity"] == 1
        )
        return job, blocked_by_user, servers_item_still_in_flight

    async def _try_auto_smelt(self, guild_id: int):
        """Queues the server's own furnace job(s) against its own material
        storage - only called when the furnace queue is completely empty.
        Follows the normal recipe cost + coal tax, but skips the furnace fee
        entirely (the server isn't paying itself). Only touches ore that's
        above the market's target stock for that ore, so it never eats into
        the reserve the market's buy-price curve is centered on. Every
        recipe here is queued one item at a time (see _pending_server_job)
        rather than as a single large batch."""
        guild = self.bot.get_guild(guild_id)
        if guild is None or not guild.member_count:
            return
        member_count = await human_member_count(guild)

        coal_stock = await get_server_stock(self.db, guild_id, "coal")
        jobs_to_queue: list[tuple[str, int, dict[str, int]]] = []

        iron_ore_target = target_stock(member_count, "iron_ore")
        iron_ore_stock = await get_server_stock(self.db, guild_id, "iron_ore")
        if iron_ore_stock >= iron_ore_target:
            surplus = iron_ore_stock - iron_ore_target
            pending_job, blocked_by_user, servers_item_still_in_flight = await self._pending_server_job(
                guild_id, ("iron", "steel")
            )
            if pending_job is None:
                iron_stock = await get_server_stock(self.db, guild_id, "iron")
                steel_stock = await get_server_stock(self.db, guild_id, "steel")
                # Steer the stockpile towards an iron:steel 4:1 ratio -
                # produce whichever one is currently under-represented.
                recipe_id = "steel" if steel_stock < iron_stock / SERVER_IRON_TO_STEEL_RATIO else "iron"
                recipe = SMELTED_MATERIALS[recipe_id]
                ore_per_unit = recipe["inputs"]["iron_ore"]
                coal_per_unit = recipe["inputs"].get("coal", 0) + FURNACE_COAL_COST_PER_UNIT
                quantity = min(1, surplus // ore_per_unit, coal_stock // coal_per_unit)
                if quantity > 0:
                    jobs_to_queue.append((recipe_id, quantity, {"iron_ore": ore_per_unit * quantity, "coal": coal_per_unit * quantity}))
                    coal_stock -= coal_per_unit * quantity
            elif not blocked_by_user and not servers_item_still_in_flight:
                pass  # unexpected job shape - leave it alone, see _pending_server_job

        copper_ore_target = target_stock(member_count, "copper_ore")
        copper_ore_stock = await get_server_stock(self.db, guild_id, "copper_ore")
        if copper_ore_stock >= copper_ore_target:
            surplus = copper_ore_stock - copper_ore_target
            pending_job, blocked_by_user, servers_item_still_in_flight = await self._pending_server_job(
                guild_id, ("copper",)
            )
            if pending_job is None:
                recipe = SMELTED_MATERIALS["copper"]
                ore_per_unit = recipe["inputs"]["copper_ore"]
                coal_per_unit = recipe["inputs"].get("coal", 0) + FURNACE_COAL_COST_PER_UNIT
                quantity = min(1, surplus // ore_per_unit, coal_stock // coal_per_unit)
                if quantity > 0:
                    jobs_to_queue.append(("copper", quantity, {"copper_ore": ore_per_unit * quantity, "coal": coal_per_unit * quantity}))
            elif not blocked_by_user and not servers_item_still_in_flight:
                pass  # unexpected job shape - leave it alone, see _pending_server_job

        for target_id, quantity, needs in jobs_to_queue:
            # Consuming the server's ore and queueing the job it pays for
            # commit together - otherwise a failure between them would burn the
            # market's stock with nothing queued to show for it. The guarded
            # deduction also stops the server smelting ore it no longer has if
            # a player bought some between the stock read above and here.
            try:
                async with self.db.transaction() as tx:
                    for material_id, amount in needs.items():
                        await deduct_server_stock(tx, guild_id, material_id, amount)
                    await tx.execute(
                        "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'furnace', ?, ?)",
                        (guild_id, SERVER_JOB_USER_ID, target_id, quantity),
                    )
            except InsufficientQuantity:
                # The market sold the ore out from under this tick. Nothing was
                # taken; the next tick reconsiders from current stock.
                continue

    @process_loop.before_loop
    async def before_process_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the furnace_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(FurnaceCog(bot))
