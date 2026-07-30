"""
cogs/recipe.py

Implements the /recipe book: /recipe factory and /recipe furnace list every
recipe for their machine. Recipes used to be a field on the /factory status
and /furnace status embeds; they live here now so those embeds stay focused
on queue and upgrade info.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.embeds import make_embed, add_multi_field, RECIPE_COLOR
from utils.responses import respond
from utils.db_helpers import build_recipe_lines

from data.materials import (
    COMPONENT_MATERIALS,
    DRILLS,
    DRILL_BITS,
    DRILL_COMPONENTS,
    SMELTED_MATERIALS,
    STORAGE_CONTAINERS,
    UPGRADE_MATERIALS,
    PRESS_RECIPES,
    BASE_STORAGE_CAPACITY,
    effective_capacity,
)

# DRILL_BITS/DRILL_COMPONENTS are id tuples; the book needs the recipe dicts.
COMPONENT_RECIPES = {k: COMPONENT_MATERIALS[k] for k in DRILL_COMPONENTS}
DRILL_BIT_RECIPES = {k: COMPONENT_MATERIALS[k] for k in DRILL_BITS}


def _container_lines() -> list[str]:
    """Container recipes with the number that actually matters attached - a
    container is bought for its capacity, which the bare recipe doesn't show."""
    return [
        f"{line} [holds {effective_capacity(container_id)}]"
        for container_id, line in zip(STORAGE_CONTAINERS, build_recipe_lines(STORAGE_CONTAINERS))
    ]


class RecipeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    recipe_group = app_commands.Group(name="recipe", description="Browse the recipe book")

    @recipe_group.command(name="factory", description="List every factory recipe")
    async def recipe_factory(self, interaction: discord.Interaction):
        embed = make_embed("🏭 Factory Recipes", RECIPE_COLOR)
        add_multi_field(embed, "Components", build_recipe_lines(COMPONENT_RECIPES))
        add_multi_field(embed, "Drill Bits", build_recipe_lines(DRILL_BIT_RECIPES))
        add_multi_field(embed, "Drills", build_recipe_lines(DRILLS))
        add_multi_field(embed, "Storage Containers", _container_lines())
        add_multi_field(embed, "Upgrades", build_recipe_lines(UPGRADE_MATERIALS))
        embed.add_field(
            name="Drill Upgrades",
            value=(
                f"Every drill holds {BASE_STORAGE_CAPACITY} on its own and mines faster with each "
                f"level. `/factory upgrade` raises a drill one level for an Upgrade Pack plus its "
                f"tier material, and that cost doubles every level - run it to see what your drill "
                f"needs next."
            ),
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)

    @recipe_group.command(name="furnace", description="List every furnace recipe")
    async def recipe_furnace(self, interaction: discord.Interaction):
        embed = make_embed("🔥 Furnace Recipes", RECIPE_COLOR)
        add_multi_field(embed, "Recipes", build_recipe_lines(SMELTED_MATERIALS))
        await respond(interaction, self.db, embed=embed)

    @recipe_group.command(name="press", description="List every hydraulic press recipe")
    async def recipe_press(self, interaction: discord.Interaction):
        embed = make_embed("⚙️ Hydraulic Press Recipes", RECIPE_COLOR)
        embed.description = (
            "Each recipe costs a little under what you'd have mined alongside that gem "
            "on average before finding one - the press trades a very large pile of ore "
            "for the certainty of a gem."
        )
        lines = [
            f"{line} [{PRESS_RECIPES[product_id]['press_days']} press-day"
            f"{'s' if PRESS_RECIPES[product_id]['press_days'] != 1 else ''}]"
            for product_id, line in zip(PRESS_RECIPES, build_recipe_lines(PRESS_RECIPES))
        ]
        add_multi_field(embed, "Recipes", lines)
        embed.add_field(
            name="Press Time",
            value=(
                "A press produces one press-day of work per day for each level it has, "
                "so a level 1 press takes nine days over a diamond and a level 3 press "
                "takes three."
            ),
            inline=False,
        )
        await respond(interaction, self.db, embed=embed)


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the recipe_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(RecipeCog(bot))
