"""
utils/embeds.py

Shared embed-building helpers used by multiple cogs, plus the bot's embed
color palette and standard footer (see docs/stylization.md).
"""
import discord

import config
from utils.formatting import format_currency, format_duration
from data.materials import effective_max_queue

# Fully saturated brand colors - one per feature area (docs/stylization.md).
DEFAULT_COLOR = discord.Color(0x00FF3C)    # bot settings, manual, and fallback
MINING_COLOR = discord.Color(0x8C00FF)     # purple
INVENTORY_COLOR = discord.Color(0xFF8C00)  # orange (also /balance)
MARKET_COLOR = discord.Color(0xFFE600)     # yellow
FURNACE_COLOR = discord.Color(0xFF0059)    # purple-red
FACTORY_COLOR = discord.Color(0xFF4000)    # red-orange
RECIPE_COLOR = discord.Color(0x00FFEA)     # cyan
PRESS_COLOR = discord.Color(0x0066FF)       # blue
SCRAPPER_COLOR = discord.Color(0x9EFF00)   # chartreuse
JOBBOARD_COLOR = discord.Color(0xFF00AA)   # magenta
# The two notification feeds (utils/notifications.py). They get separate colors
# because they carry different authority - one is the bot announcing something
# about itself to everybody, the other is one server's own business - and a
# player should be able to tell which without reading a word.
GLOBAL_NOTICE_COLOR = discord.Color(0x00FF00)  # green
SERVER_NOTICE_COLOR = discord.Color(0xFFFF00)  # yellow

FOOTER_TEXT = f"Dragonhoard by Isaac Day · Version {config.VERSION}"


def make_embed(title: str, color: discord.Color = DEFAULT_COLOR, **kwargs) -> discord.Embed:
    """Every embed the bot sends should be built through this, so the brand
    footer and a palette color are never forgotten."""
    embed = discord.Embed(title=title, color=color, **kwargs)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def make_infrastructure_embed(
    *,
    emoji: str,
    name: str,
    color: discord.Color,
    level: int,
    speed_text: str,
    fees_collected: float,
    upgrade_cost: float,
    currency_emoji: str | None,
) -> discord.Embed:
    """The shared shell of the /furnace, /factory and /press status embeds.

    All three used to open with six inline fields - Level, Speed, Queue Limit,
    Fee, Pending, Queue Finishes - four of which were one number each. The
    embed's own furniture holds those for free: what the machine IS goes in the
    author line, what it DOES goes in the title, and how close it is to the
    next level is one sentence of description. That leaves the fields for the
    two settings a server manager actually changes, and the queue.

    `emoji` has to be a unicode glyph, not a custom <:Name:ID> - Discord
    renders custom emoji in descriptions and field values but not in author
    lines. Same constraint rules out a relative timestamp in the title.
    """
    embed = discord.Embed(title=speed_text, color=color)
    embed.set_author(name=f"{emoji} {name} • Level {level}")
    # Levels are unbounded - each threshold is UPGRADE_THRESHOLD_STEP times the
    # last, which is what keeps them in check - so there is always a next one to
    # show. The collected
    # total is clamped so a machine that has banked far past the threshold reads
    # as "5.00 / 5.00" rather than overshooting its own progress bar.
    embed.description = (
        f"{format_currency(min(fees_collected, upgrade_cost), currency_emoji)} / "
        f"{format_currency(upgrade_cost, currency_emoji)} to level {level + 1}"
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def queue_field_name(items: int, jobs: int, wait_hours: float) -> str:
    """The queue field's heading, which carries the counts and the total wait
    that used to be their own "Pending" and "Queue Finishes" fields.

    format_duration rather than a relative timestamp: this is a field NAME, and
    Discord doesn't render <t:...> markup there."""
    if not jobs:
        return "Queue • empty"
    item_word = "item" if items == 1 else "items"
    job_word = "job" if jobs == 1 else "jobs"
    return f"Queue • {items:,} {item_word} / {jobs:,} {job_word} ({format_duration(wait_hours)} wait)"


def queue_limit_field_value(base: int, level: int) -> str:
    """The "Queue Limit" field on every infrastructure status embed.

    Shows the cap that is actually enforced, with the arithmetic underneath it.
    The multiplication is otherwise invisible - a manager who set 5 with
    /setup max_queue and is being allowed 15 has no way to tell that from a
    bug - and spelling it out is also what advertises that levelling a machine
    buys queue room as well as speed."""
    effective = effective_max_queue(base, level)
    if level <= 1:
        return f"**{effective:,}** items per user"
    return f"**{effective:,}** items per user\n({base:,} × level {level:,})"


def job_owner_label(user_id: int) -> str:
    """Who queued a job. A raw mention: embeds never fire a notification, and
    the client resolves it whether or not that member happens to be cached."""
    return f"<@{user_id}>"


def add_multi_field(embed: discord.Embed, name: str, lines: list[str], inline: bool = False, empty_text: str = "None"):
    """Adds a list of lines to an embed as a field, splitting across
    multiple fields if the combined text would exceed Discord's 1024
    character-per-field limit."""
    if not lines:
        embed.add_field(name=name, value=empty_text, inline=inline)
        return

    chunk_lines: list[str] = []
    chunk_len = 0
    first = True
    for line in lines:
        added_len = len(line) + (1 if chunk_lines else 0)  # +1 accounts for the newline joining lines
        if chunk_lines and chunk_len + added_len > 1024:
            embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk_lines), inline=inline)
            first = False
            chunk_lines = [line]
            chunk_len = len(line)
        else:
            chunk_lines.append(line)
            chunk_len += added_len
    if chunk_lines:
        embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk_lines), inline=inline)
