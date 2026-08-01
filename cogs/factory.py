"""
cogs/factory.py

Implements /factory craft <item> <quantity>, covering component materials
(wiring, drill chassis, drill bits), fully assembled drills, storage
containers and drill upgrade packs; plus /factory upgrade <drill>, which
queues a level-up for one of the user's drills. Structurally identical to
cogs/furnace.py - see that file's comments for the FIFO queue explanation.

A drill level-up rides the ordinary factory queue rather than being its own
job type: production_jobs.job_type has a CHECK constraint that only SQLite
table rebuild could widen, and every existing query already filters on
job_type = 'factory', so a new type would silently miss the queue cap, the
status listing and the drain loop alike. An upgrade is instead a 'factory' job
whose target_id is the DRILL_UPGRADE_JOB_TARGET sentinel and whose
target_drill_id names the drill.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import (
    add_multi_field,
    job_owner_label,
    make_infrastructure_embed,
    queue_field_name,
    FACTORY_COLOR,
)
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
from utils.drills import (
    drill_choices,
    drill_label,
    drill_short_label,
    describe_cost,
    fetch_drill,
)

from data.materials import (
    COMPONENT_MATERIALS,
    DRILLS,
    STORAGE_CONTAINERS,
    UPGRADE_MATERIALS,
    DRILL_UPGRADE_JOB_TARGET,
    factory_rate,
    upgrade_threshold,
    effective_rate,
    get_material_info,
    upgrade_cost as drill_upgrade_cost,
)

PROCESS_TICK_MINUTES = 5

# How many queued jobs a status embed names individually before collapsing the
# rest into an "and N more" line.
JOB_DISPLAY_LIMIT = 10

# Everything the factory can build, merged so app_commands.choices has one
# flat list to offer. That list currently sits at 18 entries against Discord's
# hard limit of 25 static choices - the next few additions will need an
# autocomplete callback here instead.
CRAFTABLE = {**COMPONENT_MATERIALS, **DRILLS, **UPGRADE_MATERIALS, **STORAGE_CONTAINERS}

# Crafting one of these produces a tracked drill instance rather than an
# inventory stack, so the drain step has to insert rows instead of counting up.
DRILL_IDS = frozenset(DRILLS)


class FactoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._production_progress: dict[int, float] = {}
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    factory_group = app_commands.Group(name="factory", description="Craft components and drills")

    @factory_group.command(name="craft", description="Queue a component or drill to be crafted")
    @app_commands.describe(item="What to craft", quantity="How many to produce")
    @app_commands.choices(item=[
        app_commands.Choice(name=info["name"], value=key) for key, info in CRAFTABLE.items()
    ])
    async def factory_craft(self, interaction: discord.Interaction, item: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        recipe = CRAFTABLE[item.value]

        needs = {input_id: per_unit * quantity for input_id, per_unit in recipe["inputs"].items()}

        # Validating and deducting share one transaction: the inputs, the fee
        # and the job row all land together or not at all, and a second
        # invocation can't spend the same materials in the gap between the
        # check and the deduction.
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
                    "SELECT factory_fee, factory_max_queue, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_rate = cfg["factory_fee"]
                currency_emoji = cfg["currency_emoji"]
                max_queue = cfg["factory_max_queue"]
                user_queue_row = await tx.fetchone(
                    "SELECT COALESCE(SUM(quantity), 0) as queued_items FROM production_jobs WHERE guild_id = ? AND user_id = ? AND job_type = 'factory' AND status != 'complete'",
                    (interaction.guild_id, interaction.user.id),
                )
                queued_items = user_queue_row["queued_items"] if user_queue_row else 0
                if queued_items + quantity > max_queue:
                    await interaction.response.send_message(
                        f"You can only queue up to {max_queue} items worth of factory recipes per user at once. Complete some jobs first.",
                        ephemeral=True,
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
                        "UPDATE server_config SET factory_fees_collected = factory_fees_collected + ? WHERE guild_id = ?",
                        (fee_total, interaction.guild_id),
                    )
                    await self._maybe_upgrade_factory(tx, interaction.guild_id)

                items_ahead = await self._items_ahead(tx, interaction.guild_id)

                await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'factory', ?, ?)",
                    (interaction.guild_id, interaction.user.id, item.value, quantity),
                )
                level = await self._current_level(tx, interaction.guild_id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your materials or balance changed while that was going through - "
                "nothing was queued or spent. Try again.",
                ephemeral=True,
            )
            return

        embed = build_receipt_embed(
            title="🏭 Crafting Receipt",
            color=FACTORY_COLOR,
            action="crafting",
            product_id=item.value,
            quantity=quantity,
            consumed=[
                (input_id, needed, have[input_id] - needed)
                for input_id, needed in needs.items()
            ],
            fee_total=fee_total,
            balance_after=balance_after,
            currency_emoji=currency_emoji,
            eta_hours=(items_ahead + quantity) / factory_rate(level),
        )
        await respond(interaction, self.db, embed=embed)

    @staticmethod
    async def _items_ahead(db, guild_id: int) -> int:
        """Items already queued that the factory will get to before whatever is
        about to be inserted - the queue is plain FIFO, so this is the whole
        outstanding quantity."""
        row = await db.fetchone(
            "SELECT COALESCE(SUM(quantity), 0) AS items FROM production_jobs "
            "WHERE guild_id = ? AND job_type = 'factory' AND status != 'complete'",
            (guild_id,),
        )
        return row["items"]

    @staticmethod
    async def _current_level(db, guild_id: int) -> int:
        """Read after the fee lands rather than reused from the config read at
        the top: the job's own fee may have just upgraded the factory, and the
        wait quoted on the receipt should use the speed it will run at."""
        row = await db.fetchone(
            "SELECT factory_level FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["factory_level"]

    async def _upgradable_drill_autocomplete(self, interaction: discord.Interaction, current: str):
        return await drill_choices(
            self.db, interaction.user.id, current, unplaced_only=True
        )

    @factory_group.command(name="upgrade", description="Queue a level-up for one of your drills")
    @app_commands.describe(drill="Which drill to upgrade - it must be in your inventory, not placed")
    @app_commands.autocomplete(drill=_upgradable_drill_autocomplete)
    async def factory_upgrade(self, interaction: discord.Interaction, drill: int):
        have: dict[str, int] = {}
        try:
            async with self.db.transaction() as tx:
                # The drill is re-read inside the transaction, so its level -
                # which sets the price - can't change between being quoted and
                # being charged, and two upgrades can't both claim it.
                row = await fetch_drill(tx, drill, interaction.user.id)
                if row is None:
                    await interaction.response.send_message("That isn't one of your drills.", ephemeral=True)
                    return
                if row["guild_id"] is not None:
                    await interaction.response.send_message(
                        f"**{drill_label(row)}** is placed in a server. Run `/mine remove` first - "
                        f"it keeps its level and container when you pull it out.",
                        ephemeral=True,
                    )
                    return
                if row["locked_job_id"] is not None:
                    await interaction.response.send_message(
                        f"**{drill_label(row)}** is already queued for an upgrade.", ephemeral=True
                    )
                    return

                level = row["level"]
                needs = drill_upgrade_cost(row["drill_type"], level)

                for input_id, needed in needs.items():
                    have[input_id] = await get_user_quantity(tx, interaction.user.id, input_id)
                    if have[input_id] < needed:
                        info = get_material_info(input_id)
                        label = f"{info['emoji']} {info['name']}" if info else f"`{input_id}`"
                        await interaction.response.send_message(
                            f"Upgrading **{drill_label(row)}** to level {level + 1} costs {describe_cost(needs)}. "
                            f"You need {needed:,}x {label} but only have {have[input_id]:,}.",
                            ephemeral=True,
                        )
                        return

                await ensure_server_row(tx, interaction.guild_id)
                cfg = await tx.fetchone(
                    "SELECT factory_fee, factory_max_queue, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_rate = cfg["factory_fee"]
                currency_emoji = cfg["currency_emoji"]
                max_queue = cfg["factory_max_queue"]

                # An upgrade is one item of factory work, so it counts against
                # the same per-user queue cap as any craft.
                user_queue_row = await tx.fetchone(
                    "SELECT COALESCE(SUM(quantity), 0) as queued_items FROM production_jobs WHERE guild_id = ? AND user_id = ? AND job_type = 'factory' AND status != 'complete'",
                    (interaction.guild_id, interaction.user.id),
                )
                queued_items = user_queue_row["queued_items"] if user_queue_row else 0
                if queued_items + 1 > max_queue:
                    await interaction.response.send_message(
                        f"You can only queue up to {max_queue} items worth of factory recipes per user at once. Complete some jobs first.",
                        ephemeral=True,
                    )
                    return

                fee_total = fee_rate
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
                        "UPDATE server_config SET factory_fees_collected = factory_fees_collected + ? WHERE guild_id = ?",
                        (fee_total, interaction.guild_id),
                    )
                    await self._maybe_upgrade_factory(tx, interaction.guild_id)

                items_ahead = await self._items_ahead(tx, interaction.guild_id)

                # Queue first, then lock, so locked_job_id always names a job
                # that exists - a lock pointing at nothing would strand the
                # drill. Both land in the same commit either way.
                job_id = await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, target_drill_id) "
                    "VALUES (?, ?, 'factory', ?, 1, ?)",
                    (interaction.guild_id, interaction.user.id, DRILL_UPGRADE_JOB_TARGET, row["drill_id"]),
                )
                await tx.execute(
                    "UPDATE drills SET locked_job_id = ? WHERE drill_id = ? AND locked_job_id IS NULL",
                    (job_id, row["drill_id"]),
                )
                factory_level = await self._current_level(tx, interaction.guild_id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your materials or balance changed while that was going through - "
                "nothing was queued or spent. Try again.",
                ephemeral=True,
            )
            return

        embed = build_receipt_embed(
            title="🔧 Drill Upgrade Receipt",
            color=FACTORY_COLOR,
            action="upgrading",
            product_id=DRILL_UPGRADE_JOB_TARGET,
            quantity=1,
            product_label=("🔧", f"{drill_short_label(row)} → Level {level + 1}"),
            consumed=[
                (input_id, needed, have[input_id] - needed)
                for input_id, needed in needs.items()
            ],
            fee_total=fee_total,
            balance_after=balance_after,
            currency_emoji=currency_emoji,
            # An upgrade is one item of factory work, so it takes exactly as
            # long as crafting one thing from the same position in the queue.
            eta_hours=(items_ahead + 1) / factory_rate(factory_level),
        )
        embed.add_field(
            name="Mining Rate",
            value=(
                f"{effective_rate(row['drill_type'], level):g} → "
                f"**{effective_rate(row['drill_type'], level + 1):g}** items/hour once complete"
            ),
            inline=False,
        )
        embed.add_field(
            name="Locked",
            value="This drill can't be placed or modified until the upgrade finishes.",
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)

    async def _factory_status_impl(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        cfg = await self.db.fetchone(
            "SELECT factory_level, factory_fee, factory_fees_collected, factory_max_queue, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["factory_level"]
        fee_rate = cfg["factory_fee"]
        max_queue = cfg["factory_max_queue"]
        fees_collected = cfg["factory_fees_collected"]
        currency_emoji = cfg["currency_emoji"]

        rate = factory_rate(level)
        upgrade_cost = upgrade_threshold(level + 1)

        # LEFT JOIN so an upgrade job can name the drill it's working on: its
        # own target_id is a sentinel rather than a material, so there is
        # nothing to look up without the join. The joined level is the drill's
        # CURRENT one, which is exactly what the "Lv.1 → 2" arrow wants.
        jobs = await self.db.fetchall(
            """
            SELECT pj.job_id, pj.user_id, pj.target_id, pj.quantity, pj.target_drill_id,
                   d.drill_type, d.level AS drill_level
            FROM production_jobs pj
            LEFT JOIN drills d ON d.drill_id = pj.target_drill_id
            WHERE pj.guild_id = ? AND pj.job_type = 'factory' AND pj.status != 'complete'
            ORDER BY pj.queued_at ASC
            """,
            (interaction.guild_id,),
        )
        pending_items = sum(job["quantity"] for job in jobs)

        embed = make_infrastructure_embed(
            emoji="🏭",
            name="Factory",
            color=FACTORY_COLOR,
            level=level,
            # A level 1 factory really does produce one item an hour.
            speed_text=f"{rate} item{'s' if rate != 1 else ''}/hour",
            fees_collected=fees_collected,
            upgrade_cost=upgrade_cost,
            currency_emoji=currency_emoji,
        )
        embed.add_field(name="Fee", value=f"{format_currency(fee_rate, currency_emoji)} per item", inline=True)
        embed.add_field(name="Queue Limit", value=f"**{max_queue}** items per user", inline=True)

        lines = []
        for job in jobs[:JOB_DISPLAY_LIMIT]:
            if job["target_drill_id"] is not None and job["drill_type"] is not None:
                info = DRILLS[job["drill_type"]]
                label = f"🔧 {info['emoji']} {info['name']} Lv.{job['drill_level']} → {job['drill_level'] + 1}"
            else:
                info = get_material_info(job["target_id"])
                emoji = info["emoji"] if info else "❓"
                label = f"{emoji} {info['name'] if info else job['target_id']}"
            lines.append(f"{job['quantity']}x {label} • {job_owner_label(job['user_id'])}")
        if len(jobs) > JOB_DISPLAY_LIMIT:
            lines.append(f"... and {len(jobs) - JOB_DISPLAY_LIMIT} more")

        add_multi_field(
            embed,
            # An estimate at the current speed: it moves out if anyone queues
            # more behind this, and in if the factory levels up on their fees.
            queue_field_name(pending_items, len(jobs), pending_items / rate),
            lines,
            empty_text="Nothing queued.",
        )

        await respond(interaction, self.db, embed=embed)

    @factory_group.command(name="status", description="Show factory level, queue, and upgrade progress")
    async def factory_status(self, interaction: discord.Interaction):
        await self._factory_status_impl(interaction)

    @factory_group.command(name="queue", description="Alias for /factory status")
    async def factory_queue_alias(self, interaction: discord.Interaction):
        await self._factory_status_impl(interaction)

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's factory processes its hourly rate spread
        over time. The loop keeps a fractional accumulator per guild so level
        1 can produce 1 item/hour without over-producing every 5 minutes."""
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall(
            "SELECT guild_id, factory_level FROM server_config"
        )
        for cfg in configs:
            rate = factory_rate(cfg["factory_level"])
            progress = self._production_progress.get(cfg["guild_id"], 0.0) + (rate / ticks_per_hour)
            produced_units = int(progress)
            self._production_progress[cfg["guild_id"]] = progress - produced_units

            remaining_capacity = produced_units
            while remaining_capacity > 0:
                # One transaction per job: claiming the job, delivering its
                # output and updating its row commit together, so a failure
                # can't hand out goods for a job that stays queued (or retire
                # a job whose output never arrived).
                async with self.db.transaction() as tx:
                    job = await tx.fetchone(
                        """
                        SELECT * FROM production_jobs
                        WHERE guild_id = ? AND job_type = 'factory' AND status != 'complete'
                        ORDER BY queued_at ASC LIMIT 1
                        """,
                        (cfg["guild_id"],),
                    )
                    if job is None:
                        break

                    produced = min(remaining_capacity, job["quantity"])
                    new_quantity = job["quantity"] - produced
                    remaining_capacity -= produced

                    is_upgrade = job["target_drill_id"] is not None
                    if is_upgrade:
                        pass  # applied on completion below, never partially
                    elif job["target_id"] in DRILL_IDS:
                        await self._deliver_drills(tx, job["user_id"], job["target_id"], produced)
                    else:
                        await adjust_user_quantity(tx, job["user_id"], job["target_id"], produced)

                    if new_quantity <= 0:
                        await tx.execute(
                            "UPDATE production_jobs SET status = 'complete', quantity = 0 WHERE job_id = ?",
                            (job["job_id"],),
                        )
                        if is_upgrade:
                            # Matching on locked_job_id makes this idempotent: a
                            # job that somehow drains twice can't bump a drill that
                            # has since been locked by a different upgrade.
                            await tx.execute(
                                "UPDATE drills SET level = level + 1, locked_job_id = NULL "
                                "WHERE drill_id = ? AND locked_job_id = ?",
                                (job["target_drill_id"], job["job_id"]),
                            )
                    else:
                        await tx.execute(
                            "UPDATE production_jobs SET quantity = ?, status = 'in_progress' WHERE job_id = ?",
                            (new_quantity, job["job_id"]),
                        )

    async def _deliver_drills(self, db, user_id: int, drill_type: str, count: int):
        """A crafted drill becomes a tracked instance rather than an inventory
        stack, so it carries its own level and container from the moment it
        exists. `count` can be more than one when a levelled-up factory drains
        several units of the same job in a tick, hence the loop."""
        for _ in range(count):
            await db.execute(
                "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (NULL, ?, ?)",
                (user_id, drill_type),
            )

    async def _release_stale_drill_locks(self):
        """Frees any drill still pointing at a job that has already finished or
        no longer exists, which would otherwise lock that drill out of every
        command forever.

        Queueing and completing an upgrade are each one transaction now, so
        this shouldn't find anything - it stays as a cheap backstop for rows
        left behind by the pre-transaction code, and for anything that manages
        to desync the two in future."""
        await self.db.execute(
            "UPDATE drills SET locked_job_id = NULL WHERE locked_job_id IS NOT NULL "
            "AND locked_job_id NOT IN (SELECT job_id FROM production_jobs WHERE status != 'complete')"
        )

    async def cog_load(self):
        await self._release_stale_drill_locks()

    async def _maybe_upgrade_factory(self, db, guild_id: int):
        """Takes an executor rather than using self.db, so it reads the fee
        total its caller just wrote rather than the pre-transaction value."""
        cfg = await db.fetchone(
            "SELECT factory_level, factory_fees_collected FROM server_config WHERE guild_id = ?",
            (guild_id,),
        )
        # Loops because one expensive job can cross more than one threshold,
        # and there's no cap to stop at.
        level, collected = cfg["factory_level"], cfg["factory_fees_collected"]
        while collected >= upgrade_threshold(level + 1):
            level += 1
        if level != cfg["factory_level"]:
            await db.execute(
                "UPDATE server_config SET factory_level = ? WHERE guild_id = ?",
                (level, guild_id),
            )

    @process_loop.before_loop
    async def before_process_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the factory_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(FactoryCog(bot))
