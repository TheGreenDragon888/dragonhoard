"""
cogs/changelog.py

Implements /changelog - what changed in each release, newest first.

Structurally the same as cogs/manual.py, deliberately: a dropdown that edits
the reply in place so a reader can browse versions from one message instead of
running the command again. All the text lives in data/changelog.py; this file
only decides how it gets on screen.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond

from data.changelog import (
    VERSIONS,
    LATEST_VERSION,
    MAX_SELECTABLE_VERSIONS,
    build_version_embed,
)

# Same as the manual's: long enough to read a release or two, short enough that
# dead views aren't sitting in memory all day.
VIEW_TIMEOUT_SECONDS = 180

# Newest first, capped at what a Discord select menu will hold.
_SELECTABLE = list(VERSIONS.values())[:MAX_SELECTABLE_VERSIONS]

VERSION_CHOICES = [
    app_commands.Choice(name=f"Version {version.version}", value=version.version)
    for version in _SELECTABLE
]


def _version_options(current_version: str) -> list[discord.SelectOption]:
    """The dropdown's contents, with the release being read marked as selected
    so the reader can see where they are."""
    return [
        discord.SelectOption(
            label=f"Version {version.version}",
            value=version.version,
            description=version.summary,
            emoji=version.emoji,
            default=version.version == current_version,
        )
        for version in _SELECTABLE
    ]


class ChangelogView(discord.ui.View):
    def __init__(self, version: str, user_id: int):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.user_id = user_id
        # Set by the command once the reply exists; on_timeout needs it to
        # reach back and strip the dropdown off the message.
        self.origin: discord.Interaction | None = None

        self.version_select = discord.ui.Select(
            placeholder="Jump to a version...",
            options=_version_options(version),
        )
        self.version_select.callback = self._on_select
        self.add_item(self.version_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """On a server that's opted into public replies this is visible to
        everyone, so make sure only the person who opened it can drive it."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "That changelog isn't yours - run `/changelog` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        version = VERSIONS[self.version_select.values[0]]
        self.version_select.options = _version_options(version.version)
        await interaction.response.edit_message(embed=build_version_embed(version), view=self)

    async def on_timeout(self):
        if self.origin is None:
            return
        try:
            await self.origin.edit_original_response(view=None)
        except discord.HTTPException:
            # The message was dismissed or the interaction token expired -
            # either way there's no dropdown left to tidy up.
            pass


class ChangelogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @app_commands.command(name="changelog", description="What changed in each version of Dragonhoard")
    @app_commands.describe(version="Jump straight to one release - leave blank for the newest")
    @app_commands.choices(version=VERSION_CHOICES)
    async def changelog(self, interaction: discord.Interaction, version: app_commands.Choice[str] | None = None):
        entry = VERSIONS[version.value if version else LATEST_VERSION]
        view = ChangelogView(version=entry.version, user_id=interaction.user.id)
        await respond(interaction, self.db, embed=build_version_embed(entry), view=view)
        view.origin = interaction


async def setup(bot: commands.Bot):
    await bot.add_cog(ChangelogCog(bot))
