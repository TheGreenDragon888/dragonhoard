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
from utils.db_helpers import ensure_server_row, ensure_user_row
from utils.mining_pool import refill_pool
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
