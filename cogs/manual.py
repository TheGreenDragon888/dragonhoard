"""
cogs/manual.py

The in-Discord manual: /help, /manual and /man all open the same thing. Slash
commands have no aliases the way prefix commands do, so all three are
registered separately and hand off to one implementation - the same approach
/furnace queue takes to /furnace status.

The reader can either jump straight to a section with the `topic` argument or
browse with the dropdown attached to the reply. All the text lives in
data/manual.py; this file only decides how it gets on screen.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond

from data.manual import SECTIONS, DEFAULT_SECTION, build_section_embed

# How long the dropdown stays clickable. Long enough to actually read a page or
# two, short enough that dead views aren't sitting in memory all day.
VIEW_TIMEOUT_SECONDS = 180

TOPIC_CHOICES = [
    app_commands.Choice(name=section.label, value=section.key)
    for section in SECTIONS.values()
]


def _section_options(current_key: str) -> list[discord.SelectOption]:
    """The dropdown's contents, with the page being read marked as selected so
    the reader can see where they are in the book."""
    return [
        discord.SelectOption(
            label=section.label,
            value=section.key,
            description=section.summary,
            emoji=section.emoji,
            default=section.key == current_key,
        )
        for section in SECTIONS.values()
    ]


class ManualView(discord.ui.View):
    """The section dropdown. Editing the message in place means the reader
    browses the whole manual from one reply instead of spamming /help."""

    def __init__(self, section_key: str, user_id: int):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.user_id = user_id
        # Set by the command once the reply exists; on_timeout needs it to
        # reach back and strip the dropdown off the message.
        self.origin: discord.Interaction | None = None

        self.section_select = discord.ui.Select(
            placeholder="Jump to a section...",
            options=_section_options(section_key),
        )
        self.section_select.callback = self._on_select
        self.add_item(self.section_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """On a server that's opted into public replies the manual is visible
        to everyone, so make sure only the person who opened it can drive it."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "That manual isn't yours - run `/help` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        section = SECTIONS[self.section_select.values[0]]
        self.section_select.options = _section_options(section.key)
        await interaction.response.edit_message(embed=build_section_embed(section), view=self)

    async def on_timeout(self):
        if self.origin is None:
            return
        try:
            await self.origin.edit_original_response(view=None)
        except discord.HTTPException:
            # The message was dismissed or the interaction token expired -
            # either way there's no dropdown left to tidy up.
            pass


class ManualCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _manual_impl(self, interaction: discord.Interaction, topic: app_commands.Choice[str] | None):
        section = SECTIONS[topic.value if topic else DEFAULT_SECTION]
        view = ManualView(section_key=section.key, user_id=interaction.user.id)
        await respond(interaction, self.db, embed=build_section_embed(section), view=view)
        view.origin = interaction

    @app_commands.command(name="help", description="Open the Dragonhoard manual")
    @app_commands.describe(topic="Jump straight to one section - leave blank to start at the beginning")
    @app_commands.choices(topic=TOPIC_CHOICES)
    async def help_command(self, interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None):
        await self._manual_impl(interaction, topic)

    @app_commands.command(name="manual", description="Open the Dragonhoard manual")
    @app_commands.describe(topic="Jump straight to one section - leave blank to start at the beginning")
    @app_commands.choices(topic=TOPIC_CHOICES)
    async def manual_command(self, interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None):
        await self._manual_impl(interaction, topic)

    @app_commands.command(name="man", description="Open the Dragonhoard manual")
    @app_commands.describe(topic="Jump straight to one section - leave blank to start at the beginning")
    @app_commands.choices(topic=TOPIC_CHOICES)
    async def man_command(self, interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None):
        await self._manual_impl(interaction, topic)


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the three app commands - do not also call
    # bot.tree.add_command() or they'll double-register.
    await bot.add_cog(ManualCog(bot))
