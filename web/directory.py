"""
web/directory.py

Resolves a display name for a guild/user/channel id, in this order:

  1. web/directory.json - a manual, hand-maintained override. Always wins,
     since it's an explicit human decision (a custom label, or a correction
     for something Discord's API got wrong or can't answer - e.g. a
     departed guild the bot can no longer look up).
  2. web/discord_lookup.py - Discord's REST API, cached (the bot's own
     DISCORD_BOT_TOKEN, no new credential - see that module's docstring).
  3. A truncated-ID label (`Server •1234`, `user_5678`) - the last resort
     when neither of the above has an answer, so the dashboard always shows
     *something* rather than erroring.

warm() should be called once per request, before the loop that calls
guild_name/user_name/channel_name repeatedly, so a cold Discord-lookup
cache costs one batch of concurrent requests instead of many sequential
ones - see web/queries.py.
"""
import json
from pathlib import Path

from web import discord_lookup

_DIRECTORY_PATH = Path(__file__).parent / "directory.json"


def _load() -> dict:
    if not _DIRECTORY_PATH.exists():
        return {"guilds": {}, "users": {}, "channels": {}}
    with _DIRECTORY_PATH.open() as f:
        data = json.load(f)
    return {
        "guilds": data.get("guilds", {}),
        "users": data.get("users", {}),
        "channels": data.get("channels", {}),
    }


_DIRECTORY = _load()


def reload() -> None:
    """Re-reads directory.json from disk. Called on every /api/ops request -
    the file is tiny and hand-edited, so there is no reason to require a
    process restart to pick up a rename."""
    global _DIRECTORY
    _DIRECTORY = _load()


def warm(guild_ids: list[int], user_ids: list[int] | None, channel_ids: list[int]) -> None:
    """Pre-resolves every id NOT already covered by a directory.json
    override, via Discord, in parallel. `user_ids` is None when the caller
    is rendering with anonymize on - player names never reach Discord in
    that mode, since nothing would use the result."""
    def unmapped(ids: list[int], table: dict) -> list[int]:
        return [i for i in ids if not table.get(str(i))]

    discord_lookup.warm_cache(
        guild_ids=unmapped(guild_ids, _DIRECTORY["guilds"]),
        user_ids=unmapped(user_ids, _DIRECTORY["users"]) if user_ids is not None else [],
        channel_ids=unmapped(channel_ids, _DIRECTORY["channels"]),
    )


def guild_name(guild_id: int) -> str:
    override = _DIRECTORY["guilds"].get(str(guild_id))
    if override:
        return override
    looked_up = discord_lookup.guild_name(guild_id)
    return looked_up or f"Server •{str(guild_id)[-4:]}"


def user_name(user_id: int) -> str:
    override = _DIRECTORY["users"].get(str(user_id))
    if override:
        return override
    looked_up = discord_lookup.user_name(user_id)
    return looked_up or f"user_{str(user_id)[-4:]}"


def channel_name(channel_id: int | None) -> str | None:
    if channel_id is None:
        return None
    override = _DIRECTORY["channels"].get(str(channel_id))
    if override:
        return override
    looked_up = discord_lookup.channel_name(channel_id)
    return looked_up or f"#channel-•{str(channel_id)[-4:]}"
