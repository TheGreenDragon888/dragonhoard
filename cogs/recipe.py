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
    get_material_info,
    scrap_yield,
)

# DRILL_BITS/DRILL_COMPONENTS are id tuples; the book needs the recipe dicts.
COMPONENT_RECIPES = {k: COMPONENT_MATERIALS[k] for k in DRILL_COMPONENTS}
DRILL_BIT_RECIPES = {k: COMPONENT_MATERIALS[k] for k in DRILL_BITS}


def _scrap_lines(items: dict) -> list[str]:
    """One line per item: what it is, and what the scrapper hands back for it.
    Shaped like build_recipe_lines' output so the two books read the same way -
    the difference is that the arrow points the other direction."""
    lines = []
    for material_id, info in items.items():
        returns = scrap_yield(material_id)
        parts = []
        for return_id, quantity in returns.items():
            return_info = get_material_info(return_id)
            parts.append(f"{return_info['emoji'] if return_info else '❓'} {quantity}")
        lines.append(f"{info['emoji']} {info['name']} - {' , '.join(parts) or 'nothing'}")
    return lines


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

    @recipe_group.command(name="factory", description="List the factory recipes for one kind of item")
    @app_commands.describe(section="Which part of the recipe book to open")
    @app_commands.choices(section=[
        app_commands.Choice(name="Drill Components", value="components"),
        app_commands.Choice(name="Drills", value="drills"),
        app_commands.Choice(name="Containers", value="containers"),
    ])
    async def recipe_factory(self, interaction: discord.Interaction, section: app_commands.Choice[str]):
        """The factory builds far more than the furnace or the press do, and
        listing all five of its groups at once produced an embed nobody read to
        the bottom of. A section per lookup keeps each page to the thing the
        player came for."""
        embed = make_embed(f"🏭 Factory Recipes · {section.name}", RECIPE_COLOR)

        if section.value == "components":
            add_multi_field(embed, "Components", build_recipe_lines(COMPONENT_RECIPES))
            add_multi_field(embed, "Drill Bits", build_recipe_lines(DRILL_BIT_RECIPES))
        elif section.value == "drills":
            add_multi_field(embed, "Drills", build_recipe_lines(DRILLS))
            add_multi_field(embed, "Upgrades", build_recipe_lines(UPGRADE_MATERIALS))
            embed.add_field(
                name="Drill Upgrades",
                value=(
                    f"Every drill holds {BASE_STORAGE_CAPACITY} on its own. `/factory upgrade` "
                    f"raises a drill one level for an Upgrade Pack plus its tier material, and "
                    f"each level adds a fifth of that drill's own base speed - so an upgrade is "
                    f"worth the same proportion whichever drill you spend it on. The cost doubles "
                    f"every level; run the command to see what your drill needs next."
                ),
                inline=False,
            )
        else:
            add_multi_field(embed, "Storage Containers", _container_lines())

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


    @recipe_group.command(name="scrapper", description="List what the scrapper gives back for each item")
    async def recipe_scrapper(self, interaction: discord.Interaction):
        embed = make_embed("♻️ Scrapper Returns", RECIPE_COLOR)
        embed.description = (
            "The scrapper undoes **one tier** of crafting and gives back **half** of that "
            "recipe, rounded down - so a drill comes back as components, and those components "
            "have to be scrapped again to reach metal.\n\n"
            "It never returns less than one of a recipe's most valuable part. That's what makes "
            "scrapping a drill worth anything at all (a drill's recipe is one of each part, and "
            "half of one is nothing), and it's why a gem-tier item never has its gem destroyed."
        )
        add_multi_field(embed, "Components", _scrap_lines(COMPONENT_RECIPES))
        add_multi_field(embed, "Drill Bits", _scrap_lines(DRILL_BIT_RECIPES))
        add_multi_field(embed, "Drills", _scrap_lines(DRILLS))
        add_multi_field(embed, "Containers", _scrap_lines(STORAGE_CONTAINERS))
        add_multi_field(embed, "Upgrades", _scrap_lines(UPGRADE_MATERIALS))
        await respond(interaction, self.db, embed=embed)


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the recipe_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(RecipeCog(bot))
