"""
cogs/mining.py

Implements:
  - /mine place [drill]       - place one of your drills in this server, up to the
                                number of mining slots that server has unlocked
  - /mine status              - show your active drills + this server's mining pool
  - /collect [here]           - empty your drills, in every server you have
                                them placed, into your inventory
  - /mine remove <drill>      - pull a drill back out early, refunding it + its contents
  - /mine attach <drill> <container> - fit a storage container, swapping any existing one
  - /mine detach <drill>      - pull a drill's container back off
  - /focus [focus]            - commit your mining to one ore (costs a Ruby)
  - /efficiency [efficiency]  - double the raw materials one smelted recipe
                                needs, and trim their ratio (costs an Obsidian)
  - A background loop that has drills harvest from their server's pool.

Mining is server-wide, not channel-scoped - there's no designated "dig site"
channel. Every server has a single raw-material pool that all of that
server's drills draw from, regardless of which channel a command is run in.

A drill is an individually tracked row for its whole life (see the drills
table in schema.sql), not a fungible stack, because its level and attached
container have to survive being unplaced. Commands therefore take a drill_id
picked from an autocomplete list rather than a drill type.

HARVEST_TICK_MINUTES is 5, giving 12 ticks/hour - matching the other three
machines' PROCESS_TICK_MINUTES. A drill's rate comes from its type and is
scaled by its level (see LEVEL_RATE_ANCHOR), so a tick's share of it is
generally a fraction of an item rather than a whole number - drills carry the
remainder in harvest_progress rather than rounding it away (see
advance_harvest). A tick pays for the time since drills.mined_until, never
more than one tick, so a drill that only started mining since the last tick -
placed, or freed by a /collect or a container - gets only what it has mined.

Bumped from 24 (2.5 ticks/hour) as of the 1.2.1 drill speed buff: at 24
minutes, a drill's own base 100-item capacity fills inside a single tick once
its effective rate passes 250/hour, which the buffed Diamond Drill (120/hour
at level 1) now reaches at level 7 - a level worth evaluating progression at,
where before the buff (15/hour) the same threshold sat at level 80. Filling
inside one tick isn't incorrect (space_left still clamps the take), it just
means the drill jumps straight to FULL with no visible progress in between.
At 5 minutes that threshold moves back out to level 46 for Diamond.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.responses import respond
from utils.embeds import make_embed, add_multi_field, footer_with, MINING_COLOR
from utils.formatting import format_currency
from utils.job_board import job_board_today, next_reset
from utils.government import guilds_with_bonanza
from database.db import InsufficientQuantity
from utils.db_helpers import (
    clock_now,
    elapsed_work_hours,
    bonanza_active,
    sqlite_timestamp,
    ensure_user_row,
    ensure_server_row,
    get_user_quantity,
    adjust_currency_balance,
    adjust_user_quantity,
    deduct_user_quantity,
    mining_slot_status,
    mining_slots_full_message,
)
from utils.drills import (
    DrillScope,
    DRILL_AVAILABLE_SQL,
    add_drill_contents,
    take_drill_contents,
    capacity_of,
    rate_of,
    drill_cell,
    drill_choices,
    drill_label,
    drill_unavailable_message,
    drill_short_label,
    fetch_drill,
    container_name,
    is_local_drill,
    material_breakdown_lines,
    retract_drill,
    set_container,
)

from utils.mining_focus import convert_haul, focus_label, get_focus, set_focus
from utils.mining_efficiency import (
    boost_haul,
    efficiency_label,
    get_efficiency,
    set_efficiency,
)
from utils.mining_affinity import (
    convert_gems,
    get_affinity,
    affinity_label,
    affinity_progress,
    set_affinity,
)
from utils.mining_pool import pool_contents, pool_display_lines, take_from_pool
from utils.production_ledger import record_mined, split_by_guild

from data.materials import (
    BONANZA_SPEED_MULTIPLIER,
    player_price_total,
    DEFAULT_MINING_EFFICIENCY,
    DEFAULT_MINING_FOCUS,
    DEFAULT_MINING_AFFINITY,
    DRILLS,
    GEMSTONES,
    ORES,
    MARKET_PRICE_CENTS,
    MINING_EFFICIENCIES,
    MINING_EFFICIENCY_UNLOCK_COST,
    MINING_FOCUSES,
    material_name,
    MINING_FOCUS_UNLOCK_COST,
    MINING_AFFINITIES,
    MINING_AFFINITY_UNLOCK_COST,
    STORAGE_CONTAINERS,
    BASE_STORAGE_CAPACITY,
    advance_harvest,
    effective_capacity,
    get_material_info,
    recipe_true_inputs,
)
from data.emoji import MINING_POOL_EMOJI

log = logging.getLogger("dragonhoard")

HARVEST_TICK_MINUTES = 5

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

# Empties one drill /collect selected, guarded on the stored_amount it read so a
# racing command can't pay the same haul out twice. A full drill has been
# stopped and starts again now, so its clock restarts too - otherwise its next
# harvest tick would pay for time it spent full (drills.mined_until). One that
# wasn't full never stopped, and keeps its clock. SQLite evaluates every SET
# against the row as it was, so is_full in the CASE is the old value. At module
# level for the same reason as the two queries above.
COLLECT_EMPTY_DRILL_SQL = (
    "UPDATE drills SET stored_amount = 0, is_full = 0, "
    "mined_until = CASE WHEN is_full = 1 THEN datetime('now') ELSE mined_until END "
    "WHERE drill_id = ? AND stored_amount = ?"
)

# /mine status groups "Your Drills" by material, best first - the reverse of
# DRILLS' own iron-to-diamond declaration order, which follows the crafting
# ladder rather than what a player wants to see at the top of their list.
DRILL_STATUS_ORDER = {
    drill_type: rank
    for rank, drill_type in enumerate(reversed(DRILLS))
}


def unlock_footer(cost: dict[str, int]) -> str:
    """The footer on a just-unlocked Mining Focus, Mining Efficiency or Mining
    Affinity embed, saying what the unlock cost.

    The gemstone is NAMED here, unlike everywhere else in this cog, which shows
    it as its emoji: footer text does not render emoji. Naming it rather than
    dropping it keeps the line worth having - all three unlocks are
    gemstone-gated and which gem it took is the whole point of saying anything.

    Shared by all three commands so that rule lives in one place rather than in
    three branches that happened to be written the same way.
    """
    spent = ", ".join(
        f"{quantity} {material_name(get_material_info(material_id), quantity)}"
        for material_id, quantity in cost.items()
    )
    return footer_with(f"unlocked for {spent}")


class MiningCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._now = clock_now
        self.harvest_loop.start()

    def cog_unload(self):
        self.harvest_loop.cancel()

    mine_group = app_commands.Group(name="mine", description="Manage mining drills")

    async def _retract_guild_drills(self, guild_id: int) -> int:
        """Pulls every drill placed in a server back to its owner's inventory,
        crediting whatever it was holding. Called when the bot is removed from
        that server: those drills can't mine any more, and leaving them placed
        would strand them somewhere their owner can no longer run a command.

        The materials are credited rather than dropped for the same reason
        /mine remove credits them - the drill already mined them, and they're
        the player's. It also isn't optional: the drills table CHECKs that an
        unplaced drill holds nothing, so the contents have to go somewhere.

        Level, container and harvest_progress ride along untouched, so a
        re-invited server's players get their drills back exactly as they were.
        Returns how many were retracted.
        """
        rows = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ?", (guild_id,)
        )
        retracted = 0
        for row in rows:
            # One transaction per drill. retract_drill returns None when a
            # racing /collect or /mine remove already emptied this one, which is
            # simply not something to count.
            async with self.db.transaction() as tx:
                if await retract_drill(tx, row) is not None:
                    retracted += 1
        return retracted

    async def _withdraw_guild_market(self, guild_id: int) -> int:
        """Cancels every open listing and order in a server and returns what
        they were holding. Called alongside _retract_guild_drills when the bot
        is removed.

        Same reasoning as the drills: escrow is real goods and real currency
        held against a trade, and a server nobody can run a command in is a
        server where that trade can never happen or be withdrawn. Leaving the
        rows would strand a stack of ore and a pile of currency somewhere their
        owner can no longer reach either.

        Balances themselves are deliberately NOT returned or cleared - they are
        kept exactly as they are so a re-invite restores them untouched
        (see _set_guild_presence). Escrow is different because it is not a
        balance: it is currency that has already left one, and a departed
        server would leave it belonging to nobody.

        Returns how many rows were withdrawn.
        """
        withdrawn = 0
        async with self.db.transaction() as tx:
            listings = await tx.fetchall(
                "SELECT * FROM market_listings WHERE guild_id = ?", (guild_id,)
            )
            for row in listings:
                if row["drill_id"] is not None:
                    await tx.execute(
                        "UPDATE drills SET listed_id = NULL WHERE drill_id = ? AND listed_id = ?",
                        (row["drill_id"], row["listing_id"]),
                    )
                else:
                    await adjust_user_quantity(
                        tx, row["seller_id"], row["material_id"], row["quantity"]
                    )
                withdrawn += 1

            orders = await tx.fetchall(
                "SELECT * FROM market_orders WHERE guild_id = ?", (guild_id,)
            )
            for row in orders:
                # Not a mint: this currency was escrowed, never burned, so
                # returning it restores a balance rather than creating one.
                await adjust_currency_balance(
                    tx, guild_id, row["buyer_id"],
                    player_price_total(row["price_units"], row["quantity"]),
                )
                withdrawn += 1

            await tx.execute("DELETE FROM market_listings WHERE guild_id = ?", (guild_id,))
            await tx.execute("DELETE FROM market_orders WHERE guild_id = ?", (guild_id,))
        return withdrawn

    async def _set_guild_presence(self, guild_id: int, present: bool):
        await ensure_server_row(self.db, guild_id)
        await self.db.execute(
            "UPDATE server_config SET bot_present = ? WHERE guild_id = ?",
            (1 if present else 0, guild_id),
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        retracted = await self._retract_guild_drills(guild.id)
        withdrawn = await self._withdraw_guild_market(guild.id)
        await self._set_guild_presence(guild.id, False)
        log.info(
            "Removed from guild %s - retracted %d drill(s), withdrew %d market row(s).",
            guild.id, retracted, withdrawn,
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        # Balances, market stock and the mining pool were never deleted, so
        # this is all a re-invite needs to put the server back in play.
        await self._set_guild_presence(guild.id, True)

    @commands.Cog.listener()
    async def on_ready(self):
        """Reconciles stored presence against the servers the bot is actually
        in. This is what makes a removal that happened while the bot was OFFLINE
        get cleaned up at all - on_guild_remove never fires for those, so
        without this their drills would keep sitting in a server the bot left
        and their currency would keep showing up in /balance forever.

        Idempotent, because on_ready fires again on every reconnect - which is
        why the present servers are marked in one transaction rather than two
        statements each: a reconnect is routine, and this used to be the
        largest burst of writes the bot ever made at once."""
        present_ids = {guild.id for guild in self.bot.guilds}
        known = {
            row["guild_id"]
            for row in await self.db.fetchall("SELECT guild_id FROM server_config")
        }
        async with self.db.transaction() as tx:
            # A server joined while offline has no row yet, and the row has to
            # exist before the UPDATE below can mark it.
            for guild_id in present_ids - known:
                await ensure_server_row(tx, guild_id)
            if present_ids:
                placeholders = ",".join("?" * len(present_ids))
                await tx.execute(
                    f"UPDATE server_config SET bot_present = 1 WHERE guild_id IN ({placeholders})",
                    tuple(present_ids),
                )

        stale = await self.db.fetchall(
            "SELECT guild_id FROM server_config WHERE bot_present = 1"
        )
        for row in stale:
            if row["guild_id"] in present_ids:
                continue
            retracted = await self._retract_guild_drills(row["guild_id"])
            await self._set_guild_presence(row["guild_id"], False)
            log.info(
                "Guild %s was left while offline - retracted %d drill(s).",
                row["guild_id"], retracted,
            )

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
            self.db, interaction.user.id, current, scope=DrillScope.UNPLACED
        )

    async def _placed_here_autocomplete(self, interaction: discord.Interaction, current: str):
        return await drill_choices(
            self.db, interaction.user.id, current,
            scope=DrillScope.PLACED_HERE, guild_id=interaction.guild_id
        )

    async def _local_drill_autocomplete(self, interaction: discord.Interaction, current: str):
        """Drills you can fit a container to from here: the ones in your
        inventory, plus the ones you have placed in THIS server.

        Not every drill you own. A container is a physical thing being bolted
        onto a machine, and a machine standing in another server isn't somewhere
        you can reach from this one - see is_local_drill, which is what actually
        enforces that."""
        return await drill_choices(
            self.db, interaction.user.id, current,
            scope=DrillScope.LOCAL, guild_id=interaction.guild_id,
            bot=self.bot,
        )

    async def _local_containered_drill_autocomplete(self, interaction: discord.Interaction, current: str):
        return await drill_choices(
            self.db, interaction.user.id, current,
            scope=DrillScope.LOCAL, guild_id=interaction.guild_id,
            require_container=True,
            bot=self.bot,
        )

    async def _owned_container_autocomplete(self, interaction: discord.Interaction, current: str):
        """Only container types the player actually has at least one of. A
        static choice list would offer all five regardless of inventory and
        let someone pick one /mine attach was always going to refuse for want
        of the item - this is the same "don't offer what you can't act on"
        rule _scrappable_drill_autocomplete follows for drills."""
        rows = await self.db.fetchall(
            "SELECT material_id FROM user_materials WHERE user_id = ? "
            f"AND material_id IN ({','.join('?' * len(STORAGE_CONTAINERS))}) AND quantity > 0",
            (interaction.user.id, *STORAGE_CONTAINERS),
        )
        owned = {row["material_id"] for row in rows}
        search = current.strip().lower()
        return [
            app_commands.Choice(name=info["name"], value=key)
            for key, info in STORAGE_CONTAINERS.items()
            if key in owned and search in info["name"].lower()
        ]

    @mine_group.command(name="place", description="Place one of your drills in this server")
    @app_commands.describe(drill="Which drill to place - leave blank to place your only one")
    @app_commands.autocomplete(drill=_unplaced_drill_autocomplete)
    async def mine_place(self, interaction: discord.Interaction, drill: int | None = None):
        await ensure_server_row(self.db, interaction.guild_id)

        # Read once and reused by both rejections below. Fetching it inside the
        # transaction would open a second read while that transaction holds the
        # write lock, for a value no part of the placement can change.
        emoji_row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        currency_emoji = emoji_row["currency_emoji"] if emoji_row else None

        # Checked here as well as inside the transaction below, because the
        # free-drill grant happens in between: without this, a player already
        # at their limit would be handed a drill they then can't place.
        existing = await self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND owner_id = ?",
            (interaction.guild_id, interaction.user.id),
        )
        slots = await mining_slot_status(self.db, interaction.guild_id)
        if existing["cnt"] >= slots.slots:
            await interaction.response.send_message(
                mining_slots_full_message(slots, currency_emoji), ephemeral=True
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
            unavailable = drill_unavailable_message(row, "place it")
            if unavailable is not None:
                await interaction.response.send_message(unavailable, ephemeral=True)
                return

            # Re-read inside the transaction rather than reusing the figure from
            # above: a fee banked in between can only ever RAISE the cap, but
            # the drill count it is compared against is exactly what two
            # commands racing would both read stale.
            slots = await mining_slot_status(tx, interaction.guild_id)
            placed_count = await tx.fetchone(
                "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND owner_id = ?",
                (interaction.guild_id, interaction.user.id),
            )
            if placed_count["cnt"] >= slots.slots:
                await interaction.response.send_message(
                    mining_slots_full_message(slots, currency_emoji), ephemeral=True
                )
                return

            # Mining starts now, so its first tick pays for the time since
            # and not a whole tick (drills.mined_until). placed_at is what
            # voting eligibility is measured against (utils/government.py:
            # can_vote).
            await tx.execute(
                "UPDATE drills SET guild_id = ?, mined_until = datetime('now'), "
                "placed_at = datetime('now') WHERE drill_id = ? AND guild_id IS NULL",
                (interaction.guild_id, row["drill_id"]),
            )

            if granted:
                # A brand-new player's free drill starts full rather than
                # empty, so their very next command can be /collect instead of
                # a wait. Drawn from this server's own pool rather than
                # invented, since a drill's contents are supposed to be real
                # materials taken from there - anything else risks minting a
                # second copy of whatever gemstone the pool is guaranteeing
                # (docs/mining.txt).
                capacity = capacity_of(row)
                drawn = await take_from_pool(tx, interaction.guild_id, capacity)
                harvested = sum(drawn.values())
                await tx.execute(
                    "UPDATE drills SET stored_amount = ?, is_full = ? WHERE drill_id = ?",
                    (harvested, 1 if harvested >= capacity else 0, row["drill_id"]),
                )
                await add_drill_contents(tx, row["drill_id"], drawn)

        placed = DRILLS[row["drill_type"]]["name"]
        if granted:
            await respond(
                interaction, self.db,
                content=(
                    f"⛏️ You didn't have any drills, so I gave you an **{placed}**, filled "
                    f"it, and placed it. Run `/collect` to bank what's in it."
                ),
            )
        else:
            await respond(
                interaction, self.db,
                content=f"⛏️ Placed **{drill_short_label(row)}** in this server.",
            )

    async def _sole_unplaced_drill(self, interaction: discord.Interaction) -> int | None:
        """Resolves an omitted `drill` argument when the player already owns
        drills: unambiguous if exactly one is free to place, otherwise they
        have to say which. Sends its own error and returns None if it can't."""
        candidates = await self.db.fetchall(
            "SELECT * FROM drills WHERE owner_id = ? AND guild_id IS NULL "
            f"AND {DRILL_AVAILABLE_SQL}",
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
            "SELECT mining_pool_remaining, currency_emoji, bonanza_until FROM server_config "
            "WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        pool_remaining = cfg["mining_pool_remaining"] if cfg else 0
        currency_emoji = cfg["currency_emoji"] if cfg else None
        # A running Server Bonanza doubles every drill here, so every rate
        # below is quoted at the speed the harvest loop is actually paying.
        boost = BONANZA_SPEED_MULTIPLIER if cfg and bonanza_active(cfg["bonanza_until"]) else 1
        slots = await mining_slot_status(self.db, interaction.guild_id)
        contents = await pool_contents(self.db, interaction.guild_id)

        embed = make_embed("Mining Status", MINING_COLOR)

        # The second of the three places an affinity is shown, and the only one
        # that shows all three enhancements together - which is the point of
        # the field. Each is listed only once its own gem has been paid, so a
        # player who has bought none sees no field at all and a player who has
        # bought one sees one line, rather than two rows of "None" advertising
        # features they can't reach yet. The unlock embeds are where they are
        # sold; this is where they are checked.
        enhancements = []
        focus_id, _, _, focus_unlocked = await get_focus(self.db, interaction.user.id)
        if focus_unlocked:
            enhancements.append(f"Focus · {focus_label(focus_id)}")
        efficiency_id, _, efficiency_unlocked = await get_efficiency(
            self.db, interaction.user.id
        )
        if efficiency_unlocked:
            enhancements.append(f"Efficiency · {efficiency_label(efficiency_id)}")
        affinity_id, affinity_carry, _, affinity_unlocked = await get_affinity(
            self.db, interaction.user.id
        )
        if affinity_unlocked:
            line = f"Affinity · {affinity_label(affinity_id)}"
            progress = affinity_progress(affinity_id, affinity_carry)
            if progress:
                line += f"\n{progress}"
            enhancements.append(line)
        if enhancements:
            embed.add_field(
                name="Mining Enhancements", value="\n".join(enhancements), inline=False
            )

        if not drills:
            embed.add_field(name="Your Drills", value="No drills placed yet.", inline=False)
        else:
            # Same compact prefix /inventory uses, plus the two things that only
            # mean anything for a drill that's actually in the ground: how full
            # it is, and whether it's still working. Grouped by drill material,
            # best first (diamond down to iron), and within a material the
            # most-leveled drill leads - the two things a player actually
            # scans this list for.
            drills = sorted(
                drills,
                key=lambda d: (DRILL_STATUS_ORDER[d["drill_type"]], -d["level"]),
            )
            lines = []
            for d in drills:
                status = "FULL - awaiting /collect" if d["is_full"] else f"mining {rate_of(d) * boost:g}/hr"
                lines.append(
                    f"{drill_cell(d)} · {d['stored_amount']:,}/{capacity_of(d):,} · {status}"
                )
            add_multi_field(embed, "Your Drills", lines)

        # Directly under the drill list, because the number that list is
        # allowed to reach is the only reason a player looks for it. Both lines
        # are facts like everything else in this embed - the server's actual
        # mining slot progress, against what the next slot actually costs.
        embed.add_field(
            name="Mining Slots",
            value=(
                f"**{len(drills):,} / {slots.slots:,}** used\n"
                f"{format_currency(slots.progress, currency_emoji)} / "
                f"{format_currency(slots.next_threshold, currency_emoji)}"
            ),
            inline=False,
        )

        # The whole server's throughput, the viewer's own drills included -
        # it's what decides how fast the shared pool actually drains, not
        # just what everyone else is contributing. A full drill has stopped
        # mining until /collect empties it, so it's excluded here too.
        active_drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND is_full = 0",
            (interaction.guild_id,),
        )
        if active_drills:
            total_rate = sum(rate_of(d) for d in active_drills) * boost

            counts: dict[str, int] = {}
            for d in active_drills:
                counts[d["drill_type"]] = counts.get(d["drill_type"], 0) + 1
            # Same "{emoji} {count}" cell /inventory uses for stacked
            # materials, one per drill type rather than one line per type.
            # Fastest to slowest, same ordering as "Your Drills" above.
            cells = [
                f"{DRILLS[drill_type]['emoji']} {count}"
                for drill_type, count in sorted(
                    counts.items(), key=lambda item: DRILL_STATUS_ORDER[item[0]]
                )
            ]

            embed.add_field(
                name="Server Mining Speed" + (" · 🎉 Bonanza x2" if boost > 1 else ""),
                value=f"{round(total_rate * 24):,}/day\n" + " ".join(cells),
                inline=False,
            )

        # Every line here is a FACT read from the database. There is no
        # forecast field any more and no estimate anywhere in this embed,
        # because with a real bag there is nothing left to predict - the
        # gemstones are either in it or they are not.
        #
        # The version this replaced had both kinds of number side by side and
        # unlabelled: ore counts were real while gemstone lines were projected
        # from an accrual rate, and the gemstone line silently changed from a
        # prediction into a statement of fact depending on whether a gem
        # happened to be in the pool that moment. Removing the daily top-up
        # removed the accrual rate, and with it the only thing there was to
        # estimate.
        embed.add_field(
            name=f"{MINING_POOL_EMOJI} Server Mining Pool",
            value="\n".join(pool_display_lines(pool_remaining, contents)),
            inline=False,
        )

        await respond(interaction, self.db, embed=embed)

    @mine_group.command(name="remove", description="Pull one of your drills out of this server and collect its items")
    @app_commands.describe(drill="Which drill to remove")
    @app_commands.autocomplete(drill=_placed_here_autocomplete)
    async def mine_remove(self, interaction: discord.Interaction, drill: int):
        # The drill is read INSIDE the transaction, so the stored_amount that
        # decides the haul is the one retract_drill then guards its UPDATE on.
        # Reading it outside is how this used to hand the same haul out twice:
        # a /collect committing in between zeroed the drill, and the unplace -
        # which guarded only on guild_id - still matched and credited a
        # breakdown built from the pre-collect amount.
        async with self.db.transaction() as tx:
            row = await fetch_drill(tx, drill, interaction.user.id)
            if row is None or row["guild_id"] != interaction.guild_id:
                await interaction.response.send_message(
                    "You don't have that drill placed in this server.", ephemeral=True
                )
                return
            unavailable = drill_unavailable_message(row, "remove it")
            if unavailable is not None:
                await interaction.response.send_message(unavailable, ephemeral=True)
                return

            # Back to the inventory as the same drill, keeping its level and
            # container - that persistence is the whole reason drills are
            # tracked per instance.
            collected_breakdown = await retract_drill(tx, row)
            if collected_breakdown is None:
                await interaction.response.send_message(
                    "That drill changed while this was going through. Try again.", ephemeral=True
                )
                return

        embed = make_embed("Drill Removed", MINING_COLOR)
        embed.add_field(
            name="Drill",
            value=f"{DRILLS[row['drill_type']]['emoji']} **{drill_short_label(row)}** returned to your inventory",
            inline=False,
        )
        lines = material_breakdown_lines(collected_breakdown)
        embed.add_field(
            name="Items Collected", value="\n".join(lines) if lines else "None", inline=False
        )
        await respond(interaction, self.db, embed=embed)

    @mine_group.command(name="attach", description="Fit a storage container to one of your drills")
    @app_commands.describe(drill="Which drill to fit", container="Which container to fit")
    @app_commands.autocomplete(drill=_local_drill_autocomplete, container=_owned_container_autocomplete)
    async def mine_attach(
        self,
        interaction: discord.Interaction,
        drill: int,
        container: str,
    ):
        row = await fetch_drill(self.db, drill, interaction.user.id)
        if row is None:
            await interaction.response.send_message("That isn't one of your drills.", ephemeral=True)
            return
        # The autocomplete only offers local drills, but its value is never
        # trusted to have come from the list we offered (see utils/drills.py's
        # module docstring) - this is what actually enforces the restriction.
        if not is_local_drill(row, interaction.guild_id):
            await interaction.response.send_message(
                f"**{drill_label(row)}** is placed in another server - run this there instead.",
                ephemeral=True,
            )
            return
        unavailable = drill_unavailable_message(row, "fit the container")
        if unavailable is not None:
            await interaction.response.send_message(unavailable, ephemeral=True)
            return
        if container not in STORAGE_CONTAINERS:
            await interaction.response.send_message("That isn't a storage container.", ephemeral=True)
            return
        if row["container_type"] == container:
            await interaction.response.send_message(
                f"**{drill_label(row)}** already has a {container_name(container)} fitted.", ephemeral=True
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
                have = await get_user_quantity(tx, interaction.user.id, container)
                if have < 1:
                    await interaction.response.send_message(
                        f"You don't have a **{container_name(container)}**. Craft one with `/factory craft`.",
                        ephemeral=True,
                    )
                    return

                if not await set_container(tx, row, container):
                    await interaction.response.send_message(
                        "That drill's container changed while this was going through. Try again.",
                        ephemeral=True,
                    )
                    return
                await deduct_user_quantity(tx, interaction.user.id, container, 1)
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
        new_capacity = effective_capacity(container)

        embed = make_embed("Container Fitted", MINING_COLOR)
        embed.description = (
            f"{STORAGE_CONTAINERS[container]['emoji']} Fitted a **{container_name(container)}** to "
            f"**{drill_short_label(row)}**."
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
    @app_commands.autocomplete(drill=_local_containered_drill_autocomplete)
    async def mine_detach(self, interaction: discord.Interaction, drill: int):
        row = await fetch_drill(self.db, drill, interaction.user.id)
        if row is None:
            await interaction.response.send_message("That isn't one of your drills.", ephemeral=True)
            return
        # Detach follows the same rule as attach rather than letting you always
        # pull your own container back: one rule to learn, and the paired
        # commands offer identical lists. Nothing gets stranded by it - leaving
        # a server retracts every drill in it, container and all.
        if not is_local_drill(row, interaction.guild_id):
            await interaction.response.send_message(
                f"**{drill_label(row)}** is placed in another server - run this there instead.",
                ephemeral=True,
            )
            return
        if row["container_type"] is None:
            await interaction.response.send_message(
                f"**{drill_label(row)}** has no container fitted.", ephemeral=True
            )
            return
        unavailable = drill_unavailable_message(row, "pull the container")
        if unavailable is not None:
            await interaction.response.send_message(unavailable, ephemeral=True)
            return

        removed = row["container_type"]
        old_capacity = capacity_of(row)

        async with self.db.transaction() as tx:
            await ensure_user_row(tx, interaction.user.id)
            # Pull it off first: if another command got there already this
            # matches nothing, and the refund below never happens - otherwise
            # racing detaches would each hand back a copy of the same one.
            if not await set_container(tx, row, None):
                await interaction.response.send_message(
                    "That drill's container changed while this was going through. Try again.",
                    ephemeral=True,
                )
                return
            await adjust_user_quantity(tx, interaction.user.id, removed, 1)

        embed = make_embed("Container Removed", MINING_COLOR)
        embed.description = (
            f"{STORAGE_CONTAINERS[removed]['emoji']} Pulled the **{container_name(removed)}** off "
            f"**{drill_short_label(row)}** and returned it to your inventory."
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
            # The same haul kept broken down by the guild whose pool it came
            # out of, which the aggregate above deliberately loses. Nothing
            # the player sees needs it - user_materials is global - but the
            # production ledger does: mined value is credited to
            # drills.guild_id, not to wherever /collect was typed.
            per_guild_raw: dict[int, dict[str, int]] = {}
            for d in drills:
                changed = await tx.execute_changes(
                    COLLECT_EMPTY_DRILL_SQL, (d["drill_id"], d["stored_amount"])
                )
                if not changed:
                    continue
                hauls.append((d["guild_id"], d["stored_amount"]))
                total_collected += d["stored_amount"]
                # Real materials, drawn from the server's pool when they were
                # mined, rather than rolled here at handover.
                drill_contents = await take_drill_contents(tx, d)
                from_guild = per_guild_raw.setdefault(d["guild_id"], {})
                for material_id, qty in drill_contents.items():
                    collected_breakdown[material_id] = collected_breakdown.get(material_id, 0) + qty
                    from_guild[material_id] = from_guild.get(material_id, 0) + qty

            # The focus converts the WHOLE haul at once, not drill by drill.
            # Its rounding carry is per player, so converting each drill
            # separately would give a different answer depending on how many
            # drills someone happened to have going.
            focus_id, _, _, _ = await get_focus(tx, interaction.user.id)
            # Read before anything rewrites it, for the ore-only comparison the
            # receipt draws below.
            raw_ore_total = sum(
                qty for mid, qty in collected_breakdown.items() if mid in ORES
            )
            collected_breakdown = await convert_haul(
                tx, interaction.user.id, collected_breakdown
            )

            # The efficiency runs AFTER the focus, on what the focus produced.
            # The focus decides what the materials ARE and the efficiency then
            # decides how many of them there are, so the other order would
            # boost ore that was about to be converted away.
            efficiency_id, _, _ = await get_efficiency(tx, interaction.user.id)
            collected_breakdown = await boost_haul(
                tx, interaction.user.id, collected_breakdown
            )

            # The affinity runs last and is the only one of the three that
            # touches gemstones - the other two convert and boost ore, and a
            # affinity converts gems. Nothing they produce is an input to it,
            # so the order between them is arbitrary; stating it keeps it
            # arbitrary rather than accidental.
            affinity_id, _, _, _ = await get_affinity(tx, interaction.user.id)
            raw_gems = {
                mid: qty for mid, qty in collected_breakdown.items() if mid in GEMSTONES
            }
            collected_breakdown = await convert_gems(
                tx, interaction.user.id, collected_breakdown
            )
            # Re-read for the carry AFTER the conversion. The receipt promises
            # progress the player can go and check against /mine affinity, so
            # it has to be the figure that was committed rather than the one
            # this haul started with.
            _, affinity_carry, _, _ = await get_affinity(tx, interaction.user.id)

            for material_id, qty in collected_breakdown.items():
                await adjust_user_quantity(tx, interaction.user.id, material_id, qty)

            # The production ledger's mining rows, credited to the server each
            # item was actually dug out of. THIS COMMAND SPANS SERVERS, so
            # interaction.guild_id is the wrong guild here and would quietly
            # hand whichever server the player happened to type /collect in
            # every other server's ore. See docs/market.md section 5.
            #
            # What is recorded is what LANDED IN THE INVENTORY above, not what
            # the drills banked - the focus and the efficiency change both the
            # mix and the count, and the ledger has to agree with the receipt
            # printed from the same numbers a few lines down.
            #
            # The ores were pooled - they have to be, since the focus's
            # rounding carry is per player - so they are divided back out by
            # each guild's share of the raw ore it contributed. That share is
            # exact whenever the collect covered one server, which is all but
            # the rarest of them.
            ore_weights = {
                guild_id: sum(qty for mid, qty in raw.items() if mid in ORES)
                for guild_id, raw in per_guild_raw.items()
            }
            converted_ores = {
                mid: qty for mid, qty in collected_breakdown.items() if mid in ORES
            }
            for guild_id, share in split_by_guild(converted_ores, ore_weights).items():
                await record_mined(tx, guild_id, share)

            # Gemstones were exactly attributable while nothing touched them,
            # and for everyone without an affinity they still are: a gem is the
            # one its own drill dug up, so it is credited to that drill's
            # server and no reconstruction is needed or wanted.
            #
            # An affinity pools them exactly as the focus pools ore, and then
            # they need the same treatment - weighted by what each guild's gems
            # were WORTH rather than by how many there were, because one
            # diamond and one ruby are not interchangeable the way two iron ore
            # are. Comparing the gems before and after is what decides which
            # case this is, so an affinity whose haul happened to contain only
            # its own target keeps the exact attribution it is entitled to.
            converted_gems = {
                mid: qty for mid, qty in collected_breakdown.items() if mid in GEMSTONES
            }
            if converted_gems == raw_gems:
                for guild_id, raw in per_guild_raw.items():
                    gems = {mid: qty for mid, qty in raw.items() if mid in GEMSTONES}
                    if gems:
                        await record_mined(tx, guild_id, gems)
            else:
                gem_weights = {
                    guild_id: sum(
                        qty * MARKET_PRICE_CENTS[mid]
                        for mid, qty in raw.items()
                        if mid in GEMSTONES
                    )
                    for guild_id, raw in per_guild_raw.items()
                }
                for guild_id, share in split_by_guild(converted_gems, gem_weights).items():
                    await record_mined(tx, guild_id, share)

            # What the player now holds of everything that just came in, read
            # after the credits and inside the same transaction so the numbers
            # on the embed are the ones that were actually committed. One query
            # rather than one per material: a drill only ever produces raw
            # materials, so this IN list is at most six long.
            totals: dict[str, int] = {}
            if collected_breakdown:
                placeholders = ",".join("?" * len(collected_breakdown))
                rows = await tx.fetchall(
                    f"SELECT material_id, quantity FROM user_materials "
                    f"WHERE user_id = ? AND material_id IN ({placeholders})",
                    (interaction.user.id, *collected_breakdown),
                )
                totals = {row["material_id"]: row["quantity"] for row in rows}

        server_count = len({guild_id for guild_id, _ in hauls})

        embed = make_embed("Collection Complete", MINING_COLOR)
        description_lines = [f"📦 Emptied **{total_collected:,}** raw materials"]
        drill_line = f"from **{len(hauls)}** drill(s)"
        if server_count > 1:
            drill_line += f" across **{server_count}** servers"
        description_lines.append(drill_line)

        # A focus changes the item count as well as the mix - a coal focus
        # returns fewer, denser items and an iron focus more - so the haul and
        # what landed in the inventory are two different numbers and the embed
        # has to say which is which rather than quietly contradicting itself.
        #
        # COUNTED OVER ORE ONLY, on both sides. This line belongs to the two
        # features that act on ore, and an affinity is deliberately absent from
        # it: it converts gems and leaves every ore alone, so naming it here
        # would credit it for a change it did not make. Counting the whole haul
        # instead would be worse than merely untidy - 45 rubies becoming one
        # diamond drops the total by 44, and the line would report that as
        # something the focus did. Gems get their own field below.
        ore_received = sum(qty for mid, qty in collected_breakdown.items() if mid in ORES)
        if ore_received != raw_ore_total:
            applied = []
            if focus_id != DEFAULT_MINING_FOCUS:
                applied.append(f"**{focus_label(focus_id)}** focus")
            if efficiency_id != DEFAULT_MINING_EFFICIENCY:
                applied.append(f"**{efficiency_label(efficiency_id)}** efficiency")
            if applied:
                description_lines.append(
                    f"\nWhich your {'\nand '.join(applied)}"
                    f"\nturned into **{ore_received:,}** raw materials"
                )
        embed.description = "\n".join(description_lines)

        lines = material_breakdown_lines(collected_breakdown, totals)
        if lines:
            add_multi_field(embed, "Materials", lines)

        # The first of the three places an affinity's progress is shown, and the
        # one that matters most: a player aiming at a diamond who collects a
        # ruby receives NOTHING in the materials list above, because 45 of them
        # make one diamond. Without this the rarest event in the game would
        # read as an empty haul. Shown whenever there is either a conversion to
        # report or progress standing, so it never appears as a bare heading.
        #
        # The field ends with what the affinity MADE, separated by a blank
        # line, because that is the one number the materials list above cannot
        # answer. A diamond in that list is just a diamond; whether the pool
        # handed it over or 45 rubies and 3 obsidian were melted into it is the
        # question this field exists to settle, and it came up the first time
        # anyone mined a mixed haul.
        affinity_lines = []
        target = MINING_AFFINITIES[affinity_id]["primary"]
        if target is not None:
            target_info = get_material_info(target)
            # GEMSTONES order (commonest first) rather than whatever order the
            # drill's rows came back in, so the same haul always renders the
            # same way - the rule material_breakdown_lines follows for ore.
            for material_id in GEMSTONES:
                quantity = raw_gems.get(material_id, 0)
                if material_id == target or not quantity:
                    continue
                info = get_material_info(material_id)
                affinity_lines.append(
                    f"{info['emoji']} **{quantity:,} {material_name(info, quantity)}** → "
                    f"{target_info['emoji']} {target_info['name']}"
                )
            progress = affinity_progress(affinity_id, affinity_carry)
            if progress:
                affinity_lines.append(progress)
            # What the conversion added, not what the haul holds: a diamond
            # affinity that drew a diamond from the pool converted nothing, and
            # counting the whole line would claim it.
            created = collected_breakdown.get(target, 0) - raw_gems.get(target, 0)
            if created > 0:
                affinity_lines.append(
                    f"\n{target_info['emoji']} **{created:,} {material_name(target_info, created)}** "
                    f"{'was' if created == 1 else 'were'} created this collection"
                )
        if affinity_lines:
            add_multi_field(embed, f"{affinity_label(affinity_id)} Affinity", affinity_lines)

        await respond(interaction, self.db, embed=embed)

    @app_commands.command(
        name="focus",
        description="Choose which ore everything you mine arrives as (costs one Ruby to unlock)",
    )
    @app_commands.describe(focus="Leave blank to see your current focus and what the others do")
    # Names and nothing else. Discord's picker is not the place for either of
    # the two decorations the embed carries:
    #
    #   * No icon. A choice name is rendered as plain text, so a custom
    #     <:IronOre:...> arrives in the menu as that literal markup.
    #   * No "(selected)" marker. This list is built once, when the class is
    #     defined, and is served identically to every player - a per-player mark
    #     is not something it can express, and hard-coding one would be wrong
    #     for everyone it didn't apply to. `/focus` on its own is where a player
    #     sees what they're on.
    #
    # Static rather than an autocomplete callback precisely because there is
    # nothing per-player left to compute: this way Discord validates the
    # submitted value against the list for us, which an autocomplete - a
    # suggestion list, not a constraint - does not do.
    @app_commands.choices(focus=[
        app_commands.Choice(name=info["name"], value=focus_id)
        for focus_id, info in MINING_FOCUSES.items()
    ])
    async def focus(
        self,
        interaction: discord.Interaction,
        focus: app_commands.Choice[str] | None = None,
    ):
        """Sets, or shows, this player's mining focus.

        Global rather than per-server, matching user_materials: /collect empties
        drills across every server in one call, so a per-server focus would
        convert each drill's haul differently inside a single receipt.

        The ruby is charged ONCE, on the first call that actually chooses a
        focus. Charging it per change would price the choice far above its own
        worth - a ruby is about a month of a starting player's entire output,
        against a benefit measured in fractions of a coin - and nobody would
        ever revise a focus, which defeats the point of having them. Changes are
        free and limited to one a day instead.
        """
        current, _, last_changed, unlocked = await get_focus(self.db, interaction.user.id)
        today = job_board_today()

        if focus is None:
            await respond(interaction, self.db, embed=self._focus_embed(current, unlocked))
            return

        chosen = focus.value
        # Deliberately NOT gated on `unlocked`. A player who hasn't paid yet
        # reads as Balance, so this also catches someone picking Balance as
        # their first focus - which would charge them a ruby for exactly the
        # mining they already had. Balance is worth choosing only as a way back
        # from a real focus, and by then they've unlocked it.
        if chosen == current:
            await interaction.response.send_message(
                f"You're already mining **{focus_label(current)}**."
                + ("" if unlocked else " It's the default - choosing it wouldn't change anything, "
                   "so it isn't worth a Ruby. Pick one of the others."),
                ephemeral=True,
            )
            return
        if unlocked and last_changed == today:
            await interaction.response.send_message(
                f"You've already changed your mining focus today. "
                f"You can change it <t:{int(next_reset().timestamp())}:R>.",
                ephemeral=True,
            )
            return

        # Taking the ruby, recording the focus and clearing the rounding carry
        # commit together: a failure between them either charges for nothing or
        # hands the feature out free.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                if not unlocked:
                    for material_id, quantity in MINING_FOCUS_UNLOCK_COST.items():
                        have = await get_user_quantity(tx, interaction.user.id, material_id)
                        if have < quantity:
                            info = get_material_info(material_id)
                            await interaction.response.send_message(
                                f"Choosing a mining focus costs {info['emoji']} "
                                f"**{quantity} {material_name(info, quantity)}**, and you have {have}. "
                                f"Mine one, or press one with `/press craft`.",
                                ephemeral=True,
                            )
                            return
                        await deduct_user_quantity(tx, interaction.user.id, material_id, quantity)
                await set_focus(tx, interaction.user.id, chosen, today)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your inventory changed while that was going through - nothing was spent. Try again.",
                ephemeral=True,
            )
            return

        embed = self._focus_embed(chosen, True)
        embed.title = "Mining Focus Set" if unlocked else "Mining Focus Unlocked"
        if not unlocked:
            embed.set_footer(text=unlock_footer(MINING_FOCUS_UNLOCK_COST))
        await respond(interaction, self.db, embed=embed)

    def _focus_embed(self, current: str, unlocked: bool) -> discord.Embed:
        """The focus menu: what you're on, and what each of the others does.

        Every focus is described in full even when the player hasn't unlocked
        one, because the two facts that decide the choice are both bad news and
        both easy to discover too late - copper and coal can't make steel at
        all, and iron helps steel far less than doubling your iron ore sounds
        like it should.
        """
        embed = make_embed("Mining Focus", MINING_COLOR)
        if unlocked:
            embed.description = f"You're mining **{focus_label(current)}**."
        else:
            costs = ", ".join(
                f"{get_material_info(m)['emoji']} **{q} {material_name(get_material_info(m), q)}**"
                for m, q in MINING_FOCUS_UNLOCK_COST.items()
            )
            embed.description = (
                f"A mining focus converts the ore you don't want into the one you do. Copper is "
                f"worth two iron because iron drops around twice as often "
                f"— you get more of what you want and none of what you don't.\n\n"
                f"Unlocking it costs {costs}, once. After that, changing is free, once a day.\n\n"
                f"Gemstones are unaffected by focus; rubies, obsidian and diamonds are equally "
                f"likely whatever you choose."
            )

        # Icon and marker both belong in the heading: the icon because it is how
        # every ore is identified everywhere else in the bot, and the marker
        # because "which one am I on" is the only question this embed exists to
        # answer that the blurbs don't. Field names DO render custom emoji -
        # /mine status has done it since 1.0 - unlike author lines and footers.
        # Marked whether or not they've unlocked it. Someone who has never spent
        # the ruby genuinely IS mining Balance - it's what convert_haul does
        # with them - so leaving every heading unmarked would answer "what am I
        # on?" with nothing at all, and make Balance read as something they
        # can't have rather than the thing they already have.
        for focus_id, info in MINING_FOCUSES.items():
            marker = " (selected)" if focus_id == current else ""
            embed.add_field(
                name=f"{info['emoji']} {info['name']}{marker}",
                value=info["blurb"],
                inline=False,
            )

        return embed

    @app_commands.command(
        name="efficiency",
        description="Double the raw materials one smelted recipe needs (costs one Obsidian to unlock)",
    )
    @app_commands.describe(
        efficiency="Leave blank to see your current efficiency and what the others do"
    )
    # Static choices, names only, for exactly the reasons /focus uses them -
    # see the comment there.
    @app_commands.choices(efficiency=[
        app_commands.Choice(name=info["name"], value=efficiency_id)
        for efficiency_id, info in MINING_EFFICIENCIES.items()
    ])
    async def efficiency(
        self,
        interaction: discord.Interaction,
        efficiency: app_commands.Choice[str] | None = None,
    ):
        """Sets, or shows, this player's mining efficiency.

        Global rather than per-server and applied at collection, for the same
        reasons /focus is: /collect empties drills across every server in one
        call, so a per-server setting would boost each drill's haul differently
        inside a single receipt.

        The obsidian is charged ONCE, on the first call that actually chooses
        an efficiency. Changes are free and limited to one a day, matching the
        focus - the choice is worth revising as a player's server prices move,
        and pricing each revision at a gem would stop anyone ever doing it.
        """
        current, last_changed, unlocked = await get_efficiency(self.db, interaction.user.id)
        focus_id, _, _, _ = await get_focus(self.db, interaction.user.id)
        today = job_board_today()

        if efficiency is None:
            await respond(
                interaction, self.db, embed=self._efficiency_embed(current, focus_id, unlocked)
            )
            return

        chosen = efficiency.value
        # Deliberately NOT gated on `unlocked`, exactly as /focus isn't: someone
        # who has never paid reads as None, so this also catches a player
        # picking None as their first efficiency and charging them an obsidian
        # for the mining they already had.
        if chosen == current:
            await interaction.response.send_message(
                f"Your mining efficiency is already **{efficiency_label(current)}**."
                + ("" if unlocked else " That's the default - choosing it wouldn't change "
                   "anything, so it isn't worth an Obsidian. Pick one of the others."),
                ephemeral=True,
            )
            return
        if unlocked and last_changed == today:
            await interaction.response.send_message(
                f"You've already changed your mining efficiency today. "
                f"You can change it <t:{int(next_reset().timestamp())}:R>.",
                ephemeral=True,
            )
            return

        # Taking the obsidian, recording the choice and clearing the carries
        # commit together: a failure between them either charges for nothing or
        # hands the feature out free.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                if not unlocked:
                    for material_id, quantity in MINING_EFFICIENCY_UNLOCK_COST.items():
                        have = await get_user_quantity(tx, interaction.user.id, material_id)
                        if have < quantity:
                            info = get_material_info(material_id)
                            await interaction.response.send_message(
                                f"Choosing a mining efficiency costs {info['emoji']} "
                                f"**{quantity} {material_name(info, quantity)}**, and you have {have}. "
                                f"Mine one, or press one with `/press craft`.",
                                ephemeral=True,
                            )
                            return
                        await deduct_user_quantity(tx, interaction.user.id, material_id, quantity)
                await set_efficiency(tx, interaction.user.id, chosen, today)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your inventory changed while that was going through - nothing was spent. Try again.",
                ephemeral=True,
            )
            return

        embed = self._efficiency_embed(chosen, focus_id, True)
        embed.title = "Mining Efficiency Set" if unlocked else "Mining Efficiency Unlocked"
        if not unlocked:
            embed.set_footer(text=unlock_footer(MINING_EFFICIENCY_UNLOCK_COST))
        await respond(interaction, self.db, embed=embed)

    def _efficiency_embed(
        self, current: str, focus_id: str, unlocked: bool
    ) -> discord.Embed:
        """The efficiency menu: what you're on, what each option does, and
        whether your focus can actually feed the one you've chosen.

        That last part is the reason this embed isn't just a list of blurbs. An
        efficiency and a focus are independent, so nothing stops a player
        pairing Steel with Copper & Coal - which produces no iron ore at all,
        leaving the efficiency with only the coal to work with. The result is
        legal, poor, and completely invisible until they look at a receipt, so
        it gets said here instead.
        """
        embed = make_embed("Mining Efficiency", MINING_COLOR)
        if unlocked:
            embed.description = f"Your mining efficiency is **{efficiency_label(current)}**."
        else:
            costs = ", ".join(
                f"{get_material_info(m)['emoji']} **{q} {material_name(get_material_info(m), q)}**"
                for m, q in MINING_EFFICIENCY_UNLOCK_COST.items()
            )
            embed.description = (
                "A mining efficiency doubles the raw materials one smelted recipe needs, "
                "then converts a little of whichever one you have too much of into the "
                "other — so what you collect smelts down with less left over.\n\n"
                f"Unlocking it costs {costs}, once. After that, changing is free, once a day.\n\n"
                "It's separate from your mining focus: you can have either, both or neither, "
                "and the two stack."
            )

        kept = MINING_FOCUSES[focus_id]["keep"]
        for efficiency_id, info in MINING_EFFICIENCIES.items():
            marker = " (selected)" if efficiency_id == current else ""
            value = info["blurb"]
            produces = info["produces"]
            if produces is not None:
                missing = [m for m in recipe_true_inputs(produces) if m not in kept]
                if missing:
                    names = " or ".join(get_material_info(m)["name"] for m in missing)
                    value += (
                        f"\n⚠️ Your **{focus_label(focus_id)}** focus mines no {names}, "
                        f"so this would have very little to work with."
                    )
            embed.add_field(name=f"{info['emoji']} {info['name']}{marker}", value=value, inline=False)

        return embed

    @app_commands.command(
        name="affinity",
        description="Choose which gemstone every other gem you mine arrives as (costs one Diamond)",
    )
    @app_commands.describe(
        affinity="Leave blank to see your current affinity and what the others do"
    )
    # Static choices, names only, for exactly the reasons /focus uses them -
    # see the comment there.
    @app_commands.choices(affinity=[
        app_commands.Choice(name=info["name"], value=affinity_id)
        for affinity_id, info in MINING_AFFINITIES.items()
    ])
    async def affinity(
        self,
        interaction: discord.Interaction,
        affinity: app_commands.Choice[str] | None = None,
    ):
        """Sets, or shows, this player's mining affinity.

        Global rather than per-server and applied at collection, for the same
        reasons /focus and /efficiency are: /collect empties drills across
        every server in one call, so a per-server setting would convert each
        drill's gems differently inside a single receipt.

        The diamond is charged ONCE, on the first call that actually chooses a
        gem. Changes are free and limited to one a day, matching both siblings.

        Unlike them, a change can hand the player materials: the accrued carry
        is worth real gems, so set_affinity converts it to the new target and
        pays out whatever whole ones fall out. That payout is reported here
        rather than left to be discovered in /inventory.
        """
        current, carry, last_changed, unlocked = await get_affinity(
            self.db, interaction.user.id
        )
        today = job_board_today()

        if affinity is None:
            await respond(
                interaction, self.db, embed=self._affinity_embed(current, carry, unlocked)
            )
            return

        chosen = affinity.value
        # Deliberately NOT gated on `unlocked`, exactly as neither sibling is:
        # someone who has never paid reads as None, so this also catches a
        # player picking None as their first affinity and charging them a
        # diamond for the mining they already had.
        if chosen == current:
            await interaction.response.send_message(
                f"Your mining affinity is already **{affinity_label(current)}**."
                + ("" if unlocked else " That's the default - choosing it wouldn't change "
                   "anything, so it isn't worth a Diamond. Pick one of the others."),
                ephemeral=True,
            )
            return
        if unlocked and last_changed == today:
            await interaction.response.send_message(
                f"You've already changed your mining affinity today. "
                f"You can change it <t:{int(next_reset().timestamp())}:R>.",
                ephemeral=True,
            )
            return

        # Taking the diamond, recording the choice, moving the carry and paying
        # out whatever it came to all commit together: a failure between them
        # either charges for nothing, hands the feature out free, or pays a
        # player gems that were never deducted from their progress.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                if not unlocked:
                    for material_id, quantity in MINING_AFFINITY_UNLOCK_COST.items():
                        have = await get_user_quantity(tx, interaction.user.id, material_id)
                        if have < quantity:
                            info = get_material_info(material_id)
                            await interaction.response.send_message(
                                f"Choosing a mining affinity costs {info['emoji']} "
                                f"**{quantity} {material_name(info, quantity)}**, and you have {have}. "
                                f"Mine one, or press one with `/press craft`.",
                                ephemeral=True,
                            )
                            return
                        await deduct_user_quantity(tx, interaction.user.id, material_id, quantity)
                paid = await set_affinity(tx, interaction.user.id, chosen, today)
                _, carry, _, _ = await get_affinity(tx, interaction.user.id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your inventory changed while that was going through - nothing was spent. Try again.",
                ephemeral=True,
            )
            return

        embed = self._affinity_embed(chosen, carry, True)
        embed.title = "Mining Affinity Set" if unlocked else "Mining Affinity Unlocked"
        if paid:
            banked = ", ".join(
                f"{get_material_info(m)['emoji']} **{q:,} {material_name(get_material_info(m), q)}**"
                for m, q in paid.items()
            )
            embed.add_field(
                name="Progress Carried Over",
                value=f"What you had accrued came to {banked}, now in your inventory.",
                inline=False,
            )
        if not unlocked:
            embed.set_footer(text=unlock_footer(MINING_AFFINITY_UNLOCK_COST))
        await respond(interaction, self.db, embed=embed)

    def _affinity_embed(self, current: str, carry: float, unlocked: bool) -> discord.Embed:
        """The affinity menu, laid out exactly as the focus and efficiency
        menus are: one field per option, "(selected)" on the one you're on.

        The text above the fields is deliberately shorter than theirs. Those
        two need sentences - a focus that can't feed steel, an efficiency your
        focus can't supply - but an affinity's options are exchange rates, and
        the rates say what the feature does on their own.

        The third of the three places progress is shown, and the only one a
        player can reach without mining anything.
        """
        embed = make_embed("Mining Affinity", MINING_COLOR)
        if unlocked:
            embed.description = f"Your mining affinity is **{affinity_label(current)}**."
            progress = affinity_progress(current, carry)
            if progress:
                embed.description += f"\n{progress}"
        else:
            costs = ", ".join(
                f"{get_material_info(m)['emoji']} **{q} {material_name(get_material_info(m), q)}**"
                for m, q in MINING_AFFINITY_UNLOCK_COST.items()
            )
            embed.description = (
                f"Unlocking it costs {costs}, once. After that, changing is free, once a day."
            )

        # Same heading shape as /focus and /efficiency - see the comment in
        # _focus_embed on why the marker belongs in the field name.
        for affinity_id, info in MINING_AFFINITIES.items():
            marker = " (selected)" if affinity_id == current else ""
            embed.add_field(
                name=f"{info['emoji']} {info['name']}{marker}",
                value=info["blurb"],
                inline=False,
            )

        return embed

    @tasks.loop(minutes=HARVEST_TICK_MINUTES)
    async def harvest_loop(self):
        """Every tick, each placed non-full drill pulls what the time since it
        was last credited pays for from its server's mining pool, filling up
        to its capacity and then marking itself full.

        That is a whole tick for a drill that was mining all along, and less
        for one that only started since the last tick - placed, or freed up by
        a /collect or a container. drills.mined_until is what tells the two
        apart, and it costs nothing extra: this loop already re-reads and
        rewrites every drill it visits."""
        now = self._now()
        now_text = sqlite_timestamp(now)
        # Read once per tick rather than per drill: a Bonanza is a handful of
        # servers at most, and every drill in them mines at double rate.
        bonanza = await guilds_with_bonanza(self.db, now)
        # Only the ids: every drill is re-read inside its own transaction
        # below, so nothing else from this select would be used.
        drills = await self.db.fetchall(
            "SELECT drill_id FROM drills WHERE is_full = 0 AND guild_id IS NOT NULL"
        )
        for d in drills:
            # One transaction per drill: the pool is shared by every drill in
            # the server and by /market, so reading what's left and taking a
            # share of it has to be atomic. Otherwise two drills can both read
            # the same remainder and between them mine more than the pool held,
            # driving it negative and inventing raw materials from nothing.
            async with self.db.transaction() as tx:
                # No "has the pool got anything left" check, and no clamping the
                # take to what's in it. The bag refills the moment it empties
                # (take_from_pool), so a drill is never stopped by the server
                # running out - that was the daily allowance, and it's gone. The
                # only thing that stops a drill now is its own storage filling
                # up, which is what space_left below is.

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

                rate = rate_of(current)
                if current["guild_id"] in bonanza:
                    rate *= BONANZA_SPEED_MULTIPLIER
                amount, carry = advance_harvest(
                    current["harvest_progress"],
                    rate,
                    elapsed_work_hours(current["mined_until"], now, HARVEST_TICK_MINUTES),
                )

                # What comes out is decided HERE rather than at /collect, which
                # is the 1.2 change this all turns on: the pool has a real
                # finite composition, and a guaranteed diamond sitting in a
                # shared bag can't be drawn per-player at handover without every
                # player drawing their own copy of it. take_from_pool removes
                # what it returns, so this is the one place the material becomes
                # the drill's.
                drawn = await take_from_pool(
                    tx, current["guild_id"], min(amount, space_left)
                )
                harvested = sum(drawn.values())
                new_stored = current["stored_amount"] + harvested

                # The carry is written even when harvested is 0, so a drill
                # that mines less than one item per tick still accumulates
                # toward one instead of resetting every tick. Whole items lost
                # to the space_left clamp are dropped rather than banked: a
                # drill that filled up mid-tick shouldn't pay out the rest of
                # that tick the instant it's emptied.
                await tx.execute(
                    "UPDATE drills SET stored_amount = ?, is_full = ?, harvest_progress = ?, "
                    "mined_until = ? WHERE drill_id = ?",
                    (new_stored, 1 if new_stored >= capacity else 0, carry, now_text,
                     current["drill_id"]),
                )
                await add_drill_contents(tx, current["drill_id"], drawn)

    @harvest_loop.before_loop
    async def before_harvest_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the mine_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(MiningCog(bot))
