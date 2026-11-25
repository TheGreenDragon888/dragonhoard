"""
cogs/exchange.py

Implements:
  - /exchange                     - the exchange page: the rate, the Exchange
                                    Tax both ways, your balances
  - /exchange <amount> [buy|sell] - buy DragonCoin from this server, or sell
                                    it back (1.5.1)

Every rule and every currency movement is utils/exchange.py; this file decides
who may ask and how the answer looks. The design is docs/market.md section 2.

One command with a direction rather than a group of subcommands, by the
owner's choice: a bare /exchange was the specification, and Discord will not
let a command with subcommands be invoked on its own. The amount is in
DragonCoin either way, so buying and selling read the same.

The market's yellow (docs/stylization.md): the exchange is the server trading,
the way /market's trades with the server are.
"""
import discord
from discord import app_commands
from discord.ext import commands

from database.db import InsufficientQuantity
from utils.db_helpers import get_currency_balance
from utils.embeds import MARKET_COLOR, footer_with, make_embed
from utils.exchange import (
    BUY,
    DRAGONCOIN_RATE,
    SELL,
    buy_dragoncoin,
    dragoncoin_cents,
    exchange_tax_rate,
    get_dragoncoin_cents,
    max_buyable_cents,
    quote,
    sell_dragoncoin,
)
from utils.formatting import (
    DEFAULT_CURRENCY_EMOJI,
    format_currency,
    format_dragoncoin,
    format_receipt_price,
)
from utils.government import format_percent
from utils.receipts import build_action_receipt, currency_line, dragoncoin_line
from utils.responses import respond

DIRECTION_CHOICES = [
    app_commands.Choice(name="buy DragonCoin", value=BUY),
    app_commands.Choice(name="sell DragonCoin", value=SELL),
]

RACE_MESSAGE = "Your balance changed while that was going through - nothing was exchanged. Try again."

# For titles and author lines, which render no custom emoji (docs/stylization.md),
# so DragonCoin's own icon (data/emoji.py) cannot go there. Unicode "currency
# exchange", which names the feature rather than the coin.
EXCHANGE_EMOJI = "\U0001F4B1"


class ExchangeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _currency(self, guild_id: int) -> tuple[str | None, str | None]:
        """This server's currency name and emoji, either of which may be unset."""
        row = await self.db.fetchone(
            "SELECT currency_name, currency_emoji FROM server_config WHERE guild_id = ?",
            (guild_id,),
        )
        if row is None:
            return None, None
        return row["currency_name"], row["currency_emoji"]

    async def _refuse(self, interaction: discord.Interaction, message: str):
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="exchange", description="Buy DragonCoin with this server's currency, or sell it back"
    )
    @app_commands.describe(
        amount="How much DragonCoin, to the nearest 0.01. Leave it out to see the rate",
        direction="buy (the default) or sell",
    )
    @app_commands.choices(direction=DIRECTION_CHOICES)
    async def exchange(
        self,
        interaction: discord.Interaction,
        amount: float | None = None,
        direction: app_commands.Choice[str] | None = None,
    ):
        if interaction.guild_id is None:
            await self._refuse(interaction, "Each server runs its own exchange. Use this in a server.")
            return
        if amount is None:
            await self._exchange_page(interaction)
            return

        cents = dragoncoin_cents(amount)
        if cents < 1:
            await self._refuse(interaction, f"The smallest exchange is {format_dragoncoin(1)}.")
            return
        # Said only when it changed something, since a player who typed 1.234
        # and is charged for 1.23 should not have to work out why.
        rounded = abs(amount * 100 - cents) > 1e-9
        if direction is not None and direction.value == SELL:
            await self._sell(interaction, cents, rounded)
        else:
            await self._buy(interaction, cents, rounded)

    async def _exchange_page(self, interaction: discord.Interaction):
        """The rate after tax both ways, and what the player holds. Read-only."""
        guild_id, user_id = interaction.guild_id, interaction.user.id
        currency_name, emoji = await self._currency(guild_id)
        percent = await exchange_tax_rate(self.db, guild_id)
        balance = await get_currency_balance(self.db, guild_id, user_id)
        held = await get_dragoncoin_cents(self.db, user_id)
        one = quote(100, percent)

        # The title is the rate, named with the currency's name since a title
        # renders no custom emoji - the same reason /economy status's is.
        unit = f" {currency_name}" if currency_name else ""
        embed = make_embed(f"1 DragonCoin = {DRAGONCOIN_RATE:,}{unit}", MARKET_COLOR)
        if interaction.guild is not None:
            embed.set_author(name=f"{EXCHANGE_EMOJI} Exchange • {interaction.guild.name}")
        embed.description = f"Exchange Tax: **{format_percent(percent)}**"
        embed.add_field(
            name=f"Buy {format_dragoncoin(100)}",
            value=format_currency(one.cost_cents / 100, emoji, True),
            inline=True,
        )
        embed.add_field(
            name=f"Sell {format_dragoncoin(100)}",
            value=format_currency(one.proceeds_cents / 100, emoji),
            inline=True,
        )
        embed.add_field(
            name="You have",
            value=f"{format_currency(balance, emoji)}\n{format_dragoncoin(held)}",
            inline=True,
        )
        embed.add_field(
            name="You can buy",
            value=format_dragoncoin(max_buyable_cents(balance, percent)),
            inline=True,
        )
        embed.set_footer(text=footer_with("/help exchange"))
        await respond(interaction, self.db, embed=embed)

    async def _buy(self, interaction: discord.Interaction, cents: int, rounded: bool):
        guild_id, user_id = interaction.guild_id, interaction.user.id
        _, emoji = await self._currency(guild_id)
        try:
            async with self.db.transaction() as tx:
                result = await buy_dragoncoin(tx, guild_id, user_id, cents)
        except InsufficientQuantity:
            await self._refuse(interaction, await self._cannot_afford(guild_id, user_id, cents, emoji))
            return

        priced = result.quote
        fields = [("Spent", currency_line(priced.cost_cents / 100, result.balance_after, emoji, gained=False))]
        if priced.tax_cents:
            fields.append(self._tax_field(priced.tax_cents, result.percent, emoji))
        fields.append(("Received", dragoncoin_line(cents, result.dragoncoin_after, gained=True)))
        summary = f"Bought {format_dragoncoin(cents)} from the server."
        if rounded:
            summary += " Amounts round to the nearest 0.01."
        embed = build_action_receipt(
            f"{EXCHANGE_EMOJI} DragonCoin Bought", MARKET_COLOR, summary, fields,
            footer_note="sell it back in any server with /exchange",
        )
        await respond(interaction, self.db, embed=embed)

    async def _sell(self, interaction: discord.Interaction, cents: int, rounded: bool):
        guild_id, user_id = interaction.guild_id, interaction.user.id
        _, emoji = await self._currency(guild_id)
        try:
            async with self.db.transaction() as tx:
                result = await sell_dragoncoin(tx, guild_id, user_id, cents)
        except InsufficientQuantity:
            held = await get_dragoncoin_cents(self.db, user_id)
            if held >= cents:
                await self._refuse(interaction, RACE_MESSAGE)
            else:
                await self._refuse(interaction, f"You only have {format_dragoncoin(held)}.")
            return

        priced = result.quote
        fields = [("Sold", dragoncoin_line(cents, result.dragoncoin_after, gained=False))]
        if priced.tax_cents:
            fields.append(self._tax_field(priced.tax_cents, result.percent, emoji))
        fields.append(
            ("Received", currency_line(priced.proceeds_cents / 100, result.balance_after, emoji, gained=True))
        )
        summary = f"Sold {format_dragoncoin(cents)} to the server."
        if rounded:
            summary += " Amounts round to the nearest 0.01."
        embed = build_action_receipt(f"{EXCHANGE_EMOJI} DragonCoin Sold", MARKET_COLOR, summary, fields)
        await respond(interaction, self.db, embed=embed)

    @staticmethod
    def _tax_field(tax_cents: int, percent: float, emoji: str | None) -> tuple[str, str]:
        """The receipt's Exchange Tax row. In the server's currency on both
        sides, because that is what it is counted and held in
        (utils/exchange.py)."""
        return (
            f"Exchange Tax · {format_percent(percent)}",
            f"{emoji or DEFAULT_CURRENCY_EMOJI} **{format_receipt_price(tax_cents / 100, round_up=True)}** "
            "to the government",
        )

    async def _cannot_afford(self, guild_id: int, user_id: int, cents: int, emoji: str | None) -> str:
        """The refusal for a purchase the player can't cover, naming the most
        they could buy - the same courtesy /market buy's refusal pays, so
        nobody has to bisect by hand. Re-read after the failed transaction; if
        the player can cover it now, their balance moved under the command."""
        percent = await exchange_tax_rate(self.db, guild_id)
        balance = await get_currency_balance(self.db, guild_id, user_id)
        cost = quote(cents, percent).cost_cents / 100
        if balance + 1e-9 >= cost:
            return RACE_MESSAGE
        shortfall = (
            f"That costs {format_currency(cost, emoji, True)}, "
            f"but you only have {format_currency(balance, emoji)}."
        )
        most = max_buyable_cents(balance, percent)
        if most < 1:
            return f"{shortfall} That isn't enough for {format_dragoncoin(1)}."
        return f"{shortfall} You can buy up to {format_dragoncoin(most)}."


async def setup(bot: commands.Bot):
    await bot.add_cog(ExchangeCog(bot))
