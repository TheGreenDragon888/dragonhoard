"""
cogs/scrapper.py

Implements the scrapper, the fourth piece of server infrastructure:
  - /scrapper scrap <item> <quantity> - recycle components, containers or
                                        upgrade packs back into materials
  - /scrapper drill <drill>           - break down one of your drills
  - /scrapper status | /scrapper queue - level, fee, queue

Structurally the factory in reverse, and deliberately built from the same
parts: the same server_config column naming, the same production_jobs rows, the
same fee-funded automatic level-up, the same FIFO drain loop. See cogs/factory.py
for the queue mechanics; only what differs is commented here.

Why it exists: components, containers and drills are the one class of goods
with no exit. docs/market.md section 3 keeps finished goods off the market, so
before this a mis-planned batch of drill bits or an out-grown iron drill sat in
an inventory forever. The scrapper is their way out, at half the recipe (see
scrap_yield in data/materials.py) and one tier at a time - scrapping a
component or container gives back materials, and scrapping those gives back
materials again.

Drills are the one exception to "one tier at a time": scrapping a drill skips
the component tier entirely, via drill_scrap_yield rather than scrap_yield -
see that function for why a whole wiring or chassis is never handed back.

Two subcommands rather than one, because Discord will not accept a single
parameter that is both a material chosen from a list and a drill chosen from an
autocomplete. That split turns out to be the honest one anyway: a drill is an
individually tracked row with a level and a container to deal with, not a
quantity of something.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import (
    add_multi_field,
    job_owner_label,
    make_embed,
    make_infrastructure_embed,
    queue_field_name,
    queue_limit_field_value,
    SCRAPPER_COLOR,
)
from utils.responses import respond
from utils.formatting import (
    format_currency,
    format_price,
    format_relative_timestamp,
    DEFAULT_CURRENCY_EMOJI,
)
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
from utils.drills import (
    DrillScope,
    drill_choices,
    drill_label,
    drill_short_label,
    describe_cost,
    fetch_drill,
    set_container,
)

from data.materials import (
    COMPONENT_MATERIALS,
    DRILLS,
    STORAGE_CONTAINERS,
    UPGRADE_MATERIALS,
    DRILL_SCRAP_JOB_TARGET,
    drill_scrap_yield,
    scrap_yield,
    scrapper_rate,
    upgrade_threshold,
    get_material_info,
)

PROCESS_TICK_MINUTES = 5

# How many queued jobs a status embed names individually before collapsing the
# rest into an "and N more" line.
JOB_DISPLAY_LIMIT = 10

# Everything the scrapper takes as a stack. 13 entries, comfortably inside
# Discord's hard limit of 25 static choices - folding the five drills in here
# too would put it at 18, which is the no-headroom position /factory craft is
# already in, and drills need their own command anyway (see the module
# docstring).
#
# Ultra dense matter is deliberately absent. It's the terminal prestige item,
# nothing consumes it yet, and until something does, allowing it to be scrapped
# would only ever turn ten diamonds into five with no upside.
SCRAPPABLE = {**COMPONENT_MATERIALS, **STORAGE_CONTAINERS, **UPGRADE_MATERIALS}


class ScrapperCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._production_progress: dict[int, float] = {}
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    scrapper_group = app_commands.Group(name="scrapper", description="Recycle components and drills back into materials")

    @staticmethod
    async def _items_ahead(db, guild_id: int) -> int:
        """Everything already queued at this scrapper, which is everything
        that will be processed before a job about to be inserted - the queue is
        plain FIFO."""
        row = await db.fetchone(
            "SELECT COALESCE(SUM(quantity), 0) AS items FROM production_jobs "
            "WHERE guild_id = ? AND job_type = 'scrapper' AND status != 'complete'",
            (guild_id,),
        )
        return row["items"]

    @staticmethod
    async def _current_level(db, guild_id: int) -> int:
        """Read after the fee lands rather than reused from the config read at
        the top: the job's own fee may have just upgraded the scrapper, and the
        wait quoted on the receipt should use the speed it will run at."""
        row = await db.fetchone(
            "SELECT scrapper_level FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["scrapper_level"]

    @scrapper_group.command(name="scrap", description="Recycle items back into half of what they were made from")
    @app_commands.describe(item="What to break down", quantity="How many to break down — leave blank for 1")
    @app_commands.choices(item=[
        app_commands.Choice(name=info["name"], value=key) for key, info in SCRAPPABLE.items()
    ])
    async def scrapper_scrap(self, interaction: discord.Interaction, item: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000] | None = None):
        quantity = quantity or 1
        yields = scrap_yield(item.value)

        have = 0
        try:
            async with self.db.transaction() as tx:
                have = await get_user_quantity(tx, interaction.user.id, item.value)
                if have < quantity:
                    await interaction.response.send_message(
                        f"You only have {have:,}x {item.name}.", ephemeral=True
                    )
                    return

                await ensure_server_row(tx, interaction.guild_id)
                cfg = await tx.fetchone(
                    "SELECT scrapper_fee, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_rate = cfg["scrapper_fee"]
                currency_emoji = cfg["currency_emoji"]

                room = await queue_room(tx, interaction.guild_id, interaction.user.id, "scrapper", quantity)
                if not room.fits:
                    await interaction.response.send_message(
                        queue_full_message("scrapper", room), ephemeral=True
                    )
                    return

                # Charged up front like every other machine, and the items go
                # in now too - what the scrapper is holding has left your
                # inventory, exactly as materials queued at the furnace have.
                fee_total = fee_rate * quantity
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

                await deduct_user_quantity(tx, interaction.user.id, item.value, quantity)

                if fee_total > 0:
                    await charge_user_fee(tx, interaction.guild_id, interaction.user.id, fee_total)
                    await bank_infrastructure_fee(
                        tx, interaction.guild_id, "scrapper", fee_total
                    )

                items_ahead = await self._items_ahead(tx, interaction.guild_id)
                await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                    "VALUES (?, ?, 'scrapper', ?, ?)",
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
            title="♻️ Scrapping Receipt",
            color=SCRAPPER_COLOR,
            action="recycling",
            product_id=item.value,
            quantity=quantity,
            consumed=[(item.value, quantity, have - quantity)],
            fee_total=fee_total,
            balance_after=balance_after,
            currency_emoji=currency_emoji,
            eta_hours=(items_ahead + quantity) / scrapper_rate(level),
        )
        embed.add_field(
            name="You'll Get Back",
            value=describe_cost({m: q * quantity for m, q in yields.items()}),
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)

    async def _scrappable_drill_autocomplete(self, interaction: discord.Interaction, current: str):
        """Only unplaced drills. A placed one is deliberately NOT pulled out
        automatically the way /factory upgrade does it: an upgrade is something
        you can undo by upgrading again, and this destroys the drill outright,
        so it's worth making the player take it out of the ground themselves
        first."""
        return await drill_choices(
            self.db, interaction.user.id, current, scope=DrillScope.UNPLACED
        )

    @scrapper_group.command(name="drill", description="Break one of your drills down into its parts")
    @app_commands.describe(drill="Which drill to scrap - it must be in your inventory, not placed")
    @app_commands.autocomplete(drill=_scrappable_drill_autocomplete)
    async def scrapper_drill(self, interaction: discord.Interaction, drill: int):
        returned_container: str | None = None
        try:
            async with self.db.transaction() as tx:
                row = await fetch_drill(tx, drill, interaction.user.id)
                if row is None:
                    await interaction.response.send_message("That isn't one of your drills.", ephemeral=True)
                    return
                if row["guild_id"] is not None:
                    await interaction.response.send_message(
                        f"**{drill_label(row)}** is placed in a server. Run `/mine remove` first - "
                        f"scrapping destroys the drill, so it's worth being sure.",
                        ephemeral=True,
                    )
                    return
                if row["locked_job_id"] is not None:
                    await interaction.response.send_message(
                        f"**{drill_label(row)}** is already queued at a machine.", ephemeral=True
                    )
                    return

                await ensure_server_row(tx, interaction.guild_id)
                cfg = await tx.fetchone(
                    "SELECT scrapper_fee, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                fee_total = cfg["scrapper_fee"]
                currency_emoji = cfg["currency_emoji"]

                # A drill is one item of scrapper work, so it counts as one
                # against the same per-user queue cap.
                room = await queue_room(tx, interaction.guild_id, interaction.user.id, "scrapper", 1)
                if not room.fits:
                    await interaction.response.send_message(
                        queue_full_message("scrapper", room), ephemeral=True
                    )
                    return

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

                # The container comes off intact rather than going into the
                # machine with the drill. It's an ordinary fungible item with
                # no per-instance state, /mine detach already hands it back for
                # free, and destroying it as a side effect of scrapping the
                # drill would be a cost the player never agreed to.
                if row["container_type"] is not None:
                    if not await set_container(tx, row, None):
                        await interaction.response.send_message(
                            "That drill's container changed while this was going through. Try again.",
                            ephemeral=True,
                        )
                        return
                    returned_container = row["container_type"]
                    await adjust_user_quantity(tx, interaction.user.id, returned_container, 1)

                if fee_total > 0:
                    await charge_user_fee(tx, interaction.guild_id, interaction.user.id, fee_total)
                    await bank_infrastructure_fee(
                        tx, interaction.guild_id, "scrapper", fee_total
                    )

                items_ahead = await self._items_ahead(tx, interaction.guild_id)

                # Queue first, then lock, so locked_job_id always names a job
                # that exists - a lock pointing at nothing would strand the
                # drill. Both land in the same commit either way.
                #
                # The drills row is NOT deleted here. It goes when the job
                # completes, which is what lets /scrapper status name the drill
                # it's working on, and meanwhile the lock is what keeps it from
                # being placed or modified - the same mechanism a queued
                # upgrade uses, rather than a second one.
                job_id = await tx.execute(
                    "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity, target_drill_id) "
                    "VALUES (?, ?, 'scrapper', ?, 1, ?)",
                    (interaction.guild_id, interaction.user.id, DRILL_SCRAP_JOB_TARGET, row["drill_id"]),
                )
                await tx.execute(
                    "UPDATE drills SET locked_job_id = ? WHERE drill_id = ? AND locked_job_id IS NULL",
                    (job_id, row["drill_id"]),
                )
                level = await self._current_level(tx, interaction.guild_id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your balance changed while that was going through - nothing was queued or spent. Try again.",
                ephemeral=True,
            )
            return

        # Built by hand rather than through build_receipt_embed, which always
        # renders a "Consumed" field listing what left the inventory. A drill
        # isn't a stack - it's the row being destroyed - so that field would
        # read "None" about the one thing this command actually consumes.
        embed = make_embed(
            "♻️ Drill Scrapping Receipt",
            SCRAPPER_COLOR,
            description=(
                f"Queued {DRILLS[row['drill_type']]['emoji']} **{drill_short_label(row)}** for "
                f"recycling. It will be broken down "
                f"{format_relative_timestamp((items_ahead + 1) / scrapper_rate(level))}."
            ),
        )
        embed.add_field(
            name="You'll Get Back", value=describe_cost(drill_scrap_yield(row["drill_type"])), inline=False
        )
        if fee_total > 0:
            embed.add_field(
                name="Fee Paid",
                value=(
                    f"{currency_emoji or DEFAULT_CURRENCY_EMOJI} "
                    f"**{format_price(fee_total, round_up=True)}** "
                    f"({format_price(balance_after)} remaining)"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Fee Paid", value="Free", inline=False)
        if returned_container is not None:
            info = get_material_info(returned_container)
            embed.add_field(
                name="Container Removed",
                value=f"{info['emoji']} **{info['name']}** was pulled off first and is back in your inventory.",
                inline=False,
            )
        embed.add_field(
            name="This Is Permanent",
            value=(
                "The drill is destroyed when the job finishes, losing its level. "
                "It can't be placed or modified in the meantime."
            ),
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)

    async def _scrapper_status_impl(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        cfg = await self.db.fetchone(
            "SELECT scrapper_level, scrapper_fee, scrapper_fees_collected, scrapper_max_queue, "
            "currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["scrapper_level"]
        fee_rate = cfg["scrapper_fee"]
        max_queue = cfg["scrapper_max_queue"]
        fees_collected = cfg["scrapper_fees_collected"]
        currency_emoji = cfg["currency_emoji"]

        rate = scrapper_rate(level)

        # LEFT JOIN so a drill scrap can name the drill it's working on: its
        # own target_id is a sentinel rather than a material, so there is
        # nothing to look up without the join. Same shape as /factory status.
        jobs = await self.db.fetchall(
            """
            SELECT pj.job_id, pj.user_id, pj.target_id, pj.quantity, pj.target_drill_id,
                   d.drill_type, d.level AS drill_level
            FROM production_jobs pj
            LEFT JOIN drills d ON d.drill_id = pj.target_drill_id
            WHERE pj.guild_id = ? AND pj.job_type = 'scrapper' AND pj.status != 'complete'
            ORDER BY pj.queued_at ASC
            """,
            (interaction.guild_id,),
        )
        pending_items = sum(job["quantity"] for job in jobs)

        embed = make_infrastructure_embed(
            emoji="♻️",
            name="Scrapper",
            color=SCRAPPER_COLOR,
            level=level,
            speed_text=f"{rate} item{'s' if rate != 1 else ''}/hour",
            fees_collected=fees_collected,
            upgrade_cost=upgrade_threshold(level + 1),
            currency_emoji=currency_emoji,
        )
        embed.add_field(name="Fee", value=f"{format_currency(fee_rate, currency_emoji)} per item", inline=True)
        embed.add_field(name="Queue Limit", value=queue_limit_field_value(max_queue, level), inline=True)

        lines = []
        for job in jobs[:JOB_DISPLAY_LIMIT]:
            if job["target_drill_id"] is not None and job["drill_type"] is not None:
                info = DRILLS[job["drill_type"]]
                label = f"♻️ {info['emoji']} {info['name']} Lv.{job['drill_level']}"
            else:
                info = get_material_info(job["target_id"])
                emoji = info["emoji"] if info else "❓"
                label = f"{emoji} {info['name'] if info else job['target_id']}"
            lines.append(f"{job['quantity']}x {label} • {job_owner_label(job['user_id'])}")
        if len(jobs) > JOB_DISPLAY_LIMIT:
            lines.append(f"... and {len(jobs) - JOB_DISPLAY_LIMIT} more")

        add_multi_field(
            embed,
            queue_field_name(pending_items, len(jobs), pending_items / rate),
            lines,
            empty_text="Nothing queued.",
        )

        await respond(interaction, self.db, embed=embed)

    @scrapper_group.command(name="status", description="Show scrapper level, queue, and upgrade progress")
    async def scrapper_status(self, interaction: discord.Interaction):
        await self._scrapper_status_impl(interaction)

    @scrapper_group.command(name="queue", description="Alias for /scrapper status")
    async def scrapper_queue_alias(self, interaction: discord.Interaction):
        await self._scrapper_status_impl(interaction)

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's scrapper works through its hourly rate
        spread over time, keeping a fractional accumulator per guild so a slow
        machine doesn't over-produce every 5 minutes. See cogs/factory.py."""
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall("SELECT guild_id, scrapper_level FROM server_config")
        for cfg in configs:
            rate = scrapper_rate(cfg["scrapper_level"])
            progress = self._production_progress.get(cfg["guild_id"], 0.0) + (rate / ticks_per_hour)
            produced_units = int(progress)
            self._production_progress[cfg["guild_id"]] = progress - produced_units

            remaining_capacity = produced_units
            while remaining_capacity > 0:
                async with self.db.transaction() as tx:
                    job = await tx.fetchone(
                        """
                        SELECT pj.*, d.drill_type
                        FROM production_jobs pj
                        LEFT JOIN drills d ON d.drill_id = pj.target_drill_id
                        WHERE pj.guild_id = ? AND pj.job_type = 'scrapper' AND pj.status != 'complete'
                        ORDER BY pj.queued_at ASC LIMIT 1
                        """,
                        (cfg["guild_id"],),
                    )
                    if job is None:
                        break

                    produced = min(remaining_capacity, job["quantity"])
                    new_quantity = job["quantity"] - produced
                    remaining_capacity -= produced

                    is_drill_scrap = job["target_drill_id"] is not None
                    if is_drill_scrap:
                        pass  # applied on completion below, never partially
                    else:
                        for material_id, per_unit in scrap_yield(job["target_id"]).items():
                            await adjust_user_quantity(
                                tx, job["user_id"], material_id, per_unit * produced
                            )

                    if new_quantity <= 0:
                        await tx.execute(
                            "UPDATE production_jobs SET status = 'complete', quantity = 0 WHERE job_id = ?",
                            (job["job_id"],),
                        )
                        if is_drill_scrap:
                            # Matching on locked_job_id makes this idempotent:
                            # a job that somehow drains twice can't delete a
                            # drill that has since been locked by something
                            # else. The delete has to come after the credit,
                            # since the join above is where drill_type came
                            # from - and if the delete matches nothing, the
                            # drill wasn't ours to scrap and the credit is
                            # rolled back with it.
                            deleted = await tx.execute_changes(
                                "DELETE FROM drills WHERE drill_id = ? AND locked_job_id = ?",
                                (job["target_drill_id"], job["job_id"]),
                            )
                            if deleted and job["drill_type"] is not None:
                                for material_id, quantity in drill_scrap_yield(job["drill_type"]).items():
                                    await adjust_user_quantity(
                                        tx, job["user_id"], material_id, quantity
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
    # bot.add_cog() auto-registers the scrapper_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(ScrapperCog(bot))
