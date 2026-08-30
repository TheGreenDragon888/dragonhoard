"""
web/discord_lookup.py

Resolves real Discord names for guilds/users/channels via Discord's REST
API, using the same DISCORD_BOT_TOKEN the bot itself already authenticates
with (config.py) - no new credential. This is a REST client only, not the
gateway: no websocket, no intents, just authenticated HTTP GETs against the
handful of ids the dashboard actually needs a name for.

Results are cached in memory for CACHE_TTL_SECONDS - real names change
rarely, and the alternative is a Discord API call on every single page
load. The cache starts empty on process start, so the first load after
(re)starting the dashboard is the only slow one, and warm_cache() resolves
a batch of ids concurrently so that first load is one round of parallel
requests rather than dozens of sequential ones.

web/directory.py is what actually decides what to show: a directory.json
entry always wins over this (an explicit human override), and this is only
consulted for ids directory.json has nothing to say about. Anything that
fails here (bot removed from the guild, network hiccup, rate limit, revoked
token) returns None and directory.py falls back to its truncated-ID label -
a broken lookup degrades to something ugly but honest, never to a crash.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config

API_BASE = "https://discord.com/api/v10"
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 5

# key ("guild:123") -> (resolved_at, name_or_None). A cached None (a lookup
# that failed or came back empty) still expires after the TTL rather than
# being retried every call - a bot removed from a guild mid-session
# shouldn't mean every request pays that failure's timeout again.
_cache: dict[str, tuple[float, str | None]] = {}


def _headers() -> dict:
    return {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}


def _fetch(kind: str, entity_id: int, path: str, extract) -> str | None:
    key = f"{kind}:{entity_id}"
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    name = None
    try:
        resp = requests.get(f"{API_BASE}{path}", headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            name = extract(resp.json())
    except (requests.RequestException, ValueError):
        # ValueError covers a non-JSON 200 body, which would otherwise crash
        # a page load over something Discord's API has never actually done -
        # not worth a narrower catch for a lookup that's always allowed to
        # come back empty anyway.
        name = None
    _cache[key] = (time.monotonic(), name)
    return name


def guild_name(guild_id: int) -> str | None:
    return _fetch("guild", guild_id, f"/guilds/{guild_id}", lambda j: j.get("name"))


def user_name(user_id: int) -> str | None:
    # global_name is the modern display name (post the 2023 username
    # migration); username is the always-present fallback for accounts that
    # have never set one.
    return _fetch("user", user_id, f"/users/{user_id}", lambda j: j.get("global_name") or j.get("username"))


def channel_name(channel_id: int) -> str | None:
    return _fetch("channel", channel_id, f"/channels/{channel_id}",
                   lambda j: f"#{j['name']}" if j.get("name") else None)


def warm_cache(guild_ids: list[int], user_ids: list[int], channel_ids: list[int]) -> None:
    """Resolves every id not already cached, concurrently, before the
    request that needs them starts rendering rows."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = (
            [pool.submit(guild_name, gid) for gid in guild_ids]
            + [pool.submit(user_name, uid) for uid in user_ids]
            + [pool.submit(channel_name, cid) for cid in channel_ids]
        )
        for j in jobs:
            j.result()  # each already swallows its own errors; just wait for the batch
