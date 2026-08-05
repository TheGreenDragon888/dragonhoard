"""
cogs/economy.py

Implements:
  - /balance                              - your balance of this server's currency
  - /inventory                            - your inventory alongside your balance
  - /market sell <material> <quantity>    - sell to the server (the currency faucet)
  - /market buy <material> <quantity>     - buy from the server's stock (a currency sink)
  - /market status                        - show the server's current stock and prices

Per docs/market.md, the server itself is an economic actor with its own
material storage (server_material_storage table). It buys raw/smelted
materials from users at a per-unit price that decays as its stock grows -
full ceiling price at zero stock, half price at the server's "target stock"
(bot-excluded member_count * that material's own per-member constant in
MATERIAL_TARGET_STOCK_PER_MEMBER), tapering toward (but never reaching)
zero beyond it. It sells back to users at that same rate plus a
full ceiling price on top - double the ceiling price at zero stock, tapering
toward (but never below) the ceiling price as stock grows - constrained by
what it actually has in stock, since the server can't sell what it never
acquired. Both rates are locked in at the stock level when the trade starts:
quantity scales the total but never shifts the per-unit price within a
single trade, only the stock change carried into future trades does.

DragonCoin (users.dragoncoin) is intentionally NOT surfaced here or anywhere
else - per docs/market.md section 2, it exists solely as a future conceptual
unit for cross-server exchange rates and isn't spendable, earnable, or shown
in any menu.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond
from utils.embeds import make_embed, add_multi_field, INVENTORY_COLOR, MARKET_COLOR
from utils.formatting import format_currency, format_compact_price, DEFAULT_CURRENCY_EMOJI
from utils.guild_helpers import human_member_count
from utils.job_board import credit_job_progress
from utils.drills import drill_cell
from database.db import InsufficientQuantity
from utils.db_helpers import (
    ensure_user_row,
    ensure_server_row,
    get_user_quantity,
    adjust_user_quantity,
    deduct_user_quantity,
    get_server_stock,
    adjust_server_stock,
    deduct_server_stock,
    get_currency_balance,
    adjust_currency_balance,
    deduct_currency_balance,
    record_minted,
    record_burned,
)

from data.materials import (
    ALL_MATERIALS,
    TRADEABLE_ORDER,
    INVENTORY_CATEGORIES,
    get_material_info,
    target_stock,
)

# Only raw and smelted materials are tradeable through the market - component
# materials and drills are excluded (docs/market.md section 3).
#
# Built in TRADEABLE_ORDER rather than by merging the two tables, because dict
# order IS display order here: this one mapping drives /market status's lines
# and both /market sell's and /market buy's choice lists. That ordering - ores,
# then smelted, then gemstones, each commonest first - lives in
# data/materials.py, derived from drop chances and recipes, so retuning either
# reorders all three surfaces without anyone remembering to.
TRADEABLE_MATERIALS = {material_id: ALL_MATERIALS[material_id] for material_id in TRADEABLE_ORDER}

# How many drills /inventory lists individually before collapsing the rest
# into a count, mirroring the pending-jobs cap in /factory status.
DRILL_DISPLAY_LIMIT = 20

# How many drill cells /inventory fits on one line. Fewer than the six used for
# the plain material grid because a drill cell carries a level and a container
# emoji as well as its own, so it's roughly half again as wide.
DRILL_GRID_COLUMNS = 4


def max_affordable(unit_cost: float, balance: float) -> int:
    """How many units a balance covers at a flat per-unit cost.

    Exact arithmetic rather than a search, because _sell_price locks its rate
    at the stock level the trade starts from - quantity scales the total but
    never moves the price within one trade, so the total is simply linear.

    The 1e-9 nudge is the same guard format_price uses: a balance that exactly
    covers N units can land a hair under N on float division, and quoting N-1
    back to someone who can afford N is precisely the confusion this message
    exists to remove.
    """
    if unit_cost <= 0 or balance <= 0:
        return 0
    return int(balance / unit_cost + 1e-9)


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _get_currency_emoji(self, guild_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["currency_emoji"] if row else None

    def _buy_price(self, ceiling_price: float, current_stock: int, quantity: int, target_stock: int) -> float:
        """Total the server pays to acquire `quantity` units, priced at the
        flat per-unit rate for the stock level at the start of the trade -
        ceiling_price * target / (target + current_stock) - full price at
        zero stock, half price at target_stock, approaching (but never
        reaching) zero beyond it. Target stock is an equilibrium point, not
        a maximum: the server always buys, and every unit is paid the same
        rate within a trade - quantity doesn't move the price mid-trade,
        only the stock change persisted afterward does, for the next trade."""
        if target_stock <= 0 or quantity <= 0:
            return 0.0
        return ceiling_price * target_stock / (target_stock + current_stock) * quantity

    def _sell_price(self, ceiling_price: float, current_stock: int, quantity: int, target_stock: int) -> float:
        """Total a user pays to buy `quantity` units from the server, priced
        at the flat per-unit rate for the stock level at the start of the
        trade - ceiling_price * (1 + target / (target + current_stock)) -
        double the ceiling price at zero stock, tapering down toward (but
        never below) the ceiling price as stock grows. As with _buy_price,
        quantity doesn't move the price mid-trade. This rate is always
        exactly ceiling_price above _buy_price's rate at the same stock
        level, so round-tripping (sell then immediately buy back) is never
        profitable, regardless of within-trade granularity."""
        if target_stock <= 0 or quantity <= 0 or current_stock - quantity < 0:
            return 0.0
        return ceiling_price * (1 + target_stock / (target_stock + current_stock)) * quantity

    def _cannot_afford_message(
        self,
        ceiling_price: float,
        current_stock: int,
        target: int,
        quantity: int,
        total_cost: float,
        balance: float,
        currency_emoji: str | None,
    ) -> str:
        """The /market buy rejection, which also names the largest quantity the
        user COULD buy right now. Being told only "you can't afford this" leaves
        them bisecting by hand, which is a pointless thing to make someone do
        when the answer is one division away."""
        unit_cost = self._sell_price(ceiling_price, current_stock, 1, target)
        affordable = max_affordable(unit_cost, balance)
        # The requested quantity is by definition unaffordable - we're in this
        # branch because of it - so float noise must never let it be quoted back
        # as the answer.
        affordable = min(affordable, quantity - 1)

        shortfall = (
            f"This costs {format_currency(total_cost, currency_emoji, True)}, "
            f"but you only have {format_currency(balance, currency_emoji)}."
        )
        if affordable < 1:
            return f"{shortfall} That isn't enough for even one."
        return (
            f"{shortfall} You can afford up to **{affordable:,}**, "
            f"for {format_currency(unit_cost * affordable, currency_emoji, True)}."
        )

    async def _currency_lines(self, interaction: discord.Interaction) -> list[str]:
        """Every server currency balance this user holds, formatted for
        display and labelled with the currency's own name. The current
        server's currency always comes first (even if the balance is 0),
        followed by every other server's currency ordered highest balance to
        lowest.

        Servers Dragonhoard has been removed from are filtered out by
        bot_present: their balances are deliberately kept in the database (a
        re-invite restores them untouched), but a currency you have no way to
        earn or spend is just noise in a list you read to decide what to do
        next. The current server needs no such guard - running a command in it
        proves the bot is there."""
        rows = await self.db.fetchall(
            """
            SELECT scb.guild_id, scb.balance, sc.currency_name, sc.currency_emoji
            FROM server_currency_balances scb
            JOIN server_config sc ON sc.guild_id = scb.guild_id
            WHERE scb.user_id = ? AND sc.bot_present = 1
            """,
            (interaction.user.id,),
        )
        by_guild = {row["guild_id"]: row for row in rows}

        ordered_rows = []
        if interaction.guild_id is not None:
            current = by_guild.pop(interaction.guild_id, None)
            if current is None:
                server_cfg = await self.db.fetchone(
                    "SELECT currency_name, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                current = {
                    "guild_id": interaction.guild_id,
                    "balance": 0.0,
                    "currency_name": server_cfg["currency_name"] if server_cfg else None,
                    "currency_emoji": server_cfg["currency_emoji"] if server_cfg else None,
                }
            ordered_rows.append(current)

        ordered_rows.extend(sorted(by_guild.values(), key=lambda r: r["balance"], reverse=True))

        lines = []
        for row in ordered_rows:
            emoji = row["currency_emoji"] if row["currency_name"] else None
            # A server that hasn't run /setup currency has no currency name
            # yet, so fall back to naming the server itself.
            guild = self.bot.get_guild(row["guild_id"])
            label = row["currency_name"] or (guild.name if guild else f"Server {row['guild_id']}")
            lines.append(f"{format_currency(row['balance'], emoji)} {label}")
        return lines

    @app_commands.command(name="balance", description="Check your currency balances across every server")
    async def balance(self, interaction: discord.Interaction):
        embed = make_embed(f"{interaction.user.display_name}'s Balance", INVENTORY_COLOR)
        lines = await self._currency_lines(interaction)
        add_multi_field(embed, "Currencies", lines)
        await respond(interaction, self.db, embed=embed)

    @app_commands.command(name="inventory", description="Show your inventory alongside your balance")
    async def inventory(self, interaction: discord.Interaction):
        embed = make_embed(f"{interaction.user.display_name}'s Inventory", INVENTORY_COLOR)

        # _currency_lines puts this server first, then every other server by
        # balance descending - so the first three are this server plus the
        # user's two largest balances elsewhere.
        currency_lines = await self._currency_lines(interaction)
        embed.description = "\n".join(currency_lines[:3])

        inventory_rows = await self.db.fetchall(
            "SELECT material_id, quantity FROM user_materials WHERE user_id = ? AND quantity > 0",
            (interaction.user.id,),
        )
        quantities = {row["material_id"]: row["quantity"] for row in inventory_rows}

        has_items = False
        for field_name, material_ids in INVENTORY_CATEGORIES:
            cells = []
            for material_id in material_ids:
                quantity = quantities.get(material_id)
                info = get_material_info(material_id)
                if not quantity or info is None:
                    continue
                cells.append(f"{info['emoji']} {quantity}")
            if not cells:
                continue
            has_items = True
            grid_lines = [" ".join(cells[i:i + 6]) for i in range(0, len(cells), 6)]
            add_multi_field(embed, field_name, grid_lines)

        # Drills get their own field rather than sharing the grid above: they
        # aren't stacks in user_materials any more but individual rows, each
        # with its own level and container, so a bare "{emoji} {count}" cell
        # can't say what any one of them actually is.
        #
        # Only UNPLACED drills are listed. A placed drill isn't in your
        # inventory in any sense that matters - you can't craft with it, fit a
        # container to it or place it somewhere else - so listing it here made
        # the field read as a roster rather than as stock on hand. /mine status
        # is where a server's placed drills are shown.
        drill_rows = await self.db.fetchall(
            "SELECT * FROM drills WHERE owner_id = ? AND guild_id IS NULL "
            "ORDER BY level DESC, drill_id ASC",
            (interaction.user.id,),
        )
        if drill_rows:
            has_items = True
            cells = [drill_cell(row) for row in drill_rows[:DRILL_DISPLAY_LIMIT]]
            grid_lines = [
                " ".join(cells[i:i + DRILL_GRID_COLUMNS])
                for i in range(0, len(cells), DRILL_GRID_COLUMNS)
            ]
            if len(drill_rows) > DRILL_DISPLAY_LIMIT:
                grid_lines.append(f"... and {len(drill_rows) - DRILL_DISPLAY_LIMIT} more")
            add_multi_field(embed, "Drills", grid_lines)

        if not has_items:
            embed.add_field(name="Items", value="Your inventory is empty.", inline=False)

        await respond(interaction, self.db, embed=embed)

    market_group = app_commands.Group(name="market", description="Trade raw and smelted materials with the server")

    @market_group.command(name="sell", description="Sell materials from your inventory to the server")
    @app_commands.describe(material="What to sell", quantity="How many to sell")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in TRADEABLE_MATERIALS.items()
    ])
    async def market_sell(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        info = TRADEABLE_MATERIALS[material.value]
        ceiling_price = info["market_ceiling_price"]

        # Read the member count before opening the transaction: it can chunk
        # the guild over the gateway, and holding the write lock across a
        # network round-trip would stall every other command in the bot.
        member_count = await human_member_count(interaction.guild)

        # Everything the price depends on is read inside the transaction, so
        # nothing can move between deciding the payout and deducting the
        # materials. Run twice concurrently without this and both invocations
        # read the same stock, both pass the "do you have enough" check, and
        # the player is paid twice for one stack.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                have = await get_user_quantity(tx, interaction.user.id, material.value)
                if have < quantity:
                    await interaction.response.send_message(
                        f"You only have {have} of that item.", ephemeral=True
                    )
                    return

                current_stock = await get_server_stock(tx, interaction.guild_id, material.value)
                target = target_stock(member_count, material.value)
                total_value = self._buy_price(ceiling_price, current_stock, quantity, target)

                await deduct_user_quantity(tx, interaction.user.id, material.value, quantity)
                await adjust_server_stock(tx, interaction.guild_id, material.value, quantity)
                await adjust_currency_balance(tx, interaction.guild_id, interaction.user.id, total_value)
                await record_minted(tx, interaction.guild_id, total_value)

                # The day's job board task, if this sale counts towards it -
                # in the same transaction as the sale, so the bonus and the
                # sale that earned it can never come apart. Returns 0.0 unless
                # this is the sale that completed it. See utils/job_board.py.
                bonus = await credit_job_progress(
                    tx, interaction.guild_id, interaction.user.id,
                    material.value, quantity, member_count,
                )
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your inventory changed while that was going through - nothing was sold. Try again.",
                ephemeral=True,
            )
            return

        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        message = (
            f"Sold {quantity}x **{info['name']}** to the server for "
            f"{format_currency(total_value, currency_emoji)}."
        )
        if bonus > 0:
            message += (
                f"\n📋 That finished today's job board task - "
                f"**{format_currency(bonus, currency_emoji)}** bonus."
            )
        await respond(interaction, self.db, content=message)

    @market_group.command(name="buy", description="Buy materials from the server's stock")
    @app_commands.describe(material="What to buy", quantity="How many to buy")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in TRADEABLE_MATERIALS.items()
    ])
    async def market_buy(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        info = TRADEABLE_MATERIALS[material.value]
        ceiling_price = info["market_ceiling_price"]

        # Both of these hit Discord, so they happen before the write lock is
        # taken - see the note in market_sell.
        member_count = await human_member_count(interaction.guild)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id)

        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                current_stock = await get_server_stock(tx, interaction.guild_id, material.value)
                if current_stock < quantity:
                    await interaction.response.send_message(
                        f"The server only has {current_stock} of that in stock.", ephemeral=True
                    )
                    return

                target = target_stock(member_count, material.value)
                total_cost = self._sell_price(ceiling_price, current_stock, quantity, target)

                balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
                if balance < total_cost:
                    await interaction.response.send_message(
                        self._cannot_afford_message(
                            ceiling_price, current_stock, target,
                            quantity, total_cost, balance, currency_emoji,
                        ),
                        ephemeral=True,
                    )
                    return

                await deduct_currency_balance(tx, interaction.guild_id, interaction.user.id, total_cost)
                await deduct_server_stock(tx, interaction.guild_id, material.value, quantity)
                await adjust_user_quantity(tx, interaction.user.id, material.value, quantity)
                await record_burned(tx, interaction.guild_id, total_cost)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "The server's stock or your balance changed while that was going through - "
                "nothing was bought. Try again.",
                ephemeral=True,
            )
            return

        await respond(
            interaction, self.db,
            content=f"Bought {quantity}x **{info['name']}** from the server for {format_currency(total_cost, currency_emoji, True)}.",
        )

    @market_group.command(name="status", description="Show the server's current market prices")
    async def market_status(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        member_count = await human_member_count(interaction.guild)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id) or DEFAULT_CURRENCY_EMOJI

        # One line per material - {emoji} {sell price} {buy price} - instead
        # of three parallel Material/Sell/Buy columns split across separate
        # fields. The material name is deliberately left out: it's
        # proportional-width text, so keeping it would shift the price
        # columns per line and defeat the alignment below. Only the material
        # emoji precedes the prices - custom Discord emoji all render at a
        # fixed size, unlike text - and prices go through
        # format_compact_price so every price is a fixed 5-character-wide
        # string (digits + an optional K/M/B/T suffix). Together that keeps
        # the sell/buy columns visually aligned without needing a code
        # block, which would stop the custom material emoji from rendering.
        lines = []

        for material_id, info in TRADEABLE_MATERIALS.items():
            current_stock: int = await get_server_stock(self.db, interaction.guild_id, material_id)
            ceiling_price = info["market_ceiling_price"]
            target = target_stock(member_count, material_id)
            # SELL = what you receive per unit selling to the server (/market sell).
            # BUY = what you pay per unit buying from the server (/market buy).
            sell_price_each = self._buy_price(ceiling_price, current_stock, 1, target)
            buy_price_each = self._sell_price(ceiling_price, current_stock, 1, target)
            sell_str = f"`{format_compact_price(sell_price_each)}`"
            if current_stock > 0:
                buy_str = f"`{format_compact_price(buy_price_each)}` ({current_stock} in stock)"
            else:
                buy_str = "`N/A`"
            lines.append(f"{info['emoji']} {currency_emoji} {sell_str} {currency_emoji} {buy_str}")

        embed = make_embed("Server Market", MARKET_COLOR)
        # What you can actually spend, next to what everything costs - the two
        # numbers are only useful together, and /balance is a separate command.
        balance = await get_currency_balance(self.db, interaction.guild_id, interaction.user.id)
        embed.description = f"Your balance: {format_currency(balance, currency_emoji)}"
        add_multi_field(embed, "Item · Sell · Buy", lines)
        await respond(interaction, self.db, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
