"""
cogs/recipe.py

Implements the /recipe book: /recipe factory and /recipe furnace list every
recipe for their machine. Recipes used to be a field on the /factory status
and /furnace status embeds; they live here now so those embeds stay focused
on queue and upgrade info.

/recipe furnace covers the blast furnace too, since its recipes are the
furnace's own scaled by BLAST_FURNACE_BATCH_SIZE - see recipe_furnace.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.embeds import make_embed, add_multi_field, RECIPE_COLOR
from utils.responses import respond
from utils.db_helpers import build_recipe_lines

from data.materials import (
    BLAST_FURNACE_BATCH_SIZE,
    BLAST_FURNACE_RECIPES,
    COMPONENT_MATERIALS,
    DRILLS,
    DRILL_BITS,
    DRILL_COMPONENTS,
    SMELTED_MATERIALS,
    STORAGE_CONTAINERS,
    UPGRADE_MATERIALS,
    PRESS_RECIPES,
    drill_scrap_yield,
    effective_capacity,
    get_material_info,
    scrap_yield,
    upgrade_cost,
)

# DRILL_BITS/DRILL_COMPONENTS are id tuples; the book needs the recipe dicts.
COMPONENT_RECIPES = {k: COMPONENT_MATERIALS[k] for k in DRILL_COMPONENTS}
DRILL_BIT_RECIPES = {k: COMPONENT_MATERIALS[k] for k in DRILL_BITS}


def _scrap_lines(items: dict, yield_fn=scrap_yield) -> list[str]:
    """One line per item: what it is, and what the scrapper hands back for it.
    Shaped like build_recipe_lines' output so the two books read the same way -
    the difference is that the arrow points the other direction.

    yield_fn defaults to scrap_yield; DRILLS is rendered with
    drill_scrap_yield instead, since that's what /scrapper drill actually
    pays out (see that function's docstring)."""
    lines = []
    for material_id, info in items.items():
        returns = yield_fn(material_id)
        parts = []
        for return_id, quantity in returns.items():
            return_info = get_material_info(return_id)
            parts.append(f"{return_info['emoji'] if return_info else '❓'} {quantity}")
        lines.append(f"{info['emoji']} {info['name']} - {' , '.join(parts) or 'nothing'}")
    return lines


def _blast_furnace_lines() -> list[str]:
    """The furnace's recipes at blast-furnace scale. Not build_recipe_lines,
    for two reasons: the product line has to name the batch it produces rather
    than one unit, and four- and five-figure input quantities need thousands
    separators that the ordinary recipe lines have no use for."""
    lines = []
    for material_id, recipe in BLAST_FURNACE_RECIPES.items():
        info = get_material_info(material_id)
        costs = []
        for input_id, quantity in recipe["inputs"].items():
            input_info = get_material_info(input_id)
            input_emoji = input_info["emoji"] if input_info else "❓"
            costs.append(f"{input_emoji} {quantity:,}")
        lines.append(f"{info['emoji']} {recipe['output']:,} {info['name']} - {' , '.join(costs)}")
    return lines


def _container_lines() -> list[str]:
    """Container recipes with the number that actually matters attached - a
    container is bought for its capacity, which the bare recipe doesn't show."""
    return [
        f"{line} — [holds {effective_capacity(container_id):,}]"
        for container_id, line in zip(STORAGE_CONTAINERS, build_recipe_lines(STORAGE_CONTAINERS))
    ]


def _drill_lines() -> list[str]:
    """Drill recipes with the number that actually matters attached - a
    drill is bought for its mining speed, which the bare recipe doesn't show.
    :g drops the trailing zero on whole-number rates (Ruby's 30) while still
    showing Steel's fractional 7.5, matching /factory upgrade's own receipt
    (cogs/factory.py)."""
    return [
        f"{line} — [mines {DRILLS[drill_id]['mines_per_hour']:g}/hour]"
        for drill_id, line in zip(DRILLS, build_recipe_lines(DRILLS))
    ]


def _drill_upgrade_lines() -> list[str]:
    """Each drill's level-1 upgrade cost - what /factory upgrade takes to
    raise it from level 1 to 2. Every part of the cost doubles per level
    from there (see upgrade_cost)."""
    upgrade_recipes = {drill_id: {"inputs": upgrade_cost(drill_id, 1)} for drill_id in DRILLS}
    return build_recipe_lines(upgrade_recipes)


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
        add_multi_field(embed, "Drills", _drill_lines())
        add_multi_field(embed, "Upgrade Pack", build_recipe_lines(UPGRADE_MATERIALS))
        add_multi_field(
            embed, "Drill Upgrades",
            _drill_upgrade_lines() + ["Cost doubles every level."],
        )
        add_multi_field(embed, "Storage Containers", _container_lines())
        await respond(interaction, self.db, embed=embed)

    @recipe_group.command(name="furnace", description="List every furnace and blast furnace recipe")
    async def recipe_furnace(self, interaction: discord.Interaction):
        """Both smelters on one page rather than a /recipe blast of its own.
        The blast furnace's recipes ARE the furnace's, multiplied by
        BLAST_FURNACE_BATCH_SIZE, so a separate page would be a near-duplicate
        of this one - and the question a player actually has ("is bulk smelting
        a better deal?") is answered by seeing the two tables together, where
        the ratios are visibly identical."""
        embed = make_embed("🔥 Furnace Recipes", RECIPE_COLOR)
        add_multi_field(embed, "Recipes", build_recipe_lines(SMELTED_MATERIALS))
        add_multi_field(
            embed,
            f"♨️ Blast Furnace · batches of {BLAST_FURNACE_BATCH_SIZE}",
            _blast_furnace_lines(),
        )
        embed.add_field(
            name="Which to use",
            value=(
                f"The blast furnace runs these same recipes {BLAST_FURNACE_BATCH_SIZE} at a "
                "time - the same ore per bar, the same coal per bar, the same fee per bar, "
                "and a lot faster. Its own fuel and fee are charged per batch, so it's worth "
                "it once you're smelting in the thousands. `/blast smelt` queues one."
            ),
            inline=False,
        )
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
            "recipe, rounded down. It never returns less than one of a recipe's most valuable "
            "part - that's why a gem-tier item never has its gem destroyed.\n\n"
            "Drills are the one exception: scrapping one skips the component tier entirely. "
            "It returns the drill's bit whole, plus half the raw materials (rounded down) that "
            "its wiring and chassis were made from - never a whole chassis or wiring to build "
            "another drill from for free."
        )
        add_multi_field(embed, "Components", _scrap_lines(COMPONENT_RECIPES))
        add_multi_field(embed, "Drill Bits", _scrap_lines(DRILL_BIT_RECIPES))
        add_multi_field(embed, "Drills", _scrap_lines(DRILLS, drill_scrap_yield))
        add_multi_field(embed, "Containers", _scrap_lines(STORAGE_CONTAINERS))
        add_multi_field(embed, "Upgrades", _scrap_lines(UPGRADE_MATERIALS))
        await respond(interaction, self.db, embed=embed)


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the recipe_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(RecipeCog(bot))
