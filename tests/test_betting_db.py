"""
Tests for /bet against a real database (cogs/betting.py, utils/betting.py).

ConservationTests is the class that matters. Everything else pins behaviour a
player would notice; that one pins the promise the feature was built around -
no currency is created or destroyed by a bet - and it is the failure that
would be silent.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import discord
from discord import app_commands

from cogs.betting import ACTION_KIND, BettingCog, StakeModal, _notice_action_items
from database.db import Database
from utils.betting import (
    AGAINST,
    FOR,
    MAX_OPEN_BETS,
    fetch_bet,
    pools_for,
    to_cents,
)
from utils.db_helpers import (
    adjust_currency_balance,
    circulating_currency_for,
    ensure_server_row,
    ensure_user_row,
    get_currency_balance,
)
from utils.betting import MAX_PREDICTION_LENGTH
from utils.responses import _action_items, respond

GUILD = 9090
OTHER_GUILD = 9091
ALICE = 111
BOB = 222
CARA = 333
DAVE = 444
ADMIN = 999

EVERYONE = (ALICE, BOB, CARA, DAVE, ADMIN)

# What each player starts with, in currency. Comfortably above every stake in
# this file so that "you cannot afford it" is only ever tested deliberately.
STARTING_BALANCE = 10_000.0


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.display_name = f"User{user_id}"


class FakeClient:
    """Enough of commands.Bot for the button path, which reaches the database
    through interaction.client.db the way bot.py wires it."""

    def __init__(self, db):
        self.db = db


class FakeInteraction:
    def __init__(self, user_id, db, guild_id=GUILD):
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.client = FakeClient(db)
        self.response = AsyncMock()

    @property
    def sent(self):
        """The plain-text rejection this interaction received, or None if it
        was answered with an embed instead."""
        call = self.response.send_message.call_args
        if call is None or not call.args:
            return None
        return call.args[0]

    @property
    def embed(self):
        call = self.response.send_message.call_args
        if call is None:
            return None
        embeds = call.kwargs.get("embeds")
        return embeds[0] if embeds else call.kwargs.get("embed")

    @property
    def view(self):
        call = self.response.send_message.call_args
        return None if call is None else call.kwargs.get("view")


class BettingTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        for user in EVERYONE:
            await ensure_user_row(self.db, user)
            await adjust_currency_balance(self.db, GUILD, user, STARTING_BALANCE)
        self.cog = BettingCog.__new__(BettingCog)
        self.cog.db = self.db
        self.cog.bot = None

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    # -- command shims ------------------------------------------------------
    # Each command is wrapped in an app_commands.Command by its decorator, so
    # the cog has to be passed to the raw callback by hand. The permission
    # check on resolve/cancel lives on the wrapper and is asserted separately
    # in PermissionTests rather than exercised through here.

    async def open(self, user, prediction="Will it rain?", amount=10.0, closes_in=24):
        i = FakeInteraction(user, self.db)
        await BettingCog.bet_open.callback(self.cog, i, prediction, amount, closes_in)
        return i

    async def place(self, user, bet_id, side, amount):
        i = FakeInteraction(user, self.db)
        choice = app_commands.Choice(name=side, value=side)
        await BettingCog.bet_place.callback(self.cog, i, bet_id, choice, amount)
        return i

    async def resolve(self, bet_id, outcome, user=ADMIN):
        i = FakeInteraction(user, self.db)
        choice = app_commands.Choice(name=outcome, value=outcome)
        await BettingCog.bet_resolve.callback(self.cog, i, bet_id, choice)
        return i

    async def cancel(self, bet_id, user=ADMIN):
        i = FakeInteraction(user, self.db)
        await BettingCog.bet_cancel.callback(self.cog, i, bet_id)
        return i

    async def status(self, user, bet_id=None):
        i = FakeInteraction(user, self.db)
        await BettingCog.bet_status.callback(self.cog, i, bet_id)
        return i

    # -- helpers ------------------------------------------------------------

    async def newest_bet_id(self) -> int:
        row = await self.db.fetchone("SELECT MAX(bet_id) AS id FROM prediction_bets")
        return row["id"]

    async def balances(self) -> dict[int, float]:
        return {user: await get_currency_balance(self.db, GUILD, user) for user in EVERYONE}

    async def totals(self):
        row = await self.db.fetchone(
            "SELECT currency_minted_total, currency_burned_total FROM server_config "
            "WHERE guild_id = ?", (GUILD,)
        )
        return row["currency_minted_total"], row["currency_burned_total"]

    async def expire(self, bet_id):
        """Drags a bet's deadline into the past, which is what the clock would
        otherwise have to do."""
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        await self.db.execute(
            "UPDATE prediction_bets SET closes_at = ? WHERE bet_id = ?", (past, bet_id)
        )


class ConservationTests(BettingTestCase):
    """Currency may move between players. It may not appear or disappear.

    Each test here takes the whole server's balances before and after and
    asserts the total is untouched, which is the only formulation that cannot
    be satisfied by an accounting error that happens to be symmetric.
    """

    async def total_held(self) -> float:
        balances = await self.balances()
        return sum(balances.values())

    async def test_a_resolved_bet_moves_currency_without_creating_any(self):
        before = await self.total_held()
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 300.0)
        await self.place(CARA, bet_id, AGAINST, 150.0)
        await self.place(DAVE, bet_id, FOR, 50.0)
        await self.resolve(bet_id, FOR)

        self.assertAlmostEqual(await self.total_held(), before, places=6)

    async def test_the_winners_gain_exactly_what_the_losers_lost(self):
        """Measured across the whole bet, not across the resolve. A stake is
        taken when the wager is placed, so by resolution time the losers have
        already paid and the payout looks one-sided - which is exactly the
        shape that would hide a leak."""
        before = await self.balances()
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 300.0)
        await self.place(DAVE, bet_id, FOR, 50.0)
        await self.resolve(bet_id, FOR)
        after = await self.balances()

        gained = sum(after[u] - before[u] for u in EVERYONE if after[u] > before[u])
        lost = sum(before[u] - after[u] for u in EVERYONE if after[u] < before[u])
        self.assertAlmostEqual(gained, lost, places=6)
        # A 450 pot on a 150 winning pool trebles both winners' stakes.
        self.assertAlmostEqual(after[ALICE] - before[ALICE], 200.0, places=6)
        self.assertAlmostEqual(after[DAVE] - before[DAVE], 100.0, places=6)
        # The loser is out their stake and not a unit more.
        self.assertAlmostEqual(before[BOB] - after[BOB], 300.0, places=6)

    async def test_a_cancelled_bet_returns_every_balance_to_where_it_started(self):
        before = await self.balances()
        await self.open(ALICE, amount=250.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 75.25)
        await self.place(CARA, bet_id, FOR, 0.01)
        await self.cancel(bet_id)

        self.assertEqual(await self.balances(), before)

    async def test_an_awkward_split_still_conserves_to_the_cent(self):
        """Three equal stakes against one, so the pot does not divide evenly
        between the winners and the largest-remainder rule has to place the odd
        cents somewhere."""
        before = await self.total_held()
        await self.open(ALICE, amount=0.01)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, FOR, 0.01)
        await self.place(CARA, bet_id, FOR, 0.01)
        await self.place(DAVE, bet_id, AGAINST, 0.07)
        await self.resolve(bet_id, FOR)

        self.assertAlmostEqual(await self.total_held(), before, places=6)
        paid = await self.db.fetchall(
            "SELECT payout_cents FROM prediction_wagers WHERE bet_id = ?", (bet_id,)
        )
        self.assertEqual(sum(row["payout_cents"] for row in paid), 10)

    async def test_a_bet_mints_and_burns_nothing(self):
        """A wager is neither a faucet nor a sink - it is a transfer, exactly
        as /donate player is (docs/market.md section 1). The running totals
        must not move for any of it."""
        before = await self.totals()
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 300.0)
        self.assertEqual(await self.totals(), before)
        await self.resolve(bet_id, AGAINST)
        self.assertEqual(await self.totals(), before)

    async def test_escrow_does_not_shrink_the_money_supply(self):
        """A stake leaves its owner's balance but not the economy, so
        circulating_currency has to keep counting it - the same property
        tests/test_player_market.py pins for an open order."""
        before = await circulating_currency_for(self.db, GUILD)
        await self.open(ALICE, amount=500.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 250.0)

        self.assertAlmostEqual(
            await circulating_currency_for(self.db, GUILD), before, places=6
        )
        # And it is still right once the pot has been paid out, when the
        # currency is back in balances and the escrow is gone.
        await self.resolve(bet_id, FOR)
        self.assertAlmostEqual(
            await circulating_currency_for(self.db, GUILD), before, places=6
        )

    async def test_a_cancelled_bet_stops_counting_as_escrow(self):
        await self.open(ALICE, amount=500.0)
        bet_id = await self.newest_bet_id()
        before = await circulating_currency_for(self.db, GUILD)
        await self.cancel(bet_id)
        self.assertAlmostEqual(
            await circulating_currency_for(self.db, GUILD), before, places=6
        )


class PayoutTests(BettingTestCase):
    async def test_the_pot_is_split_in_proportion_to_stake(self):
        await self.open(ALICE, amount=100.0)      # for
        bet_id = await self.newest_bet_id()
        await self.place(DAVE, bet_id, FOR, 300.0)
        await self.place(BOB, bet_id, AGAINST, 400.0)
        before = await self.balances()
        await self.resolve(bet_id, FOR)
        after = await self.balances()

        # An 800 pot, a 400 winning pool: every winner doubles.
        self.assertAlmostEqual(after[ALICE] - before[ALICE], 200.0, places=6)
        self.assertAlmostEqual(after[DAVE] - before[DAVE], 600.0, places=6)

    async def test_an_unopposed_bet_hands_the_stakes_straight_back(self):
        """Nobody took the other side, so the pot is the winners' own money and
        the formula pays each of them exactly what they staked."""
        before = await self.balances()
        await self.open(ALICE, amount=120.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, FOR, 80.0)
        await self.resolve(bet_id, FOR)
        self.assertEqual(await self.balances(), before)

    async def test_a_side_nobody_backed_voids_the_bet(self):
        """There is no honest way to resolve toward a side with no backers -
        the losing stakes cannot be paid to nobody and cannot be kept."""
        before = await self.balances()
        await self.open(ALICE, amount=120.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, FOR, 80.0)
        interaction = await self.resolve(bet_id, AGAINST)

        self.assertEqual(await self.balances(), before)
        self.assertEqual(interaction.embed.fields[0].name, "Voided")
        self.assertIn("no winner to pay", interaction.embed.fields[0].value)
        # And the card does not claim the side that "won" won anything.
        self.assertIn("nobody had taken that side", interaction.embed.description)
        bet = await fetch_bet(self.db, bet_id)
        self.assertEqual(bet["status"], "resolved")
        self.assertEqual(bet["outcome"], AGAINST)

    async def test_losing_wagers_are_recorded_as_paid_nothing(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 100.0)
        await self.resolve(bet_id, FOR)

        row = await self.db.fetchone(
            "SELECT payout_cents FROM prediction_wagers WHERE bet_id = ? AND user_id = ?",
            (bet_id, BOB),
        )
        # 0, not NULL: NULL means "has not settled", and this one has.
        self.assertEqual(row["payout_cents"], 0)

    async def test_a_winner_is_told_what_they_got(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 100.0)
        await self.resolve(bet_id, FOR)

        notices = await self.db.fetchall(
            "SELECT user_id, notice_key FROM user_notifications WHERE notice_key = ?",
            (f"bet_payout:{bet_id}",),
        )
        self.assertEqual([row["user_id"] for row in notices], [ALICE])


class StakeTests(BettingTestCase):
    async def test_a_stake_leaves_the_balance_immediately(self):
        await self.open(ALICE, amount=250.0)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, ALICE),
            STARTING_BALANCE - 250.0,
            places=6,
        )

    async def test_topping_up_adds_to_the_same_side(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(ALICE, bet_id, FOR, 50.0)

        rows = await self.db.fetchall(
            "SELECT side, stake_cents FROM prediction_wagers WHERE bet_id = ? AND user_id = ?",
            (bet_id, ALICE),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stake_cents"], to_cents(150.0))

    async def test_a_player_cannot_take_both_sides(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        interaction = await self.place(ALICE, bet_id, AGAINST, 50.0)

        self.assertIn("locked in", interaction.sent)
        pools = await pools_for(self.db, bet_id)
        self.assertEqual(pools.against_cents, 0)
        # And the refused stake was not taken.
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, ALICE),
            STARTING_BALANCE - 100.0,
            places=6,
        )

    async def test_a_stake_bigger_than_the_balance_is_refused_whole(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        interaction = await self.place(BOB, bet_id, AGAINST, STARTING_BALANCE * 2)

        self.assertIn("you have", interaction.sent.lower())
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE, places=6
        )
        self.assertEqual((await pools_for(self.db, bet_id)).against_cents, 0)

    async def test_wagers_stop_when_the_bet_closes(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.expire(bet_id)
        interaction = await self.place(BOB, bet_id, AGAINST, 50.0)

        self.assertIn("closed", interaction.sent)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE, places=6
        )

    async def test_a_passed_deadline_moves_the_bet_to_closed(self):
        """Nothing sweeps for this - the status flips the next time anything
        touches the bet, which is what makes a sixth background loop
        unnecessary."""
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.expire(bet_id)
        self.assertEqual((await fetch_bet(self.db, bet_id))["status"], "open")

        await self.status(BOB, bet_id)
        self.assertEqual((await fetch_bet(self.db, bet_id))["status"], "closed")

    async def test_a_closed_bet_can_still_be_resolved(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 100.0)
        await self.expire(bet_id)
        await self.resolve(bet_id, FOR)
        self.assertEqual((await fetch_bet(self.db, bet_id))["status"], "resolved")


class SettlementRefusalTests(BettingTestCase):
    async def test_a_bet_cannot_be_resolved_twice(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 100.0)
        await self.resolve(bet_id, FOR)
        before = await self.balances()

        interaction = await self.resolve(bet_id, FOR)
        self.assertIn("already been resolved", interaction.sent)
        self.assertEqual(await self.balances(), before)

    async def test_a_resolved_bet_cannot_be_cancelled(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 100.0)
        await self.resolve(bet_id, FOR)
        before = await self.balances()

        interaction = await self.cancel(bet_id)
        self.assertIn("cannot be unwound", interaction.sent)
        self.assertEqual(await self.balances(), before)

    async def test_a_cancelled_bet_cannot_be_resolved(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.cancel(bet_id)
        before = await self.balances()

        interaction = await self.resolve(bet_id, FOR)
        self.assertIn("cancelled", interaction.sent)
        self.assertEqual(await self.balances(), before)

    async def test_a_cancelled_bet_takes_no_more_wagers(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.cancel(bet_id)
        interaction = await self.place(BOB, bet_id, AGAINST, 50.0)

        self.assertIn("cancelled", interaction.sent)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE, places=6
        )


class ModalTests(BettingTestCase):
    """The stake modal takes free text, which is the one place in the feature
    somebody can type something that is not a number at all."""

    async def submit(self, bet_id, side, typed):
        bet = await fetch_bet(self.db, bet_id)
        modal = StakeModal(bet, side)
        modal.amount._value = typed
        interaction = FakeInteraction(BOB, self.db)
        await modal.on_submit(interaction)
        return interaction

    async def test_a_plain_number_is_staked(self):
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        await self.submit(bet_id, AGAINST, "25.50")

        self.assertEqual((await pools_for(self.db, bet_id)).against_cents, to_cents(25.50))

    async def test_thousands_separators_are_accepted(self):
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        await self.submit(bet_id, AGAINST, "1,250")

        self.assertEqual((await pools_for(self.db, bet_id)).against_cents, to_cents(1_250))

    async def test_nonsense_is_refused_without_taking_anything(self):
        """'inf' and 'nan' are the ones worth naming: both parse as perfectly
        good floats and neither survives being rounded into cents."""
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        for typed in ("", "lots", "inf", "-inf", "nan", "1e400"):
            with self.subTest(typed=typed):
                interaction = await self.submit(bet_id, AGAINST, typed)
                self.assertIn("isn't an amount", interaction.sent)
        self.assertEqual((await pools_for(self.db, bet_id)).against_cents, 0)
        self.assertAlmostEqual(
            await get_currency_balance(self.db, GUILD, BOB), STARTING_BALANCE, places=6
        )

    async def test_a_stake_below_a_cent_is_refused(self):
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        interaction = await self.submit(bet_id, AGAINST, "0.004")

        self.assertIn("smallest bet", interaction.sent)
        self.assertEqual((await pools_for(self.db, bet_id)).against_cents, 0)


class ScopeTests(BettingTestCase):
    async def test_a_bet_cannot_be_reached_from_another_server(self):
        """Bet ids are global (one AUTOINCREMENT table), so every lookup is
        scoped by guild - otherwise one server's admin could resolve another
        server's bet by typing its number."""
        await ensure_server_row(self.db, OTHER_GUILD)
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()

        interaction = FakeInteraction(ADMIN, self.db, guild_id=OTHER_GUILD)
        choice = app_commands.Choice(name=FOR, value=FOR)
        await BettingCog.bet_resolve.callback(self.cog, interaction, bet_id, choice)

        self.assertIn("no bet with that number", interaction.sent)
        self.assertEqual((await fetch_bet(self.db, bet_id))["status"], "open")

    async def test_the_open_bet_cap_is_enforced(self):
        for n in range(MAX_OPEN_BETS):
            await self.open(ALICE, prediction=f"Prediction {n}", amount=1.0)
        interaction = await self.open(ALICE, prediction="One too many", amount=1.0)

        self.assertIn("which is the limit", interaction.sent)
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM prediction_bets WHERE guild_id = ?", (GUILD,)
        )
        self.assertEqual(row["n"], MAX_OPEN_BETS)

    async def test_a_settled_bet_frees_a_slot(self):
        for n in range(MAX_OPEN_BETS):
            await self.open(ALICE, prediction=f"Prediction {n}", amount=1.0)
        await self.cancel(await self.newest_bet_id())

        interaction = await self.open(ALICE, prediction="Room again", amount=1.0)
        self.assertIsNone(interaction.sent)


class NoticeTests(BettingTestCase):
    async def test_opening_a_bet_raises_a_notice_carrying_its_buttons(self):
        await self.open(ALICE, prediction="Will it rain?", amount=10.0)
        bet_id = await self.newest_bet_id()

        row = await self.db.fetchone(
            "SELECT title, body, action_key FROM notifications WHERE scope = 'server' "
            "AND guild_id = ? ORDER BY notification_id DESC",
            (GUILD,),
        )
        self.assertEqual(row["action_key"], f"{ACTION_KIND}:{bet_id}")
        self.assertIn("Will it rain?", row["title"])

    async def test_the_registry_turns_that_key_into_two_buttons(self):
        """The whole delivery mechanism in one assertion: a notice row goes in,
        the components that reach the player come out."""
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        rows = await self.db.fetchall(
            "SELECT action_key FROM notifications WHERE guild_id = ?", (GUILD,)
        )
        items = _action_items(rows)

        self.assertEqual(
            sorted(item.custom_id for item in items),
            [f"dh:bet:{bet_id}:against", f"dh:bet:{bet_id}:for"],
        )

    async def test_a_malformed_action_key_is_ignored_rather_than_raised(self):
        """This runs while delivering an unrelated command's reply, so a stale
        or broken action must never be what fails that reply."""
        for action_key in (None, "", "bet", "bet:not-a-number", "unregistered:7"):
            with self.subTest(action_key=action_key):
                self.assertEqual(_action_items([{"action_key": action_key}]), [])

    async def test_settling_tells_the_server_how_it_went(self):
        await self.open(ALICE, amount=100.0)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 100.0)
        await self.resolve(bet_id, FOR)

        row = await self.db.fetchone(
            "SELECT title, action_key FROM notifications WHERE scope = 'server' "
            "AND guild_id = ? ORDER BY notification_id DESC",
            (GUILD,),
        )
        self.assertIn(f"#{bet_id}", row["title"])
        # No action on a settled bet - there is nothing left to press.
        self.assertIsNone(row["action_key"])

    async def test_the_buttons_survive_a_bet_they_no_longer_match(self):
        """A button outlives the bet it points at, because it outlives the
        process. Building one for a settled bet must not raise - the press
        explains itself instead."""
        self.assertEqual(len(_notice_action_items("12345")), 2)


class StatusTests(BettingTestCase):
    async def test_an_open_bet_carries_the_buttons_on_its_status_reply(self):
        """The durable home of the buttons: the notice is shown once, this can
        be asked for whenever."""
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        interaction = await self.status(BOB, bet_id)

        self.assertIsNotNone(interaction.view)
        self.assertEqual(len(interaction.view.children), 2)

    async def test_the_notice_does_not_duplicate_buttons_the_reply_already_has(self):
        """The reply to /bet status #N carries that bet's two buttons, and the
        notice announcing the same bet wants to add the same two. Discord
        rejects a message whose components share a custom_id, so one set has to
        be dropped."""
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        # BOB has not seen the opening notice yet, so this reply carries both.
        interaction = await self.status(BOB, bet_id)

        custom_ids = [child.custom_id for child in interaction.view.children]
        self.assertEqual(len(custom_ids), len(set(custom_ids)))
        self.assertEqual(
            sorted(custom_ids), [f"dh:bet:{bet_id}:against", f"dh:bet:{bet_id}:for"]
        )

    async def test_a_settled_bet_carries_none(self):
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()
        await self.cancel(bet_id)
        interaction = await self.status(BOB, bet_id)

        self.assertIsNone(interaction.view)

    async def test_the_list_names_every_live_bet(self):
        await self.open(ALICE, prediction="First one", amount=1.0)
        await self.open(BOB, prediction="Second one", amount=1.0)
        interaction = await self.status(CARA)

        names = {field.name for field in interaction.embed.fields}
        self.assertEqual(len(names), 2)

    async def test_the_list_says_so_when_there_is_nothing(self):
        interaction = await self.status(ALICE)
        self.assertIn("Nothing is running here", interaction.embed.description)

    async def test_a_prediction_cannot_style_or_ping_its_way_out_of_the_embed(self):
        """The first player-supplied text this bot puts in front of other
        players, so both guards are pinned: markdown is escaped, and no send
        carrying one is allowed to resolve a mention."""
        await self.open(ALICE, prediction="**bold** <@everyone>", amount=1.0)
        interaction = await self.status(BOB, await self.newest_bet_id())

        self.assertIn(r"\*\*bold\*\*", interaction.embed.description)
        mentions = interaction.response.send_message.call_args.kwargs["allowed_mentions"]
        self.assertEqual((mentions.users, mentions.roles, mentions.everyone), (False, False, False))


class DeliveryTests(BettingTestCase):
    """The claim the whole delivery design rests on: a bet's buttons reach a
    player on the reply to whatever command they happen to run next, with no
    channel post and no extra query.

    Exercised through respond() itself rather than through some particular
    command, because respond() is the mechanism - every command funnels through
    it, so what is true here is true for all of them.
    """

    async def test_an_unrelated_reply_carries_the_bet_buttons(self):
        await self.open(ALICE, amount=10.0)
        bet_id = await self.newest_bet_id()

        # BOB running anything at all. No bet command, no channel post.
        interaction = FakeInteraction(BOB, self.db)
        await respond(interaction, self.db, embed=discord.Embed(title="Some other command"))

        view = interaction.response.send_message.call_args.kwargs["view"]
        self.assertEqual(
            sorted(child.custom_id for child in view.children),
            [f"dh:bet:{bet_id}:against", f"dh:bet:{bet_id}:for"],
        )
        # The command's own answer still comes first; the notice rides behind it.
        embeds = interaction.response.send_message.call_args.kwargs["embeds"]
        self.assertEqual(embeds[0].title, "Some other command")

    async def test_a_reply_with_no_notice_carries_no_view(self):
        """A server with nothing going on must not start getting empty views
        bolted onto every reply."""
        interaction = FakeInteraction(BOB, self.db)
        await respond(interaction, self.db, embed=discord.Embed(title="Some other command"))
        self.assertIsNone(interaction.response.send_message.call_args.kwargs.get("view"))

    async def test_a_notice_is_shown_once(self):
        """Inherited from the notice feed, and the reason /bet status carries
        the same buttons: this delivery is a nudge, not the way in."""
        await self.open(ALICE, amount=10.0)
        for expected_view in (True, False):
            interaction = FakeInteraction(BOB, self.db)
            await respond(interaction, self.db, embed=discord.Embed(title="x"))
            has_view = interaction.response.send_message.call_args.kwargs.get("view") is not None
            self.assertEqual(has_view, expected_view)


class EmbedBudgetTests(BettingTestCase):
    """/bet status has to fit Discord's embed limits on the busiest board the
    feature can produce - every slot filled with a maximum-length prediction,
    on a server with a long animated currency emoji.

    Discord rejects an over-length embed outright rather than truncating it, so
    this is the difference between a crowded page and a command that does not
    work at all. Same worst case, same reasoning, as
    tests/test_player_market.py: EmbedBudgetTests.

    Two constants bound this page and neither can be raised without re-running
    it: MAX_OPEN_BETS, which is how many fields the list can have, and
    MAX_PREDICTION_LENGTH, which is how long each one is.
    """

    LONG_EMOJI = "<a:SuperLongAnimatedCurrencyName:1533722773418016868>"
    MAX_EMBED_LENGTH = 6000
    MAX_EMBED_FIELDS = 25

    async def test_a_full_board_fits(self):
        await self.db.execute(
            "UPDATE server_config SET currency_emoji = ? WHERE guild_id = ?",
            (self.LONG_EMOJI, GUILD),
        )
        await adjust_currency_balance(self.db, GUILD, ALICE, 10 ** 9)
        for n in range(MAX_OPEN_BETS):
            # Markdown throughout, since escaping it is what makes a prediction
            # longer on screen than it is in the database.
            await self.open(
                ALICE,
                prediction=("*" * MAX_PREDICTION_LENGTH)[:MAX_PREDICTION_LENGTH],
                amount=10 ** 6,
            )
        interaction = await self.status(BOB)

        embed = interaction.embed
        self.assertLessEqual(len(embed), self.MAX_EMBED_LENGTH, f"renders to {len(embed)}")
        self.assertLessEqual(len(embed.fields), self.MAX_EMBED_FIELDS)

    async def test_one_bets_card_fits(self):
        await self.db.execute(
            "UPDATE server_config SET currency_emoji = ? WHERE guild_id = ?",
            (self.LONG_EMOJI, GUILD),
        )
        await adjust_currency_balance(self.db, GUILD, ALICE, 10 ** 9)
        await adjust_currency_balance(self.db, GUILD, BOB, 10 ** 9)
        await self.open(ALICE, prediction="*" * MAX_PREDICTION_LENGTH, amount=10 ** 6)
        bet_id = await self.newest_bet_id()
        await self.place(BOB, bet_id, AGAINST, 10 ** 6)
        interaction = await self.status(CARA, bet_id)

        self.assertLessEqual(len(interaction.embed), self.MAX_EMBED_LENGTH)


class PermissionTests(unittest.TestCase):
    """The settlement commands are gated on Manage Server, the same permission
    every /setup subcommand needs. Asserted on the command objects rather than
    exercised through a callback, because the check lives on the wrapper that
    the callback shims in this file deliberately bypass."""

    def _checked(self, command):
        return any(
            getattr(check, "__qualname__", "").startswith("has_permissions")
            for check in command.checks
        )

    def test_resolve_and_cancel_require_manage_server(self):
        for command in (BettingCog.bet_resolve, BettingCog.bet_cancel):
            with self.subTest(command=command.name):
                self.assertTrue(command.checks, f"/bet {command.name} has no checks at all")
                self.assertTrue(self._checked(command))

    def test_placing_a_bet_does_not(self):
        for command in (BettingCog.bet_open, BettingCog.bet_place, BettingCog.bet_status):
            with self.subTest(command=command.name):
                self.assertFalse(self._checked(command))


if __name__ == "__main__":
    unittest.main()
