"""
config.py

Loads settings from the .env file (or real environment variables) into one
place. Every other module imports from here instead of calling os.getenv()
directly - that way, if you ever change how config is loaded, you only edit
one file.
"""
import os
from dotenv import load_dotenv

# Reads the ".env" file in the current directory and loads its key=value
# pairs into the process environment (os.environ). If .env doesn't exist,
# this just does nothing and os.getenv() falls back to real env vars,
# which is useful in production (e.g. systemd EnvironmentFile).
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/dragonhoard.db")

# Shown in every embed's footer (see utils/embeds.py and docs/stylization.md).
VERSION = "1.3"

# Per-item infrastructure fees a server starts with (docs/mining.txt).
# Server managers can change them with /setup fee. Also mirrored in the
# server_config column DEFAULTs in database/schema.sql, which SQL can't
# read from here - keep the two in sync.
DEFAULT_FURNACE_FEE = 0.01
# The blast furnace charges per BATCH, and a batch is
# data.materials.BLAST_FURNACE_BATCH_SIZE items, so this is the furnace's fee
# times that batch size. Bulk smelting is meant to be faster, not cheaper: a
# smelted unit costs the same 0.01 in fees whichever machine produced it.
DEFAULT_BLAST_FURNACE_FEE = 1.00
DEFAULT_FACTORY_FEE = 0.25
# Charged per ruby-equivalent of press time, so a recipe pays this multiplied
# by its press_days - a diamond costs nine times a ruby. Far above the other
# two because a single pressed gem is worth thousands on the market.
DEFAULT_PRESS_FEE = 5.00
# The scrapper. Charged per item recycled. A scrap returns half of one factory
# item's inputs, so it is priced against the factory rather than independently:
# 0.10 is 40% of the factory's fee. Kept well under it because a steep fee on
# top of the 50% material loss scrapping already costs would make the machine
# feel punitive rather than useful.
DEFAULT_SCRAPPER_FEE = 0.10

# getenv returns a string or None. We convert to int only if present, since
# discord.py's guild sync functions expect an int object ID, not a string.
_dev_guild = os.getenv("DEV_GUILD_ID")
DEV_GUILD_ID = int(_dev_guild) if _dev_guild else None

# Which of the bot's two Discord applications this process logged in as -
# the real "Dragonhoard" app ("live") or "Dragonhoard Beta" ("beta"), the one
# this directory normally runs as (docs/testing.md). data/emoji.py reads this
# to pick the right ID for every custom emoji, since the two applications
# each have their own separate uploaded copy of every icon.
# Defaults to "live" so production's .env never needs to mention this at all
# - only beta's does.
BOT_ENVIRONMENT = os.getenv("BOT_ENVIRONMENT", "live").strip().lower()
if BOT_ENVIRONMENT not in ("live", "beta"):
    raise RuntimeError(
        f"BOT_ENVIRONMENT must be 'live' or 'beta' (or unset, which means "
        f"'live'), got {BOT_ENVIRONMENT!r}."
    )
IS_BETA = BOT_ENVIRONMENT == "beta"

if not DISCORD_BOT_TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
    )
