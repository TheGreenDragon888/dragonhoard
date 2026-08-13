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
"""
import logging

import discord

from database.db import Database
from utils.notifications import fetch_unseen, mark_seen, notice_embed

log = logging.getLogger("dragonhoard")


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

    notices = await fetch_unseen(db, interaction.user.id, interaction.guild_id)
    if notices:
        _merge_embeds(kwargs, [notice_embed(row) for row in notices])

    await interaction.response.send_message(ephemeral=not public, **kwargs)

    # Only after the send has succeeded. If Discord rejected the message the
    # notice is still pending and rides along with the next command - showing
    # an announcement twice is a far smaller failure than silently swallowing
    # one, which is what marking beforehand would risk.
    if notices:
        await mark_seen(db, interaction.user.id, notices)
