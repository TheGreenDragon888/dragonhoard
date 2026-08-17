"""
utils/channel_guard.py

Enforces a server's designated bot channel: if a manager has set one with
/setup channel, Dragonhoard only answers there.

The check lives in ONE place - a CommandTree subclass whose interaction_check
discord.py calls before dispatching any interaction - rather than as a
decorator on each command or an interaction_check on each cog. The alternatives
both have the same disqualifying flaw: a command or a cog added later opts out
of the restriction by simply not having had the check pasted onto it, and a
rule that quietly stops applying to new features is worse than no rule. The
command list has only ever grown, which is what makes that the deciding
argument rather than a hypothetical one.

Three things about discord.py's CommandTree.interaction_check drive the shape
of the code below, all of them verified against the installed version rather
than assumed:

  1. It runs BEFORE the tree resolves which command was invoked, so
     interaction.command is still None. The command name has to come out of the
     raw payload - interaction.data["name"], which for a subcommand like
     /setup fee is the top-level "setup". That happens to be exactly the
     granularity the exemption list wants.
  2. It fires for AUTOCOMPLETE interactions too, not just command invocations.
     Blocking those is right - a drill list shouldn't populate in a channel the
     command can't be run in - but an autocomplete can only be answered with
     choices, never with a message, so a refusal there has to be silent.
  3. Returning False sends nothing and raises nothing. Anything that refuses
     has to send its own reply first, or the user is left looking at Discord's
     "The application did not respond".
"""
import logging

import discord
from discord import app_commands

log = logging.getLogger("dragonhoard")

# Commands that work anywhere, no matter what bot_channel_id says. These are
# top-level names, which is what interaction.data["name"] gives for a
# subcommand too - so this covers all of /setup's subcommands at once.
#
# /setup, because a manager who sets a bot channel and then deletes it, or
# picks one nobody can post in, has to be able to fix that from wherever they
# are - a lockout whose only escape is the channel you locked yourself out of
# isn't a setting, it's a trap. The manual, because "where am I allowed to use
# this bot" is one of the questions it answers, and it's private by default and
# produces no lasting spam.
EXEMPT_ROOT_COMMANDS = frozenset({"setup", "help", "manual", "man"})


def is_allowed_channel(bot_channel_id: int | None, channel_id: int | None, parent_id: int | None) -> bool:
    """Whether a command run in this channel is allowed through.

    A pure predicate rather than a method, so every case it has to get right -
    no restriction set, the channel itself, a thread hanging off it, somewhere
    else entirely - is testable without a gateway connection.

    A thread whose PARENT is the bot channel counts as inside it. The bot
    channel is where conversation about the bot is supposed to happen, and a
    thread is the Discord-native way to have one of those without burying the
    channel; treating threads as outside would make a thread started in the bot
    channel a dead zone, which reads as a bug rather than as a rule.
    """
    if bot_channel_id is None:
        return True
    return channel_id == bot_channel_id or parent_id == bot_channel_id


class DragonhoardTree(app_commands.CommandTree):
    """The command tree, with the bot-channel check wired into every
    interaction. Installed in bot.py via commands.Bot(tree_cls=...)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # No guild, no server config, nothing to enforce. (Most commands would
        # fail in a DM for other reasons anyway; this doesn't change that.)
        if interaction.guild_id is None:
            return True

        if interaction.data and interaction.data.get("name") in EXEMPT_ROOT_COMMANDS:
            return True

        row = await interaction.client.db.fetchone(
            "SELECT bot_channel_id FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        bot_channel_id = row["bot_channel_id"] if row else None
        if bot_channel_id is None:
            return True

        # A channel the gateway cache can't resolve. Deliberately fails OPEN:
        # the usual reason is that the channel really was deleted, and refusing
        # every command while pointing at a channel that no longer exists would
        # lock the server out of the bot entirely. The cleanup that matters is
        # the on_guild_channel_delete listener in cogs/setup.py; this only
        # covers the window where the deletion happened while the bot was
        # offline. Nothing is written from here - a cache miss isn't proof of
        # deletion, and a write per interaction would be waste either way.
        if interaction.guild is not None and interaction.guild.get_channel(bot_channel_id) is None:
            log.info(
                "Guild %s has bot_channel_id %s, which no longer resolves - allowing anyway.",
                interaction.guild_id, bot_channel_id,
            )
            return True

        if is_allowed_channel(
            bot_channel_id, interaction.channel_id, getattr(interaction.channel, "parent_id", None)
        ):
            return True

        # An autocomplete can only be answered with choices, so refusing one
        # means returning no list at all rather than explaining why.
        if interaction.type is discord.InteractionType.autocomplete:
            return False

        # Always ephemeral, bypassing utils/responses.respond: this is an error
        # path, and a server that has opted into public replies has done so for
        # results, not for telling everyone that someone typed in the wrong
        # channel.
        await interaction.response.send_message(
            f"Dragonhoard only answers in <#{bot_channel_id}> on this server.",
            ephemeral=True,
        )
        return False
