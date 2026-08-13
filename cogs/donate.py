"""
cogs/donate.py

Implements:
  - /donate infrastructure <machine> <amount>  - pay into a machine's upgrade fund
  - /donate player <member> <amount>           - hand currency to someone else

Two very different operations sharing a verb, and the difference is the one
worth understanding (docs/market.md section 1):

  INFRASTRUCTURE IS A SINK. The currency leaves circulation entirely, exactly as
  a fee does, and counts toward the machine's level. This is the good kind of
  sink the design doc asks for - money spent on a permanent, visible improvement
  that the whole server gets, rather than an arbitrary toll. It is also the only
  way to level a machine deliberately rather than as a side effect of using it.

  A PLAYER TRANSFER IS NEITHER. The money moves; the supply is unchanged, so it
  is neither minted nor burned and the section 4 ledger correctly ignores it.

Both are per-server, because a server's currency is. You cannot donate across
servers any more than you can spend across them.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond
from utils.embeds import make_embed, DEFAULT_COLOR
from utils.formatting import format_currency
from database.db import InsufficientQuantity
from utils.db_helpers import (
    MACHINES,
    apply_machine_upgrades,
    adjust_currency_balance,
    deduct_currency_balance,
    ensure_server_row,
    ensure_user_row,
    get_currency_balance,
    record_burned,
)
from data.materials import upgrade_threshold

# Below this a donation is pure noise: format_price shows two decimals, so
# anything smaller rounds to nothing on every embed that would report it.
MIN_DONATION = 0.01


class DonateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    donate_group = app_commands.Group(
        name="donate", description="Put your currency into a machine, or hand it to someone else"
    )

    async def _currency_emoji(self, guild_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["currency_emoji"] if row else None

    @donate_group.command(
        name="infrastructure",
        description="Pay into one of this server's machines, levelling it up for everyone",
    )
    @app_commands.describe(
        infrastructure="Which machine to pay into",
        amount="How much of this server's currency to give",
    )
    @app_commands.choices(infrastructure=[
        app_commands.Choice(name=machine, value=machine) for machine in MACHINES
    ])
    async def donate_infrastructure(
        self,
        interaction: discord.Interaction,
        infrastructure: app_commands.Choice[str],
        amount: float,
    ):
        """Adds to a machine's collected fees, which is what levels it up.

        Deliberately the same pot the machine's own fees go into rather than a
        separate donation fund. A machine's level is meant to say how much work
        the server has put through it, and money is money - splitting the two
        would mean a server could see 'donated 500' next to a machine still at
        level 1, which describes nothing anyone wants to know.
        """
        machine = infrastructure.value
        if amount < MIN_DONATION:
            await interaction.response.send_message(
                f"The smallest donation is {MIN_DONATION:.2f}.", ephemeral=True
            )
            return

        currency_emoji = await self._currency_emoji(interaction.guild_id)

        # Charging the donor, banking it and re-levelling the machine commit
        # together: a failure between them either takes the money without
        # crediting it, or levels a machine nobody paid for.
        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_server_row(tx, interaction.guild_id)

                balance = await get_currency_balance(
                    tx, interaction.guild_id, interaction.user.id
                )
                if balance < amount:
                    await interaction.response.send_message(
                        f"You only have {format_currency(balance, currency_emoji)}.",
                        ephemeral=True,
                    )
                    return

                before = await tx.fetchone(
                    f"SELECT {machine}_level AS level FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                await deduct_currency_balance(
                    tx, interaction.guild_id, interaction.user.id, amount
                )
                await tx.execute(
                    f"UPDATE server_config SET {machine}_fees_collected = "
                    f"{machine}_fees_collected + ? WHERE guild_id = ?",
                    (amount, interaction.guild_id),
                )
                # A donation is a sink in exactly the way a fee is - the money
                # is gone, not moved - so it goes through the same ledger.
                await record_burned(tx, interaction.guild_id, amount)
                level = await apply_machine_upgrades(tx, interaction.guild_id, machine)

                collected = await tx.fetchone(
                    f"SELECT {machine}_fees_collected AS collected FROM server_config "
                    f"WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your balance changed while that was going through - nothing was donated. "
                "Try again.",
                ephemeral=True,
            )
            return

        embed = make_embed(f"Donated to the {machine.title()}", DEFAULT_COLOR)
        embed.description = (
            f"{interaction.user.mention} gave "
            f"**{format_currency(amount, currency_emoji)}** to this server's {machine}."
        )
        if level > before["level"]:
            embed.add_field(
                name="⬆️ Levelled Up",
                value=f"The {machine} is now **level {level:,}** (was {before['level']:,}).",
                inline=False,
            )
        next_cost = upgrade_threshold(level + 1)
        embed.add_field(
            name=f"Towards level {level + 1:,}",
            value=(
                f"{format_currency(min(collected['collected'], next_cost), currency_emoji)} / "
                f"{format_currency(next_cost, currency_emoji)}"
            ),
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)

    @donate_group.command(
        name="player", description="Give some of your currency to another member"
    )
    @app_commands.describe(member="Who to give it to", amount="How much to give")
    async def donate_player(
        self, interaction: discord.Interaction, member: discord.Member, amount: float
    ):
        """Moves currency between two members of the same server.

        No fee is taken. A transfer neither mints nor burns, so it does not
        touch the section 4 ledger - the supply is exactly what it was, in
        different hands. Taking a cut would make it a sink, which sounds tidy
        but would mean two players cannot settle a debt without the server
        skimming it, and there is no problem here that needs solving.

        Bots are refused because they have no way to spend it, and self-donation
        because it is a no-op that would otherwise print a receipt claiming
        something happened.
        """
        if member.bot:
            await interaction.response.send_message(
                "Bots have nothing to spend it on.", ephemeral=True
            )
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "That would just move it from your left hand to your right.", ephemeral=True
            )
            return
        if amount < MIN_DONATION:
            await interaction.response.send_message(
                f"The smallest donation is {MIN_DONATION:.2f}.", ephemeral=True
            )
            return

        currency_emoji = await self._currency_emoji(interaction.guild_id)

        try:
            async with self.db.transaction() as tx:
                await ensure_user_row(tx, interaction.user.id)
                await ensure_user_row(tx, member.id)
                await ensure_server_row(tx, interaction.guild_id)

                balance = await get_currency_balance(
                    tx, interaction.guild_id, interaction.user.id
                )
                if balance < amount:
                    await interaction.response.send_message(
                        f"You only have {format_currency(balance, currency_emoji)}.",
                        ephemeral=True,
                    )
                    return

                # Debit first. deduct_currency_balance refuses to overdraw, so
                # if anything has changed since the check above this raises and
                # the credit never runs - the alternative order would create
                # currency out of nothing on exactly that race.
                await deduct_currency_balance(
                    tx, interaction.guild_id, interaction.user.id, amount
                )
                await adjust_currency_balance(
                    tx, interaction.guild_id, member.id, amount
                )
        except InsufficientQuantity:
            await interaction.response.send_message(
                "Your balance changed while that was going through - nothing was sent. "
                "Try again.",
                ephemeral=True,
            )
            return

        await respond(
            interaction, self.db,
            content=(
                f"💸 {interaction.user.mention} gave "
                f"**{format_currency(amount, currency_emoji)}** to {member.mention}."
            ),
        )


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the donate_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(DonateCog(bot))
