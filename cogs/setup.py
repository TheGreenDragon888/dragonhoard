"""
cogs/setup.py

Implements the /setup command group, restricted to members with "Manage
Server" permission, matching the design doc:
  /setup currency <name> <emoji>       - configure this server's currency
  /setup fee <machine> <amt>           - set infrastructure usage fee
  /setup max_queue <machine> <amt>     - set per-user production queue cap,
                                         per machine level
  /setup messages <public|private>     - toggle whether bot responses are public
  /setup channel [channel]             - restrict the bot to one channel, or
                                         leave blank to allow every channel

A "cog" is discord.py's term for a self-contained module of commands/events
that gets loaded into the bot at startup (see bot.py's load_extension calls).
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.formatting import format_currency
from utils.db_helpers import ensure_server_row, machine_label, MACHINES
from utils.embeds import make_embed, DEFAULT_COLOR
from utils.notifications import post_server_notification
from data.materials import BLAST_FURNACE_BATCH_SIZE, effective_max_queue

log = logging.getLogger("dragonhoard")


def setup_guide_embed(guild_name: str) -> discord.Embed:
    """The "get your server going" card, posted once when the bot joins.

    Leads on the currency because it is the one setting that is genuinely
    missing rather than merely defaulted - until it is named, every price in
    the server reads with a placeholder symbol. The other two are here because
    they are the settings a server notices the absence of within a day: fees are
    what level the machines up, and whether replies are public decides whether
    the bot feels like a shared game or a private one.
    """
    embed = make_embed(f"Welcome to Dragonhoard, {guild_name}", DEFAULT_COLOR)
    embed.description = (
        "Everyone can start mining right away - `/mine place` puts a drill in the ground "
        "and `/help` explains the rest. There are three things an admin should set, though, "
        "and the first one matters most."
    )
    embed.add_field(
        name="1. Name your currency  ⭐",
        value=(
            "```/setup currency <name> <emoji>```"
            "Every server has its own money with its own name and symbol, and until you pick "
            "one, prices show a placeholder. It's the only setting with no sensible default - "
            "the other two below already work."
        ),
        inline=False,
    )
    embed.add_field(
        name="2. Set your machine fees",
        value=(
            "```/setup fee <machine> <amount>```"
            "Fees are what level your furnace, blast furnace, factory, press and scrapper "
            "up - a server charging nothing has machines that never improve, and one "
            "charging too much prices its players out. This is the main dial you have."
        ),
        inline=False,
    )
    embed.add_field(
        name="3. Decide how the bot talks",
        value=(
            "```/setup messages <public|private>\n/setup channel [channel]```"
            "Replies are private by default so the bot stays out of the way. If you'd rather "
            "everyone saw each other's hauls, make them public - and `/setup channel` keeps "
            "all of it in one room."
        ),
        inline=False,
    )
    embed.set_footer(text="All /setup commands need the Manage Server permission.")
    return embed


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # the shared Database instance, attached in bot.py

    def _welcome_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Where to post the setup prompt: the server's system channel if the
        bot can talk there, otherwise the first channel it can.

        Deliberately not a DM to whoever has Manage Server. DMs from bots are
        blocked by a great many people and by some servers outright, so the
        message most likely to be silently dropped is the one explaining why
        nothing has a currency symbol. Posting in the server also lets ordinary
        members see what to ask their admins for."""
        candidates = []
        if guild.system_channel is not None:
            candidates.append(guild.system_channel)
        candidates.extend(guild.text_channels)
        for channel in candidates:
            permissions = channel.permissions_for(guild.me)
            if permissions.send_messages and permissions.embed_links:
                return channel
        return None

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Posts the setup prompt once, the first time the bot joins a server.

        Guarded on a stored flag rather than on "is the currency still unset",
        because a server that has decided not to name one should not be nagged
        every time the bot reconnects - and the flag survives a re-invite, which
        is the whole reason server_config rows aren't deleted on removal.
        """
        await ensure_server_row(self.db, guild.id)
        claimed = await self.db.execute_changes(
            "UPDATE server_config SET setup_prompt_sent = 1 "
            "WHERE guild_id = ? AND setup_prompt_sent = 0",
            (guild.id,),
        )
        if not claimed:
            return

        embed = setup_guide_embed(guild.name)
        channel = self._welcome_channel(guild)
        if channel is not None:
            try:
                await channel.send(
                    content="Thanks for the invite! A couple of things to set up 👇",
                    embed=embed,
                )
                return
            except discord.HTTPException:
                log.warning("Could not post the setup prompt in guild %s.", guild.id)

        # Nowhere to post - no channel the bot may speak in, or Discord refused.
        # Falling back to a server notification means the prompt reaches whoever
        # runs the first command instead of being lost entirely.
        await post_server_notification(
            self.db, guild.id, embed.title,
            "An admin needs to run `/setup currency <name> <emoji>` to name this server's "
            "money - until then prices show a placeholder symbol. `/setup fee` sets what the "
            "machines charge (which is what levels them up), and `/setup messages public` "
            "makes the bot reply where everyone can see. All of them need Manage Server.",
        )

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

    @setup_group.command(name="channel", description="Restrict Dragonhoard to one channel, or leave blank to allow every channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="The only channel Dragonhoard will answer in - leave blank to lift the restriction")
    async def setup_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        # One command for both setting and clearing, rather than a separate
        # /setup channel_clear: omitting the argument is the natural way to say
        # "no channel", and the discord.TextChannel type has Discord filter the
        # picker so a category or a voice channel can't be chosen by accident.
        await ensure_server_row(self.db, interaction.guild_id)
        await self.db.execute(
            "UPDATE server_config SET bot_channel_id = ? WHERE guild_id = ?",
            (channel.id if channel else None, interaction.guild_id),
        )
        if channel is None:
            await interaction.response.send_message(
                "✅ Dragonhoard now answers in **every channel** in this server.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Dragonhoard now only answers in {channel.mention} (and threads inside it). "
            f"`/setup` and the manual still work anywhere, so you can always change this back.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Lifts the restriction if the channel it names is deleted.

        Without this, deleting the bot channel would leave every command
        pointing players at a channel that no longer exists - and only /setup
        and the manual would still work. The guard in utils/channel_guard.py
        fails open for the same reason; this is what makes that fallback a
        rare edge case rather than the normal state of a server."""
        await self.db.execute(
            "UPDATE server_config SET bot_channel_id = NULL "
            "WHERE guild_id = ? AND bot_channel_id = ?",
            (channel.guild.id, channel.id),
        )

    # Every machine's settings live in one column per machine, named the same
    # way, so both commands below just prefix the choice value. Derived from
    # MACHINES rather than written out, so a new machine appears in both
    # commands the moment it's added there. The name shown is prose and the
    # value behind it is the column prefix, which is the only reason a machine
    # whose id has an underscore in it reads properly here.
    INFRASTRUCTURE_CHOICES = [
        app_commands.Choice(name=machine_label(machine), value=machine) for machine in MACHINES
    ]

    # What one unit of a machine's fee actually buys, and what its queue cap
    # counts in. Both are an item for most machines: the press charges per
    # ruby-equivalent of press time instead, and the blast furnace charges and
    # queues in batches of BLAST_FURNACE_BATCH_SIZE items. A confirmation that
    # said "per item" for either would understate the real cost by a factor of
    # nine or a hundred.
    FEE_UNITS = {"press": "press-day", "blast_furnace": f"batch of {BLAST_FURNACE_BATCH_SIZE}"}
    QUEUE_UNITS = {"blast_furnace": "batch"}

    @setup_group.command(name="fee", description="Set a fee (in server currency) to use a machine")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(infrastructure="Which infrastructure to set a fee for", amount="Fee per item produced (per press-day for the press, per batch for the blast furnace)")
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
        # (nine press-days) costs nine times what this number says; the blast
        # furnace charges per batch of a hundred items.
        unit = self.FEE_UNITS.get(infrastructure.value, "item")
        await interaction.response.send_message(
            f"✅ {machine_label(infrastructure.value).title()} fee set to "
            f"{format_currency(amount, currency_emoji)} per {unit}.",
            ephemeral=True,
        )

    @setup_group.command(name="max_queue", description="Set the maximum queued items per user, per level, for a machine")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(infrastructure="Which infrastructure to set a queue limit for", amount="Maximum queued items (batches, for the blast furnace) per user, per machine level (1-50)")
    @app_commands.choices(infrastructure=INFRASTRUCTURE_CHOICES)
    async def setup_max_queue(self, interaction: discord.Interaction, infrastructure: app_commands.Choice[str], amount: app_commands.Range[int, 1, 50]):
        await ensure_server_row(self.db, interaction.guild_id)
        await self.db.execute(
            f"UPDATE server_config SET {infrastructure.value}_max_queue = ? WHERE guild_id = ?",
            (amount, interaction.guild_id),
        )
        # This sets the BASE. What's enforced is the base times the machine's
        # level (see effective_max_queue), so the confirmation has to quote the
        # number players will actually run into - a manager who set 5 and then
        # watched someone queue 15 would otherwise reasonably read it as broken.
        cfg = await self.db.fetchone(
            f"SELECT {infrastructure.value}_level AS level FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["level"] if cfg else 1
        unit = self.QUEUE_UNITS.get(infrastructure.value, "item")
        await interaction.response.send_message(
            f"✅ {machine_label(infrastructure.value).title()} max queue set to **{amount}** "
            f"{unit}s per user, per level - **{effective_max_queue(amount, level):,}** at its "
            f"current level {level:,}.",
            ephemeral=True,
        )

    @setup_messages.error
    @setup_currency.error
    @setup_channel.error
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
