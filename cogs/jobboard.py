"""
cogs/jobboard.py

Implements /jobboard - today's task, what it pays, and how far into it you are.

Named /jobboard rather than /jobs on purpose. "Job" already means "a queued
production job" everywhere else in this bot, including in what players actually
read: /furnace status says "Queue - 12 items / 3 jobs". Anyone who has used a
machine would reasonably expect /jobs to list theirs. /market jobs was the
other candidate and hides a daily task under a trading menu, where nobody who
hasn't already found it will look.

The mechanics all live in utils/job_board.py; this file only decides how the
board gets on screen. Completing a task happens through /market sell, not here.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond
from utils.embeds import make_embed, JOBBOARD_COLOR
from utils.formatting import format_currency, format_relative_timestamp
from utils.guild_helpers import human_member_count
from utils.db_helpers import ensure_server_row
from utils.job_board import ensure_todays_job, get_progress, hours_until_reset

from data.materials import get_material_info


class JobBoardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @app_commands.command(name="jobboard", description="Today's job for this server, and what it pays")
    async def jobboard(self, interaction: discord.Interaction):
        # Both of these reach Discord, so they happen before the write lock is
        # taken - see the note on Database.transaction.
        member_count = await human_member_count(interaction.guild)
        currency_emoji = await self._currency_emoji(interaction.guild_id)

        async with self.db.transaction() as tx:
            await ensure_server_row(tx, interaction.guild_id)
            job = await ensure_todays_job(tx, interaction.guild_id, member_count)

        progress = await get_progress(self.db, interaction.guild_id, interaction.user.id, job["job_date"])
        sold = progress["sold"] if progress else 0
        completions = progress["claims_paid"] if progress else 0
        # Progress toward the NEXT completion, not toward the day's only one:
        # the task repeats, so what is left of the current lap is the number a
        # player is actually working against.
        toward_next = sold - completions * job["quantity"]

        info = get_material_info(job["material_id"])
        embed = make_embed("📋 Job Board", JOBBOARD_COLOR)
        embed.description = (
            f"The server is short on {info['emoji']} **{info['name']}** and is paying a bonus "
            f"for it today, on top of what the market already pays.\n"
            f"A new job is posted {format_relative_timestamp(hours_until_reset())}."
        )
        embed.add_field(
            name="Today's Job",
            value=f"Sell {info['emoji']} **{job['quantity']:,} {info['name']}** to the server with `/market sell`.",
            inline=False,
        )
        embed.add_field(
            name="Bonus",
            value=f"{format_currency(job['reward'], currency_emoji)}, every time you finish it.",
            inline=True,
        )
        # Two numbers rather than one, because the task no longer has a done
        # state: how many times it has been finished, and how far into the next
        # one this player is.
        status = f"**{toward_next:,}/{job['quantity']:,}** sold."
        if completions:
            paid = format_currency(job["reward"] * completions, currency_emoji)
            times = "once" if completions == 1 else f"**{completions:,}** times"
            status = f"✅ Finished {times} for {paid}.\n{status}"
        embed.add_field(name="Your Progress", value=status, inline=True)

        await respond(interaction, self.db, embed=embed)

    async def _currency_emoji(self, guild_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["currency_emoji"] if row else None


async def setup(bot: commands.Bot):
    await bot.add_cog(JobBoardCog(bot))
