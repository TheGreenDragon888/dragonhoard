"""
bot.py

The entry point you actually run: `python bot.py`.

What it does, in order:
  1. Creates a discord.py Bot instance with the permissions ("intents") it needs
  2. Attaches a shared Database object so every cog can query SQLite
  3. Loads each cog (extension) from the cogs/ folder
  4. Syncs slash commands with Discord so they show up in the / menu
  5. Logs in and starts listening for events
"""
import logging

import discord
from discord.ext import commands

import config
from database.db import Database
from data.notifications import GLOBAL_NOTICES
from utils.channel_guard import DragonhoardTree
from utils.notifications import seed_global_notices

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dragonhoard")

# Intents are permission flags telling Discord which events your bot wants
# to receive. members is privileged - it must ALSO be enabled in the Discord
# Developer Portal under your bot's "Privileged Gateway Intents".
intents = discord.Intents.default()
intents.members = True  # needed for guild.members (utils/guild_helpers.py: human_member_count)

# tree_cls is what installs the designated-bot-channel check. It has to be
# passed here rather than set later: Bot builds its CommandTree in __init__,
# and every command registered by a cog goes into whichever tree already
# exists. See utils/channel_guard.py for why the check lives on the tree.
bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=DragonhoardTree)

# Every cog accesses this via `bot.db`, so it's created once here and shared,
# rather than each cog opening its own separate connection pool.
bot.db = Database(config.DATABASE_PATH)

def build_initial_extensions(is_beta: bool) -> list[str]:
    """Every file in cogs/ that should be loaded as an extension. Add new
    cogs here as you build more features.

    Pulled out as a pure function (rather than a bare module-level list) so
    the beta-only conditional below is unit-testable without importing bot.py
    itself - see tests/test_bot.py."""
    extensions = [
        "cogs.setup",
        "cogs.economy",
        "cogs.mining",
        "cogs.furnace",
        "cogs.factory",
        "cogs.press",
        "cogs.scrapper",
        "cogs.jobboard",
        "cogs.donate",
        "cogs.recipe",
        "cogs.manual",
        "cogs.changelog",
        "cogs.fun",
    ]
    if is_beta:
        # cogs/devtools.py - never registered as a slash command on the live
        # bot, so a global sync can never expose it there. See that module's
        # docstring for the rest of this feature's defense-in-depth layers.
        extensions.append("cogs.devtools")
    return extensions


INITIAL_EXTENSIONS = build_initial_extensions(config.IS_BETA)


@bot.event
async def setup_hook():
    """discord.py calls this automatically once, before the bot logs in.
    This is the correct place to load extensions and sync the command tree -
    doing it here (rather than in on_ready) guarantees it only runs once,
    even if the bot's connection drops and reconnects later."""
    await bot.db.init_schema()
    log.info("Database schema ready.")

    # Idempotent on each notice's key, so this runs on every boot and inserts
    # only what's new - a release's announcements go out once, not every time
    # the service restarts. See data/notifications.py.
    seeded = await seed_global_notices(bot.db, GLOBAL_NOTICES)
    log.info("Global notifications: %d new, %d defined.", seeded, len(GLOBAL_NOTICES))

    for ext in INITIAL_EXTENSIONS:
        await bot.load_extension(ext)
        log.info(f"Loaded extension: {ext}")

    if config.DEV_GUILD_ID:
        # Fast path for development: syncing to one specific guild applies
        # instantly, whereas a global sync can take up to an hour to show
        # up in every server's slash command list.
        guild = discord.Object(id=config.DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} commands to dev guild {config.DEV_GUILD_ID}.")
    else:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} commands globally.")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    bot.run(config.DISCORD_BOT_TOKEN, log_handler=None)
