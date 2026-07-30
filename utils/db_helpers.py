"""
utils/db_helpers.py

Shared database accessors used by multiple cogs. Every cog was carrying its
own identical copies of these (get/adjust inventory quantities, server stock,
currency balances, fee burning) - they live here once instead.

Every function here takes an `_Executor`, which is either a Database (each
statement standing alone) or a Transaction (all of them committing together).
Pass a Transaction whenever the caller reads a value and then writes based on
it - see Database.transaction for why that matters.
"""
import config
from database.db import Database, InsufficientQuantity, _Executor
from data.materials import get_material_info


async def ensure_user_row(db: _Executor, user_id: int):
    await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


async def ensure_server_row(db: _Executor, guild_id: int):
    # Fees are inserted explicitly rather than left to the schema DEFAULTs:
    # a database created before a default changed keeps its old column
    # DEFAULT forever, so relying on it would give new servers stale fees.
    await db.execute(
        "INSERT OR IGNORE INTO server_config (guild_id, furnace_fee, factory_fee) VALUES (?, ?, ?)",
        (guild_id, config.DEFAULT_FURNACE_FEE, config.DEFAULT_FACTORY_FEE),
    )


async def get_user_quantity(db: _Executor, user_id: int, material_id: str) -> int:
    row = await db.fetchone(
        "SELECT quantity FROM user_materials WHERE user_id = ? AND material_id = ?",
        (user_id, material_id),
    )
    return row["quantity"] if row else 0


async def adjust_user_quantity(db: _Executor, user_id: int, material_id: str, delta: int):
    await db.execute(
        """
        INSERT INTO user_materials (user_id, material_id, quantity) VALUES (?, ?, ?)
        ON CONFLICT (user_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity
        """,
        (user_id, material_id, delta),
    )


async def deduct_user_quantity(db: _Executor, user_id: int, material_id: str, amount: int):
    """Takes materials out of an inventory, refusing to take more than is
    there. Use this rather than a negative adjust_user_quantity anywhere the
    amount came from a validated read.

    The guard is in the WHERE clause, so "do they have enough" and "take it"
    are one statement and nothing can change the quantity in between. It's a
    backstop, not the primary defence - the caller should already have checked
    inside the same transaction - but it means a future regression aborts the
    operation instead of quietly minting materials out of a negative balance.
    """
    if amount <= 0:
        return
    changed = await db.execute_changes(
        "UPDATE user_materials SET quantity = quantity - ? "
        "WHERE user_id = ? AND material_id = ? AND quantity >= ?",
        (amount, user_id, material_id, amount),
    )
    if not changed:
        raise InsufficientQuantity(
            f"user {user_id} does not have {amount}x {material_id}"
        )


async def deduct_server_stock(db: _Executor, guild_id: int, material_id: str, amount: int):
    """The server-side counterpart to deduct_user_quantity - stops the market
    selling stock it doesn't actually hold."""
    if amount <= 0:
        return
    changed = await db.execute_changes(
        "UPDATE server_material_storage SET quantity = quantity - ? "
        "WHERE guild_id = ? AND material_id = ? AND quantity >= ?",
        (amount, guild_id, material_id, amount),
    )
    if not changed:
        raise InsufficientQuantity(
            f"guild {guild_id} does not have {amount}x {material_id} in stock"
        )


async def deduct_currency_balance(db: _Executor, guild_id: int, user_id: int, amount: float):
    """Charges a user, refusing to overdraw them."""
    if amount <= 0:
        return
    changed = await db.execute_changes(
        "UPDATE server_currency_balances SET balance = balance - ? "
        "WHERE guild_id = ? AND user_id = ? AND balance >= ?",
        (amount, guild_id, user_id, amount),
    )
    if not changed:
        raise InsufficientQuantity(
            f"user {user_id} cannot afford {amount} in guild {guild_id}"
        )


async def get_server_stock(db: _Executor, guild_id: int, material_id: str) -> int:
    row = await db.fetchone(
        "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
        (guild_id, material_id),
    )
    return row["quantity"] if row else 0


async def adjust_server_stock(db: _Executor, guild_id: int, material_id: str, delta: int):
    await db.execute(
        """
        INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)
        ON CONFLICT (guild_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity
        """,
        (guild_id, material_id, delta),
    )


async def get_currency_balance(db: _Executor, guild_id: int, user_id: int) -> float:
    row = await db.fetchone(
        "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return row["balance"] if row else 0.0


async def adjust_currency_balance(db: _Executor, guild_id: int, user_id: int, delta: float):
    await db.execute(
        """
        INSERT INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, ?)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET balance = balance + excluded.balance
        """,
        (guild_id, user_id, delta),
    )


async def record_minted(db: _Executor, guild_id: int, amount: float):
    await db.execute(
        "UPDATE server_config SET currency_minted_total = currency_minted_total + ? WHERE guild_id = ?",
        (amount, guild_id),
    )


async def record_burned(db: _Executor, guild_id: int, amount: float):
    await db.execute(
        "UPDATE server_config SET currency_burned_total = currency_burned_total + ? WHERE guild_id = ?",
        (amount, guild_id),
    )


async def charge_user_fee(db: _Executor, guild_id: int, user_id: int, amount: float):
    """Deducts an infrastructure fee from a user's balance. Fees are a currency
    sink (docs/market.md section 1/4) - the amount leaves circulation entirely
    rather than moving to another balance.

    Raises InsufficientQuantity if the user can't cover it, which aborts the
    surrounding transaction. It used to clamp the balance to zero instead, so a
    fee charged against too small a balance silently burned less than it
    recorded, drifting currency_burned_total away from the currency that
    actually left circulation."""
    if amount <= 0:
        return
    await db.execute(
        "INSERT OR IGNORE INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, 0.0)",
        (guild_id, user_id),
    )
    await deduct_currency_balance(db, guild_id, user_id, amount)
    await record_burned(db, guild_id, amount)


def build_recipe_lines(recipes: dict) -> list[str]:
    """One display line per recipe: the product's emoji and name, followed by
    each input's emoji and quantity. Shared by /furnace status and /factory
    status."""
    lines = []
    for material_id, recipe in recipes.items():
        info = get_material_info(material_id)
        emoji = info["emoji"] if info else "❓"
        name = info["name"] if info else material_id
        costs = []
        for input_id, qty in recipe.get("inputs", {}).items():
            input_info = get_material_info(input_id)
            input_emoji = input_info["emoji"] if input_info else "❓"
            costs.append(f"{input_emoji} {qty}")
        lines.append(f"{emoji} {name} - {' , '.join(costs)}")
    return lines
