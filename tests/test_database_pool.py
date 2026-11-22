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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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


class StandaloneWriteQueueTests(unittest.IsolatedAsyncioTestCase):
    """Standalone writes wait for a transaction on the asyncio write lock, not
    inside an executor thread on SQLite's busy timeout.

    The 2026-11-22 incident: each one used to sit in a worker thread retrying
    SQLite's lock. When as many of them were waiting as the default executor
    had threads, the transaction holding the lock had no thread left to run its
    next statement on, so nothing moved until the busy timeout failed the
    waiting writes. Commands that only read stalled with them.

    The executor here is two threads and the busy timeout two seconds, so the
    old behaviour fails these tests in seconds instead of hanging them."""

    async def asyncSetUp(self):
        self._timeout = patch("database.db.BUSY_TIMEOUT_SECONDS", 2.0)
        self._timeout.start()
        self._executor = ThreadPoolExecutor(max_workers=2)
        asyncio.get_running_loop().set_default_executor(self._executor)
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "test.db"))
        await self.db.init_schema()
        await self.db.execute("INSERT INTO users (user_id) VALUES (?)", (USER,))

    async def asyncTearDown(self):
        self.db.close()
        self._executor.shutdown(wait=True)
        self._timeout.stop()
        self._dir.cleanup()

    async def test_writers_queued_behind_a_transaction_leave_it_a_thread(self):
        async with self.db.transaction() as tx:
            # More writers than the executor has threads, all arriving while
            # the transaction holds the lock.
            writers = [
                asyncio.create_task(
                    self.db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (USER,))
                )
                for _ in range(3)
            ]
            await asyncio.sleep(0.2)
            await asyncio.wait_for(
                tx.execute("INSERT INTO users (user_id) VALUES (?)", (USER + 1,)), timeout=1
            )
        # Each one waited its turn and then ran, rather than timing out.
        await asyncio.wait_for(asyncio.gather(*writers), timeout=5)
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM users")
        self.assertEqual(row["n"], 2)

    async def test_a_standalone_write_inside_its_own_transaction_raises(self):
        # The transaction holds the lock the write would wait for, so this
        # could only ever deadlock. It has to fail at once instead.
        async with self.db.transaction():
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(
                    self.db.execute("INSERT INTO users (user_id) VALUES (?)", (USER + 1,)),
                    timeout=1,
                )


if __name__ == "__main__":
    unittest.main()
