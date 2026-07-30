"""
cogs/mining.py

Implements:
  - /mine place [drill]       - place one of your drills in this server (max 3/user/server)
  - /mine status              - show your active drills + this server's mining pool
  - /collect [here]           - empty your drills, in every server you have
                                them placed, into your inventory
  - /mine remove <drill>      - pull a drill back out early, refunding it + its contents
  - /mine attach <drill> <container> - fit a storage container, swapping any existing one
  - /mine detach <drill>      - pull a drill's container back off
  - A background loop that tops up each server's shared raw-material pool once
    per day, and another loop that has drills harvest from that pool periodically.

Mining is server-wide, not channel-scoped - there's no designated "dig site"
channel. Every server has a single raw-material pool that all of that
server's drills draw from, regardless of which channel a command is run in.

A drill is an individually tracked row for its whole life (see the drills
table in schema.sql), not a fungible stack, because its level and attached
container have to survive being unplaced. Commands therefore take a drill_id
picked from an autocomplete list rather than a drill type.

HARVEST_TICK_MINUTES is 24, giving 2.5 ticks/hour. Every drill's base
mines_per_hour is a multiple of 2.5, but a level's +1 item/hour is not, so
per-tick amounts don't come out whole - drills carry the remainder in
harvest_progress rather than rounding it away (see advance_harvest).
"""
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.responses import respond
from utils.embeds import make_embed, add_multi_field, MINING_COLOR
from utils.guild_helpers import human_member_count
from database.db import InsufficientQuantity
from utils.db_helpers import (
    ensure_user_row,
    ensure_server_row,
    get_user_quantity,
    adjust_user_quantity,
    deduct_user_quantity,
)
from utils.drills import (
    capacity_of,
    rate_of,
    collection_summary_lines,
    drill_choices,
    drill_label,
    fetch_drill,
    guild_name_map,
    container_name,
)

from data.materials import (
    DRILLS,
    RAW_MATERIALS,
    STORAGE_CONTAINERS,
    BASE_STORAGE_CAPACITY,
    MAX_DRILLS_PER_USER_PER_SERVER,
    MINING_POOL_DAILY_PER_MEMBER,
    MINING_POOL_CAP_MULTIPLIER,
    advance_harvest,
    effective_capacity,
    get_material_info,
)

HARVEST_TICK_MINUTES = 24
POOL_TOPUP_CHECK_MINUTES = 60

# The two drill sets /collect can empty, kept at module level so tests can run
# them against a real database rather than restating them. The default reaches
# every server the player has drills placed in; `guild_id IS NOT NULL` is
# implied by stored_amount > 0 (see the CHECK on drills) but stated so the
# query reads as "placed drills only", and idx_drills_owner covers it.
COLLECT_EVERYWHERE_SQL = (
    "SELECT * FROM drills WHERE owner_id = ? AND guild_id IS NOT NULL AND stored_amount > 0"
)
COLLECT_HERE_SQL = (
    "SELECT * FROM drills WHERE guild_id = ? AND owner_id = ? AND stored_amount > 0"
)


def build_material_breakdown(total_items: int, roll_material) -> dict[str, int]:
    """Rolls `total_items` individual drops and tallies them per material."""
    breakdown: dict[str, int] = {}
    for _ in range(max(0, total_items)):
        material_id = roll_material()
        breakdown[material_id] = breakdown.get(material_id, 0) + 1
    return breakdown


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class MiningCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.harvest_loop.start()
        self.pool_topup_loop.start()

    def cog_unload(self):
        self.harvest_loop.cancel()
        self.pool_topup_loop.cancel()

    mine_group = app_commands.Group(name="mine", description="Manage mining drills")

    def _roll_raw_material(self) -> str:
        roll = random.random()
        cumulative = 0.0
        for mat_id, info in RAW_MATERIALS.items():
            cumulative += info["drop_chance"]
            if roll <= cumulative:
                return mat_id
        # RAW_MATERIALS' drop_chance values sum to exactly 1.0, so this only
        # triggers on float rounding at the roll==1.0 edge - falls back to the
        # first (most common) material rather than ever returning nothing.
        return next(iter(RAW_MATERIALS))

    async def _grant_fallback_drill(self, user_id: int) -> int | None:
        """If a player owns no drills at all, give them a free iron drill
        (docs/mining.txt) and return its ID. Returns None if they already have
        one. Owning any drills row counts - placed, sitting in inventory, or
        locked in an upgrade job - so this can't be farmed by emptying your
        inventory."""
        # Checking and granting share a transaction, or two /mine place calls
        # racing would both see an empty inventory and hand out a drill each.
        async with self.db.transaction() as tx:
            await ensure_user_row(tx, user_id)

            owned = await tx.fetchone(
                "SELECT 1 AS found FROM drills WHERE owner_id = ? LIMIT 1", (user_id,)
            )
            if owned:
                return None

            return await tx.execute(
                "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (NULL, ?, 'iron_drill')",
                (user_id,),
            )

    async def _unplaced_drill_autocomplete(self, interaction: discord.Interaction, current: str):
        return await drill_choices(
            self.db, interaction.user.id, current, unplaced_only=True
        )

    async def _placed_here_autocomplete(self, interaction: discord.Interaction, current: str):
        return await drill_choices(
            self.db, interaction.user.id, current, guild_id=interaction.guild_id
        )

    async def _any_drill_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await self.db.fetchall(
            "SELECT * FROM drills WHERE owner_id = ?", (interaction.user.id,)
        )
        return await drill_choices(
            self.db, interaction.user.id, current,
            guild_names=guild_name_map(self.bot, rows),
        )

    async def _containered_drill_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await self.db.fetchall(
            "SELECT * FROM drills WHERE owner_id = ?", (interaction.user.id,)
        )
        return await drill_choices(
            self.db, interaction.user.id, current,
            require_container=True,
            guild_names=guild_name_map(self.bot, rows),
        )

    @mine_group.command(name="place", description="Place one of your drills in this server")
    @app_commands.describe(drill="Which drill to place - leave blank to place your only one")
    @app_commands.autocomplete(drill=_unplaced_drill_autocomplete)
    async def mine_place(self, interaction: discord.Interaction, drill: int | None = None):
        await ensure_server_row(self.db, interaction.guild_id)

        # Checked here as well as inside the transaction below, because the
        # free-drill grant happens in between: without this, a player already
        # at their limit would be handed a drill they then can't place.
        existing = await self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND owner_id = ?",
            (interaction.guild_id, interaction.user.id),
        )
        if existing["cnt"] >= MAX_DRILLS_PER_USER_PER_SERVER:
            await interaction.response.send_message(
                f"You already have the max of {MAX_DRILLS_PER_USER_PER_SERVER} drills in this server.",
                ephemeral=True,
            )
            return

        # `drill` is optional because a brand-new player's autocomplete list is
        # empty - a required parameter would make the command unusable for
        # exactly the people the free-drill rule exists for.
        granted = False
        if drill is None:
            new_drill_id = await self._grant_fallback_drill(interaction.user.id)
            if new_drill_id is not None:
                drill, granted = new_drill_id, True
            else:
                drill = await self._sole_unplaced_drill(interaction)
                if drill is None:
                    return

        # Re-reading the drill and claiming it share a transaction, so the same
        # drill can't be placed twice by two commands racing, and the max-drills
        # count can't be beaten by firing several at once.
        async with self.db.transaction() as tx:
            row = await fetch_drill(tx, drill, interaction.user.id)
            if row is None:
                await interaction.response.send_message("That isn't one of your drills.", ephemeral=True)
                return
            if row["guild_id"] is not None:
                await interaction.response.send_message(
                    f"**{drill_label(row)}** is already placed in a server.", ephemeral=True
                )
                return
            if row["locked_job_id"] is not None:
                await interaction.response.send_message(
                    f"**{drill_label(row)}** is being upgraded in the factory - it can't be placed until that finishes.",
                    ephemeral=True,
                )
                return

            placed_count = await tx.fetchone(
                "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND owner_id = ?",
                (interaction.guild_id, interaction.user.id),
            )
            if placed_count["cnt"] >= MAX_DRILLS_PER_USER_PER_SERVER:
                await interaction.response.send_message(
                    f"You already have the max of {MAX_DRILLS_PER_USER_PER_SERVER} drills in this server.",
                    ephemeral=True,
                )
                return

            await tx.execute(
                "UPDATE drills SET guild_id = ? WHERE drill_id = ? AND guild_id IS NULL",
                (interaction.guild_id, row["drill_id"]),
            )

        placed = DRILLS[row["drill_type"]]["name"]
        if granted:
            await respond(
                interaction, self.db,
                content=f"⛏️ You didn't have any drills, so I gave you an **{placed}** and placed it.",
            )
        else:
            await respond(
                interaction, self.db,
                content=f"⛏️ Placed **{drill_label(row)}** in this server.",
            )

    async def _sole_unplaced_drill(self, interaction: discord.Interaction) -> int | None:
        """Resolves an omitted `drill` argument when the player already owns
        drills: unambiguous if exactly one is free to place, otherwise they
        have to say which. Sends its own error and returns None if it can't."""
        candidates = await self.db.fetchall(
            "SELECT * FROM drills WHERE owner_id = ? AND guild_id IS NULL AND locked_job_id IS NULL",
            (interaction.user.id,),
        )
        if len(candidates) == 1:
            return candidates[0]["drill_id"]

        if not candidates:
            message = "You have no drills in your inventory to place. Craft one with `/factory craft`."
        else:
            listed = "\n".join(f"- {drill_label(row)}" for row in candidates)
            message = f"You have several drills to choose from - pick one with the `drill` option:\n{listed}"
        await interaction.response.send_message(message, ephemeral=True)
        return None

    @mine_group.command(name="status", description="Show your drills and this server's mining pool")
    async def mine_status(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)

        drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND owner_id = ?",
            (interaction.guild_id, interaction.user.id),
        )
        cfg = await self.db.fetchone(
            "SELECT mining_pool_remaining FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        pool_remaining = cfg["mining_pool_remaining"] if cfg else 0
        member_count = await human_member_count(interaction.guild) if interaction.guild else 0
        pool_cap = member_count * MINING_POOL_DAILY_PER_MEMBER * MINING_POOL_CAP_MULTIPLIER

        embed = make_embed("Mining Status", MINING_COLOR)
        if not drills:
            embed.add_field(name="Your Drills", value="No drills placed yet.", inline=False)
        else:
            lines = []
            for d in drills:
                info = DRILLS[d["drill_type"]]
                status = "FULL - awaiting /collect" if d["is_full"] else f"mining {rate_of(d):g}/hr"
                container = f" [{container_name(d['container_type'])}]" if d["container_type"] else ""
                lines.append(
                    f"{info['emoji']} #{d['drill_id']} {info['name']} Lv{d['level']}{container}: "
                    f"{d['stored_amount']}/{capacity_of(d)} - {status}"
                )
            add_multi_field(embed, "Your Drills", lines)

        other_drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND owner_id != ? AND is_full = 0",
            (interaction.guild_id, interaction.user.id),
        )
        if other_drills:
            counts: dict[str, int] = {}
            for d in other_drills:
                counts[d["drill_type"]] = counts.get(d["drill_type"], 0) + 1

            lines = [
                f"{DRILLS[drill_type]['emoji']} {DRILLS[drill_type]['name']}{'s' if count != 1 else ''}: {count}"
                for drill_type, count in counts.items()
            ]
            embed.add_field(name="Other Active Drills in Server", value="\n".join(lines), inline=False)

        embed.add_field(
            name="<:MiningBlock:1523436645729173514> Server Mining Pool",
            value=f"{pool_remaining}/{pool_cap} raw materials remaining",
            inline=False,
        )

        await respond(interaction, self.db, embed=embed)

    @mine_group.command(name="remove", description="Pull one of your drills out of this server and collect its items")
    @app_commands.describe(drill="Which drill to remove")
    @app_commands.autocomplete(drill=_placed_here_autocomplete)
    async def mine_remove(self, interaction: discord.Interaction, drill: int):
        row = await fetch_drill(self.db, drill, interaction.user.id)
        if row is None or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(
                "You don't have that drill placed in this server.", ephemeral=True
            )
            return
        if row["locked_job_id"] is not None:
            await interaction.response.send_message(
                f"**{drill_label(row)}** is being upgraded in the factory - it can't be removed until that finishes.",
                ephemeral=True,
            )
            return

        collected_breakdown = build_material_breakdown(row["stored_amount"], self._roll_raw_material)

        # Crediting the contents and emptying the drill commit together - the
        # drill's stored_amount is the only record of them, so a failure
        # between the two would either duplicate the haul or destroy it.
        async with self.db.transaction() as tx:
            await ensure_user_row(tx, interaction.user.id)
            for material_id, qty in collected_breakdown.items():
                await adjust_user_quantity(tx, interaction.user.id, material_id, qty)

            # Back to the inventory as the same drill, keeping its level and
            # container - that persistence is the whole reason drills are
            # tracked per instance. harvest_progress rides along too; it's a
            # property of the drill, and clearing it would only ever cost the
            # player. The guild_id guard makes this a no-op if the drill was
            # already pulled out by a racing command.
            await tx.execute(
                "UPDATE drills SET guild_id = NULL, stored_amount = 0, is_full = 0 "
                "WHERE drill_id = ? AND guild_id = ?",
                (row["drill_id"], interaction.guild_id),
            )

        embed = make_embed("Drill Removed", MINING_COLOR)
        embed.add_field(
            name="Drill",
            value=f"{DRILLS[row['drill_type']]['emoji']} **{drill_label(row)}** returned to your inventory",
            inline=False,
        )

        if collected_breakdown:
            lines = []
            for mat_id, qty in sorted(collected_breakdown.items()):
                info = get_material_info(mat_id)
                if info:
                    lines.append(f"{info['emoji']} {qty}x {info['name']}")
            embed.add_field(name="Items Collected", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Items Collected", value="None", inline=False)

        await respond(interaction, self.db, embed=embed)

    @staticmethod
    async def _set_container(db, drill_row, container_type: str | None):
        """Fits or removes a container and re-derives is_full against the new
        capacity in the same statement.

        That recompute is not optional. The harvest loop only ever selects
        drills with is_full = 0, so a full drill that gains a container would
        never mine again; and a drill that loses one while holding more than
        its bare capacity would sit in the loop's result set forever, doing
        nothing while /mine status called it "mining".

        The container_type guard makes the swap safe to race: if another
        command changed the container first, this matches nothing and its
        transaction rolls back rather than returning the wrong item."""
        return await db.execute_changes(
            "UPDATE drills SET container_type = ?, "
            "is_full = CASE WHEN stored_amount >= ? THEN 1 ELSE 0 END "
            "WHERE drill_id = ? AND container_type IS ?",
            (
                container_type,
                effective_capacity(container_type),
                drill_row["drill_id"],
                drill_row["container_type"],
            ),
        )

    @mine_group.command(name="attach", description="Fit a storage container to one of your drills")
    @app_commands.describe(drill="Which drill to fit", container="Which container to fit")
    @app_commands.autocomplete(drill=_any_drill_autocomplete)
    @app_commands.choices(container=[
        app_commands.Choice(name=info["name"], value=key) for key, info in STORAGE_CONTAINERS.items()
    ])
    async def mine_attach(
        self,
        interaction: discord.Interaction,
        drill: int,
        container: app_commands.Choice[str],
    ):
        row = await fetch_drill(self.db, drill, interaction.user.id)
        if row is None:
            await interaction.response.send_message("That isn't one of your drills.", ephemeral=True)
            return
        if row["locked_job_id"] is not None:
            await interaction.response.send_message(
                f"**{drill_label(row)}** is being upgraded in the factory - fit the container once that finishes.",
                ephemeral=True,
            )
            return
        if row["container_type"] == container.value:
            await interaction.response.send_message(
                f"**{drill_label(row)}** already has a {container.name} fitted.", ephemeral=True
            )
            return

        previous = row["container_type"]

        # Consuming the new container, returning the old one and fitting it to
        # the drill all commit together. Without that, a failure between the
        # first two writes destroys a container outright - the item is gone
        # from the inventory and never came back.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                have = await get_user_quantity(tx, interaction.user.id, container.value)
                if have < 1:
                    await interaction.response.send_message(
                        f"You don't have a **{container.name}**. Craft one with `/factory craft`.",
                        ephemeral=True,
                    )
                    return

                if not await self._set_container(tx, row, container.value):
                    await interaction.response.send_message(
                        "That drill's container changed while this was going through. Try again.",
                        ephemeral=True,
                    )
                    return
                await deduct_user_quantity(tx, interaction.user.id, container.value, 1)
                if previous:
                    # Swapped out intact rather than destroyed - containers are
                    # ordinary fungible items with no per-instance state to lose.
                    await adjust_user_quantity(tx, interaction.user.id, previous, 1)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your containers changed while that was going through - nothing was fitted. Try again.",
                ephemeral=True,
            )
            return

        old_capacity = capacity_of(row)
        new_capacity = effective_capacity(container.value)

        embed = make_embed("Container Fitted", MINING_COLOR)
        embed.description = (
            f"{STORAGE_CONTAINERS[container.value]['emoji']} Fitted a **{container.name}** to "
            f"**#{row['drill_id']} {DRILLS[row['drill_type']]['name']}**."
        )
        embed.add_field(name="Storage", value=f"{old_capacity} → **{new_capacity}**", inline=False)
        if previous:
            embed.add_field(
                name="Swapped Out",
                value=f"{STORAGE_CONTAINERS[previous]['emoji']} **{container_name(previous)}** returned to your inventory",
                inline=False,
            )
        await respond(interaction, self.db, embed=embed)

    @mine_group.command(name="detach", description="Pull the storage container off one of your drills")
    @app_commands.describe(drill="Which drill to pull the container from")
    @app_commands.autocomplete(drill=_containered_drill_autocomplete)
    async def mine_detach(self, interaction: discord.Interaction, drill: int):
        row = await fetch_drill(self.db, drill, interaction.user.id)
        if row is None:
            await interaction.response.send_message("That isn't one of your drills.", ephemeral=True)
            return
        if row["container_type"] is None:
            await interaction.response.send_message(
                f"**{drill_label(row)}** has no container fitted.", ephemeral=True
            )
            return
        if row["locked_job_id"] is not None:
            await interaction.response.send_message(
                f"**{drill_label(row)}** is being upgraded in the factory - pull the container once that finishes.",
                ephemeral=True,
            )
            return

        removed = row["container_type"]
        old_capacity = capacity_of(row)

        async with self.db.transaction() as tx:
            await ensure_user_row(tx, interaction.user.id)
            # Pull it off first: if another command got there already this
            # matches nothing, and the refund below never happens - otherwise
            # racing detaches would each hand back a copy of the same one.
            if not await self._set_container(tx, row, None):
                await interaction.response.send_message(
                    "That drill's container changed while this was going through. Try again.",
                    ephemeral=True,
                )
                return
            await adjust_user_quantity(tx, interaction.user.id, removed, 1)

        embed = make_embed("Container Removed", MINING_COLOR)
        embed.description = (
            f"{STORAGE_CONTAINERS[removed]['emoji']} Pulled the **{container_name(removed)}** off "
            f"**#{row['drill_id']} {DRILLS[row['drill_type']]['name']}** and returned it to your inventory."
        )
        embed.add_field(
            name="Storage",
            value=f"{old_capacity} → **{BASE_STORAGE_CAPACITY}**",
            inline=False,
        )
        if row["stored_amount"] > BASE_STORAGE_CAPACITY:
            # Nothing is lost - the drill just stops until /collect drains it
            # back under the smaller capacity.
            embed.add_field(
                name="Note",
                value=(
                    f"This drill is holding {row['stored_amount']} items, over its new capacity. "
                    f"Nothing is lost, but it won't mine again until you `/collect`."
                ),
                inline=False,
            )
        await respond(interaction, self.db, embed=embed)

    @app_commands.command(
        name="collect",
        description="Collect materials from your drills in every server you have them placed",
    )
    @app_commands.describe(here="Only collect from your drills in this server (default: everywhere)")
    async def collect(self, interaction: discord.Interaction, here: bool = False):
        """Empties every drill this player has placed, in every server, into
        their inventory at once - user_materials is global, so where a drill sat
        never affected where its haul landed, and making the player run this
        once per server was friction with nothing behind it. Drills in servers
        the bot has since left are collected too: they kept mining, and the
        material is already the player's.

        `here` limits it to the current server, for a player who only wants to
        bank what's in front of them."""
        collected_breakdown: dict[str, int] = {}
        total_collected = 0

        # Emptying the drills and crediting what came out commit together, and
        # each drill is emptied with its stored_amount in the WHERE clause so
        # two /collect calls racing can't both bank the same haul: whichever
        # commits second matches nothing and rolls its own read back.
        async with self.db.transaction() as tx:
            if here:
                drills = await tx.fetchall(
                    COLLECT_HERE_SQL, (interaction.guild_id, interaction.user.id)
                )
            else:
                drills = await tx.fetchall(COLLECT_EVERYWHERE_SQL, (interaction.user.id,))
            if not drills:
                await interaction.response.send_message(
                    "You have no drills with materials to collect here."
                    if here
                    else "You have no drills with materials to collect. "
                         "`/mine place` puts one to work if you haven't got one going yet.",
                    ephemeral=True,
                )
                return

            await ensure_user_row(tx, interaction.user.id)

            # One (guild_id, items) pair per drill actually emptied, so the
            # per-server summary below counts only what was really banked.
            hauls: list[tuple[int, int]] = []
            for d in drills:
                changed = await tx.execute_changes(
                    "UPDATE drills SET stored_amount = 0, is_full = 0 "
                    "WHERE drill_id = ? AND stored_amount = ?",
                    (d["drill_id"], d["stored_amount"]),
                )
                if not changed:
                    continue
                hauls.append((d["guild_id"], d["stored_amount"]))
                total_collected += d["stored_amount"]
                drill_breakdown = build_material_breakdown(d["stored_amount"], self._roll_raw_material)
                for material_id, qty in drill_breakdown.items():
                    collected_breakdown[material_id] = collected_breakdown.get(material_id, 0) + qty

            for material_id, qty in collected_breakdown.items():
                await adjust_user_quantity(tx, interaction.user.id, material_id, qty)

        # Naming the servers reads the gateway cache, so it happens after the
        # transaction has committed - see the note on Database.transaction.
        server_count = len({guild_id for guild_id, _ in hauls})

        embed = make_embed("Collection Complete", MINING_COLOR)
        embed.description = (
            f"📦 Collected **{total_collected}** raw materials from **{len(hauls)}** drill(s)"
        )
        if server_count > 1:
            embed.description += f" across **{server_count}** servers"

        lines = []
        for mat_id in sorted(collected_breakdown.keys()):
            qty = collected_breakdown[mat_id]
            info = get_material_info(mat_id)
            if info:
                lines.append(f"{info['emoji']} {qty}x {info['name']}")

        if lines:
            add_multi_field(embed, "Materials", lines)

        if server_count > 1:
            guild_names = guild_name_map(self.bot, drills)
            add_multi_field(
                embed,
                "By Server",
                collection_summary_lines(
                    hauls, guild_names, current_guild_id=interaction.guild_id
                ),
            )

        await respond(interaction, self.db, embed=embed)

    @tasks.loop(minutes=POOL_TOPUP_CHECK_MINUTES)
    async def pool_topup_loop(self):
        """Checks every server the bot is in once an hour; if that server's UTC
        calendar date has changed since its last top-up, adds
        (human_member_count * MINING_POOL_DAILY_PER_MEMBER) to its pool,
        capped at MINING_POOL_CAP_MULTIPLIER times that daily amount. Bots
        are excluded from the count (utils/guild_helpers.py) the same way
        the market's target stock excludes them."""
        today = _today()
        for guild in self.bot.guilds:
            await ensure_server_row(self.db, guild.id)

            # Counting members can chunk the guild over the gateway, so it
            # happens before the transaction is opened - see the note on
            # Database.transaction about not awaiting Discord under the lock.
            daily_amount = await human_member_count(guild) * MINING_POOL_DAILY_PER_MEMBER
            cap = daily_amount * MINING_POOL_CAP_MULTIPLIER

            # The top-up writes an absolute figure derived from what it read,
            # so it has to read and write atomically: a drill harvesting in
            # between would have its take silently restored, minting raw
            # materials back into the pool. The last_topup guard in the WHERE
            # clause also makes the once-a-day rule safe against two ticks
            # overlapping.
            async with self.db.transaction() as tx:
                cfg = await tx.fetchone(
                    "SELECT mining_pool_remaining, mining_pool_last_topup FROM server_config WHERE guild_id = ?",
                    (guild.id,),
                )
                if cfg["mining_pool_last_topup"] == today:
                    continue  # already topped up today

                new_remaining = min(cfg["mining_pool_remaining"] + daily_amount, cap)
                await tx.execute(
                    "UPDATE server_config SET mining_pool_remaining = ?, mining_pool_last_topup = ? "
                    "WHERE guild_id = ? AND mining_pool_last_topup IS NOT ?",
                    (new_remaining, today, guild.id, today),
                )

    @pool_topup_loop.before_loop
    async def before_pool_topup_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=HARVEST_TICK_MINUTES)
    async def harvest_loop(self):
        """Every tick, each placed non-full drill pulls a tick's share of its
        hourly rate from its server's mining pool, filling up to its capacity
        and then marking itself full."""
        ticks_per_hour = 60 / HARVEST_TICK_MINUTES
        drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE is_full = 0 AND guild_id IS NOT NULL"
        )
        for d in drills:
            # One transaction per drill: the pool is shared by every drill in
            # the server and by /market, so reading what's left and taking a
            # share of it has to be atomic. Otherwise two drills can both read
            # the same remainder and between them mine more than the pool held,
            # driving it negative and inventing raw materials from nothing.
            async with self.db.transaction() as tx:
                cfg = await tx.fetchone(
                    "SELECT mining_pool_remaining FROM server_config WHERE guild_id = ?",
                    (d["guild_id"],),
                )
                pool_remaining = cfg["mining_pool_remaining"] if cfg else 0
                # Bail out before touching harvest_progress: a drill shouldn't
                # bank credit for hours it couldn't have mined anyway, or it
                # would dump the whole backlog the moment the pool tops up.
                if pool_remaining <= 0:
                    continue

                # Re-read rather than trusting the row from the batch select:
                # a /collect or /mine attach may have landed since then.
                current = await tx.fetchone(
                    "SELECT * FROM drills WHERE drill_id = ?", (d["drill_id"],)
                )
                if current is None or current["guild_id"] is None or current["is_full"]:
                    continue

                capacity = capacity_of(current)
                space_left = capacity - current["stored_amount"]
                if space_left <= 0:
                    # Already at or past capacity - a container was pulled off,
                    # or the drill predates the flat base capacity. Flag it so
                    # the loop stops picking up a row it can never act on.
                    await tx.execute(
                        "UPDATE drills SET is_full = 1 WHERE drill_id = ?", (current["drill_id"],)
                    )
                    continue

                amount, carry = advance_harvest(
                    current["harvest_progress"], rate_of(current), ticks_per_hour
                )
                harvested = min(amount, space_left, pool_remaining)
                new_stored = current["stored_amount"] + harvested

                # The carry is written even when harvested is 0, so a drill
                # that mines less than one item per tick still accumulates
                # toward one instead of resetting every tick. Any whole items
                # lost to the space_left/pool_remaining clamp are dropped
                # rather than banked, matching how the pool has always been
                # first-come-first-served.
                await tx.execute(
                    "UPDATE drills SET stored_amount = ?, is_full = ?, harvest_progress = ? WHERE drill_id = ?",
                    (new_stored, 1 if new_stored >= capacity else 0, carry, current["drill_id"]),
                )
                if harvested > 0:
                    await tx.execute(
                        "UPDATE server_config SET mining_pool_remaining = mining_pool_remaining - ? WHERE guild_id = ?",
                        (harvested, current["guild_id"]),
                    )

    @harvest_loop.before_loop
    async def before_harvest_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the mine_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(MiningCog(bot))
