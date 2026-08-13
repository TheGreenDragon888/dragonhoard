"""
Tests for scripts/revert_gem_sales.py, the one-time repair for gemstone sales
made before 1.2 pulled gemstones out of the market.

Worth real tests despite being a throwaway script: it runs ONCE, against live
player data, and there is no undo. Every assertion here is something that would
be discovered too late otherwise.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.revert_gem_sales import (
    BALANCE_THRESHOLD,
    REFUND_MATERIALS,
    apply,
    gem_stock,
    rows_to_revert,
)

GUILD = 500
OTHER_GUILD = 600
SELLER = 1
INNOCENT = 2

SCHEMA = Path(__file__).resolve().parent.parent / "database" / "schema.sql"


class GemRevertTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.conn = sqlite3.connect(Path(self._dir.name) / "test.db", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text())
        self.addCleanup(self.conn.close)
        for guild in (GUILD, OTHER_GUILD):
            self.conn.execute("INSERT INTO server_config (guild_id) VALUES (?)", (guild,))

    def set_balance(self, user_id, balance, guild_id=GUILD):
        self.conn.execute(
            "INSERT INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, ?)",
            (guild_id, user_id, balance),
        )

    def set_stock(self, material_id, quantity, guild_id=GUILD):
        self.conn.execute(
            "INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)",
            (guild_id, material_id, quantity),
        )

    def run_it(self):
        balances, stock = rows_to_revert(self.conn), gem_stock(self.conn)
        apply(self.conn, balances, stock)
        return balances, stock

    def balance_of(self, user_id, guild_id=GUILD):
        row = self.conn.execute(
            "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return row["balance"] if row else None

    def held(self, user_id, material_id):
        row = self.conn.execute(
            "SELECT quantity FROM user_materials WHERE user_id = ? AND material_id = ?",
            (user_id, material_id),
        ).fetchone()
        return row["quantity"] if row else 0


class SelectionTests(GemRevertTestCase):
    def test_it_catches_a_balance_over_the_threshold(self):
        self.set_balance(SELLER, 5500.0)
        self.assertEqual([r["user_id"] for r in rows_to_revert(self.conn)], [SELLER])

    def test_it_leaves_an_ordinary_balance_alone(self):
        # The whole rest of the game: a full day's job board pays a little over
        # 1.00, so a real player's balance is single digits.
        self.set_balance(INNOCENT, 4.29)
        self.assertEqual(rows_to_revert(self.conn), [])

    def test_the_threshold_is_exclusive(self):
        self.set_balance(INNOCENT, BALANCE_THRESHOLD)
        self.assertEqual(rows_to_revert(self.conn), [])

    def test_it_finds_gemstone_stock_but_not_ore_stock(self):
        self.set_stock("ruby", 2)
        self.set_stock("iron_ore", 400)
        found = {(r["material_id"], r["quantity"]) for r in gem_stock(self.conn)}
        self.assertEqual(found, {("ruby", 2)})

    def test_it_spans_every_server(self):
        self.set_balance(SELLER, 5500.0, guild_id=GUILD)
        self.set_balance(SELLER, 9000.0, guild_id=OTHER_GUILD)
        self.assertEqual(len(rows_to_revert(self.conn)), 2)


class ApplyTests(GemRevertTestCase):
    def test_it_zeroes_the_balance_and_refunds_the_gem(self):
        self.set_balance(SELLER, 5500.0)
        self.run_it()
        self.assertEqual(self.balance_of(SELLER), 0.0)
        for material_id, quantity in REFUND_MATERIALS.items():
            self.assertEqual(self.held(SELLER, material_id), quantity)

    def test_the_refund_adds_to_gems_they_already_hold(self):
        # They may well have mined more since. Overwriting rather than adding
        # would quietly confiscate those.
        self.conn.execute("INSERT INTO users (user_id) VALUES (?)", (SELLER,))
        self.conn.execute(
            "INSERT INTO user_materials (user_id, material_id, quantity) VALUES (?, 'ruby', 3)",
            (SELLER,),
        )
        self.set_balance(SELLER, 5500.0)
        self.run_it()
        self.assertEqual(self.held(SELLER, "ruby"), 3 + REFUND_MATERIALS["ruby"])

    def test_an_untouched_player_keeps_everything(self):
        self.set_balance(SELLER, 5500.0)
        self.set_balance(INNOCENT, 4.29)
        self.run_it()
        self.assertEqual(self.balance_of(INNOCENT), 4.29)
        self.assertEqual(self.held(INNOCENT, "ruby"), 0)

    def test_the_removed_currency_is_recorded_as_burned(self):
        # docs/market.md section 4's faucet/sink ledger is only meaningful if
        # every removal goes through it - currency vanishing off the books
        # would read as a bug in the market forever after.
        self.set_balance(SELLER, 5500.0)
        self.run_it()
        row = self.conn.execute(
            "SELECT currency_burned_total FROM server_config WHERE guild_id = ?", (GUILD,)
        ).fetchone()
        self.assertEqual(row["currency_burned_total"], 5500.0)

    def test_the_servers_gemstone_stock_is_cleared(self):
        # The server bought those with the currency being reversed, and nobody
        # can buy them back now that gemstones have left the market.
        self.set_balance(SELLER, 5500.0)
        self.set_stock("ruby", 1)
        self.run_it()
        row = self.conn.execute(
            "SELECT quantity FROM server_material_storage "
            "WHERE guild_id = ? AND material_id = 'ruby'",
            (GUILD,),
        ).fetchone()
        self.assertEqual(row["quantity"], 0)

    def test_ore_stock_is_untouched(self):
        self.set_balance(SELLER, 5500.0)
        self.set_stock("iron_ore", 400)
        self.run_it()
        row = self.conn.execute(
            "SELECT quantity FROM server_material_storage "
            "WHERE guild_id = ? AND material_id = 'iron_ore'",
            (GUILD,),
        ).fetchone()
        self.assertEqual(row["quantity"], 400)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        # It acts on balances above the threshold and on gemstone stock; after
        # a successful run there is neither. That is what stands in for a
        # run-once marker, so it had better actually hold.
        self.set_balance(SELLER, 5500.0)
        self.set_stock("ruby", 1)
        self.run_it()
        self.assertEqual(rows_to_revert(self.conn), [])
        self.assertEqual(gem_stock(self.conn), [])
        self.run_it()
        self.assertEqual(self.held(SELLER, "ruby"), REFUND_MATERIALS["ruby"])

    def test_a_balance_changing_underneath_aborts_the_whole_run(self):
        # The window between the dry run someone read and the --apply they then
        # typed is a real one. A blind "SET balance = 0" would swallow whatever
        # landed in between without saying so.
        self.set_balance(SELLER, 5500.0)
        self.set_balance(INNOCENT, 9000.0)
        balances = rows_to_revert(self.conn)
        self.conn.execute(
            "UPDATE server_currency_balances SET balance = 5600 WHERE user_id = ?", (SELLER,)
        )
        with self.assertRaises(RuntimeError):
            apply(self.conn, balances, gem_stock(self.conn))
        # Rolled back whole: the other user's balance was not quietly zeroed
        # on the way past.
        self.assertEqual(self.balance_of(SELLER), 5600.0)
        self.assertEqual(self.balance_of(INNOCENT), 9000.0)
        self.assertEqual(self.held(SELLER, "ruby"), 0)


if __name__ == "__main__":
    unittest.main()
