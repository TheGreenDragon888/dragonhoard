"""
cogs/betting.py

Implements:
  - /bet open <prediction> <amount> <closes_in>  - propose an outcome and back it
  - /bet place <bet> <side> <amount>             - back or oppose an open bet
  - /bet status [bet]                            - a bet's pools and odds, or the list
  - /bet close <bet>                             - stop wagers early (its creator, or Manage Server)
  - /bet resolve <bet> <outcome>                 - call it (Manage Server)
  - /bet cancel <bet>                            - void it and refund (Manage Server)

The pot arithmetic and every currency movement live in utils/betting.py; this
file decides how a bet gets on screen and who is allowed to do what. The design
and the economics are in docs/betting.md.

A GROUP RATHER THAN A BARE /bet, for the reason /economy is one: Discord will
not let a command that has subcommands be invoked on its own, and /bet resolve
has to exist. So the headline action is named - /bet open.

HOW THE BUTTONS REACH PEOPLE, which is the part with no precedent in this
codebase. Opening a bet does NOT post into a channel. It raises an ordinary
server notice (utils/notifications.py) carrying an action_key, and
utils/responses.py hangs this file's two buttons on whatever reply that notice
next rides along with. So a bet reaches a server through the machinery that was
already delivering announcements, and a server that has set /setup messages
private keeps every reply private.

What that inherits is the notice feed's shape: only the newest notice is shown,
and only once. The buttons are therefore a nudge and never the only way in -
/bet status carries the same two, on a reply anybody can ask for at any time,
and /bet place needs no buttons at all.

The buttons are discord.ui.DynamicItem rather than a view held in memory. A bet
runs for hours or days and the bot restarts; a view stored per message would
not survive that, and re-registering one per open bet at startup would mean
holding every live bet in memory to do it. A dynamic item carries its bet id in
its own custom_id and is matched by pattern on arrival, so it keeps working
after any restart and after the view it was sent on has expired - discord.py
matches it before it looks up the message's view at all (discord/ui/view.py:
dispatch_view calls dispatch_dynamic_items first).
"""
import logging
import math
import re

import discord
from discord import app_commands
from discord.ext import commands

from database.db import InsufficientQuantity
from utils.responses import respond, register_notice_action
from utils.embeds import make_embed, footer_with, BET_COLOR
from utils.formatting import format_currency, format_price, format_relative_timestamp
from utils.notifications import mark_seen, post_server_notification, post_user_notification
from utils.db_helpers import ensure_server_row, ensure_user_row, get_currency_balance
from utils.betting import (
    AGAINST,
    FOR,
    MAX_CLOSES_IN_HOURS,
    MAX_PREDICTION_LENGTH,
    MIN_CLOSES_IN_HOURS,
    MIN_STAKE_CENTS,
    SIDE_LABELS,
    BetUnavailable,
    Pools,
    cancel_bet,
    close_bet,
    closes_at_text,
    fetch_bet,
    from_cents,
    hours_until,
    live_bets,
    now_text,
    open_bet,
    place_wager,
    pools_for,
    refresh_status,
    resolve_bet,
    return_multiple,
    to_cents,
)

log = logging.getLogger("dragonhoard")

# The smallest stake, as currency rather than cents, for the command signature
# and the error text.
MIN_STAKE = from_cents(MIN_STAKE_CENTS)

# Discord shows at most 25 autocomplete choices. utils/betting.py: MAX_OPEN_BETS
# is below that, so a server's whole live list always fits and this only ever
# trims a list that is already short.
MAX_CHOICES = 25

# The action kind this cog registers with utils/responses.py. A notice whose
# action_key is "bet:12" gets this cog's two buttons for bet 12.
ACTION_KIND = "bet"

# The custom_id every bet button carries. Namespaced with "dh:" so nothing else
# this bot ever adds can collide with it, and holding the two things a press
# needs to know - which bet, and which way.
BUTTON_TEMPLATE = r"dh:bet:(?P<bet_id>\d+):(?P<side>for|against)"

# Which way round the two buttons read. Green for the side that says the thing
# happens, red for the side that says it doesn't - the two colors Discord's
# palette makes that distinction with.
BUTTON_STYLES = {FOR: discord.ButtonStyle.success, AGAINST: discord.ButtonStyle.danger}
BUTTON_LABELS = {FOR: "Bet for", AGAINST: "Bet against"}


def _clean(text: str) -> str:
    """A player's prediction as it is safe to render.

    Markdown is escaped so a prediction cannot style the embed it is quoted in,
    and every send that carries one passes allowed_mentions=none so it cannot
    ping anybody. This is the first feature in the bot to put arbitrary player
    text in front of other players, so neither guard had a precedent here to
    follow.
    """
    return discord.utils.escape_markdown(text)


def _bet_label(bet) -> str:
    """One bet as a line in an autocomplete: its number and its prediction.

    Trimmed to Discord's 100-character choice-name limit, which a prediction
    alone can exceed - MAX_PREDICTION_LENGTH is twice that. Deliberately no
    pot figure: the label is what a player picks a bet BY, and a number that
    moves between the list being built and the command being run is noise in
    it. /bet status is where the money is.
    """
    label = f"#{bet['bet_id']}: {bet['prediction']}"
    return label if len(label) <= 100 else label[:97] + "..."


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


class BetButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    """One side's button. Pressing it opens the stake modal.

    Deliberately no interaction_check restricting who may press it, unlike
    ManualView in cogs/manual.py. That one guards a reader's own dropdown; this
    is an invitation to everybody in the server, and on a server with public
    replies the whole point is that somebody else's reply is where you find it.
    """

    def __init__(self, bet_id: int, side: str):
        self.bet_id = bet_id
        self.side = side
        super().__init__(
            discord.ui.Button(
                label=BUTTON_LABELS[side],
                style=BUTTON_STYLES[side],
                custom_id=f"dh:bet:{bet_id}:{side}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["bet_id"]), match["side"])

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        bet = await fetch_bet(db, self.bet_id, interaction.guild_id)
        if bet is None:
            # The bet is from another server, or the row is gone. Not an error
            # worth a stack trace - the button simply outlived what it pointed
            # at, which is the cost of a component that survives restarts.
            await interaction.response.send_message(
                "That bet isn't running in this server any more.", ephemeral=True
            )
            return
        await interaction.response.send_modal(StakeModal(bet, self.side))


def _buttons(bet_id: int) -> list[BetButton]:
    """The pair, for a notice or for a /bet status reply."""
    return [BetButton(bet_id, FOR), BetButton(bet_id, AGAINST)]


def _notice_action_items(argument: str) -> list:
    """The registry hook utils/responses.py calls for a "bet:<id>" notice.

    Synchronous and database-free by contract - it runs while delivering some
    unrelated command's reply. It cannot therefore check whether the bet is
    still open; a button on a settled bet explains itself when pressed, which
    is the right side of that trade given what checking would cost every
    command in the bot.
    """
    try:
        bet_id = int(argument)
    except ValueError:
        return []
    return _buttons(bet_id)


# At import rather than in cog_load below, because this is a static mapping and
# nothing about it needs a running bot. add_dynamic_items does need one, which
# is why the two halves of "these buttons work" are registered in different
# places despite describing the same thing.
register_notice_action(ACTION_KIND, _notice_action_items)


class StakeModal(discord.ui.Modal, title="Place your bet"):
    """Asks how much. Opened by a button; /bet place takes the same figure as a
    command argument and runs the same code."""

    amount = discord.ui.TextInput(
        label="How much?",
        placeholder="e.g. 250",
        required=True,
        max_length=20,
    )

    def __init__(self, bet, side: str):
        # Which way this modal is betting goes in the TITLE rather than on the
        # input's label: discord.py 2.7 deprecates writing TextInput.label, and
        # a title is where Discord shows it larger anyway. Held well inside
        # Discord's 45-character cap, which the bet id cannot push past.
        super().__init__(title=f"Bet {SIDE_LABELS[side].lower()} · #{bet['bet_id']}")
        self.bet = bet
        self.side = side

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).strip().replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            amount = None
        # isfinite as well as the parse: "inf" and "nan" are both perfectly
        # good floats to Python and neither survives round(amount * 100). A
        # modal takes whatever somebody types, so this is the one place in the
        # feature where that matters.
        if amount is None or not math.isfinite(amount):
            await interaction.response.send_message(
                f"**{discord.utils.escape_markdown(raw)[:100]}** isn't an amount.",
                ephemeral=True,
            )
            return
        await _stake(interaction, self.bet, self.side, amount)


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------


# How a side of a finished bet came out, for _odds_line.
WON, LOST, RETURNED = "won", "lost", "returned"


def _odds_line(
    pools: Pools, side: str, currency_emoji: str | None, result: str | None = None
) -> str:
    """One side's pool and what it pays - or, once the bet is over, what
    became of it (`result`: WON, LOST or RETURNED; None while it runs).

    The multiple is a TOTAL RETURN - stake included - so there is one number on
    screen and nothing has to say whether the stake is in it. See
    utils/betting.py: return_multiple.

    A finished bet's card is a record: the winning side shows what it was paid,
    the losing side that it lost. Until 1.4.1 both sides read "returned to this
    side", which on the losing side said the opposite of what happened.
    """
    staked = pools.side_cents(side)
    backers = pools.backers(side)
    if staked <= 0:
        return f"{format_currency(0, currency_emoji)} · nobody yet"
    people = f"{backers:,} player{'' if backers == 1 else 's'}"
    head = f"{format_currency(from_cents(staked), currency_emoji)} · {people}"
    if result == LOST:
        return f"{head}\nlost"
    if result == RETURNED:
        return f"{head}\nevery stake returned"
    other = pools.side_cents(FOR if side == AGAINST else AGAINST)
    if other <= 0:
        # Nothing is being bet against this side, so the "odds" would be 1.00x -
        # a true figure that reads as though the bet were a bad one rather than
        # an unopposed one.
        return f"{head}\nnothing against it yet"
    multiple = return_multiple(pools, side)
    outcome = "paid out" if result == WON else "if it wins"
    return f"{head}\n**{multiple:,.2f}x** {outcome}"


def _results(bet, pools: Pools, status: str) -> dict[str, str | None]:
    """What each side's line should say about how the bet ended."""
    if status == "cancelled":
        return {FOR: RETURNED, AGAINST: RETURNED}
    if status != "resolved":
        return {FOR: None, AGAINST: None}
    if pools.backers(bet["outcome"]) == 0:
        # Called toward a side nobody took, so the bet was voided.
        return {FOR: RETURNED, AGAINST: RETURNED}
    loser = FOR if bet["outcome"] == AGAINST else AGAINST
    return {bet["outcome"]: WON, loser: LOST}


def build_bet_embed(bet, pools: Pools, status: str, currency_emoji: str | None) -> discord.Embed:
    """A bet as it stands. The same card for /bet status, for the confirmation
    after a wager, and for the settled record afterwards.

    Two inline fields, one per side; a reply about the caller's own wager adds
    a third, "Your stake", so the row stays one row. The pot was a field of its
    own until 1.4.1 - it is the two sides added up."""
    embed = make_embed(f"🎲 Bet #{bet['bet_id']}", BET_COLOR)
    embed.description = f"> {_clean(bet['prediction'])}\n\nProposed by <@{bet['creator_id']}>."

    if status == "open":
        embed.description += (
            f"\nWagers close {format_relative_timestamp(hours_until(bet['closes_at']))}."
        )
    elif status == "closed":
        embed.description += "\nClosed to new wagers, waiting on an admin to call it."
    elif status == "cancelled":
        embed.description += "\nCancelled."
    else:
        won = SIDE_LABELS[bet["outcome"]]
        if pools.backers(bet["outcome"]) == 0:
            # Called toward a side nobody had taken. Saying "resolved For" flat
            # would read as though the For side had won something, when the
            # bet paid out nothing and every stake went home.
            embed.description += (
                f"\nCalled **{won}** by <@{bet['resolved_by']}>, but nobody had taken that "
                f"side, so the bet was voided."
            )
        else:
            embed.description += f"\nResolved **{won}** by <@{bet['resolved_by']}>."

    results = _results(bet, pools, status)
    embed.add_field(
        name="✅ For", value=_odds_line(pools, FOR, currency_emoji, results[FOR]), inline=True
    )
    embed.add_field(
        name="❌ Against", value=_odds_line(pools, AGAINST, currency_emoji, results[AGAINST]), inline=True
    )
    if status == "open":
        # Only while it can still change. On a closed or settled bet the odds
        # are whatever they ended up as, and saying they move would be wrong.
        embed.set_footer(text=footer_with("odds move as people bet"))
    return embed


def _stake_field(pools: Pools, side: str, staked_cents: int, balance: float, currency_emoji) -> str:
    """The caller's own position, for the third inline field: what they have
    on which side, what it returns at the odds right now, and what their
    balance is down to."""
    line = f"{format_currency(from_cents(staked_cents), currency_emoji)} **{SIDE_LABELS[side].lower()}**"
    multiple = return_multiple(pools, side)
    # Only once somebody is on the other side: before that the "return" is the
    # stake itself, which the line above already says.
    other = pools.side_cents(FOR if side == AGAINST else AGAINST)
    if multiple is not None and other > 0:
        line += f"\npays {format_currency(from_cents(staked_cents) * multiple, currency_emoji)} now"
    return f"{line}\n({format_price(balance)} remaining)"


# ---------------------------------------------------------------------------
# The shared wager path
# ---------------------------------------------------------------------------


async def _stake(interaction: discord.Interaction, bet, side: str, amount: float):
    """Places `amount` on `side` of `bet` and reports it. What a button press
    and /bet place both end up in."""
    db = interaction.client.db
    stake_cents = to_cents(amount)
    if stake_cents < MIN_STAKE_CENTS:
        await interaction.response.send_message(
            f"The smallest bet is {MIN_STAKE:.2f}.", ephemeral=True
        )
        return

    # Reaches Discord, so it happens before the write lock is taken - see the
    # note on Database.transaction.
    currency_emoji = await _currency_emoji(db, interaction.guild_id)

    try:
        async with db.transaction() as tx:
            await ensure_server_row(tx, interaction.guild_id)
            # Re-read inside the transaction: the row the caller is holding was
            # fetched before the lock and the bet may have been resolved since.
            current = await fetch_bet(tx, bet["bet_id"], interaction.guild_id)
            if current is None:
                raise BetUnavailable("That bet isn't running in this server any more.")
            staked = await place_wager(tx, current, interaction.user.id, side, stake_cents)
            pools = await pools_for(tx, current["bet_id"])
            balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
    except BetUnavailable as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    except InsufficientQuantity:
        have = await get_currency_balance(db, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(
            f"You'd need {format_currency(amount, currency_emoji, round_up=True)} for that "
            f"and you have {format_currency(have, currency_emoji)}.",
            ephemeral=True,
        )
        return

    embed = build_bet_embed(bet, pools, "open", currency_emoji)
    embed.add_field(
        name="Your stake",
        value=_stake_field(pools, side, staked, balance, currency_emoji),
        inline=True,
    )
    await respond(interaction, db, embed=embed)


async def _already_seen_by(tx, user_id: int, guild_id: int, notice_id: int) -> None:
    """Marks a server notice the player's own command just raised as read by
    that player, so their reply isn't followed by the server telling them what
    they did (1.4.1). Everyone else still gets it.

    Safe because a server's feed only ever shows its newest notice
    (utils/notifications.py: fetch_unseen) and this one is the newest: marking
    it read hides it and nothing older, which the feed had already superseded.
    """
    await mark_seen(
        tx, user_id, [{"scope": "server", "guild_id": guild_id, "notification_id": notice_id}]
    )


def _may_close(bet, interaction: discord.Interaction) -> bool:
    """Whether the caller may /bet close this bet: the player who opened it,
    or anyone with Manage Server.

    The creator is included so that a bet maker who has already announced the
    result can stop wagers while they wait for an admin to resolve it. That is
    also why this cannot be the has_permissions decorator /bet resolve and
    /bet cancel carry - it would turn the creator away before the command ran.
    interaction.permissions is what that decorator reads, so "admin" means the
    same thing here as it does there.
    """
    return interaction.user.id == bet["creator_id"] or interaction.permissions.manage_guild


async def _currency_emoji(db, guild_id: int) -> str | None:
    row = await db.fetchone(
        "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
    )
    return row["currency_emoji"] if row else None


class BettingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self):
        """Teaches the client to route a button press back to BetButton however
        old the message carrying it is.

        This is the half of "the buttons work" that needs a running bot; the
        other half - teaching utils/responses.py to put them on a notice in the
        first place - is registered at import above.
        """
        self.bot.add_dynamic_items(BetButton)

    bet_group = app_commands.Group(
        name="bet", description="Bet this server's currency on what happens next"
    )

    async def _live_choices(self, interaction: discord.Interaction, current: str):
        """The server's open and closed bets, for every command that names one."""
        if interaction.guild_id is None:
            return []
        rows = await live_bets(self.db, interaction.guild_id)
        needle = current.lower()
        choices = []
        for bet in rows:
            label = _bet_label(bet)
            if needle and needle not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=bet["bet_id"]))
        return choices[:MAX_CHOICES]

    @bet_group.command(name="open", description="Propose an outcome and back it with your own currency")
    @app_commands.describe(
        prediction="What you think will happen",
        amount="How much you're staking that it will",
        closes_in="Hours before wagers close",
    )
    async def bet_open(
        self,
        interaction: discord.Interaction,
        prediction: app_commands.Range[str, 1, MAX_PREDICTION_LENGTH],
        amount: app_commands.Range[float, MIN_STAKE, None],
        closes_in: app_commands.Range[float, MIN_CLOSES_IN_HOURS, MAX_CLOSES_IN_HOURS],
    ):
        stake_cents = to_cents(amount)
        if stake_cents < MIN_STAKE_CENTS:
            await interaction.response.send_message(
                f"The smallest bet is {MIN_STAKE:.2f}.", ephemeral=True
            )
            return

        currency_emoji = await _currency_emoji(self.db, interaction.guild_id)

        try:
            async with self.db.transaction() as tx:
                await ensure_server_row(tx, interaction.guild_id)
                await ensure_user_row(tx, interaction.user.id)
                bet_id = await open_bet(
                    tx,
                    interaction.guild_id,
                    interaction.user.id,
                    prediction,
                    closes_at_text(closes_in),
                )
                bet = await fetch_bet(tx, bet_id, interaction.guild_id)
                # The proposer is always ON the for side. Proposing an outcome
                # and staking against it is not a prediction, and somebody has
                # to put the first money up or there is nothing to oppose.
                await place_wager(tx, bet, interaction.user.id, FOR, stake_cents)
                pools = await pools_for(tx, bet_id)
                # The notice and the bet commit together: a notice announcing a
                # bet that rolled back would send a server chasing a bet id
                # that was never created.
                notice_id = await post_server_notification(
                    tx,
                    interaction.guild_id,
                    f"🎲 New bet: {prediction}",
                    f"<@{interaction.user.id}> is staking "
                    f"{format_currency(amount, currency_emoji)} that this happens, and wagers "
                    f"close {format_relative_timestamp(closes_in)}. Back them or take the other "
                    f"side with the buttons below, or with `/bet place bet:{bet_id}`.",
                    action_key=f"{ACTION_KIND}:{bet_id}",
                )
                await _already_seen_by(tx, interaction.user.id, interaction.guild_id, notice_id)
                balance = await get_currency_balance(tx, interaction.guild_id, interaction.user.id)
        except BetUnavailable as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except InsufficientQuantity:
            have = await get_currency_balance(self.db, interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(
                f"You'd need {format_currency(amount, currency_emoji, round_up=True)} to open "
                f"that and you have {format_currency(have, currency_emoji)}.",
                ephemeral=True,
            )
            return

        embed = build_bet_embed(bet, pools, "open", currency_emoji)
        embed.add_field(
            name="Your stake",
            value=_stake_field(pools, FOR, stake_cents, balance, currency_emoji),
            inline=True,
        )
        view = discord.ui.View(timeout=None)
        for button in _buttons(bet_id):
            view.add_item(button)
        await respond(
            interaction,
            self.db,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bet_group.command(name="place", description="Back or oppose one of this server's open bets")
    @app_commands.describe(bet="Which bet", side="Which way", amount="How much to stake")
    @app_commands.autocomplete(bet=_live_choices)
    @app_commands.choices(side=[
        app_commands.Choice(name=SIDE_LABELS[FOR], value=FOR),
        app_commands.Choice(name=SIDE_LABELS[AGAINST], value=AGAINST),
    ])
    async def bet_place(
        self,
        interaction: discord.Interaction,
        bet: int,
        side: app_commands.Choice[str],
        amount: app_commands.Range[float, MIN_STAKE, None],
    ):
        row = await fetch_bet(self.db, bet, interaction.guild_id)
        if row is None:
            await interaction.response.send_message(
                "There's no bet with that number in this server.", ephemeral=True
            )
            return
        await _stake(interaction, row, side.value, amount)

    @bet_group.command(name="status", description="What's riding on a bet, or every bet running here")
    @app_commands.describe(bet="A specific bet - leave blank to list them all")
    @app_commands.autocomplete(bet=_live_choices)
    async def bet_status(self, interaction: discord.Interaction, bet: int | None = None):
        currency_emoji = await _currency_emoji(self.db, interaction.guild_id)

        if bet is None:
            await self._status_list(interaction, currency_emoji)
            return

        row = await fetch_bet(self.db, bet, interaction.guild_id)
        if row is None:
            await interaction.response.send_message(
                "There's no bet with that number in this server.", ephemeral=True
            )
            return

        # A plain read unless the deadline has actually passed. refresh_status
        # can write, so the write-lock-taking transaction is opened only when
        # there is something to write - /bet status is otherwise a read, and it
        # should not queue behind every other command in the bot to do it. The
        # check-then-write is safe to do this way round because the UPDATE is
        # guarded on `status = 'open'`, so two callers racing to close the same
        # bet both end up with it closed once.
        needs_closing = row["status"] == "open" and row["closes_at"] <= now_text()
        if needs_closing:
            async with self.db.transaction() as tx:
                status = await refresh_status(tx, row)
                pools = await pools_for(tx, row["bet_id"])
        else:
            status = row["status"]
            pools = await pools_for(self.db, row["bet_id"])

        embed = build_bet_embed(row, pools, status, currency_emoji)
        kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
        if status == "open":
            # The durable home of the two buttons. The notice that announced
            # this bet is shown once and then gone; this reply can be asked for
            # whenever somebody wants it.
            view = discord.ui.View(timeout=None)
            for button in _buttons(row["bet_id"]):
                view.add_item(button)
            kwargs["view"] = view
        await respond(interaction, self.db, **kwargs)

    async def _status_list(self, interaction: discord.Interaction, currency_emoji: str | None):
        rows = await live_bets(self.db, interaction.guild_id)
        embed = make_embed("🎲 Bets", BET_COLOR)
        if not rows:
            embed.description = (
                "Nothing is running here. `/bet open` proposes something and stakes the first "
                "currency on it."
            )
            await respond(interaction, self.db, embed=embed)
            return

        embed.description = (
            f"{len(rows)} bet{'' if len(rows) == 1 else 's'} running. "
            f"`/bet status` with a number opens one."
        )
        for row in rows:
            pools = await pools_for(self.db, row["bet_id"])
            closing = (
                f"closes {format_relative_timestamp(hours_until(row['closes_at']))}"
                if row["status"] == "open"
                else "closed, waiting to be resolved"
            )
            embed.add_field(
                name=f"#{row['bet_id']}",
                value=(
                    f"> {_clean(row['prediction'])}\n"
                    f"{format_currency(from_cents(pools.pot_cents), currency_emoji)} in the pot · "
                    f"{closing}"
                ),
                inline=False,
            )
        await respond(
            interaction, self.db, embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _closable_choices(self, interaction: discord.Interaction, current: str):
        """The bets /bet close could close for this caller: open ones only, and
        for anybody without Manage Server only the ones they opened. A
        convenience - _may_close in the command is the enforcement."""
        if interaction.guild_id is None:
            return []
        rows = await live_bets(self.db, interaction.guild_id)
        now = now_text()
        needle = current.lower()
        choices = []
        for bet in rows:
            # Open by status but past its deadline is closed in every way that
            # matters; refresh_status just has not been asked yet.
            if bet["status"] != "open" or bet["closes_at"] <= now:
                continue
            if not _may_close(bet, interaction):
                continue
            label = _bet_label(bet)
            if needle and needle not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=bet["bet_id"]))
        return choices[:MAX_CHOICES]

    @bet_group.command(name="close", description="Stop a bet taking wagers before its deadline")
    @app_commands.describe(bet="Which bet to close")
    @app_commands.autocomplete(bet=_closable_choices)
    async def bet_close(self, interaction: discord.Interaction, bet: int):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Bets belong to a server - run this in the one the bet is in.", ephemeral=True
            )
            return

        currency_emoji = await _currency_emoji(self.db, interaction.guild_id)
        row = await fetch_bet(self.db, bet, interaction.guild_id)
        if row is None:
            await interaction.response.send_message(
                "There's no bet with that number in this server.", ephemeral=True
            )
            return
        # Checked against the row read before the lock: creator_id is written
        # once, when the bet is opened, so the re-read below cannot change it.
        if not _may_close(row, interaction):
            await interaction.response.send_message(
                "Only the player who opened this bet, or someone with Manage Server, can "
                "close it.",
                ephemeral=True,
            )
            return

        try:
            async with self.db.transaction() as tx:
                current = await fetch_bet(tx, bet, interaction.guild_id)
                if current is None:
                    raise BetUnavailable("That bet isn't in this server any more.")
                await close_bet(tx, current)
                pools = await pools_for(tx, bet)
                notice_id = await post_server_notification(
                    tx,
                    interaction.guild_id,
                    f"🎲 Bet #{bet} closed early",
                    f"<@{interaction.user.id}> closed it to new wagers. It's waiting on an admin "
                    f"to call it. The bet was: {current['prediction']}",
                )
                await _already_seen_by(tx, interaction.user.id, interaction.guild_id, notice_id)
        except BetUnavailable as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        embed = build_bet_embed(row, pools, "closed", currency_emoji)
        await respond(
            interaction, self.db, embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @bet_group.command(name="resolve", description="Call a bet and pay out the pot")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(bet="Which bet", outcome="Which side was right")
    @app_commands.autocomplete(bet=_live_choices)
    @app_commands.choices(outcome=[
        app_commands.Choice(name="It happened (For wins)", value=FOR),
        app_commands.Choice(name="It didn't (Against wins)", value=AGAINST),
    ])
    async def bet_resolve(
        self, interaction: discord.Interaction, bet: int, outcome: app_commands.Choice[str]
    ):
        currency_emoji = await _currency_emoji(self.db, interaction.guild_id)
        row = await fetch_bet(self.db, bet, interaction.guild_id)
        if row is None:
            await interaction.response.send_message(
                "There's no bet with that number in this server.", ephemeral=True
            )
            return

        try:
            async with self.db.transaction() as tx:
                current = await fetch_bet(tx, bet, interaction.guild_id)
                if current is None:
                    raise BetUnavailable("That bet isn't in this server any more.")
                pools = await pools_for(tx, bet)
                settlement = await resolve_bet(
                    tx, current, outcome.value, interaction.user.id
                )
                await self._announce_settlement(
                    tx, current, settlement, currency_emoji,
                    actor_id=interaction.user.id,
                    headline=(
                        f"🎲 Bet #{bet} was voided"
                        if settlement.voided
                        else f"🎲 Bet #{bet}: {SIDE_LABELS[outcome.value]} wins"
                    ),
                )
        except BetUnavailable as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        settled = await fetch_bet(self.db, bet, interaction.guild_id)
        embed = build_bet_embed(settled, pools, "resolved", currency_emoji)
        embed.insert_field_at(
            0,
            name="Voided" if settlement.voided else "Paid out",
            value=self._settlement_text(settlement, outcome.value, currency_emoji),
            inline=False,
        )
        await respond(
            interaction, self.db, embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @bet_group.command(name="cancel", description="Void a bet and hand every stake back")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(bet="Which bet to void")
    @app_commands.autocomplete(bet=_live_choices)
    async def bet_cancel(self, interaction: discord.Interaction, bet: int):
        currency_emoji = await _currency_emoji(self.db, interaction.guild_id)
        row = await fetch_bet(self.db, bet, interaction.guild_id)
        if row is None:
            await interaction.response.send_message(
                "There's no bet with that number in this server.", ephemeral=True
            )
            return

        try:
            async with self.db.transaction() as tx:
                current = await fetch_bet(tx, bet, interaction.guild_id)
                if current is None:
                    raise BetUnavailable("That bet isn't in this server any more.")
                pools = await pools_for(tx, bet)
                settlement = await cancel_bet(tx, current)
                await self._announce_settlement(
                    tx, current, settlement, currency_emoji,
                    actor_id=interaction.user.id,
                    headline=f"🎲 Bet #{bet} was cancelled",
                )
        except BetUnavailable as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        settled = await fetch_bet(self.db, bet, interaction.guild_id)
        embed = build_bet_embed(settled, pools, "cancelled", currency_emoji)
        embed.insert_field_at(
            0,
            name="Refunded",
            value=(
                f"{format_currency(from_cents(settlement.pot_cents), currency_emoji)} back to "
                f"{len(settlement.payouts)} player{'' if len(settlement.payouts) == 1 else 's'}"
            ),
            inline=False,
        )
        await respond(
            interaction, self.db, embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    def _settlement_text(self, settlement, outcome: str, currency_emoji: str | None) -> str:
        pot = format_currency(from_cents(settlement.pot_cents), currency_emoji)
        if settlement.voided:
            return f"{pot} back to whoever staked it · nobody took **{SIDE_LABELS[outcome]}**"
        winners = len(settlement.payouts)
        return (
            f"{pot} to {winners} player{'' if winners == 1 else 's'} on "
            f"**{SIDE_LABELS[outcome]}**"
        )

    async def _announce_settlement(
        self, tx, bet, settlement, currency_emoji: str | None, *, headline: str, actor_id: int
    ):
        """Tells the server how a bet ended, and each winner what they got.

        Both inside the caller's transaction, so what a player is told and what
        their balance says can never disagree - post_user_notification is
        documented as safe there and this is exactly the case it describes.

        The personal notice's key carries the bet id, which makes raising it
        idempotent per player per bet: a resolve that somehow ran twice would
        insert nothing the second time.
        """
        notice_id = await post_server_notification(
            tx,
            bet["guild_id"],
            headline,
            f"The bet was: {bet['prediction']}",
        )
        await _already_seen_by(tx, actor_id, bet["guild_id"], notice_id)
        for user_id, paid in settlement.payouts.items():
            if paid <= 0:
                continue
            await post_user_notification(
                tx,
                user_id,
                f"bet_payout:{bet['bet_id']}",
                "🎲 Your bet paid out",
                f"Bet #{bet['bet_id']} ({bet['prediction']}) returned "
                f"{format_currency(from_cents(paid), currency_emoji)} to you.",
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(BettingCog(bot))
