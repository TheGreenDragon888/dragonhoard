"""
utils/responses.py

Shared helper for sending a command's main (successful) response. Per-server,
bot responses are private (ephemeral) by default so the bot doesn't clutter
channels - a "Manage Server" admin can opt a server into public responses via
/setup messages public, e.g. once they've set up a dedicated bot channel.

This does NOT apply to error/validation messages (missing permissions,
insufficient materials, etc.) - those should keep using
interaction.response.send_message(..., ephemeral=True) directly, since
they're personal to the user who triggered them regardless of the server's
setting.

It is also where pending notifications ride along (utils/notifications.py).
Being the one funnel for successful replies is exactly what makes it the right
hook: every command passes through here exactly once, so "the first time you
interact with the bot after a notice was posted" needs no per-command wiring.

And since 1.4, where a notice's BUTTONS ride along too. A notice can name an
action (notifications.action_key) and the registry below turns it into
components on the same reply, which is how a new prediction bet reaches a
server's players without the bot posting into a channel unbidden. The costing
is the point: the notice rows are fetched here anyway, so a server with nothing
going on pays nothing for the feature existing.

What that inherits is the notice feed's own shape, which callers should know
before relying on it: only the NEWEST notice of a feed is ever shown, and only
once. Buttons delivered this way are a nudge toward a feature, never the only
way to reach it - /bet status carries the same two buttons on a reply a player
can ask for whenever they like.
"""
import logging
from typing import Callable

import discord

from database.db import Database
from utils.notifications import (
    fetch_unseen,
    fetch_unseen_personal,
    mark_personal_seen,
    mark_seen,
    notice_embed,
)

log = logging.getLogger("dragonhoard")


# How a notice's action_key becomes something clickable. A feature registers
# its kind when its module is imported - register_notice_action("bet", builder)
# - and a notice whose action_key is "bet:12" gets that builder called with
# "12".
#
# A REGISTRY RATHER THAN AN IMPORT, for one reason: this module is imported by
# every cog, so importing a cog back from here to reach its buttons is a cycle.
# Registration inverts that - the feature knows about responses.py, and
# responses.py knows only that some kinds exist.
#
# A builder returns the discord.ui.Item objects to hang on the reply, or an
# empty list if the action has since gone stale (a bet resolved between the
# notice being raised and the player next running a command). It must not touch
# the database or the network: it runs on the reply path of an unrelated
# command, which is the hot path this whole mechanism was shaped to keep cheap.
_NOTICE_ACTIONS: dict[str, "Callable[[str], list[discord.ui.Item]]"] = {}


def register_notice_action(kind: str, builder) -> None:
    """Registers the components a notice of this kind carries. Idempotent on
    the kind, so a cog reloaded during development replaces its builder rather
    than accumulating copies."""
    _NOTICE_ACTIONS[kind] = builder


def _action_items(rows) -> list:
    """Every component the notices in `rows` want to add, in their order.

    A malformed or unregistered action_key yields nothing rather than raising.
    This runs while delivering somebody else's command reply, and a stale
    action is not worth failing that reply over."""
    items = []
    for row in rows:
        action_key = row["action_key"]
        if not action_key or ":" not in action_key:
            continue
        kind, _, argument = action_key.partition(":")
        builder = _NOTICE_ACTIONS.get(kind)
        if builder is None:
            continue
        try:
            items.extend(builder(argument))
        except Exception:
            log.exception("Notice action %r could not build its components.", action_key)
    return items


def _attach_items(kwargs: dict, items: list) -> None:
    """Puts `items` on the reply, joining whatever view the caller was already
    sending rather than replacing it.

    Discord allows one view per message, and /help and /changelog both pass
    their own - so a notice's buttons have to move in with the dropdown rather
    than evict it. The host view's timeout does not matter: discord.py matches
    a dynamic item on the custom_id alone, before it ever looks up which view
    the message had (discord/ui/view.py: dispatch_view calls
    dispatch_dynamic_items first), so these keep working long after the view
    they arrived on has expired.
    """
    if not items:
        return
    view = kwargs.get("view")
    if view is None:
        view = discord.ui.View(timeout=None)
        kwargs["view"] = view

    # Skip anything the reply is already carrying. Discord rejects a message
    # whose components share a custom_id, and the collision is a real case
    # rather than a theoretical one: /bet status for a bet puts that bet's two
    # buttons on the reply, and the notice announcing the very same bet wants
    # to put the same two there. Whoever got there first wins; they are the
    # same button either way.
    taken = {getattr(existing, "custom_id", None) for existing in view.children}
    for item in items:
        custom_id = getattr(item, "custom_id", None)
        if custom_id is not None and custom_id in taken:
            continue
        taken.add(custom_id)
        view.add_item(item)


def _merge_embeds(kwargs: dict, extra: list[discord.Embed]) -> None:
    """Folds `extra` onto whatever embed(s) the caller was already sending.

    Callers pass `embed=` (most of them), `embeds=`, or neither, and
    send_message rejects both keys at once - so the two have to be collapsed
    into one list here rather than appended to blindly. The notices go last:
    the command's own answer is what the player asked for and should be what
    they read first."""
    existing = kwargs.pop("embeds", None) or []
    single = kwargs.pop("embed", None)
    if single is not None:
        existing = [single, *existing]
    kwargs["embeds"] = [*existing, *extra]


async def respond(interaction: discord.Interaction, db: Database, **kwargs):
    """Sends the interaction's main response, ephemeral unless this server
    has opted into public bot messages, with any unseen notifications attached.

    Notices follow the server's public/private setting rather than forcing
    themselves ephemeral. They arrive attached to a message the player asked
    for, so splitting the two apart would mean either a second reply (which an
    interaction only gets one of) or a visibility mismatch inside one message,
    which Discord has no way to express anyway.
    """
    public = False
    if interaction.guild_id is not None:
        cfg = await db.fetchone(
            "SELECT public_messages FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        public = bool(cfg["public_messages"]) if cfg else False

    # Broadcasts first, then anything personal. Broadest to narrowest, matching
    # the order fetch_unseen already puts global ahead of server in - and it
    # puts "something happened to YOU" closest to the reply the player asked
    # for, which is the notice most likely to be worth acting on.
    notices = await fetch_unseen(db, interaction.user.id, interaction.guild_id)
    personal = await fetch_unseen_personal(db, interaction.user.id)
    if notices or personal:
        rows = (*notices, *personal)
        _merge_embeds(kwargs, [notice_embed(row) for row in rows])
        # Anything the reader can act on goes on this same message. No extra
        # query pays for it: these rows are already in hand, which is what
        # makes "every command carries the newest notice's buttons" cost
        # nothing on the commands of a server with no bet running.
        _attach_items(kwargs, _action_items(rows))

    await interaction.response.send_message(ephemeral=not public, **kwargs)

    # Only after the send has succeeded. If Discord rejected the message the
    # notice is still pending and rides along with the next command - showing
    # an announcement twice is a far smaller failure than silently swallowing
    # one, which is what marking beforehand would risk.
    if notices:
        await mark_seen(db, interaction.user.id, notices)
    if personal:
        await mark_personal_seen(db, interaction.user.id, personal)
