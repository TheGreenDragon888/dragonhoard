"""
tests/test_ops_dashboard.py

The Ops dashboard's payload (web/queries.py), which otherwise has no test at
all. This file exists for one reason: the "/ 1,000,000" denominator that
utils/mining_pool.py deliberately refuses to show a player was reintroduced
here once already, on a surface nobody was testing.

So this mirrors tests/test_mining_focus.py's
test_it_does_not_quote_a_fraction_of_the_bag_size against the dashboard, and
asserts the two rules a pool that can exceed one bag has to obey: the label
quotes no denominator, and the percentage that drives a progress bar - which
.progress clips at full anyway - never runs past 100.

Discord name resolution is stubbed out. web/directory.py falls through to a
live REST call for any id not in directory.json, and a unit test has no
business making one.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from database.db import Database
from data.materials import MINING_POOL_BAG_SIZE
from utils.db_helpers import (
    adjust_currency_balance,
    circulating_currency_for,
    ensure_server_row,
    ensure_user_row,
)
from utils.betting import FOR, cancel_bet, closes_at_text, fetch_bet, open_bet, place_wager
from utils.mining_pool import refill_pool
from utils.production_ledger import record_mined, record_output, smelting_inputs
from web import queries

GUILD = 4242
USER = 77


class _StubDirectory:
    """web/directory.py's surface, resolving every id offline."""

    def reload(self):
        pass

    def warm(self, guild_ids, user_ids, channel_ids):
        pass

    def guild_name(self, guild_id):
        return f"Server {guild_id}"

    def user_name(self, user_id):
        return f"user_{user_id}"

    def channel_name(self, channel_id):
        return None if channel_id is None else f"#channel-{channel_id}"


class PoolPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "test.db")
        self.db = Database(self.path)
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)
        # Two bags, so the pool holds more than MINING_POOL_BAG_SIZE - the
        # state that made the old label read "2,000,000 / 1,000,000" and the
        # old percentage read 200 into a bar that stops at 100.
        async with self.db.transaction() as tx:
            await refill_pool(tx, GUILD)
            await refill_pool(tx, GUILD)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    def payload(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with mock.patch.object(queries, "directory", _StubDirectory()):
                return queries.build_payload(conn)
        finally:
            conn.close()

    async def test_the_pool_label_quotes_no_denominator(self):
        # The payload keys servers by string id, for the JSON the browser gets.
        server = self.payload()["servers"][str(GUILD)]
        self.assertEqual(server["pool_remaining_raw"], MINING_POOL_BAG_SIZE * 2)
        self.assertIn(f"{MINING_POOL_BAG_SIZE * 2:,}", server["poolLabel"])
        self.assertNotIn(f"/ {MINING_POOL_BAG_SIZE:,}", server["poolLabel"])

    async def test_the_pool_percentage_stops_at_a_full_bar(self):
        server = self.payload()["servers"][str(GUILD)]
        self.assertEqual(server["poolPct"], 100)


class EscrowPayloadTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard's circulating figure has to count escrowed currency, or a
    server looks like it burned money it merely staked.

    This is the second reader of circulating_currency - /economy status is the
    other - and the pair disagreeing is exactly what that shared function
    exists to prevent, so what is pinned here is that the dashboard agrees with
    the bot rather than that it prints any particular number.
    """

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "test.db")
        self.db = Database(self.path)
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)
        await adjust_currency_balance(self.db, GUILD, USER, 1_000.0)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    def payload(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with mock.patch.object(queries, "directory", _StubDirectory()):
                return queries.build_payload(conn)
        finally:
            conn.close()

    async def open_bet_staking(self, cents: int):
        async with self.db.transaction() as tx:
            bet_id = await open_bet(
                tx, GUILD, USER, "Will it rain?", closes_at_text(24)
            )
            bet = await fetch_bet(tx, bet_id)
            await place_wager(tx, bet, USER, FOR, cents)
        return bet_id

    async def test_a_running_bet_does_not_shrink_the_money_supply(self):
        before = self.payload()["servers"][str(GUILD)]["circulating_raw"]
        await self.open_bet_staking(25_000)   # 250.00 staked
        after = self.payload()["servers"][str(GUILD)]["circulating_raw"]

        self.assertAlmostEqual(after, before, places=6)
        # And the balance really did move, so the figure above is the escrow
        # term doing the work rather than nothing having happened.
        row = await self.db.fetchone(
            "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        )
        self.assertAlmostEqual(row["balance"], 750.0, places=6)

    async def test_it_matches_what_the_bot_reports(self):
        await self.open_bet_staking(12_345)
        self.assertAlmostEqual(
            self.payload()["servers"][str(GUILD)]["circulating_raw"],
            await circulating_currency_for(self.db, GUILD),
            places=9,
        )

    async def test_a_settled_bet_stops_being_counted_as_escrow(self):
        bet_id = await self.open_bet_staking(25_000)
        during = self.payload()["servers"][str(GUILD)]["circulating_raw"]
        async with self.db.transaction() as tx:
            await cancel_bet(tx, await fetch_bet(tx, bet_id))

        self.assertAlmostEqual(
            self.payload()["servers"][str(GUILD)]["circulating_raw"], during, places=6
        )


class GdpPayloadTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard's copy of /economy's headline figure.

    It is a second reader of the same table, which is exactly how two numbers
    that are supposed to be the same start disagreeing - so what is pinned
    here is that it uses the bot's own definitions (the windows and
    GDP_SOURCES from utils/production_ledger.py) rather than a lookalike
    computed in web/queries.py.
    """

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "test.db")
        self.db = Database(self.path)
        await self.db.init_schema()
        await ensure_server_row(self.db, GUILD)
        await ensure_user_row(self.db, USER)

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    def payload(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with mock.patch.object(queries, "directory", _StubDirectory()):
                return queries.build_payload(conn)
        finally:
            conn.close()

    async def test_a_server_with_no_ledger_rows_reports_zero_and_says_why(self):
        """Which is every server on the day this ships - the ledger has no
        history to backfill, so a run of noughts is the correct answer and the
        note is what stops it reading as a broken query."""
        server = self.payload()["servers"][str(GUILD)]
        self.assertEqual(server["gdp"]["day_raw"], 0)
        self.assertIsNone(server["gdp"]["tracked_since"])
        self.assertIn("Nothing recorded yet", server["gdp"]["note"])

    async def test_it_reports_value_added_the_same_way_economy_does(self):
        async with self.db.transaction() as tx:
            await record_mined(tx, GUILD, {"iron_ore": 100})              # 1.00
            await record_output(tx, GUILD, "furnace", "iron", 10,
                                smelting_inputs("iron", 10))              # 0.20

        server = self.payload()["servers"][str(GUILD)]
        self.assertAlmostEqual(server["gdp"]["day_raw"], 1.20, places=6)
        self.assertAlmostEqual(server["gdp"]["week_raw"], 1.20, places=6)
        self.assertIsNotNone(server["gdp"]["tracked_since"])

    async def test_gemstones_and_non_gdp_sources_are_left_out(self):
        """The same two exclusions the embed makes. A dashboard that summed a
        diamond would report half a million for a server that mined one."""
        async with self.db.transaction() as tx:
            await record_mined(tx, GUILD, {"diamond": 1})
            await record_output(tx, GUILD, "scrapper", "copper", 6)

        self.assertEqual(self.payload()["servers"][str(GUILD)]["gdp"]["day_raw"], 0)

