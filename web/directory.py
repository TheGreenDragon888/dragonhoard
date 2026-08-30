"""
web/directory.py

dragonhoard.db never stores a Discord display name - only the numeric
snowflake IDs the bot itself uses (guild_id, user_id, bot_channel_id). A
guild's name, a user's username, and a channel's name all live on Discord's
side, and resolving them for real would mean giving this read-only dashboard
its own Discord API credentials and network access - the exact dependency
the ops dashboard was deliberately built without (see web/README.md).

Instead this is a small, optionally-present JSON file the dashboard's one
human operator hand-maintains for their own ~8 servers and ~15 players -
cheap for them, since they already know who everyone is, and it never has to
match production automation deploy machinery. Missing entirely, or missing a
given id, both degrade to a truncated-ID label rather than an error.
"""
import json
from pathlib import Path

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


def guild_name(guild_id: int) -> str:
    name = _DIRECTORY["guilds"].get(str(guild_id))
    return name if name else f"Server •{str(guild_id)[-4:]}"


def user_name(user_id: int) -> str:
    name = _DIRECTORY["users"].get(str(user_id))
    return name if name else f"user_{str(user_id)[-4:]}"


def channel_name(channel_id: int | None) -> str | None:
    if channel_id is None:
        return None
    name = _DIRECTORY["channels"].get(str(channel_id))
    return name if name else f"#channel-•{str(channel_id)[-4:]}"
