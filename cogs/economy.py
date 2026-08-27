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
materials from users at that material's market price and sells them back at
MARKET_BUY_MARKUP times the same figure, constrained by what it actually has
in stock, since the server can't sell what it never acquired.

Both rates are STATIC as of 1.3 (data/materials.py: sale_unit_price,
purchase_unit_price). They used to decay with the server's stock - full price
at zero stock, half at the server's "target stock", tapering toward zero
beyond it - which meant a price was only knowable by running the command, a
large sale paid a rate the player could not have worked out in advance, and
every figure quoted anywhere had to say which stock level it was quoted at.
The stock itself still matters: it is what the server can sell back, what the
furnace's auto-smelt reads, and what the job board picks a material from.

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
from utils.formatting import format_currency, format_price, DEFAULT_CURRENCY_EMOJI
from utils.receipts import build_market_receipt_embed
from utils.guild_helpers import human_member_count
from utils.job_board import credit_job_progress, ensure_todays_job
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
    purchase_total,
    purchase_unit_price,
    sale_total,
    sale_unit_price,
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


# The most one /market command will move. Raised from 1,000 in 1.3, alongside
# static prices: the old limit was partly a pricing guard - a big enough sale
# used to walk the price down under itself - and partly a display one. Neither
# applies to a flat rate, and a drill wearing a Diamond Container holds 32,000
# items (data/materials.py: effective_capacity), which is thirty-two commands
# to sell at the old limit even if it were all one material.
#
# A million rather than no limit at all because discord.py's Range is what
# produces the "too large" error client-side, before the command is even sent;
# without one the rejection would come from the "you only have N of that"
# branch after a round trip, which reads as a bug rather than as a limit.
MAX_MARKET_QUANTITY = 1_000_000


def max_affordable(unit_cost: float, balance: float) -> int:
    """How many units a balance covers at a flat per-unit cost.

    Exact arithmetic rather than a search, because the per-unit price does not
    move with quantity (or with anything else) - so the total is simply linear.

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

    def _cannot_afford_message(
        self,
        material_id: str,
        quantity: int,
        total_cost: float,
        balance: float,
        currency_emoji: str | None,
    ) -> str:
        """The /market buy rejection, which also names the largest quantity the
        user COULD buy right now. Being told only "you can't afford this" leaves
        them bisecting by hand, which is a pointless thing to make someone do
        when the answer is one division away."""
        unit_cost = purchase_unit_price(material_id)
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
            f"for {format_currency(purchase_total(material_id, affordable), currency_emoji, True)}."
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
                cells.append(f"{info['emoji']} {quantity:,}")
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
        # Only UNPLACED, UNLOCKED drills are listed. A placed drill isn't in
        # your inventory in any sense that matters - you can't craft with it,
        # fit a container to it or place it somewhere else - so listing it
        # here made the field read as a roster rather than as stock on hand.
        # /mine status is where a server's placed drills are shown. A locked
        # drill (queued for an upgrade or already handed to the scrapper) is
        # excluded for the same reason: locked_job_id is what stops it being
        # placed or modified until that job finishes, so it's just as
        # unavailable as a placed one in the meantime.
        drill_rows = await self.db.fetchall(
            "SELECT * FROM drills WHERE owner_id = ? AND guild_id IS NULL "
            "AND locked_job_id IS NULL "
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
    async def market_sell(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, MAX_MARKET_QUANTITY]):
        info = TRADEABLE_MATERIALS[material.value]
        total_value = sale_total(material.value, quantity)

        # Read the member count before opening the transaction: it can chunk
        # the guild over the gateway, and holding the write lock across a
        # network round-trip would stall every other command in the bot. The
        # price no longer needs it; the job board still does, to weigh which
        # material a server of this size is short of.
        member_count = await human_member_count(interaction.guild)

        # The payout no longer depends on anything readable - the price is a
        # constant - but the INVENTORY still has to be checked and deducted
        # atomically. Run twice concurrently without a transaction and both
        # invocations pass the "do you have enough" check against the same
        # quantity, and the player is paid twice for one stack.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                # Post the day's job BEFORE this sale moves the stock. Its
                # size and bonus no longer depend on stock at all, but WHICH
                # material it picks still does - so posting it afterwards
                # would let whoever sells first each day steer the board away
                # from what they had just delivered. Idempotent, so the
                # credit_job_progress call below finds this same row.
                await ensure_todays_job(tx, interaction.guild_id, member_count)

                have = await get_user_quantity(tx, interaction.user.id, material.value)
                if have < quantity:
                    await interaction.response.send_message(
                        f"You only have {have} of that item.", ephemeral=True
                    )
                    return

                await deduct_user_quantity(tx, interaction.user.id, material.value, quantity)
                await adjust_server_stock(tx, interaction.guild_id, material.value, quantity)
                await adjust_currency_balance(tx, interaction.guild_id, interaction.user.id, total_value)
                await record_minted(tx, interaction.guild_id, total_value)

                # The day's job board task, if this sale counts towards it -
                # in the same transaction as the sale, so the bonus and the
                # sale that earned it can never come apart. One sale can
                # complete the task any number of times; completions is how
                # many, and bonus is the total for all of them. Both are 0
                # unless this sale finished at least one. See
                # utils/job_board.py.
                bonus, completions = await credit_job_progress(
                    tx, interaction.guild_id, interaction.user.id,
                    material.value, quantity, member_count,
                )

                # Read inside the same transaction so the receipt's totals
                # can never be stale relative to the writes above.
                remaining = await get_user_quantity(tx, interaction.user.id, material.value)
                new_balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your inventory changed while that was going through - nothing was sold. Try again.",
                ephemeral=True,
            )
            return

        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        description = (
            f"Sold {info['emoji']} **{quantity:,}x {info['name']}** to the server for "
            f"{format_currency(total_value, currency_emoji)}."
        )
        if completions > 0:
            # A second line on the same description rather than a separate
            # field - one sale triggering two payouts is a single event, not
            # two, and this keeps the receipt's shape identical to /market
            # buy's even when a job board bonus lands.
            times = "" if completions == 1 else f" **{completions:,}** times"
            description += (
                f"\nThat finished today's job board task{times} for a bonus of "
                f"{format_currency(bonus, currency_emoji)}."
            )
        embed = build_market_receipt_embed(
            title="🪙 Sale Receipt",
            color=MARKET_COLOR,
            description=description,
            material_field="Sold",
            material_id=material.value,
            quantity=quantity,
            material_remaining=remaining,
            currency_field="Received",
            # Includes the job board bonus, if one landed - balance_after
            # already reflects both credits, and this field is the amount
            # that moved to get there, not just the sale's own half of it.
            currency_amount=total_value + bonus,
            balance_after=new_balance,
            currency_emoji=currency_emoji,
            round_up_currency=False,
        )
        await respond(interaction, self.db, embed=embed)

    @market_group.command(name="buy", description="Buy materials from the server's stock")
    @app_commands.describe(material="What to buy", quantity="How many to buy")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in TRADEABLE_MATERIALS.items()
    ])
    async def market_buy(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, MAX_MARKET_QUANTITY]):
        info = TRADEABLE_MATERIALS[material.value]
        total_cost = purchase_total(material.value, quantity)

        # This hits Discord, so it happens before the write lock is taken -
        # see the note in market_sell.
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

                balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
                if balance < total_cost:
                    await interaction.response.send_message(
                        self._cannot_afford_message(
                            material.value, quantity, total_cost, balance, currency_emoji,
                        ),
                        ephemeral=True,
                    )
                    return

                await deduct_currency_balance(tx, interaction.guild_id, interaction.user.id, total_cost)
                await deduct_server_stock(tx, interaction.guild_id, material.value, quantity)
                await adjust_user_quantity(tx, interaction.user.id, material.value, quantity)
                await record_burned(tx, interaction.guild_id, total_cost)

                # Read inside the same transaction so the receipt's totals
                # can never be stale relative to the writes above.
                remaining = await get_user_quantity(tx, interaction.user.id, material.value)
                balance_after = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
        except InsufficientQuantity:
            await interaction.response.send_message(
                "The server's stock or your balance changed while that was going through - "
                "nothing was bought. Try again.",
                ephemeral=True,
            )
            return

        embed = build_market_receipt_embed(
            title="🛒 Purchase Receipt",
            color=MARKET_COLOR,
            description=(
                f"Bought {info['emoji']} **{quantity:,}x {info['name']}** from the server for "
                f"{format_currency(total_cost, currency_emoji, True)}."
            ),
            material_field="Bought",
            material_id=material.value,
            quantity=quantity,
            material_remaining=remaining,
            currency_field="Spent",
            currency_amount=total_cost,
            balance_after=balance_after,
            currency_emoji=currency_emoji,
            round_up_currency=True,
        )
        await respond(interaction, self.db, embed=embed)

    @market_group.command(name="status", description="Show the server's current market prices")
    async def market_status(self, interaction: discord.Interaction):
        await ensure_server_row(self.db, interaction.guild_id)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id) or DEFAULT_CURRENCY_EMOJI

        # One line per material - {emoji} {sell price} {buy price} - instead
        # of three parallel Material/Sell/Buy columns split across separate
        # fields. The material name is deliberately left out: it's
        # proportional-width text, so keeping it would shift the price columns
        # per line and defeat the alignment. Only the material emoji precedes
        # the prices, and custom Discord emoji all render at a fixed size
        # unlike text.
        #
        # The prices themselves need nothing but format_price now. Every one is
        # a whole number of cents under a single currency unit, so they are all
        # four characters wide on their own and the columns line up for free.
        # This used to take a dedicated formatter that padded every price to a
        # fixed five significant digits with a K/M/B/T suffix, which existed
        # because a price could be any magnitude and could be worth a small
        # fraction of a cent - it rendered iron ore's 0.01 as "0.0100". Neither
        # is true of a static cent price. A material priced at 1.00 or more
        # would simply take one more character than its neighbours, which costs
        # a line of alignment rather than breaking anything.
        lines = []

        for material_id, info in TRADEABLE_MATERIALS.items():
            # The prices are the same on every server and every day (1.3), so
            # this is now the stock lookup rather than the price lookup - the
            # server can only sell back what it is holding.
            current_stock: int = await get_server_stock(self.db, interaction.guild_id, material_id)
            # SELL = what you receive per unit selling to the server (/market sell).
            # BUY = what you pay per unit buying from the server (/market buy).
            sell_str = f"`{format_price(sale_unit_price(material_id))}`"
            if current_stock > 0:
                buy_str = (
                    f"`{format_price(purchase_unit_price(material_id))}` "
                    f"({current_stock:,} in stock)"
                )
            else:
                buy_str = "`N/A`"
            lines.append(f"{info['emoji']} {currency_emoji} {sell_str} {currency_emoji} {buy_str}")

        embed = make_embed("Server Market", MARKET_COLOR)
        # What you can actually spend, next to what everything costs - the two
        # numbers are only useful together, and /balance is a separate command.
        balance = await get_currency_balance(self.db, interaction.guild_id, interaction.user.id)
        embed.description = (
            f"Your balance: {format_currency(balance, currency_emoji)}"
        )
        add_multi_field(embed, "Item · Sell · Buy", lines)
        await respond(interaction, self.db, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
