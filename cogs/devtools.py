"""
cogs/devtools.py

Beta-only developer tooling for giving items directly, without playing the
game to get them. Implements:
  - /devtools give_item <member> <item> <quantity>   - materials/components/etc.
  - /devtools give_drill <member> <drill_type> [level] - an unplaced drill
  - /devtools give_currency <member> <amount>          - this server's currency

This is dangerous by construction - it mints anything, unconditionally - so
it's gated by three independent layers, any one of which alone would be
enough to keep it off the live bot:

  1. This cog is only added to bot.INITIAL_EXTENSIONS when config.IS_BETA is
     true (bot.py). A command that was never registered can never be synced,
     so the live Discord application has no way to expose it even if the
     other two layers were removed.
  2. BetaDevGroup.interaction_check (below) re-checks config.IS_BETA AND
     interaction.guild_id == config.DEV_GUILD_ID on every invocation, in one
     place rather than in each command body. The guild check matters
     separately from the IS_BETA check: docs/testing.md describes copying
     live data into beta for testing, so the beta bot could plausibly be
     invited to more than one guild - without this, any Manage Server member
     on ANY guild the beta bot sits in, including one seeded with real
     copied data, could spawn items.
  3. Each subcommand also requires the Manage Server permission
     (@app_commands.checks.has_permissions(manage_guild=True)), same gate
     /setup uses - so an ordinary member of the dev guild still can't reach
     it.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.db_helpers import (
    adjust_currency_balance,
    adjust_user_quantity,
    ensure_server_row,
    ensure_user_row,
)
from utils.formatting import format_currency
from data.materials import ALL_MATERIALS, DRILLS, get_material_info

# Matches utils/drills.py's own cap on how many autocomplete results Discord
# will render at once.
MAX_AUTOCOMPLETE_RESULTS = 25

# Every giveable material EXCEPT drills - drills aren't a fungible quantity in
# user_materials, they're rows in the drills table with their own level and
# placement state, so give_drill handles them separately.
GIVEABLE_ITEMS = {
    material_id: info for material_id, info in ALL_MATERIALS.items() if material_id not in DRILLS
}


class BetaDevGroup(app_commands.Group):
    """Refuses every subcommand unless this process is the beta bot AND is
    running in its own development guild - layer 2 of this cog's module
    docstring. Group.interaction_check is discord.py's own hook for enforcing
    something once across every subcommand, the same idiom
    utils/channel_guard.py's DragonhoardTree.interaction_check uses at the
    whole-tree level."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not config.IS_BETA or interaction.guild_id != config.DEV_GUILD_ID:
            await interaction.response.send_message(
                "This command only exists on the beta bot's development guild.",
                ephemeral=True,
            )
            return False
        return True


class DevToolsCog(commands.Cog):
    # Must be a class attribute (not a module-level global) - that's how
    # commands.Cog's introspection finds it to auto-register with the tree
    # when bot.add_cog() runs, the same pattern every other *_group in this
    # codebase (e.g. cogs/donate.py's donate_group) already follows.
    devtools_group = BetaDevGroup(
        name="devtools", description="Beta only: give items directly (Manage Server only)"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _currency_emoji(self, guild_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["currency_emoji"] if row else None

    async def _item_autocomplete(self, interaction: discord.Interaction, current: str):
        search = current.strip().lower()
        choices = []
        for material_id, info in GIVEABLE_ITEMS.items():
            if search and search not in info["name"].lower():
                continue
            choices.append(app_commands.Choice(name=info["name"], value=material_id))
            if len(choices) >= MAX_AUTOCOMPLETE_RESULTS:
                break
        return choices

    @devtools_group.command(name="give_item", description="Beta only: give a player materials, components, etc.")
    @app_commands.describe(member="Who to give it to", item="Which item", quantity="How many")
    @app_commands.autocomplete(item=_item_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def give_item(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        item: str,
        quantity: app_commands.Range[int, 1, 1_000_000],
    ):
        # The value a user submits need never have come from the autocomplete
        # list we offered - it always has to be validated on arrival.
        if item not in GIVEABLE_ITEMS:
            await interaction.response.send_message(f"Unknown item `{item}`.", ephemeral=True)
            return
        info = get_material_info(item)

        # An unconditional additive admin mutation, not a read-then-write
        # race - nothing here needs db.transaction()'s guarantees.
        await ensure_user_row(self.db, member.id)
        await adjust_user_quantity(self.db, member.id, item, quantity)

        await interaction.response.send_message(
            f"Gave {member.mention} **{quantity:,}x {info['emoji']} {info['name']}**.",
            ephemeral=True,
        )

    @devtools_group.command(name="give_drill", description="Beta only: give a player an unplaced drill")
    @app_commands.describe(
        member="Who to give it to", drill_type="Which drill type",
        level="Starting level - leave blank for 1",
    )
    @app_commands.choices(drill_type=[
        app_commands.Choice(name=info["name"], value=key) for key, info in DRILLS.items()
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def give_drill(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        drill_type: app_commands.Choice[str],
        level: app_commands.Range[int, 1, 1000] | None = None,
    ):
        level = level or 1
        await ensure_user_row(self.db, member.id)
        # guild_id = NULL means unplaced, in inventory - the same shape
        # cogs/mining.py's free starter-drill grant uses.
        await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type, level) VALUES (NULL, ?, ?, ?)",
            (member.id, drill_type.value, level),
        )
        await interaction.response.send_message(
            f"Gave {member.mention} a **level {level:,} {drill_type.name}** (unplaced, in inventory).",
            ephemeral=True,
        )

    @devtools_group.command(name="give_currency", description="Beta only: give a player this server's currency")
    @app_commands.describe(member="Who to give it to", amount="How much to give")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def give_currency(
        self, interaction: discord.Interaction, member: discord.Member, amount: float,
    ):
        await ensure_server_row(self.db, interaction.guild_id)
        await ensure_user_row(self.db, member.id)
        await adjust_currency_balance(self.db, interaction.guild_id, member.id, amount)

        currency_emoji = await self._currency_emoji(interaction.guild_id)
        await interaction.response.send_message(
            f"Gave {member.mention} **{format_currency(amount, currency_emoji)}**.",
            ephemeral=True,
        )

    @give_item.error
    @give_drill.error
    @give_currency.error
    async def devtools_error_handler(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # Fires when a non-admin (of the dev guild) tries to run one of these.
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to do that.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the devtools_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(DevToolsCog(bot))
