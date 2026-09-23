"""
tests/test_database_pool.py

The connection reuse database/db.py gained in 1.4: standalone statements
borrow a pooled connection and transactions share one of their own, where
every statement used to open and close its own. What has to stay true is
everything that was true when each statement had a fresh connection - a
failed statement can't leave state behind for the next borrower, a rolled-back
transaction can't leak into the next one, and readers still get through while
a transaction holds the write lock (which is what WAL mode is for).
"""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database.db import POOL_SIZE, Database

USER = 1


class DatabasePoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()

    async def asyncTearDown(self):
        self.db.close()
        self._dir.cleanup()

    async def test_a_statement_that_raises_does_not_poison_the_next_one(self):
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute(
                "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) "
                "VALUES (1, 1, 'not_a_machine', 'iron', 1)"
            )
        await self.db.execute("INSERT INTO users (user_id) VALUES (?)", (USER,))
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM users")
        self.assertEqual(row["n"], 1)

    async def test_a_rolled_back_transaction_leaves_nothing_behind_for_the_next(self):
        with self.assertRaises(RuntimeError):
            async with self.db.transaction() as tx:
                await tx.execute("INSERT INTO users (user_id) VALUES (?)", (USER,))
                raise RuntimeError("abort")
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT COUNT(*) AS n FROM users")
            self.assertEqual(row["n"], 0)

    async def test_a_reader_gets_through_while_a_transaction_holds_the_lock(self):
        await self.db.execute("INSERT INTO users (user_id) VALUES (?)", (USER,))
        async with self.db.transaction() as tx:
            await tx.execute("INSERT INTO users (user_id) VALUES (?)", (USER + 1,))
            # A standalone read on the pool, not the transaction: it must
            # neither block on the open write nor see its uncommitted row.
            row = await asyncio.wait_for(
                self.db.fetchone("SELECT COUNT(*) AS n FROM users"), timeout=5
            )
            self.assertEqual(row["n"], 1)

    async def test_the_pool_never_grows_past_its_size(self):
        await asyncio.gather(*(
            self.db.fetchone("SELECT 1") for _ in range(POOL_SIZE * 3)
        ))
        self.assertLessEqual(len(self.db._pool), POOL_SIZE)

    async def test_close_releases_everything_and_the_database_still_works(self):
        await self.db.fetchone("SELECT 1")
        async with self.db.transaction() as tx:
            await tx.fetchone("SELECT 1")
        self.db.close()
        self.assertEqual(self.db._pool, [])
        self.assertIsNone(self.db._tx_conn)
        # Reopens on demand rather than being dead after close.
        self.assertIsNotNone(await self.db.fetchone("SELECT 1"))


if __name__ == "__main__":
    unittest.main()
