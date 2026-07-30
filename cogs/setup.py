"""
cogs/setup.py

Implements the /setup command group, restricted to members with "Manage
Server" permission, matching the design doc:
  /setup currency <name> <emoji>       - configure this server's currency
  /setup fee <furnace|factory> <amt>   - set infrastructure usage fee
  /setup max_queue <furnace|factory> <amt> - set per-user production queue cap
  /setup messages <public|private>     - toggle whether bot responses are public

A "cog" is discord.py's term for a self-contained module of commands/events
that gets loaded into the bot at startup (see bot.py's load_extension calls).
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.formatting import format_currency
from utils.db_helpers import ensure_server_row


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # the shared Database instance, attached in bot.py

    # A "group" bundles related slash commands under one parent, so users
    # see them in Discord as /setup currency, /setup fee, /setup messages.
    setup_group = app_commands.Group(
        name="setup", description="Server configuration (requires Manage Server permission)"
    )

    @setup_group.command(name="messages", description="Set whether the bot's responses are public or private in this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(visibility="Public responses are visible to everyone; private ones only to the command's user")
    @app_commands.choices(visibility=[
        app_commands.Choice(name="public", value="public"),
        app_commands.Choice(name="private", value="private"),
    ])
    async def setup_messages(self, interaction: discord.Interaction, visibility: app_commands.Choice[str]):
        await ensure_server_row(self.db, interaction.guild_id)
        public_messages = 1 if visibility.value == "public" else 0
        await self.db.execute(
            "UPDATE server_config SET public_messages = ? WHERE guild_id = ?",
            (public_messages, interaction.guild_id),
        )
        await interaction.response.send_message(
            f"✅ Bot responses in **{interaction.guild.name}** are now **{visibility.value}**.",
            ephemeral=True,
        )

    @setup_group.command(name="currency", description="Set this server's custom currency name and emoji")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(name="Currency name, e.g. 'Gold'", emoji="Emoji to represent the currency, e.g. 🪙")
    async def setup_currency(self, interaction: discord.Interaction, name: str, emoji: str):
        await ensure_server_row(self.db, interaction.guild_id)
        await self.db.execute(
            "UPDATE server_config SET currency_name = ?, currency_emoji = ? WHERE guild_id = ?",
            (name, emoji, interaction.guild_id),
        )
        await interaction.response.send_message(
            f"✅ This server's currency is now **{name}** {emoji}.",
            ephemeral=True,
        )

    # Every machine's settings live in one column per machine, named the same
    # way, so both commands below just prefix the choice value.
    INFRASTRUCTURE_CHOICES = [
        app_commands.Choice(name="furnace", value="furnace"),
        app_commands.Choice(name="factory", value="factory"),
        app_commands.Choice(name="press", value="press"),
    ]

    @setup_group.command(name="fee", description="Set a fee (in server currency) to use a machine")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(infrastructure="Which infrastructure to set a fee for", amount="Fee per item produced (per press-day for the press)")
    @app_commands.choices(infrastructure=INFRASTRUCTURE_CHOICES)
    async def setup_fee(self, interaction: discord.Interaction, infrastructure: app_commands.Choice[str], amount: float):
        if amount < 0:
            await interaction.response.send_message("Fee can't be negative.", ephemeral=True)
            return
        await ensure_server_row(self.db, interaction.guild_id)
        await self.db.execute(
            f"UPDATE server_config SET {infrastructure.value}_fee = ? WHERE guild_id = ?",
            (amount, interaction.guild_id),
        )
        cfg = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (interaction.guild_id,)
        )
        currency_emoji = cfg["currency_emoji"] if cfg else None
        # The press charges per press-day rather than per item, so a diamond
        # (nine press-days) costs nine times what this number says.
        unit = "press-day" if infrastructure.value == "press" else "item"
        await interaction.response.send_message(
            f"✅ {infrastructure.value.title()} fee set to {format_currency(amount, currency_emoji)} per {unit}.",
            ephemeral=True,
        )

    @setup_group.command(name="max_queue", description="Set the maximum queued items per user for a machine")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(infrastructure="Which infrastructure to set a queue limit for", amount="Maximum queued items per user (1-50)")
    @app_commands.choices(infrastructure=INFRASTRUCTURE_CHOICES)
    async def setup_max_queue(self, interaction: discord.Interaction, infrastructure: app_commands.Choice[str], amount: app_commands.Range[int, 1, 50]):
        await ensure_server_row(self.db, interaction.guild_id)
        await self.db.execute(
            f"UPDATE server_config SET {infrastructure.value}_max_queue = ? WHERE guild_id = ?",
            (amount, interaction.guild_id),
        )
        await interaction.response.send_message(
            f"✅ {infrastructure.value.title()} max queue set to **{amount}** items per user.",
            ephemeral=True,
        )

    @setup_messages.error
    @setup_currency.error
    @setup_fee.error
    @setup_max_queue.error
    async def setup_error_handler(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # Fires when a non-admin tries to run a /setup command.
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to do that.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    """The special function discord.py looks for when loading this file as
    an extension (see bot.py: await bot.load_extension('cogs.setup')).
    Note: bot.add_cog() automatically registers any app_commands.Group
    class attributes on the cog - no need to call bot.tree.add_command()
    separately (doing so causes a "CommandAlreadyRegistered" error)."""
    await bot.add_cog(SetupCog(bot))
