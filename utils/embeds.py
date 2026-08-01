"""
utils/embeds.py

Shared embed-building helpers used by multiple cogs, plus the bot's embed
color palette and standard footer (see docs/stylization.md).
"""
import discord

import config

# Fully saturated brand colors - one per feature area (docs/stylization.md).
DEFAULT_COLOR = discord.Color(0x00FF3C)    # bot settings, manual, and fallback
MINING_COLOR = discord.Color(0x8C00FF)     # purple
INVENTORY_COLOR = discord.Color(0xFF8C00)  # orange (also /balance)
MARKET_COLOR = discord.Color(0xFFE600)     # yellow
FURNACE_COLOR = discord.Color(0xFF0059)    # purple-red
FACTORY_COLOR = discord.Color(0xFF4000)    # red-orange
RECIPE_COLOR = discord.Color(0x00FFEA)     # cyan
PRESS_COLOR = discord.Color(0x0066FF)      # blue

FOOTER_TEXT = f"Dragonhoard by Isaac Day · Version {config.VERSION}"


def make_embed(title: str, color: discord.Color = DEFAULT_COLOR, **kwargs) -> discord.Embed:
    """Every embed the bot sends should be built through this, so the brand
    footer and a palette color are never forgotten."""
    embed = discord.Embed(title=title, color=color, **kwargs)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


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
