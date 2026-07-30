"""
cogs/fun.py

Implements:
  - /honk - sends the honk

Commands here are noise for its own sake: nothing in this cog touches the
economy, so it can't be farmed and has no cooldown to speak of.

On "a button that plays the sound": Discord has no API for a message component
that plays audio. The only way a bot can actually make a noise is to join a
voice channel, which needs PyNaCl and ffmpeg installed on the host (neither is,
on this container) and only reaches people already sitting in that channel.
Sending the file instead gets the client's own inline audio player - play
button, scrubber and all - which works for everyone who can see the message,
in every channel, with no extra dependencies.
"""
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond

# Resolved from this file rather than the working directory, because systemd
# starts the bot with whatever WorkingDirectory the unit says.
HONK_PATH = Path(__file__).resolve().parent.parent / "assets" / "honk.flac"


class FunCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @app_commands.command(name="honk", description="Honk.")
    async def honk(self, interaction: discord.Interaction):
        if not HONK_PATH.is_file():
            # The audio lives on disk, so a bad deploy is the one way this
            # fails - say so plainly rather than sending an empty message.
            await interaction.response.send_message(
                "The honk is missing from the server. Tell Isaac.", ephemeral=True
            )
            return

        # No embed - the clip is the whole response, and a title card above the
        # player would only get in the way of it. This is the one command that
        # sends nothing but an attachment. A discord.File is consumed by the
        # send that uses it, so it's built per invocation rather than held on
        # the cog.
        await respond(
            interaction,
            self.db,
            file=discord.File(HONK_PATH, filename="honk.flac"),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(FunCog(bot))
